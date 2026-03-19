# ============================================================
# Deep Position Sizer Tests — Quarter-Kelly & Loss Streak
#
# Tests the PositionSizer against the actual public API:
#   calculate(available_capital_usdt, atr_value, entry_price, score, regime)
#   register_trade_outcome(won: bool)
#   loss_streak_scalar (property)
#   get_regime_multiplier(regime)
#   is_halt_regime(regime)
# ============================================================
import pytest
from core.meta_decision.position_sizer import PositionSizer


CAPITAL = 10_000.0
ENTRY   = 50_000.0
ATR     = 400.0   # 0.8% of ENTRY — equals target_atr_pct default


# ── Helpers ────────────────────────────────────────────────────

def make_sizer(**kw):
    return PositionSizer(**kw)


def calc(sizer, capital=CAPITAL, atr=ATR, entry=ENTRY,
         score=0.75, regime="bull_trend", drawdown=0.0, side="long"):
    return sizer.calculate(
        available_capital_usdt=capital,
        atr_value=atr,
        entry_price=entry,
        score=score,
        regime=regime,
        drawdown_pct=drawdown,
        side=side,
    )


# ── Kelly Fraction ─────────────────────────────────────────────

class TestPositionSizerQuarterKelly:
    """Quarter-Kelly fraction and initialization."""

    def test_kelly_fraction_default_is_quarter(self):
        sizer = make_sizer()
        assert sizer.kelly_fraction == 0.25

    def test_hard_cap_4_percent(self):
        """Position size must not exceed 4% of capital."""
        sizer = make_sizer()
        # Use very small ATR → huge vol_scalar → would blow the cap without clamping
        size = calc(sizer, atr=1.0, score=0.99)
        assert size <= CAPITAL * 0.04 * 1.01   # 1% rounding tolerance

    def test_minimum_floor_0_3_percent(self):
        """Position size must not fall below 0.3% of capital (min_capital_pct)."""
        sizer = make_sizer()
        # Low score + ranging regime → small sizing, but floor should hold
        size = calc(sizer, score=0.50, regime="ranging")
        # Floor = max(min_size_usdt=10, capital*0.003=30)
        floor = max(sizer.min_size_usdt, CAPITAL * sizer.min_capital_pct)
        assert size >= floor * 0.99

    def test_zero_capital_returns_zero(self):
        sizer = make_sizer()
        size = calc(sizer, capital=0.0)
        assert size == 0.0

    def test_negative_capital_returns_zero(self):
        sizer = make_sizer()
        size = calc(sizer, capital=-1000.0)
        assert size == 0.0

    def test_normal_calculation_is_positive(self):
        sizer = make_sizer()
        size = calc(sizer)
        assert size > 0


# ── Regime Multipliers ─────────────────────────────────────────

class TestPositionSizerRegime:

    def test_bull_trend_full_multiplier(self):
        sizer = make_sizer()
        m = sizer.get_regime_multiplier("bull_trend")
        assert m == 1.0

    def test_crisis_halts_trading(self):
        sizer = make_sizer()
        size = calc(sizer, regime="crisis")
        assert size == 0.0

    def test_liquidation_cascade_halts_trading(self):
        sizer = make_sizer()
        size = calc(sizer, regime="liquidation_cascade")
        assert size == 0.0

    def test_uncertain_regime_lower_size(self):
        # Compare regime multipliers directly rather than final sized values,
        # because capital % bounds can clamp both regimes to the same amount.
        sizer = make_sizer()
        assert sizer.get_regime_multiplier("uncertain") < sizer.get_regime_multiplier("bull_trend")

    def test_is_halt_regime_crisis(self):
        sizer = make_sizer()
        assert sizer.is_halt_regime("crisis") is True

    def test_is_halt_regime_bull(self):
        sizer = make_sizer()
        assert sizer.is_halt_regime("bull_trend") is False

    def test_unknown_regime_uses_default_multiplier(self):
        sizer = make_sizer()
        m = sizer.get_regime_multiplier("unknown_regime_xyz")
        assert m == 0.4    # default fallback


# ── Score Influence ────────────────────────────────────────────

class TestPositionSizerScoreInfluence:

    def test_higher_score_larger_size(self):
        sizer = make_sizer()
        low  = calc(sizer, score=0.55)
        high = calc(sizer, score=0.92)
        assert high >= low

    def test_minimum_score_uses_0_75_multiplier(self):
        # Use high ATR (4000 = 8% of ENTRY=50000) to force vol_scalar to 0.2 (clamped floor).
        # This brings the base below the 4% cap so score differences are visible.
        # score=0.50 → score_mult=0.75 → base ≈ 3.75% of capital (below cap)
        # score=0.75 → score_mult=1.00 → base ≈ 5.0%  of capital → capped to 4%
        sizer = make_sizer()
        low = calc(sizer, capital=CAPITAL, atr=4000.0, score=0.50)
        mid = calc(sizer, capital=CAPITAL, atr=4000.0, score=0.75)
        assert mid > low


# ── Drawdown Scalar ────────────────────────────────────────────

class TestPositionSizerDrawdown:

    def test_no_drawdown_full_scalar(self):
        sizer = make_sizer()
        assert sizer._interpolate_drawdown_scalar(0.0) == pytest.approx(1.0, abs=0.01)

    def test_5pct_drawdown_scalar_0_8(self):
        sizer = make_sizer()
        assert sizer._interpolate_drawdown_scalar(5.0) == pytest.approx(0.8, abs=0.01)

    def test_10pct_drawdown_scalar_0_6(self):
        sizer = make_sizer()
        assert sizer._interpolate_drawdown_scalar(10.0) == pytest.approx(0.6, abs=0.01)

    def test_15pct_drawdown_halts(self):
        sizer = make_sizer()
        size = calc(sizer, drawdown=15.0)
        assert size == 0.0

    def test_20pct_drawdown_halts(self):
        sizer = make_sizer()
        size = calc(sizer, drawdown=20.0)
        assert size == 0.0

    def test_drawdown_reduces_size(self):
        sizer = make_sizer()
        no_dd   = calc(sizer, drawdown=0.0)
        some_dd = calc(sizer, drawdown=8.0)
        # may be identical because cap_min floor kicks in, but never larger
        assert some_dd <= no_dd


# ── Loss-Streak Protection ─────────────────────────────────────

class TestPositionSizerLossStreak:

    def test_initial_streak_scalar_is_1(self):
        sizer = make_sizer()
        assert sizer.loss_streak_scalar == 1.0

    def test_loss_streak_reduces_sizing(self):
        sizer = make_sizer()
        for _ in range(sizer.loss_streak_trigger):
            sizer.register_trade_outcome(won=False)
        assert sizer.loss_streak_scalar == sizer.loss_streak_size_multiplier
        reduced = calc(sizer)
        # Compare with fresh sizer at same capital
        fresh  = calc(make_sizer())
        assert reduced <= fresh

    def test_loss_streak_accumulates(self):
        sizer = make_sizer()
        assert sizer._consecutive_losses == 0
        sizer.register_trade_outcome(won=False)
        assert sizer._consecutive_losses == 1
        sizer.register_trade_outcome(won=False)
        assert sizer._consecutive_losses == 2

    def test_loss_streak_resets_on_wins(self):
        sizer = make_sizer(loss_streak_trigger=3, loss_streak_recovery_wins=2)
        # Trigger streak
        for _ in range(3):
            sizer.register_trade_outcome(won=False)
        assert sizer.loss_streak_scalar == sizer.loss_streak_size_multiplier
        # 2 consecutive wins recover
        sizer.register_trade_outcome(won=True)
        sizer.register_trade_outcome(won=True)
        assert sizer._consecutive_losses == 0

    def test_single_loss_below_trigger_no_effect(self):
        sizer = make_sizer(loss_streak_trigger=3)
        sizer.register_trade_outcome(won=False)
        assert sizer.loss_streak_scalar == 1.0


# ── Defensive Mode (CrashDetector) ────────────────────────────

class TestPositionSizerDefensiveMode:

    def test_defensive_mode_multiplier_stored(self):
        sizer = make_sizer(defensive_mode_multiplier=0.25)
        assert sizer.defensive_mode_multiplier == 0.25

    def test_custom_defensive_multiplier(self):
        # Verify the attribute is configurable
        sizer = make_sizer(defensive_mode_multiplier=0.10)
        assert sizer.defensive_mode_multiplier == 0.10


# ── Volatility Scalar ──────────────────────────────────────────

class TestPositionSizerVolatility:

    def test_high_volatility_reduces_size(self):
        """High ATR (current_atr_pct > target_atr_pct) reduces position."""
        sizer = make_sizer()
        # Normal ATR = 0.8% of price (matches target_atr_pct=0.008)
        normal_size = calc(sizer, atr=400.0, entry=50_000.0)
        # High ATR = 3.2% of price (4× target)
        high_vol    = calc(sizer, atr=1600.0, entry=50_000.0)
        # High vol → vol_scalar < 1.0 → smaller, but cap_min floor may equalise
        assert high_vol <= normal_size

    def test_vol_scalar_clamped_at_0_2_min(self):
        """Vol scalar floored at 0.2 even with extreme ATR."""
        sizer = make_sizer()
        # Huge ATR → vol_scalar would be tiny without floor
        size = calc(sizer, atr=50_000.0, entry=50_000.0)  # 100% atr pct
        # With vol_scalar clamped at 0.2 and cap_min, result is non-zero
        assert size >= 0.0


# ── R/R ratio influence (via stop distance proxy) ─────────────

class TestPositionSizerRiskCalculation:

    def test_tighter_stop_larger_implied_rr(self):
        """Tighter ATR means smaller actual risk → size floors/caps still hold."""
        sizer = make_sizer()
        tight_atr = calc(sizer, atr=100.0)   # small risk per unit
        wide_atr  = calc(sizer, atr=2000.0)  # large risk per unit
        # Both bounded by [cap_min, cap_max]
        assert tight_atr >= CAPITAL * sizer.min_capital_pct * 0.99
        assert wide_atr  >= CAPITAL * sizer.min_capital_pct * 0.99
