# ============================================================
# NEXUS TRADER — Regime Transition Controller  (Phase 1a)
#
# Sits between RegimeClassifier and SignalGenerator.
# Prevents rapid regime oscillation via:
#   - Hysteresis (separate entry/exit thresholds)
#   - Minimum dwell time (required persistence)
#   - Confirmation count (consecutive matches needed)
#   - Confidence floor (ignore low-confidence proposals)
#   - Soft transitions (blended regime weights during switch)
# ============================================================
from __future__ import annotations

import logging
from typing import Optional

from core.regime.regime_classifier import ALL_REGIMES, REGIME_UNCERTAIN

logger = logging.getLogger(__name__)


class RegimeTransitionController:
    """
    Controls regime transitions to prevent oscillation and ensure stability.

    Enforces:
    - Hysteresis via separate entry/exit ADX thresholds
    - Minimum dwell time before allowing regime switch
    - Confirmation count (consecutive proposals needed)
    - Confidence floor (reject low-confidence proposals)
    - Soft transitions with blended weights during switch period

    Parameters
    ----------
    min_dwell_candles : int
        Minimum candles a regime must persist before switching is allowed (default 3)
    adx_bull_entry : float
        ADX threshold to enter bull trend (default 27.0)
    adx_bull_exit : float
        ADX threshold to exit bull trend (default 22.0)
    min_confidence : float
        Minimum confidence to accept a regime proposal (default 0.60)
    confirmation_count : int
        Number of consecutive same-regime proposals before accepting (default 3)
    """

    def __init__(
        self,
        min_dwell_candles: int = 3,
        adx_bull_entry: float = 27.0,
        adx_bull_exit: float = 22.0,
        adx_bear_entry: float = 25.0,
        adx_bear_exit: float = 20.0,
        min_confidence: float = 0.60,
        confirmation_count: int = 3,
    ):
        self.min_dwell_candles = min_dwell_candles
        self.adx_bull_entry = adx_bull_entry
        self.adx_bull_exit = adx_bull_exit
        self.adx_bear_entry = adx_bear_entry
        self.adx_bear_exit = adx_bear_exit
        self.min_confidence = min_confidence
        self.confirmation_count = confirmation_count

        # State tracking
        self._current_regime: str = REGIME_UNCERTAIN
        self._bars_in_current: int = 0
        self._proposed_regime: str = REGIME_UNCERTAIN
        self._proposal_count: int = 0
        self._transition_in_progress: bool = False
        self._bars_in_new_regime: int = 0
        self._blend_weight: float = 1.0

    def update(
        self,
        proposed_regime: str,
        confidence: float,
        features: dict,
    ) -> tuple[str, bool, float]:
        """
        Update controller state with a new regime proposal.

        Parameters
        ----------
        proposed_regime : str
            Regime proposed by RegimeClassifier
        confidence : float
            Confidence of the proposal (0.0-1.0)
        features : dict
            Feature dict from RegimeClassifier (contains adx, rsi, bb_width_ratio, etc.)

        Returns
        -------
        (confirmed_regime, transition_in_progress, blend_weight_new)
            confirmed_regime : str
                Current confirmed regime (may be same as current)
            transition_in_progress : bool
                True if regime was just switched and in soft-transition period
            blend_weight_new : float
                Weight for new regime during transition (0.0-1.0):
                  - During soft transition (first 2 bars): bars_in_new / min_dwell_candles, capped at 0.85
                  - After dwell complete: 1.0
                  - If not transitioning: 1.0 for current regime
        """
        # Reject if confidence below floor
        if confidence < self.min_confidence:
            self._bars_in_current += 1
            return self._current_regime, self._transition_in_progress, self._blend_weight

        # Check hysteresis (ADX-based for bull regimes)
        if not self._check_hysteresis(proposed_regime, features):
            self._bars_in_current += 1
            return self._current_regime, self._transition_in_progress, self._blend_weight

        # Same regime proposed again
        if proposed_regime == self._proposed_regime:
            self._proposal_count += 1
        else:
            # Different regime proposed; reset counter
            self._proposed_regime = proposed_regime
            self._proposal_count = 1

        # Check if we have enough confirmations
        if self._proposal_count >= self.confirmation_count:
            # Confirm the transition
            if proposed_regime != self._current_regime:
                self._current_regime = proposed_regime
                self._bars_in_current = 0
                self._transition_in_progress = True
                self._bars_in_new_regime = 0
                logger.info(
                    "Regime transition: %s -> %s (confirmed after %d proposals)",
                    self._current_regime,
                    proposed_regime,
                    self._proposal_count,
                )
            self._proposal_count = 0

        # Update dwell and transition state
        self._bars_in_current += 1
        if self._transition_in_progress:
            self._bars_in_new_regime += 1
            if self._bars_in_new_regime >= self.min_dwell_candles:
                # Soft transition complete
                self._transition_in_progress = False
                self._blend_weight = 1.0
                logger.info(
                    "Regime transition complete for %s (dwell %d candles)",
                    self._current_regime,
                    self._bars_in_new_regime,
                )
            else:
                # Still in soft transition
                self._blend_weight = min(
                    0.85,
                    self._bars_in_new_regime / self.min_dwell_candles,
                )
        else:
            self._blend_weight = 1.0

        return self._current_regime, self._transition_in_progress, self._blend_weight

    def _check_hysteresis(self, proposed_regime: str, features: dict) -> bool:
        """
        Apply hysteresis logic to prevent oscillation.

        Uses separate entry/exit ADX thresholds for both bull and bear regimes,
        preventing rapid flipping in ranging markets near regime boundaries.
        Other regimes pass through without hysteresis check.
        """
        if proposed_regime not in ["bull_trend", "bear_trend"]:
            return True

        adx = features.get("adx")
        if adx is None:
            return True

        if proposed_regime == "bull_trend":
            # Entering bull: need ADX >= entry threshold
            if self._current_regime != "bull_trend":
                return adx >= self.adx_bull_entry
            # Already in bull: need ADX >= exit threshold to stay
            return adx >= self.adx_bull_exit

        elif proposed_regime == "bear_trend":
            # Symmetric hysteresis for bear trend (entry/exit thresholds)
            if self._current_regime != "bear_trend":
                return adx >= self.adx_bear_entry
            return adx >= self.adx_bear_exit

        return True

    @property
    def current_regime(self) -> str:
        """Return the current confirmed regime."""
        return self._current_regime

    @property
    def dwell_remaining(self) -> int:
        """
        Return candles remaining until the current regime has dwelt long enough.

        Returns 0 if already dwelling long enough.
        """
        return max(0, self.min_dwell_candles - self._bars_in_current)

    def reset(self) -> None:
        """Clear all state and return to initial uncertain regime."""
        self._current_regime = REGIME_UNCERTAIN
        self._bars_in_current = 0
        self._proposed_regime = REGIME_UNCERTAIN
        self._proposal_count = 0
        self._transition_in_progress = False
        self._bars_in_new_regime = 0
        self._blend_weight = 1.0
        logger.info("RegimeTransitionController reset to initial state")

    def get_state(self) -> dict:
        """
        Return full state dict for GUI display and debugging.
        """
        return {
            "current_regime": self._current_regime,
            "bars_in_current": self._bars_in_current,
            "dwell_remaining": self.dwell_remaining,
            "proposed_regime": self._proposed_regime,
            "proposal_count": self._proposal_count,
            "transition_in_progress": self._transition_in_progress,
            "bars_in_new_regime": self._bars_in_new_regime,
            "blend_weight_new": self._blend_weight,
            "min_dwell_candles": self.min_dwell_candles,
            "adx_bull_entry": self.adx_bull_entry,
            "adx_bull_exit": self.adx_bull_exit,
            "adx_bear_entry": self.adx_bear_entry,
            "adx_bear_exit": self.adx_bear_exit,
            "min_confidence": self.min_confidence,
            "confirmation_count": self.confirmation_count,
        }
