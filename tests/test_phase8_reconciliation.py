"""
Phase 8 Test Suite — Reconciliation Engine & Recovery Manager

Tests:
- Order state mismatch detection
- Position mismatch detection
- Orphan order detection
- Fill quantity mismatch
- Clean reconciliation (no mismatches)
- Auto-resolve orphan orders
- Recovery manager 5-phase sequence
- Crash-after-submit recovery
- Recovery blocks trading until clean
- RecoveryReport diagnostics
"""

import pytest
from unittest.mock import MagicMock
from pathlib import Path
from core.intraday.live.reconciliation_engine import (
    OrderReconciliationEngine,
    ReconciliationResult,
    ReconciliationAction,
    MismatchType,
    Mismatch,
)
from core.intraday.live.recovery_manager import (
    RestartRecoveryManager,
    RecoveryReport,
    RecoveryPhase,
)
from core.intraday.live.idempotency_store import IdempotencyStore
from core.intraday.live.exchange_adapter import ExchangeResponse
from core.intraday.live.order_lifecycle import LiveOrder, OrderLifecycleState


# ══════════════════════════════════════════════════════════════
# FIXTURES
# ══════════════════════════════════════════════════════════════

def _make_live_order(**overrides) -> LiveOrder:
    defaults = dict(
        client_order_id="NT-abc123",
        request_id="req-1",
        decision_id="dec-1",
        trigger_id="trig-1",
        symbol="BTCUSDT",
        side="buy",
        order_type="market",
        requested_price=50000.0,
        requested_quantity=0.01,
        size_usdt=500.0,
        strategy_name="MomentumBreakout",
        regime="bull_trend",
        stop_loss=49000.0,
        take_profit=52000.0,
        state=OrderLifecycleState.LIVE,
        exchange_order_id="BYBIT-123",
        created_at_ms=1000000,
    )
    defaults.update(overrides)
    return LiveOrder(**defaults)


class MockPosition:
    def __init__(self, position_id, symbol, quantity, direction="long"):
        self.position_id = position_id
        self.symbol = symbol
        self.quantity = quantity
        self.direction = direction


def _make_adapter_mock(open_orders=None, positions=None, fetch_order_resp=None):
    adapter = MagicMock()
    adapter.fetch_open_orders.return_value = open_orders or []
    adapter.fetch_positions.return_value = positions or []
    adapter.fetch_balance.return_value = {"USDT": {"total": 10000.0}}

    if fetch_order_resp:
        adapter.fetch_order.return_value = fetch_order_resp
    else:
        adapter.fetch_order.return_value = ExchangeResponse(
            success=False, timestamp_ms=1000000
        )

    return adapter


# ══════════════════════════════════════════════════════════════
# 1. CLEAN RECONCILIATION
# ══════════════════════════════════════════════════════════════

class TestCleanReconciliation:
    def test_no_orders_no_positions(self):
        adapter = _make_adapter_mock()
        engine = OrderReconciliationEngine(adapter)
        result = engine.reconcile(internal_orders={})
        assert result.success
        assert result.is_clean
        assert result.mismatch_count == 0

    def test_terminal_orders_ignored(self):
        """Terminal orders are skipped — not checked against exchange."""
        adapter = _make_adapter_mock()
        engine = OrderReconciliationEngine(adapter)
        order = _make_live_order(state=OrderLifecycleState.FILLED)
        result = engine.reconcile(internal_orders={"NT-abc": order})
        assert result.success
        assert result.mismatch_count == 0


# ══════════════════════════════════════════════════════════════
# 2. ORDER STATE MISMATCH
# ══════════════════════════════════════════════════════════════

class TestOrderStateMismatch:
    def test_internal_live_exchange_filled(self):
        """Internal says LIVE, exchange says filled."""
        adapter = _make_adapter_mock(
            fetch_order_resp=ExchangeResponse(
                success=True,
                exchange_order_id="BYBIT-123",
                status="closed",
                filled_quantity=0.01,
                avg_price=50050.0,
                timestamp_ms=1000000,
            )
        )
        engine = OrderReconciliationEngine(adapter)
        order = _make_live_order(state=OrderLifecycleState.LIVE)

        result = engine.reconcile(internal_orders={"NT-abc": order})
        assert result.has_mismatches
        types = {m.mismatch_type for m in result.mismatches}
        assert MismatchType.ORDER_STATE_MISMATCH in types

    def test_internal_live_exchange_cancelled(self):
        """Internal says LIVE, exchange says cancelled."""
        adapter = _make_adapter_mock(
            fetch_order_resp=ExchangeResponse(
                success=True,
                exchange_order_id="BYBIT-123",
                status="canceled",
                filled_quantity=0,
                timestamp_ms=1000000,
            )
        )
        engine = OrderReconciliationEngine(adapter)
        order = _make_live_order(state=OrderLifecycleState.LIVE)

        result = engine.reconcile(internal_orders={"NT-abc": order})
        assert result.has_mismatches
        found = [m for m in result.mismatches if m.mismatch_type == MismatchType.ORDER_STATE_MISMATCH]
        assert len(found) >= 1
        assert found[0].recommended_action == ReconciliationAction.SYNC_FROM_EXCHANGE


# ══════════════════════════════════════════════════════════════
# 3. FILL QUANTITY MISMATCH
# ══════════════════════════════════════════════════════════════

class TestFillMismatch:
    def test_fill_quantity_differs(self):
        """Internal has 0.005 filled, exchange has 0.01 filled."""
        adapter = _make_adapter_mock(
            fetch_order_resp=ExchangeResponse(
                success=True,
                exchange_order_id="BYBIT-123",
                status="closed",
                filled_quantity=0.01,
                avg_price=50050.0,
                timestamp_ms=1000000,
            )
        )
        engine = OrderReconciliationEngine(adapter)
        order = _make_live_order(state=OrderLifecycleState.PARTIALLY_FILLED)
        order.filled_quantity = 0.005

        result = engine.reconcile(internal_orders={"NT-abc": order})
        types = {m.mismatch_type for m in result.mismatches}
        assert MismatchType.ORDER_FILL_MISMATCH in types


# ══════════════════════════════════════════════════════════════
# 4. ORDER MISSING ON EXCHANGE
# ══════════════════════════════════════════════════════════════

class TestOrderMissing:
    def test_submitted_order_not_found(self):
        """Order in SUBMISSION_ATTEMPTED but not on exchange."""
        adapter = _make_adapter_mock(
            fetch_order_resp=ExchangeResponse(
                success=False, timestamp_ms=1000000
            )
        )
        engine = OrderReconciliationEngine(adapter)
        order = _make_live_order(
            state=OrderLifecycleState.LIVE,
            exchange_order_id="BYBIT-123",
        )

        result = engine.reconcile(internal_orders={"NT-abc": order})
        types = {m.mismatch_type for m in result.mismatches}
        assert MismatchType.ORDER_MISSING_ON_EXCHANGE in types

    def test_submitted_no_exchange_id(self):
        """SUBMISSION_ATTEMPTED with no exchange_order_id."""
        adapter = _make_adapter_mock()
        engine = OrderReconciliationEngine(adapter)
        order = _make_live_order(
            state=OrderLifecycleState.SUBMISSION_ATTEMPTED,
            exchange_order_id="",
        )

        result = engine.reconcile(internal_orders={"NT-abc": order})
        found = [m for m in result.mismatches if m.mismatch_type == MismatchType.ORDER_MISSING_ON_EXCHANGE]
        assert len(found) == 1
        assert "crash-after-submit" in found[0].details.lower()


# ══════════════════════════════════════════════════════════════
# 5. ORPHAN ORDERS
# ══════════════════════════════════════════════════════════════

class TestOrphanOrders:
    def test_orphan_order_detected(self):
        """Order on exchange with NT- prefix but not in internal orders."""
        open_order = ExchangeResponse(
            success=True,
            exchange_order_id="BYBIT-999",
            status="open",
            raw={
                "id": "BYBIT-999",
                "clientOrderId": "",
                "orderLinkId": "NT-orphan123",
                "symbol": "BTC/USDT:USDT",
            },
            timestamp_ms=1000000,
        )
        adapter = _make_adapter_mock(open_orders=[open_order])
        engine = OrderReconciliationEngine(adapter)

        result = engine.reconcile(internal_orders={})
        orphans = [m for m in result.mismatches if m.mismatch_type == MismatchType.ORPHAN_ORDER_ON_EXCHANGE]
        assert len(orphans) == 1
        assert orphans[0].recommended_action == ReconciliationAction.CANCEL_ON_EXCHANGE


# ══════════════════════════════════════════════════════════════
# 6. POSITION RECONCILIATION
# ══════════════════════════════════════════════════════════════

class TestPositionReconciliation:
    def test_position_missing_on_exchange(self):
        """Position internally but not on exchange."""
        adapter = _make_adapter_mock(positions=[])
        engine = OrderReconciliationEngine(adapter)

        pos = MockPosition("pos-1", "BTCUSDT", 0.01)
        result = engine.reconcile(
            internal_orders={},
            internal_positions={"pos-1": pos},
        )
        found = [m for m in result.mismatches if m.mismatch_type == MismatchType.POSITION_MISSING_ON_EXCHANGE]
        assert len(found) == 1
        assert found[0].recommended_action == ReconciliationAction.CLOSE_INTERNAL_POSITION

    def test_position_size_mismatch(self):
        """Internal qty=0.01, exchange qty=0.005."""
        adapter = _make_adapter_mock(
            positions=[{"symbol": "BTCUSDT", "contracts": 0.005, "id": "EP-1"}]
        )
        engine = OrderReconciliationEngine(adapter)

        pos = MockPosition("pos-1", "BTCUSDT", 0.01)
        result = engine.reconcile(
            internal_orders={},
            internal_positions={"pos-1": pos},
        )
        found = [m for m in result.mismatches if m.mismatch_type == MismatchType.POSITION_SIZE_MISMATCH]
        assert len(found) == 1

    def test_position_missing_internally(self):
        """Position on exchange but not internally."""
        adapter = _make_adapter_mock(
            positions=[{"symbol": "ETHUSDT", "contracts": 0.1, "id": "EP-2"}]
        )
        engine = OrderReconciliationEngine(adapter)

        result = engine.reconcile(
            internal_orders={},
            internal_positions={},
        )
        found = [m for m in result.mismatches if m.mismatch_type == MismatchType.POSITION_MISSING_INTERNALLY]
        assert len(found) == 1
        assert found[0].recommended_action == ReconciliationAction.OPEN_INTERNAL_POSITION


# ══════════════════════════════════════════════════════════════
# 7. AUTO-RESOLVE
# ══════════════════════════════════════════════════════════════

class TestAutoResolve:
    def test_cancel_orphan_order(self):
        """Auto-resolve cancels orphan orders on exchange."""
        open_order = ExchangeResponse(
            success=True,
            exchange_order_id="BYBIT-999",
            status="open",
            raw={
                "id": "BYBIT-999",
                "clientOrderId": "",
                "orderLinkId": "NT-orphan",
                "symbol": "BTC/USDT:USDT",
            },
            timestamp_ms=1000000,
        )
        adapter = _make_adapter_mock(open_orders=[open_order])
        adapter.cancel_order.return_value = ExchangeResponse(
            success=True, status="canceled", timestamp_ms=1000000,
        )
        engine = OrderReconciliationEngine(adapter)

        result = engine.reconcile(internal_orders={}, auto_resolve=True)
        assert len(result.actions_taken) >= 1
        action = result.actions_taken[0]
        assert action["action"] == "cancel_orphan"


# ══════════════════════════════════════════════════════════════
# 8. RESULT SERIALIZATION
# ══════════════════════════════════════════════════════════════

class TestResultSerialization:
    def test_reconciliation_result_to_dict(self):
        result = ReconciliationResult(
            success=True,
            timestamp_ms=1000000,
            orders_checked=5,
            positions_checked=2,
        )
        d = result.to_dict()
        assert d["success"] is True
        assert d["orders_checked"] == 5
        assert d["mismatch_count"] == 0

    def test_mismatch_to_dict(self):
        m = Mismatch(
            mismatch_type=MismatchType.ORDER_STATE_MISMATCH,
            symbol="BTCUSDT",
            internal_id="NT-abc",
            exchange_id="BYBIT-123",
            internal_state="live",
            exchange_state="filled",
            recommended_action=ReconciliationAction.SYNC_FROM_EXCHANGE,
        )
        d = m.to_dict()
        assert d["mismatch_type"] == "order_state_mismatch"
        assert d["recommended_action"] == "sync_from_exchange"


# ══════════════════════════════════════════════════════════════
# 9. RECOVERY MANAGER
# ══════════════════════════════════════════════════════════════

class TestRecoveryManager:
    def _make_manager(self, tmp_path, adapter=None):
        clock = [1000000]
        if adapter is None:
            adapter = _make_adapter_mock()

        store = IdempotencyStore(
            store_path=tmp_path / "idemp.json",
            now_ms_fn=lambda: clock[0],
        )
        reconciliation = OrderReconciliationEngine(
            adapter, now_ms_fn=lambda: clock[0],
        )
        manager = RestartRecoveryManager(
            exchange_adapter=adapter,
            idempotency_store=store,
            reconciliation_engine=reconciliation,
            now_ms_fn=lambda: clock[0],
        )
        return manager, store, clock

    def test_clean_recovery(self, tmp_path):
        """Empty state → clean recovery → trading allowed."""
        manager, store, clock = self._make_manager(tmp_path)
        report = manager.recover()

        assert report.success
        assert report.is_clean
        assert report.trading_allowed
        assert report.phase == RecoveryPhase.COMPLETED
        assert manager.recovery_complete
        assert manager.trading_allowed

    def test_recovery_with_pending_submissions(self, tmp_path):
        """Pending submissions detected → reconciliation runs."""
        manager, store, clock = self._make_manager(tmp_path)

        # Pre-populate with a pending submission
        store.register("NT-pending", "req-1", "BTCUSDT", "buy")
        store.mark_submitted("NT-pending")
        store.save()

        report = manager.recover()
        assert report.success
        assert report.pending_submissions_found == 1

    def test_recovery_blocks_trading_on_mismatch(self, tmp_path):
        """Mismatches found → trading NOT allowed."""
        # Create adapter that returns a position we don't know about
        adapter = _make_adapter_mock(
            positions=[{"symbol": "XRPUSDT", "contracts": 100, "id": "EP-1"}]
        )
        manager, store, clock = self._make_manager(tmp_path, adapter=adapter)

        report = manager.recover(internal_positions={})
        assert report.success  # Recovery itself succeeded
        assert not report.is_clean  # But state isn't clean
        assert not report.trading_allowed

    def test_recovery_report_diagnostics(self, tmp_path):
        manager, store, clock = self._make_manager(tmp_path)
        report = manager.recover()

        d = report.to_dict()
        assert "phase" in d
        assert "success" in d
        assert "trading_allowed" in d
        assert "reconciliation" in d

    def test_recovery_cleans_generated_entries(self, tmp_path):
        """Generated-but-not-submitted entries are cleaned up."""
        manager, store, clock = self._make_manager(tmp_path)

        store.register("NT-gen", "req-1", "BTCUSDT", "buy")
        # Don't mark submitted — simulates crash-before-submit

        report = manager.recover()
        entry = store.get("NT-gen")
        assert entry.state == "failed"  # Cleaned up

    def test_recovery_exception_handling(self, tmp_path):
        """If reconciliation throws, recovery fails gracefully."""
        adapter = _make_adapter_mock()
        adapter.fetch_open_orders.side_effect = RuntimeError("exchange down")

        manager, store, clock = self._make_manager(tmp_path, adapter=adapter)
        report = manager.recover()

        assert report.phase == RecoveryPhase.FAILED
        assert not report.success
        assert not manager.trading_allowed
        assert len(report.errors) > 0

    def test_get_state(self, tmp_path):
        manager, _, _ = self._make_manager(tmp_path)
        state = manager.get_state()
        assert state["recovery_complete"] is False
        assert state["trading_allowed"] is False

        manager.recover()
        state = manager.get_state()
        assert state["recovery_complete"] is True
