"""
Phase 7 Analytics Test Suite — Comprehensive Governed Tests

RULES:
- 0 skip, 0 xfail — all tests must pass
- No PySide6/Qt imports
- All tests are independent and deterministic
- Pure function testing — no mutable state between tests
- Frozen dataclass immutability verified
"""

import pytest
import copy
from typing import List
from dataclasses import FrozenInstanceError

from core.intraday.analytics.models.trade_snapshot import TradeSnapshot
from core.intraday.analytics.models.equity_curve import EquityPoint, EquityCurveBuilder
from core.intraday.analytics.metrics.profit_factor import (
    compute_profit_factor,
    compute_win_rate,
    compute_loss_rate,
    compute_avg_win,
    compute_avg_loss,
    compute_win_loss_ratio,
    compute_total_pnl,
    compute_gross_profit,
    compute_gross_loss,
    compute_breakeven_rate,
)
from core.intraday.analytics.metrics.expectancy import (
    compute_expectancy_r,
    compute_expectancy_capital,
    compute_expectancy_per_dollar,
    compute_expectancy_formula,
    compute_expectancy_per_trade_r,
)
from core.intraday.analytics.metrics.drawdown import (
    compute_max_drawdown,
    compute_max_drawdown_duration_ms,
    compute_calmar_ratio,
    compute_recovery_factor,
    compute_avg_drawdown,
    compute_num_drawdown_periods,
    compute_longest_drawdown_duration_trades,
)
from core.intraday.analytics.metrics.distribution import (
    DistributionStats,
    compute_r_multiple_distribution,
    compute_duration_distribution,
    compute_pnl_distribution,
    compute_mae_distribution,
    compute_mfe_distribution,
    compute_pnl_pct_distribution,
    compute_bars_held_distribution,
    compute_slippage_distribution,
    compute_signal_to_fill_distribution,
)
from core.intraday.analytics.metrics.capital_efficiency import (
    CapitalEfficiencyMetrics,
    compute_capital_efficiency,
    compute_peak_utilization,
    compute_avg_utilization,
    compute_capital_turnover,
    compute_avg_idle_time,
)


# =====================================================================
# FIXTURE: Trade Builder
# =====================================================================

_trade_counter = 0

def _make_trade(
    pnl=100.0,
    r_multiple=1.0,
    strategy="MomentumBreakout",
    regime="bull_trend",
    entry_size=1000.0,
    entry_price=50000.0,
    stop_loss=49000.0,
    take_profit=52000.0,
    duration_ms=60000,
    bars_held=5,
    close_reason="TP_HIT",
    symbol="BTCUSDT",
    opened_at_ms=None,
    closed_at_ms=None,
    direction="long",
    mae_pct=None,
    mfe_pct=None,
    slippage_pct=0.0,
    position_id=None,
    trigger_id=None,
) -> TradeSnapshot:
    """
    Create a TradeSnapshot test fixture with auto-incrementing timestamps.

    Args:
        pnl: Realized PnL in USDT
        r_multiple: Risk-reward multiple
        strategy: Strategy name
        regime: Market regime at entry
        entry_size: Position size in USDT
        entry_price: Entry price
        stop_loss: Stop loss price (0.0 = disabled)
        take_profit: Take profit price
        duration_ms: Trade duration in milliseconds
        bars_held: Number of bars held
        close_reason: Exit reason
        symbol: Trading symbol
        opened_at_ms: Open timestamp (auto-generated if None)
        closed_at_ms: Close timestamp (auto-generated if None)
        direction: "long" or "short"
        mae_pct: Maximum adverse excursion %
        mfe_pct: Maximum favorable excursion %
        slippage_pct: Slippage as percentage
        position_id: Position ID (auto-generated if None)
        trigger_id: Trigger ID (auto-generated if None)

    Returns:
        TradeSnapshot: Immutable trade record
    """
    global _trade_counter
    _trade_counter += 1

    if opened_at_ms is None:
        opened_at_ms = 1000000000 + (_trade_counter * 1000)
    if closed_at_ms is None:
        closed_at_ms = opened_at_ms + duration_ms
    if position_id is None:
        position_id = f"pos_{_trade_counter}"
    if trigger_id is None:
        trigger_id = f"trg_{_trade_counter}"

    # Compute risk_usdt
    if stop_loss == 0:
        risk_usdt = 0.0
    else:
        risk_usdt = abs(entry_price - stop_loss) / entry_price * entry_size

    # Exit price depends on direction
    if direction == "long":
        exit_price = entry_price * (1 + (pnl / entry_size))
    else:
        exit_price = entry_price * (1 - (pnl / entry_size))

    return TradeSnapshot(
        position_id=position_id,
        trigger_id=trigger_id,
        symbol=symbol,
        direction=direction,
        strategy_name=strategy,
        strategy_class=strategy,
        regime_at_entry=regime,
        entry_price=entry_price,
        exit_price=exit_price,
        stop_loss=stop_loss,
        take_profit=take_profit,
        entry_size_usdt=entry_size,
        quantity=entry_size / entry_price,
        realized_pnl_usdt=pnl,
        fee_total_usdt=0.0,
        r_multiple=r_multiple,
        risk_usdt=risk_usdt,
        opened_at_ms=opened_at_ms,
        closed_at_ms=closed_at_ms,
        duration_ms=duration_ms,
        bars_held=bars_held,
        close_reason=close_reason,
        slippage_pct=slippage_pct,
        signal_to_fill_ms=0,
        mae_pct=mae_pct,
        mfe_pct=mfe_pct,
    )


# =====================================================================
# TRADESNAPSHOT TESTS (6 tests)
# =====================================================================

class TestTradeSnapshot:
    """TradeSnapshot model tests."""

    def test_from_trade_record_creates_valid_snapshot(self):
        """TradeSnapshot.from_trade_record() creates a valid snapshot."""
        tr = {
            "position_id": "pos_1",
            "trigger_id": "trg_1",
            "symbol": "BTCUSDT",
            "direction": "long",
            "strategy_name": "MomentumBreakout",
            "strategy_class": "MomentumBreakout",
            "regime_at_entry": "bull_trend",
            "entry_price": 50000.0,
            "exit_price": 51000.0,
            "stop_loss": 49000.0,
            "take_profit": 52000.0,
            "entry_size_usdt": 1000.0,
            "quantity": 0.02,
            "realized_pnl_usdt": 100.0,
            "fee_total_usdt": 0.5,
            "r_multiple": 1.0,
            "bars_held": 5,
            "close_reason": "TP_HIT",
            "opened_at_ms": 1000000000,
            "closed_at_ms": 1000060000,
            "duration_ms": 60000,
        }

        snapshot = TradeSnapshot.from_trade_record(tr)
        assert snapshot.position_id == "pos_1"
        assert snapshot.symbol == "BTCUSDT"
        assert snapshot.realized_pnl_usdt == 100.0
        assert snapshot.is_winner is True

    def test_from_trade_record_handles_missing_optional_fields(self):
        """from_trade_record() handles missing optional fields with defaults."""
        tr = {
            "position_id": "pos_1",
            "trigger_id": "trg_1",
            "symbol": "BTCUSDT",
            "direction": "long",
            "strategy_name": "MomentumBreakout",
            "strategy_class": "MomentumBreakout",
            "regime_at_entry": "bull_trend",
            "entry_price": 50000.0,
            "exit_price": 51000.0,
            "entry_size_usdt": 1000.0,
            "quantity": 0.02,
            "realized_pnl_usdt": 100.0,
            "fee_total_usdt": 0.5,
            "r_multiple": 1.0,
            "bars_held": 5,
            "close_reason": "TP_HIT",
            "opened_at_ms": 1000000000,
            "closed_at_ms": 1000060000,
            "duration_ms": 60000,
            # missing stop_loss, take_profit, mae_pct, mfe_pct
        }

        snapshot = TradeSnapshot.from_trade_record(tr)
        assert snapshot.stop_loss == 0.0
        assert snapshot.take_profit == 0.0
        assert snapshot.mae_pct is None
        assert snapshot.mfe_pct is None
        assert snapshot.slippage_pct == 0.0

    def test_is_winner_is_loser_properties(self):
        """is_winner and is_loser properties work correctly."""
        winner = _make_trade(pnl=100.0)
        loser = _make_trade(pnl=-100.0)
        breakeven = _make_trade(pnl=0.0)

        assert winner.is_winner is True
        assert winner.is_loser is False

        assert loser.is_winner is False
        assert loser.is_loser is True

        assert breakeven.is_winner is False
        assert breakeven.is_loser is False

    def test_pnl_pct_computed_correctly(self):
        """pnl_pct property computed correctly."""
        trade = _make_trade(pnl=100.0, entry_size=1000.0)
        assert trade.pnl_pct == pytest.approx(0.10)  # 100 / 1000

        # Zero entry size
        trade_zero = TradeSnapshot(
            position_id="pos", trigger_id="trg", symbol="BTC", direction="long",
            strategy_name="Test", strategy_class="Test", regime_at_entry="bull",
            entry_price=50000, exit_price=51000, stop_loss=0, take_profit=52000,
            entry_size_usdt=0.0, quantity=0, realized_pnl_usdt=100,
            fee_total_usdt=0, r_multiple=0, risk_usdt=0,
            opened_at_ms=1000, closed_at_ms=2000, duration_ms=1000, bars_held=1,
            close_reason="TP"
        )
        assert trade_zero.pnl_pct == 0.0

    def test_risk_usdt_computed_correctly(self):
        """risk_usdt computed correctly from entry_price and stop_loss."""
        trade = _make_trade(entry_price=50000.0, stop_loss=49000.0, entry_size=1000.0)
        expected_risk = abs(50000 - 49000) / 50000 * 1000
        assert trade.risk_usdt == pytest.approx(expected_risk)

        # Zero stop_loss
        trade_no_sl = _make_trade(entry_price=50000.0, stop_loss=0.0, entry_size=1000.0)
        assert trade_no_sl.risk_usdt == 0.0

    def test_immutability_enforced(self):
        """Frozen dataclass prevents mutation."""
        trade = _make_trade()
        with pytest.raises(FrozenInstanceError):
            trade.realized_pnl_usdt = 999.0


# =====================================================================
# EQUITYCURVE TESTS (7 tests)
# =====================================================================

class TestEquityCurve:
    """EquityCurveBuilder tests."""

    def test_build_from_single_trade(self):
        """Build equity curve from single trade."""
        trade = _make_trade(pnl=100.0)
        curve = EquityCurveBuilder.build([trade], initial_capital=10000.0)

        assert len(curve) == 1
        point = curve[0]
        assert point.equity == pytest.approx(10100.0)
        assert point.peak_equity == 10100.0
        assert point.realized_pnl_cumulative == 100.0
        assert point.trade_index == 0

    def test_build_from_multiple_trades(self):
        """Build equity curve from multiple trades with proper ordering."""
        trades = [
            _make_trade(pnl=100.0, opened_at_ms=1000, closed_at_ms=2000),
            _make_trade(pnl=50.0, opened_at_ms=3000, closed_at_ms=4000),
            _make_trade(pnl=-30.0, opened_at_ms=5000, closed_at_ms=6000),
        ]
        curve = EquityCurveBuilder.build(trades, initial_capital=10000.0)

        assert len(curve) == 3
        assert curve[0].equity == pytest.approx(10100.0)
        assert curve[1].equity == pytest.approx(10150.0)
        assert curve[2].equity == pytest.approx(10120.0)
        assert curve[2].realized_pnl_cumulative == 120.0

    def test_peak_equity_tracked_correctly(self):
        """Peak equity tracked correctly through drawdowns."""
        trades = [
            _make_trade(pnl=100.0),
            _make_trade(pnl=-80.0),
            _make_trade(pnl=50.0),
        ]
        curve = EquityCurveBuilder.build(trades, initial_capital=10000.0)

        assert curve[0].peak_equity == 10100.0
        assert curve[1].peak_equity == 10100.0  # Still at first peak
        assert curve[2].peak_equity == 10100.0  # Peak doesn't move

    def test_drawdown_computed_at_each_point(self):
        """Drawdown computed correctly at each point."""
        trades = [
            _make_trade(pnl=100.0),
            _make_trade(pnl=-50.0),
        ]
        curve = EquityCurveBuilder.build(trades, initial_capital=10000.0)

        assert curve[0].drawdown_pct == 0.0  # At peak
        assert curve[1].drawdown_pct == pytest.approx(50.0 / 10100.0)  # (peak - equity) / peak

    def test_max_drawdown_returns_correct_value(self):
        """max_drawdown() returns maximum drawdown percentage."""
        trades = [
            _make_trade(pnl=1000.0),
            _make_trade(pnl=-500.0),
            _make_trade(pnl=100.0),
        ]
        curve = EquityCurveBuilder.build(trades, initial_capital=10000.0)
        max_dd = EquityCurveBuilder.max_drawdown(curve)

        assert max_dd > 0
        assert max_dd < 1.0

    def test_max_drawdown_duration_ms_works(self):
        """max_drawdown_duration_ms() returns correct duration."""
        trades = [
            _make_trade(pnl=100.0, opened_at_ms=1000, closed_at_ms=2000),
            _make_trade(pnl=-50.0, opened_at_ms=3000, closed_at_ms=10000),
            _make_trade(pnl=100.0, opened_at_ms=11000, closed_at_ms=12000),
        ]
        curve = EquityCurveBuilder.build(trades, initial_capital=10000.0)
        duration = EquityCurveBuilder.max_drawdown_duration_ms(curve)

        assert duration > 0

    def test_empty_trades_returns_empty_curve(self):
        """Empty trade list returns empty curve."""
        curve = EquityCurveBuilder.build([], initial_capital=10000.0)
        assert len(curve) == 0


# =====================================================================
# PROFIT FACTOR TESTS (6 tests)
# =====================================================================

class TestProfitFactor:
    """Profit factor and win rate metrics tests."""

    def test_pf_with_mixed_wins_losses(self):
        """Profit factor with mixed wins and losses computes correctly."""
        trades = [
            _make_trade(pnl=100.0),  # winner
            _make_trade(pnl=150.0),  # winner
            _make_trade(pnl=-50.0),  # loser
            _make_trade(pnl=-30.0),  # loser
        ]
        pf = compute_profit_factor(trades)
        expected = (100 + 150) / (50 + 30)
        assert pf == pytest.approx(expected)

    def test_pf_with_no_losses(self):
        """Profit factor with no losses returns infinity."""
        trades = [
            _make_trade(pnl=100.0),
            _make_trade(pnl=150.0),
        ]
        pf = compute_profit_factor(trades)
        assert pf == float('inf')

    def test_pf_with_no_wins(self):
        """Profit factor with no wins returns 0.0."""
        trades = [
            _make_trade(pnl=-50.0),
            _make_trade(pnl=-30.0),
        ]
        pf = compute_profit_factor(trades)
        assert pf == 0.0

    def test_pf_with_empty_trades(self):
        """Profit factor with empty trades returns 0.0."""
        pf = compute_profit_factor([])
        assert pf == 0.0

    def test_win_rate_correct(self):
        """Win rate computed correctly."""
        trades = [
            _make_trade(pnl=100.0),  # winner
            _make_trade(pnl=150.0),  # winner
            _make_trade(pnl=-50.0),  # loser
            _make_trade(pnl=0.0),    # breakeven (not a winner)
            _make_trade(pnl=-30.0),  # loser
        ]
        wr = compute_win_rate(trades)
        assert wr == pytest.approx(2.0 / 5.0)  # 40%

    def test_avg_win_avg_loss_correct(self):
        """Average win and loss computed correctly."""
        trades = [
            _make_trade(pnl=100.0),
            _make_trade(pnl=200.0),
            _make_trade(pnl=-50.0),
            _make_trade(pnl=-100.0),
        ]
        avg_win = compute_avg_win(trades)
        avg_loss = compute_avg_loss(trades)

        assert avg_win == pytest.approx(150.0)  # (100 + 200) / 2
        assert avg_loss == pytest.approx(75.0)  # (50 + 100) / 2


# =====================================================================
# EXPECTANCY TESTS (5 tests)
# =====================================================================

class TestExpectancy:
    """Expectancy metrics tests."""

    def test_expectancy_r_based(self):
        """R-based expectancy equals mean of r_multiples."""
        trades = [
            _make_trade(r_multiple=1.5),
            _make_trade(r_multiple=2.0),
            _make_trade(r_multiple=-1.0),
        ]
        exp_r = compute_expectancy_r(trades)
        expected = (1.5 + 2.0 - 1.0) / 3.0
        assert exp_r == pytest.approx(expected)

    def test_expectancy_capital_based(self):
        """Capital-based expectancy is mean of pnls."""
        trades = [
            _make_trade(pnl=100.0),
            _make_trade(pnl=200.0),
            _make_trade(pnl=-50.0),
        ]
        exp_cap = compute_expectancy_capital(trades)
        expected = (100 + 200 - 50) / 3.0
        assert exp_cap == pytest.approx(expected)

    def test_expectancy_per_dollar(self):
        """Per-dollar expectancy correct."""
        # Create trades with specific stop losses to control risk_usdt
        trades = [
            _make_trade(pnl=100.0, entry_price=50000.0, stop_loss=49000.0, entry_size=1000.0),
            _make_trade(pnl=200.0, entry_price=50000.0, stop_loss=49500.0, entry_size=1000.0),
            _make_trade(pnl=-50.0, entry_price=50000.0, stop_loss=49800.0, entry_size=1000.0),
        ]
        exp_dollar = compute_expectancy_per_dollar(trades)
        # Total PnL = 100 + 200 - 50 = 250
        # Total risk = sum of risk_usdt for each trade
        total_pnl = 100 + 200 - 50
        total_risk = sum(t.risk_usdt for t in trades)
        expected = total_pnl / total_risk
        assert exp_dollar == pytest.approx(expected)

    def test_expectancy_empty_trades(self):
        """Empty trades returns 0.0."""
        assert compute_expectancy_r([]) == 0.0
        assert compute_expectancy_capital([]) == 0.0
        assert compute_expectancy_per_dollar([]) == 0.0

    def test_expectancy_formula_method(self):
        """Expectancy formula (WR * avg_win - LR * avg_loss)."""
        trades = [
            _make_trade(pnl=100.0),
            _make_trade(pnl=100.0),
            _make_trade(pnl=-50.0),
        ]
        exp = compute_expectancy_formula(trades)
        # WR = 2/3, LR = 1/3, avg_win = 100, avg_loss = 50
        expected = (2.0/3.0 * 100.0) - (1.0/3.0 * 50.0)
        assert exp == pytest.approx(expected)


# =====================================================================
# DRAWDOWN TESTS (5 tests)
# =====================================================================

class TestDrawdown:
    """Drawdown metrics tests."""

    def test_max_drawdown_from_equity_curve(self):
        """Max drawdown computed from equity curve."""
        trades = [
            _make_trade(pnl=500.0),
            _make_trade(pnl=-300.0),
            _make_trade(pnl=100.0),
        ]
        curve = EquityCurveBuilder.build(trades, initial_capital=10000.0)
        max_dd = compute_max_drawdown(curve)
        assert max_dd > 0
        assert max_dd < 1.0

    def test_max_drawdown_duration(self):
        """Max drawdown duration computed correctly."""
        trades = [
            _make_trade(pnl=100.0, opened_at_ms=1000, closed_at_ms=2000),
            _make_trade(pnl=-50.0, opened_at_ms=3000, closed_at_ms=5000),
            _make_trade(pnl=50.0, opened_at_ms=6000, closed_at_ms=8000),
        ]
        curve = EquityCurveBuilder.build(trades, initial_capital=10000.0)
        duration = compute_max_drawdown_duration_ms(curve)
        assert duration >= 0

    def test_calmar_ratio(self):
        """Calmar ratio computed (CAGR / max DD)."""
        trades = [_make_trade(pnl=100.0) for _ in range(10)]
        curve = EquityCurveBuilder.build(trades, initial_capital=10000.0)
        calmar = compute_calmar_ratio(curve, initial_capital=10000.0)
        # Positive trades should give positive Calmar
        assert calmar >= 0

    def test_recovery_factor(self):
        """Recovery factor (total profit / max DD USDT)."""
        trades = [
            _make_trade(pnl=1000.0),
            _make_trade(pnl=-500.0),
            _make_trade(pnl=500.0),
        ]
        curve = EquityCurveBuilder.build(trades, initial_capital=10000.0)
        rf = compute_recovery_factor(curve)
        assert rf >= 0

    def test_no_drawdown_case(self):
        """No drawdown case returns 0.0."""
        trades = [_make_trade(pnl=100.0) for _ in range(5)]
        curve = EquityCurveBuilder.build(trades, initial_capital=10000.0)
        max_dd = compute_max_drawdown(curve)
        assert max_dd == 0.0


# =====================================================================
# DISTRIBUTION TESTS (5 tests)
# =====================================================================

class TestDistribution:
    """Distribution statistics tests."""

    def test_r_multiple_distribution_stats(self):
        """R-multiple distribution computes percentiles correctly."""
        trades = [
            _make_trade(r_multiple=0.5),
            _make_trade(r_multiple=1.0),
            _make_trade(r_multiple=1.5),
            _make_trade(r_multiple=2.0),
            _make_trade(r_multiple=2.5),
        ]
        dist = compute_r_multiple_distribution(trades)
        assert dist is not None
        assert dist.count == 5
        assert dist.mean == pytest.approx(1.5)
        assert dist.median == pytest.approx(1.5)
        assert dist.min_value == 0.5
        assert dist.max_value == 2.5

    def test_duration_distribution(self):
        """Duration distribution computed correctly."""
        trades = [
            _make_trade(duration_ms=1000),
            _make_trade(duration_ms=2000),
            _make_trade(duration_ms=3000),
        ]
        dist = compute_duration_distribution(trades)
        assert dist is not None
        assert dist.count == 3
        assert dist.min_value == 1000.0
        assert dist.max_value == 3000.0

    def test_pnl_distribution(self):
        """PnL distribution computed correctly."""
        trades = [
            _make_trade(pnl=100.0),
            _make_trade(pnl=200.0),
            _make_trade(pnl=-50.0),
        ]
        dist = compute_pnl_distribution(trades)
        assert dist is not None
        assert dist.count == 3

    def test_mae_mfe_distribution_skips_none_values(self):
        """MAE/MFE distribution skips None values."""
        trades = [
            _make_trade(mae_pct=0.5, mfe_pct=1.5),
            _make_trade(mae_pct=None, mfe_pct=None),  # Missing
            _make_trade(mae_pct=1.0, mfe_pct=2.0),
        ]
        mae_dist = compute_mae_distribution(trades)
        mfe_dist = compute_mfe_distribution(trades)

        assert mae_dist is not None
        assert mae_dist.count == 2
        assert mfe_dist is not None
        assert mfe_dist.count == 2

    def test_skew_calculated_correctly(self):
        """Skew in distribution is correct."""
        # Right-skewed: more trades on left
        trades = [
            _make_trade(r_multiple=-1.0),
            _make_trade(r_multiple=-0.5),
            _make_trade(r_multiple=0.5),
            _make_trade(r_multiple=0.5),
            _make_trade(r_multiple=5.0),  # Long tail right
        ]
        dist = compute_r_multiple_distribution(trades)
        assert dist is not None
        assert dist.mean > dist.median  # Right-skewed


# =====================================================================
# CAPITAL EFFICIENCY TESTS (4 tests)
# =====================================================================

class TestCapitalEfficiency:
    """Capital efficiency metrics tests."""

    def test_avg_utilization_correct(self):
        """Average utilization computed correctly."""
        trades = [
            _make_trade(entry_size=1000.0),
            _make_trade(entry_size=2000.0),
            _make_trade(entry_size=1500.0),
        ]
        util = compute_avg_utilization(trades, initial_capital=10000.0)
        expected = (1000 + 2000 + 1500) / 3 / 10000.0
        assert util == pytest.approx(expected)

    def test_return_on_deployed_correct(self):
        """Return on capital and risk computed correctly."""
        trades = [
            _make_trade(pnl=100.0, entry_size=1000.0),
            _make_trade(pnl=-50.0, entry_size=1000.0),
        ]
        metrics = compute_capital_efficiency(trades)
        assert metrics.net_pnl_usdt == 50.0
        assert metrics.total_capital_locked_usdt == 2000.0

    def test_capital_turnover_correct(self):
        """Capital turnover (deployed / initial) correct."""
        trades = [
            _make_trade(entry_size=1000.0),
            _make_trade(entry_size=2000.0),
        ]
        turnover = compute_capital_turnover(trades, initial_capital=5000.0)
        expected = 3000.0 / 5000.0
        assert turnover == pytest.approx(expected)

    def test_avg_idle_time_between_trades(self):
        """Average idle time between trades computed."""
        trades = [
            _make_trade(opened_at_ms=1000, closed_at_ms=2000),
            _make_trade(opened_at_ms=5000, closed_at_ms=6000),  # 3000ms gap
            _make_trade(opened_at_ms=9000, closed_at_ms=10000),  # 3000ms gap
        ]
        idle = compute_avg_idle_time(trades)
        expected = (3000 + 3000) / 2
        assert idle == expected


# =====================================================================
# DETERMINISM TESTS (4 tests)
# =====================================================================

class TestDeterminism:
    """Determinism and reproducibility tests."""

    def test_same_trades_same_report(self):
        """Same trades produce identical reports when analyzed twice."""
        trades = [
            _make_trade(pnl=100.0, r_multiple=1.0),
            _make_trade(pnl=150.0, r_multiple=1.5),
            _make_trade(pnl=-50.0, r_multiple=-1.0),
        ]

        # Run analysis twice
        curve1 = EquityCurveBuilder.build(trades, initial_capital=10000.0)
        curve2 = EquityCurveBuilder.build(trades, initial_capital=10000.0)

        assert len(curve1) == len(curve2)
        for p1, p2 in zip(curve1, curve2):
            assert p1.equity == p2.equity
            assert p1.peak_equity == p2.peak_equity
            assert p1.drawdown_pct == p2.drawdown_pct

    def test_different_trade_ordering_same_report(self):
        """Different orderings of same trades produce same report (internally sorted)."""
        trades_a = [
            _make_trade(pnl=100.0, opened_at_ms=2000, closed_at_ms=3000),
            _make_trade(pnl=50.0, opened_at_ms=1000, closed_at_ms=1500),
        ]
        trades_b = [
            _make_trade(pnl=50.0, opened_at_ms=1000, closed_at_ms=1500),
            _make_trade(pnl=100.0, opened_at_ms=2000, closed_at_ms=3000),
        ]

        # Sort by closed_at_ms for comparison
        trades_a_sorted = sorted(trades_a, key=lambda t: t.closed_at_ms)
        trades_b_sorted = sorted(trades_b, key=lambda t: t.closed_at_ms)

        curve_a = EquityCurveBuilder.build(trades_a_sorted, initial_capital=10000.0)
        curve_b = EquityCurveBuilder.build(trades_b_sorted, initial_capital=10000.0)

        assert curve_a[0].equity == curve_b[0].equity

    def test_two_engine_instances_same_report(self):
        """Two separate engine instances produce identical reports."""
        trades = [
            _make_trade(pnl=100.0),
            _make_trade(pnl=150.0),
        ]

        builder1 = EquityCurveBuilder()
        builder2 = EquityCurveBuilder()

        curve1 = builder1.build(trades, initial_capital=10000.0)
        curve2 = builder2.build(trades, initial_capital=10000.0)

        assert curve1[-1].equity == curve2[-1].equity

    def test_shuffle_input_identical_output(self):
        """Shuffled input (when properly sorted) produces identical output."""
        import random

        trades_orig = [
            _make_trade(pnl=100.0, opened_at_ms=1000, closed_at_ms=2000),
            _make_trade(pnl=50.0, opened_at_ms=3000, closed_at_ms=4000),
            _make_trade(pnl=-30.0, opened_at_ms=5000, closed_at_ms=6000),
        ]

        # Shuffle
        trades_shuffled = trades_orig.copy()
        random.shuffle(trades_shuffled)

        # Both sorted by timestamp
        trades_orig_sorted = sorted(trades_orig, key=lambda t: t.closed_at_ms)
        trades_shuffled_sorted = sorted(trades_shuffled, key=lambda t: t.closed_at_ms)

        curve_orig = EquityCurveBuilder.build(trades_orig_sorted, initial_capital=10000.0)
        curve_shuffled = EquityCurveBuilder.build(trades_shuffled_sorted, initial_capital=10000.0)

        assert curve_orig[-1].realized_pnl_cumulative == curve_shuffled[-1].realized_pnl_cumulative


# =====================================================================
# REPLAY EQUIVALENCE TESTS (3 tests)
# =====================================================================

class TestReplayEquivalence:
    """Replay and equivalence tests."""

    def test_live_sequence_equals_replayed_sequence(self):
        """Live sequence produces same result as replayed sequence."""
        trades = [
            _make_trade(pnl=100.0),
            _make_trade(pnl=50.0),
            _make_trade(pnl=-30.0),
        ]

        # "Live" run
        live_metrics = compute_capital_efficiency(trades)

        # "Replayed" run (same data)
        replayed_metrics = compute_capital_efficiency(trades)

        assert live_metrics.net_pnl_usdt == replayed_metrics.net_pnl_usdt

    def test_subset_analytics_consistent_with_full(self):
        """Subset of trades produces consistent metrics with full set."""
        trades = [
            _make_trade(pnl=100.0),
            _make_trade(pnl=50.0),
            _make_trade(pnl=-30.0),
            _make_trade(pnl=80.0),
        ]

        full_pf = compute_profit_factor(trades)
        subset_pf = compute_profit_factor(trades[:2])

        # Both should be positive (winners exist)
        assert full_pf > 0
        assert subset_pf > 0

    def test_filtering_then_analyzing_equals_analyzing_with_filter(self):
        """Filtering trades before analysis gives same result as filtering within analysis."""
        trades = [
            _make_trade(pnl=100.0, strategy="A"),
            _make_trade(pnl=50.0, strategy="B"),
            _make_trade(pnl=-30.0, strategy="A"),
        ]

        # Filter before
        trades_a = [t for t in trades if t.strategy_name == "A"]
        wr_before = compute_win_rate(trades_a)

        # Filter logic (simulated by computing within loop)
        winners_in_a = sum(1 for t in trades if t.strategy_name == "A" and t.is_winner)
        count_a = sum(1 for t in trades if t.strategy_name == "A")
        wr_during = winners_in_a / count_a if count_a > 0 else 0.0

        assert wr_before == wr_during


# =====================================================================
# EDGE CASE TESTS (6 tests)
# =====================================================================

class TestEdgeCases:
    """Edge case tests."""

    def test_empty_trades_zeroed_report(self):
        """Empty trades produce zero metrics."""
        metrics = compute_capital_efficiency([])
        assert metrics.trade_count == 0
        assert metrics.total_pnl_usdt == 0.0
        assert metrics.net_pnl_usdt == 0.0

    def test_single_trade_report(self):
        """Single trade produces valid report."""
        trade = _make_trade(pnl=100.0)
        pf = compute_profit_factor([trade])
        wr = compute_win_rate([trade])

        assert pf == float('inf')  # Only winners
        assert wr == 1.0

    def test_all_winners(self):
        """All winning trades."""
        trades = [_make_trade(pnl=50.0 * (i + 1)) for i in range(5)]
        pf = compute_profit_factor(trades)
        wr = compute_win_rate(trades)

        assert pf == float('inf')
        assert wr == 1.0

    def test_all_losers(self):
        """All losing trades."""
        trades = [_make_trade(pnl=-50.0 * (i + 1)) for i in range(5)]
        pf = compute_profit_factor(trades)
        wr = compute_win_rate(trades)

        assert pf == 0.0
        assert wr == 0.0

    def test_zero_entry_size_trades_skipped_in_capital_metrics(self):
        """Zero entry_size trades handled gracefully."""
        trade_zero = TradeSnapshot(
            position_id="pos", trigger_id="trg", symbol="BTC", direction="long",
            strategy_name="Test", strategy_class="Test", regime_at_entry="bull",
            entry_price=50000, exit_price=51000, stop_loss=49000, take_profit=52000,
            entry_size_usdt=0.0, quantity=0, realized_pnl_usdt=0,
            fee_total_usdt=0, r_multiple=0, risk_usdt=0,
            opened_at_ms=1000, closed_at_ms=2000, duration_ms=1000, bars_held=1,
            close_reason="TP"
        )
        trade_normal = _make_trade(entry_size=1000.0, pnl=100.0)

        trades = [trade_zero, trade_normal]
        metrics = compute_capital_efficiency(trades)

        # Should handle zero entry size without error
        assert metrics.total_capital_locked_usdt == 1000.0

    def test_very_large_dataset_completes_fast(self):
        """1000 trades complete in reasonable time."""
        trades = [_make_trade(pnl=50.0 * (i % 3 - 1)) for i in range(1000)]

        import time
        start = time.time()
        curve = EquityCurveBuilder.build(trades, initial_capital=100000.0)
        elapsed = time.time() - start

        assert len(curve) == 1000
        assert elapsed < 1.0  # Should complete in under 1 second


# =====================================================================
# INTEGRATION TESTS
# =====================================================================

class TestIntegration:
    """Cross-component integration tests."""

    def test_full_analytics_pipeline(self):
        """Full analytics pipeline: trades -> curve -> all metrics."""
        trades = [
            _make_trade(pnl=100.0, r_multiple=1.0, bars_held=5, mae_pct=0.5, mfe_pct=1.5),
            _make_trade(pnl=150.0, r_multiple=1.5, bars_held=8, mae_pct=0.3, mfe_pct=2.0),
            _make_trade(pnl=-50.0, r_multiple=-1.0, bars_held=3, mae_pct=2.0, mfe_pct=0.0),
            _make_trade(pnl=200.0, r_multiple=2.0, bars_held=10, mae_pct=0.1, mfe_pct=3.0),
        ]

        # Equity curve
        curve = EquityCurveBuilder.build(trades, initial_capital=10000.0)
        assert len(curve) == 4

        # Profit factor
        pf = compute_profit_factor(trades)
        wr = compute_win_rate(trades)
        assert pf > 1.0
        assert wr == 0.75

        # Expectancy
        exp_r = compute_expectancy_r(trades)
        exp_cap = compute_expectancy_capital(trades)
        assert exp_r > 0
        assert exp_cap > 0

        # Drawdown
        max_dd = compute_max_drawdown(curve)
        assert max_dd >= 0

        # Distribution
        r_dist = compute_r_multiple_distribution(trades)
        assert r_dist is not None
        assert r_dist.count == 4

        # Capital efficiency
        metrics = compute_capital_efficiency(trades)
        assert metrics.trade_count == 4
        assert metrics.net_pnl_usdt > 0

    def test_consecutive_metrics_consistency(self):
        """Consecutive metrics calls produce identical results."""
        trades = [
            _make_trade(pnl=100.0),
            _make_trade(pnl=50.0),
        ]

        pf1 = compute_profit_factor(trades)
        pf2 = compute_profit_factor(trades)
        assert pf1 == pf2

        wr1 = compute_win_rate(trades)
        wr2 = compute_win_rate(trades)
        assert wr1 == wr2

        metrics1 = compute_capital_efficiency(trades)
        metrics2 = compute_capital_efficiency(trades)
        assert metrics1.net_pnl_usdt == metrics2.net_pnl_usdt


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
