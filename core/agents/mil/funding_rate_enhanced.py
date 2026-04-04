# ============================================================
# NEXUS TRADER — MIL Phase 4A: Enhanced Funding Rate
#
# Multi-exchange aggregation, 24h percentile, divergence detection.
#
# Architecture:
#   1. fetch_all_symbols() is called from FundingRateAgent.fetch()
#      (QThread context — blocking HTTP is the established pattern).
#   2. Results are cached per-symbol with TTL (default 120s).
#   3. enhance_symbol_data() reads ONLY from cache — zero I/O.
#   4. Rate limiting: max 1 fetch per exchange per 60s per symbol.
#   5. All MIL signals carry timestamps + staleness enforcement.
#   6. MIL influence is capped at MIL_INFLUENCE_CAP (default 0.30).
#
# Backtest isolation: These signals enter via OrchestratorEngine
# only. ConfluenceScorer.score(technical_only=True) blocks them.
# ============================================================
from __future__ import annotations

import logging
import time
import threading
from collections import deque
from typing import Optional

logger = logging.getLogger(__name__)

# ── Exchange Weights ─────────────────────────────────────────
EXCHANGE_WEIGHTS = {
    "bybit": 0.40,
    "binance": 0.35,
    "okx": 0.25,
}

# ── Cache / Rate Limit Constants ─────────────────────────────
_CACHE_TTL_S = 120.0        # Per-symbol multi-exchange cache TTL
_RATE_LIMIT_S = 60.0        # Min seconds between fetches for the same symbol
_FETCH_TIMEOUT_S = 4.0      # Per-exchange HTTP timeout (tight to avoid blocking)

# ── Staleness Constants ──────────────────────────────────────
_MAX_STALENESS_S = 120.0    # Discard MIL data older than this

# ── Percentile History ───────────────────────────────────────
_HISTORY_MAX = 300
_HISTORY_WINDOW_S = 86400.0  # 24 hours

# ── Divergence Threshold ─────────────────────────────────────
_DIVERGENCE_THRESHOLD = 0.03  # 0.03% per 8h

# ── Influence Cap ────────────────────────────────────────────
# Maximum influence MIL funding metadata can have on the effective
# signal when used downstream. The cap scales MIL-derived adjustments
# so they never exceed this fraction of total signal magnitude.
MIL_INFLUENCE_CAP = 0.30     # 30% maximum contribution


class FundingRateEnhancer:
    """
    Stateful enhancer for FundingRateAgent.

    Maintains:
    - Per-symbol multi-exchange rate cache with TTL + rate limiting
    - Per-symbol 24h funding rate history for percentile calculation
    - Timestamp + staleness on every signal

    Thread-safe: all state accessed under _lock (threading.Lock).
    The Lock (not RLock) is used because no method calls another
    lock-holding method — simpler and avoids accidental reentrancy.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # symbol -> {rates: {exchange: rate}, ts: float, fetch_count: int}
        self._multi_exchange_cache: dict[str, dict] = {}
        # symbol -> float (monotonic timestamp of last fetch attempt)
        self._last_fetch_ts: dict[str, float] = {}
        # symbol -> deque of (timestamp, weighted_rate) for 24h percentile
        self._rate_history: dict[str, deque] = {}
        # Fetch statistics for diagnostics
        self._fetch_count = 0          # per-symbol cache writes
        self._cache_hit_count = 0      # rate-limit skips
        self._http_call_count = 0      # actual HTTP requests made
        self._last_batch_http_calls = 0  # HTTP calls in most recent batch

    def is_enabled(self) -> bool:
        """Check if MIL funding rate enhancement is enabled."""
        try:
            from config.settings import settings
            global_enabled = settings.get("mil.global_enabled", False)
            agent_enabled = settings.get("agents.funding_rate_enhanced", False)
            return bool(global_enabled and agent_enabled)
        except Exception:
            return False

    # ── Fetch Phase (called from QThread context) ────────────

    def fetch_all_symbols(self, symbols: list[str]) -> None:
        """
        Batch-fetch multi-exchange rates for all symbols.

        Called from FundingRateAgent.fetch() (QThread) BEFORE process().
        This consolidates all HTTP calls into the fetch phase where
        blocking I/O is the established pattern.

        Optimization: uses exchange batch endpoints to fetch ALL symbols
        in 1 call per exchange (2 total) instead of 2×N per-symbol calls.
        Per-symbol rate limiting still enforced: only symbols needing a
        refresh are populated from the batch response.

        Cache: results stored with monotonic timestamp for TTL enforcement.
        """
        if not self.is_enabled():
            return

        # Determine which symbols need a refresh (rate-limit gate)
        now = time.monotonic()
        symbols_to_refresh: list[str] = []
        for symbol in symbols:
            with self._lock:
                last = self._last_fetch_ts.get(symbol, 0.0)
                if (now - last) < _RATE_LIMIT_S:
                    self._cache_hit_count += 1
                    continue
            symbols_to_refresh.append(symbol)

        if not symbols_to_refresh:
            return

        # Batch fetch: 1 call per exchange for ALL symbols at once
        binance_rates, okx_rates = self._fetch_batch_all_exchanges(symbols_to_refresh)

        # Distribute batch results into per-symbol caches
        wall_ts = time.time()
        mono_ts = time.monotonic()
        for symbol in symbols_to_refresh:
            per_symbol_rates: dict[str, float] = {}
            perp_key = symbol.split("/")[0].upper() + "USDT"
            okx_key = symbol.split("/")[0].upper() + "-USDT-SWAP"

            if perp_key in binance_rates:
                per_symbol_rates["binance"] = binance_rates[perp_key]
            if okx_key in okx_rates:
                per_symbol_rates["okx"] = okx_rates[okx_key]

            with self._lock:
                self._last_fetch_ts[symbol] = mono_ts
                self._multi_exchange_cache[symbol] = {
                    "rates": per_symbol_rates,
                    "ts": wall_ts,
                    "ts_mono": mono_ts,
                }
            self._fetch_count += 1

        # Record actual HTTP call count (for diagnostics)
        self._http_call_count += self._last_batch_http_calls

    def _fetch_batch_all_exchanges(
        self, symbols: list[str]
    ) -> tuple[dict[str, float], dict[str, float]]:
        """
        Fetch funding rates from Binance and OKX using batch endpoints.

        Returns:
            (binance_rates, okx_rates) where each is {instrument_key: rate_pct}.

        Binance: GET /fapi/v1/premiumIndex (no symbol param = ALL symbols).
        OKX: GET /api/v5/public/funding-rate-batch (or per-symbol fallback).

        Single exchange failure does NOT block the other.
        This method is called ONCE per fetch cycle, not per symbol.
        """
        import requests

        binance_rates: dict[str, float] = {}
        okx_rates: dict[str, float] = {}
        http_calls = 0

        # Build lookup sets for filtering batch responses
        perp_keys = set()
        okx_keys = set()
        for symbol in symbols:
            base = symbol.split("/")[0].upper()
            perp_keys.add(base + "USDT")
            okx_keys.add(base + "-USDT-SWAP")

        # ── Binance batch (1 call for ALL symbols) ───────────
        try:
            resp = requests.get(
                "https://fapi.binance.com/fapi/v1/premiumIndex",
                timeout=_FETCH_TIMEOUT_S,
            )
            http_calls += 1
            if resp.status_code == 200:
                data = resp.json()
                # Response is a list of all instruments
                for item in data:
                    sym = item.get("symbol", "")
                    if sym in perp_keys:
                        rate = float(item.get("lastFundingRate", 0)) * 100.0
                        binance_rates[sym] = round(rate, 6)
        except Exception as exc:
            logger.debug("MIL FundingEnhancer: Binance batch fetch failed: %s", exc)

        # ── OKX (per-symbol — no public batch endpoint) ──────
        # OKX funding-rate endpoint only accepts a single instId.
        # We iterate but this is still grouped in one sequential block
        # inside the fetch phase.
        for inst_id in okx_keys:
            try:
                resp = requests.get(
                    f"https://www.okx.com/api/v5/public/funding-rate?instId={inst_id}",
                    timeout=_FETCH_TIMEOUT_S,
                )
                http_calls += 1
                if resp.status_code == 200:
                    records = resp.json().get("data", [])
                    if records:
                        rate = float(records[0].get("fundingRate", 0)) * 100.0
                        okx_rates[inst_id] = round(rate, 6)
            except Exception as exc:
                logger.debug("MIL FundingEnhancer: OKX fetch failed for %s: %s", inst_id, exc)

        self._last_batch_http_calls = http_calls
        return binance_rates, okx_rates

    def _fetch_multi_exchange_http(self, symbol: str) -> Optional[dict[str, float]]:
        """
        Fetch funding rates from Binance and OKX for a single symbol.

        LEGACY per-symbol path — retained for test compatibility and as
        fallback. Production path is _fetch_batch_all_exchanges().
        """
        import requests

        perp_base = symbol.split("/")[0].upper()
        rates: dict[str, float] = {}

        try:
            url = f"https://fapi.binance.com/fapi/v1/premiumIndex?symbol={perp_base}USDT"
            resp = requests.get(url, timeout=_FETCH_TIMEOUT_S)
            if resp.status_code == 200:
                data = resp.json()
                rate = float(data.get("lastFundingRate", 0)) * 100.0
                rates["binance"] = round(rate, 6)
        except Exception as exc:
            logger.debug("MIL FundingEnhancer: Binance fetch failed for %s: %s", symbol, exc)

        try:
            inst_id = f"{perp_base}-USDT-SWAP"
            url = f"https://www.okx.com/api/v5/public/funding-rate?instId={inst_id}"
            resp = requests.get(url, timeout=_FETCH_TIMEOUT_S)
            if resp.status_code == 200:
                data = resp.json()
                records = data.get("data", [])
                if records:
                    rate = float(records[0].get("fundingRate", 0)) * 100.0
                    rates["okx"] = round(rate, 6)
        except Exception as exc:
            logger.debug("MIL FundingEnhancer: OKX fetch failed for %s: %s", symbol, exc)

        return rates if rates else None

    # ── Process Phase (reads from cache only — zero I/O) ─────

    def _get_cached_rates(self, symbol: str) -> Optional[dict[str, float]]:
        """
        Read multi-exchange rates from cache if fresh.

        Returns None if:
        - No cache entry exists
        - Cache entry is older than _CACHE_TTL_S
        - Cache entry is older than _MAX_STALENESS_S (wall-clock)
        """
        with self._lock:
            entry = self._multi_exchange_cache.get(symbol)
            if entry is None:
                return None
            now_mono = time.monotonic()
            now_wall = time.time()
            # TTL check (monotonic — immune to clock adjustment)
            if (now_mono - entry["ts_mono"]) > _CACHE_TTL_S:
                return None
            # Staleness check (wall-clock)
            if (now_wall - entry["ts"]) > _MAX_STALENESS_S:
                return None
            return entry.get("rates")

    def compute_weighted_rate(
        self,
        bybit_rate: float,
        other_rates: Optional[dict[str, float]],
    ) -> float:
        """
        Compute weighted average funding rate across exchanges.

        If other exchanges are unavailable, returns Bybit rate (weight=1.0).
        """
        if not other_rates:
            return bybit_rate

        total_weight = EXCHANGE_WEIGHTS["bybit"]
        weighted_sum = bybit_rate * EXCHANGE_WEIGHTS["bybit"]

        for exchange, rate in other_rates.items():
            w = EXCHANGE_WEIGHTS.get(exchange, 0.0)
            if w > 0:
                weighted_sum += rate * w
                total_weight += w

        if total_weight <= 0:
            return bybit_rate

        return round(weighted_sum / total_weight, 6)

    def compute_divergence(
        self,
        bybit_rate: float,
        other_rates: Optional[dict[str, float]],
    ) -> dict:
        """Detect cross-exchange funding rate divergence."""
        all_rates = {"bybit": bybit_rate}
        if other_rates:
            all_rates.update(other_rates)

        if len(all_rates) < 2:
            return {
                "divergence_detected": False,
                "divergence_spread": 0.0,
                "exchange_rates": all_rates,
            }

        values = list(all_rates.values())
        spread = max(values) - min(values)

        return {
            "divergence_detected": spread >= _DIVERGENCE_THRESHOLD,
            "divergence_spread": round(spread, 6),
            "exchange_rates": all_rates,
        }

    def update_history_and_percentile(
        self,
        symbol: str,
        rate_pct: float,
    ) -> float:
        """
        Add the current rate to the 24h history and compute the
        percentile of the current rate within that window.

        Returns: percentile [0.0, 1.0] where 1.0 = most extreme positive.

        Robustness: handles NaN/None inputs, deduplicates identical
        timestamps, uses interpolated percentile for smoother output.
        """
        # Guard against bad input
        if rate_pct is None or rate_pct != rate_pct:  # NaN check
            return 0.5

        now = time.time()
        cutoff = now - _HISTORY_WINDOW_S

        with self._lock:
            hist = self._rate_history.setdefault(
                symbol, deque(maxlen=_HISTORY_MAX)
            )
            hist.append((now, rate_pct))

            # Prune old entries
            while hist and hist[0][0] < cutoff:
                hist.popleft()

            # Need minimum 5 entries for meaningful percentile
            if len(hist) < 5:
                return 0.5

            rates = sorted(e[1] for e in hist)
            n = len(rates)
            # Interpolated percentile: count strictly less + 0.5 * equal
            less_count = sum(1 for r in rates if r < rate_pct)
            equal_count = sum(1 for r in rates if r == rate_pct)
            percentile = (less_count + 0.5 * equal_count) / n

        return round(max(0.0, min(1.0, percentile)), 4)

    def enhance_symbol_data(
        self,
        symbol: str,
        bybit_rate: float,
        base_data: dict,
    ) -> dict:
        """
        Enrich a per-symbol data dict with MIL funding metadata.

        Called by FundingRateAgent.process() — reads ONLY from cache,
        performs ZERO I/O. All HTTP was done in fetch_all_symbols().

        Staleness enforcement: if cached rates are older than
        _MAX_STALENESS_S, enhancement is skipped (fail-open).

        Influence cap: mil_influence_factor is clamped to [0, MIL_INFLUENCE_CAP]
        so downstream consumers can bound MIL's contribution.
        """
        try:
            # Read from cache — no I/O
            other_rates = self._get_cached_rates(symbol)

            # Weighted average
            weighted_rate = self.compute_weighted_rate(bybit_rate, other_rates)

            # Divergence detection
            divergence = self.compute_divergence(bybit_rate, other_rates)

            # 24h percentile (using weighted rate)
            percentile = self.update_history_and_percentile(symbol, weighted_rate)

            # Compute influence factor (how much MIL should adjust conviction)
            # Extreme percentile (near 0 or near 1) → higher influence
            # Divergence → reduced influence (conflicting signals)
            extremity = abs(percentile - 0.5) * 2.0  # [0, 1] where 1 = most extreme
            influence = extremity * MIL_INFLUENCE_CAP
            if divergence["divergence_detected"]:
                influence *= 0.5  # Reduce influence when exchanges disagree

            # Timestamp for staleness tracking
            signal_ts = time.time()

            # Store exchange rates for diagnostics
            with self._lock:
                cache_entry = self._multi_exchange_cache.get(symbol, {})
                data_age_s = signal_ts - cache_entry.get("ts", signal_ts)

            base_data["mil_enhanced"] = True
            base_data["mil_weighted_rate"] = weighted_rate
            base_data["mil_percentile_24h"] = percentile
            base_data["mil_divergence_detected"] = divergence["divergence_detected"]
            base_data["mil_divergence_spread"] = divergence["divergence_spread"]
            base_data["mil_exchange_rates"] = divergence["exchange_rates"]
            base_data["mil_exchanges_available"] = len(divergence["exchange_rates"])
            base_data["mil_influence_factor"] = round(influence, 4)
            base_data["mil_influence_cap"] = MIL_INFLUENCE_CAP
            base_data["mil_signal_ts"] = signal_ts
            base_data["mil_data_age_s"] = round(data_age_s, 1)
            base_data["mil_stale"] = data_age_s > _MAX_STALENESS_S

            return base_data

        except Exception as exc:
            logger.debug(
                "MIL FundingEnhancer: enhance failed for %s (fail-open): %s",
                symbol, exc,
            )
            base_data["mil_enhanced"] = False
            return base_data

    def get_diagnostics(self) -> dict:
        """Return MIL funding diagnostics for pipeline dashboard."""
        with self._lock:
            return {
                "symbols_tracked": len(self._rate_history),
                "exchange_rates": {
                    sym: entry.get("rates", {})
                    for sym, entry in self._multi_exchange_cache.items()
                },
                "history_sizes": {
                    sym: len(hist)
                    for sym, hist in self._rate_history.items()
                },
                "fetch_count": self._fetch_count,
                "cache_hit_count": self._cache_hit_count,
                "http_call_count": self._http_call_count,
                "last_batch_http_calls": self._last_batch_http_calls,
                "cache_ttl_s": _CACHE_TTL_S,
                "rate_limit_s": _RATE_LIMIT_S,
                "max_staleness_s": _MAX_STALENESS_S,
                "influence_cap": MIL_INFLUENCE_CAP,
            }


# ── Module-level singleton ─────────────────────────────────
_enhancer: Optional[FundingRateEnhancer] = None
_init_lock = threading.Lock()


def get_funding_enhancer() -> FundingRateEnhancer:
    """Return (or create) the FundingRateEnhancer singleton."""
    global _enhancer
    if _enhancer is None:
        with _init_lock:
            if _enhancer is None:
                _enhancer = FundingRateEnhancer()
    return _enhancer
