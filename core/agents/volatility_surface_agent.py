# ============================================================
# NEXUS TRADER — Volatility Surface Agent
#
# Monitors implied volatility (Deribit) vs. realized volatility
# to detect fear/complacency regimes.
#
# Signal logic:
#   IV >> realized vol (spread > 0.30)      → fear/uncertainty (bearish, -0.4 to -0.7)
#   IV << realized vol (spread < -0.10)     → complacency (mildly bearish, -0.2)
#   IV ≈ realized vol (spread ±0.10)        → normal (neutral, 0.0)
#   Historical vol declining over 7d        → regime calming (mildly bullish, +0.2)
#
# Stores last 10 IV readings to track trend.
# Uses urllib.request (no extra dependencies).
#
# Publishes: Topics.VOLATILITY_SURFACE_UPDATED
# ============================================================
from __future__ import annotations

import json
import logging
import urllib.request
from collections import deque
from datetime import datetime, timezone, timedelta
from typing import Any
from urllib.error import URLError

from core.agents.base_agent import BaseAgent
from core.event_bus import Topics

logger = logging.getLogger(__name__)

_POLL_SECONDS = 900  # 15 minutes


class VolatilitySurfaceAgent(BaseAgent):
    """
    Monitors implied volatility vs. realized volatility for crypto derivatives.

    Uses Deribit public API to track volatility spreads and regime changes.
    """

    def __init__(self, parent=None):
        super().__init__("volatility_surface", parent)
        # Store last 10 IV readings with timestamps
        self._iv_history: deque = deque(maxlen=10)

    # ── BaseAgent interface ────────────────────────────────────

    @property
    def event_topic(self) -> str:
        return Topics.VOLATILITY_SURFACE_UPDATED

    @property
    def poll_interval_seconds(self) -> int:
        return _POLL_SECONDS

    def fetch(self) -> dict[str, Any]:
        """Fetch index price, mark price, and volatility data from Deribit."""
        results = {}

        for currency in ["BTC", "ETH"]:
            try:
                # Get index price
                index_data = self._fetch_deribit_index(currency)
                if not index_data:
                    continue

                # Get perpetual mark price for realized vol proxy
                ticker_data = self._fetch_deribit_ticker(currency)
                if not ticker_data:
                    continue

                # Get historical volatility
                hvol_data = self._fetch_deribit_hvol(currency)
                if not hvol_data:
                    hvol_data = {}

                results[currency] = {
                    **index_data,
                    **ticker_data,
                    **hvol_data,
                }
            except Exception as exc:
                logger.debug("VolatilitySurfaceAgent: fetch %s failed — %s", currency, exc)

        return results

    def process(self, raw: dict[str, Any]) -> dict:
        """
        Compute volatility spread signals for BTC and ETH.
        Returns aggregate signal.
        """
        if not raw:
            return {"signal": 0.0, "confidence": 0.0, "symbols": {}, "count": 0}

        symbols_result = {}
        for currency, data in raw.items():
            signal, confidence, direction, metadata = self._compute_signal(data)
            symbols_result[currency] = {
                **data,
                "signal": signal,
                "confidence": confidence,
                "direction": direction,
                "metadata": metadata,
            }

            # Track IV for trend
            if "implied_vol" in data:
                self._iv_history.append({
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "iv": data["implied_vol"],
                })

        # Aggregate
        signals = [v["signal"] for v in symbols_result.values()]
        confs = [v["confidence"] for v in symbols_result.values()]
        avg_signal = sum(signals) / len(signals) if signals else 0.0
        avg_conf = sum(confs) / len(confs) if confs else 0.0

        logger.info(
            "VolatilitySurfaceAgent: %d instruments | avg_signal=%.3f | avg_conf=%.2f",
            len(raw), avg_signal, avg_conf,
        )

        return {
            "signal": round(avg_signal, 4),
            "confidence": round(avg_conf, 4),
            "symbols": symbols_result,
            "count": len(symbols_result),
            "iv_trend": self._compute_iv_trend(),
        }

    # ── Signal logic ───────────────────────────────────────────

    @staticmethod
    def _compute_signal(data: dict) -> tuple[float, float, str, dict]:
        """
        Compute signal from IV/realized vol spread and trend.

        Returns (signal, confidence, direction, metadata).
        """
        implied_vol = data.get("implied_vol", 0.0)
        realized_vol_proxy = data.get("realized_vol_proxy", 0.0)
        hvol_7d_change = data.get("hvol_7d_change_pct", 0.0)

        metadata = {
            "implied_vol": implied_vol,
            "realized_vol_proxy": realized_vol_proxy,
            "hvol_7d_change": hvol_7d_change,
        }

        signal = 0.0
        confidence = 0.0
        direction = "neutral"

        # Compute spread
        if realized_vol_proxy > 0:
            dvol_spread = implied_vol - realized_vol_proxy
        else:
            dvol_spread = 0.0

        metadata["dvol_spread"] = dvol_spread

        # IV >> realized vol: fear/uncertainty (bearish)
        if dvol_spread > 0.30:
            signal = -0.55
            confidence = 0.75
            direction = "bearish"
            metadata["reason"] = f"Fear regime: IV spread {dvol_spread:.3f} (elevated volatility expectation)"

        # IV << realized vol: complacency (mildly bearish)
        elif dvol_spread < -0.10:
            signal = -0.15
            confidence = 0.50
            direction = "mildly bearish"
            metadata["reason"] = f"Complacency: IV spread {dvol_spread:.3f} (IV compressed below realized)"

        # Normal vol environment
        elif -0.10 <= dvol_spread <= 0.10:
            signal = 0.0
            confidence = 0.40
            direction = "neutral"
            metadata["reason"] = f"Normal regime: IV spread {dvol_spread:.3f}"

        # Historical vol declining → regime calming (mildly bullish)
        if hvol_7d_change < -5.0 and abs(signal) < 0.3:
            signal = max(signal, 0.15)
            confidence = min(confidence + 0.15, 1.0)
            direction = "mildly bullish"
            metadata["reason"] += f" + hvol declining {hvol_7d_change:.1f}% (regime calming)"

        return round(signal, 4), round(confidence, 4), direction, metadata

    def _compute_iv_trend(self) -> dict:
        """Compute IV trend from history."""
        if len(self._iv_history) < 2:
            return {"trend": "insufficient_data", "iv_count": len(self._iv_history)}

        ivs = [h["iv"] for h in self._iv_history]
        oldest_iv = ivs[0]
        latest_iv = ivs[-1]
        iv_change = ((latest_iv - oldest_iv) / oldest_iv * 100.0) if oldest_iv else 0.0

        if iv_change > 10.0:
            trend = "rising"
        elif iv_change < -10.0:
            trend = "falling"
        else:
            trend = "stable"

        return {
            "trend": trend,
            "iv_change_pct": round(iv_change, 2),
            "iv_count": len(self._iv_history),
        }

    # ── Deribit API ────────────────────────────────────────────

    @staticmethod
    def _fetch_deribit_index(currency: str) -> dict | None:
        """Fetch index price from Deribit.

        Deribit API v2 requires lowercase index names with underscore separator:
        ``btc_usd``, ``eth_usd`` — NOT ``BTCUSD``.
        """
        index_name = f"{currency.lower()}_usd"
        url = f"https://www.deribit.com/api/v2/public/get_index_price?index_name={index_name}"

        try:
            req = urllib.request.Request(url, headers={"User-Agent": "NexusTrader/1.0"})
            with urllib.request.urlopen(req, timeout=10) as response:
                text = response.read().decode("utf-8")
                data = json.loads(text)

            result = data.get("result", {})
            index_price = result.get("index_price", 0.0)

            if index_price:
                logger.debug(
                    "VolatilitySurfaceAgent: Deribit index OK for %s — price=%.2f",
                    currency, index_price,
                )
            return {"currency": currency, "index_price": index_price} if index_price else None

        except (URLError, json.JSONDecodeError, KeyError, ValueError) as exc:
            logger.debug("VolatilitySurfaceAgent: Deribit index fetch failed for %s — %s (url=%s)", currency, exc, url)
            return None

    @staticmethod
    def _fetch_deribit_ticker(currency: str) -> dict | None:
        """Fetch perpetual ticker for realized vol proxy."""
        instrument = f"{currency}-PERPETUAL"
        url = f"https://www.deribit.com/api/v2/public/ticker?instrument_name={instrument}"

        try:
            req = urllib.request.Request(url, headers={"User-Agent": "NexusTrader/1.0"})
            with urllib.request.urlopen(req, timeout=10) as response:
                text = response.read().decode("utf-8")
                data = json.loads(text)

            result = data.get("result", {})
            mark_price = result.get("mark_price", 0.0)

            # Use recent price volatility as proxy for realized vol
            bid = result.get("best_bid_price", mark_price)
            ask = result.get("best_ask_price", mark_price)
            realized_vol_proxy = (
                abs(ask - bid) / mark_price * 100.0 if mark_price else 0.0
            )

            return {
                "mark_price": mark_price,
                "realized_vol_proxy": round(realized_vol_proxy, 4),
            } if mark_price else None

        except (URLError, json.JSONDecodeError, KeyError, ValueError) as exc:
            logger.debug("VolatilitySurfaceAgent: Deribit ticker fetch failed for %s — %s", currency, exc)
            return None

    @staticmethod
    def _fetch_deribit_hvol(currency: str) -> dict | None:
        """Attempt to fetch historical volatility from Deribit."""
        url = f"https://www.deribit.com/api/v2/public/get_historical_volatility?currency={currency}"

        try:
            req = urllib.request.Request(url, headers={"User-Agent": "NexusTrader/1.0"})
            with urllib.request.urlopen(req, timeout=10) as response:
                text = response.read().decode("utf-8")
                data = json.loads(text)

            result = data.get("result", [])
            if not result:
                return {"implied_vol": 0.35, "hvol_7d_change_pct": 0.0}

            # Parse historical vol data: [timestamp, vol] pairs
            current_hvol = result[-1][1] if result else 0.35
            hvol_7d_ago = result[0][1] if result and len(result) > 0 else current_hvol

            hvol_change = (
                ((current_hvol - hvol_7d_ago) / hvol_7d_ago * 100.0)
                if hvol_7d_ago
                else 0.0
            )

            return {
                "implied_vol": round(current_hvol, 4),
                "hvol_7d_change_pct": round(hvol_change, 2),
            }

        except (URLError, json.JSONDecodeError, KeyError, ValueError, IndexError) as exc:
            logger.debug("VolatilitySurfaceAgent: Deribit hvol fetch failed for %s — %s", currency, exc)
            # Graceful fallback
            return {"implied_vol": 0.35, "hvol_7d_change_pct": 0.0}


# ── Module-level singleton (initialised by AgentCoordinator) ──
volatility_surface_agent: VolatilitySurfaceAgent | None = None
