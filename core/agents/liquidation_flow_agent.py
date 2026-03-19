# ============================================================
# NEXUS TRADER — Liquidation Flow Agent
#
# Detects liquidation cascades using CCXT fetchLiquidations()
# or fallback proxy via price action + funding rate spikes.
#
# Signal logic (5-minute window):
#   Long liquidations >> short liq.       → forced longs getting liquidated (bearish, -0.6 to -0.9)
#   Short liquidations >> long liq.       → forced shorts getting liquidated (bullish, +0.6 to +0.9)
#   Balanced                               → neutral (0.0)
#
# Fallback proxy: large price swing + high volume + large wick = liquidation event
#
# Publishes: Topics.LIQUIDATION_FLOW_UPDATED
# ============================================================
from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
from typing import Any

from core.agents.base_agent import BaseAgent
from core.event_bus import Topics

logger = logging.getLogger(__name__)

_POLL_SECONDS = 60  # 1 minute — needs fast refresh

# Liquidation detection thresholds
_LIQ_RATIO_THRESHOLD = 2.0      # If one side is 2x the other, signal
_LIQUIDATION_WINDOW_MIN = 5     # Look back 5 minutes


class LiquidationFlowAgent(BaseAgent):
    """
    Monitors liquidation cascades on perpetual futures.

    Uses CCXT fetch_liquidations() where available, falls back to
    price action + funding rate spikes as proxy.
    """

    def __init__(self, parent=None):
        super().__init__("liquidation_flow", parent)
        self._last_liquidations: list[dict] = []
        self._last_ohlcv_price: float = 0.0

    # ── BaseAgent interface ────────────────────────────────────

    @property
    def event_topic(self) -> str:
        return Topics.LIQUIDATION_FLOW_UPDATED

    @property
    def poll_interval_seconds(self) -> int:
        return _POLL_SECONDS

    def fetch(self) -> dict[str, Any]:
        """Fetch liquidation data from exchange or proxy."""
        from core.scanning.watchlist import WatchlistManager
        from core.market_data.exchange_manager import exchange_manager

        exchange = exchange_manager.get_exchange()
        if exchange is None:
            raise RuntimeError("No exchange connected")

        wl = WatchlistManager()
        symbols = wl.get_active_symbols()
        if not symbols:
            return {}

        results: dict[str, dict] = {}

        for symbol in symbols:
            try:
                liq_data = self._fetch_liquidations(exchange, symbol)
                if liq_data:
                    results[symbol] = liq_data
            except Exception as exc:
                logger.debug("LiquidationFlowAgent: fetch %s failed — %s", symbol, exc)

        return results

    def process(self, raw: dict[str, Any]) -> dict:
        """
        Compute liquidation cascade signals.
        Returns aggregate signal across symbols.
        """
        if not raw:
            return {"signal": 0.0, "confidence": 0.0, "symbols": {}, "count": 0}

        symbols_result = {}
        for symbol, data in raw.items():
            # Skip non-dict entries (e.g. metadata scalars stored in the raw dict)
            if not isinstance(data, dict):
                continue
            signal, confidence, direction, metadata = self._compute_signal(data)
            symbols_result[symbol] = {
                **data,
                "signal": signal,
                "confidence": confidence,
                "direction": direction,
                "metadata": metadata,
            }

        # Aggregate
        signals = [v["signal"] for v in symbols_result.values()]
        confs = [v["confidence"] for v in symbols_result.values()]
        avg_signal = sum(signals) / len(signals) if signals else 0.0
        avg_conf = sum(confs) / len(confs) if confs else 0.0

        logger.info(
            "LiquidationFlowAgent: %d symbols | avg_signal=%.3f | avg_conf=%.2f",
            len(raw), avg_signal, avg_conf,
        )

        return {
            "signal": round(avg_signal, 4),
            "confidence": round(avg_conf, 4),
            "symbols": symbols_result,
            "count": len(symbols_result),
        }

    # ── Signal logic ───────────────────────────────────────────

    @staticmethod
    def _compute_signal(data: dict) -> tuple[float, float, str, dict]:
        """
        Compute signal from liquidation volume imbalance.

        Returns (signal, confidence, direction, metadata).
        """
        long_liq_vol = data.get("long_liq_volume", 0.0)
        short_liq_vol = data.get("short_liq_volume", 0.0)
        has_liquidations = data.get("has_liquidations", False)

        metadata = {
            "long_liq_volume": long_liq_vol,
            "short_liq_volume": short_liq_vol,
            "has_liquidations": has_liquidations,
        }

        signal = 0.0
        confidence = 0.0
        direction = "neutral"

        total_vol = long_liq_vol + short_liq_vol

        # No liquidations detected
        if total_vol == 0.0:
            confidence = 0.15
            metadata["reason"] = "No liquidations detected (normal market)"
            return signal, confidence, direction, metadata

        # Long liquidations dominate (forced longs getting liquidated → bearish)
        if long_liq_vol > short_liq_vol * _LIQ_RATIO_THRESHOLD:
            ratio = long_liq_vol / short_liq_vol if short_liq_vol > 0 else long_liq_vol
            signal = -0.75
            confidence = min(0.85, 0.5 + (ratio - _LIQ_RATIO_THRESHOLD) * 0.1)
            direction = "bearish"
            metadata["reason"] = f"Long liquidation cascade (ratio {ratio:.1f}x)"

        # Short liquidations dominate (forced shorts getting liquidated → bullish)
        elif short_liq_vol > long_liq_vol * _LIQ_RATIO_THRESHOLD:
            ratio = short_liq_vol / long_liq_vol if long_liq_vol > 0 else short_liq_vol
            signal = 0.75
            confidence = min(0.85, 0.5 + (ratio - _LIQ_RATIO_THRESHOLD) * 0.1)
            direction = "bullish"
            metadata["reason"] = f"Short liquidation cascade (ratio {ratio:.1f}x)"

        # Balanced liquidations
        else:
            signal = 0.0
            confidence = 0.20
            direction = "neutral"
            metadata["reason"] = "Balanced liquidation flow"

        return round(signal, 4), round(confidence, 4), direction, metadata

    # ── Exchange data fetching ─────────────────────────────────

    def _fetch_liquidations(self, exchange, symbol: str) -> dict | None:
        """
        Fetch liquidations via CCXT or fallback to proxy.

        Returns dict with long_liq_volume, short_liq_volume, has_liquidations.
        """
        long_liq_vol = 0.0
        short_liq_vol = 0.0
        has_liquidations = False

        # Try native fetch_liquidations() if supported
        try:
            if hasattr(exchange, "fetch_liquidations"):
                liqs = exchange.fetch_liquidations(symbol)
                if liqs:
                    has_liquidations = True
                    now = datetime.now(timezone.utc)
                    cutoff = now - timedelta(minutes=_LIQUIDATION_WINDOW_MIN)

                    for liq in liqs:
                        timestamp = datetime.fromtimestamp(liq.get("timestamp", 0) / 1000.0, tz=timezone.utc)
                        if timestamp < cutoff:
                            continue

                        side = liq.get("side", "").lower()
                        amount = float(liq.get("amount", 0) or 0)

                        if side == "long":
                            long_liq_vol += amount
                        elif side == "short":
                            short_liq_vol += amount

                    self._last_liquidations = liqs[-10:]  # Keep last 10 for fallback
                    return {
                        "symbol": symbol,
                        "long_liq_volume": long_liq_vol,
                        "short_liq_volume": short_liq_vol,
                        "has_liquidations": has_liquidations,
                    }
        except Exception as exc:
            logger.debug("LiquidationFlowAgent: fetch_liquidations not available for %s — %s", symbol, exc)

        # Fallback: use price action proxy
        return self._fetch_liquidations_proxy(exchange, symbol)

    def _fetch_liquidations_proxy(self, exchange, symbol: str) -> dict | None:
        """
        Fallback proxy: detect liquidation events from OHLCV bars.

        Large volume + large wick + price move = likely liquidation event.
        """
        long_liq_vol = 0.0
        short_liq_vol = 0.0

        try:
            # Fetch last few 1-min bars
            ohlcv = exchange.fetch_ohlcv(symbol, "1m", limit=5)
            if not ohlcv:
                return {
                    "symbol": symbol,
                    "long_liq_volume": 0.0,
                    "short_liq_volume": 0.0,
                    "has_liquidations": False,
                }

            latest_bar = ohlcv[-1]
            open_p, high, low, close, volume = latest_bar[1], latest_bar[2], latest_bar[3], latest_bar[4], latest_bar[5]

            # Compute wick size and price move
            wick_high = high - close
            wick_low = close - low
            price_move_pct = abs(close - open_p) / open_p * 100.0 if open_p else 0.0

            # Check if 20-bar average volume (as proxy)
            avg_volume = sum(b[5] for b in ohlcv[:-1]) / len(ohlcv[:-1]) if len(ohlcv) > 1 else volume

            # Liquidation signal: large volume + significant wicks + price move > 1.5%
            if volume > avg_volume * 2.0 and (wick_high > 0.01 * close or wick_low > 0.01 * close) and price_move_pct > 1.5:
                # Infer direction from wick: large upper wick = forced longs, lower wick = forced shorts
                if wick_high > wick_low:
                    # Upper wick dominant → longs likely stopped out
                    long_liq_vol = volume * (wick_high / (wick_high + wick_low))
                else:
                    # Lower wick dominant → shorts likely stopped out
                    short_liq_vol = volume * (wick_low / (wick_high + wick_low))

            self._last_ohlcv_price = close

            return {
                "symbol": symbol,
                "long_liq_volume": round(long_liq_vol, 2),
                "short_liq_volume": round(short_liq_vol, 2),
                "has_liquidations": long_liq_vol > 0 or short_liq_vol > 0,
            }

        except Exception as exc:
            logger.debug("LiquidationFlowAgent: OHLCV proxy fetch failed for %s — %s", symbol, exc)
            return {
                "symbol": symbol,
                "long_liq_volume": 0.0,
                "short_liq_volume": 0.0,
                "has_liquidations": False,
            }


# ── Module-level singleton (initialised by AgentCoordinator) ──
liquidation_flow_agent: LiquidationFlowAgent | None = None
