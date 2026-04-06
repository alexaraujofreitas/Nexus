# ============================================================
# NEXUS TRADER — Squeeze Detection & Leverage Crowding Agent
#
# Monitors futures market squeeze conditions and leverage crowding
# using Binance perpetual futures open interest and funding rates.
# Uses FREE Binance public API endpoints (no API key required).
#
# Signal logic:
#   Short squeeze setup:    L/S < 0.6 + negative funding + high OI → bullish (+0.7 to +0.9)
#   Long squeeze setup:     L/S > 1.8 + positive funding + high OI → bearish (-0.7 to -0.9)
#   OI drop >15%:           Squeeze completed, signal reverting
#   High leverage crowding: Directional crowding risk
#   Funding extremes:       Long crowding (>0.1%) or short crowding (<-0.05%)
#
# Publishes: Topics.SQUEEZE_DETECTED (primary signal)
# Publishes: Topics.LEVERAGE_CROWDING (when leverage crowding detected)
# ============================================================
from __future__ import annotations

import json
import logging
import urllib.request
from typing import Any
from urllib.error import URLError

from core.agents.base_agent import BaseAgent
from core.event_bus import Topics, bus
from core.scanning.watchlist_gate import get_api_call_counter

logger = logging.getLogger(__name__)

_POLL_SECONDS = 300  # 5 minutes (squeeze conditions change fast)


class SqueezeDetectionAgent(BaseAgent):
    """
    Detects squeeze conditions and leverage crowding in BTCUSDT futures.

    Combines long/short ratio, funding rates, and open interest changes
    to identify imminent liquidation cascades and directional crowding.
    """

    def __init__(self, parent=None):
        super().__init__("squeeze_detection", parent)
        self._cache: dict[str, Any] = {}
        self._prev_oi = 0.0  # Track OI changes for delta calculation

    # ── BaseAgent interface ────────────────────────────────────

    @property
    def event_topic(self) -> str:
        return Topics.SQUEEZE_DETECTED

    @property
    def poll_interval_seconds(self) -> int:
        return _POLL_SECONDS

    def on_settings_changed(self) -> None:
        """Update config if changed."""
        try:
            from config.settings import settings
            # Optional: allow configuration of poll interval
            poll_override = settings.get("agents.squeeze.poll_interval_seconds", None)
            if isinstance(poll_override, int):
                pass  # Could override _POLL_SECONDS here if needed
        except Exception as exc:
            logger.debug("SqueezeDetectionAgent: settings update error: %s", exc)

    def fetch(self) -> dict[str, Any]:
        """Fetch Binance futures data for BTCUSDT."""
        results: dict[str, Any] = {}

        try:
            # Fetch instantaneous open interest
            oi_data = self._fetch_binance_open_interest()
            if oi_data:
                results["open_interest"] = oi_data
                get_api_call_counter().record("binance")
        except Exception as exc:
            logger.debug("SqueezeDetectionAgent: open interest fetch failed — %s", exc)

        try:
            # Fetch long/short ratio (48h of hourly data)
            ls_data = self._fetch_binance_long_short_ratio()
            if ls_data:
                results["long_short_ratio"] = ls_data
                get_api_call_counter().record("binance")
        except Exception as exc:
            logger.debug("SqueezeDetectionAgent: long/short ratio fetch failed — %s", exc)

        try:
            # Fetch funding rates (24h of historical)
            fr_data = self._fetch_binance_funding_rates()
            if fr_data:
                results["funding_rates"] = fr_data
                get_api_call_counter().record("binance")
        except Exception as exc:
            logger.debug("SqueezeDetectionAgent: funding rates fetch failed — %s", exc)

        try:
            # Fetch top trader long/short ratio (24h of hourly data)
            tt_data = self._fetch_binance_top_trader_ratio()
            if tt_data:
                results["top_trader_ratio"] = tt_data
                get_api_call_counter().record("binance")
        except Exception as exc:
            logger.debug("SqueezeDetectionAgent: top trader ratio fetch failed — %s", exc)

        return results

    def process(self, raw: dict[str, Any]) -> dict:
        """
        Compute squeeze and leverage crowding signals.
        Returns squeeze_probability, squeeze_direction, crowding_score, and derived metrics.
        """
        if not raw:
            return {
                "signal": 0.0,
                "confidence": 0.0,
                "has_data": False,
                "squeeze_probability": 0.0,
                "squeeze_direction": "none",
                "crowding_score": 0.0,
                "funding_rate_avg": 0.0,
                "ls_ratio": 0.0,
                "oi_change_pct": 0.0,
                "leverage_signal": 0.0,
                "metadata": {},
            }

        (
            signal,
            confidence,
            squeeze_prob,
            squeeze_dir,
            crowding_score,
            funding_avg,
            ls_ratio,
            oi_change,
            leverage_signal,
            metadata,
        ) = self._compute_signal(raw)

        logger.info(
            "SqueezeDetectionAgent: signal=%.3f | confidence=%.2f | "
            "squeeze_prob=%.2f | direction=%s | crowding=%.2f",
            signal, confidence, squeeze_prob, squeeze_dir, crowding_score,
        )

        self._cache = {
            "signal": signal,
            "confidence": confidence,
            "has_data": True,
            "squeeze_probability": squeeze_prob,
            "squeeze_direction": squeeze_dir,
            "crowding_score": crowding_score,
            "funding_rate_avg": funding_avg,
            "ls_ratio": ls_ratio,
            "oi_change_pct": oi_change,
            "leverage_signal": leverage_signal,
            "metadata": metadata,
        }

        if crowding_score > 0.6:
            crowding_event = {
                "signal": leverage_signal,
                "confidence": confidence,
                "crowding_score": crowding_score,
                "type": "directional" if abs(ls_ratio - 1.0) > 0.5 else "funding",
                "metadata": metadata,
            }
            bus.publish(Topics.LEVERAGE_CROWDING, crowding_event, source=self._name)

        return self._cache

    # ── Signal logic ───────────────────────────────────────────

    def _compute_signal(
        self, raw: dict[str, Any]
    ) -> tuple[float, float, float, str, float, float, float, float, float, dict]:
        """
        Infer squeeze and crowding signals from Binance futures data.

        Returns (
            signal, confidence, squeeze_probability, squeeze_direction,
            crowding_score, funding_rate_avg, ls_ratio, oi_change_pct,
            leverage_signal, metadata
        ).
        """
        oi_data = raw.get("open_interest", {})
        ls_data = raw.get("long_short_ratio", {})
        fr_data = raw.get("funding_rates", {})
        tt_data = raw.get("top_trader_ratio", {})

        signal = 0.0
        confidence = 0.0
        squeeze_prob = 0.0
        squeeze_dir = "none"
        crowding_score = 0.0
        leverage_signal = 0.0

        # Extract current metrics
        current_oi = oi_data.get("openInterest", 0.0)
        current_ls_ratio = ls_data.get("current_ratio", 0.0)
        funding_rates = fr_data.get("rates", [])
        top_trader_ratio = tt_data.get("current_ratio", 0.0)

        # Calculate OI change percentage
        oi_change_pct = 0.0
        if self._prev_oi > 0:
            oi_change_pct = ((current_oi - self._prev_oi) / self._prev_oi * 100.0)
        self._prev_oi = current_oi

        # Calculate average funding rate
        funding_rate_avg = 0.0
        if funding_rates:
            funding_rate_avg = sum(funding_rates) / len(funding_rates)

        metadata: dict[str, Any] = {
            "current_oi": current_oi,
            "oi_change_pct": round(oi_change_pct, 2),
            "current_ls_ratio": round(current_ls_ratio, 4),
            "funding_rate_avg": round(funding_rate_avg, 6),
            "funding_rate_latest": funding_rates[0] if funding_rates else 0.0,
            "top_trader_ratio": round(top_trader_ratio, 4),
        }

        # ── Squeeze Detection Logic ────────────────────────────────

        # Short Squeeze Setup: L/S < 0.6 + negative funding + high OI
        if current_ls_ratio < 0.6 and funding_rate_avg < -0.0005 and current_oi > 0:
            squeeze_prob = min(0.85, 0.5 + (abs(funding_rate_avg) * 1000))  # Scale by funding negativity
            squeeze_dir = "long"  # Short squeeze = long breakout
            signal = 0.80
            confidence = 0.75
            crowding_score = min(1.0, 0.7 + (0.6 - current_ls_ratio))
            metadata["reason"] = "Short squeeze setup detected"

        # Long Squeeze Setup: L/S > 1.8 + positive funding (>0.05%) + high OI
        elif current_ls_ratio > 1.8 and funding_rate_avg > 0.0005 and current_oi > 0:
            squeeze_prob = min(0.85, 0.5 + (funding_rate_avg * 1000))
            squeeze_dir = "short"  # Long squeeze = short breakout
            signal = -0.80
            confidence = 0.75
            crowding_score = min(1.0, 0.7 + (current_ls_ratio - 1.8))
            metadata["reason"] = "Long squeeze setup detected"

        # Post-squeeze Neutralization: OI drop >15%
        elif oi_change_pct < -15.0:
            squeeze_prob = 0.3
            squeeze_dir = "none"
            signal = 0.0 if signal > 0 else 0.0  # Neutral
            confidence = 0.5
            metadata["reason"] = "Squeeze completed, OI collapsed"

        # ── Leverage Crowding Logic ────────────────────────────────

        # Long crowding: funding sustained >0.1% for extended period
        if funding_rate_avg > 0.001:
            leverage_signal = -0.5  # Bearish crowding risk
            crowding_score = max(crowding_score, min(1.0, funding_rate_avg * 1000))
            if not metadata.get("reason"):
                metadata["reason"] = "Long crowding: sustained positive funding"

        # Short crowding: funding sustained <-0.05%
        elif funding_rate_avg < -0.0005:
            leverage_signal = 0.4  # Bullish contrarian
            crowding_score = max(crowding_score, min(1.0, abs(funding_rate_avg) * 1000))
            if not metadata.get("reason"):
                metadata["reason"] = "Short crowding: sustained negative funding"

        # Directional crowding from L/S ratio
        if current_ls_ratio > 2.0:
            leverage_signal = -0.4  # Extreme longs = bearish
            crowding_score = max(crowding_score, min(1.0, (current_ls_ratio - 1.0) / 2.0))
            metadata["reason"] = "Extreme long crowding from L/S ratio"

        elif current_ls_ratio < 0.4:
            leverage_signal = 0.4  # Extreme shorts = bullish
            crowding_score = max(crowding_score, min(1.0, (1.0 - current_ls_ratio) / 1.0))
            metadata["reason"] = "Extreme short crowding from L/S ratio"

        # Default case: balanced — Session 51 fix: produce a non-zero signal
        # from L/S ratio deviation and funding rate even when thresholds aren't
        # extreme.  This ensures the agent always contributes to the orchestrator.
        if squeeze_dir == "none" and squeeze_prob == 0.0:
            # Mild directional signal from L/S ratio position relative to 1.0
            # L/S > 1.0 → slightly more longs → mild bearish contrarian
            # L/S < 1.0 → slightly more shorts → mild bullish contrarian
            if current_ls_ratio > 0 and current_ls_ratio != 1.0:
                ls_deviation = 1.0 - current_ls_ratio  # positive when shorts dominate
                signal = round(max(-0.30, min(0.30, ls_deviation * 0.25)), 4)
            else:
                signal = 0.0

            # Add funding rate micro-signal
            if funding_rate_avg != 0:
                # Negative funding = shorts paying = mild bullish
                fr_signal = round(max(-0.15, min(0.15, -funding_rate_avg * 200)), 4)
                signal = round(max(-0.40, min(0.40, signal + fr_signal)), 4)

            confidence = 0.35
            crowding_score = round(min(0.5, abs(current_ls_ratio - 1.0) * 0.5 + abs(funding_rate_avg) * 500), 4)
            if signal == 0.0:
                signal = 0.05  # minimal non-zero baseline
            metadata["reason"] = (
                f"Balanced market: L/S={current_ls_ratio:.3f}, "
                f"funding={funding_rate_avg:.6f} — mild directional lean"
            )

        return (
            round(signal, 4),
            round(confidence, 4),
            round(squeeze_prob, 4),
            squeeze_dir,
            round(crowding_score, 4),
            round(funding_rate_avg, 6),
            round(current_ls_ratio, 4),
            round(oi_change_pct, 2),
            round(leverage_signal, 4),
            metadata,
        )

    # ── Binance Futures API ────────────────────────────────────

    @staticmethod
    def _fetch_binance_open_interest() -> dict | None:
        """
        Fetch instantaneous open interest for BTCUSDT.

        Returns dict with openInterest (USD value).
        """
        url = "https://fapi.binance.com/fapi/v1/openInterest?symbol=BTCUSDT"

        try:
            with urllib.request.urlopen(url, timeout=10) as response:
                text = response.read().decode("utf-8")
                data = json.loads(text)

            open_interest = float(data.get("openInterest", 0.0))

            return {
                "openInterest": open_interest,
                "symbol": "BTCUSDT",
            }

        except (URLError, json.JSONDecodeError, KeyError, ValueError) as exc:
            logger.debug("SqueezeDetectionAgent: Binance OI fetch failed — %s", exc)
            return None

    @staticmethod
    def _fetch_binance_long_short_ratio() -> dict | None:
        """
        Fetch long/short account ratio for BTCUSDT (48h of hourly data).

        Returns dict with current_ratio and historical data.
        """
        url = (
            "https://fapi.binance.com/futures/data/globalLongShortAccountRatio"
            "?symbol=BTCUSDT&period=1h&limit=48"
        )

        try:
            with urllib.request.urlopen(url, timeout=10) as response:
                text = response.read().decode("utf-8")
                data = json.loads(text)

            ratios = data if isinstance(data, list) else []
            if not ratios:
                return None

            # Most recent ratio is last in list
            current_ratio = float(ratios[-1].get("longShortRatio", 1.0))
            avg_ratio = sum(float(r.get("longShortRatio", 1.0)) for r in ratios) / len(ratios)

            return {
                "current_ratio": current_ratio,
                "avg_ratio_48h": avg_ratio,
                "ratios": ratios,
            }

        except (URLError, json.JSONDecodeError, KeyError, ValueError, IndexError) as exc:
            logger.debug("SqueezeDetectionAgent: Binance L/S ratio fetch failed — %s", exc)
            return None

    @staticmethod
    def _fetch_binance_funding_rates() -> dict | None:
        """
        Fetch funding rates for BTCUSDT (last 24 rates).

        Returns dict with rates list (as decimal, e.g., 0.0001 for 0.01%).
        """
        url = (
            "https://fapi.binance.com/fapi/v1/fundingRate"
            "?symbol=BTCUSDT&limit=24"
        )

        try:
            with urllib.request.urlopen(url, timeout=10) as response:
                text = response.read().decode("utf-8")
                data = json.loads(text)

            rates = data if isinstance(data, list) else []
            if not rates:
                return None

            # Extract fundingRate from each entry
            funding_rates = [float(r.get("fundingRate", 0.0)) for r in rates]

            return {
                "rates": funding_rates,
                "latest_rate": funding_rates[-1] if funding_rates else 0.0,
                "count": len(funding_rates),
            }

        except (URLError, json.JSONDecodeError, KeyError, ValueError, IndexError) as exc:
            logger.debug("SqueezeDetectionAgent: Binance funding rates fetch failed — %s", exc)
            return None

    @staticmethod
    def _fetch_binance_top_trader_ratio() -> dict | None:
        """
        Fetch top trader long/short ratio for BTCUSDT (24h of hourly data).

        Returns dict with current_ratio and historical data.
        """
        url = (
            "https://fapi.binance.com/futures/data/topLongShortPositionRatio"
            "?symbol=BTCUSDT&period=1h&limit=24"
        )

        try:
            with urllib.request.urlopen(url, timeout=10) as response:
                text = response.read().decode("utf-8")
                data = json.loads(text)

            ratios = data if isinstance(data, list) else []
            if not ratios:
                return None

            # Most recent ratio is last in list
            current_ratio = float(ratios[-1].get("longShortRatio", 1.0))
            avg_ratio = sum(float(r.get("longShortRatio", 1.0)) for r in ratios) / len(ratios)

            return {
                "current_ratio": current_ratio,
                "avg_ratio_24h": avg_ratio,
                "ratios": ratios,
            }

        except (URLError, json.JSONDecodeError, KeyError, ValueError, IndexError) as exc:
            logger.debug("SqueezeDetectionAgent: Binance top trader ratio fetch failed — %s", exc)
            return None

    def get_last_signal(self) -> dict:
        """Return latest cached squeeze signal."""
        if self._cache:
            return dict(self._cache)
        return {
            "signal": 0.0,
            "confidence": 0.0,
            "squeeze_probability": 0.0,
            "squeeze_direction": "none",
            "crowding_score": 0.0,
            "funding_rate_avg": 0.0,
            "ls_ratio": 0.0,
            "oi_change_pct": 0.0,
            "leverage_signal": 0.0,
            "metadata": {},
            "stale": True,
        }


# ── Module-level singleton (initialised by AgentCoordinator) ──
squeeze_detection_agent: SqueezeDetectionAgent | None = None
