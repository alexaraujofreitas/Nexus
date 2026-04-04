# core/meta_decision/regime_capital_allocator.py
"""
RegimeCapitalAllocator — adjusts position sizing risk% based on regime expected edge.

Integration: called from PositionSizer.calculate_risk_based() to multiply risk_usdt.

Config gate: capital.regime_scaling_enabled (default False)

Safety constraints:
- Never increases risk above 1.5x base (prevents runaway sizing)
- Crisis/liquidation always returns 0.0 (no new positions)
- Transition signals use 0.60x multiplier (uncertainty discount)
- Fallback trades use the multiplier from CoverageGuarantee (0.3-0.5x)
"""
from __future__ import annotations
import logging
from typing import Optional

logger = logging.getLogger(__name__)


# Regime risk multipliers — based on validated backtest edge profiles
# These are applied ON TOP of the base risk_pct (0.5%) and existing caps
_REGIME_MULTIPLIERS: dict[str, float] = {
    "bull_trend":              1.20,   # Highest edge: PBL PF=1.27 validated
    "bear_trend":              1.10,   # Strong edge: SLC PF=1.55 validated
    "volatility_expansion":    0.80,   # Moderate: MB PF ~1.21
    "ranging":                 0.70,   # Unvalidated: RAM pending
    "accumulation":            0.70,   # Unvalidated: RAM pending
    "recovery":                0.75,   # Post-crisis: moderate
    "volatility_compression":  0.50,   # Pre-breakout: minimal until resolved
    "distribution":            0.60,   # Risk-off signal
    "uncertain":               0.50,   # Low confidence
    "squeeze":                 0.40,   # Extreme conditions
    "crisis":                  0.00,   # No new positions
    "liquidation_cascade":     0.00,   # No new positions
}

# Max capital per regime (percentage of total equity)
_REGIME_MAX_CAPITAL: dict[str, float] = {
    "bull_trend":              0.050,  # 5.0%
    "bear_trend":              0.045,  # 4.5%
    "volatility_expansion":    0.030,  # 3.0%
    "ranging":                 0.025,  # 2.5%
    "accumulation":            0.025,  # 2.5%
    "recovery":                0.030,  # 3.0%
    "volatility_compression":  0.020,  # 2.0%
    "distribution":            0.020,  # 2.0%
    "uncertain":               0.020,  # 2.0%
    "squeeze":                 0.015,  # 1.5%
    "crisis":                  0.000,
    "liquidation_cascade":     0.000,
}

# Portfolio heat budget per regime family
_HEAT_BUDGET: dict[str, float] = {
    "trending":    0.035,  # 3.5% for bull_trend + bear_trend
    "expansion":   0.015,  # 1.5% for vol_expansion + transitions
    "ranging":     0.010,  # 1.0% for ranging + accumulation
}

_REGIME_TO_FAMILY: dict[str, str] = {
    "bull_trend": "trending", "bear_trend": "trending",
    "volatility_expansion": "expansion", "recovery": "expansion",
    "ranging": "ranging", "accumulation": "ranging",
    "distribution": "ranging", "uncertain": "ranging",
    "volatility_compression": "expansion", "squeeze": "expansion",
}

# Hard cap: never increase risk above this multiplier
_MAX_MULTIPLIER = 1.50


class RegimeCapitalAllocator:
    """
    Provides regime-aware risk multipliers and capital caps.

    Stateless — can be instantiated per call or kept as singleton.
    """

    def __init__(self):
        self._enabled: Optional[bool] = None

    def _is_enabled(self) -> bool:
        """Lazy check of config gate."""
        if self._enabled is None:
            try:
                from config.settings import settings
                self._enabled = bool(settings.get("capital.regime_scaling_enabled", False))
            except Exception:
                self._enabled = False
        return self._enabled

    def get_risk_multiplier(
        self,
        regime: str,
        is_transition: bool = False,
        is_fallback: bool = False,
        fallback_multiplier: float = 1.0,
    ) -> float:
        """
        Get the risk multiplier for position sizing.

        Parameters
        ----------
        regime : str — current confirmed regime
        is_transition : bool — True if this is a transition signal trade
        is_fallback : bool — True if this is a CoverageGuarantee fallback trade
        fallback_multiplier : float — size multiplier from CoverageGuarantee (0.3-0.5)

        Returns
        -------
        float — multiplier to apply to base risk_usdt. Range [0.0, 1.50].
        """
        if not self._is_enabled():
            return 1.0  # Passthrough when disabled

        base_mult = _REGIME_MULTIPLIERS.get(regime, 0.50)

        # Transition discount
        if is_transition:
            base_mult = min(base_mult, 0.60)
            logger.debug("RegimeCapitalAllocator: transition discount -> %.2f", base_mult)

        # Fallback cap
        if is_fallback:
            base_mult = min(base_mult, fallback_multiplier)
            logger.debug("RegimeCapitalAllocator: fallback cap -> %.2f", base_mult)

        # Hard cap
        result = min(base_mult, _MAX_MULTIPLIER)

        logger.debug(
            "RegimeCapitalAllocator: regime=%s transition=%s fallback=%s -> mult=%.2f",
            regime, is_transition, is_fallback, result,
        )
        return result

    def get_max_capital_pct(self, regime: str) -> float:
        """Get regime-specific max capital percentage."""
        if not self._is_enabled():
            return 0.04  # Default 4% passthrough
        return _REGIME_MAX_CAPITAL.get(regime, 0.020)

    def get_heat_budget(self, regime: str) -> float:
        """Get heat budget for this regime's family."""
        family = _REGIME_TO_FAMILY.get(regime, "ranging")
        return _HEAT_BUDGET.get(family, 0.010)

    def get_regime_family(self, regime: str) -> str:
        """Get the family classification for a regime."""
        return _REGIME_TO_FAMILY.get(regime, "ranging")

    @staticmethod
    def get_all_multipliers() -> dict:
        """Return full multiplier table for diagnostics/dashboard."""
        return dict(_REGIME_MULTIPLIERS)

    @staticmethod
    def get_all_max_capital() -> dict:
        """Return full max capital table for diagnostics/dashboard."""
        return dict(_REGIME_MAX_CAPITAL)


# Module-level singleton
_allocator: Optional[RegimeCapitalAllocator] = None

def get_regime_capital_allocator() -> RegimeCapitalAllocator:
    """Get or create the singleton RegimeCapitalAllocator."""
    global _allocator
    if _allocator is None:
        _allocator = RegimeCapitalAllocator()
    return _allocator
