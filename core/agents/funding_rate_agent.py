# ============================================================
# NEXUS TRADER — Funding Rate & Open Interest Agent  (Sprint 1)
#
# Polls CCXT for perpetual futures funding rates and open interest
# for all active watchlist symbols.
#
# Signal logic (CONTRARIAN — extreme funding signals reversals):
#   funding > +0.10%/8h  → signal = -0.85  (overleveraged longs, fade)
#   funding +0.05→+0.10  → signal = -0.45
#   funding -0.02→+0.05  → signal =  0.0   (neutral)
#   funding -0.05→-0.02  → signal = +0.45  (crowded shorts, potential squeeze)
#   funding < -0.05%/8h  → signal = +0.85  (extreme short squeeze setup)
#
# OI confirmation:
#   Rising OI + extreme funding  → amplify signal × 1.3
#   Falling OI                   → reduce confidence × 0.5
#
# Publishes: Topics.FUNDING_RATE_UPDATED
# Data:      {symbol, rate_pct, oi_usdt, oi_change_pct,
#             signal, confidence, direction, explanation}
# ============================================================
from __future__ import annotations

import logging
import threading
import time
from typing import Any

from core.agents.base_agent import BaseAgent
from core.event_bus import Topics
from core.scanning.watchlist_gate import get_watchlist_symbols, get_api_call_counter

logger = logging.getLogger(__name__)

# Funding rate thresholds (% per 8-hour period)
_EXTREME_LONG   =  0.10   # extreme positive funding → short signal
_HIGH_LONG      =  0.05
_HIGH_SHORT     = -0.05   # extreme negative funding → long signal
_MODERATE_SHORT = -0.02

# OI change thresholds (% change over last poll)
_OI_RISING_THRESHOLD  = +2.0   # % OI increase = trend confirming
_OI_FALLING_THRESHOLD = -2.0   # % OI decrease = trend weakening

_POLL_SECONDS = 300   # 5 minutes — funding rates update every 8h but checking often keeps data fresh


class FundingRateAgent(BaseAgent):
    """
    Monitors funding rates and open interest for perpetual futures.

    Maintains a per-symbol cache that the FundingRateModel sub-model
    reads during scan cycles.  Also publishes events for the UI.
    """

    def __init__(self, parent=None):
        super().__init__("funding_rate", parent)
        # symbol → {rate_pct, oi_usdt, prev_oi_usdt, signal, confidence, ...}
        self._cache: dict[str, dict] = {}
        self._lock  = threading.RLock()
        # symbol → list of {timestamp, rate_pct} — rolling 24h history
        self._rate_history: dict[str, list[dict]] = {}

    # ── BaseAgent interface ────────────────────────────────────

    @property
    def event_topic(self) -> str:
        return Topics.FUNDING_RATE_UPDATED

    @property
    def poll_interval_seconds(self) -> int:
        return _POLL_SECONDS

    def fetch(self) -> dict[str, dict]:
        """Fetch funding rates + OI for all watchlist symbols."""
        from core.market_data.exchange_manager import exchange_manager

        exchange = exchange_manager.get_exchange()
        if exchange is None:
            raise RuntimeError("No exchange connected")

        symbols = get_watchlist_symbols()  # Use watchlist-gated symbols
        if not symbols:
            return {}

        # ── MIL Phase 4A: Pre-fetch multi-exchange rates ────────
        # Batch-fetch Binance+OKX rates for all symbols DURING the
        # fetch phase (QThread context — blocking HTTP is established
        # pattern). This ensures enhance_symbol_data() in process()
        # reads ONLY from cache (zero I/O).
        try:
            from core.agents.mil.funding_rate_enhanced import get_funding_enhancer
            _enhancer = get_funding_enhancer()
            if _enhancer.is_enabled():
                _enhancer.fetch_all_symbols(symbols)
        except Exception as exc:
            logger.debug("FundingRateAgent: MIL pre-fetch skipped: %s", exc)

        results: dict[str, dict] = {}
        for symbol in symbols:
            try:
                raw = self._fetch_symbol(exchange, symbol)
                if raw:
                    results[symbol] = raw
                    get_api_call_counter().record("ccxt")
            except Exception as exc:
                logger.debug("FundingRateAgent: skip %s — %s", symbol, exc)

        return results

    def process(self, raw: dict[str, dict]) -> dict:
        """
        Compute per-symbol signals and update cache.
        Returns a summary dict (aggregate snapshot for the event).
        """
        if not raw:
            return {"signal": 0.0, "confidence": 0.0, "symbols": {}, "count": 0}

        with self._lock:
            now_ts = time.time()
            cutoff_24h = now_ts - 86400.0

            for symbol, data in raw.items():
                prev_oi = self._cache.get(symbol, {}).get("oi_usdt", data["oi_usdt"])
                rate_pct = data["rate_pct"]

                # Update 24h rate history
                if symbol not in self._rate_history:
                    self._rate_history[symbol] = []
                self._rate_history[symbol].append({"timestamp": now_ts, "rate_pct": rate_pct})
                # Prune entries older than 24h (keep last 200 max)
                self._rate_history[symbol] = [
                    e for e in self._rate_history[symbol]
                    if e["timestamp"] >= cutoff_24h
                ][-200:]

                # Compute 24h average rate
                history = self._rate_history[symbol]
                avg_rate_24h = (
                    sum(e["rate_pct"] for e in history) / len(history)
                    if history else rate_pct
                )

                signal, confidence, direction, explanation = self._compute_signal(
                    rate_pct, data["oi_usdt"], prev_oi, avg_rate_24h
                )
                entry = {
                    **data,
                    "prev_oi_usdt":  prev_oi,
                    "oi_change_pct": self._pct_change(prev_oi, data["oi_usdt"]),
                    "avg_rate_24h":  round(avg_rate_24h, 5),
                    "signal":        signal,
                    "confidence":    confidence,
                    "direction":     direction,
                    "explanation":   explanation,
                }

                # ── MIL Phase 4A: Enhanced funding metadata ──────
                # Gated by mil.global_enabled AND agents.funding_rate_enhanced.
                # Fail-open: if enhancer fails, entry is unchanged.
                try:
                    from core.agents.mil.funding_rate_enhanced import get_funding_enhancer
                    _enhancer = get_funding_enhancer()
                    if _enhancer.is_enabled():
                        entry = _enhancer.enhance_symbol_data(
                            symbol, rate_pct, entry
                        )
                except Exception as exc:
                    logger.debug("FundingRateAgent: MIL enhance skipped: %s", exc)

                self._cache[symbol] = entry
            cache_snapshot = dict(self._cache)

        # Aggregate summary signal (average across symbols)
        signals = [v["signal"] for v in cache_snapshot.values() if not v.get("stale")]
        avg_signal = sum(signals) / len(signals) if signals else 0.0
        avg_conf   = sum(v["confidence"] for v in cache_snapshot.values()) / max(len(cache_snapshot), 1)

        # MIL diagnostics for aggregate summary
        mil_active = any(v.get("mil_enhanced") for v in cache_snapshot.values())

        logger.info(
            "FundingRateAgent: %d symbols | avg_signal=%.3f | avg_conf=%.2f%s",
            len(raw), avg_signal, avg_conf,
            " | MIL=ON" if mil_active else "",
        )

        return {
            "signal":     round(avg_signal, 4),
            "confidence": round(avg_conf,   4),
            "symbols":    cache_snapshot,
            "count":      len(cache_snapshot),
            "mil_active": mil_active,
        }

    # ── Per-symbol signal logic ───────────────────────────────

    def _compute_signal(
        self,
        rate_pct: float,
        oi_usdt: float,
        prev_oi_usdt: float,
        avg_rate_24h: float = 0.0,
    ) -> tuple[float, float, str, str]:
        """
        Returns (signal, confidence, direction, explanation).
        signal is CONTRARIAN to funding: extreme positive funding → bearish signal.
        avg_rate_24h: 24-hour rolling average rate for sustained-signal detection.
        """
        # Base signal from funding rate
        if rate_pct >= _EXTREME_LONG:
            base_signal = -0.85
            confidence  =  0.85
            direction   = "bearish"
            explanation = f"Extreme positive funding ({rate_pct:.3f}%/8h) — overleveraged longs, reversal risk"
        elif rate_pct >= _HIGH_LONG:
            base_signal = -0.45
            confidence  =  0.65
            direction   = "bearish"
            explanation = f"High positive funding ({rate_pct:.3f}%/8h) — moderately crowded longs"
        elif rate_pct <= _HIGH_SHORT:
            base_signal = +0.85
            confidence  =  0.85
            direction   = "bullish"
            explanation = f"Extreme negative funding ({rate_pct:.3f}%/8h) — crowded shorts, squeeze potential"
        elif rate_pct <= _MODERATE_SHORT:
            base_signal = +0.45
            confidence  =  0.65
            direction   = "bullish"
            explanation = f"Negative funding ({rate_pct:.3f}%/8h) — shorts dominant"
        else:
            base_signal = 0.0
            confidence  = 0.30
            direction   = "neutral"
            explanation = f"Neutral funding ({rate_pct:.3f}%/8h)"

        # Sustained signal amplification: if current rate > 1.5× 24h average,
        # the positioning is unusually extreme → amplify signal by 20%
        if abs(base_signal) > 0.1 and avg_rate_24h != 0.0:
            avg_abs = abs(avg_rate_24h)
            curr_abs = abs(rate_pct)
            if avg_abs > 1e-6 and curr_abs > avg_abs * 1.5:
                base_signal = max(-1.0, min(1.0, base_signal * 1.20))
                confidence = min(1.0, confidence * 1.10)
                explanation += f" | Sustained extremity (24h avg {avg_rate_24h:.3f}%) +20% amplification"

        # OI adjustment
        oi_change_pct = self._pct_change(prev_oi_usdt, oi_usdt)
        if abs(base_signal) > 0.1:   # Only amplify/dampen non-neutral signals
            if oi_change_pct >= _OI_RISING_THRESHOLD:
                confidence = min(1.0, confidence * 1.3)
                explanation += f" | OI rising +{oi_change_pct:.1f}% (trend confirming)"
            elif oi_change_pct <= _OI_FALLING_THRESHOLD:
                confidence *= 0.5
                explanation += f" | OI falling {oi_change_pct:.1f}% (conviction weakening)"

        return round(base_signal, 4), round(confidence, 4), direction, explanation

    # ── CCXT helpers ──────────────────────────────────────────

    @staticmethod
    def _to_perp_symbol(symbol: str) -> str:
        """
        Convert a spot symbol to a linear perpetual futures symbol.
        Funding rates only exist on perp contracts, not spot.
        Examples:
          BTC/USDT  →  BTC/USDT:USDT   (Bybit linear perp)
          ETH/USDT  →  ETH/USDT:USDT
        If the symbol already has a settle currency (contains ':') leave it as-is.
        """
        if ":" in symbol:
            return symbol
        if "/" in symbol:
            base, quote = symbol.split("/", 1)
            return f"{base}/{quote}:{quote}"
        return symbol

    @staticmethod
    def _fetch_symbol(exchange, symbol: str) -> dict | None:
        """Fetch funding rate and OI for one symbol from CCXT.
        Tries the perpetual futures symbol (e.g. BTC/USDT:USDT) because
        funding rates do not exist on spot pairs."""
        rate_pct = 0.0
        oi_usdt  = 0.0

        perp = FundingRateAgent._to_perp_symbol(symbol)

        # Funding rate
        try:
            fr_data = exchange.fetch_funding_rate(perp)
            rate_pct = float(fr_data.get("fundingRate", 0) or 0) * 100.0
        except Exception:
            pass  # Exchange may not support this market type

        # Open interest
        try:
            oi_data = exchange.fetch_open_interest(perp)
            oi_usdt = float(oi_data.get("openInterestValue", 0) or 0)
        except Exception:
            pass

        if rate_pct == 0.0 and oi_usdt == 0.0:
            return None   # No useful data — skip

        return {"rate_pct": rate_pct, "oi_usdt": oi_usdt}

    @staticmethod
    def _pct_change(prev: float, curr: float) -> float:
        if prev and prev != 0:
            return round((curr - prev) / abs(prev) * 100.0, 2)
        return 0.0

    # ── Public API for FundingRateModel ───────────────────────

    def get_symbol_signal(self, symbol: str) -> dict:
        """
        Return the latest cached signal for one symbol.
        Used by FundingRateModel during scan cycles.
        """
        with self._lock:
            if symbol in self._cache:
                return dict(self._cache[symbol])
        return {"signal": 0.0, "confidence": 0.0, "stale": True, "rate_pct": 0.0}


# ── Module-level singleton (initialised by AgentCoordinator) ──
funding_rate_agent: FundingRateAgent | None = None
