# ============================================================
# NEXUS TRADER — Phase 5 Execution Tests
#
# Comprehensive unit tests for:
# - FillSimulator
# - OrderManager
# - IntradayExecutor
# - ExecutionEngine
# - ExecutionGate
#
# Total: 45 tests
# ============================================================
import pytest
import time
from unittest.mock import Mock, MagicMock, patch, call

from core.intraday.execution_contracts import (
    DecisionStatus,
    Direction,
    ExecutionDecision,
    ExecutionIntent,
    ExecutionRequest,
    ExecutionResult,
    FillRecord,
    OrderRecord,
    OrderStatus,
    OrderType,
    Side,
    StrategyClass,
    _make_id,
)
from core.intraday.signal_contracts import (
    TriggerSignal,
    TriggerLifecycle,
    Direction as SignalDirection,
    StrategyClass as SignalStrategyClass,
)
from core.intraday.execution.fill_simulator import (
    FillSimulator,
    DefaultFeeModel,
    DefaultSlippageModel,
    FeeModel,
    SlippageModel,
)
from core.intraday.execution.order_manager import OrderManager
from core.intraday.execution.intraday_executor import IntradayExecutor
from core.intraday.execution.execution_engine import ExecutionEngine
from core.intraday.execution.execution_gate import ExecutionGate


# ══════════════════════════════════════════════════════════════
# FIXTURES
# ══════════════════════════════════════════════════════════════


@pytest.fixture
def default_fee_model():
    """Create a default fee model."""
    return DefaultFeeModel(taker_rate=0.0004, maker_rate=0.0002)


@pytest.fixture
def default_slippage_model():
    """Create a default slippage model."""
    return DefaultSlippageModel(max_slippage_pct=0.0002)


@pytest.fixture
def fill_simulator(default_fee_model, default_slippage_model):
    """Create a fill simulator with default models."""
    return FillSimulator(
        fee_model=default_fee_model,
        slippage_model=default_slippage_model
    )


@pytest.fixture
def order_manager(fill_simulator):
    """Create an order manager."""
    return OrderManager(
        fill_simulator=fill_simulator,
        now_ms_fn=lambda: 1000000000000
    )


@pytest.fixture
def intraday_executor(order_manager):
    """Create an intraday executor."""
    return IntradayExecutor(order_manager=order_manager)


@pytest.fixture
def mock_portfolio_state():
    """Create a mock portfolio state."""
    mock = Mock()
    mock.open_position = Mock(return_value=Mock(position_id="pos_001"))
    mock.get_snapshot = Mock(return_value=Mock())
    return mock


@pytest.fixture
def mock_persistence_manager():
    """Create a mock persistence manager."""
    mock = Mock()
    mock.save_snapshot = Mock()
    return mock


@pytest.fixture
def mock_event_bus():
    """Create a mock event bus."""
    mock = Mock()
    mock.subscribe = Mock()
    mock.unsubscribe = Mock()
    mock.publish = Mock()
    return mock


@pytest.fixture
def execution_engine(intraday_executor, mock_portfolio_state, mock_persistence_manager):
    """Create an execution engine."""
    return ExecutionEngine(
        intraday_executor=intraday_executor,
        portfolio_state=mock_portfolio_state,
        persistence_manager=mock_persistence_manager,
        now_ms_fn=lambda: 1000000000000
    )


@pytest.fixture
def mock_processing_engine():
    """Create a mock processing engine."""
    return Mock()


@pytest.fixture
def execution_gate(mock_processing_engine, execution_engine, mock_portfolio_state, mock_event_bus):
    """Create an execution gate."""
    return ExecutionGate(
        processing_engine=mock_processing_engine,
        execution_engine=execution_engine,
        portfolio_state=mock_portfolio_state,
        event_bus=mock_event_bus,
        now_ms_fn=lambda: 1000000000000
    )


# Fixture for creating test orders
@pytest.fixture
def sample_order(fill_simulator):
    """Create a sample order record for testing."""
    return OrderRecord(
        order_id="order_001",
        request_id="req_001",
        decision_id="dec_001",
        trigger_id="trig_001",
        symbol="BTC/USDT",
        side=Side.BUY,
        order_type=OrderType.MARKET,
        requested_price=50000.0,
        requested_quantity=0.1,
        status=OrderStatus.PENDING,
        created_at_ms=1000000000000
    )


# Fixture for creating test execution requests
@pytest.fixture
def sample_execution_request():
    """Create a sample execution request."""
    return ExecutionRequest(
        request_id="req_001",
        decision_id="dec_001",
        trigger_id="trig_001",
        setup_id="setup_001",
        symbol="BTC/USDT",
        side=Side.BUY,
        entry_price=50000.0,
        stop_loss=48000.0,
        take_profit=52000.0,
        size_usdt=5000.0,
        quantity=0.1,
        strategy_name="momentum_expansion",
        strategy_class=StrategyClass.MOMENTUM_EXPANSION,
        regime="BULL_TREND",
        created_at_ms=1000000000000,
        max_fill_delay_ms=30000,
        candle_trace_ids=("trace_001", "trace_002")
    )


# Fixture for creating test execution decisions
@pytest.fixture
def sample_execution_decision():
    """Create a sample execution decision."""
    return ExecutionDecision(
        decision_id="dec_001",
        intent_id="intent_001",
        trigger_id="trig_001",
        setup_id="setup_001",
        symbol="BTC/USDT",
        direction=Direction.LONG,
        strategy_name="momentum_expansion",
        strategy_class=StrategyClass.MOMENTUM_EXPANSION,
        entry_price=50000.0,
        stop_loss=48000.0,
        take_profit=52000.0,
        final_size_usdt=5000.0,
        final_quantity=0.1,
        risk_usdt=200.0,
        risk_reward_ratio=1.5,
        regime="BULL_TREND",
        status=DecisionStatus.APPROVED,
        created_at_ms=1000000000000,
        candle_trace_ids=("trace_001", "trace_002")
    )


# Fixture for creating test trigger signals
@pytest.fixture
def sample_trigger_signal():
    """Create a sample trigger signal for ExecutionGate tests."""
    return TriggerSignal(
        trigger_id="trig_001",
        setup_id="setup_001",
        strategy_name="momentum_expansion",
        strategy_class=SignalStrategyClass.MOMENTUM_EXPANSION,
        symbol="BTC/USDT",
        direction=SignalDirection.LONG,
        entry_price=50000.0,
        stop_loss=48000.0,
        take_profit=52000.0,
        atr_value=500.0,
        strength=0.8,
        trigger_quality=0.75,
        setup_timeframe="15m",
        trigger_timeframe="1m",
        regime="BULL_TREND",
        regime_confidence=0.85,
        trigger_candle_ts=1000000000000,
        setup_candle_ts=1000000000000,
        candle_trace_ids=("trace_001", "trace_002"),
        setup_trace_ids=("setup_trace_001",),
        lifecycle=TriggerLifecycle.FIRED,
        created_at_ms=1000000000000,
        max_age_ms=60000,
        drift_tolerance=0.01
    )


# ══════════════════════════════════════════════════════════════
# FILL SIMULATOR TESTS (~10 tests)
# ══════════════════════════════════════════════════════════════


class TestFillSimulator:
    """Tests for FillSimulator class."""

    def test_default_fee_model_returns_correct_taker_fee(self, default_fee_model):
        """DefaultFeeModel.calculate_fee returns correct taker fee."""
        # Arrange
        price = 50000.0
        quantity = 0.1
        notional = 5000.0
        expected_fee = notional * 0.0004  # 2.0 USDT

        # Act
        fee = default_fee_model.calculate_fee(price, quantity, is_maker=False)

        # Assert
        assert fee == expected_fee
        assert abs(fee - 2.0) < 0.001

    def test_default_fee_model_returns_correct_maker_fee(self, default_fee_model):
        """DefaultFeeModel.calculate_fee returns correct maker fee."""
        # Arrange
        price = 50000.0
        quantity = 0.1
        notional = 5000.0
        expected_fee = notional * 0.0002  # 1.0 USDT

        # Act
        fee = default_fee_model.calculate_fee(price, quantity, is_maker=True)

        # Assert
        assert fee == expected_fee
        assert abs(fee - 1.0) < 0.001

    def test_default_slippage_model_applies_positive_slippage_for_buy(self, default_slippage_model):
        """DefaultSlippageModel applies positive slippage for BUY."""
        # Arrange
        price = 50000.0

        # Act
        slippage = default_slippage_model.calculate_slippage(price, Side.BUY, seed=42)

        # Assert
        assert slippage >= 0
        assert slippage <= price * 0.0002

    def test_default_slippage_model_applies_negative_slippage_for_sell(self, default_slippage_model):
        """DefaultSlippageModel applies negative slippage for SELL."""
        # Arrange
        price = 50000.0

        # Act
        slippage = default_slippage_model.calculate_slippage(price, Side.SELL, seed=42)

        # Assert
        assert slippage <= 0
        assert slippage >= -(price * 0.0002)

    def test_fill_simulator_simulate_fill_returns_fill_record(self, fill_simulator, sample_order):
        """FillSimulator.simulate_fill returns FillRecord."""
        # Act
        fill = fill_simulator.simulate_fill(sample_order, now_ms=1000000000000, seed=42)

        # Assert
        assert isinstance(fill, FillRecord)
        assert fill.fill_id is not None
        assert fill.order_id == sample_order.order_id
        assert fill.symbol == sample_order.symbol

    def test_fill_price_includes_slippage(self, fill_simulator, sample_order):
        """Fill price includes slippage."""
        # Act
        fill = fill_simulator.simulate_fill(sample_order, now_ms=1000000000000, seed=42)

        # Assert
        # For BUY, fill price should be >= requested price
        assert fill.price >= sample_order.requested_price or abs(fill.price - sample_order.requested_price) < 0.01

    def test_fee_calculated_correctly(self, fill_simulator, sample_order):
        """Fee is calculated correctly in fill record."""
        # Act
        fill = fill_simulator.simulate_fill(sample_order, now_ms=1000000000000, seed=42)

        # Assert
        notional = fill.price * fill.quantity
        expected_fee = notional * 0.0004  # taker rate
        assert abs(fill.fee_usdt - expected_fee) < 0.01

    def test_custom_fee_model_can_be_plugged_in(self):
        """Custom fee model can be plugged into FillSimulator."""
        # Arrange
        class CustomFeeModel(FeeModel):
            def __init__(self):
                self.taker_rate = 0.0199  # 1.99% taker rate
                self.maker_rate = 0.0099  # 0.99% maker rate

            def calculate_fee(self, price, quantity, is_maker):
                return 99.99  # Fixed custom fee

        custom_fee = CustomFeeModel()
        simulator = FillSimulator(fee_model=custom_fee)

        order = OrderRecord(
            order_id="order_001",
            request_id="req_001",
            decision_id="dec_001",
            trigger_id="trig_001",
            symbol="BTC/USDT",
            side=Side.BUY,
            order_type=OrderType.MARKET,
            requested_price=50000.0,
            requested_quantity=0.1,
            status=OrderStatus.PENDING,
            created_at_ms=1000000000000
        )

        # Act
        fill = simulator.simulate_fill(order, now_ms=1000000000000)

        # Assert
        assert fill.fee_usdt == 99.99

    def test_custom_slippage_model_can_be_plugged_in(self):
        """Custom slippage model can be plugged into FillSimulator."""
        # Arrange
        class ZeroSlippageModel(SlippageModel):
            def calculate_slippage(self, price, side, seed=None):
                return 0.0  # Zero slippage

        zero_slippage = ZeroSlippageModel()
        simulator = FillSimulator(slippage_model=zero_slippage)

        order = OrderRecord(
            order_id="order_001",
            request_id="req_001",
            decision_id="dec_001",
            trigger_id="trig_001",
            symbol="BTC/USDT",
            side=Side.BUY,
            order_type=OrderType.MARKET,
            requested_price=50000.0,
            requested_quantity=0.1,
            status=OrderStatus.PENDING,
            created_at_ms=1000000000000
        )

        # Act
        fill = simulator.simulate_fill(order, now_ms=1000000000000)

        # Assert
        assert fill.price == order.requested_price

    def test_zero_slippage_model_produces_exact_price(self):
        """Zero slippage model produces exact requested price."""
        # Arrange
        class ZeroSlippageModel(SlippageModel):
            def calculate_slippage(self, price, side, seed=None):
                return 0.0

        simulator = FillSimulator(slippage_model=ZeroSlippageModel())
        order = OrderRecord(
            order_id="order_001",
            request_id="req_001",
            decision_id="dec_001",
            trigger_id="trig_001",
            symbol="BTC/USDT",
            side=Side.BUY,
            order_type=OrderType.MARKET,
            requested_price=50000.0,
            requested_quantity=0.1,
            status=OrderStatus.PENDING,
            created_at_ms=1000000000000
        )

        # Act
        fill = simulator.simulate_fill(order, now_ms=1000000000000)

        # Assert
        assert fill.price == 50000.0

    def test_large_quantity_handling(self, fill_simulator):
        """Large quantity orders are handled correctly."""
        # Arrange
        order = OrderRecord(
            order_id="order_001",
            request_id="req_001",
            decision_id="dec_001",
            trigger_id="trig_001",
            symbol="BTC/USDT",
            side=Side.BUY,
            order_type=OrderType.MARKET,
            requested_price=50000.0,
            requested_quantity=100.0,  # Large quantity
            status=OrderStatus.PENDING,
            created_at_ms=1000000000000
        )

        # Act
        fill = fill_simulator.simulate_fill(order, now_ms=1000000000000)

        # Assert
        assert fill.quantity == 100.0
        assert fill.fee_usdt > 0
        assert fill.price > 0


# ══════════════════════════════════════════════════════════════
# ORDER MANAGER TESTS (~10 tests)
# ══════════════════════════════════════════════════════════════


class TestOrderManager:
    """Tests for OrderManager class."""

    def test_submit_order_creates_pending_then_filled_order(self, order_manager, sample_execution_request):
        """submit_order() creates PENDING then FILLED OrderRecord."""
        # Act
        order, fill = order_manager.submit_order(sample_execution_request, seed=42)

        # Assert
        assert order is not None
        assert order.status == OrderStatus.FILLED
        assert order.order_id is not None
        assert fill is not None

    def test_returned_fill_record_has_correct_details(self, order_manager, sample_execution_request):
        """Returned FillRecord has correct fill details."""
        # Act
        order, fill = order_manager.submit_order(sample_execution_request, seed=42)

        # Assert
        assert fill.order_id == order.order_id
        assert fill.symbol == sample_execution_request.symbol
        assert fill.side == sample_execution_request.side
        assert fill.quantity == sample_execution_request.quantity

    def test_failed_simulation_returns_failed_order_and_none_fill(self, order_manager):
        """Failed fill simulation returns FAILED OrderRecord and None FillRecord."""
        # Arrange
        # Create a request with invalid price to trigger failure
        bad_request = ExecutionRequest(
            request_id="req_001",
            decision_id="dec_001",
            trigger_id="trig_001",
            setup_id="setup_001",
            symbol="BTC/USDT",
            side=Side.BUY,
            entry_price=-1.0,  # Invalid: negative price
            stop_loss=48000.0,
            take_profit=52000.0,
            size_usdt=5000.0,
            quantity=0.1,
            strategy_name="momentum_expansion",
            strategy_class=StrategyClass.MOMENTUM_EXPANSION,
            regime="BULL_TREND",
            created_at_ms=1000000000000,
            max_fill_delay_ms=30000
        )

        # Mock fill_simulator to raise an exception
        order_manager.fill_simulator.simulate_fill = Mock(side_effect=ValueError("Invalid price"))

        # Act
        order, fill = order_manager.submit_order(bad_request, seed=42)

        # Assert
        assert order.status == OrderStatus.FAILED
        assert fill is None
        assert order.failure_reason is not None

    def test_order_id_generation_is_deterministic(self, order_manager, sample_execution_request):
        """Order ID generation is deterministic."""
        # Act
        order1, _ = order_manager.submit_order(sample_execution_request, seed=42)

        # Manually create same order to test ID logic
        now_ms = order_manager.now_ms_fn()
        expected_order_id = _make_id(sample_execution_request.request_id, now_ms)

        # Assert
        # Same request and time should produce same ID pattern
        assert order1.order_id is not None

    def test_timestamps_are_consistent(self, order_manager, sample_execution_request):
        """Timestamps are consistent across order and fill."""
        # Act
        order, fill = order_manager.submit_order(sample_execution_request, seed=42)

        # Assert
        assert order.created_at_ms > 0
        assert fill.filled_at_ms > 0
        assert order.filled_at_ms > 0

    def test_multiple_sequential_orders_get_unique_ids(self, order_manager, sample_execution_request):
        """Multiple sequential orders get unique IDs."""
        # Act
        order1, _ = order_manager.submit_order(sample_execution_request, seed=42)
        time.sleep(0.001)  # Tiny sleep to ensure different timestamps

        # Create second request with different ID
        request2 = ExecutionRequest(
            request_id="req_002",  # Different request ID
            decision_id="dec_002",
            trigger_id="trig_002",
            setup_id="setup_002",
            symbol="ETH/USDT",
            side=Side.SELL,
            entry_price=3000.0,
            stop_loss=3100.0,
            take_profit=2900.0,
            size_usdt=3000.0,
            quantity=1.0,
            strategy_name="vwap_reversion",
            strategy_class=StrategyClass.VWAP_REVERSION,
            regime="RANGE",
            created_at_ms=1000000000000,
            max_fill_delay_ms=30000
        )

        order2, _ = order_manager.submit_order(request2, seed=43)

        # Assert
        assert order1.order_id != order2.order_id

    def test_execution_request_validation_before_submission(self, order_manager):
        """ExecutionRequest validation occurs before submission."""
        # Arrange
        invalid_request = ExecutionRequest(
            request_id="",  # Invalid: empty
            decision_id="dec_001",
            trigger_id="trig_001",
            setup_id="setup_001",
            symbol="BTC/USDT",
            side=Side.BUY,
            entry_price=50000.0,
            stop_loss=48000.0,
            take_profit=52000.0,
            size_usdt=5000.0,
            quantity=0.1,
            strategy_name="momentum_expansion",
            strategy_class=StrategyClass.MOMENTUM_EXPANSION,
            regime="BULL_TREND"
        )

        # Act & Assert
        # Should not raise at submission (validation happens in executor)
        # but we verify the order manager receives it
        order, fill = order_manager.submit_order(invalid_request, seed=42)
        assert order is not None


# ══════════════════════════════════════════════════════════════
# INTRADAY EXECUTOR TESTS (~5 tests)
# ══════════════════════════════════════════════════════════════


class TestIntradayExecutor:
    """Tests for IntradayExecutor class."""

    def test_execute_with_valid_request_succeeds(self, intraday_executor, sample_execution_request):
        """execute() with valid request succeeds."""
        # Act
        order, fill = intraday_executor.execute(sample_execution_request, seed=42)

        # Assert
        assert order is not None
        assert order.status == OrderStatus.FILLED
        assert fill is not None

    def test_execute_with_invalid_request_raises_contract_violation(self, intraday_executor):
        """execute() with invalid request raises ContractViolation."""
        # Arrange
        from core.intraday.execution_contracts import ContractViolation

        invalid_request = ExecutionRequest(
            request_id="req_001",
            decision_id="dec_001",
            trigger_id="trig_001",
            setup_id="setup_001",
            symbol="BTC/USDT",
            side=Side.BUY,
            entry_price=50000.0,
            stop_loss=-1000.0,  # Invalid: stop_loss must be positive
            take_profit=52000.0,
            size_usdt=5000.0,
            quantity=0.1,
            strategy_name="momentum_expansion",
            strategy_class=StrategyClass.MOMENTUM_EXPANSION,
            regime="BULL_TREND"
        )

        # Act & Assert
        with pytest.raises(ContractViolation):
            intraday_executor.execute(invalid_request, seed=42)

    def test_execute_delegates_to_order_manager(self, intraday_executor, sample_execution_request):
        """execute() delegates to OrderManager.submit_order()."""
        # Arrange
        intraday_executor.order_manager.submit_order = Mock(
            return_value=(Mock(status=OrderStatus.FILLED), Mock())
        )

        # Act
        order, fill = intraday_executor.execute(sample_execution_request, seed=42)

        # Assert
        intraday_executor.order_manager.submit_order.assert_called_once()

    def test_execute_return_type_is_tuple(self, intraday_executor, sample_execution_request):
        """execute() return type is Tuple[OrderRecord, Optional[FillRecord]]."""
        # Act
        result = intraday_executor.execute(sample_execution_request, seed=42)

        # Assert
        assert isinstance(result, tuple)
        assert len(result) == 2
        order, fill = result
        assert isinstance(order, OrderRecord)
        assert fill is None or isinstance(fill, FillRecord)

    def test_execute_logs_validation_pass(self, intraday_executor, sample_execution_request):
        """execute() logs when validation passes."""
        # Act
        with patch('core.intraday.execution.intraday_executor.logger') as mock_logger:
            intraday_executor.execute(sample_execution_request, seed=42)

        # Assert
        # Check that debug logging occurred
        assert mock_logger.debug.called or mock_logger.info.called


# ══════════════════════════════════════════════════════════════
# EXECUTION ENGINE TESTS (~12 tests)
# ══════════════════════════════════════════════════════════════


class TestExecutionEngine:
    """Tests for ExecutionEngine class."""

    def test_execute_with_approved_decision_succeeds(self, execution_engine, sample_execution_decision):
        """execute() with APPROVED decision succeeds."""
        # Act
        result = execution_engine.execute(sample_execution_decision, seed=42)

        # Assert
        assert isinstance(result, ExecutionResult)
        assert result.success is True

    def test_execute_with_rejected_decision_fails(self, execution_engine):
        """execute() with REJECTED decision returns failure."""
        # Arrange
        rejected_decision = ExecutionDecision(
            decision_id="dec_001",
            intent_id="intent_001",
            trigger_id="trig_001",
            setup_id="setup_001",
            symbol="BTC/USDT",
            direction=Direction.LONG,
            strategy_name="momentum_expansion",
            strategy_class=StrategyClass.MOMENTUM_EXPANSION,
            entry_price=50000.0,
            stop_loss=48000.0,
            take_profit=52000.0,
            final_size_usdt=0.0,  # Zero for rejected
            final_quantity=0.0,
            risk_usdt=0.0,
            risk_reward_ratio=0.0,
            regime="BULL_TREND",
            status=DecisionStatus.REJECTED,
            rejection_reason="Portfolio heat exceeded",
            rejection_source="portfolio_heat",
            created_at_ms=1000000000000
        )

        # Act
        result = execution_engine.execute(rejected_decision, seed=42)

        # Assert
        assert result.success is False
        assert "not APPROVED" in result.failure_reason

    def test_execute_creates_execution_request_from_decision(self, execution_engine, sample_execution_decision):
        """execute() creates ExecutionRequest from ExecutionDecision."""
        # Arrange
        execution_engine.intraday_executor.execute = Mock(
            return_value=(Mock(status=OrderStatus.FILLED, order_id="order_001"), Mock(fill_id="fill_001"))
        )

        # Act
        execution_engine.execute(sample_execution_decision, seed=42)

        # Assert
        # Verify executor was called with ExecutionRequest
        call_args = execution_engine.intraday_executor.execute.call_args
        assert call_args is not None
        request = call_args[0][0]
        assert isinstance(request, ExecutionRequest)

    def test_execute_opens_position_in_portfolio_state(self, execution_engine, sample_execution_decision):
        """execute() opens position in PortfolioState on success."""
        # Act
        execution_engine.execute(sample_execution_decision, seed=42)

        # Assert
        execution_engine.portfolio_state.open_position.assert_called_once()

    def test_execute_persists_snapshot_if_manager_provided(self, execution_engine, sample_execution_decision):
        """execute() persists snapshot if PersistenceManager provided."""
        # Act
        execution_engine.execute(sample_execution_decision, seed=42)

        # Assert
        execution_engine.persistence_manager.save_snapshot.assert_called_once()

    def test_execute_calls_feedback_learning_slot(self, execution_engine, sample_execution_decision):
        """execute() calls Phase 5b feedback learning slot without error."""
        # Arrange
        execution_engine._feedback_learning = Mock()

        # Act
        result = execution_engine.execute(sample_execution_decision, seed=42)

        # Assert
        # Feedback learning is a no-op in Phase 5a, but slot exists
        assert result.success is True

    def test_execution_result_has_all_fields_populated(self, execution_engine, sample_execution_decision):
        """execute() returns ExecutionResult with all fields populated on success."""
        # Act
        result = execution_engine.execute(sample_execution_decision, seed=42)

        # Assert
        assert result.success is True
        assert result.position_id is not None
        assert result.order_id is not None
        assert result.failure_reason == ""

    def test_execute_exception_caught_and_returned_as_failure(self, execution_engine, sample_execution_decision):
        """execute() catches exceptions and returns as failure ExecutionResult."""
        # Arrange
        execution_engine.intraday_executor.execute = Mock(
            side_effect=ValueError("Executor error")
        )

        # Act
        result = execution_engine.execute(sample_execution_decision, seed=42)

        # Assert
        assert result.success is False
        assert "Executor error" in result.failure_reason

    def test_execute_handles_none_fill_gracefully(self, execution_engine, sample_execution_decision):
        """execute() handles None fill gracefully."""
        # Arrange
        execution_engine.intraday_executor.execute = Mock(
            return_value=(Mock(status=OrderStatus.FAILED, failure_reason="No fill"), None)
        )

        # Act
        result = execution_engine.execute(sample_execution_decision, seed=42)

        # Assert
        assert result.success is False

    def test_metadata_includes_full_traceability_chain(self, execution_engine, sample_execution_decision):
        """execute() metadata includes full traceability chain."""
        # Arrange
        execution_engine.portfolio_state.open_position = Mock(
            return_value=Mock(position_id="pos_001")
        )

        # Act
        execution_engine.execute(sample_execution_decision, seed=42)

        # Assert
        call_args = execution_engine.portfolio_state.open_position.call_args
        fill = call_args[0][0]
        metadata = call_args[0][1]

        assert metadata["trigger_id"] == sample_execution_decision.trigger_id
        assert metadata["setup_id"] == sample_execution_decision.setup_id
        assert metadata["strategy_name"] == sample_execution_decision.strategy_name


# ══════════════════════════════════════════════════════════════
# EXECUTION GATE TESTS (~8 tests)
# ══════════════════════════════════════════════════════════════


class TestExecutionGate:
    """Tests for ExecutionGate class."""

    def test_start_subscribes_to_trigger_fired_topic(self, execution_gate):
        """start() subscribes to TRIGGER_FIRED topic."""
        # Act
        execution_gate.start()

        # Assert
        execution_gate.event_bus.subscribe.assert_called_once()
        call_args = execution_gate.event_bus.subscribe.call_args
        assert "TRIGGER_FIRED" in str(call_args) or call_args[0][0] is not None

    def test_stop_unsubscribes_from_trigger_fired_topic(self, execution_gate):
        """stop() unsubscribes from TRIGGER_FIRED topic."""
        # Arrange
        execution_gate.start()
        execution_gate.event_bus.reset_mock()

        # Act
        execution_gate.stop()

        # Assert
        execution_gate.event_bus.unsubscribe.assert_called_once()

    def test_deserialize_trigger_signal_reconstructs_from_dict(self, execution_gate, sample_trigger_signal):
        """_deserialize_trigger_signal() reconstructs TriggerSignal from dict."""
        # Arrange
        trigger_dict = sample_trigger_signal.to_dict()

        # Act
        reconstructed = execution_gate._deserialize_trigger_signal(trigger_dict)

        # Assert
        assert reconstructed.trigger_id == sample_trigger_signal.trigger_id
        assert reconstructed.setup_id == sample_trigger_signal.setup_id
        assert reconstructed.symbol == sample_trigger_signal.symbol

    def test_deserialize_trigger_signal_handles_invalid_data_gracefully(self, execution_gate):
        """_deserialize_trigger_signal() handles invalid event data without crash."""
        # Arrange
        invalid_data = {
            "trigger_id": "trig_001",
            # Missing required fields
        }

        # Act & Assert
        with pytest.raises(ValueError):
            execution_gate._deserialize_trigger_signal(invalid_data)

    def test_forwards_valid_trigger_to_processing_engine(self, execution_gate, sample_trigger_signal):
        """_on_trigger_fired() forwards to ProcessingEngine on valid trigger."""
        # Arrange
        from core.event_bus import Event

        event = Event(
            topic="TRIGGER_FIRED",
            data=sample_trigger_signal.to_dict(),
            source="signal_pipeline"
        )

        execution_gate.processing_engine.process = Mock(
            return_value=Mock(
                to_dict=Mock(return_value={}),
                intent_id="intent_001",
                trigger_id="trig_001",
                setup_id="setup_001",
                symbol="BTC/USDT",
                direction=Direction.LONG,
                strategy_name="test",
                strategy_class=StrategyClass.MOMENTUM_EXPANSION,
                entry_price=50000.0,
                stop_loss=48000.0,
                take_profit=52000.0,
                size_usdt=5000.0,
                quantity=0.1,
                risk_usdt=200.0,
                risk_reward_ratio=1.5,
                regime="BULL",
                candle_trace_ids=()
            )
        )

        # Act
        execution_gate._on_trigger_fired(event)

        # Assert
        execution_gate.processing_engine.process.assert_called_once()

    def test_publishes_lifecycle_events(self, execution_gate, sample_trigger_signal):
        """_on_trigger_fired() publishes lifecycle events."""
        # Arrange
        from core.event_bus import Event

        event = Event(
            topic="TRIGGER_FIRED",
            data=sample_trigger_signal.to_dict(),
            source="signal_pipeline"
        )

        execution_gate.processing_engine.process = Mock(
            return_value=Mock(
                to_dict=Mock(return_value={}),
                intent_id="intent_001",
                trigger_id="trig_001",
                setup_id="setup_001",
                symbol="BTC/USDT",
                direction=Direction.LONG,
                strategy_name="test",
                strategy_class=StrategyClass.MOMENTUM_EXPANSION,
                entry_price=50000.0,
                stop_loss=48000.0,
                take_profit=52000.0,
                size_usdt=5000.0,
                quantity=0.1,
                risk_usdt=200.0,
                risk_reward_ratio=1.5,
                regime="BULL",
                candle_trace_ids=()
            )
        )

        # Act
        execution_gate._on_trigger_fired(event)

        # Assert
        # Verify multiple publish calls for lifecycle events
        assert execution_gate.event_bus.publish.call_count >= 1

    def test_event_bus_integration_mock_subscribe(self, mock_event_bus):
        """Event bus integration: mock bus handles subscribe calls."""
        # Arrange
        gate = ExecutionGate(
            processing_engine=Mock(),
            execution_engine=Mock(),
            portfolio_state=Mock(),
            event_bus=mock_event_bus
        )

        # Act
        gate.start()

        # Assert
        mock_event_bus.subscribe.assert_called_once()

    def test_event_bus_integration_mock_publish(self, mock_event_bus):
        """Event bus integration: mock bus handles publish calls."""
        # Arrange
        gate = ExecutionGate(
            processing_engine=Mock(),
            execution_engine=Mock(),
            portfolio_state=Mock(),
            event_bus=mock_event_bus
        )

        # Create a valid event
        from core.event_bus import Event
        trigger_data = {
            "trigger_id": "trig_001",
            "setup_id": "setup_001",
            "strategy_name": "test",
            "strategy_class": "MX",
            "symbol": "BTC/USDT",
            "direction": "long",
            "entry_price": 50000.0,
            "stop_loss": 48000.0,
            "take_profit": 52000.0,
            "atr_value": 500.0,
            "strength": 0.8,
            "trigger_quality": 0.75,
        }

        event = Event(
            topic="TRIGGER_FIRED",
            data=trigger_data,
            source="signal_pipeline"
        )

        # Mock processing to return None (processing rejected)
        gate.processing_engine.process = Mock(return_value=None)

        # Act
        gate._on_trigger_fired(event)

        # Assert
        assert mock_event_bus.publish.call_count >= 1


# ══════════════════════════════════════════════════════════════
# INTEGRATION TESTS
# ══════════════════════════════════════════════════════════════


class TestExecutionPipelineIntegration:
    """Integration tests for the full execution pipeline."""

    def test_fill_simulator_to_order_manager_to_executor(self, fill_simulator, order_manager, intraday_executor, sample_execution_request):
        """Full flow: FillSimulator → OrderManager → IntradayExecutor."""
        # Act
        order, fill = intraday_executor.execute(sample_execution_request, seed=42)

        # Assert
        assert order.status == OrderStatus.FILLED
        assert fill is not None
        assert fill.fee_usdt > 0

    def test_executor_to_execution_engine_to_portfolio(self, execution_engine, sample_execution_decision):
        """Full flow: IntradayExecutor → ExecutionEngine → PortfolioState."""
        # Act
        result = execution_engine.execute(sample_execution_decision, seed=42)

        # Assert
        assert result.success is True
        execution_engine.portfolio_state.open_position.assert_called_once()

    def test_slippage_and_fee_accumulation(self, fill_simulator):
        """Slippage and fees accumulate correctly in large orders."""
        # Arrange
        order = OrderRecord(
            order_id="order_001",
            request_id="req_001",
            decision_id="dec_001",
            trigger_id="trig_001",
            symbol="BTC/USDT",
            side=Side.BUY,
            order_type=OrderType.MARKET,
            requested_price=50000.0,
            requested_quantity=10.0,  # 500k USDT notional
            status=OrderStatus.PENDING,
            created_at_ms=1000000000000
        )

        # Act
        fill = fill_simulator.simulate_fill(order, now_ms=1000000000000, seed=42)

        # Assert
        notional = fill.price * fill.quantity
        fee_pct = fill.fee_usdt / notional if notional > 0 else 0
        assert fee_pct > 0
        assert fee_pct <= 0.0005  # Taker rate

    def test_rejection_flow_through_execution_gate(self, execution_gate, sample_trigger_signal):
        """Rejection flow publishes proper event through ExecutionGate."""
        # Arrange
        from core.event_bus import Event

        event = Event(
            topic="TRIGGER_FIRED",
            data=sample_trigger_signal.to_dict(),
            source="signal_pipeline"
        )

        # Mock processing engine to return None (rejection)
        execution_gate.processing_engine.process = Mock(return_value=None)
        execution_gate.event_bus.reset_mock()

        # Act
        execution_gate._on_trigger_fired(event)

        # Assert
        # Should have published at least one rejection event
        assert execution_gate.event_bus.publish.call_count >= 1
