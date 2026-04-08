"""
Session 52: Comprehensive test suite for Live Bridge (F-01 through F-06).

Tests cover:
  F-01: LiveBridge wired into OrderRouter; OrderCandidate → ExecutionRequest → Phase 8
  F-02: Server-side stop-loss placement after fill
  F-03: Startup recovery via RestartRecoveryManager
  F-04/F-05: Exchange balance/position hydration (risk engine uses exchange truth)
  F-06: Periodic reconciliation via ReconciliationScheduler

All tests use mocks for the Phase 8 subsystem and exchange adapter —
no real exchange calls.
"""

from __future__ import annotations

import os
import sys

# ── Headless Qt setup (must happen before any PySide6 import) ──
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# ── Mock PySide6 if not installed (CI / sandbox) ──
if "PySide6" not in sys.modules:
    try:
        import PySide6  # noqa: F401
    except ImportError:
        from unittest.mock import MagicMock as _MagicMock

        _pyside_mock = _MagicMock()

        # QObject needs to be a real class so subclassing works
        class _QObjectStub:
            def __init__(self, *args, **kwargs):
                pass

        class _SignalStub:
            def __init__(self, *args, **kwargs):
                pass
            def connect(self, *a, **k):
                pass
            def emit(self, *a, **k):
                pass

        _pyside_mock.QtCore.QObject = _QObjectStub
        _pyside_mock.QtCore.Signal = _SignalStub
        _pyside_mock.QtCore.QMetaObject = _MagicMock()
        _pyside_mock.QtCore.Qt = _MagicMock()
        _pyside_mock.QtCore.QTimer = _MagicMock()
        _pyside_mock.QtCore.Slot = lambda *a, **k: (lambda f: f)
        _pyside_mock.QtWidgets = _MagicMock()
        _pyside_mock.QtGui = _MagicMock()
        sys.modules["PySide6"] = _pyside_mock
        sys.modules["PySide6.QtCore"] = _pyside_mock.QtCore
        sys.modules["PySide6.QtWidgets"] = _pyside_mock.QtWidgets
        sys.modules["PySide6.QtGui"] = _pyside_mock.QtGui

import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import MagicMock, patch, PropertyMock, call

import pytest


# ──────────────────────────────────────────────────────────────
# FIXTURES & HELPERS
# ──────────────────────────────────────────────────────────────

@dataclass
class _FakeOrderRecord:
    """Minimal OrderRecord stand-in."""
    order_id: str = "ord-001"
    request_id: str = "req-001"
    decision_id: str = "dec-001"
    trigger_id: str = "trg-001"
    status: str = "filled"
    failure_reason: str = ""


@dataclass
class _FakeFillRecord:
    """Minimal FillRecord stand-in."""
    fill_id: str = "fill-001"
    order_id: str = "ord-001"
    price: float = 60000.0
    quantity: float = 0.05
    fee_usdt: float = 1.65
    fee_rate: float = 0.00055
    slippage_pct: float = 0.01


@dataclass
class _FakeExchangeResponse:
    """Minimal ExchangeResponse stand-in."""
    success: bool = True
    exchange_order_id: str = "exch-sl-001"
    status: str = "open"
    avg_price: float = 60000.0
    error: Any = None


class _FakeReconciliationResult:
    """Minimal ReconciliationResult stand-in."""
    def __init__(self, *, has_mismatches=False, mismatch_count=0, mismatches=None, is_clean=True):
        self.has_mismatches = has_mismatches
        self.mismatch_count = mismatch_count
        self.mismatches = mismatches or []
        self.is_clean = is_clean
        self.success = True

    def to_dict(self):
        return {
            "success": self.success,
            "mismatch_count": self.mismatch_count,
            "mismatches": [m.to_dict() for m in self.mismatches] if self.mismatches else [],
        }


class _FakeRecoveryReport:
    """Minimal RecoveryReport stand-in."""
    def __init__(self, *, success=True, trading_allowed=True, phase="completed",
                 exchange_positions=0, exchange_balance_usdt=10000.0,
                 orders_recovered=0, errors=None):
        self.success = success
        self.trading_allowed = trading_allowed
        self.phase = phase
        self.exchange_positions = exchange_positions
        self.exchange_balance_usdt = exchange_balance_usdt
        self.orders_recovered = orders_recovered
        self.errors = errors or []

    def to_dict(self):
        return {
            "success": self.success,
            "trading_allowed": self.trading_allowed,
            "phase": self.phase,
            "exchange_positions": self.exchange_positions,
            "exchange_balance_usdt": self.exchange_balance_usdt,
            "orders_recovered": self.orders_recovered,
            "errors": self.errors,
        }


def _make_candidate(**overrides):
    """Build a minimal OrderCandidate-like object for testing."""
    from core.meta_decision.order_candidate import OrderCandidate
    defaults = dict(
        symbol="BTC/USDT",
        side="buy",
        entry_type="market",
        entry_price=60000.0,
        stop_loss_price=58000.0,
        take_profit_price=64000.0,
        position_size_usdt=3000.0,
        score=0.85,
        models_fired=["momentum_breakout"],
        regime="TRENDING_UP",
        rationale="test signal",
        timeframe="30m",
        atr_value=500.0,
        approved=True,
        candidate_id=str(uuid.uuid4()),
    )
    defaults.update(overrides)
    return OrderCandidate(**defaults)


def _make_bridge_with_mocks():
    """Create a LiveBridge with mocked Phase 8 components, already initialised."""
    from core.execution.live_bridge import LiveBridge

    bridge = LiveBridge()

    # Mocks
    adapter = MagicMock(name="ExchangeAdapter")
    idempotency = MagicMock(name="IdempotencyStore")
    executor = MagicMock(name="Phase8LiveExecutor")
    recon_engine = MagicMock(name="OrderReconciliationEngine")
    recovery_mgr = MagicMock(name="RestartRecoveryManager")

    # Default behaviours
    adapter.fetch_balance.return_value = {"USDT": {"free": 10000.0, "total": 10000.0}}
    adapter.fetch_positions.return_value = []
    adapter.create_order.return_value = _FakeExchangeResponse()
    adapter.cancel_order.return_value = None

    executor.execute.return_value = (_FakeOrderRecord(), _FakeFillRecord())
    executor.get_all_orders.return_value = {}

    recovery_mgr.recover.return_value = _FakeRecoveryReport()

    recon_engine.reconcile.return_value = _FakeReconciliationResult()

    bridge.set_components(
        exchange_adapter=adapter,
        idempotency_store=idempotency,
        phase8_executor=executor,
        reconciliation_engine=recon_engine,
        recovery_manager=recovery_mgr,
    )

    return bridge, adapter, idempotency, executor, recon_engine, recovery_mgr


# ══════════════════════════════════════════════════════════════
# F-01: LiveBridge wired into OrderRouter
# ══════════════════════════════════════════════════════════════

class TestF01_OrderRouterLiveBridge:
    """F-01: OrderRouter.active_executor returns LiveBridge in live mode."""

    def test_live_mode_returns_live_bridge(self):
        from core.execution.order_router import OrderRouter
        router = OrderRouter(mode="live")
        executor = router.active_executor
        from core.execution.live_bridge import LiveBridge
        assert isinstance(executor, LiveBridge), f"Expected LiveBridge, got {type(executor)}"

    def test_paper_mode_returns_paper_executor(self):
        from core.execution.order_router import OrderRouter
        router = OrderRouter(mode="paper")
        executor = router.active_executor
        from core.execution.paper_executor import PaperExecutor
        assert isinstance(executor, PaperExecutor), f"Expected PaperExecutor, got {type(executor)}"

    def test_mode_switch_paper_to_live(self):
        from core.execution.order_router import OrderRouter
        router = OrderRouter(mode="paper")
        router.set_mode("live")
        assert router.mode == "live"
        from core.execution.live_bridge import LiveBridge
        assert isinstance(router.active_executor, LiveBridge)

    def test_mode_switch_live_to_paper(self):
        from core.execution.order_router import OrderRouter
        router = OrderRouter(mode="live")
        router.set_mode("paper")
        assert router.mode == "paper"
        from core.execution.paper_executor import PaperExecutor
        assert isinstance(router.active_executor, PaperExecutor)

    def test_invalid_mode_raises(self):
        from core.execution.order_router import OrderRouter
        router = OrderRouter(mode="paper")
        with pytest.raises(ValueError, match="invalid mode"):
            router.set_mode("demo")


class TestF01_LiveBridgeSubmission:
    """F-01: LiveBridge.submit() converts OrderCandidate → Phase 8 execute()."""

    def test_submit_calls_phase8_execute(self):
        bridge, adapter, _, executor, _, recovery = _make_bridge_with_mocks()
        # Must run recovery first so trading is allowed
        bridge.run_startup_recovery()
        candidate = _make_candidate()
        result = bridge.submit(candidate)
        assert result is True
        assert executor.execute.call_count == 1

    def test_submit_before_init_returns_false(self):
        from core.execution.live_bridge import LiveBridge
        bridge = LiveBridge()
        candidate = _make_candidate()
        assert bridge.submit(candidate) is False

    def test_submit_before_recovery_returns_false(self):
        """Trading blocked until recovery allows it."""
        bridge, *_ = _make_bridge_with_mocks()
        # set_components done, but recovery NOT run → _trading_allowed = False
        candidate = _make_candidate()
        assert bridge.submit(candidate) is False

    def test_submit_duplicate_symbol_rejected(self):
        bridge, adapter, _, executor, _, recovery = _make_bridge_with_mocks()
        bridge.run_startup_recovery()
        candidate1 = _make_candidate()
        bridge.submit(candidate1)
        # Second submit for same symbol
        candidate2 = _make_candidate()
        result = bridge.submit(candidate2)
        assert result is False

    def test_submit_creates_position_dict(self):
        bridge, adapter, _, executor, _, recovery = _make_bridge_with_mocks()
        bridge.run_startup_recovery()
        candidate = _make_candidate()
        bridge.submit(candidate)
        positions = bridge.get_open_positions()
        assert len(positions) == 1
        pos = positions[0]
        assert pos["symbol"] == "BTC/USDT"
        assert pos["side"] == "buy"
        assert pos["entry_price"] == 60000.0
        assert pos["quantity"] == 0.05

    def test_submit_builds_execution_request_correctly(self):
        """Verify the ExecutionRequest passed to Phase 8 has correct fields."""
        bridge, adapter, _, executor, _, recovery = _make_bridge_with_mocks()
        bridge.run_startup_recovery()
        candidate = _make_candidate(
            symbol="ETH/USDT",
            side="sell",
            entry_price=3000.0,
            stop_loss_price=3200.0,
            take_profit_price=2600.0,
            position_size_usdt=1500.0,
            models_fired=["pullback_long", "swing_low_continuation"],
        )
        bridge.submit(candidate)
        request = executor.execute.call_args[0][0]
        assert request.symbol == "ETH/USDT"
        assert request.side.value == "sell"
        assert request.stop_loss == 3200.0
        assert request.take_profit == 2600.0
        assert request.size_usdt == 1500.0
        assert "pullback_long" in request.strategy_name
        assert "swing_low_continuation" in request.strategy_name

    def test_submit_rejected_order_returns_false(self):
        bridge, adapter, _, executor, _, recovery = _make_bridge_with_mocks()
        bridge.run_startup_recovery()
        executor.execute.return_value = (
            _FakeOrderRecord(status="rejected", failure_reason="insufficient_margin"),
            None,
        )
        candidate = _make_candidate()
        assert bridge.submit(candidate) is False

    def test_submit_exceeds_50pct_balance_rejected(self):
        """F-04 safety cap: single trade > 50% of balance → reject."""
        bridge, adapter, _, executor, _, recovery = _make_bridge_with_mocks()
        bridge.run_startup_recovery()
        # Balance is 10000; size 6000 > 50% cap
        candidate = _make_candidate(position_size_usdt=6000.0)
        result = bridge.submit(candidate)
        assert result is False
        assert executor.execute.call_count == 0

    def test_submit_zero_balance_rejected(self):
        bridge, adapter, _, executor, _, recovery = _make_bridge_with_mocks()
        bridge.run_startup_recovery()
        adapter.fetch_balance.return_value = {"USDT": {"free": 0.0}}
        candidate = _make_candidate()
        result = bridge.submit(candidate)
        assert result is False


class TestF01_PendingConfirmation:
    """F-01: Pending confirmation flow for live mode."""

    def test_requires_confirmation_stores_pending(self):
        bridge, *_ = _make_bridge_with_mocks()
        bridge.run_startup_recovery()
        candidate = _make_candidate()
        candidate.requires_confirmation = True
        bridge.submit(candidate)
        pending = bridge.get_pending_confirmations()
        assert len(pending) == 1

    def test_confirm_and_execute_works(self):
        bridge, adapter, _, executor, _, recovery = _make_bridge_with_mocks()
        bridge.run_startup_recovery()
        candidate = _make_candidate()
        candidate.requires_confirmation = True
        candidate.candidate_id = "test-cid-123"
        bridge.submit(candidate)
        assert len(bridge.get_pending_confirmations()) == 1
        # Confirm
        result = bridge.confirm_and_execute("test-cid-123")
        assert result is True
        assert executor.execute.call_count == 1
        assert len(bridge.get_pending_confirmations()) == 0

    def test_reject_pending_removes_candidate(self):
        bridge, *_ = _make_bridge_with_mocks()
        bridge.run_startup_recovery()
        candidate = _make_candidate()
        candidate.requires_confirmation = True
        candidate.candidate_id = "reject-me"
        bridge.submit(candidate)
        result = bridge.reject_pending("reject-me")
        assert result is True
        assert len(bridge.get_pending_confirmations()) == 0


# ══════════════════════════════════════════════════════════════
# F-02: Server-Side Stop-Loss
# ══════════════════════════════════════════════════════════════

class TestF02_ServerSideStopLoss:
    """F-02: After fill, a stop-market order is placed on exchange."""

    def test_sl_order_placed_after_fill(self):
        bridge, adapter, _, executor, _, recovery = _make_bridge_with_mocks()
        bridge.run_startup_recovery()
        candidate = _make_candidate(stop_loss_price=58000.0)
        bridge.submit(candidate)

        # create_order called TWICE: once for entry (via Phase8), once for SL
        # But Phase 8 executor.execute handles entry internally;
        # the LiveBridge calls adapter.create_order only for the SL
        assert adapter.create_order.call_count == 1
        call_kwargs = adapter.create_order.call_args
        assert call_kwargs[1]["order_type"] == "stop" or call_kwargs[0][1] == "stop"

    def test_sl_order_params_correct(self):
        bridge, adapter, _, executor, _, recovery = _make_bridge_with_mocks()
        bridge.run_startup_recovery()
        candidate = _make_candidate(
            side="buy", stop_loss_price=57500.0, entry_price=60000.0,
        )
        bridge.submit(candidate)
        _, kwargs = adapter.create_order.call_args
        # For a buy position, SL close_side is sell
        assert kwargs.get("side") == "sell"
        params = kwargs.get("params", {})
        assert params.get("stopPrice") == 57500.0
        assert params.get("triggerPrice") == 57500.0
        assert params.get("reduceOnly") is True

    def test_sl_order_tracked_in_sl_orders(self):
        bridge, adapter, _, executor, _, recovery = _make_bridge_with_mocks()
        bridge.run_startup_recovery()
        candidate = _make_candidate()
        bridge.submit(candidate)
        state = bridge.get_state()
        assert state["sl_orders_active"] == 1

    def test_sl_failure_does_not_block_trade(self):
        """SL placement failure should NOT prevent the position from being opened."""
        bridge, adapter, _, executor, _, recovery = _make_bridge_with_mocks()
        bridge.run_startup_recovery()
        error_mock = MagicMock()
        error_mock.message = "exchange timeout"
        adapter.create_order.return_value = _FakeExchangeResponse(
            success=False, error=error_mock,
        )
        candidate = _make_candidate()
        result = bridge.submit(candidate)
        # Position should still be opened even though SL failed
        assert result is True
        assert len(bridge.get_open_positions()) == 1

    def test_sl_cancelled_on_position_close(self):
        bridge, adapter, _, executor, _, recovery = _make_bridge_with_mocks()
        bridge.run_startup_recovery()
        candidate = _make_candidate()
        bridge.submit(candidate)
        # Close position
        bridge.close_position("BTC/USDT")
        assert adapter.cancel_order.call_count == 1

    def test_adjust_stop_cancels_old_places_new(self):
        bridge, adapter, _, executor, _, recovery = _make_bridge_with_mocks()
        bridge.run_startup_recovery()
        candidate = _make_candidate()
        bridge.submit(candidate)
        adapter.create_order.reset_mock()
        adapter.cancel_order.reset_mock()
        # Adjust stop
        bridge.adjust_stop("BTC/USDT", 59000.0)
        assert adapter.cancel_order.call_count == 1
        assert adapter.create_order.call_count == 1


# ══════════════════════════════════════════════════════════════
# F-03: Startup Recovery
# ══════════════════════════════════════════════════════════════

class TestF03_StartupRecovery:
    """F-03: run_startup_recovery() calls Phase 8 RecoveryManager."""

    def test_recovery_calls_recovery_manager(self):
        bridge, adapter, _, executor, _, recovery_mgr = _make_bridge_with_mocks()
        result = bridge.run_startup_recovery()
        assert recovery_mgr.recover.call_count == 1
        assert result["trading_allowed"] is True

    def test_recovery_sets_trading_allowed(self):
        bridge, *_ = _make_bridge_with_mocks()
        bridge.run_startup_recovery()
        assert bridge._trading_allowed is True
        assert bridge._recovery_complete is True

    def test_recovery_failure_blocks_trading(self):
        bridge, adapter, _, executor, _, recovery_mgr = _make_bridge_with_mocks()
        recovery_mgr.recover.return_value = _FakeRecoveryReport(
            success=False, trading_allowed=False, phase="failed",
            errors=["exchange_unreachable"],
        )
        result = bridge.run_startup_recovery()
        assert result["trading_allowed"] is False
        assert bridge._trading_allowed is False

    def test_recovery_hydrates_positions(self):
        """After clean recovery, positions are fetched from exchange."""
        bridge, adapter, _, executor, _, recovery = _make_bridge_with_mocks()
        adapter.fetch_positions.return_value = [
            {
                "symbol": "ETH/USDT",
                "contracts": 2.0,
                "side": "long",
                "entryPrice": 3000.0,
                "markPrice": 3050.0,
                "unrealizedPnl": 100.0,
            }
        ]
        bridge.run_startup_recovery()
        positions = bridge.get_open_positions()
        assert len(positions) == 1
        assert positions[0]["symbol"] == "ETH/USDT"
        assert positions[0]["quantity"] == 2.0

    def test_recovery_hydrates_balance(self):
        """After clean recovery, balance is fetched from exchange."""
        bridge, adapter, _, executor, _, recovery = _make_bridge_with_mocks()
        adapter.fetch_balance.return_value = {"USDT": {"free": 25000.0}}
        bridge.run_startup_recovery()
        assert bridge.available_capital == 25000.0

    def test_recovery_before_init_returns_error(self):
        from core.execution.live_bridge import LiveBridge
        bridge = LiveBridge()
        result = bridge.run_startup_recovery()
        assert result["trading_allowed"] is False
        assert "components_not_initialised" in str(result.get("errors", []))


# ══════════════════════════════════════════════════════════════
# F-04 / F-05: Exchange Balance & Positions
# ══════════════════════════════════════════════════════════════

class TestF04_F05_ExchangeBalance:
    """F-04/F-05: available_capital, drawdown_pct from exchange."""

    def test_available_capital_fetches_exchange(self):
        bridge, adapter, *_ = _make_bridge_with_mocks()
        adapter.fetch_balance.return_value = {"USDT": {"free": 12345.0}}
        bridge.run_startup_recovery()
        assert bridge.available_capital == 12345.0
        assert adapter.fetch_balance.call_count >= 1

    def test_available_capital_uninitialised_returns_zero(self):
        from core.execution.live_bridge import LiveBridge
        assert LiveBridge().available_capital == 0.0

    def test_balance_cache_avoids_repeated_calls(self):
        bridge, adapter, *_ = _make_bridge_with_mocks()
        adapter.fetch_balance.return_value = {"USDT": {"free": 5000.0}}
        bridge.run_startup_recovery()
        call_count_after_recovery = adapter.fetch_balance.call_count
        # Multiple reads within 30s should use cache
        _ = bridge.available_capital
        _ = bridge.available_capital
        assert adapter.fetch_balance.call_count == call_count_after_recovery

    def test_balance_force_refresh_bypasses_cache(self):
        bridge, adapter, *_ = _make_bridge_with_mocks()
        adapter.fetch_balance.return_value = {"USDT": {"free": 5000.0}}
        bridge.run_startup_recovery()
        call_count = adapter.fetch_balance.call_count
        bridge._fetch_usdt_balance(force=True)
        assert adapter.fetch_balance.call_count == call_count + 1

    def test_drawdown_pct_computed_correctly(self):
        bridge, adapter, *_ = _make_bridge_with_mocks()
        adapter.fetch_balance.return_value = {"USDT": {"free": 10000.0}}
        bridge.run_startup_recovery()
        # Peak should be 10000; now drop to 9000
        adapter.fetch_balance.return_value = {"USDT": {"free": 9000.0}}
        bridge._balance_cache["ts"] = 0  # expire cache
        dd = bridge.drawdown_pct
        assert abs(dd - 10.0) < 0.01  # 10% drawdown

    def test_drawdown_zero_when_at_peak(self):
        bridge, adapter, *_ = _make_bridge_with_mocks()
        adapter.fetch_balance.return_value = {"USDT": {"free": 10000.0}}
        bridge.run_startup_recovery()
        assert bridge.drawdown_pct == 0.0

    def test_position_hydration_maps_fields_correctly(self):
        bridge, adapter, *_ = _make_bridge_with_mocks()
        adapter.fetch_positions.return_value = [
            {
                "symbol": "SOL/USDT",
                "contracts": 50.0,
                "side": "short",
                "entryPrice": 150.0,
                "markPrice": 145.0,
                "unrealizedPnl": 250.0,
            }
        ]
        bridge.run_startup_recovery()
        pos = bridge.get_open_positions()[0]
        assert pos["side"] == "sell"  # short → sell
        assert pos["entry_price"] == 150.0
        assert pos["quantity"] == 50.0
        assert pos["size_usdt"] == 50.0 * 150.0

    def test_zero_qty_positions_filtered_out(self):
        bridge, adapter, *_ = _make_bridge_with_mocks()
        adapter.fetch_positions.return_value = [
            {"symbol": "BTC/USDT", "contracts": 0, "side": "long", "entryPrice": 60000},
            {"symbol": "ETH/USDT", "contracts": 1.0, "side": "long", "entryPrice": 3000},
        ]
        bridge.run_startup_recovery()
        positions = bridge.get_open_positions()
        assert len(positions) == 1
        assert positions[0]["symbol"] == "ETH/USDT"


# ══════════════════════════════════════════════════════════════
# F-06: Periodic Reconciliation
# ══════════════════════════════════════════════════════════════

class TestF06_Reconciliation:
    """F-06: run_reconciliation() calls Phase 8 ReconciliationEngine."""

    def test_reconciliation_calls_engine(self):
        bridge, adapter, _, executor, recon, recovery = _make_bridge_with_mocks()
        bridge.run_startup_recovery()
        result = bridge.run_reconciliation()
        assert recon.reconcile.call_count == 1
        assert result.get("success") is True

    def test_reconciliation_before_init_returns_error(self):
        from core.execution.live_bridge import LiveBridge
        bridge = LiveBridge()
        result = bridge.run_reconciliation()
        assert result["success"] is False
        assert "not_initialised" in result.get("errors", [])

    def test_reconciliation_mismatch_triggers_rehydration(self):
        """On mismatch, positions should be re-hydrated from exchange."""
        bridge, adapter, _, executor, recon, recovery = _make_bridge_with_mocks()
        bridge.run_startup_recovery()
        adapter.fetch_positions.reset_mock()

        mismatch_result = _FakeReconciliationResult(
            has_mismatches=True, mismatch_count=1, is_clean=False,
        )
        recon.reconcile.return_value = mismatch_result
        bridge.run_reconciliation()
        # fetch_positions should be called again for rehydration
        assert adapter.fetch_positions.call_count >= 1

    def test_reconciliation_passes_internal_positions(self):
        """Reconciliation should pass current positions to engine."""
        bridge, adapter, _, executor, recon, recovery = _make_bridge_with_mocks()
        bridge.run_startup_recovery()
        # Add a position
        candidate = _make_candidate()
        bridge.submit(candidate)
        bridge.run_reconciliation()
        call_kwargs = recon.reconcile.call_args[1]
        internal_positions = call_kwargs.get("internal_positions", {})
        assert "BTC/USDT" in internal_positions

    def test_reconciliation_exception_handled(self):
        bridge, adapter, _, executor, recon, recovery = _make_bridge_with_mocks()
        bridge.run_startup_recovery()
        recon.reconcile.side_effect = RuntimeError("exchange down")
        result = bridge.run_reconciliation()
        assert result["success"] is False


class TestF06_ReconciliationScheduler:
    """F-06: ReconciliationScheduler periodic tick."""

    def test_scheduler_init_and_state(self):
        from core.services.reconciliation_scheduler import ReconciliationScheduler
        bridge_mock = MagicMock()
        bridge_mock.is_initialised = True
        sched = ReconciliationScheduler(live_bridge=bridge_mock, interval_ms=1000)
        state = sched.get_state()
        assert state["running"] is False
        assert state["interval_ms"] == 1000

    def test_scheduler_on_tick_calls_reconciliation(self):
        from core.services.reconciliation_scheduler import ReconciliationScheduler
        bridge_mock = MagicMock()
        bridge_mock.is_initialised = True
        bridge_mock.run_reconciliation.return_value = {"success": True, "mismatch_count": 0}
        sched = ReconciliationScheduler(live_bridge=bridge_mock)
        sched._on_tick()
        assert bridge_mock.run_reconciliation.call_count == 1
        assert sched._total_runs == 1
        assert sched._consecutive_failures == 0

    def test_scheduler_tracks_consecutive_failures(self):
        from core.services.reconciliation_scheduler import ReconciliationScheduler
        bridge_mock = MagicMock()
        bridge_mock.is_initialised = True
        bridge_mock.run_reconciliation.return_value = {"success": False, "errors": ["timeout"]}
        sched = ReconciliationScheduler(live_bridge=bridge_mock)
        sched._on_tick()
        sched._on_tick()
        assert sched._consecutive_failures == 2

    def test_scheduler_skips_uninitialised_bridge(self):
        from core.services.reconciliation_scheduler import ReconciliationScheduler
        bridge_mock = MagicMock()
        bridge_mock.is_initialised = False
        sched = ReconciliationScheduler(live_bridge=bridge_mock)
        sched._on_tick()
        assert bridge_mock.run_reconciliation.call_count == 0


# ══════════════════════════════════════════════════════════════
# POSITION MANAGEMENT (crash defense interface)
# ══════════════════════════════════════════════════════════════

class TestPositionManagement:
    """LiveBridge exposes PaperExecutor-compatible close/adjust methods."""

    def test_close_all(self):
        bridge, adapter, _, executor, _, recovery = _make_bridge_with_mocks()
        bridge.run_startup_recovery()
        bridge.submit(_make_candidate(symbol="BTC/USDT"))
        # Need separate fill for second symbol
        executor.execute.return_value = (
            _FakeOrderRecord(order_id="ord-002"),
            _FakeFillRecord(fill_id="fill-002", order_id="ord-002", price=3000.0, quantity=1.0),
        )
        bridge.submit(_make_candidate(symbol="ETH/USDT", entry_price=3000.0))
        assert len(bridge.get_open_positions()) == 2
        count = bridge.close_all()
        assert count == 2
        assert len(bridge.get_open_positions()) == 0

    def test_close_all_longs_only(self):
        bridge, adapter, _, executor, _, recovery = _make_bridge_with_mocks()
        bridge.run_startup_recovery()
        bridge.submit(_make_candidate(symbol="BTC/USDT", side="buy"))
        executor.execute.return_value = (
            _FakeOrderRecord(order_id="ord-002"),
            _FakeFillRecord(fill_id="fill-002", order_id="ord-002", price=3000.0, quantity=1.0),
        )
        bridge.submit(_make_candidate(symbol="ETH/USDT", side="sell", entry_price=3000.0,
                                       stop_loss_price=3200.0, take_profit_price=2600.0))
        count = bridge.close_all_longs()
        assert count == 1
        remaining = bridge.get_open_positions()
        assert len(remaining) == 1
        assert remaining[0]["side"] == "sell"

    def test_partial_close(self):
        bridge, adapter, _, executor, _, recovery = _make_bridge_with_mocks()
        bridge.run_startup_recovery()
        bridge.submit(_make_candidate())
        original_qty = bridge.get_open_positions()[0]["quantity"]
        adapter.create_order.reset_mock()
        adapter.create_order.return_value = _FakeExchangeResponse(avg_price=60500.0)
        result = bridge.partial_close("BTC/USDT", 0.50)
        assert result is True
        new_qty = bridge.get_open_positions()[0]["quantity"]
        assert abs(new_qty - original_qty * 0.50) < 1e-6

    def test_move_all_longs_to_breakeven(self):
        bridge, adapter, _, executor, _, recovery = _make_bridge_with_mocks()
        bridge.run_startup_recovery()
        bridge.submit(_make_candidate(entry_price=60000.0))
        adapter.create_order.reset_mock()
        adapter.cancel_order.reset_mock()
        count = bridge.move_all_longs_to_breakeven()
        assert count == 1
        pos = bridge.get_open_positions()[0]
        assert pos["stop_loss"] == 60000.0

    def test_get_stats_returns_expected_keys(self):
        bridge, *_ = _make_bridge_with_mocks()
        bridge.run_startup_recovery()
        stats = bridge.get_stats()
        expected_keys = {
            "total_trades", "win_rate", "total_pnl_usdt", "wins", "losses",
            "best_trade_usdt", "worst_trade_usdt", "avg_duration_s",
            "profit_factor", "open_positions", "drawdown_pct", "available_capital",
        }
        assert expected_keys.issubset(stats.keys())


# ══════════════════════════════════════════════════════════════
# THREAD SAFETY
# ══════════════════════════════════════════════════════════════

class TestThreadSafety:
    """LiveBridge RLock protects concurrent access."""

    def test_concurrent_balance_reads(self):
        bridge, adapter, *_ = _make_bridge_with_mocks()
        adapter.fetch_balance.return_value = {"USDT": {"free": 7777.0}}
        bridge.run_startup_recovery()
        results = []

        def read_balance():
            results.append(bridge.available_capital)

        threads = [threading.Thread(target=read_balance) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)
        assert all(r == 7777.0 for r in results)

    def test_concurrent_position_reads(self):
        bridge, adapter, _, executor, _, recovery = _make_bridge_with_mocks()
        bridge.run_startup_recovery()
        bridge.submit(_make_candidate())
        results = []

        def read_positions():
            results.append(len(bridge.get_open_positions()))

        threads = [threading.Thread(target=read_positions) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)
        assert all(r == 1 for r in results)


# ══════════════════════════════════════════════════════════════
# POSITION MONITOR AGENT INTEGRATION
# ══════════════════════════════════════════════════════════════

class TestPositionMonitorAgent:
    """position_monitor_agent uses order_router.active_executor."""

    def test_agent_imports_from_order_router(self):
        """Verify the agent no longer imports live_executor directly."""
        import inspect
        from core.agents import position_monitor_agent
        source = inspect.getsource(position_monitor_agent)
        assert "from core.execution.live_executor import" not in source
        assert "order_router" in source


# ══════════════════════════════════════════════════════════════
# ON-TICK CLIENT-SIDE BACKUP
# ══════════════════════════════════════════════════════════════

class TestOnTick:
    """LiveBridge.on_tick() updates price and checks SL/TP."""

    def test_on_tick_updates_price(self):
        bridge, adapter, _, executor, _, recovery = _make_bridge_with_mocks()
        bridge.run_startup_recovery()
        bridge.submit(_make_candidate())
        bridge.on_tick("BTC/USDT", 61000.0)
        pos = bridge.get_open_positions()[0]
        assert pos["current_price"] == 61000.0

    def test_on_tick_sl_triggers_close(self):
        bridge, adapter, _, executor, _, recovery = _make_bridge_with_mocks()
        bridge.run_startup_recovery()
        bridge.submit(_make_candidate(stop_loss_price=58000.0))
        adapter.create_order.reset_mock()
        adapter.create_order.return_value = _FakeExchangeResponse(avg_price=57900.0)
        bridge.on_tick("BTC/USDT", 57900.0)
        # Position should be closed
        assert len(bridge.get_open_positions()) == 0

    def test_on_tick_tp_triggers_close(self):
        bridge, adapter, _, executor, _, recovery = _make_bridge_with_mocks()
        bridge.run_startup_recovery()
        bridge.submit(_make_candidate(take_profit_price=64000.0))
        adapter.create_order.reset_mock()
        adapter.create_order.return_value = _FakeExchangeResponse(avg_price=64100.0)
        bridge.on_tick("BTC/USDT", 64100.0)
        assert len(bridge.get_open_positions()) == 0

    def test_on_tick_no_position_noop(self):
        bridge, *_ = _make_bridge_with_mocks()
        bridge.run_startup_recovery()
        # No exception, no crash
        bridge.on_tick("NONEXIST/USDT", 100.0)
