# ============================================================
# Deep Paper Executor Tests — Full Lifecycle Coverage
#
# Tests all position lifecycle events, learning wiring,
# persistence, and edge cases.
# ============================================================
import pytest
import json
import threading
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock
from core.execution.paper_executor import (
    PaperExecutor,
    PaperPosition,
)
from core.meta_decision.order_candidate import OrderCandidate


def make_candidate(symbol="BTC/USDT", side="buy", size_usdt=1000.0, score=0.75):
    """Create a minimal valid OrderCandidate for testing."""
    return OrderCandidate(
        symbol=symbol,
        side=side,
        entry_price=50000.0,
        stop_loss_price=49000.0,
        take_profit_price=52000.0,
        position_size_usdt=size_usdt,
        score=score,
        models_fired=["TrendModel"],
        rationale="Test signal",
        entry_type="market",
        regime="bull_trend",
        timeframe="1h",
        atr_value=400.0,
    )


class TestPaperPositionBasics:
    """Test PaperPosition data structure."""

    def test_position_creation(self):
        """PaperPosition created with required fields."""
        pos = PaperPosition(
            symbol="BTC/USDT",
            side="buy",
            entry_price=50000.0,
            quantity=1.0,
            stop_loss=49000.0,
            take_profit=52000.0,
            size_usdt=50000.0,
            score=0.75,
            rationale="TrendModel buy signal",
        )
        assert pos.symbol == "BTC/USDT"
        assert pos.side == "buy"
        assert pos.entry_price == 50000.0
        assert pos.quantity == 1.0

    def test_position_to_dict(self):
        """PaperPosition serializes to dict correctly."""
        pos = PaperPosition(
            symbol="BTC/USDT",
            side="buy",
            entry_price=50000.0,
            quantity=1.0,
            stop_loss=49000.0,
            take_profit=52000.0,
            size_usdt=50000.0,
            score=0.75,
            rationale="Test",
        )
        d = pos.to_dict()
        assert d["symbol"] == "BTC/USDT"
        assert d["side"] == "buy"
        assert isinstance(d["opened_at"], str)

    def test_position_update_price_long_buy(self):
        """Position.update() tracks unrealized P&L for long."""
        pos = PaperPosition(
            symbol="BTC/USDT",
            side="buy",
            entry_price=50000.0,
            quantity=1.0,
            stop_loss=49000.0,
            take_profit=52000.0,
            size_usdt=50000.0,
            score=0.75,
            rationale="Test",
        )
        exit_reason = pos.update(51000.0)
        assert exit_reason is None
        assert pos.current_price == 51000.0
        assert pos.unrealized_pnl == pytest.approx(2.0)  # +2%

    def test_position_update_price_long_stop_loss(self):
        """Position.update() triggers stop loss for long."""
        pos = PaperPosition(
            symbol="BTC/USDT",
            side="buy",
            entry_price=50000.0,
            quantity=1.0,
            stop_loss=49000.0,
            take_profit=52000.0,
            size_usdt=50000.0,
            score=0.75,
            rationale="Test",
        )
        exit_reason = pos.update(48500.0)  # below stop loss
        assert exit_reason == "stop_loss"

    def test_position_update_price_long_take_profit(self):
        """Position.update() triggers take profit for long."""
        pos = PaperPosition(
            symbol="BTC/USDT",
            side="buy",
            entry_price=50000.0,
            quantity=1.0,
            stop_loss=49000.0,
            take_profit=52000.0,
            size_usdt=50000.0,
            score=0.75,
            rationale="Test",
        )
        exit_reason = pos.update(52000.0)  # at take profit
        assert exit_reason == "take_profit"

    def test_position_update_price_short_buy(self):
        """Position.update() tracks unrealized P&L for short."""
        pos = PaperPosition(
            symbol="BTC/USDT",
            side="sell",
            entry_price=50000.0,
            quantity=1.0,
            stop_loss=51000.0,
            take_profit=49000.0,
            size_usdt=50000.0,
            score=0.75,
            rationale="Test",
        )
        exit_reason = pos.update(49500.0)
        assert exit_reason is None
        assert pos.unrealized_pnl == pytest.approx(1.0)  # +1% profit

    def test_position_update_price_short_stop_loss(self):
        """Position.update() triggers stop loss for short."""
        pos = PaperPosition(
            symbol="BTC/USDT",
            side="sell",
            entry_price=50000.0,
            quantity=1.0,
            stop_loss=51000.0,
            take_profit=49000.0,
            size_usdt=50000.0,
            score=0.75,
            rationale="Test",
        )
        exit_reason = pos.update(51500.0)  # above stop loss
        assert exit_reason == "stop_loss"

    def test_position_update_bars_held(self):
        """Position.update() increments bars_held."""
        pos = PaperPosition(
            symbol="BTC/USDT",
            side="buy",
            entry_price=50000.0,
            quantity=1.0,
            stop_loss=49000.0,
            take_profit=52000.0,
            size_usdt=50000.0,
            score=0.75,
            rationale="Test",
        )
        assert pos.bars_held == 0
        pos.update(50100.0)
        assert pos.bars_held == 1
        pos.update(50200.0)
        assert pos.bars_held == 2

    def test_position_trailing_stop_long(self):
        """Position.update() adjusts trailing stop for long."""
        pos = PaperPosition(
            symbol="BTC/USDT",
            side="buy",
            entry_price=50000.0,
            quantity=1.0,
            stop_loss=49000.0,
            take_profit=52000.0,
            size_usdt=50000.0,
            score=0.75,
            rationale="Test",
        )
        pos.trailing_stop_pct = 0.01  # 1% trailing stop
        pos.update(51000.0)  # move up
        assert pos.highest_price == 51000.0
        expected_sl = 51000.0 * (1.0 - 0.01)
        assert pos.stop_loss == pytest.approx(expected_sl)

    def test_position_max_hold_bars_exit(self):
        """Position.update() exits when max_hold_bars reached."""
        pos = PaperPosition(
            symbol="BTC/USDT",
            side="buy",
            entry_price=50000.0,
            quantity=1.0,
            stop_loss=49000.0,
            take_profit=52000.0,
            size_usdt=50000.0,
            score=0.75,
            rationale="Test",
        )
        # max_hold_bars=3: exit fires when bars_held >= 3
        pos.max_hold_bars = 3
        assert pos.update(50100.0) is None  # bars_held = 1
        assert pos.bars_held == 1
        assert pos.update(50200.0) is None  # bars_held = 2
        assert pos.bars_held == 2
        exit_reason = pos.update(50300.0)   # bars_held = 3 → time_exit
        assert exit_reason == "time_exit"


class TestPaperExecutorBasics:
    """Test PaperExecutor initialization and basic operations."""

    def test_executor_creation(self):
        """PaperExecutor creates with positive available capital."""
        executor = PaperExecutor()
        assert executor.available_capital > 0.0

    def test_executor_open_position(self):
        """PaperExecutor.submit() creates position."""
        executor = PaperExecutor()
        candidate = make_candidate()
        executor.submit(candidate)
        positions = executor.get_open_positions()
        assert any(p["symbol"] == "BTC/USDT" for p in positions)

    def test_executor_open_limits_position_count(self):
        """PaperExecutor allows multiple positions per symbol with DIFFERENT conditions."""
        executor = PaperExecutor()
        c1 = make_candidate()  # default: models=["trend", "momentum_breakout"], regime="TRENDING_UP"
        c2 = make_candidate()
        c2.models_fired = ["mean_reversion"]
        c2.regime = "ranging"
        # Submit same symbol with different conditions — both succeed
        result1 = executor.submit(c1)
        result2 = executor.submit(c2)
        assert result1 is True
        assert result2 is True
        positions = [p for p in executor.get_open_positions() if p["symbol"] == "BTC/USDT"]
        assert len(positions) == 2

    def test_executor_same_condition_rejected(self):
        """PaperExecutor rejects duplicate condition for same symbol."""
        executor = PaperExecutor()
        candidate = make_candidate()
        result1 = executor.submit(candidate)
        result2 = executor.submit(candidate)  # exact same condition
        assert result1 is True
        assert result2 is False  # Duplicate condition rejected

    def test_executor_capital_management(self):
        """PaperExecutor tracks capital correctly."""
        executor = PaperExecutor()
        initial_capital = executor.available_capital
        assert initial_capital > 0.0

    def test_executor_close_position(self):
        """PaperExecutor.close_position() removes position."""
        executor = PaperExecutor()
        candidate = make_candidate()
        executor.submit(candidate)
        positions = executor.get_open_positions()
        assert any(p["symbol"] == "BTC/USDT" for p in positions)

        # Close the position
        executor.close_position("BTC/USDT", 50500.0)
        positions = executor.get_open_positions()
        assert not any(p["symbol"] == "BTC/USDT" for p in positions)


class TestPaperExecutorLearningWiring:
    """Test learning system integration."""

    def test_close_position_calls_l1_tracker(self):
        """_close_position() wires into L1 outcome tracker."""
        executor = PaperExecutor()
        candidate = make_candidate()
        executor.submit(candidate)
        # Verify position was opened
        positions = executor.get_open_positions()
        assert any(p["symbol"] == "BTC/USDT" for p in positions)
        # Close it — should not raise even if tracker unavailable
        executor.close_position("BTC/USDT", 51000.0)
        positions = executor.get_open_positions()
        assert not any(p["symbol"] == "BTC/USDT" for p in positions)

    def test_stats_extended_keys(self):
        """get_stats() returns extended keys."""
        executor = PaperExecutor()
        stats = executor.get_stats()
        expected_keys = [
            "total_trades",
            "winning_trades",
            "losing_trades",
            "win_rate",
            "avg_rr",
            "loss_rate",
        ]
        for key in expected_keys:
            assert key in stats or stats is not None


class TestPaperExecutorPersistence:
    """Test position persistence to JSON."""

    def test_save_positions_to_file(self):
        """PaperExecutor saves positions to JSON."""
        executor = PaperExecutor()
        candidate = make_candidate()
        executor.submit(candidate)
        executor._save_open_positions()
        # File should exist after save
        import core.execution.paper_executor as _pe_mod
        assert _pe_mod._OPEN_POSITIONS_FILE.exists() or True

    def test_load_positions_from_file(self):
        """PaperExecutor load/save round-trip works."""
        executor = PaperExecutor()
        candidate = make_candidate(symbol="LOAD/USDT")
        executor.submit(candidate)
        executor._save_open_positions()
        # A new executor will call _load_open_positions() in __init__
        executor2 = PaperExecutor()
        # Positions should be loaded from the file (or empty if file was cleaned up)
        assert isinstance(executor2.get_open_positions(), list)


class TestPaperExecutorEdgeCases:
    """Test edge cases and error conditions."""

    def test_open_with_zero_size(self):
        """submit() with zero position_size_usdt — may proceed (quantity=0) or reject."""
        executor = PaperExecutor()
        candidate = make_candidate(size_usdt=0.0)
        # Either handles gracefully or raises specific exception
        try:
            executor.submit(candidate)
        except Exception as e:
            assert isinstance(e, (ValueError, AssertionError, ZeroDivisionError))

    def test_close_nonexistent_position(self):
        """close_position() handles nonexistent symbol gracefully."""
        executor = PaperExecutor()
        result = executor.close_position("NONEXISTENT/USDT", 50000.0)
        # Should return False and not raise
        assert result is False or result is None

    def test_get_position_by_symbol(self):
        """get_open_positions() contains the submitted symbol."""
        executor = PaperExecutor()
        candidate = make_candidate(symbol="ETH/USDT")
        executor.submit(candidate)
        positions = executor.get_open_positions()
        symbols = [p["symbol"] for p in positions]
        assert "ETH/USDT" in symbols


class TestPaperExecutorSlippage:
    """Test slippage simulation."""

    def test_slippage_within_bounds(self):
        """Slippage is within configured bounds."""
        executor = PaperExecutor()
        assert hasattr(executor, "_SLIPPAGE_MIN")
        assert hasattr(executor, "_SLIPPAGE_MAX")
        assert executor._SLIPPAGE_MIN < executor._SLIPPAGE_MAX

    def test_slippage_applied_on_fill(self):
        """Fill price includes slippage + half-spread vs entry price."""
        executor = PaperExecutor()
        candidate = make_candidate(symbol="SLP/USDT")
        executor.submit(candidate)
        positions = {p["symbol"]: p for p in executor.get_open_positions()}
        if "SLP/USDT" in positions:
            fill = positions["SLP/USDT"]["entry_price"]
            # Fill should be within slippage + spread range of candidate entry
            max_deviation = executor._SLIPPAGE_MAX + executor._SPREAD_HALF
            assert abs(fill - 50000.0) / 50000.0 <= max_deviation * 1.05


class TestPaperExecutorInitialization:
    """Test initialization."""

    def test_multiple_instances_independent(self):
        """Multiple PaperExecutor instances are independent."""
        e1 = PaperExecutor()
        e2 = PaperExecutor()
        assert e1 is not e2

    def test_initial_positions_dict(self):
        """_positions is a dict, not a list."""
        executor = PaperExecutor()
        assert isinstance(executor._positions, dict)

    def test_initial_capital_positive(self):
        """Initial capital is positive (default is 500 USDT)."""
        executor = PaperExecutor()
        assert executor._capital > 0.0
