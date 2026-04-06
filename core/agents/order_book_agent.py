# ============================================================
# NEXUS TRADER — Order Book Microstructure Agent  (Sprint 2)
#
# Analyses live L2 order book depth to detect:
#   • Bid/ask imbalance (directional short-term pressure)
#   • Bid and ask walls (large limit orders = support/resistance)
#   • Spread conditions
#
# Signal (directional — positive = bullish pressure):
#   imbalance = bid_volume / (bid_volume + ask_volume) within top N levels
#   imbalance > 0.65 → strong bid pressure → signal = +0.7 to +0.9
#   imbalance < 0.35 → strong ask pressure → signal = -0.7 to -0.9
#
# Publishes: Topics.ORDERBOOK_SIGNAL
# Data: {symbol, imbalance, bid_wall_pct, ask_wall_pct,
#        spread_pct, signal, confidence, direction}
# ============================================================
from __future__ import annotations

import logging
import threading
from typing import Any

from core.agents.base_agent import BaseAgent
from core.event_bus import Topics
from core.scanning.watchlist_gate import get_watchlist_symbols, get_api_call_counter

logger = logging.getLogger(__name__)

_POLL_SECONDS   = 30     # Order book data is very short-lived
_DEPTH_LEVELS   = 50     # Use top 50 levels for deeper liquidity visibility
_WALL_MULTIPLE  = 5.0    # A wall is a level with >5× average size

# Imbalance thresholds
_STRONG_BID = 0.65
_WEAK_BID   = 0.55
_WEAK_ASK   = 0.45
_STRONG_ASK = 0.35


class OrderBookAgent(BaseAgent):
    """
    Monitors order book microstructure for short-term directional bias.

    Useful for confirming entry direction and timing within a candle.
    Most effective on 1m-15m timeframes; confidence is reduced for
    longer timeframes where the signal's short-lived nature is less relevant.
    """

    def __init__(self, parent=None):
        super().__init__("order_book", parent)
        self._cache: dict[str, dict] = {}
        self._lock  = threading.RLock()

    # ── BaseAgent interface ────────────────────────────────────

    @property
    def event_topic(self) -> str:
        return Topics.ORDERBOOK_SIGNAL

    @property
    def poll_interval_seconds(self) -> int:
        return _POLL_SECONDS

    def fetch(self) -> dict[str, dict]:
        from core.market_data.exchange_manager import exchange_manager

        exchange = exchange_manager.get_exchange()
        if exchange is None:
            raise RuntimeError("No exchange connected")

        symbols = get_watchlist_symbols()  # Use watchlist-gated symbols
        results = {}
        for symbol in symbols[:20]:   # Limit to top 20 to avoid rate limits
            try:
                ob = exchange.fetch_order_book(symbol, limit=_DEPTH_LEVELS)
                results[symbol] = ob
                get_api_call_counter().record("ccxt")
            except Exception as exc:
                logger.debug("OrderBookAgent: skip %s — %s", symbol, exc)

        return results

    def process(self, raw: dict[str, dict]) -> dict:
        if not raw:
            return {"signal": 0.0, "confidence": 0.0, "has_data": False, "symbols": {}, "count": 0}

        with self._lock:
            for symbol, ob in raw.items():
                analysis = self._analyse_book(ob)
                self._cache[symbol] = analysis

            cache_snapshot = dict(self._cache)

        signals = [v["signal"] for v in cache_snapshot.values()]
        avg_signal = sum(signals) / len(signals) if signals else 0.0
        avg_conf   = sum(v["confidence"] for v in cache_snapshot.values()) / max(len(cache_snapshot), 1)

        logger.info(
            "OrderBookAgent: %d symbols | avg_signal=%.3f | avg_conf=%.2f",
            len(raw), avg_signal, avg_conf,
        )

        return {
            "signal":     round(avg_signal, 4),
            "confidence": round(avg_conf,   4),
            "has_data": True,
            "symbols":    cache_snapshot,
            "count":      len(cache_snapshot),
        }

    # ── Analysis logic ────────────────────────────────────────

    def _analyse_book(self, ob: dict) -> dict:
        bids = ob.get("bids", [])[:_DEPTH_LEVELS]   # [[price, size], ...]
        asks = ob.get("asks", [])[:_DEPTH_LEVELS]

        if not bids or not asks:
            return {"signal": 0.0, "confidence": 0.0, "imbalance": 0.5,
                    "bid_wall_pct": 0.0, "ask_wall_pct": 0.0, "spread_pct": 0.0,
                    "direction": "neutral"}

        # Volumes at each level
        bid_vols  = [float(b[1]) * float(b[0]) for b in bids]  # in quote currency
        ask_vols  = [float(a[1]) * float(a[0]) for a in asks]

        total_bid = sum(bid_vols)
        total_ask = sum(ask_vols)
        total     = total_bid + total_ask

        imbalance  = total_bid / total if total > 0 else 0.5

        # Wall detection — levels with unusually large volume
        avg_bid = total_bid / len(bid_vols) if bid_vols else 0.0
        avg_ask = total_ask / len(ask_vols) if ask_vols else 0.0
        bid_wall = max(bid_vols) / avg_bid if avg_bid > 0 else 0.0
        ask_wall = max(ask_vols) / avg_ask if avg_ask > 0 else 0.0

        # Spread
        best_bid = float(bids[0][0]) if bids else 0.0
        best_ask = float(asks[0][0]) if asks else 0.0
        spread_pct = (best_ask - best_bid) / best_bid * 100.0 if best_bid > 0 else 0.0

        # Signal and confidence from imbalance
        if imbalance >= _STRONG_BID:
            signal    = 0.80 * (imbalance - 0.5) / 0.5
            confidence = 0.80
            direction  = "bullish"
        elif imbalance >= _WEAK_BID:
            signal    = 0.45 * (imbalance - 0.5) / 0.5
            confidence = 0.55
            direction  = "bullish"
        elif imbalance <= _STRONG_ASK:
            signal    = -0.80 * (0.5 - imbalance) / 0.5
            confidence = 0.80
            direction  = "bearish"
        elif imbalance <= _WEAK_ASK:
            signal    = -0.45 * (0.5 - imbalance) / 0.5
            confidence = 0.55
            direction  = "bearish"
        else:
            signal    = 0.0
            confidence = 0.25
            direction  = "neutral"

        # Boost confidence if a significant wall supports the signal direction
        if signal > 0 and bid_wall >= _WALL_MULTIPLE:
            confidence = min(1.0, confidence + 0.15)
        elif signal < 0 and ask_wall >= _WALL_MULTIPLE:
            confidence = min(1.0, confidence + 0.15)

        # Reduce confidence if spread is high (poor market quality)
        if spread_pct > 0.3:
            confidence *= 0.7

        return {
            "signal":       round(signal, 4),
            "confidence":   round(confidence, 4),
            "imbalance":    round(imbalance, 4),
            "bid_wall_pct": round(bid_wall, 2),
            "ask_wall_pct": round(ask_wall, 2),
            "spread_pct":   round(spread_pct, 4),
            "direction":    direction,
        }

    # ── Public API for sub-model ──────────────────────────────

    def get_symbol_signal(self, symbol: str) -> dict:
        with self._lock:
            if symbol in self._cache:
                return dict(self._cache[symbol])
        return {"signal": 0.0, "confidence": 0.0, "stale": True}


# ── Module-level singleton ────────────────────────────────────
order_book_agent: OrderBookAgent | None = None
