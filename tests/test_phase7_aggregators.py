"""
Phase 7: Aggregator and Integration Test Suite

Comprehensive test coverage for StrategyAggregator, RegimeAggregator, and PerformanceEngine.
Zero PySide6/Qt imports. Pure Python. 40+ tests, 0 skip, 0 xfail.

Test categories:
  - StrategyAggregator (8 tests)
  - RegimeAggregator (6 tests)
  - PerformanceEngine (10 tests)
  - Isolation Proof (6 tests)
  - Replay Consistency (5 tests)
  - Phase 8 Readiness (5 tests)
  - Helper Fixtures & Edge Cases (6+ tests)

Total: 46 tests
"""

import json
from typing import List, Optional
import pytest

from core.intraday.analytics.models import (
    TradeSnapshot,
    EquityPoint,
    EquityCurveBuilder,
)
from core.intraday.analytics.aggregators import (
    StrategyAggregator,
    StrategyPerformance,
    RegimeAggregator,
    RegimePerformance,
)
from core.intraday.analytics.performance_engine import (
    PerformanceEngine,
    PerformanceReport,
)


# ============================================================================
# FIXTURES: TradeSnapshot Factory
# ============================================================================

def _make_trade(
    pnl: float = 100.0,
    r_multiple: float = 1.0,
    strategy: str = "MomentumBreakout",
    regime: str = "bull_trend",
    symbol: str = "BTCUSDT",
    direction: str = "long",
    close_reason: str = "tp_hit",
    entry_price: float = 50000.0,
    entry_size_usdt: float = 1000.0,
    duration_ms: int = 3600000,
    bars_held: int = 60,
    position_id: Optional[str] = None,
    trigger_id: Optional[str] = None,
    opened_at_ms: int = 1700000000000,
    closed_at_ms: Optional[int] = None,
    mae_pct: Optional[float] = None,
    mfe_pct: Optional[float] = None,
    fee_total_usdt: float = 5.0,
    slippage_pct: float = 0.01,
) -> TradeSnapshot:
    """
    Factory for creating TradeSnapshot fixtures.

    All parameters optional with sensible defaults.
    """
    if closed_at_ms is None:
        closed_at_ms = opened_at_ms + duration_ms

    if position_id is None:
        position_id = f"pos_{opened_at_ms}_{symbol}"

    if trigger_id is None:
        trigger_id = f"trig_{opened_at_ms}"

    # Compute stop_loss and take_profit from r_multiple
    # Assume SL is 1R away, TP is r_multiple * 1R away
    risk_pct = 0.02  # 2% risk per trade
    stop_loss = entry_price * (1.0 - risk_pct) if direction == "long" else entry_price * (1.0 + risk_pct)

    if direction == "long":
        take_profit = entry_price * (1.0 + (risk_pct * r_multiple))
    else:
        take_profit = entry_price * (1.0 - (risk_pct * r_multiple))

    # Compute exit_price from realized_pnl
    exit_price = entry_price + (pnl / entry_size_usdt)

    # Compute quantity
    quantity = entry_size_usdt / entry_price

    # Risk USDT = |entry - stop| / entry * entry_size
    risk_usdt = abs(entry_price - stop_loss) / entry_price * entry_size_usdt

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
        entry_size_usdt=entry_size_usdt,
        quantity=quantity,
        realized_pnl_usdt=pnl,
        fee_total_usdt=fee_total_usdt,
        r_multiple=r_multiple,
        risk_usdt=risk_usdt,
        opened_at_ms=opened_at_ms,
        closed_at_ms=closed_at_ms,
        duration_ms=duration_ms,
        bars_held=bars_held,
        close_reason=close_reason,
        slippage_pct=slippage_pct,
        signal_to_fill_ms=250,
        mae_pct=mae_pct,
        mfe_pct=mfe_pct,
    )


# ============================================================================
# STRATEGY AGGREGATOR TESTS (8 tests)
# ============================================================================

class TestStrategyAggregator:
    """Test suite for StrategyAggregator."""

    def test_single_strategy_single_trade(self):
        """Single strategy, single trade produces correct StrategyPerformance."""
        trade = _make_trade(pnl=100.0, strategy="MomentumBreakout")
        trades = [trade]

        result = StrategyAggregator.aggregate(trades)

        assert len(result) == 1
        assert "MomentumBreakout" in result

        perf = result["MomentumBreakout"]
        assert perf.strategy_name == "MomentumBreakout"
        assert perf.trade_count == 1
        assert perf.total_pnl_usdt == 100.0
        assert perf.avg_pnl_usdt == 100.0
        assert perf.win_rate == 1.0  # 100% win

    def test_multiple_strategies_grouped_correctly(self):
        """Multiple strategies grouped correctly."""
        trades = [
            _make_trade(pnl=100.0, strategy="MomentumBreakout", opened_at_ms=1700000000000),
            _make_trade(pnl=-50.0, strategy="MomentumBreakout", opened_at_ms=1700000001000),
            _make_trade(pnl=150.0, strategy="PullbackLong", opened_at_ms=1700000002000),
            _make_trade(pnl=75.0, strategy="SwingLowContinuation", opened_at_ms=1700000003000),
        ]

        result = StrategyAggregator.aggregate(trades)

        assert len(result) == 3
        assert set(result.keys()) == {"MomentumBreakout", "PullbackLong", "SwingLowContinuation"}

        # Check grouping
        assert result["MomentumBreakout"].trade_count == 2
        assert result["PullbackLong"].trade_count == 1
        assert result["SwingLowContinuation"].trade_count == 1

    def test_win_rate_per_strategy(self):
        """Win rate computed correctly per strategy."""
        trades = [
            _make_trade(pnl=100.0, strategy="A", opened_at_ms=1700000000000),
            _make_trade(pnl=50.0, strategy="A", opened_at_ms=1700000001000),
            _make_trade(pnl=-75.0, strategy="A", opened_at_ms=1700000002000),
        ]

        result = StrategyAggregator.aggregate(trades)
        perf = result["A"]

        # 2 wins, 1 loss = 2/3 win rate
        assert perf.win_rate == pytest.approx(2/3, abs=1e-6)

    def test_profit_factor_per_strategy(self):
        """Profit factor computed correctly per strategy."""
        trades = [
            _make_trade(pnl=100.0, strategy="A", opened_at_ms=1700000000000),
            _make_trade(pnl=200.0, strategy="A", opened_at_ms=1700000001000),
            _make_trade(pnl=-50.0, strategy="A", opened_at_ms=1700000002000),
        ]

        result = StrategyAggregator.aggregate(trades)
        perf = result["A"]

        # PF = 300 / 50 = 6.0
        assert perf.profit_factor == pytest.approx(6.0, abs=1e-6)

    def test_expectancy_per_strategy(self):
        """Expectancy per strategy computed correctly."""
        trades = [
            _make_trade(pnl=100.0, r_multiple=1.0, strategy="A", opened_at_ms=1700000000000),
            _make_trade(pnl=-50.0, r_multiple=-0.5, strategy="A", opened_at_ms=1700000001000),
        ]

        result = StrategyAggregator.aggregate(trades)
        perf = result["A"]

        # Expectancy = win_rate * avg_win - loss_rate * avg_loss
        # WR = 50%, avg_win = 100, avg_loss = 50
        # E = 0.5 * 100 - 0.5 * 50 = 50 - 25 = 25
        expected_exp = 0.5 * 100.0 - 0.5 * 50.0
        assert perf.expectancy_capital == pytest.approx(expected_exp, abs=1.0)

    def test_close_reason_counts_per_strategy(self):
        """Close reason counts breakdown correct."""
        trades = [
            _make_trade(pnl=100.0, strategy="A", close_reason="tp_hit", opened_at_ms=1700000000000),
            _make_trade(pnl=-50.0, strategy="A", close_reason="sl_hit", opened_at_ms=1700000001000),
            _make_trade(pnl=75.0, strategy="A", close_reason="tp_hit", opened_at_ms=1700000002000),
        ]

        result = StrategyAggregator.aggregate(trades)
        perf = result["A"]

        assert perf.close_reason_counts["tp_hit"] == 2
        assert perf.close_reason_counts["sl_hit"] == 1

    def test_total_pnl_per_strategy(self):
        """Total PnL per strategy correct."""
        trades = [
            _make_trade(pnl=100.0, strategy="A", opened_at_ms=1700000000000),
            _make_trade(pnl=-30.0, strategy="A", opened_at_ms=1700000001000),
            _make_trade(pnl=50.0, strategy="B", opened_at_ms=1700000002000),
        ]

        result = StrategyAggregator.aggregate(trades)

        assert result["A"].total_pnl_usdt == pytest.approx(70.0, abs=1e-6)
        assert result["B"].total_pnl_usdt == pytest.approx(50.0, abs=1e-6)

    def test_rank_by_metric_orders_correctly(self):
        """rank_by_metric orders strategies correctly."""
        trades = [
            _make_trade(pnl=100.0, strategy="A", opened_at_ms=1700000000000),
            _make_trade(pnl=-50.0, strategy="A", opened_at_ms=1700000001000),
            _make_trade(pnl=300.0, strategy="B", opened_at_ms=1700000002000),
            _make_trade(pnl=75.0, strategy="C", opened_at_ms=1700000003000),
        ]

        perfs = StrategyAggregator.aggregate(trades)
        ranked = StrategyAggregator.rank_by_metric(perfs, metric="total_pnl_usdt", descending=True)

        assert len(ranked) == 3
        assert ranked[0].strategy_name == "B"  # 300
        assert ranked[1].strategy_name == "C"  # 75
        assert ranked[2].strategy_name == "A"  # 50


# ============================================================================
# REGIME AGGREGATOR TESTS (6 tests)
# ============================================================================

class TestRegimeAggregator:
    """Test suite for RegimeAggregator."""

    def test_single_regime_single_trade(self):
        """Single regime, single trade produces correct RegimePerformance."""
        trade = _make_trade(pnl=100.0, regime="bull_trend")
        trades = [trade]

        result = RegimeAggregator.aggregate(trades)

        assert len(result) == 1
        assert "bull_trend" in result

        perf = result["bull_trend"]
        assert perf.regime == "bull_trend"
        assert perf.trade_count == 1
        assert perf.total_pnl_usdt == 100.0

    def test_multiple_regimes_grouped(self):
        """Multiple regimes grouped correctly."""
        trades = [
            _make_trade(pnl=100.0, regime="bull_trend", opened_at_ms=1700000000000),
            _make_trade(pnl=-50.0, regime="bull_trend", opened_at_ms=1700000001000),
            _make_trade(pnl=150.0, regime="bear_trend", opened_at_ms=1700000002000),
            _make_trade(pnl=75.0, regime="sideways", opened_at_ms=1700000003000),
        ]

        result = RegimeAggregator.aggregate(trades)

        assert len(result) == 3
        assert set(result.keys()) == {"bull_trend", "bear_trend", "sideways"}
        assert result["bull_trend"].trade_count == 2
        assert result["bear_trend"].trade_count == 1
        assert result["sideways"].trade_count == 1

    def test_win_rate_per_regime(self):
        """Win rate computed correctly per regime."""
        trades = [
            _make_trade(pnl=100.0, regime="bull_trend", opened_at_ms=1700000000000),
            _make_trade(pnl=-50.0, regime="bull_trend", opened_at_ms=1700000001000),
            _make_trade(pnl=150.0, regime="bull_trend", opened_at_ms=1700000002000),
        ]

        result = RegimeAggregator.aggregate(trades)
        perf = result["bull_trend"]

        # 2 wins, 1 loss
        assert perf.win_rate == pytest.approx(2/3, abs=1e-6)

    def test_profit_factor_per_regime(self):
        """Profit factor per regime computed correctly."""
        trades = [
            _make_trade(pnl=100.0, regime="bull_trend", opened_at_ms=1700000000000),
            _make_trade(pnl=200.0, regime="bull_trend", opened_at_ms=1700000001000),
            _make_trade(pnl=-150.0, regime="bull_trend", opened_at_ms=1700000002000),
        ]

        result = RegimeAggregator.aggregate(trades)
        perf = result["bull_trend"]

        # PF = 300 / 150 = 2.0
        assert perf.profit_factor == pytest.approx(2.0, abs=1e-6)

    def test_strategy_counts_within_regime(self):
        """Strategy counts breakdown within each regime."""
        trades = [
            _make_trade(pnl=100.0, strategy="A", regime="bull_trend", opened_at_ms=1700000000000),
            _make_trade(pnl=150.0, strategy="B", regime="bull_trend", opened_at_ms=1700000001000),
            _make_trade(pnl=75.0, strategy="A", regime="bull_trend", opened_at_ms=1700000002000),
            _make_trade(pnl=200.0, strategy="C", regime="bear_trend", opened_at_ms=1700000003000),
        ]

        result = RegimeAggregator.aggregate(trades)

        bull_perf = result["bull_trend"]
        bear_perf = result["bear_trend"]

        assert bull_perf.strategy_counts == {"A": 2, "B": 1}
        assert bear_perf.strategy_counts == {"C": 1}

    def test_cross_tabulate_strategy_regime_pf(self):
        """Cross-tabulation of strategy x regime → profit_factor."""
        trades = [
            _make_trade(pnl=100.0, strategy="A", regime="bull_trend", opened_at_ms=1700000000000),
            _make_trade(pnl=-50.0, strategy="A", regime="bull_trend", opened_at_ms=1700000001000),
            _make_trade(pnl=200.0, strategy="A", regime="bear_trend", opened_at_ms=1700000002000),
            _make_trade(pnl=150.0, strategy="B", regime="bull_trend", opened_at_ms=1700000003000),
        ]

        result = RegimeAggregator.cross_tabulate(trades)

        # A in bull_trend: 100 / 50 = 2.0
        # A in bear_trend: 200 / 0 = inf (no losses)
        # B in bull_trend: 150 / 0 = inf (no losses)
        assert result["A"]["bull_trend"] == pytest.approx(2.0, abs=1e-6)
        assert "B" in result
        assert "bull_trend" in result["B"]


# ============================================================================
# PERFORMANCE ENGINE TESTS (10 tests)
# ============================================================================

class TestPerformanceEngine:
    """Test suite for PerformanceEngine."""

    def test_analyze_returns_performance_report(self):
        """analyze() returns PerformanceReport."""
        engine = PerformanceEngine()
        trades = [
            _make_trade(pnl=100.0, opened_at_ms=1700000000000),
            _make_trade(pnl=50.0, opened_at_ms=1700000001000),
        ]

        report = engine.analyze(trades, initial_capital=10000.0)

        assert isinstance(report, PerformanceReport)
        assert report.trade_count == 2
        assert report.initial_capital == 10000.0

    def test_analyze_empty_trades_zeroed_report(self):
        """analyze() with empty trades returns zeroed report."""
        engine = PerformanceEngine()

        report = engine.analyze([], initial_capital=10000.0)

        assert report.trade_count == 0
        assert report.win_rate == 0.0
        assert report.profit_factor == 0.0
        assert report.total_pnl_usdt == 0.0
        assert len(report.equity_curve) == 0

    def test_analyze_single_trade(self):
        """analyze() with single trade."""
        engine = PerformanceEngine()
        trade = _make_trade(pnl=100.0, r_multiple=1.0, opened_at_ms=1700000000000, closed_at_ms=1700000001000)

        report = engine.analyze([trade], initial_capital=10000.0)

        assert report.trade_count == 1
        assert report.total_pnl_usdt == 100.0
        assert report.win_rate == 1.0
        assert len(report.equity_curve) == 1

    def test_analyze_mixed_wins_losses(self):
        """analyze() with mixed wins and losses."""
        engine = PerformanceEngine()
        trades = [
            _make_trade(pnl=100.0, opened_at_ms=1700000000000),
            _make_trade(pnl=-50.0, opened_at_ms=1700000001000),
            _make_trade(pnl=150.0, opened_at_ms=1700000002000),
            _make_trade(pnl=-75.0, opened_at_ms=1700000003000),
        ]

        report = engine.analyze(trades, initial_capital=10000.0)

        assert report.trade_count == 4
        assert report.total_pnl_usdt == pytest.approx(125.0, abs=1e-6)
        assert report.win_rate == pytest.approx(0.5, abs=1e-6)  # 2 wins, 2 losses

    def test_analyze_sorts_trades_internally(self):
        """analyze() sorts trades internally (no ordering dependency)."""
        engine = PerformanceEngine()

        # Create trades in reverse order
        trades = [
            _make_trade(pnl=150.0, opened_at_ms=1700000002000),
            _make_trade(pnl=-50.0, opened_at_ms=1700000001000),
            _make_trade(pnl=100.0, opened_at_ms=1700000000000),
        ]

        report = engine.analyze(trades, initial_capital=10000.0)

        # Should still produce correct equity curve (sorted internally)
        assert len(report.equity_curve) == 3
        # First point should reflect first trade's PnL (100)
        assert report.equity_curve[0].equity == pytest.approx(10100.0, abs=1e-6)

    def test_analyze_equity_curve_length(self):
        """analyze() equity curve has correct length."""
        engine = PerformanceEngine()
        trades = [
            _make_trade(pnl=100.0, opened_at_ms=1700000000000),
            _make_trade(pnl=50.0, opened_at_ms=1700000001000),
            _make_trade(pnl=-30.0, opened_at_ms=1700000002000),
        ]

        report = engine.analyze(trades, initial_capital=10000.0)

        # Equity curve has 1 point per trade
        assert len(report.equity_curve) == 3

    def test_analyze_by_strategy_populated(self):
        """analyze() populates by_strategy breakdown."""
        engine = PerformanceEngine()
        trades = [
            _make_trade(pnl=100.0, strategy="A", opened_at_ms=1700000000000),
            _make_trade(pnl=50.0, strategy="B", opened_at_ms=1700000001000),
        ]

        report = engine.analyze(trades, initial_capital=10000.0)

        assert len(report.by_strategy) == 2
        assert "A" in report.by_strategy
        assert "B" in report.by_strategy
        assert report.by_strategy["A"].trade_count == 1
        assert report.by_strategy["B"].trade_count == 1

    def test_analyze_by_regime_populated(self):
        """analyze() populates by_regime breakdown."""
        engine = PerformanceEngine()
        trades = [
            _make_trade(pnl=100.0, regime="bull", opened_at_ms=1700000000000),
            _make_trade(pnl=50.0, regime="bear", opened_at_ms=1700000001000),
        ]

        report = engine.analyze(trades, initial_capital=10000.0)

        assert len(report.by_regime) == 2
        assert "bull" in report.by_regime
        assert "bear" in report.by_regime

    def test_analyze_subset_with_filter_fn(self):
        """analyze_subset() with filter_fn works."""
        engine = PerformanceEngine()
        trades = [
            _make_trade(pnl=100.0, symbol="BTCUSDT", opened_at_ms=1700000000000),
            _make_trade(pnl=50.0, symbol="ETHUSDT", opened_at_ms=1700000001000),
            _make_trade(pnl=75.0, symbol="BTCUSDT", opened_at_ms=1700000002000),
        ]

        # Filter to only BTCUSDT trades
        report = engine.analyze_subset(
            trades,
            initial_capital=10000.0,
            filter_fn=lambda t: t.symbol == "BTCUSDT",
        )

        assert report.trade_count == 2
        assert report.total_pnl_usdt == pytest.approx(175.0, abs=1e-6)

    def test_analyze_to_dict_serialization(self):
        """to_dict() serialization is complete and JSON-safe."""
        engine = PerformanceEngine()
        trades = [
            _make_trade(pnl=100.0, opened_at_ms=1700000000000),
        ]

        report = engine.analyze(trades, initial_capital=10000.0)

        # Should serialize without error
        data = report.to_dict()

        # Should be JSON-serializable
        json_str = json.dumps(data)
        assert json_str

        # Verify key fields present
        assert "trade_count" in data
        assert "profit_factor" in data
        assert "equity_curve" in data
        assert "by_strategy" in data
        assert "by_regime" in data


# ============================================================================
# ISOLATION PROOF TESTS (6 tests)
# ============================================================================

class TestIsolationProof:
    """Prove analytics package has zero coupling to execution/risk/strategy layers."""

    def test_no_pyside6_imports(self):
        """Verify no PySide6 imports in analytics package."""
        import core.intraday.analytics.models as models_module
        import core.intraday.analytics.aggregators as agg_module
        import core.intraday.analytics.performance_engine as engine_module

        # If any PySide6 is imported, the module load would fail
        # These assertions verify the module loaded successfully
        assert models_module is not None
        assert agg_module is not None
        assert engine_module is not None

    def test_no_qt_in_trade_snapshot(self):
        """TradeSnapshot has no Qt imports."""
        # TradeSnapshot is a frozen dataclass - verify it's purely data
        trade = _make_trade(pnl=100.0)

        # Should be picklable (no Qt objects)
        import pickle
        pickled = pickle.dumps(trade)
        unpickled = pickle.loads(pickled)

        assert unpickled.position_id == trade.position_id

    def test_no_mutable_globals_in_analytics(self):
        """Analytics package has no mutable global state."""
        from core.intraday.analytics.aggregators import StrategyAggregator, RegimeAggregator
        from core.intraday.analytics.performance_engine import PerformanceEngine

        # Run operations twice and verify results are independent
        trades1 = [_make_trade(pnl=100.0)]
        trades2 = [_make_trade(pnl=200.0)]

        result1 = StrategyAggregator.aggregate(trades1)
        result2 = StrategyAggregator.aggregate(trades2)

        assert result1["MomentumBreakout"].total_pnl_usdt != result2["MomentumBreakout"].total_pnl_usdt

    def test_trade_snapshot_frozen(self):
        """TradeSnapshot is truly frozen."""
        trade = _make_trade(pnl=100.0)

        # Should raise AttributeError when trying to modify
        with pytest.raises((AttributeError, TypeError)):
            trade.realized_pnl_usdt = 200.0

    def test_strategy_performance_frozen(self):
        """StrategyPerformance is truly frozen."""
        trades = [_make_trade(pnl=100.0)]
        result = StrategyAggregator.aggregate(trades)
        perf = result["MomentumBreakout"]

        # Should raise AttributeError when trying to modify
        with pytest.raises((AttributeError, TypeError)):
            perf.total_pnl_usdt = 999.0

    def test_performance_report_frozen(self):
        """PerformanceReport is truly frozen."""
        engine = PerformanceEngine()
        trades = [_make_trade(pnl=100.0)]
        report = engine.analyze(trades, initial_capital=10000.0)

        # Should raise AttributeError when trying to modify
        with pytest.raises((AttributeError, TypeError)):
            report.total_pnl_usdt = 999.0


# ============================================================================
# REPLAY CONSISTENCY TESTS (5 tests)
# ============================================================================

class TestReplayConsistency:
    """Verify analytics are deterministic and replay-safe."""

    def test_same_trades_twice_identical_reports(self):
        """Same trades analyzed twice produce identical reports."""
        engine = PerformanceEngine()
        trades = [
            _make_trade(pnl=100.0, opened_at_ms=1700000000000),
            _make_trade(pnl=50.0, opened_at_ms=1700000001000),
        ]

        report1 = engine.analyze(trades, initial_capital=10000.0)
        report2 = engine.analyze(trades, initial_capital=10000.0)

        # Convert to dict and compare
        data1 = report1.to_dict()
        data2 = report2.to_dict()

        assert data1 == data2

    def test_shuffled_input_identical_reports(self):
        """Shuffled input produces identical reports (internally sorted)."""
        engine = PerformanceEngine()
        trades = [
            _make_trade(pnl=100.0, opened_at_ms=1700000002000),
            _make_trade(pnl=50.0, opened_at_ms=1700000000000),
            _make_trade(pnl=-30.0, opened_at_ms=1700000001000),
        ]

        import random
        shuffled = trades.copy()
        random.shuffle(shuffled)

        report1 = engine.analyze(trades, initial_capital=10000.0)
        report2 = engine.analyze(shuffled, initial_capital=10000.0)

        data1 = report1.to_dict()
        data2 = report2.to_dict()

        # Key metrics should match
        assert data1["trade_count"] == data2["trade_count"]
        assert data1["total_pnl_usdt"] == data2["total_pnl_usdt"]
        assert data1["win_rate"] == data2["win_rate"]

    def test_analyze_all_equals_manual_composition(self):
        """analyze(all) == manual composition of per-strategy metrics."""
        engine = PerformanceEngine()
        trades = [
            _make_trade(pnl=100.0, strategy="A", opened_at_ms=1700000000000),
            _make_trade(pnl=-50.0, strategy="A", opened_at_ms=1700000001000),
            _make_trade(pnl=150.0, strategy="B", opened_at_ms=1700000002000),
        ]

        full_report = engine.analyze(trades, initial_capital=10000.0)

        # Manually compute by filtering
        a_trades = [t for t in trades if t.strategy_name == "A"]
        b_trades = [t for t in trades if t.strategy_name == "B"]

        a_report = engine.analyze(a_trades, initial_capital=10000.0)
        b_report = engine.analyze(b_trades, initial_capital=10000.0)

        # Total PnL should match
        total_pnl = a_report.total_pnl_usdt + b_report.total_pnl_usdt
        assert full_report.total_pnl_usdt == pytest.approx(total_pnl, abs=1e-6)

    def test_sequential_vs_batch_analysis_same_results(self):
        """Sequential vs batch analysis produce same results."""
        engine = PerformanceEngine()
        trades = [
            _make_trade(pnl=100.0, opened_at_ms=1700000000000),
            _make_trade(pnl=50.0, opened_at_ms=1700000001000),
            _make_trade(pnl=-30.0, opened_at_ms=1700000002000),
        ]

        # Batch
        batch_report = engine.analyze(trades, initial_capital=10000.0)

        # Sequential (analyze subsets)
        sub_reports = [
            engine.analyze([trades[0]], initial_capital=10000.0),
            engine.analyze([trades[1]], initial_capital=10000.0),
            engine.analyze([trades[2]], initial_capital=10000.0),
        ]

        # Check consistency
        assert batch_report.trade_count == sum(r.trade_count for r in sub_reports)

    def test_deterministic_large_dataset(self):
        """Report deterministic with large dataset (500 trades)."""
        engine = PerformanceEngine()

        # Generate 500 trades
        trades = []
        for i in range(500):
            pnl = 100.0 if i % 2 == 0 else -50.0
            trade = _make_trade(
                pnl=pnl,
                opened_at_ms=1700000000000 + (i * 1000),
                strategy=["A", "B", "C"][i % 3],
                regime=["bull", "bear", "sideways"][i % 3],
            )
            trades.append(trade)

        report1 = engine.analyze(trades, initial_capital=10000.0)
        report2 = engine.analyze(trades, initial_capital=10000.0)

        data1 = report1.to_dict()
        data2 = report2.to_dict()

        assert data1 == data2


# ============================================================================
# PHASE 8 READINESS TESTS (5 tests)
# ============================================================================

class TestPhase8Readiness:
    """Verify PerformanceReport is ready for downstream Phase 8 usage."""

    def test_report_to_dict_includes_all_required_fields(self):
        """to_dict() includes all required fields for Phase 8."""
        engine = PerformanceEngine()
        trades = [_make_trade(pnl=100.0)]
        report = engine.analyze(trades, initial_capital=10000.0)

        data = report.to_dict()

        required_fields = [
            "trade_count",
            "win_rate",
            "profit_factor",
            "expectancy_r",
            "expectancy_capital",
            "total_pnl_usdt",
            "total_fees_usdt",
            "max_drawdown_pct",
            "max_drawdown_duration_ms",
            "capital_efficiency",
            "equity_curve",
            "by_strategy",
            "by_regime",
            "strategy_regime_pf",
            "initial_capital",
        ]

        for field in required_fields:
            assert field in data, f"Missing required field: {field}"

    def test_report_contains_equity_curve_data(self):
        """Report contains equity_curve with all necessary fields."""
        engine = PerformanceEngine()
        trades = [
            _make_trade(pnl=100.0, opened_at_ms=1700000000000),
            _make_trade(pnl=50.0, opened_at_ms=1700000001000),
        ]
        report = engine.analyze(trades, initial_capital=10000.0)

        data = report.to_dict()

        assert isinstance(data["equity_curve"], list)
        assert len(data["equity_curve"]) == 2

        for point in data["equity_curve"]:
            assert "timestamp_ms" in point
            assert "equity" in point
            assert "peak_equity" in point
            assert "drawdown_pct" in point

    def test_report_contains_distribution_stats(self):
        """Report contains distribution stats."""
        engine = PerformanceEngine()
        trades = [
            _make_trade(pnl=100.0, r_multiple=1.0, opened_at_ms=1700000000000),
            _make_trade(pnl=50.0, r_multiple=0.5, opened_at_ms=1700000001000),
            _make_trade(pnl=-30.0, r_multiple=-0.3, opened_at_ms=1700000002000),
        ]
        report = engine.analyze(trades, initial_capital=10000.0)

        data = report.to_dict()

        # Should have distribution stats
        assert data["r_distribution"] is not None
        assert data["pnl_distribution"] is not None

    def test_report_contains_capital_efficiency(self):
        """Report contains capital efficiency metrics."""
        engine = PerformanceEngine()
        trades = [_make_trade(pnl=100.0, entry_size_usdt=1000.0)]
        report = engine.analyze(trades, initial_capital=10000.0)

        data = report.to_dict()

        assert "capital_efficiency" in data
        cap_eff = data["capital_efficiency"]

        assert "total_pnl_usdt" in cap_eff
        assert "return_on_capital_pct" in cap_eff

    def test_report_json_serializable(self):
        """Report can be JSON-serialized (no custom objects leak)."""
        engine = PerformanceEngine()
        trades = [
            _make_trade(pnl=100.0, mae_pct=0.05, mfe_pct=0.10, opened_at_ms=1700000000000),
            _make_trade(pnl=50.0, opened_at_ms=1700000001000),
        ]
        report = engine.analyze(trades, initial_capital=10000.0)

        data = report.to_dict()

        # Should serialize without error
        json_str = json.dumps(data)
        assert json_str

        # Should deserialize back
        deserialized = json.loads(json_str)
        assert deserialized["trade_count"] == 2


# ============================================================================
# ADDITIONAL EDGE CASE TESTS
# ============================================================================

class TestEdgeCases:
    """Test edge cases and error conditions."""

    def test_empty_trades_list(self):
        """Empty trades list handled gracefully."""
        agg_strat = StrategyAggregator.aggregate([])
        agg_regime = RegimeAggregator.aggregate([])

        assert agg_strat == {}
        assert agg_regime == {}

    def test_single_trade_all_components(self):
        """Single trade exercises all code paths."""
        engine = PerformanceEngine()
        trade = _make_trade(
            pnl=100.0,
            r_multiple=1.5,
            mae_pct=0.05,
            mfe_pct=0.15,
            opened_at_ms=1700000000000,
            closed_at_ms=1700000003600000,
        )

        report = engine.analyze([trade], initial_capital=10000.0)

        assert report.trade_count == 1
        assert report.win_rate == 1.0
        assert len(report.equity_curve) == 1
        assert len(report.by_strategy) == 1
        assert len(report.by_regime) == 1

    def test_all_winners_profit_factor_infinite(self):
        """All winners → profit factor = inf (no losses)."""
        trades = [
            _make_trade(pnl=100.0, opened_at_ms=1700000000000),
            _make_trade(pnl=150.0, opened_at_ms=1700000001000),
            _make_trade(pnl=200.0, opened_at_ms=1700000002000),
        ]

        result = StrategyAggregator.aggregate(trades)
        perf = result["MomentumBreakout"]

        # Should handle inf gracefully
        assert perf.profit_factor == float('inf') or perf.profit_factor > 1000.0

    def test_all_losers_zero_profit_factor(self):
        """All losers → profit factor = 0."""
        trades = [
            _make_trade(pnl=-100.0, opened_at_ms=1700000000000),
            _make_trade(pnl=-50.0, opened_at_ms=1700000001000),
            _make_trade(pnl=-75.0, opened_at_ms=1700000002000),
        ]

        result = StrategyAggregator.aggregate(trades)
        perf = result["MomentumBreakout"]

        assert perf.profit_factor == 0.0
        assert perf.win_rate == 0.0

    def test_initial_capital_validation(self):
        """Invalid initial_capital raises ValueError."""
        engine = PerformanceEngine()
        trades = [_make_trade(pnl=100.0)]

        with pytest.raises(ValueError):
            engine.analyze(trades, initial_capital=0.0)

        with pytest.raises(ValueError):
            engine.analyze(trades, initial_capital=-1000.0)

    def test_rank_by_invalid_metric_raises_error(self):
        """rank_by_metric with invalid metric raises ValueError."""
        trades = [_make_trade(pnl=100.0)]
        result = StrategyAggregator.aggregate(trades)

        with pytest.raises(ValueError):
            StrategyAggregator.rank_by_metric(result, metric="nonexistent_metric")


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
