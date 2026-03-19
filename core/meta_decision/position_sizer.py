# ============================================================
# NEXUS TRADER — Position Sizer (Phase 1c/1d)
#
# Replaces the 4-tier multiplier in ConfluenceScorer with
# mathematically rigorous Kelly Criterion-based position sizing.
#
# Formula:
#   base_kelly = kelly_fraction * available_capital_usdt
#   vol_scalar = target_atr_pct / current_atr_pct (clamped [0.2, 3.0])
#   regime_mult = REGIME_RISK_MULTIPLIERS[regime]
#   score_mult = function(score)
#   drawdown_scalar = function(drawdown_pct)
#   size = base_kelly * vol_scalar * regime_mult * score_mult * drawdown_scalar
#   size = clamp to [cap_min, cap_max] then [min_size, max_size]
# ============================================================
from __future__ import annotations


class PositionSizer:
    """
    Kelly Criterion-based position sizing with regime and drawdown adjustments.

    Safe for trading by using half-Kelly and multiple risk dampeners.
    """

    REGIME_RISK_MULTIPLIERS: dict[str, float] = {
        "bull_trend":              1.0,
        "bear_trend":              0.7,
        "ranging":                 0.8,
        "volatility_expansion":    0.6,
        "volatility_compression":  0.5,
        "accumulation":            0.8,
        "distribution":            0.6,
        "crisis":                  0.0,           # halt
        "recovery":                0.7,
        "liquidation_cascade":     0.0,           # halt
        "squeeze":                 0.4,
        "uncertain":               0.4,
    }

    def __init__(
        self,
        kelly_fraction: float = 0.25,
        target_atr_pct: float = 0.008,
        min_size_usdt: float = 10.0,
        max_size_usdt: float = 0.0,
        max_capital_pct: float = 0.04,
        min_capital_pct: float = 0.003,
        loss_streak_trigger: int = 3,
        loss_streak_size_multiplier: float = 0.50,
        loss_streak_recovery_wins: int = 2,
        defensive_mode_multiplier: float = 0.25,
    ):
        """
        Initialize the position sizer using quarter-Kelly for safety.

        Parameters
        ----------
        kelly_fraction : float
            Fraction of Kelly Criterion to use (0.25 = quarter-Kelly for safety).
        target_atr_pct : float
            Target ATR as % of price (e.g., 0.008 = 0.8%).
        min_size_usdt : float
            Minimum position size in USDT.
        max_size_usdt : float
            Maximum position size in USDT. 0 = unlimited (constrained by capital_pct).
        max_capital_pct : float
            Maximum % of available capital per trade (e.g., 0.04 = 4% hard cap).
        min_capital_pct : float
            Minimum % of available capital per trade (e.g., 0.003 = 0.3% minimum).
        loss_streak_trigger : int
            Number of consecutive losses to trigger reduced sizing.
        loss_streak_size_multiplier : float
            Size multiplier when loss streak is active.
        loss_streak_recovery_wins : int
            Wins needed to recover from loss streak.
        defensive_mode_multiplier : float
            Size multiplier in crash/defensive mode.
        """
        self.kelly_fraction = kelly_fraction
        self.target_atr_pct = target_atr_pct
        self.min_size_usdt = min_size_usdt
        self.max_size_usdt = max_size_usdt
        self.max_capital_pct = max_capital_pct
        self.min_capital_pct = min_capital_pct
        self.loss_streak_trigger = loss_streak_trigger
        self.loss_streak_size_multiplier = loss_streak_size_multiplier
        self.loss_streak_recovery_wins = loss_streak_recovery_wins
        self.defensive_mode_multiplier = defensive_mode_multiplier
        # Internal state
        self._consecutive_losses: int = 0
        self._consecutive_wins: int = 0

    def calculate(
        self,
        available_capital_usdt: float,
        atr_value: float,
        entry_price: float,
        score: float,
        regime: str,
        drawdown_pct: float = 0.0,
        side: str = "long",
    ) -> float:
        """
        Calculate position size using quarter-Kelly Criterion with regime/drawdown adjustments.

        Parameters
        ----------
        available_capital_usdt : float
            Total available trading capital (USDT).
        atr_value : float
            ATR14 value at entry price.
        entry_price : float
            Entry price (absolute, e.g., BTC/USDT price).
        score : float
            Confluence score (0.0–1.0). Affects position multiplier.
        regime : str
            Market regime (e.g., "bull_trend", "ranging", "crisis").
        drawdown_pct : float
            Current drawdown as percentage (0.0–100.0). Halts if >= 15%.
        side : str
            Trade side ("long" or "short"). Used for defensive mode check.

        Returns
        -------
        float
            Position size in USDT, rounded to 2 decimals.
            Returns 0.0 if regime is halt (crisis, liquidation_cascade).
        """
        if available_capital_usdt <= 0:
            return 0.0

        # 1. Base Kelly allocation
        base_kelly = self.kelly_fraction * available_capital_usdt

        # 2. Volatility scalar (target_atr_pct / current_atr_pct)
        current_atr_pct = atr_value / entry_price if entry_price > 0 else 1.0
        if current_atr_pct > 0:
            vol_scalar = self.target_atr_pct / current_atr_pct
        else:
            vol_scalar = 1.0
        # Clamp to [0.2, 3.0]
        vol_scalar = max(0.2, min(3.0, vol_scalar))

        # 3. Regime multiplier
        regime_mult = self.REGIME_RISK_MULTIPLIERS.get(regime, 0.4)
        if regime_mult == 0.0:
            return 0.0  # Halt regime

        # 4. Score multiplier (confidence bonus)
        if score < 0.60:
            score_mult = 0.75
        elif score < 0.70:
            score_mult = 0.85
        elif score < 0.80:
            score_mult = 1.0
        elif score < 0.90:
            score_mult = 1.15
        else:
            score_mult = 1.3

        # 5. Drawdown scalar (linear interpolation with halts)
        drawdown_scalar = self._interpolate_drawdown_scalar(drawdown_pct)
        if drawdown_scalar == 0.0:
            return 0.0  # Force halt at >= 15% drawdown

        # 6. Loss streak scalar
        loss_streak_scalar = self.loss_streak_scalar

        # 7. Defensive mode check (crash detector)
        defensive_scalar = 1.0
        try:
            from core.risk.crash_detector import get_crash_detector
            crash_det = get_crash_detector()
            if crash_det.is_crash_mode and side.lower() in ("buy", "long"):
                defensive_scalar = self.defensive_mode_multiplier
        except Exception:
            pass

        # 8. Combine all factors
        size = base_kelly * vol_scalar * regime_mult * score_mult * drawdown_scalar * loss_streak_scalar * defensive_scalar

        # 9. Capital percentage bounds
        cap_min = available_capital_usdt * self.min_capital_pct
        cap_max = available_capital_usdt * self.max_capital_pct
        size = max(cap_min, min(cap_max, size))

        # 10. Absolute max_size_usdt limit (if set)
        if self.max_size_usdt > 0:
            size = min(size, self.max_size_usdt)

        # 11. Minimum size floor
        size = max(self.min_size_usdt, size)

        return round(size, 2)

    def register_trade_outcome(self, won: bool) -> None:
        """
        Register the outcome of a trade (win/loss) and update streak counters.

        Parameters
        ----------
        won : bool
            True if trade was profitable, False otherwise
        """
        if won:
            self._consecutive_wins += 1
            if self._consecutive_wins >= self.loss_streak_recovery_wins:
                self._consecutive_losses = 0
        else:
            self._consecutive_losses += 1
            self._consecutive_wins = 0

    @property
    def loss_streak_scalar(self) -> float:
        """
        Compute loss streak scalar multiplier.

        Returns
        -------
        float
            Size multiplier: loss_streak_size_multiplier if loss streak active, else 1.0
        """
        if self._consecutive_losses >= self.loss_streak_trigger:
            return self.loss_streak_size_multiplier
        return 1.0

    def _interpolate_drawdown_scalar(self, drawdown_pct: float) -> float:
        """
        Compute drawdown scalar with linear interpolation and halt thresholds.

        Breakpoints:
          dd=0%   → 1.0
          dd=5%   → 0.8
          dd=10%  → 0.6
          dd>=15% → 0.0 (halt)
        """
        if drawdown_pct < 0:
            return 1.0
        elif drawdown_pct >= 15.0:
            return 0.0  # Force halt
        elif drawdown_pct >= 10.0:
            # Linear interpolation between 10% (0.6) and 15% (0.0)
            return 0.6 * (15.0 - drawdown_pct) / 5.0
        elif drawdown_pct >= 5.0:
            # Linear interpolation between 5% (0.8) and 10% (0.6)
            return 0.8 - 0.2 * (drawdown_pct - 5.0) / 5.0
        else:
            # Linear interpolation between 0% (1.0) and 5% (0.8)
            return 1.0 - 0.2 * drawdown_pct / 5.0

    def is_halt_regime(self, regime: str) -> bool:
        """Return True if the regime has a risk multiplier of 0.0 (trading halt)."""
        return self.REGIME_RISK_MULTIPLIERS.get(regime, 0.4) == 0.0

    def get_regime_multiplier(self, regime: str) -> float:
        """Return the risk multiplier for a given regime."""
        return self.REGIME_RISK_MULTIPLIERS.get(regime, 0.4)
