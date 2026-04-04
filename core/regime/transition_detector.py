"""
TransitionDetector — detects regime phase shifts from HMM regime_probs deltas.

Integration: called in scanner._scan_symbol_with_regime() after regime classification
and transition controller, BEFORE signal generation.

Config gate: transition_detector.enabled (default False)
"""
from __future__ import annotations
import logging
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

@dataclass(frozen=True)
class TransitionSignal:
    """Immutable output from TransitionDetector."""
    transition_type: str        # TRANSITION_BREAKOUT, TRANSITION_EXPANSION, etc.
    direction: str              # "long", "short", or "neutral"
    confidence: float           # 0.0-1.0
    source_regime: str          # regime transitioning FROM
    target_regime: str          # regime transitioning TO
    bars_remaining: int         # expiry countdown (decremented each call)

class TransitionDetector:
    """
    Monitors regime_probs snapshots to detect phase transitions.

    One instance per symbol (stored in scanner._transition_detectors dict).
    Thread-safe: each ScanWorker creates its own instances.
    """

    # Transition type constants
    TRANSITION_BREAKOUT = "transition_breakout"
    TRANSITION_EXPANSION = "transition_expansion"
    TRANSITION_BREAKDOWN = "transition_breakdown"
    TRANSITION_TREND_FORMING = "transition_trend_forming"

    # Cooldown per transition type (bars)
    _COOLDOWN = 10

    def __init__(self):
        # Rolling buffer of (regime_probs, features) snapshots
        self._buffer: deque = deque(maxlen=10)

        # Active transition (only one at a time per symbol)
        self._active: Optional[TransitionSignal] = None
        self._active_bars_remaining: int = 0

        # Cooldown tracking: transition_type -> bars until re-eligible
        self._cooldowns: dict[str, int] = {}

    def detect(
        self,
        regime_probs: dict,
        features: dict,
        confirmed_regime: str,
        in_transition: bool,
    ) -> Optional[TransitionSignal]:
        """
        Called once per bar per symbol.

        Parameters
        ----------
        regime_probs : dict[str, float] from HMM ensemble (or empty if HMM not available)
        features : dict from RegimeClassifier.classify() — contains adx, bb_width_ratio,
                   vol_trend_pct, rsi, ema_slope_pct
        confirmed_regime : str — current confirmed regime after hysteresis
        in_transition : bool — True if RegimeTransitionController is in soft-transition

        Returns
        -------
        Optional[TransitionSignal] — emitted once when detected, then decremented until expiry.
        """
        # Decrement cooldowns
        for k in list(self._cooldowns):
            self._cooldowns[k] -= 1
            if self._cooldowns[k] <= 0:
                del self._cooldowns[k]

        # If active transition exists, decrement and return it
        if self._active is not None:
            self._active_bars_remaining -= 1
            if self._active_bars_remaining <= 0:
                logger.debug("TransitionDetector: %s expired", self._active.transition_type)
                self._active = None
                return None
            # Return updated copy with decremented bars_remaining
            return TransitionSignal(
                transition_type=self._active.transition_type,
                direction=self._active.direction,
                confidence=self._active.confidence,
                source_regime=self._active.source_regime,
                target_regime=self._active.target_regime,
                bars_remaining=self._active_bars_remaining,
            )

        # Store snapshot
        self._buffer.append({"regime_probs": dict(regime_probs), "features": dict(features), "regime": confirmed_regime})

        # Need at least 3 snapshots for delta analysis
        if len(self._buffer) < 3:
            return None

        # If regime_probs empty (HMM not available), use features-only detection
        if not regime_probs:
            return self._detect_features_only(features, confirmed_regime)

        # Compute deltas
        prev = self._buffer[-2]["regime_probs"]
        curr = regime_probs

        # Check each transition type
        signal = None
        signal = signal or self._check_accumulation_breakout(prev, curr, features, confirmed_regime)
        signal = signal or self._check_compression_expansion(prev, curr, features, confirmed_regime)
        signal = signal or self._check_distribution_breakdown(prev, curr, features, confirmed_regime)
        signal = signal or self._check_recovery_trend(prev, curr, features, confirmed_regime)

        if signal is not None:
            self._active = signal
            self._active_bars_remaining = signal.bars_remaining
            self._cooldowns[signal.transition_type] = self._COOLDOWN
            logger.info(
                "TransitionDetector: DETECTED %s dir=%s conf=%.2f (%s -> %s) expires=%d bars",
                signal.transition_type, signal.direction, signal.confidence,
                signal.source_regime, signal.target_regime, signal.bars_remaining,
            )

        return signal

    def _check_accumulation_breakout(self, prev, curr, features, regime):
        """accumulation→breakout: accum_prob drops, bull/vol_exp prob rises, ADX crosses 20, volume surge."""
        if self.TRANSITION_BREAKOUT in self._cooldowns:
            return None
        if regime not in ("accumulation", "ranging"):
            return None

        accum_drop = prev.get("accumulation", 0) - curr.get("accumulation", 0)
        bull_rise = curr.get("bull_trend", 0) - prev.get("bull_trend", 0)
        bear_rise = curr.get("bear_trend", 0) - prev.get("bear_trend", 0)
        vol_exp_rise = curr.get("volatility_expansion", 0) - prev.get("volatility_expansion", 0)

        adx = features.get("adx", 0)
        vol_trend = features.get("vol_trend_pct", 0)

        # Core condition: accumulation probability dropping significantly
        if accum_drop < 0.12:
            return None

        # Target probability rising
        target_rise = max(bull_rise, bear_rise, vol_exp_rise)
        if target_rise < 0.10:
            return None

        # Confirmation: ADX showing trend emergence or volume surge
        adx_confirming = adx > 18  # ADX approaching trend threshold
        vol_confirming = vol_trend > 15  # Volume expansion

        if not (adx_confirming or vol_confirming):
            return None

        # Verify with 3-bar lookback: is this a sustained shift?
        if len(self._buffer) >= 3:
            old = self._buffer[-3]["regime_probs"]
            sustained_drop = old.get("accumulation", 0) - curr.get("accumulation", 0)
            if sustained_drop < 0.15:
                return None

        # Direction: follows the rising target
        if bull_rise >= bear_rise:
            direction = "long"
            target = "bull_trend"
        else:
            direction = "short"
            target = "bear_trend"

        confidence = min(0.85, 0.50 + accum_drop + target_rise * 0.5)

        return TransitionSignal(
            transition_type=self.TRANSITION_BREAKOUT,
            direction=direction,
            confidence=round(confidence, 3),
            source_regime=regime,
            target_regime=target,
            bars_remaining=5,
        )

    def _check_compression_expansion(self, prev, curr, features, regime):
        """compression→expansion: BB_width_ratio expanding, ATR rising, vol_exp prob rising."""
        if self.TRANSITION_EXPANSION in self._cooldowns:
            return None
        if regime not in ("volatility_compression", "ranging", "accumulation"):
            return None

        bb_ratio = features.get("bb_width_ratio", 1.0)
        vol_trend = features.get("vol_trend_pct", 0)

        # BB expanding from compressed state
        if bb_ratio < 0.7:
            return None  # Still compressed, not expanding yet

        # Check if was recently compressed (within last 3 bars)
        was_compressed = False
        for i in range(max(0, len(self._buffer) - 3), len(self._buffer) - 1):
            old_bb = self._buffer[i]["features"].get("bb_width_ratio", 1.0)
            if old_bb < 0.6:
                was_compressed = True
                break

        if not was_compressed:
            return None

        # Volume expansion confirming
        if vol_trend < 10:
            return None

        # Probability shift toward expansion
        vol_exp_rise = curr.get("volatility_expansion", 0) - prev.get("volatility_expansion", 0)
        if vol_exp_rise < 0.08:
            return None

        # Direction: use price action — EMA slope
        ema_slope = features.get("ema_slope_pct", 0)
        if abs(ema_slope) < 0.01:
            direction = "neutral"  # Direction not yet determined
        elif ema_slope > 0:
            direction = "long"
        else:
            direction = "short"

        confidence = min(0.80, 0.45 + vol_exp_rise + vol_trend / 200.0)

        return TransitionSignal(
            transition_type=self.TRANSITION_EXPANSION,
            direction=direction,
            confidence=round(confidence, 3),
            source_regime=regime,
            target_regime="volatility_expansion",
            bars_remaining=4,
        )

    def _check_distribution_breakdown(self, prev, curr, features, regime):
        """distribution→breakdown: dist_prob drops, bear_prob rises, price breaks below 20-bar low."""
        if self.TRANSITION_BREAKDOWN in self._cooldowns:
            return None
        if regime not in ("distribution", "uncertain"):
            return None

        dist_drop = prev.get("distribution", 0) - curr.get("distribution", 0)
        bear_rise = curr.get("bear_trend", 0) - prev.get("bear_trend", 0)

        if dist_drop < 0.12 or bear_rise < 0.10:
            return None

        # Price action confirmation: near 20-bar low
        price_from_high = features.get("price_from_20h_pct", 0)
        vol_trend = features.get("vol_trend_pct", 0)

        if price_from_high > -3:  # Not far enough from high
            return None
        if vol_trend < 10:  # Need volume expansion
            return None

        confidence = min(0.80, 0.50 + dist_drop + bear_rise * 0.5)

        return TransitionSignal(
            transition_type=self.TRANSITION_BREAKDOWN,
            direction="short",
            confidence=round(confidence, 3),
            source_regime=regime,
            target_regime="bear_trend",
            bars_remaining=5,
        )

    def _check_recovery_trend(self, prev, curr, features, regime):
        """recovery→trend: ADX crosses 25 from below, sustained positive EMA slope."""
        if self.TRANSITION_TREND_FORMING in self._cooldowns:
            return None
        if regime != "recovery":
            return None

        adx = features.get("adx", 0)
        ema_slope = features.get("ema_slope_pct", 0)

        # ADX must be crossing the trend threshold
        if adx < 23:
            return None

        # Check if ADX was below threshold recently
        was_below = False
        for i in range(max(0, len(self._buffer) - 5), len(self._buffer) - 1):
            old_adx = self._buffer[i]["features"].get("adx", 30)
            if old_adx < 22:
                was_below = True
                break

        if not was_below:
            return None

        # EMA slope must be positive (bull recovery)
        if ema_slope <= 0:
            return None

        # Must have been in recovery for at least 5 bars
        recovery_bars = sum(1 for b in self._buffer if b["regime"] == "recovery")
        if recovery_bars < 5:
            return None

        direction = "long" if ema_slope > 0 else "short"
        confidence = min(0.75, 0.45 + (adx - 23) / 10.0)

        return TransitionSignal(
            transition_type=self.TRANSITION_TREND_FORMING,
            direction=direction,
            confidence=round(confidence, 3),
            source_regime="recovery",
            target_regime="bull_trend" if direction == "long" else "bear_trend",
            bars_remaining=3,
        )

    def _detect_features_only(self, features, regime):
        """Fallback detection when HMM regime_probs not available — uses features only."""
        # Only check the most reliable transition: compression→expansion via BB
        if regime in ("volatility_compression", "ranging"):
            bb_ratio = features.get("bb_width_ratio", 1.0)
            vol_trend = features.get("vol_trend_pct", 0)

            was_compressed = False
            for i in range(max(0, len(self._buffer) - 3), len(self._buffer) - 1):
                old_bb = self._buffer[i]["features"].get("bb_width_ratio", 1.0)
                if old_bb < 0.6:
                    was_compressed = True
                    break

            if was_compressed and bb_ratio >= 0.75 and vol_trend > 15:
                if self.TRANSITION_EXPANSION not in self._cooldowns:
                    ema_slope = features.get("ema_slope_pct", 0)
                    direction = "long" if ema_slope > 0 else "short" if ema_slope < 0 else "neutral"
                    signal = TransitionSignal(
                        transition_type=self.TRANSITION_EXPANSION,
                        direction=direction,
                        confidence=0.55,
                        source_regime=regime,
                        target_regime="volatility_expansion",
                        bars_remaining=4,
                    )
                    self._active = signal
                    self._active_bars_remaining = 4
                    self._cooldowns[self.TRANSITION_EXPANSION] = self._COOLDOWN
                    logger.info("TransitionDetector: features-only EXPANSION detected dir=%s", direction)
                    return signal
        return None

    def reset(self):
        """Clear all state. Called on symbol change or scan restart."""
        self._buffer.clear()
        self._active = None
        self._active_bars_remaining = 0
        self._cooldowns.clear()

    @property
    def is_active(self) -> bool:
        return self._active is not None

    def get_state(self) -> dict:
        """Diagnostic state for rationale panel."""
        return {
            "active_transition": self._active.transition_type if self._active else None,
            "direction": self._active.direction if self._active else None,
            "confidence": self._active.confidence if self._active else 0.0,
            "bars_remaining": self._active_bars_remaining,
            "cooldowns": dict(self._cooldowns),
            "buffer_size": len(self._buffer),
        }
