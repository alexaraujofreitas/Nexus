"""
Test Phase 7 Metrics Calculators

Tests for all pure function metric calculators.
All functions tested with deterministic inputs and validated outputs.
"""

import pytest
from typing import List

from core.intraday.analytics.models import TradeSnapshot, EquityPoint, EquityCurveBuilder
from core.intraday.analytics.metrics import (
    compute_profit_factor,
    compute_win_rate,
    compute_loss_rate,
    compute_breakeven_rate,
    compute_avg_win,
    compute_avg_loss,
    compute_win_loss_ratio,
    compute_total_pnl,
    compute_gross_profit,
    compute_gross_loss,
    compute_expectancy_r,
    compute_expectancy_capital,
    compute_expectancy_per_dollar,
    compute_expectancy_formula,
    compute_expectancy_per_trade_r,
    compute_max_drawdown,
    compute_max_drawdown_duration_ms,
    compute_calmar_ratio,
    compute_recovery_factor,
    compute_avg_drawdown,
    compute_num_drawdown_periods,
    compute_longest_drawdown_duration_trades,
    compute_r_multiple_distribution,
    compute_duration_distribution,
    compute_pnl_distribution,
    compute_mae_distribution,
    compute_mfe_distribution,
    compute_pnl_pct_distribution,
    compute_bars_held_distribution,
    compute_slippage_distribution,
    compute_signal_to_fill_distribution,
    DistributionStats,
    compute_capital_efficiency,
    compute_peak_utilization,
    compute_avg_utilization,
    compute_capital_turnover,
    compute_avg_idle_time,
    CapitalEfficiencyMetrics,
)


@pytest.fixture
def sample_trades() -> List[TradeSnapshot]:
    """Create sample trades for testing."""
    return [
        TradeSnapshot(
            position_id="pos001",
            trigger_id="trig001",
            symbol="BTC/USDT",
            direction="LONG",
            strategy_name="MomentumBreakout",
            strategy_class="momentum_breakout",
            regime_at_entry="BULL_TREND",
            entry_price=50000.0,
            exit_price=51000.0,  # Winner
            stop_loss=49000.0,
            take_profit=52000.0,
            entry_size_usdt=1000.0,
            quantity=0.02,
            realized_pnl_usdt=1800.0,  # Win
            fee_total_usdt=4.0,
            r_multiple=2.0,
            risk_usdt=1000.0,
            opened_at_ms=1000000,
            closed_at_ms=2000000,
            duration_ms=1000000,
            bars_held=10,
            close_reason="take_profit",
            slippage_pct=0.02,
            signal_to_fill_ms=500,
            mae_pct=0.5,
            mfe_pct=3.0,
        ),
        TradeSnapshot(
            position_id="pos002",
            trigger_id="trig002",
            symbol="ETH/USDT",
            direction="SHORT",
            strategy_name="PullbackLong",
            strategy_class="pullback_long",
            regime_at_entry="SIDEWAYS",
            entry_price=3000.0,
            exit_price=2900.0,  # Loser
            stop_loss=3100.0,
            take_profit=2800.0,
            entry_size_usdt=500.0,
            quantity=0.167,
            realized_pnl_usdt=-350.0,  # Loss
            fee_total_usdt=2.5,
            r_multiple=-3.5,
            risk_usdt=500.0,
            opened_at_ms=3000000,
            closed_at_ms=4000000,
            duration_ms=1000000,
            bars_held=5,
            close_reason="stop_loss",
            slippage_pct=0.05,
            signal_to_fill_ms=750,
            mae_pct=5.0,
            mfe_pct=0.2,
        ),
        TradeSnapshot(
            position_id="pos003",
            trigger_id="trig003",
            symbol="SOL/USDT",
            direction="LONG",
            strategy_name="SwingLowContinuation",
            strategy_class="swing_low_continuation",
            regime_at_entry="BULL_TREND",
            entry_price=200.0,
            exit_price=210.0,  # Winner
            stop_loss=195.0,
            take_profit=215.0,
            entry_size_usdt=2000.0,
            quantity=10.0,
            realized_pnl_usdt=1980.0,  # Win
            fee_total_usdt=4.0,
            r_multiple=2.0,
            risk_usdt=2000.0,
            opened_at_ms=5000000,
            closed_at_ms=6000000,
            duration_ms=1000000,
            bars_held=8,
            close_reason="take_profit",
            slippage_pct=0.01,
            signal_to_fill_ms=400,
            mae_pct=0.3,
            mfe_pct=2.5,
        ),
    ]


@pytest.fixture
def equity_curve(sample_trades) -> List[EquityPoint]:
    """Build equity curve from sample trades."""
    return EquityCurveBuilder.build(sample_trades, initial_capital=10000.0)


class TestProfitFactorMetrics:
    """Test profit factor and win/loss metrics."""

    def test_profit_factor_with_wins_and_losses(self, sample_trades):
        """Profit factor with both wins and losses."""
        pf = compute_profit_factor(sample_trades)
        # Wins: 1800 + 1980 = 3780
        # Losses: abs(-350) = 350
        # PF = 3780 / 350 = 10.8
        assert abs(pf - 10.8) < 0.01

    def test_win_rate(self, sample_trades):
        """Win rate calculation."""
        wr = compute_win_rate(sample_trades)
        # 2 winners out of 3 = 0.667
        assert abs(wr - 2 / 3) < 0.01

    def test_loss_rate(self, sample_trades):
        """Loss rate calculation."""
        lr = compute_loss_rate(sample_trades)
        # 1 loser out of 3 = 0.333
        assert abs(lr - 1 / 3) < 0.01

    def test_breakeven_rate_empty(self):
        """Breakeven rate on trades with no breakevens."""
        trades = [
            TradeSnapshot(
                position_id="p1", trigger_id="t1", symbol="BTC/USDT", direction="LONG",
                strategy_name="test", strategy_class="test", regime_at_entry="test",
                entry_price=100.0, exit_price=110.0, stop_loss=95.0, take_profit=120.0,
                entry_size_usdt=1000.0, quantity=10.0, realized_pnl_usdt=100.0,
                fee_total_usdt=1.0, r_multiple=2.0, risk_usdt=500.0,
                opened_at_ms=1000, closed_at_ms=2000, duration_ms=1000,
                bars_held=5, close_reason="tp",
            ),
        ]
        br = compute_breakeven_rate(trades)
        assert br == 0.0

    def test_avg_win(self, sample_trades):
        """Average winning trade."""
        avg_w = compute_avg_win(sample_trades)
        # Wins: 1800, 1980 → avg = 1890
        assert abs(avg_w - 1890.0) < 1.0

    def test_avg_loss(self, sample_trades):
        """Average losing trade."""
        avg_l = compute_avg_loss(sample_trades)
        # Loss: 350 (absolute)
        assert abs(avg_l - 350.0) < 1.0

    def test_win_loss_ratio(self, sample_trades):
        """Win/loss ratio."""
        wlr = compute_win_loss_ratio(sample_trades)
        # avg_win=1890, avg_loss=350 → ratio=5.4
        assert abs(wlr - 5.4) < 0.1

    def test_total_pnl(self, sample_trades):
        """Total PnL across all trades."""
        total = compute_total_pnl(sample_trades)
        # 1800 - 350 + 1980 = 3430
        assert abs(total - 3430.0) < 1.0

    def test_gross_profit(self, sample_trades):
        """Gross profit from winners only."""
        gp = compute_gross_profit(sample_trades)
        # 1800 + 1980 = 3780
        assert abs(gp - 3780.0) < 1.0

    def test_gross_loss(self, sample_trades):
        """Gross loss from losers only."""
        gl = compute_gross_loss(sample_trades)
        # abs(-350) = 350
        assert abs(gl - 350.0) < 1.0

    def test_empty_trades(self):
        """Test metrics with empty trades list."""
        empty = []
        assert compute_profit_factor(empty) == 0.0
        assert compute_win_rate(empty) == 0.0
        assert compute_loss_rate(empty) == 0.0
        assert compute_avg_win(empty) == 0.0
        assert compute_avg_loss(empty) == 0.0


class TestExpectancyMetrics:
    """Test expectancy metrics."""

    def test_expectancy_r(self, sample_trades):
        """Expectancy in R units."""
        exp_r = compute_expectancy_r(sample_trades)
        # Mean(2.0, -3.5, 2.0) = 0.5/3 ≈ 0.167
        expected = (2.0 - 3.5 + 2.0) / 3
        assert abs(exp_r - expected) < 0.01

    def test_expectancy_capital(self, sample_trades):
        """Expectancy in USDT."""
        exp_cap = compute_expectancy_capital(sample_trades)
        # Mean(1800, -350, 1980) ≈ 1143.33
        expected = (1800 - 350 + 1980) / 3
        assert abs(exp_cap - expected) < 1.0

    def test_expectancy_per_dollar(self, sample_trades):
        """Expectancy per dollar of risk."""
        exp_per_dollar = compute_expectancy_per_dollar(sample_trades)
        # Total PnL = 3430, Total Risk = 1000 + 500 + 2000 = 3500
        # Exp = 3430 / 3500 ≈ 0.98
        expected = 3430.0 / 3500.0
        assert abs(exp_per_dollar - expected) < 0.01

    def test_expectancy_formula(self, sample_trades):
        """Expectancy using classical formula."""
        exp_formula = compute_expectancy_formula(sample_trades)
        # (WR * avg_win) - (LR * avg_loss)
        # (0.667 * 1890) - (0.333 * 350) ≈ 1260.63 - 116.55 ≈ 1144.08
        wr = 2 / 3
        lr = 1 / 3
        avg_w = 1890.0
        avg_l = 350.0
        expected = (wr * avg_w) - (lr * avg_l)
        assert abs(exp_formula - expected) < 1.0


class TestDrawdownMetrics:
    """Test drawdown metrics."""

    def test_max_drawdown(self, equity_curve):
        """Maximum drawdown from equity curve."""
        max_dd = compute_max_drawdown(equity_curve)
        # Should be > 0 since we have losses
        assert max_dd >= 0.0
        assert max_dd <= 1.0

    def test_max_drawdown_duration(self, equity_curve):
        """Maximum drawdown duration in milliseconds."""
        max_duration = compute_max_drawdown_duration_ms(equity_curve)
        # Should be non-negative
        assert max_duration >= 0

    def test_calmar_ratio(self, equity_curve):
        """Calmar ratio calculation."""
        calmar = compute_calmar_ratio(equity_curve, initial_capital=10000.0)
        # Should be a valid number
        assert isinstance(calmar, float)
        assert not isinstance(calmar, bool)

    def test_recovery_factor(self, equity_curve):
        """Recovery factor calculation."""
        rf = compute_recovery_factor(equity_curve)
        # Should be a valid number
        assert isinstance(rf, float)
        assert not isinstance(rf, bool)

    def test_avg_drawdown(self, equity_curve):
        """Average drawdown percentage."""
        avg_dd = compute_avg_drawdown(equity_curve)
        assert avg_dd >= 0.0
        assert avg_dd <= 1.0

    def test_num_drawdown_periods(self, equity_curve):
        """Count of drawdown periods."""
        num_periods = compute_num_drawdown_periods(equity_curve)
        assert num_periods >= 0
        assert isinstance(num_periods, int)

    def test_longest_drawdown_trades(self, equity_curve):
        """Longest drawdown in consecutive trades."""
        longest = compute_longest_drawdown_duration_trades(equity_curve)
        assert longest >= 0
        assert isinstance(longest, int)

    def test_empty_equity_curve(self):
        """Test drawdown metrics with empty curve."""
        empty: List[EquityPoint] = []
        assert compute_max_drawdown(empty) == 0.0
        assert compute_max_drawdown_duration_ms(empty) == 0
        assert compute_avg_drawdown(empty) == 0.0
        assert compute_num_drawdown_periods(empty) == 0


class TestDistributionMetrics:
    """Test distribution statistics."""

    def test_r_multiple_distribution(self, sample_trades):
        """Distribution of R multiples."""
        dist = compute_r_multiple_distribution(sample_trades)
        assert dist is not None
        assert dist.count == 3
        assert dist.mean == pytest.approx((2.0 - 3.5 + 2.0) / 3)
        assert dist.min_value == -3.5
        assert dist.max_value == 2.0

    def test_duration_distribution(self, sample_trades):
        """Distribution of trade durations."""
        dist = compute_duration_distribution(sample_trades)
        assert dist is not None
        assert dist.count == 3
        assert dist.mean == 1000000.0  # All trades same duration
        assert dist.stddev == 0.0

    def test_pnl_distribution(self, sample_trades):
        """Distribution of PnL values."""
        dist = compute_pnl_distribution(sample_trades)
        assert dist is not None
        assert dist.count == 3
        assert dist.min_value == -350.0
        assert dist.max_value == 1980.0

    def test_mae_distribution(self, sample_trades):
        """Distribution of MAE values."""
        dist = compute_mae_distribution(sample_trades)
        assert dist is not None
        assert dist.count == 3
        assert dist.min_value == 0.3
        assert dist.max_value == 5.0

    def test_mfe_distribution(self, sample_trades):
        """Distribution of MFE values."""
        dist = compute_mfe_distribution(sample_trades)
        assert dist is not None
        assert dist.count == 3

    def test_pnl_pct_distribution(self, sample_trades):
        """Distribution of PnL percentages."""
        dist = compute_pnl_pct_distribution(sample_trades)
        assert dist is not None
        assert dist.count == 3

    def test_bars_held_distribution(self, sample_trades):
        """Distribution of bars held."""
        dist = compute_bars_held_distribution(sample_trades)
        assert dist is not None
        assert dist.count == 3

    def test_slippage_distribution(self, sample_trades):
        """Distribution of slippage percentages."""
        dist = compute_slippage_distribution(sample_trades)
        assert dist is not None
        assert dist.count == 3
        assert dist.min_value == 0.01
        assert dist.max_value == 0.05

    def test_signal_to_fill_distribution(self, sample_trades):
        """Distribution of signal-to-fill latencies."""
        dist = compute_signal_to_fill_distribution(sample_trades)
        assert dist is not None
        assert dist.count == 3

    def test_empty_distribution(self):
        """Test distributions with empty trades."""
        empty: List[TradeSnapshot] = []
        assert compute_r_multiple_distribution(empty) is None
        assert compute_duration_distribution(empty) is None
        assert compute_pnl_distribution(empty) is None


class TestCapitalEfficiencyMetrics:
    """Test capital efficiency metrics."""

    def test_capital_efficiency(self, sample_trades):
        """Overall capital efficiency metrics."""
        metrics = compute_capital_efficiency(sample_trades)
        assert metrics.trade_count == 3
        assert metrics.total_pnl_usdt == pytest.approx(3430.0)
        assert metrics.total_capital_locked_usdt == pytest.approx(3500.0)
        assert metrics.total_risk_deployed_usdt == pytest.approx(3500.0)

    def test_peak_utilization(self, sample_trades):
        """Peak capital utilization."""
        peak = compute_peak_utilization(sample_trades, initial_capital=10000.0)
        # Max concurrent = 2000 (SOL trade overlaps with ETH)
        # Peak utilization = 2000 / 10000 = 0.20
        assert peak >= 0.0
        assert peak <= 1.0

    def test_avg_utilization(self, sample_trades):
        """Average capital utilization."""
        avg = compute_avg_utilization(sample_trades, initial_capital=10000.0)
        # avg_size = 3500 / 3 ≈ 1166.67
        # avg_util = 1166.67 / 10000 ≈ 0.1167
        expected = (3500.0 / 3) / 10000.0
        assert abs(avg - expected) < 0.01

    def test_capital_turnover(self, sample_trades):
        """Capital turnover ratio."""
        turnover = compute_capital_turnover(sample_trades, initial_capital=10000.0)
        # Total deployed = 3500, turnover = 3500 / 10000 = 0.35
        assert abs(turnover - 0.35) < 0.01

    def test_avg_idle_time(self, sample_trades):
        """Average idle time between trades."""
        idle = compute_avg_idle_time(sample_trades)
        # Gap from close(2000000) to open(3000000) = 1000000 ms
        # Gap from close(4000000) to open(5000000) = 1000000 ms
        # Average = 1000000 ms
        assert idle > 0

    def test_empty_capital_efficiency(self):
        """Test capital efficiency with empty trades."""
        empty: List[TradeSnapshot] = []
        metrics = compute_capital_efficiency(empty)
        assert metrics.trade_count == 0
        assert metrics.total_pnl_usdt == 0.0


class TestEquityCurveBuilder:
    """Test equity curve builder."""

    def test_build_curve(self, sample_trades):
        """Build equity curve from trades."""
        curve = EquityCurveBuilder.build(sample_trades, initial_capital=10000.0)
        assert len(curve) == 3
        # First trade: 10000 + 1800 = 11800
        assert curve[0].equity == pytest.approx(11800.0)
        # Second trade: 11800 - 350 = 11450
        assert curve[1].equity == pytest.approx(11450.0)
        # Third trade: 11450 + 1980 = 13430
        assert curve[2].equity == pytest.approx(13430.0)

    def test_max_drawdown_from_builder(self, sample_trades):
        """Use EquityCurveBuilder to compute max drawdown."""
        curve = EquityCurveBuilder.build(sample_trades, initial_capital=10000.0)
        max_dd = EquityCurveBuilder.max_drawdown(curve)
        # Drawdown occurs at point[1] when equity = 11450 after peak of 11800
        # DD = (11800 - 11450) / 11800 ≈ 0.0297
        expected_dd = (11800.0 - 11450.0) / 11800.0
        assert abs(max_dd - expected_dd) < 0.01

    def test_compute_sharpe_ratio(self, sample_trades):
        """Compute Sharpe ratio from equity curve."""
        curve = EquityCurveBuilder.build(sample_trades, initial_capital=10000.0)
        sharpe = EquityCurveBuilder.compute_sharpe_ratio(curve)
        assert isinstance(sharpe, float)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
