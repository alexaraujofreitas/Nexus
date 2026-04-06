# ============================================================
# NEXUS TRADER — Crash Detector
#
# Computes a composite crash score (0-10) from 7 components
# and feeds the CrashDefenseController. Runs on a 60-second
# timer independent of the scan cycle.
#
# Components (weights configurable from settings):
#   1. ATR spike (weight 2.0)
#   2. Price velocity (weight 1.8)
#   3. Liquidation cascade (weight 1.5)
#   4. Cross-asset correlated decline (weight 1.5)
#   5. Order book imbalance (weight 1.2)
#   6. Funding rate flip (weight 1.0)
#   7. Open interest collapse (weight 1.0)
# ============================================================
from __future__ import annotations

import logging
import threading
from typing import Optional, Dict
from datetime import datetime

import pandas as pd

logger = logging.getLogger(__name__)

# Tier constants
TIER_NORMAL = "normal"
TIER_DEFENSIVE = "defensive"
TIER_HIGH_ALERT = "high_alert"
TIER_EMERGENCY = "emergency"
TIER_SYSTEMIC = "systemic"

TIER_ORDER = [TIER_NORMAL, TIER_DEFENSIVE, TIER_HIGH_ALERT, TIER_EMERGENCY, TIER_SYSTEMIC]


class CrashDetector:
    """
    Monitors market conditions and computes a composite crash score
    that feeds the CrashDefenseController for adaptive position sizing
    and risk management.

    Thread-safe. All data fed externally (exchange tickers + OHLCV DataFrames).
    """

    def __init__(self):
        self._lock = threading.RLock()
        self._current_score: float = 0.0
        self._current_tier: str = TIER_NORMAL
        self._component_scores: dict[str, float] = {}
        self._timer = None
        self._running = False
        self._bars_below_recovery_threshold = 0
        self._atr_baselines: dict[str, float] = {}  # symbol -> rolling 20-bar ATR mean

    @property
    def current_score(self) -> float:
        """Current composite crash score (0-10)."""
        with self._lock:
            return self._current_score

    @property
    def current_tier(self) -> str:
        """Current tier: normal|defensive|high_alert|emergency|systemic."""
        with self._lock:
            return self._current_tier

    @property
    def is_crash_mode(self) -> bool:
        """True if current tier is not NORMAL."""
        with self._lock:
            return self._current_tier != TIER_NORMAL

    @property
    def component_scores(self) -> dict[str, float]:
        """Last computed scores per component."""
        with self._lock:
            return dict(self._component_scores)

    def evaluate(
        self,
        tickers: dict,
        df_by_symbol: dict[str, pd.DataFrame],
    ) -> float:
        """
        Evaluate crash conditions using latest market data.

        Parameters
        ----------
        tickers : dict
            Exchange tickers keyed by symbol (from fetch_tickers)
        df_by_symbol : dict[str, pd.DataFrame]
            OHLCV DataFrames keyed by symbol

        Returns
        -------
        float
            Current composite crash score (0-10)
        """
        with self._lock:
            from config.settings import settings

            if not settings.get("crash_detector.enabled", True):
                return 0.0

            # Get weights
            weights_cfg = settings.get("crash_detector.weights", {})
            weights = {
                "atr_spike": float(weights_cfg.get("atr_spike", 2.0)),
                "price_velocity": float(weights_cfg.get("price_velocity", 1.8)),
                "liquidation_cascade": float(weights_cfg.get("liquidation_cascade", 1.5)),
                "cross_asset_decline": float(weights_cfg.get("cross_asset_decline", 1.5)),
                "orderbook_imbalance": float(weights_cfg.get("orderbook_imbalance", 1.2)),
                "funding_rate_flip": float(weights_cfg.get("funding_rate_flip", 1.0)),
                "oi_collapse": float(weights_cfg.get("oi_collapse", 1.0)),
            }

            # Compute component scores.
            # Agent-dependent components return None when their data source is
            # unavailable.  Those components are excluded from the normalisation
            # denominator so that their absence does not suppress the overall
            # score below the emergency/systemic tier thresholds.
            scores: dict[str, Optional[float]] = {}
            scores["atr_spike"]           = self._compute_atr_spike(df_by_symbol)
            scores["price_velocity"]      = self._compute_price_velocity(df_by_symbol)
            scores["liquidation_cascade"] = self._compute_liquidation_cascade()
            scores["cross_asset_decline"] = self._compute_cross_asset_decline(df_by_symbol)
            scores["orderbook_imbalance"] = self._compute_orderbook_imbalance(tickers)
            scores["funding_rate_flip"]   = self._compute_funding_rate_flip(tickers)
            scores["oi_collapse"]         = self._compute_oi_collapse()

            # Normalise by the sum of weights of available components only.
            available_weight = 0.0
            weighted_sum     = 0.0
            unavailable = []
            for component, score_val in scores.items():
                if score_val is not None:
                    available_weight += weights[component]
                    weighted_sum     += weights[component] * score_val
                else:
                    unavailable.append(component)

            if unavailable:
                logger.debug(
                    "CrashDetector: %d component(s) unavailable (excluded from score): %s",
                    len(unavailable),
                    ", ".join(unavailable),
                )

            if available_weight <= 0:
                self._current_score = 0.0
            else:
                self._current_score = (weighted_sum / available_weight) * 10.0
                self._current_score = round(max(0.0, min(10.0, self._current_score)), 2)

            # Flatten to numeric for storage / logging (None → 0.0)
            self._component_scores = {k: (v if v is not None else 0.0) for k, v in scores.items()}

            # Update tier based on score
            self._update_tier(settings)

            logger.debug(
                "CrashDetector: score=%.2f tier=%s | atr=%.2f vel=%.2f liq=%s cross=%.2f ob=%.2f fr=%.2f oi=%s",
                self._current_score,
                self._current_tier,
                scores.get("atr_spike") or 0.0,
                scores.get("price_velocity") or 0.0,
                f"{scores['liquidation_cascade']:.2f}" if scores.get("liquidation_cascade") is not None else "n/a",
                scores.get("cross_asset_decline") or 0.0,
                scores.get("orderbook_imbalance") or 0.0,
                scores.get("funding_rate_flip") or 0.0,
                f"{scores['oi_collapse']:.2f}" if scores.get("oi_collapse") is not None else "n/a",
            )

            return self._current_score

    def _compute_atr_spike(self, df_by_symbol: dict[str, pd.DataFrame]) -> float:
        """
        ATR spike: score = min(1.0, (current_atr / baseline_atr - 1.0) / 1.5)
        where baseline_atr = rolling 20-bar ATR mean.
        """
        if not df_by_symbol:
            return 0.0

        spikes = []
        for symbol, df in df_by_symbol.items():
            if df is None or len(df) < 20 or "atr_14" not in df.columns:
                continue

            current_atr = float(df["atr_14"].iloc[-1])
            if current_atr <= 0:
                continue

            # Compute 20-bar baseline
            baseline_atr = float(df["atr_14"].tail(20).mean())
            if baseline_atr <= 0:
                baseline_atr = current_atr

            # Store for tracking
            self._atr_baselines[symbol] = baseline_atr

            ratio = current_atr / baseline_atr
            if ratio > 1.5:
                spike_score = min(1.0, (ratio - 1.0) / 1.5)
                spikes.append(spike_score)

        return max(spikes) if spikes else 0.0

    def _compute_price_velocity(self, df_by_symbol: dict[str, pd.DataFrame]) -> float:
        """
        Price velocity: 3-bar return z-score (30-bar rolling).
        score = max(0, min(1.0, (-z_score - 1.5) / 2.5))
        Only fires for negative price moves.
        """
        if not df_by_symbol:
            return 0.0

        velocities = []
        for symbol, df in df_by_symbol.items():
            if df is None or len(df) < 30 or "close" not in df.columns:
                continue

            # Compute 3-bar returns
            close = df["close"]
            ret_3bar = (close.iloc[-1] - close.iloc[-3]) / close.iloc[-3]

            # 30-bar rolling z-score
            returns_30 = close.pct_change().tail(30)
            if len(returns_30) < 10:
                continue

            mean_ret = returns_30.mean()
            std_ret = returns_30.std()
            if std_ret <= 0:
                continue

            z_score = (ret_3bar - mean_ret) / std_ret

            # Only negative moves trigger
            if z_score < -1.5:
                velocity_score = min(1.0, max(0.0, (-z_score - 1.5) / 2.5))
                velocities.append(velocity_score)

        return max(velocities) if velocities else 0.0

    def _compute_liquidation_cascade(self) -> Optional[float]:
        """
        Liquidation cascade: Try to read from Coinglass agent or intelligence data.
        Returns None when agent data is unavailable (so the component is excluded
        from the normalisation denominator rather than pulling the score down).
        """
        try:
            # Try to fetch from intelligence agents
            from core.agents.liquidation_flow_agent import LiquidationFlowAgent

            agent = LiquidationFlowAgent()
            data = agent.run()
            if data and isinstance(data, dict):
                severity = float(data.get("liquidation_severity", 0.0))
                return min(1.0, max(0.0, severity / 100.0))  # normalize if needed
        except Exception as exc:
            logger.debug("CrashDetector: liquidation agent unavailable: %s", exc)

        return None

    def _compute_cross_asset_decline(self, df_by_symbol: dict[str, pd.DataFrame]) -> float:
        """
        Cross-asset correlated decline: watchlist symbols declining simultaneously over 1 hour.
        score = declining_count/total_symbols * mean_decline_normalised
        Mean decline normalised: min(1.0, mean_decline_pct / 3.0)
        """
        if not df_by_symbol or len(df_by_symbol) < 2:
            return 0.0

        declines = []
        for symbol, df in df_by_symbol.items():
            if df is None or len(df) < 2 or "close" not in df.columns:
                continue

            # 1-hour change (1-bar on 1h timeframe, or approximate on other TFs)
            current_close = float(df["close"].iloc[-1])
            prev_close = float(df["close"].iloc[-2])
            if prev_close <= 0:
                continue

            pct_change = (current_close - prev_close) / prev_close
            if pct_change < 0:
                declines.append(abs(pct_change))

        if not declines:
            return 0.0

        declining_count = len(declines)
        mean_decline_pct = sum(declines) / len(declines)
        mean_decline_norm = min(1.0, mean_decline_pct / 3.0)

        return (declining_count / max(1, len(df_by_symbol))) * mean_decline_norm

    def _compute_orderbook_imbalance(self, tickers: dict) -> float:
        """
        Order book imbalance: From ticker bid/ask volumes if available.
        score = max(0, 1.0 - imbalance_ratio / 0.35)
        where imbalance_ratio = bid_size / (bid_size + ask_size)
        """
        if not tickers:
            return 0.0

        imbalances = []
        for symbol, ticker in tickers.items():
            if not ticker:
                continue

            bid_vol = float(ticker.get("bidVolume", 0.0))
            ask_vol = float(ticker.get("askVolume", 0.0))
            if bid_vol <= 0 or ask_vol <= 0:
                continue

            total = bid_vol + ask_vol
            if total <= 0:
                continue

            # High bid volume (lots of buy pressure) suggests imbalance
            imbalance_ratio = bid_vol / total
            # Extreme ratios (close to 1.0) indicate very unequal book
            if imbalance_ratio > 0.65 or imbalance_ratio < 0.35:
                imbalance_score = max(0.0, 1.0 - abs(0.5 - imbalance_ratio) / 0.15)
                imbalances.append(imbalance_score)

        return max(imbalances) if imbalances else 0.0

    def _compute_funding_rate_flip(self, tickers: dict) -> float:
        """
        Funding rate flip: score = min(1.0, max(0, (-funding_rate - 0.0001) / 0.0008))
        Strongly negative funding → score 1.0.
        """
        if not tickers:
            return 0.0

        flips = []
        for symbol, ticker in tickers.items():
            if not ticker:
                continue

            funding_rate = ticker.get("fundingRate")
            if funding_rate is None:
                continue

            funding_rate = float(funding_rate)
            # Negative funding is bullish (traders pay shorts), so negative flip = bearish
            if funding_rate < -0.0001:
                flip_score = min(1.0, max(0.0, (-funding_rate - 0.0001) / 0.0008))
                flips.append(flip_score)

        return max(flips) if flips else 0.0

    def _compute_oi_collapse(self) -> Optional[float]:
        """
        Open interest collapse: Try to read from intelligence agents.
        Returns None when agent data is unavailable (so the component is excluded
        from the normalisation denominator rather than pulling the score down).
        """
        try:
            from core.agents.onchain_agent import OnChainAgent

            agent = OnChainAgent()
            data = agent.run()
            if data and isinstance(data, dict):
                # Check if OI is declining sharply
                oi_change = float(data.get("oi_change_pct", 0.0))
                if oi_change < -20.0:  # >20% collapse
                    return min(1.0, abs(oi_change) / 100.0)
                return 0.0  # agent available but no collapse signal
        except Exception as exc:
            logger.debug("CrashDetector: OnChain agent unavailable: %s", exc)

        return None

    def _update_tier(self, settings) -> None:
        """
        Update current tier based on composite score.
        Entry is immediate. Recovery requires consecutive bars below threshold.
        """
        thresholds = settings.get("crash_detector.tier_thresholds", {})
        tier_defensive = float(thresholds.get("defensive", 5.0))
        tier_high_alert = float(thresholds.get("high_alert", 7.0))
        tier_emergency = float(thresholds.get("emergency", 8.0))
        tier_systemic = float(thresholds.get("systemic", 9.0))

        recovery_bars = int(settings.get("crash_detector.recovery_bars_required", 5))
        recovery_hysteresis = float(settings.get("crash_detector.recovery_hysteresis", 1.5))

        score = self._current_score
        old_tier = self._current_tier

        # Determine new tier (entry is immediate)
        if score >= tier_systemic:
            new_tier = TIER_SYSTEMIC
        elif score >= tier_emergency:
            new_tier = TIER_EMERGENCY
        elif score >= tier_high_alert:
            new_tier = TIER_HIGH_ALERT
        elif score >= tier_defensive:
            new_tier = TIER_DEFENSIVE
        else:
            new_tier = TIER_NORMAL

        # Recovery hysteresis: if returning to normal, require bars below threshold
        recovery_threshold = tier_defensive - recovery_hysteresis
        if new_tier == TIER_NORMAL and old_tier != TIER_NORMAL:
            if score < recovery_threshold:
                self._bars_below_recovery_threshold += 1
                if self._bars_below_recovery_threshold >= recovery_bars:
                    self._current_tier = TIER_NORMAL
                    self._bars_below_recovery_threshold = 0
                    logger.info("CrashDetector: recovered to NORMAL (score=%.2f)", score)
                    self._notify_controller(old_tier, new_tier)
                else:
                    logger.debug(
                        "CrashDetector: recovery in progress (%d/%d bars)",
                        self._bars_below_recovery_threshold,
                        recovery_bars,
                    )
            else:
                self._bars_below_recovery_threshold = 0
        else:
            self._bars_below_recovery_threshold = 0
            if new_tier != old_tier:
                self._current_tier = new_tier
                logger.warning(
                    "CrashDetector: tier transition %s → %s (score=%.2f)",
                    old_tier, new_tier, score,
                )
                self._notify_controller(old_tier, new_tier)

    def _notify_controller(self, old_tier: str, new_tier: str) -> None:
        """Notify CrashDefenseController of tier changes."""
        try:
            from core.risk.crash_defense_controller import get_crash_defense_controller

            ctrl = get_crash_defense_controller()
            ctrl.respond_to_tier(new_tier, self._current_score, self._component_scores)
        except Exception as exc:
            logger.debug("CrashDetector: controller notification failed: %s", exc)

    def start_monitoring(self, interval_seconds: int = 60) -> None:
        """Start the crash detection monitoring loop."""
        with self._lock:
            if self._running:
                return

            self._running = True

            # Try Qt timer first, fall back to threading.Timer
            try:
                from PySide6.QtCore import QTimer

                self._timer = QTimer()
                self._timer.timeout.connect(self._on_timer)
                self._timer.start(interval_seconds * 1000)
                logger.info("CrashDetector: started QTimer monitoring (%.0fs interval)", interval_seconds)
            except Exception:
                logger.debug("CrashDetector: Qt not available, using threading.Timer")
                self._schedule_next_check(interval_seconds)

    def stop_monitoring(self) -> None:
        """Stop the crash detection monitoring loop."""
        with self._lock:
            if not self._running:
                return

            self._running = False
            if self._timer is not None:
                try:
                    self._timer.stop()
                except Exception:
                    pass
            logger.info("CrashDetector: monitoring stopped")

    def _on_timer(self) -> None:
        """QTimer callback (called from Qt event loop)."""
        # In production, would fetch live data here; for now just periodic log
        pass

    def _schedule_next_check(self, interval_seconds: int) -> None:
        """Schedule next check via threading.Timer."""
        if not self._running:
            return

        timer = threading.Timer(interval_seconds, self._schedule_next_check, args=[interval_seconds])
        timer.daemon = True
        timer.start()


# Module-level singleton
_crash_detector = None


def get_crash_detector() -> CrashDetector:
    """Get or create the module-level CrashDetector singleton."""
    global _crash_detector
    if _crash_detector is None:
        _crash_detector = CrashDetector()
    return _crash_detector
