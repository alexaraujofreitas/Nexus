# ============================================================
# NEXUS TRADER — Watchlist Gate  (Sprint 14)
#
# Provides a cached, thread-safe accessor for the active watchlist symbols.
# All agents call get_watchlist_symbols() instead of hard-coding symbols.
# Enforces the single-user watchlist boundary at all data ingestion points.
# ============================================================
from __future__ import annotations

import logging
import threading
import time
from typing import Optional

logger = logging.getLogger(__name__)

_DEFAULT_SYMBOLS = ["BTC/USDT", "ETH/USDT"]
_CACHE_TTL_SECONDS = 60  # refresh watchlist every 60s

_lock = threading.RLock()
_cached_symbols: list[str] = []
_last_refresh: float = 0.0


def get_watchlist_symbols(fallback: Optional[list[str]] = None) -> list[str]:
    """
    Return active watchlist symbols (cached for 60s).
    Thread-safe. Falls back to DEFAULT if watchlist is empty or error.
    """
    global _cached_symbols, _last_refresh

    now = time.monotonic()
    with _lock:
        if now - _last_refresh < _CACHE_TTL_SECONDS and _cached_symbols:
            return list(_cached_symbols)

    try:
        from core.scanning.watchlist import WatchlistManager
        wm = WatchlistManager()
        symbols = wm.get_active_symbols()
        if symbols:
            with _lock:
                _cached_symbols = symbols
                _last_refresh = now
            return list(symbols)
    except Exception as exc:
        logger.debug("WatchlistGate: failed to load watchlist — %s", exc)

    default = fallback or _DEFAULT_SYMBOLS
    with _lock:
        if not _cached_symbols:
            _cached_symbols = default
    return list(_cached_symbols or default)


def get_base_symbols(watchlist_symbols: Optional[list[str]] = None) -> list[str]:
    """
    Convert watchlist symbols like 'BTC/USDT' to base assets like 'bitcoin'.
    Used for CoinGecko API calls.
    """
    syms = watchlist_symbols or get_watchlist_symbols()
    _COINGECKO_MAP = {
        "BTC": "bitcoin", "ETH": "ethereum", "SOL": "solana",
        "BNB": "binancecoin", "ADA": "cardano", "XRP": "ripple",
        "DOGE": "dogecoin", "DOT": "polkadot", "AVAX": "avalanche-2",
        "MATIC": "matic-network", "LINK": "chainlink", "UNI": "uniswap",
        "LTC": "litecoin", "BCH": "bitcoin-cash", "ATOM": "cosmos",
        "TRX": "tron", "TON": "the-open-network", "SUI": "sui",
        "APT": "aptos", "ARB": "arbitrum",
        "HYPE": "hyperliquid", "XLM": "stellar", "HBAR": "hedera-hashgraph",
        "NEAR": "near", "ICP": "internet-computer", "ONDO": "ondo-finance",
        "ALGO": "algorand", "RENDER": "render-token",
    }
    result = []
    for sym in syms:
        base = sym.split("/")[0].upper()
        cg_id = _COINGECKO_MAP.get(base)
        if cg_id and cg_id not in result:
            result.append(cg_id)
    return result or ["bitcoin", "ethereum"]


def is_symbol_in_watchlist(symbol: str) -> bool:
    """Check if a symbol is in the active watchlist."""
    active = get_watchlist_symbols()
    return symbol.upper() in [s.upper() for s in active]


def get_api_call_counter() -> "APICallCounter":
    """Return the global API call counter singleton."""
    return _api_counter


class APICallCounter:
    """
    Tracks API calls per data source per hour.
    Used by SystemHealth page to display rate usage.
    """

    def __init__(self):
        self._lock = threading.RLock()
        self._counts: dict[str, list[float]] = {}  # source → list of timestamps

    def record(self, source: str) -> None:
        """Record an API call for the given source."""
        now = time.monotonic()
        with self._lock:
            if source not in self._counts:
                self._counts[source] = []
            self._counts[source].append(now)
            # Keep only last hour
            cutoff = now - 3600
            self._counts[source] = [t for t in self._counts[source] if t > cutoff]

    def get_hourly_counts(self) -> dict[str, int]:
        """Return calls-per-hour for each source."""
        now = time.monotonic()
        cutoff = now - 3600
        with self._lock:
            return {
                src: len([t for t in times if t > cutoff])
                for src, times in self._counts.items()
            }

    def get_rate_per_minute(self, source: str) -> float:
        """Return calls per minute for a specific source (last 5 min)."""
        now = time.monotonic()
        cutoff = now - 300
        with self._lock:
            times = self._counts.get(source, [])
            recent = [t for t in times if t > cutoff]
            return len(recent) / 5.0


# Global singleton
_api_counter = APICallCounter()
