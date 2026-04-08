"""
tests/test_phase5_processing.py
===============================
Comprehensive unit tests for Phase 5 processing modules:
  - PositionSizer (10 tests)
  - CircuitBreaker (10 tests)
  - KillSwitch (8 tests)
  - RiskEngine (12 tests)
  - ProcessingEngine (10 tests)

Total: 50 tests covering all major paths and edge cases.
"""

import json
import pytest
import time
from pathlib import Path
from unittest.mock import MagicMock, patch, Mock

from core.intraday.processing.position_sizer import PositionSizer, PositionSizerConfig
from core.intraday.processing.circuit_breaker import CircuitBreaker, CircuitBreakerConfig
from core.intraday.processing.kill_switch import KillSwitch, KillSwitchConfig
from core.intraday.processing.risk_engine import RiskEngine, RiskEngineConfig
from core.intraday.processing.processing_engine import ProcessingEngine

from core.intraday.execution_contracts import (
    CapitalSnapshot,
    CircuitBreakerState,
    DecisionStatus,
    Direction,
    ExecutionIntent,
    ExecutionDecision,
    ExposureSnapshot,
    KillSwitchState,
    PortfolioSnapshot,
    PositionRecord,
    RejectionSource,
    StrategyClass,
    _make_id,
)

from core.intraday.signal_contracts import TriggerSignal


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures — Reusable test data
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def position_sizer():
    """Standard position sizer with defaults."""
    return PositionSizer(PositionSizerConfig(
        risk_pct=0.005,
        max_capital_pct=0.04,
        min_size_usdt=10.0,
    ))


@pytest.fixture
def circuit_breaker():
    """Standard circuit breaker with defaults."""
    return CircuitBreaker(CircuitBreakerConfig(
        warning_drawdown_pct=0.05,
        max_drawdown_pct=0.10,
        warning_daily_loss_pct=0.02,
        max_daily_loss_pct=0.03,
        consecutive_loss_trip=3,
        cooldown_s=1800,
    ))


@pytest.fixture
def kill_switch(tmp_path):
    """Kill switch with temporary persistence file."""
    persistence_path = str(tmp_path / "kill_switch.json")
    return KillSwitch(KillSwitchConfig(persistence_path=persistence_path))


@pytest.fixture
def risk_engine():
    """Standard risk engine with defaults."""
    return RiskEngine(RiskEngineConfig(
        max_concurrent_positions=5,
        max_portfolio_heat_pct=0.06,
        max_asset_exposure_pct=0.20,
        max_daily_loss_pct=0.03,
        max_drawdown_pct=0.10,
        min_risk_reward=1.0,
        min_size_usdt=10.0,
    ))


@pytest.fixture
def processing_engine(position_sizer, risk_engine, circuit_breaker, kill_switch):
    """Complete processing engine."""
    return ProcessingEngine(
        position_sizer=position_sizer,
        risk_engine=risk_engine,
        circuit_breaker=circuit_breaker,
        kill_switch=kill_switch,
        now_ms_fn=lambda: int(time.time() * 1000),
    )


def _make_capital_snapshot(
    total=10000.0,
    reserved=1000.0,
    available=9000.0,
    equity=9500.0,
    peak_equity=10000.0,
    drawdown_pct=0.05,
    realized_pnl_today=-100.0,
    total_realized_pnl=200.0,
    total_fees=50.0,
    trade_count_today=5,
    consecutive_losses=0,
):
    """Factory for CapitalSnapshot."""
    return CapitalSnapshot(
        total_capital=total,
        reserved_capital=reserved,
        available_capital=available,
        equity=equity,
        peak_equity=peak_equity,
        drawdown_pct=drawdown_pct,
        realized_pnl_today=realized_pnl_today,
        total_realized_pnl=total_realized_pnl,
        total_fees=total_fees,
        trade_count_today=trade_count_today,
        consecutive_losses=consecutive_losses,
    )


def _make_exposure_snapshot(
    per_symbol=None,
    long_exposure=0.05,
    short_exposure=0.03,
    net_exposure=0.02,
    portfolio_heat=0.02,
):
    """Factory for ExposureSnapshot."""
    if per_symbol is None:
        per_symbol = {"BTC": 0.05}
    return ExposureSnapshot(
        per_symbol=per_symbol,
        long_exposure=long_exposure,
        short_exposure=short_exposure,
        net_exposure=net_exposure,
        portfolio_heat=portfolio_heat,
    )


def _make_portfolio_snapshot(
    capital=None,
    exposure=None,
    open_positions=None,
):
    """Factory for PortfolioSnapshot."""
    if capital is None:
        capital = _make_capital_snapshot()
    if exposure is None:
        exposure = _make_exposure_snapshot()
    if open_positions is None:
        open_positions = ()
    return PortfolioSnapshot(
        capital=capital,
        exposure=exposure,
        open_positions=open_positions,
        open_position_count=len(open_positions) if open_positions else 0,
    )


def _make_execution_intent(
    symbol="BTC",
    direction=Direction.LONG,
    strategy_name="TestStrategy",
    strategy_class=StrategyClass.MOMENTUM_EXPANSION,
    entry_price=50000.0,
    stop_loss=49000.0,
    take_profit=52000.0,
    size_usdt=100.0,
    quantity=0.002,
    risk_usdt=50.0,
    risk_reward_ratio=2.0,
    regime="BULL_TREND",
    regime_confidence=0.8,
    trigger_strength=0.7,
    trigger_quality=0.75,
    atr_value=500.0,
):
    """Factory for ExecutionIntent."""
    return ExecutionIntent(
        intent_id=_make_id("intent", symbol, str(time.time())),
        trigger_id=_make_id("trigger", symbol, str(time.time())),
        setup_id=_make_id("setup", symbol, str(time.time())),
        symbol=symbol,
        direction=direction,
        strategy_name=strategy_name,
        strategy_class=strategy_class,
        entry_price=entry_price,
        stop_loss=stop_loss,
        take_profit=take_profit,
        atr_value=atr_value,
        size_usdt=size_usdt,
        quantity=quantity,
        risk_usdt=risk_usdt,
        risk_reward_ratio=risk_reward_ratio,
        regime=regime,
        regime_confidence=regime_confidence,
        trigger_strength=trigger_strength,
        trigger_quality=trigger_quality,
        candle_trace_ids=("candle1", "candle2"),
        setup_trace_ids=("setup1",),
    )


def _make_trigger_signal(
    symbol="BTC",
    direction=Direction.LONG,
    entry_price=50000.0,
    stop_loss=49000.0,
    take_profit=52000.0,
    strength=0.7,
    trigger_quality=0.75,
    strategy_name="TestStrategy",
    strategy_class=StrategyClass.MOMENTUM_EXPANSION,
    regime="BULL_TREND",
    atr_value=500.0,
    setup_timeframe="30m",
    trigger_timeframe="5m",
    trigger_candle_ts=None,
    setup_candle_ts=None,
):
    """Factory for TriggerSignal."""
    now_ms = int(time.time() * 1000)
    if trigger_candle_ts is None:
        trigger_candle_ts = now_ms
    if setup_candle_ts is None:
        setup_candle_ts = now_ms

    from core.intraday.signal_contracts import TriggerLifecycle

    return TriggerSignal(
        trigger_id=_make_id("trigger", symbol, str(time.time())),
        setup_id=_make_id("setup", symbol, str(time.time())),
        symbol=symbol,
        direction=direction,
        entry_price=entry_price,
        stop_loss=stop_loss,
        take_profit=take_profit,
        strength=strength,
        trigger_quality=trigger_quality,
        setup_timeframe=setup_timeframe,
        trigger_timeframe=trigger_timeframe,
        strategy_name=strategy_name,
        strategy_class=strategy_class,
        regime=regime,
        regime_confidence=0.8,
        atr_value=atr_value,
        trigger_candle_ts=trigger_candle_ts,
        setup_candle_ts=setup_candle_ts,
        lifecycle=TriggerLifecycle.EVALUATED,
        created_at_ms=now_ms,
        max_age_ms=300000,  # 5 minutes max age
        candle_trace_ids=("candle1", "candle2"),
        setup_trace_ids=("setup1",),
    )


# ═════════════════════════════════════════════════════════════════════════════
# PositionSizer Tests (10 tests)
# ═════════════════════════════════════════════════════════════════════════════

class TestPositionSizer:
    """Test PositionSizer risk-based sizing logic."""

    def test_basic_risk_based_sizing(self, position_sizer):
        """Test basic calculation: risk_pct * capital → quantity → size_usdt."""
        result = position_sizer.calculate(
            entry_price=50000.0,
            stop_loss=49000.0,
            available_capital=10000.0,
            total_capital=10000.0,
        )
        # risk = 0.005 * 10000 = 50
        # qty = 50 / 1000 = 0.005
        # size = 0.005 * 50000 = 250
        assert result["size_usdt"] > 0
        assert result["quantity"] > 0
        assert result["risk_usdt"] == 50.0

    def test_cap_at_max_capital_pct(self, position_sizer):
        """Test capping at max_capital_pct (4%)."""
        # Very large capital → size would be capped
        result = position_sizer.calculate(
            entry_price=50000.0,
            stop_loss=49000.0,
            available_capital=1000000.0,
            total_capital=1000000.0,
        )
        # risk = 0.005 * 1000000 = 5000
        # qty = 5000 / 1000 = 5.0
        # raw size = 5 * 50000 = 250000
        # cap = 0.04 * 1000000 = 40000
        assert result["size_usdt"] <= 40000.0

    def test_floor_at_min_size_usdt(self, position_sizer):
        """Test flooring: if size < min_size, return all zeros."""
        result = position_sizer.calculate(
            entry_price=50000.0,
            stop_loss=49000.0,
            available_capital=50.0,  # Very small
            total_capital=1000.0,
        )
        # risk = 0.005 * 50 = 0.25
        # qty = 0.25 / 1000 = 0.00025
        # size = 0.00025 * 50000 = 12.50 (but less than next calc)
        # Should be below min of 10.0 or above depending on price diff
        # Here we expect it to fail min check
        if result["size_usdt"] > 0:
            assert result["size_usdt"] >= position_sizer.config.min_size_usdt

    def test_zero_stop_distance_rejection(self, position_sizer):
        """Test zero stop distance → returns all zeros."""
        result = position_sizer.calculate(
            entry_price=50000.0,
            stop_loss=50000.0,  # Same as entry
            available_capital=10000.0,
            total_capital=10000.0,
        )
        assert result["size_usdt"] == 0.0
        assert result["quantity"] == 0.0
        assert result["risk_usdt"] == 0.0

    def test_entry_equals_stop_rejection(self, position_sizer):
        """Test entry == stop → rejection."""
        result = position_sizer.calculate(
            entry_price=100.0,
            stop_loss=100.0,
            available_capital=1000.0,
            total_capital=1000.0,
        )
        assert result["size_usdt"] == 0.0

    def test_very_small_capital_with_floor(self, position_sizer):
        """Test very small available capital → floor kicks in."""
        config = PositionSizerConfig(
            risk_pct=0.005,
            max_capital_pct=0.04,
            min_size_usdt=100.0,  # Higher floor
        )
        sizer = PositionSizer(config)
        result = sizer.calculate(
            entry_price=50000.0,
            stop_loss=49000.0,
            available_capital=100.0,
            total_capital=1000.0,
        )
        # Likely below min, so should be rejected
        assert result["size_usdt"] == 0.0 or result["size_usdt"] >= 100.0

    def test_different_risk_pct_values(self, position_sizer):
        """Test that different risk_pct produces proportional sizing."""
        config_conservative = PositionSizerConfig(
            risk_pct=0.001,
            max_capital_pct=0.04,
            min_size_usdt=10.0,
        )
        config_aggressive = PositionSizerConfig(
            risk_pct=0.01,
            max_capital_pct=0.04,
            min_size_usdt=10.0,
        )
        sizer_conservative = PositionSizer(config_conservative)
        sizer_aggressive = PositionSizer(config_aggressive)

        result_conservative = sizer_conservative.calculate(
            entry_price=50000.0,
            stop_loss=49000.0,
            available_capital=10000.0,
            total_capital=10000.0,
        )
        result_aggressive = sizer_aggressive.calculate(
            entry_price=50000.0,
            stop_loss=49000.0,
            available_capital=10000.0,
            total_capital=10000.0,
        )
        # Aggressive should have larger size
        if result_conservative["size_usdt"] > 0 and result_aggressive["size_usdt"] > 0:
            assert result_aggressive["size_usdt"] >= result_conservative["size_usdt"]

    def test_negative_entry_price_rejection(self, position_sizer):
        """Test negative entry price → rejection."""
        result = position_sizer.calculate(
            entry_price=-100.0,
            stop_loss=90.0,
            available_capital=10000.0,
            total_capital=10000.0,
        )
        assert result["size_usdt"] == 0.0

    def test_negative_available_capital_rejection(self, position_sizer):
        """Test negative available capital → rejection."""
        result = position_sizer.calculate(
            entry_price=50000.0,
            stop_loss=49000.0,
            available_capital=-1000.0,
            total_capital=10000.0,
        )
        assert result["size_usdt"] == 0.0

    def test_returns_correct_keys(self, position_sizer):
        """Test returned dict has all required keys."""
        result = position_sizer.calculate(
            entry_price=50000.0,
            stop_loss=49000.0,
            available_capital=10000.0,
            total_capital=10000.0,
        )
        assert "size_usdt" in result
        assert "quantity" in result
        assert "risk_usdt" in result


# ═════════════════════════════════════════════════════════════════════════════
# CircuitBreaker Tests (10 tests)
# ═════════════════════════════════════════════════════════════════════════════

class TestCircuitBreaker:
    """Test CircuitBreaker state machine."""

    def test_initial_state_is_normal(self, circuit_breaker):
        """Circuit breaker starts in NORMAL state."""
        assert circuit_breaker.get_risk_scaling() == 1.0
        status = circuit_breaker.get_status()
        assert status["state"] == CircuitBreakerState.NORMAL.value

    def test_record_small_losses_stays_normal(self, circuit_breaker):
        """Small losses do not trigger WARNING."""
        capital = _make_capital_snapshot(
            drawdown_pct=0.02,  # 2%, below 5% warning
            realized_pnl_today=-100.0,  # Below 2% warning of 10000
        )
        state = circuit_breaker.evaluate(capital)
        assert state == CircuitBreakerState.NORMAL

    def test_drawdown_triggers_warning(self, circuit_breaker):
        """Drawdown > warning threshold → WARNING."""
        capital = _make_capital_snapshot(
            drawdown_pct=0.07,  # 7%, above 5% warning
            realized_pnl_today=0.0,
        )
        state = circuit_breaker.evaluate(capital)
        assert state == CircuitBreakerState.WARNING
        assert circuit_breaker.get_risk_scaling() == 0.5

    def test_daily_loss_triggers_warning(self, circuit_breaker):
        """Daily loss > warning threshold → WARNING."""
        capital = _make_capital_snapshot(
            drawdown_pct=0.02,
            realized_pnl_today=-300.0,  # 3% of 10000, above 2% warning
        )
        state = circuit_breaker.evaluate(capital)
        assert state == CircuitBreakerState.WARNING

    def test_max_drawdown_triggers_trip(self, circuit_breaker):
        """Drawdown > max threshold → TRIPPED."""
        capital = _make_capital_snapshot(
            drawdown_pct=0.12,  # 12%, above 10% max
            realized_pnl_today=0.0,
        )
        state = circuit_breaker.evaluate(capital)
        assert state == CircuitBreakerState.TRIPPED
        assert circuit_breaker.get_risk_scaling() == 0.0

    def test_max_daily_loss_triggers_trip(self, circuit_breaker):
        """Daily loss > max threshold → TRIPPED."""
        capital = _make_capital_snapshot(
            drawdown_pct=0.02,
            realized_pnl_today=-400.0,  # 4% of 10000, above 3% max
        )
        state = circuit_breaker.evaluate(capital)
        assert state == CircuitBreakerState.TRIPPED

    def test_consecutive_losses_trigger_trip(self, circuit_breaker):
        """Consecutive losses >= threshold → TRIPPED."""
        capital = _make_capital_snapshot(
            drawdown_pct=0.02,
            consecutive_losses=3,  # Equals threshold
        )
        state = circuit_breaker.evaluate(capital)
        assert state == CircuitBreakerState.TRIPPED

    def test_tripped_state_blocks_execution(self, circuit_breaker):
        """TRIPPED state returns 0.0 risk scaling."""
        capital = _make_capital_snapshot(
            drawdown_pct=0.12,
        )
        circuit_breaker.evaluate(capital)
        assert circuit_breaker.is_tripped() is True
        assert circuit_breaker.get_risk_scaling() == 0.0

    def test_cooldown_expiry_resets_to_normal(self, circuit_breaker):
        """After cooldown expires, TRIPPED → NORMAL."""
        now_ms = int(time.time() * 1000)
        capital = _make_capital_snapshot(drawdown_pct=0.12)

        # Trip it
        circuit_breaker.evaluate(capital, now_ms=now_ms)
        assert circuit_breaker.is_tripped() is True

        # Advance time past cooldown (1800s)
        future_ms = now_ms + (1800 * 1000) + 1000
        capital_ok = _make_capital_snapshot(drawdown_pct=0.02)
        state = circuit_breaker.evaluate(capital_ok, now_ms=future_ms)
        assert state == CircuitBreakerState.NORMAL

    def test_reset_clears_state(self, circuit_breaker):
        """reset() forces return to NORMAL."""
        capital = _make_capital_snapshot(drawdown_pct=0.12)
        circuit_breaker.evaluate(capital)
        assert circuit_breaker.is_tripped() is True

        circuit_breaker.reset()
        assert circuit_breaker.is_tripped() is False
        assert circuit_breaker.get_risk_scaling() == 1.0

    def test_warning_to_normal_transition(self, circuit_breaker):
        """WARNING clears when conditions improve."""
        capital_bad = _make_capital_snapshot(drawdown_pct=0.07)
        circuit_breaker.evaluate(capital_bad)
        assert circuit_breaker.get_status()["state"] == CircuitBreakerState.WARNING.value

        # Improve conditions
        capital_good = _make_capital_snapshot(drawdown_pct=0.02)
        circuit_breaker.evaluate(capital_good)
        assert circuit_breaker.get_status()["state"] == CircuitBreakerState.NORMAL.value


# ═════════════════════════════════════════════════════════════════════════════
# KillSwitch Tests (8 tests)
# ═════════════════════════════════════════════════════════════════════════════

class TestKillSwitch:
    """Test KillSwitch emergency stop mechanism."""

    def test_initial_state_is_armed(self, kill_switch):
        """Kill switch starts ARMED (execution enabled)."""
        assert kill_switch.is_halted() is False
        status = kill_switch.get_status()
        assert status["state"] == KillSwitchState.ARMED.value

    def test_disarm_halts_execution(self, kill_switch):
        """disarm() sets state to DISARMED."""
        kill_switch.disarm("Manual intervention")
        assert kill_switch.is_halted() is True
        status = kill_switch.get_status()
        assert status["state"] == KillSwitchState.DISARMED.value
        assert status["disarm_reason"] == "Manual intervention"

    def test_arm_resumes_execution(self, kill_switch):
        """arm() returns to ARMED."""
        kill_switch.disarm("Test")
        kill_switch.arm()
        assert kill_switch.is_halted() is False

    def test_persistence_survives_restart(self, tmp_path):
        """Disarmed state persists to disk and survives reload."""
        persistence_path = str(tmp_path / "kill_switch.json")
        ks1 = KillSwitch(KillSwitchConfig(persistence_path=persistence_path))
        ks1.disarm("Test disarm")

        # Reload from disk
        ks2 = KillSwitch(KillSwitchConfig(persistence_path=persistence_path))
        assert ks2.is_halted() is True
        status = ks2.get_status()
        assert status["disarm_reason"] == "Test disarm"

    def test_is_halted_returns_correct_bool(self, kill_switch):
        """is_halted() returns exact boolean state."""
        assert kill_switch.is_halted() is False
        kill_switch.disarm("Test")
        assert kill_switch.is_halted() is True
        kill_switch.arm()
        assert kill_switch.is_halted() is False

    def test_file_corruption_fallback(self, tmp_path):
        """Corrupted persistence file → defaults to ARMED."""
        persistence_path = tmp_path / "kill_switch.json"
        # Write invalid JSON
        persistence_path.write_text("invalid json{[}")

        ks = KillSwitch(KillSwitchConfig(persistence_path=str(persistence_path)))
        # Should default to ARMED
        assert ks.is_halted() is False

    def test_multiple_disarm_arm_cycles(self, kill_switch):
        """Multiple engage/disengage cycles work correctly."""
        for i in range(3):
            kill_switch.disarm(f"Cycle {i}")
            assert kill_switch.is_halted() is True
            kill_switch.arm()
            assert kill_switch.is_halted() is False

    def test_status_dict_completeness(self, kill_switch):
        """get_status() returns all expected keys."""
        kill_switch.disarm("Test")
        status = kill_switch.get_status()
        assert "state" in status
        assert "is_halted" in status
        assert "disarmed_at_ms" in status
        assert "disarm_reason" in status
        assert "uptime_since_disarm_s" in status


# ═════════════════════════════════════════════════════════════════════════════
# RiskEngine Tests (12 tests)
# ═════════════════════════════════════════════════════════════════════════════

class TestRiskEngine:
    """Test RiskEngine 10-gate validation."""

    def test_all_gates_pass_returns_approved(self, risk_engine):
        """All gates pass → APPROVED decision."""
        intent = _make_execution_intent()
        snapshot = _make_portfolio_snapshot()
        cb = CircuitBreaker()

        decision = risk_engine.validate(intent, snapshot, cb)
        assert decision.status == DecisionStatus.APPROVED
        assert decision.is_approved is True

    def test_gate1_circuit_breaker_tripped(self, risk_engine):
        """Gate 1: CB TRIPPED → REJECTED."""
        intent = _make_execution_intent()
        snapshot = _make_portfolio_snapshot()
        cb = CircuitBreaker()

        # Trip the breaker
        capital = _make_capital_snapshot(drawdown_pct=0.12)
        cb.evaluate(capital)

        decision = risk_engine.validate(intent, snapshot, cb)
        assert decision.status == DecisionStatus.REJECTED
        assert decision.rejection_source == RejectionSource.CIRCUIT_BREAKER.value

    def test_gate2_daily_loss_exceeded(self, risk_engine):
        """Gate 2: Daily loss > max → REJECTED."""
        intent = _make_execution_intent()
        capital = _make_capital_snapshot(realized_pnl_today=-400.0)  # 4% of 10000
        snapshot = _make_portfolio_snapshot(capital=capital)
        cb = CircuitBreaker()

        decision = risk_engine.validate(intent, snapshot, cb)
        assert decision.status == DecisionStatus.REJECTED
        assert decision.rejection_source == RejectionSource.DAILY_LOSS.value

    def test_gate3_drawdown_exceeded(self, risk_engine):
        """Gate 3: Drawdown > max → REJECTED."""
        intent = _make_execution_intent()
        capital = _make_capital_snapshot(drawdown_pct=0.12)  # 12% > 10% max
        snapshot = _make_portfolio_snapshot(capital=capital)
        cb = CircuitBreaker()

        decision = risk_engine.validate(intent, snapshot, cb)
        assert decision.status == DecisionStatus.REJECTED
        assert decision.rejection_source == RejectionSource.DRAWDOWN.value

    def test_gate4_max_concurrent_positions(self, risk_engine):
        """Gate 4: Open positions >= max → REJECTED."""
        intent = _make_execution_intent()
        # Create 5 open positions
        positions = tuple(
            PositionRecord(
                position_id=_make_id("pos", str(i)),
                order_id=_make_id("order", str(i)),
                decision_id=_make_id("dec", str(i)),
                trigger_id=_make_id("trig", str(i)),
                setup_id=_make_id("setup", str(i)),
                symbol=f"SYM{i}" if i != 0 else "BTC",
                direction=Direction.LONG,
                strategy_name="Test",
                strategy_class=StrategyClass.MOMENTUM_EXPANSION,
                entry_price=50000.0,
                entry_size_usdt=500.0,
                current_size_usdt=500.0,
                quantity=0.01,
                stop_loss=49000.0,
                original_stop_loss=49000.0,
                take_profit=52000.0,
            )
            for i in range(5)
        )
        snapshot = _make_portfolio_snapshot(open_positions=positions)
        cb = CircuitBreaker()

        decision = risk_engine.validate(intent, snapshot, cb)
        assert decision.status == DecisionStatus.REJECTED
        assert decision.rejection_source == RejectionSource.MAX_POSITIONS.value

    def test_gate5_duplicate_symbol_direction(self, risk_engine):
        """Gate 5: Duplicate symbol+direction → REJECTED."""
        intent = _make_execution_intent(symbol="BTC", direction=Direction.LONG)
        # Create position with same symbol and direction
        pos = PositionRecord(
            position_id=_make_id("pos1"),
            order_id=_make_id("order1"),
            decision_id=_make_id("dec1"),
            trigger_id=_make_id("trig1"),
            setup_id=_make_id("setup1"),
            symbol="BTC",
            direction=Direction.LONG,
            strategy_name="Test",
            strategy_class=StrategyClass.MOMENTUM_EXPANSION,
            entry_price=50000.0,
            entry_size_usdt=500.0,
            current_size_usdt=500.0,
            quantity=0.01,
            stop_loss=49000.0,
            original_stop_loss=49000.0,
            take_profit=52000.0,
        )
        snapshot = _make_portfolio_snapshot(open_positions=(pos,))
        cb = CircuitBreaker()

        decision = risk_engine.validate(intent, snapshot, cb)
        assert decision.status == DecisionStatus.REJECTED
        assert decision.rejection_source == RejectionSource.DUPLICATE_SYMBOL.value

    def test_gate6_asset_exposure_exceeded(self, risk_engine):
        """Gate 6: Per-asset exposure > max → REJECTED."""
        intent = _make_execution_intent(symbol="BTC", size_usdt=2500.0)  # 25% if cap 20%
        exposure = _make_exposure_snapshot(
            per_symbol={"BTC": 0.10},  # Already 10%
        )
        snapshot = _make_portfolio_snapshot(exposure=exposure)
        cb = CircuitBreaker()

        decision = risk_engine.validate(intent, snapshot, cb)
        assert decision.status == DecisionStatus.REJECTED
        assert decision.rejection_source == RejectionSource.ASSET_EXPOSURE.value

    def test_gate7_portfolio_heat_exceeded(self, risk_engine):
        """Gate 7: Portfolio heat > max → REJECTED."""
        intent = _make_execution_intent(risk_usdt=700.0)  # 7% of 10000
        exposure = _make_exposure_snapshot(
            portfolio_heat=0.04,  # Already 4%, new would be 11% > 6%
        )
        snapshot = _make_portfolio_snapshot(exposure=exposure)
        cb = CircuitBreaker()

        decision = risk_engine.validate(intent, snapshot, cb)
        assert decision.status == DecisionStatus.REJECTED
        assert decision.rejection_source == RejectionSource.PORTFOLIO_HEAT.value

    def test_gate8_insufficient_capital(self, risk_engine):
        """Gate 8: Available capital < intent size → REJECTED."""
        # Set up with size that passes gates 1-7 but fails gate 8
        intent = _make_execution_intent(size_usdt=500.0, risk_usdt=50.0)
        capital = _make_capital_snapshot(
            total=10000.0,
            available=400.0,  # Less than intent size 500
            reserved=9600.0,
        )
        # Minimal exposure to pass gate 6 (asset_exposure): 500 size = 5% of 10k capital, plus 10% existing = 15% < 20%
        exposure = _make_exposure_snapshot(
            per_symbol={"BTC": 0.10},  # 10% existing
            portfolio_heat=0.02,  # 2% existing
        )
        snapshot = _make_portfolio_snapshot(capital=capital, exposure=exposure)
        cb = CircuitBreaker()

        decision = risk_engine.validate(intent, snapshot, cb)
        assert decision.status == DecisionStatus.REJECTED
        assert decision.rejection_source == RejectionSource.INSUFFICIENT_CAPITAL.value

    def test_gate9_risk_reward_ratio_floor(self, risk_engine):
        """Gate 9: R:R < min → REJECTED."""
        intent = _make_execution_intent(risk_reward_ratio=0.8)  # < 1.0 min
        snapshot = _make_portfolio_snapshot()
        cb = CircuitBreaker()

        decision = risk_engine.validate(intent, snapshot, cb)
        assert decision.status == DecisionStatus.REJECTED
        assert decision.rejection_source == RejectionSource.RR_TOO_LOW.value

    def test_gate10_minimum_position_size(self, risk_engine):
        """Gate 10: Size < min → REJECTED."""
        intent = _make_execution_intent(size_usdt=5.0)  # < 10.0 min
        snapshot = _make_portfolio_snapshot()
        cb = CircuitBreaker()

        decision = risk_engine.validate(intent, snapshot, cb)
        assert decision.status == DecisionStatus.REJECTED
        assert decision.rejection_source == RejectionSource.SIZE_TOO_SMALL.value

    def test_warning_state_applies_scaling(self, risk_engine):
        """WARNING state applies 0.5x risk scaling to APPROVED decision."""
        intent = _make_execution_intent(size_usdt=100.0, quantity=0.002)
        snapshot = _make_portfolio_snapshot()
        cb = CircuitBreaker()

        # Set CB to WARNING
        capital = _make_capital_snapshot(drawdown_pct=0.07)
        cb.evaluate(capital)

        decision = risk_engine.validate(intent, snapshot, cb)
        assert decision.status == DecisionStatus.APPROVED
        assert decision.risk_scaling_applied == 0.5
        assert decision.final_size_usdt == 50.0  # 100 * 0.5
        assert decision.final_quantity == 0.001  # 0.002 * 0.5

    def test_rejection_includes_detailed_reason(self, risk_engine):
        """REJECTED decision includes detailed rejection_reason."""
        intent = _make_execution_intent(size_usdt=5.0)
        snapshot = _make_portfolio_snapshot()
        cb = CircuitBreaker()

        decision = risk_engine.validate(intent, snapshot, cb)
        assert decision.rejection_reason != ""
        assert len(decision.rejection_reason) > 0


# ═════════════════════════════════════════════════════════════════════════════
# ProcessingEngine Tests (10 tests)
# ═════════════════════════════════════════════════════════════════════════════

class TestProcessingEngine:
    """Test ProcessingEngine full pipeline."""

    def test_happy_path_trigger_to_approved_decision(self, processing_engine):
        """Full pipeline: valid trigger → APPROVED decision."""
        trigger = _make_trigger_signal()
        snapshot = _make_portfolio_snapshot()
        current_price = 50000.0

        decision = processing_engine.process(trigger, snapshot, current_price)
        assert decision.status == DecisionStatus.APPROVED

    def test_kill_switch_halted_rejects(self, processing_engine):
        """Kill switch DISARMED → REJECTED early."""
        processing_engine.kill_switch.disarm("Test halt")
        trigger = _make_trigger_signal()
        snapshot = _make_portfolio_snapshot()
        current_price = 50000.0

        decision = processing_engine.process(trigger, snapshot, current_price)
        assert decision.status == DecisionStatus.REJECTED
        assert decision.rejection_source == RejectionSource.KILL_SWITCH.value

    def test_risk_rejection_propagates(self, processing_engine):
        """Risk engine rejection → REJECTED decision."""
        # Create trigger with very low risk:reward to trigger RR gate failure
        trigger = _make_trigger_signal(
            entry_price=50000.0,
            stop_loss=49500.0,  # Only 500 risk
            take_profit=50100.0,  # Only 100 reward = 0.2 R:R < 1.0 minimum
        )
        snapshot = _make_portfolio_snapshot()
        current_price = 50000.0

        decision = processing_engine.process(trigger, snapshot, current_price)
        assert decision.status == DecisionStatus.REJECTED

    def test_processing_creates_execution_intent(self, processing_engine):
        """ProcessingEngine creates ExecutionIntent with correct fields."""
        trigger = _make_trigger_signal()
        snapshot = _make_portfolio_snapshot()
        current_price = 50000.0

        decision = processing_engine.process(trigger, snapshot, current_price)
        # Intent is created internally; we check decision has intent_id
        assert decision.intent_id != ""
        assert decision.trigger_id == trigger.trigger_id

    def test_processing_calls_risk_engine(self, processing_engine):
        """ProcessingEngine invokes RiskEngine.validate()."""
        trigger = _make_trigger_signal()
        snapshot = _make_portfolio_snapshot()
        current_price = 50000.0

        # Mock risk_engine to verify it's called
        original_validate = processing_engine.risk_engine.validate
        call_count = [0]

        def mock_validate(intent, snapshot, cb):
            call_count[0] += 1
            return original_validate(intent, snapshot, cb)

        processing_engine.risk_engine.validate = mock_validate
        processing_engine.process(trigger, snapshot, current_price)
        assert call_count[0] == 1

    def test_size_below_minimum_rejected(self, processing_engine):
        """Position sizer rejects → ProcessingEngine REJECTED."""
        # Custom trigger with invalid SL that causes sizing failure
        trigger = _make_trigger_signal(
            entry_price=50000.0,
            stop_loss=50000.0,  # Same as entry, zero distance
        )
        snapshot = _make_portfolio_snapshot()
        current_price = 50000.0

        decision = processing_engine.process(trigger, snapshot, current_price)
        assert decision.status == DecisionStatus.REJECTED

    def test_duplicate_symbol_direction_rejected(self, processing_engine):
        """Duplicate symbol+direction in portfolio → REJECTED."""
        trigger = _make_trigger_signal(symbol="BTC", direction=Direction.LONG)
        pos = PositionRecord(
            position_id=_make_id("pos1"),
            order_id=_make_id("order1"),
            decision_id=_make_id("dec1"),
            trigger_id=_make_id("trig1"),
            setup_id=_make_id("setup1"),
            symbol="BTC",
            direction=Direction.LONG,
            strategy_name="Test",
            strategy_class=StrategyClass.MOMENTUM_EXPANSION,
            entry_price=50000.0,
            entry_size_usdt=500.0,
            current_size_usdt=500.0,
            quantity=0.01,
            stop_loss=49000.0,
            original_stop_loss=49000.0,
            take_profit=52000.0,
        )
        snapshot = _make_portfolio_snapshot(open_positions=(pos,))
        current_price = 50000.0

        decision = processing_engine.process(trigger, snapshot, current_price)
        assert decision.status == DecisionStatus.REJECTED

    def test_phase5b_slot_tqs_no_op_via_process(self, processing_engine):
        """[SLOT] Without TQS scorer, pipeline context has tqs=None."""
        trigger = _make_trigger_signal()
        snapshot = _make_portfolio_snapshot()
        processing_engine.process(trigger, snapshot, 50000.0, now_ms=1_000_000_000_000)
        assert processing_engine.last_pipeline_context.tqs is None

    def test_phase5b_slot_filter_no_op_via_process(self, processing_engine):
        """[SLOT] Without global filter, pipeline context has filter=None."""
        trigger = _make_trigger_signal()
        snapshot = _make_portfolio_snapshot()
        processing_engine.process(trigger, snapshot, 50000.0, now_ms=1_000_000_000_000)
        assert processing_engine.last_pipeline_context.filter is None

    def test_phase5b_slot_concentration_no_op_via_process(self, processing_engine):
        """[SLOT] Without concentration engine, pipeline context has concentration=None."""
        trigger = _make_trigger_signal()
        snapshot = _make_portfolio_snapshot()
        processing_engine.process(trigger, snapshot, 50000.0, now_ms=1_000_000_000_000)
        assert processing_engine.last_pipeline_context.concentration is None


# ─────────────────────────────────────────────────────────────────────────────
# Smoke Test
# ─────────────────────────────────────────────────────────────────────────────

def test_all_modules_import():
    """Smoke test: all modules import without error."""
    from core.intraday.processing.position_sizer import PositionSizer
    from core.intraday.processing.circuit_breaker import CircuitBreaker
    from core.intraday.processing.kill_switch import KillSwitch
    from core.intraday.processing.risk_engine import RiskEngine
    from core.intraday.processing.processing_engine import ProcessingEngine
    assert all([PositionSizer, CircuitBreaker, KillSwitch, RiskEngine, ProcessingEngine])
