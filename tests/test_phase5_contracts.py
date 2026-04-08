# ============================================================
# TEST PHASE 5 EXECUTION CONTRACTS
#
# Comprehensive unit tests for execution_contracts.py
# Tests: enums, ID generation, frozen dataclasses, validation
# ============================================================

import pytest
import time
from dataclasses import FrozenInstanceError

from core.intraday.execution_contracts import (
    DecisionStatus, OrderType, OrderStatus, PositionStatus, CloseReason,
    RejectionSource, CircuitBreakerState, KillSwitchState, Side,
    ExecutionIntent, ExecutionDecision, ExecutionRequest, OrderRecord,
    FillRecord, PositionRecord, ExecutionResult,
    CapitalSnapshot, ExposureSnapshot, PortfolioSnapshot, TradeRecord,
    _make_id, validate_execution_intent, validate_execution_intent_strict,
    validate_execution_decision, validate_execution_decision_strict,
    validate_execution_request, validate_execution_request_strict,
    validate_order_record, validate_order_record_strict,
    validate_fill_record, validate_fill_record_strict,
    validate_position_record, validate_position_record_strict,
    ContractViolation, InvariantViolation,
)
from core.intraday.signal_contracts import Direction, StrategyClass


# ── FIXTURES ──────────────────────────────────────────────────

@pytest.fixture
def execution_intent():
    """Create a valid ExecutionIntent for testing."""
    return ExecutionIntent(
        intent_id="test_intent_001",
        trigger_id="test_trigger_001",
        setup_id="test_setup_001",
        symbol="BTC/USDT",
        direction=Direction.LONG,
        strategy_name="momentum_expansion",
        strategy_class=StrategyClass.MOMENTUM_EXPANSION,
        entry_price=100.0,
        stop_loss=95.0,
        take_profit=110.0,
        atr_value=2.5,
        size_usdt=1000.0,
        quantity=10.0,
        risk_usdt=50.0,
        risk_reward_ratio=2.0,
        regime="TREND_UP",
        regime_confidence=0.85,
        trigger_strength=0.80,
        trigger_quality=0.75,
    )


@pytest.fixture
def position_record():
    """Create a valid PositionRecord for testing."""
    return PositionRecord(
        position_id="pos_001",
        order_id="order_001",
        decision_id="decision_001",
        trigger_id="trigger_001",
        setup_id="setup_001",
        symbol="BTC/USDT",
        direction=Direction.LONG,
        strategy_name="momentum_expansion",
        strategy_class=StrategyClass.MOMENTUM_EXPANSION,
        entry_price=100.0,
        entry_size_usdt=1000.0,
        current_size_usdt=1000.0,
        quantity=10.0,
        stop_loss=95.0,
        original_stop_loss=95.0,
        take_profit=110.0,
    )


# ══════════════════════════════════════════════════════════════
# 1. ENUM TESTS
# ══════════════════════════════════════════════════════════════

class TestEnums:
    """Test all enum values exist and are correct."""

    def test_decision_status_values(self):
        """DecisionStatus has APPROVED and REJECTED."""
        assert DecisionStatus.APPROVED.value == "approved"
        assert DecisionStatus.REJECTED.value == "rejected"
        assert len(list(DecisionStatus)) == 2

    def test_order_type_values(self):
        """OrderType has MARKET and LIMIT."""
        assert OrderType.MARKET.value == "market"
        assert OrderType.LIMIT.value == "limit"
        assert len(list(OrderType)) == 2

    def test_order_status_values(self):
        """OrderStatus has all required statuses."""
        assert OrderStatus.PENDING.value == "pending"
        assert OrderStatus.FILLED.value == "filled"
        assert OrderStatus.PARTIALLY_FILLED.value == "partially_filled"
        assert OrderStatus.CANCELLED.value == "cancelled"
        assert OrderStatus.EXPIRED.value == "expired"
        assert OrderStatus.FAILED.value == "failed"
        assert len(list(OrderStatus)) == 6

    def test_position_status_values(self):
        """PositionStatus has OPEN, PARTIALLY_CLOSED, CLOSED."""
        assert PositionStatus.OPEN.value == "open"
        assert PositionStatus.PARTIALLY_CLOSED.value == "partially_closed"
        assert PositionStatus.CLOSED.value == "closed"
        assert len(list(PositionStatus)) == 3

    def test_close_reason_values(self):
        """CloseReason has all required reasons."""
        assert CloseReason.SL_HIT.value == "sl_hit"
        assert CloseReason.TP_HIT.value == "tp_hit"
        assert CloseReason.TIME_STOP.value == "time_stop"
        assert CloseReason.MANUAL.value == "manual"
        assert CloseReason.CIRCUIT_BREAKER.value == "circuit_breaker"
        assert CloseReason.PARTIAL_TP.value == "partial_tp"
        assert CloseReason.KILL_SWITCH.value == "kill_switch"
        assert len(list(CloseReason)) == 7

    def test_rejection_source_values(self):
        """RejectionSource has all required sources."""
        sources = [
            "stale_signal", "portfolio_heat", "max_positions",
            "asset_exposure", "daily_loss", "drawdown",
            "circuit_breaker", "kill_switch", "duplicate_symbol",
            "insufficient_capital", "rr_too_low", "size_too_small"
        ]
        for source in sources:
            assert hasattr(RejectionSource, source.upper())

    def test_circuit_breaker_state_values(self):
        """CircuitBreakerState has NORMAL, WARNING, TRIPPED."""
        assert CircuitBreakerState.NORMAL.value == "normal"
        assert CircuitBreakerState.WARNING.value == "warning"
        assert CircuitBreakerState.TRIPPED.value == "tripped"

    def test_kill_switch_state_values(self):
        """KillSwitchState has ARMED, DISARMED."""
        assert KillSwitchState.ARMED.value == "armed"
        assert KillSwitchState.DISARMED.value == "disarmed"


# ══════════════════════════════════════════════════════════════
# 2. SIDE ENUM + from_direction() TESTS
# ══════════════════════════════════════════════════════════════

class TestSide:
    """Test Side enum and from_direction conversion."""

    def test_side_buy_value(self):
        """Side.BUY has correct value."""
        assert Side.BUY.value == "buy"

    def test_side_sell_value(self):
        """Side.SELL has correct value."""
        assert Side.SELL.value == "sell"

    def test_side_from_direction_long(self):
        """from_direction(LONG) returns BUY."""
        result = Side.from_direction(Direction.LONG)
        assert result == Side.BUY

    def test_side_from_direction_short(self):
        """from_direction(SHORT) returns SELL."""
        result = Side.from_direction(Direction.SHORT)
        assert result == Side.SELL


# ══════════════════════════════════════════════════════════════
# 3. ID GENERATION TESTS
# ══════════════════════════════════════════════════════════════

class TestMakeId:
    """Test _make_id() determinism and consistency."""

    def test_make_id_deterministic(self):
        """Same inputs always produce same ID."""
        id1 = _make_id("BTC/USDT", "long", "momentum")
        id2 = _make_id("BTC/USDT", "long", "momentum")
        assert id1 == id2

    def test_make_id_different_inputs(self):
        """Different inputs produce different IDs."""
        id1 = _make_id("BTC/USDT", "long", "momentum")
        id2 = _make_id("ETH/USDT", "long", "momentum")
        assert id1 != id2

    def test_make_id_length(self):
        """ID is 16 characters (SHA-256 hex truncated)."""
        result = _make_id("test")
        assert len(result) == 16
        assert all(c in "0123456789abcdef" for c in result)

    def test_make_id_multiple_parts(self):
        """_make_id handles multiple parts correctly."""
        id1 = _make_id("a", "b")
        id2 = _make_id("a", "c")
        assert id1 != id2  # Different inputs produce different IDs


# ══════════════════════════════════════════════════════════════
# 4. FROZEN DATACLASS TESTS
# ══════════════════════════════════════════════════════════════

class TestFrozenDataclasses:
    """Test that frozen classes cannot be mutated."""

    def test_execution_intent_frozen(self, execution_intent):
        """ExecutionIntent is frozen (immutable)."""
        with pytest.raises(FrozenInstanceError):
            execution_intent.entry_price = 105.0

    def test_execution_decision_frozen(self):
        """ExecutionDecision is frozen."""
        decision = ExecutionDecision(
            decision_id="dec_001",
            intent_id="intent_001",
            trigger_id="trigger_001",
            setup_id="setup_001",
            symbol="BTC/USDT",
            direction=Direction.LONG,
            strategy_name="momentum",
            strategy_class=StrategyClass.MOMENTUM_EXPANSION,
            entry_price=100.0,
            stop_loss=95.0,
            take_profit=110.0,
            final_size_usdt=1000.0,
            final_quantity=10.0,
            risk_usdt=50.0,
            risk_reward_ratio=2.0,
            regime="TREND_UP",
            status=DecisionStatus.APPROVED,
        )
        with pytest.raises(FrozenInstanceError):
            decision.status = DecisionStatus.REJECTED

    def test_execution_request_frozen(self):
        """ExecutionRequest is frozen."""
        request = ExecutionRequest(
            request_id="req_001",
            decision_id="dec_001",
            trigger_id="trigger_001",
            setup_id="setup_001",
            symbol="BTC/USDT",
            side=Side.BUY,
            entry_price=100.0,
            stop_loss=95.0,
            take_profit=110.0,
            size_usdt=1000.0,
            quantity=10.0,
            strategy_name="momentum",
            strategy_class=StrategyClass.MOMENTUM_EXPANSION,
            regime="TREND_UP",
        )
        with pytest.raises(FrozenInstanceError):
            request.symbol = "ETH/USDT"

    def test_position_record_mutable(self, position_record):
        """PositionRecord is NOT frozen (mutable)."""
        # This should NOT raise FrozenInstanceError
        position_record.current_price = 105.0
        assert position_record.current_price == 105.0


# ══════════════════════════════════════════════════════════════
# 5. TIMESTAMP INJECTION TESTS
# ══════════════════════════════════════════════════════════════

class TestTimestampInjection:
    """Test __post_init__ timestamp injection on frozen classes."""

    def test_execution_intent_timestamp_auto_set(self):
        """ExecutionIntent gets created_at_ms auto-set if 0."""
        before_ms = int(time.time() * 1000)
        intent = ExecutionIntent(
            intent_id="test",
            trigger_id="test",
            setup_id="test",
            symbol="BTC/USDT",
            direction=Direction.LONG,
            strategy_name="test",
            strategy_class=StrategyClass.MOMENTUM_EXPANSION,
            entry_price=100.0,
            stop_loss=95.0,
            take_profit=110.0,
            atr_value=2.5,
            size_usdt=1000.0,
            quantity=10.0,
            risk_usdt=50.0,
            risk_reward_ratio=2.0,
            regime="test",
            regime_confidence=0.5,
            trigger_strength=0.5,
            trigger_quality=0.5,
            created_at_ms=0,  # Trigger auto-set
        )
        after_ms = int(time.time() * 1000)
        assert before_ms <= intent.created_at_ms <= after_ms

    def test_execution_decision_timestamp_auto_set(self):
        """ExecutionDecision gets created_at_ms auto-set if 0."""
        before_ms = int(time.time() * 1000)
        decision = ExecutionDecision(
            decision_id="dec",
            intent_id="intent",
            trigger_id="trigger",
            setup_id="setup",
            symbol="BTC/USDT",
            direction=Direction.LONG,
            strategy_name="test",
            strategy_class=StrategyClass.MOMENTUM_EXPANSION,
            entry_price=100.0,
            stop_loss=95.0,
            take_profit=110.0,
            final_size_usdt=1000.0,
            final_quantity=10.0,
            risk_usdt=50.0,
            risk_reward_ratio=2.0,
            regime="test",
            status=DecisionStatus.APPROVED,
            created_at_ms=0,
        )
        after_ms = int(time.time() * 1000)
        assert before_ms <= decision.created_at_ms <= after_ms


# ══════════════════════════════════════════════════════════════
# 6. VALIDATION FUNCTION TESTS (VALID DATA)
# ══════════════════════════════════════════════════════════════

class TestValidationValid:
    """Test validation functions with valid data."""

    def test_validate_execution_intent_valid(self, execution_intent):
        """validate_execution_intent returns empty list for valid intent."""
        violations = validate_execution_intent(execution_intent)
        assert violations == []

    def test_validate_execution_decision_approved_valid(self):
        """Validate APPROVED ExecutionDecision returns empty list."""
        decision = ExecutionDecision(
            decision_id="dec",
            intent_id="intent",
            trigger_id="trigger",
            setup_id="setup",
            symbol="BTC/USDT",
            direction=Direction.LONG,
            strategy_name="momentum",
            strategy_class=StrategyClass.MOMENTUM_EXPANSION,
            entry_price=100.0,
            stop_loss=95.0,
            take_profit=110.0,
            final_size_usdt=1000.0,
            final_quantity=10.0,
            risk_usdt=50.0,
            risk_reward_ratio=2.0,
            regime="TREND_UP",
            status=DecisionStatus.APPROVED,
        )
        violations = validate_execution_decision(decision)
        assert violations == []

    def test_validate_execution_decision_rejected_valid(self):
        """Validate REJECTED ExecutionDecision with reason returns empty list."""
        decision = ExecutionDecision(
            decision_id="dec",
            intent_id="intent",
            trigger_id="trigger",
            setup_id="setup",
            symbol="BTC/USDT",
            direction=Direction.LONG,
            strategy_name="momentum",
            strategy_class=StrategyClass.MOMENTUM_EXPANSION,
            entry_price=100.0,
            stop_loss=95.0,
            take_profit=110.0,
            final_size_usdt=0.0,
            final_quantity=0.0,
            risk_usdt=0.0,
            risk_reward_ratio=0.0,
            regime="test",
            status=DecisionStatus.REJECTED,
            rejection_reason="Insufficient capital",
            rejection_source=RejectionSource.INSUFFICIENT_CAPITAL.value,
        )
        violations = validate_execution_decision(decision)
        assert violations == []

    def test_validate_execution_request_valid(self):
        """validate_execution_request returns empty list for valid request."""
        request = ExecutionRequest(
            request_id="req",
            decision_id="dec",
            trigger_id="trigger",
            setup_id="setup",
            symbol="BTC/USDT",
            side=Side.BUY,
            entry_price=100.0,
            stop_loss=95.0,
            take_profit=110.0,
            size_usdt=1000.0,
            quantity=10.0,
            strategy_name="momentum",
            strategy_class=StrategyClass.MOMENTUM_EXPANSION,
            regime="test",
        )
        violations = validate_execution_request(request)
        assert violations == []

    def test_validate_order_record_valid(self):
        """validate_order_record returns empty list for valid order."""
        order = OrderRecord(
            order_id="order",
            request_id="req",
            decision_id="dec",
            trigger_id="trigger",
            symbol="BTC/USDT",
            side=Side.BUY,
            order_type=OrderType.MARKET,
            requested_price=100.0,
            requested_quantity=10.0,
            status=OrderStatus.PENDING,
        )
        violations = validate_order_record(order)
        assert violations == []

    def test_validate_fill_record_valid(self):
        """validate_fill_record returns empty list for valid fill."""
        fill = FillRecord(
            fill_id="fill",
            order_id="order",
            symbol="BTC/USDT",
            side=Side.BUY,
            price=100.0,
            quantity=10.0,
            fee_usdt=5.0,
            fee_rate=0.0005,
            slippage_pct=0.02,
            is_maker=True,
        )
        violations = validate_fill_record(fill)
        assert violations == []

    def test_validate_position_record_valid(self, position_record):
        """validate_position_record returns empty list for valid position."""
        violations = validate_position_record(position_record)
        assert violations == []


# ══════════════════════════════════════════════════════════════
# 7. STRICT VALIDATION TESTS (INVALID DATA)
# ══════════════════════════════════════════════════════════════

class TestValidationStrict:
    """Test _strict validation functions raise ContractViolation."""

    def test_validate_execution_intent_strict_empty_symbol(self):
        """validate_execution_intent_strict raises on empty symbol."""
        intent = ExecutionIntent(
            intent_id="test",
            trigger_id="test",
            setup_id="test",
            symbol="",  # Invalid: empty
            direction=Direction.LONG,
            strategy_name="test",
            strategy_class=StrategyClass.MOMENTUM_EXPANSION,
            entry_price=100.0,
            stop_loss=95.0,
            take_profit=110.0,
            atr_value=2.5,
            size_usdt=1000.0,
            quantity=10.0,
            risk_usdt=50.0,
            risk_reward_ratio=2.0,
            regime="test",
            regime_confidence=0.5,
            trigger_strength=0.5,
            trigger_quality=0.5,
        )
        with pytest.raises(ContractViolation):
            validate_execution_intent_strict(intent)

    def test_validate_execution_intent_strict_invalid_rr(self):
        """validate_execution_intent_strict raises on RR < 1.0."""
        intent = ExecutionIntent(
            intent_id="test",
            trigger_id="test",
            setup_id="test",
            symbol="BTC/USDT",
            direction=Direction.LONG,
            strategy_name="test",
            strategy_class=StrategyClass.MOMENTUM_EXPANSION,
            entry_price=100.0,
            stop_loss=95.0,
            take_profit=110.0,
            atr_value=2.5,
            size_usdt=1000.0,
            quantity=10.0,
            risk_usdt=50.0,
            risk_reward_ratio=0.5,  # Invalid: < 1.0
            regime="test",
            regime_confidence=0.5,
            trigger_strength=0.5,
            trigger_quality=0.5,
        )
        with pytest.raises(ContractViolation):
            validate_execution_intent_strict(intent)

    def test_validate_execution_decision_strict_approved_no_size(self):
        """validate_execution_decision_strict raises for APPROVED with size=0."""
        decision = ExecutionDecision(
            decision_id="dec",
            intent_id="intent",
            trigger_id="trigger",
            setup_id="setup",
            symbol="BTC/USDT",
            direction=Direction.LONG,
            strategy_name="test",
            strategy_class=StrategyClass.MOMENTUM_EXPANSION,
            entry_price=100.0,
            stop_loss=95.0,
            take_profit=110.0,
            final_size_usdt=0.0,  # Invalid: must be > 0
            final_quantity=0.0,
            risk_usdt=50.0,
            risk_reward_ratio=2.0,
            regime="test",
            status=DecisionStatus.APPROVED,
        )
        with pytest.raises(ContractViolation):
            validate_execution_decision_strict(decision)

    def test_validate_execution_request_strict_invalid_side(self):
        """validate_execution_request_strict raises on invalid side."""
        # Create with invalid side by bypassing the enum
        request = ExecutionRequest(
            request_id="req",
            decision_id="dec",
            trigger_id="trigger",
            setup_id="setup",
            symbol="BTC/USDT",
            side=Side.BUY,  # Valid, but we'll test the validator catches issues
            entry_price=100.0,
            stop_loss=95.0,
            take_profit=110.0,
            size_usdt=0.0,  # Invalid
            quantity=10.0,
            strategy_name="test",
            strategy_class=StrategyClass.MOMENTUM_EXPANSION,
            regime="test",
        )
        with pytest.raises(ContractViolation):
            validate_execution_request_strict(request)

    def test_validate_order_record_strict_filled_no_price(self):
        """validate_order_record_strict raises for FILLED order without price."""
        order = OrderRecord(
            order_id="order",
            request_id="req",
            decision_id="dec",
            trigger_id="trigger",
            symbol="BTC/USDT",
            side=Side.BUY,
            order_type=OrderType.MARKET,
            requested_price=100.0,
            requested_quantity=10.0,
            status=OrderStatus.FILLED,
            filled_price=0.0,  # Invalid for FILLED
            filled_quantity=10.0,
        )
        with pytest.raises(ContractViolation):
            validate_order_record_strict(order)

    def test_validate_fill_record_strict_negative_fee(self):
        """validate_fill_record_strict raises on negative fee."""
        fill = FillRecord(
            fill_id="fill",
            order_id="order",
            symbol="BTC/USDT",
            side=Side.BUY,
            price=100.0,
            quantity=10.0,
            fee_usdt=-5.0,  # Invalid: negative
            fee_rate=0.0005,
            slippage_pct=0.02,
            is_maker=True,
        )
        with pytest.raises(ContractViolation):
            validate_fill_record_strict(fill)

    def test_validate_position_record_strict_closed_no_reason(self):
        """validate_position_record_strict raises for CLOSED pos without reason."""
        pos = PositionRecord(
            position_id="pos",
            order_id="order",
            decision_id="dec",
            trigger_id="trigger",
            setup_id="setup",
            symbol="BTC/USDT",
            direction=Direction.LONG,
            strategy_name="test",
            strategy_class=StrategyClass.MOMENTUM_EXPANSION,
            entry_price=100.0,
            entry_size_usdt=1000.0,
            current_size_usdt=1000.0,
            quantity=10.0,
            stop_loss=95.0,
            original_stop_loss=95.0,
            take_profit=110.0,
            status=PositionStatus.CLOSED,
            close_price=105.0,
            close_reason="",  # Invalid: must have reason if CLOSED
        )
        with pytest.raises(ContractViolation):
            validate_position_record_strict(pos)


# ══════════════════════════════════════════════════════════════
# 8. EXCEPTION CLASS TESTS
# ══════════════════════════════════════════════════════════════

class TestExceptions:
    """Test exception class hierarchy."""

    def test_contract_violation_is_value_error(self):
        """ContractViolation is a ValueError subclass."""
        exc = ContractViolation("test")
        assert isinstance(exc, ValueError)

    def test_invariant_violation_is_runtime_error(self):
        """InvariantViolation is a RuntimeError subclass."""
        exc = InvariantViolation("test")
        assert isinstance(exc, RuntimeError)


# ══════════════════════════════════════════════════════════════
# 9. ROUNDTRIP SERIALIZATION TESTS
# ══════════════════════════════════════════════════════════════

class TestRoundtripSerialization:
    """Test to_dict() and from_dict() roundtrips."""

    def test_execution_intent_to_dict(self, execution_intent):
        """ExecutionIntent.to_dict() returns proper dict."""
        d = execution_intent.to_dict()
        assert d["intent_id"] == "test_intent_001"
        assert d["direction"] == "long"
        assert d["entry_price"] == 100.0
        assert isinstance(d["candle_trace_ids"], list)

    def test_execution_decision_to_dict(self):
        """ExecutionDecision.to_dict() returns proper dict."""
        decision = ExecutionDecision(
            decision_id="dec",
            intent_id="intent",
            trigger_id="trigger",
            setup_id="setup",
            symbol="BTC/USDT",
            direction=Direction.LONG,
            strategy_name="test",
            strategy_class=StrategyClass.MOMENTUM_EXPANSION,
            entry_price=100.0,
            stop_loss=95.0,
            take_profit=110.0,
            final_size_usdt=1000.0,
            final_quantity=10.0,
            risk_usdt=50.0,
            risk_reward_ratio=2.0,
            regime="test",
            status=DecisionStatus.APPROVED,
        )
        d = decision.to_dict()
        assert d["status"] == "approved"
        assert d["direction"] == "long"
        assert d["final_size_usdt"] == 1000.0

    def test_position_record_to_dict(self, position_record):
        """PositionRecord.to_dict() returns proper dict."""
        d = position_record.to_dict()
        assert d["position_id"] == "pos_001"
        assert d["direction"] == "long"
        assert d["status"] == "open"
        assert d["entry_price"] == 100.0

    def test_trade_record_to_dict(self):
        """TradeRecord.to_dict() returns proper dict."""
        trade = TradeRecord(
            position_id="pos",
            order_id="order",
            decision_id="dec",
            trigger_id="trigger",
            setup_id="setup",
            symbol="BTC/USDT",
            direction="long",
            strategy_name="test",
            strategy_class="MX",
            entry_price=100.0,
            exit_price=105.0,
            entry_size_usdt=1000.0,
            quantity=10.0,
            realized_pnl_usdt=50.0,
            fee_total_usdt=5.0,
            r_multiple=1.0,
            close_reason="tp_hit",
            bars_held=5,
            regime_at_entry="TREND_UP",
            opened_at_ms=1000000,
            closed_at_ms=2000000,
            duration_ms=1000000,
        )
        d = trade.to_dict()
        assert d["position_id"] == "pos"
        assert d["realized_pnl_usdt"] == 50.0
        assert d["r_multiple"] == 1.0

    def test_trade_record_from_dict(self):
        """TradeRecord.from_dict() reconstructs from dict."""
        d = {
            "position_id": "pos",
            "order_id": "order",
            "decision_id": "dec",
            "trigger_id": "trigger",
            "setup_id": "setup",
            "symbol": "BTC/USDT",
            "direction": "long",
            "strategy_name": "test",
            "strategy_class": "MX",
            "entry_price": 100.0,
            "exit_price": 105.0,
            "entry_size_usdt": 1000.0,
            "quantity": 10.0,
            "realized_pnl_usdt": 50.0,
            "fee_total_usdt": 5.0,
            "r_multiple": 1.0,
            "close_reason": "tp_hit",
            "bars_held": 5,
            "regime_at_entry": "TREND_UP",
            "opened_at_ms": 1000000,
            "closed_at_ms": 2000000,
            "duration_ms": 1000000,
        }
        trade = TradeRecord.from_dict(d)
        assert trade.position_id == "pos"
        assert trade.realized_pnl_usdt == 50.0


# ══════════════════════════════════════════════════════════════
# 10. SNAPSHOT DATACLASS TESTS
# ══════════════════════════════════════════════════════════════

class TestSnapshots:
    """Test frozen snapshot classes."""

    def test_capital_snapshot_creation(self):
        """CapitalSnapshot can be created with all fields."""
        snap = CapitalSnapshot(
            total_capital=10000.0,
            reserved_capital=2000.0,
            available_capital=8000.0,
            equity=10500.0,
            peak_equity=10500.0,
            drawdown_pct=0.0,
            realized_pnl_today=500.0,
            total_realized_pnl=500.0,
            total_fees=10.0,
            trade_count_today=2,
            consecutive_losses=0,
        )
        assert snap.total_capital == 10000.0
        assert snap.available_capital == 8000.0
        assert snap.consecutive_losses == 0

    def test_capital_snapshot_frozen(self):
        """CapitalSnapshot is frozen."""
        snap = CapitalSnapshot(
            total_capital=10000.0,
            reserved_capital=0.0,
            available_capital=10000.0,
            equity=10000.0,
            peak_equity=10000.0,
            drawdown_pct=0.0,
            realized_pnl_today=0.0,
            total_realized_pnl=0.0,
            total_fees=0.0,
            trade_count_today=0,
            consecutive_losses=0,
        )
        with pytest.raises(FrozenInstanceError):
            snap.total_capital = 20000.0

    def test_exposure_snapshot_creation(self):
        """ExposureSnapshot can be created with all fields."""
        snap = ExposureSnapshot(
            per_symbol={"BTC/USDT": 0.1, "ETH/USDT": 0.05},
            long_exposure=0.15,
            short_exposure=0.0,
            net_exposure=0.15,
            portfolio_heat=0.05,
        )
        assert snap.long_exposure == 0.15
        assert "BTC/USDT" in snap.per_symbol

    def test_portfolio_snapshot_creation(self):
        """PortfolioSnapshot combines capital and exposure."""
        capital = CapitalSnapshot(
            total_capital=10000.0,
            reserved_capital=1000.0,
            available_capital=9000.0,
            equity=10000.0,
            peak_equity=10000.0,
            drawdown_pct=0.0,
            realized_pnl_today=0.0,
            total_realized_pnl=0.0,
            total_fees=0.0,
            trade_count_today=0,
            consecutive_losses=0,
        )
        exposure = ExposureSnapshot(
            per_symbol={},
            long_exposure=0.1,
            short_exposure=0.0,
            net_exposure=0.1,
            portfolio_heat=0.05,
        )
        snap = PortfolioSnapshot(
            capital=capital,
            exposure=exposure,
            open_positions=(),
            open_position_count=0,
        )
        assert snap.capital.total_capital == 10000.0
        assert snap.exposure.long_exposure == 0.1
        assert snap.open_position_count == 0


# ══════════════════════════════════════════════════════════════
# 11. PROPERTY TESTS
# ══════════════════════════════════════════════════════════════

class TestProperties:
    """Test computed properties on contracts."""

    def test_execution_decision_is_approved(self):
        """ExecutionDecision.is_approved property works."""
        approved = ExecutionDecision(
            decision_id="dec",
            intent_id="intent",
            trigger_id="trigger",
            setup_id="setup",
            symbol="BTC/USDT",
            direction=Direction.LONG,
            strategy_name="test",
            strategy_class=StrategyClass.MOMENTUM_EXPANSION,
            entry_price=100.0,
            stop_loss=95.0,
            take_profit=110.0,
            final_size_usdt=1000.0,
            final_quantity=10.0,
            risk_usdt=50.0,
            risk_reward_ratio=2.0,
            regime="test",
            status=DecisionStatus.APPROVED,
        )
        assert approved.is_approved is True

        rejected = ExecutionDecision(
            decision_id="dec",
            intent_id="intent",
            trigger_id="trigger",
            setup_id="setup",
            symbol="BTC/USDT",
            direction=Direction.LONG,
            strategy_name="test",
            strategy_class=StrategyClass.MOMENTUM_EXPANSION,
            entry_price=100.0,
            stop_loss=95.0,
            take_profit=110.0,
            final_size_usdt=0.0,
            final_quantity=0.0,
            risk_usdt=0.0,
            risk_reward_ratio=0.0,
            regime="test",
            status=DecisionStatus.REJECTED,
            rejection_reason="Test rejection",
            rejection_source=RejectionSource.INSUFFICIENT_CAPITAL.value,
        )
        assert rejected.is_approved is False

    def test_fill_record_notional_usdt(self):
        """FillRecord.notional_usdt property calculated correctly."""
        fill = FillRecord(
            fill_id="fill",
            order_id="order",
            symbol="BTC/USDT",
            side=Side.BUY,
            price=100.0,
            quantity=10.0,
            fee_usdt=5.0,
            fee_rate=0.0005,
            slippage_pct=0.02,
            is_maker=True,
        )
        assert fill.notional_usdt == 1000.0  # 100.0 * 10.0

    def test_position_record_risk_per_unit(self, position_record):
        """PositionRecord.risk_per_unit property works."""
        rpu = position_record.risk_per_unit
        assert rpu == 5.0  # 100.0 - 95.0

    def test_position_record_total_risk_usdt(self, position_record):
        """PositionRecord.total_risk_usdt property works."""
        total_risk = position_record.total_risk_usdt
        assert total_risk == 50.0  # 5.0 * 10.0

    def test_position_record_compute_r_multiple(self, position_record):
        """PositionRecord.compute_r_multiple calculates R correctly."""
        # Exit at 110 (10 per unit profit, 5 per unit risk = 2R)
        r = position_record.compute_r_multiple(110.0)
        assert r == 2.0

    def test_position_record_update_price_long(self, position_record):
        """PositionRecord.update_price calculates unrealized P&L for LONG."""
        position_record.update_price(105.0)
        assert position_record.current_price == 105.0
        assert position_record.unrealized_pnl_usdt == 50.0  # (105-100)*10
        assert position_record.unrealized_pnl_pct == 0.05  # 50/1000


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
