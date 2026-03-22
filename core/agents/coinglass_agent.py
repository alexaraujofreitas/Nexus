"""
coinglass_agent.py
------------------
Module-level singleton that fetches Open Interest (OI) data from the
Coinglass public API v2.  Used by:

  - ``oi_signal.assess_oi_data_quality()``  – quality gate (0–3 score)
  - ``oi_signal.get_oi_modifier()``          – trend/divergence modifier

Expected return of ``get_oi_data(symbol)`` (or ``None`` on cache miss):
    {
        "oi_change_1h_pct":  float,   # % change in aggregated OI ~1 h ago
        "age_seconds":       float,   # seconds since last successful fetch
        "raw_oi_usd":        float,   # current aggregated OI across exchanges
        "source":            str,     # "coinglass" | "cached"
    }

API key is read from settings ``agents.coinglass_api_key``; if the value is
``"__vault__"`` or empty, falls back to ``key_vault.load()``.

Caching
-------
- Per-symbol TTL: 300 s (5 min) — matches CrashDetectionAgent and the
  oi_signal staleness threshold.
- OI history window: 12 buckets × 5 min = 1 h of OI values per symbol,
  so the 1h % change is computed from real fetched values rather than
  API-provided fields (which vary by endpoint version).

Thread-safety
-------------
All public methods are protected by ``_lock``.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from typing import Optional

import requests

logger = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────
_CACHE_TTL_S   = 300          # seconds between API calls per symbol
_OI_HISTORY    = 12           # buckets to keep (~1 h at 5-min cadence)
_REQUEST_TIMEOUT = 8.0        # seconds
_BASE_URL = "https://open-api.coinglass.com/public/v2/openInterest"
_HEADERS_BASE = {
    "Accept": "application/json",
    "User-Agent": "NexusTrader/1.0",
}

# Symbol map: "BTC/USDT" → "BTC"
def _to_cg_symbol(symbol: str) -> str:
    return symbol.split("/")[0].upper()


class CoinglassAgent:
    """Fetches and caches Open Interest data from Coinglass API v2."""

    def __init__(self) -> None:
        self._lock = threading.RLock()   # reentrant — build_result called inside get_oi_data lock
        # symbol → {"oi_usd": float, "ts": float}  (latest fetch)
        self._cache: dict[str, dict] = {}
        # symbol → deque of (timestamp, oi_usd) for 1h change calc
        self._history: dict[str, deque] = {}
        self._api_key: Optional[str] = None
        self._api_key_loaded = False
        self._warned_no_key = False

    # ── Public API ─────────────────────────────────────────────────────────

    def get_oi_data(self, symbol: str) -> Optional[dict]:
        """Return OI data dict for *symbol*, or ``None`` if unavailable.

        Returns cached data if within TTL, otherwise triggers a fresh fetch.

        Dict keys:
            oi_change_1h_pct  – % change vs value ~1h ago (0.0 if not enough history)
            age_seconds       – seconds since last successful fetch
            raw_oi_usd        – latest aggregated OI in USD
            source            – "coinglass" | "cached"
        """
        with self._lock:
            cached = self._cache.get(symbol)
            if cached and (time.time() - cached["ts"]) < _CACHE_TTL_S:
                return self._build_result(symbol, cached, source="cached")

        # Outside lock — network call
        result = self._fetch(symbol)
        if result is None:
            # Return stale cache rather than nothing (oi_signal handles staleness)
            with self._lock:
                cached = self._cache.get(symbol)
                if cached:
                    return self._build_result(symbol, cached, source="cached")
            return None
        return result

    # ── Internal ───────────────────────────────────────────────────────────

    def _load_api_key(self) -> str:
        """Read Coinglass API key from settings / vault (cached after first call)."""
        if self._api_key_loaded:
            return self._api_key or ""
        try:
            from config.settings import settings
            key = settings.get("agents.coinglass_api_key", "")
            if not key or key == "__vault__":
                from core.security.key_vault import key_vault
                key = key_vault.load("agents.coinglass_api_key") or ""
            self._api_key = key
        except Exception as exc:
            logger.debug("CoinglassAgent: key load error: %s", exc)
            self._api_key = ""
        self._api_key_loaded = True
        return self._api_key or ""

    def _fetch(self, symbol: str) -> Optional[dict]:
        """Call Coinglass API and update cache + history."""
        api_key = self._load_api_key()
        if not api_key:
            if not self._warned_no_key:
                logger.warning(
                    "CoinglassAgent: no API key — OI modifiers suppressed. "
                    "Set agents.coinglass_api_key in Settings."
                )
                self._warned_no_key = True
            return None

        cg_sym = _to_cg_symbol(symbol)
        headers = {**_HEADERS_BASE, "coinglassSecret": api_key}
        params = {"symbol": cg_sym}

        try:
            resp = requests.get(
                _BASE_URL, headers=headers, params=params,
                timeout=_REQUEST_TIMEOUT
            )
            resp.raise_for_status()
            payload = resp.json()
        except requests.exceptions.Timeout:
            logger.debug("CoinglassAgent: timeout fetching OI for %s", symbol)
            return None
        except requests.exceptions.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else "?"
            logger.debug("CoinglassAgent: HTTP %s for %s", status, symbol)
            return None
        except Exception as exc:
            logger.debug("CoinglassAgent: fetch error for %s: %s", symbol, exc)
            return None

        # Parse aggregated OI across all exchanges
        oi_usd = self._parse_oi(payload, cg_sym)
        if oi_usd is None:
            logger.debug("CoinglassAgent: could not parse OI from response for %s", symbol)
            return None

        now = time.time()
        with self._lock:
            # Update history
            hist = self._history.setdefault(symbol, deque(maxlen=_OI_HISTORY))
            hist.append((now, oi_usd))

            # Update cache
            self._cache[symbol] = {"oi_usd": oi_usd, "ts": now}
            cached = self._cache[symbol]

        logger.debug("CoinglassAgent: %s OI=%.2fM USD", symbol, oi_usd / 1e6)
        return self._build_result(symbol, cached, source="coinglass")

    def _parse_oi(self, payload: dict, cg_sym: str) -> Optional[float]:
        """Extract total aggregated OI in USD from Coinglass v2 response."""
        try:
            code = str(payload.get("code", "")).strip()
            if code not in ("0", "00000", ""):
                return None
            data = payload.get("data") or []
            if not data:
                return None

            # data is a list of per-exchange entries
            total = 0.0
            found = False
            for entry in data:
                if not isinstance(entry, dict):
                    continue
                # openInterest is denominated in contracts; openInterestAmount in USD
                oi_val = (
                    entry.get("openInterestAmount")
                    or entry.get("openInterest")
                    or 0.0
                )
                try:
                    total += float(oi_val)
                    found = True
                except (TypeError, ValueError):
                    pass
            return total if found else None
        except Exception:
            return None

    def _build_result(self, symbol: str, cached: dict, source: str) -> dict:
        """Build the result dict with 1h % change from history."""
        now = time.time()
        oi_now = cached["oi_usd"]
        fetch_ts = cached["ts"]
        age_s = now - fetch_ts

        # Compute 1h % change from history
        oi_change_1h_pct = 0.0
        with self._lock:
            hist = self._history.get(symbol)
            if hist and len(hist) >= 2:
                # Find the entry closest to 1 hour ago
                target_ts = now - 3600.0
                best = min(hist, key=lambda x: abs(x[0] - target_ts))
                oi_old = best[1]
                if oi_old and oi_old != 0:
                    oi_change_1h_pct = ((oi_now - oi_old) / abs(oi_old)) * 100.0

        return {
            "oi_change_1h_pct": round(oi_change_1h_pct, 4),
            "age_seconds": round(age_s, 1),
            "raw_oi_usd": round(oi_now, 2),
            "source": source,
        }


# ── Module-level singleton ─────────────────────────────────────────────────
coinglass_agent: Optional[CoinglassAgent] = None
_init_lock = threading.Lock()


def _get_coinglass_agent() -> CoinglassAgent:
    """Return (or create) the module-level CoinglassAgent singleton."""
    global coinglass_agent
    if coinglass_agent is None:
        with _init_lock:
            if coinglass_agent is None:
                coinglass_agent = CoinglassAgent()
                logger.info("CoinglassAgent: singleton initialized")
    return coinglass_agent


# Eagerly instantiate so `from core.agents.coinglass_agent import coinglass_agent`
# always gets a live object, not None.
_get_coinglass_agent()
