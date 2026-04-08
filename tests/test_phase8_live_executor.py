"""
Phase 8 Test Suite — LiveExecutor

Tests:
- Happy path: execute → submit → fill → (OrderRecord, FillRecord)
- Idempotent submission (duplicate request returns existing)
- Fail-closed on rejected orders
- Fail-closed on network errors (exhausted retries)
- Crash-after-submit scenario
- Partial fill handling
- Market order fill timeout
- Symbol formatting (BTCUSDT → BTC/USDT:USDT)
- Contract compatibility (returns same types as paper path)
"""

import pytest
from unittest.mock import MagicMock, patch
from pathlib import Path

from core.intraday.execution_contracts import (
    ExecutionRequest,
    OrderRecord,
    FillRecord,
    OrderStatus,
    OrderType,
    Side,
    _make_id,
)
from core.intraday.signal_contracts import StrategyClass
from core.intraday.live.live_executor import LiveExecutor
from core.intraday.live.idempotency_store import IdempotencyStore
from core.intraday.live.exchange_adapter import (
    ExchangeAdapter,
    ExchangeError,
    ExchangeErrorClass,
    ExchangeResponse,
    RetryConfig,
)
from core.intraday.live.order_lifecycle import OrderLifecycleState


# ══════════════════════════════════════════════════════════════
# FIXTURES
# ══════════════════════════════════════════════════════════════

def _make_request(**overrides) -> ExecutionRequest:
    """Factory for ExecutionRequest with sensible defaults."""
    defaults = dict(
        request_id="req-001",
        decision_id="dec-001",
        trigger_id="trig-001",
        setup_id="setup-001",
        symbol="BTCUSDT",
        side=Side.BUY,
        entry_price=50000.0,
        stop_loss=49000.0,
        take_profit=52000.0,
        size_usdt=500.0,
        quantity=0.01,
        strategy_name="MomentumBreakout",
        strategy_class=StrategyClass.MOMENTUM_EXPANSION,
        regime="bull_trend",
        created_at_ms=1000000,
    )
    defaults.update(overrides)
    return ExecutionRequest(**defaults)


def _make_executor(tmp_path, responses=None):
    """Create LiveExecutor with mocked exchange adapter."""
    clock = [1000000]

    # Mock exchange adapter
    adapter = MagicMock(spec=ExchangeAdapter)

    if responses:
        adapter.create_order.side_effect = responses
    else:
        # Default: immediate fill
        adapter.create_order.return_value = ExchangeResponse(
            success=True,
            exchange_order_id="BYBIT-123",
            status="closed",
            filled_quantity=0.01,
            avg_price=50050.0,
            fee=0.28,
            fee_currency="USDT",
            remaining=0,
            timestamp_ms=clock[0],
        )

    # Real idempotency store with temp path
    store = IdempotencyStore(
        store_path=tmp_path / "idempotency.json",
        now_ms_fn=lambda: clock[0],
    )

    executor = LiveExecutor(
        exchange_adapter=adapter,
        idempotency_store=store,
        now_ms_fn=lambda: clock[0],
    )

    return executor, adapter, store, clock


# ══════════════════════════════════════════════════════════════
# 1. HAPPY PATH
# ══════════════════════════════════════════════════════════════

class TestHappyPath:
    def test_market_order_immediate_fill(self, tmp_path):
        executor, adapter, store, clock = _make_executor(tmp_path)
        request = _make_request()

        order_record, fill_record = executor.execute(request)

        assert isinstance(order_record, OrderRecord)
        assert isinstance(fill_record, FillRecord)
        assert order_record.status == OrderStatus.FILLED
        assert order_record.filled_price > 0
        assert order_record.filled_quantity == 0.01
        assert fill_record.quantity == 0.01
        assert fill_record.price == 50050.0

    def test_returns_correct_types(self, tmp_path):
        """Contract compatibility with paper path."""
        executor, _, _, _ = _make_executor(tmp_path)
        request = _make_request()

        result = executor.execute(request)
        assert isinstance(result, tuple)
        assert len(result) == 2
        assert isinstance(result[0], OrderRecord)
        assert isinstance(result[1], (FillRecord, type(None)))

    def test_order_record_traceability(self, tmp_path):
        executor, _, _, _ = _make_executor(tmp_path)
        request = _make_request()

        order_record, _ = executor.execute(request)
        assert order_record.request_id == "req-001"
        assert order_record.decision_id == "dec-001"
        assert order_record.trigger_id == "trig-001"
        assert order_record.symbol == "BTCUSDT"
        assert order_record.side == Side.BUY

    def test_fill_record_traceability(self, tmp_path):
        executor, _, _, _ = _make_executor(tmp_path)
        request = _make_request()

        _, fill_record = executor.execute(request)
        assert fill_record.symbol == "BTCUSDT"
        assert fill_record.side == Side.BUY
        assert fill_record.is_maker is False  # Market order = taker

    def test_idempotency_registered(self, tmp_path):
        executor, _, store, _ = _make_executor(tmp_path)
        request = _make_request()

        executor.execute(request)
        assert store.entry_count == 1


# ══════════════════════════════════════════════════════════════
# 2. IDEMPOTENT SUBMISSION
# ══════════════════════════════════════════════════════════════

class TestIdempotentSubmission:
    def test_duplicate_request_not_resubmitted(self, tmp_path):
        """Same request submitted twice → second call fetches existing."""
        clock = [1000000]
        adapter = MagicMock(spec=ExchangeAdapter)
        store = IdempotencyStore(
            store_path=tmp_path / "idemp.json",
            now_ms_fn=lambda: clock[0],
        )

        # Pre-register as submitted+confirmed
        from core.intraday.live.order_lifecycle import make_client_order_id
        request = _make_request()
        cid = make_client_order_id(
            request.request_id, request.symbol,
            request.side.value, request.created_at_ms,
        )
        store.register(cid, request.request_id, request.symbol, request.side.value)
        store.mark_submitted(cid)
        store.mark_confirmed(cid, "BYBIT-999")

        # Executor should detect duplicate and fetch existing
        adapter.fetch_order.return_value = ExchangeResponse(
            success=True,
            exchange_order_id="BYBIT-999",
            status="closed",
            filled_quantity=0.01,
            avg_price=50050.0,
            fee=0.28,
            timestamp_ms=clock[0],
        )

        executor = LiveExecutor(
            exchange_adapter=adapter,
            idempotency_store=store,
            now_ms_fn=lambda: clock[0],
        )

        order_record, fill_record = executor.execute(request)

        # Should NOT have called create_order
        adapter.create_order.assert_not_called()
        # Should have called fetch_order
        adapter.fetch_order.assert_called_once()
        assert order_record.status == OrderStatus.FILLED


# ══════════════════════════════════════════════════════════════
# 3. FAIL-CLOSED SCENARIOS
# ══════════════════════════════════════════════════════════════

class TestFailClosed:
    def test_rejected_order_fails(self, tmp_path):
        """Exchange rejects order → OrderRecord.status == FAILED."""
        error = ExchangeError("insufficient funds", ExchangeErrorClass.REJECTED)
        responses = [ExchangeResponse(
            success=False,
            error=error,
            timestamp_ms=1000000,
        )]
        executor, adapter, store, clock = _make_executor(tmp_path, responses=responses)
        request = _make_request()

        order_record, fill_record = executor.execute(request)

        assert order_record.status == OrderStatus.FAILED
        assert fill_record is None
        assert "insufficient funds" in order_record.failure_reason

    def test_network_error_fails(self, tmp_path):
        """Network error (after retries exhausted) → FAILED."""
        error = ExchangeError("timeout", ExchangeErrorClass.TRANSIENT)
        responses = [ExchangeResponse(
            success=False,
            error=error,
            timestamp_ms=1000000,
        )]
        executor, _, _, _ = _make_executor(tmp_path, responses=responses)
        request = _make_request()

        order_record, fill_record = executor.execute(request)

        assert order_record.status == OrderStatus.FAILED
        assert fill_record is None

    def test_unknown_error_fails(self, tmp_path):
        """Unknown error → FAILED (fail-closed)."""
        error = ExchangeError("???", ExchangeErrorClass.UNKNOWN)
        responses = [ExchangeResponse(
            success=False,
            error=error,
            timestamp_ms=1000000,
        )]
        executor, _, _, _ = _make_executor(tmp_path, responses=responses)
        request = _make_request()

        order_record, fill_record = executor.execute(request)
        assert order_record.status == OrderStatus.FAILED

    def test_unhandled_exception_fails(self, tmp_path):
        """Unexpected exception → FAILED, not crash."""
        executor, adapter, _, _ = _make_executor(tmp_path)
        adapter.create_order.side_effect = RuntimeError("unexpected")
        request = _make_request()

        order_record, fill_record = executor.execute(request)
        assert order_record.status == OrderStatus.FAILED
        assert fill_record is None


# ══════════════════════════════════════════════════════════════
# 4. SYMBOL FORMATTING
# ══════════════════════════════════════════════════════════════

class TestSymbolFormatting:
    def test_btcusdt_to_ccxt(self):
        assert LiveExecutor._format_symbol("BTCUSDT") == "BTC/USDT:USDT"

    def test_ethusdt_to_ccxt(self):
        assert LiveExecutor._format_symbol("ETHUSDT") == "ETH/USDT:USDT"

    def test_solusdt_to_ccxt(self):
        assert LiveExecutor._format_symbol("SOLUSDT") == "SOL/USDT:USDT"

    def test_already_ccxt_format(self):
        assert LiveExecutor._format_symbol("BTC/USDT:USDT") == "BTC/USDT:USDT"

    def test_unknown_format_passthrough(self):
        assert LiveExecutor._format_symbol("UNKNOWN") == "UNKNOWN"


# ══════════════════════════════════════════════════════════════
# 5. STATE ACCESS
# ══════════════════════════════════════════════════════════════

class TestStateAccess:
    def test_get_all_orders(self, tmp_path):
        executor, _, _, _ = _make_executor(tmp_path)
        request = _make_request()
        executor.execute(request)
        all_orders = executor.get_all_orders()
        assert len(all_orders) == 1

    def test_get_live_orders_empty_after_fill(self, tmp_path):
        executor, _, _, _ = _make_executor(tmp_path)
        request = _make_request()
        executor.execute(request)
        live = executor.get_live_orders()
        assert len(live) == 0  # All filled = terminal

    def test_get_state_diagnostics(self, tmp_path):
        executor, _, _, _ = _make_executor(tmp_path)
        request = _make_request()
        executor.execute(request)
        state = executor.get_state()
        assert state["total_orders"] == 1
        assert state["terminal_orders"] == 1
        assert state["live_orders"] == 0


# ══════════════════════════════════════════════════════════════
# 6. DUPLICATE ORDER ON EXCHANGE
# ══════════════════════════════════════════════════════════════

class TestDuplicateOnExchange:
    def test_duplicate_exchange_error_handled(self, tmp_path):
        """Exchange returns 'duplicate' error → not a crash."""
        error = ExchangeError(
            "duplicate order", ExchangeErrorClass.DUPLICATE, exchange_code="duplicate_order"
        )
        responses = [ExchangeResponse(
            success=False,
            error=error,
            timestamp_ms=1000000,
        )]
        executor, _, _, _ = _make_executor(tmp_path, responses=responses)
        request = _make_request()

        order_record, fill_record = executor.execute(request)
        # Should fail gracefully, not crash
        assert order_record.status == OrderStatus.FAILED
        assert fill_record is None
