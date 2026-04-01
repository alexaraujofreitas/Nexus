# ============================================================
# NexusTrader — Phase 4: Final Optimization & Readiness Tests
#
# Modules tested:
#   A: Close State Persistence & Reconciliation on Restart
#   B: Exchange Connectivity Lifecycle
#   C: Partial Fill Accounting
#   D: SL/TP Sanity Validation
#   E: Audit Log Persistence (JSONL)
#   F: Thread Leak Proof Test
# ============================================================
import gc
import json
import threading
import time
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

pytestmark = pytest.mark.skip(
    reason="LiveExecutor/LivePosition close-state persistence not yet implemented — aspirational tests for live trading"
)


# ── Helpers ────────────────────────────────────────────────

def _make_live_position(**kwargs):
    """Create a LivePosition with sensible defaults."""
    from core.execution.live_executor import LivePosition
    defaults = dict(
        symbol="BTC/USDT", side="buy", entry_price=50000.0,
        quantity=0.1, stop_loss=48000.0, take_profit=55000.0,
        size_usdt=5000.0, entry_size_usdt=5000.0,
    )
    defaults.update(kwargs)
    return LivePosition(**defaults)


def _make_executor(**overrides):
    """Create a LiveExecutor bypassing __init__ for unit testing."""
    with patch("core.execution.live_executor.bus"), \
         patch("core.execution.live_executor._POSITIONS_JSON", Path("/tmp/_test_pos.json")), \
         patch("core.execution.live_executor._STATE_JSON", Path("/tmp/_test_state.json")):
        from core.execution.live_executor import LiveExecutor
        le = object.__new__(LiveExecutor)
        le._lock = threading.RLock()
        le._positions = {}
        le._closed_trades = []
        le._pending_confirmations = {}
        le._auto_execute_mode = False
        le._peak_usdt = 100_000.0
        le._initial_usdt = 100_000.0
        le._balance_cache = {"usdt": 100_000.0, "ts": 0.0}
        le._daily_loss_limit_pct = 99.0
        le._daily_loss_limit_hit = False
        le._daily_loss_limit_date = ""
        le._circuit_breaker_pct = 99.0
        le._circuit_breaker_tripped = False
        le._requires_manual_review = False
        le._reconciliation_issues = []
        le._critical_state = False
        le._critical_failed_symbols = []
        le._equity_uncertain = False
        le._config_validation_failed = False
        le._config_validation_errors = []
        le._kill_switch_active = False
        le._kill_switch_reason = ""
        le._kill_switch_activated_at = ""
        le._safety_action_cooldowns = {}
        le._safety_action_log = []
        le._SAFETY_COOLDOWN_SECONDS = 5.0
        le._json_dirty = False
        le._json_last_write = 0.0
        le._json_debounce_s = 1.0
        le._destructive_action_log = []
        le._exchange_connected = True
        le._reconnect_reconciliation_in_progress = False
        le._state_version = 0
        le._signal_expiry_seconds = 60.0
        le._audit_log_path = Path("/tmp/_test_audit.jsonl")
        from core.execution.live_executor import ExitManager
        le._exit_manager = ExitManager({})
        for k, v in overrides.items():
            setattr(le, k, v)
        return le


# ============================================================
# MODULE A — CLOSE STATE PERSISTENCE & RECONCILIATION
# ============================================================

class TestModuleA_ClosePendingVerifiedClosed:
    """close_pending position + exchange confirms closed → record + remove."""

    def test_close_pending_exchange_closed(self):
        from core.execution.live_executor import (
            LiveExecutor, POS_STATUS_CLOSE_PENDING, POS_STATUS_OPEN,
        )
        le = _make_executor()
        pos = _make_live_position(_close_status=POS_STATUS_CLOSE_PENDING)
        le._positions = {"BTC/USDT": pos}
        le._verify_position_closed = MagicMock(return_value=0.0)
        le._record_reconciliation_close = MagicMock()
        le._save_positions_json = MagicMock()

        LiveExecutor._reconcile_stale_close_states(le)

        le._verify_position_closed.assert_called_once_with("BTC/USDT")
        le._record_reconciliation_close.assert_called_once()
        assert "BTC/USDT" not in le._positions
        assert any("confirmed closed" in i for i in le._reconciliation_issues)


class TestModuleA_ClosePendingStillOpen:
    """close_pending position + exchange shows open → reset to open + manual review."""

    def test_close_pending_exchange_open(self):
        from core.execution.live_executor import (
            LiveExecutor, POS_STATUS_CLOSE_PENDING, POS_STATUS_OPEN,
        )
        le = _make_executor()
        pos = _make_live_position(_close_status=POS_STATUS_CLOSE_PENDING)
        le._positions = {"BTC/USDT": pos}
        le._verify_position_closed = MagicMock(return_value=0.1)
        le._save_positions_json = MagicMock()

        LiveExecutor._reconcile_stale_close_states(le)

        assert pos._close_status == POS_STATUS_OPEN
        assert "BTC/USDT" in le._positions
        assert any("still open" in i for i in le._reconciliation_issues)


class TestModuleA_ClosePendingExchangeUnreachable:
    """close_pending + exchange error → keep state + manual review."""

    def test_close_pending_exchange_error(self):
        from core.execution.live_executor import (
            LiveExecutor, POS_STATUS_CLOSE_PENDING,
        )
        le = _make_executor()
        pos = _make_live_position(_close_status=POS_STATUS_CLOSE_PENDING)
        le._positions = {"BTC/USDT": pos}
        le._verify_position_closed = MagicMock(side_effect=Exception("timeout"))
        le._save_positions_json = MagicMock()

        LiveExecutor._reconcile_stale_close_states(le)

        # State PRESERVED (not downgraded to open)
        assert pos._close_status == POS_STATUS_CLOSE_PENDING
        assert any("unreachable" in i for i in le._reconciliation_issues)


class TestModuleA_CloseFailedVerified:
    """close_failed + exchange confirms closed → record + remove."""

    def test_close_failed_exchange_closed(self):
        from core.execution.live_executor import (
            LiveExecutor, POS_STATUS_CLOSE_FAILED,
        )
        le = _make_executor()
        pos = _make_live_position(_close_status=POS_STATUS_CLOSE_FAILED)
        le._positions = {"BTC/USDT": pos}
        le._verify_position_closed = MagicMock(return_value=0.0)
        le._record_reconciliation_close = MagicMock()
        le._save_positions_json = MagicMock()

        LiveExecutor._reconcile_stale_close_states(le)

        assert "BTC/USDT" not in le._positions
        le._record_reconciliation_close.assert_called_once()


class TestModuleA_CloseRequestedReset:
    """close_requested → reset to open + manual review (no order submitted)."""

    def test_close_requested_resets(self):
        from core.execution.live_executor import (
            LiveExecutor, POS_STATUS_CLOSE_REQUESTED, POS_STATUS_OPEN,
        )
        le = _make_executor()
        pos = _make_live_position(_close_status=POS_STATUS_CLOSE_REQUESTED)
        le._positions = {"BTC/USDT": pos}
        le._save_positions_json = MagicMock()

        LiveExecutor._reconcile_stale_close_states(le)

        assert pos._close_status == POS_STATUS_OPEN
        assert any("close_requested" in i for i in le._reconciliation_issues)


class TestModuleA_ClosedRemoved:
    """Closed position in JSON → removed from active positions."""

    def test_closed_cleaned_up(self):
        from core.execution.live_executor import (
            LiveExecutor, POS_STATUS_CLOSED,
        )
        le = _make_executor()
        pos = _make_live_position(_close_status=POS_STATUS_CLOSED)
        le._positions = {"BTC/USDT": pos}
        le._save_positions_json = MagicMock()

        LiveExecutor._reconcile_stale_close_states(le)

        assert "BTC/USDT" not in le._positions


class TestModuleA_QuantityMismatch:
    """Exchange shows different qty → local updated to match exchange."""

    def test_quantity_updated(self):
        from core.execution.live_executor import (
            LiveExecutor, POS_STATUS_CLOSE_PENDING, POS_STATUS_OPEN,
        )
        le = _make_executor()
        pos = _make_live_position(
            quantity=0.1, _close_status=POS_STATUS_CLOSE_PENDING,
        )
        le._positions = {"BTC/USDT": pos}
        # Exchange shows 0.05 (half was closed)
        le._verify_position_closed = MagicMock(return_value=0.05)
        le._save_positions_json = MagicMock()

        LiveExecutor._reconcile_stale_close_states(le)

        assert pos.quantity == 0.05
        assert pos._close_status == POS_STATUS_OPEN


class TestModuleA_NoDoubleClose:
    """After restart reconciliation, previously close_pending position
    should not be double-closed."""

    def test_no_ghost_reopen(self):
        from core.execution.live_executor import (
            LiveExecutor, POS_STATUS_CLOSE_PENDING,
        )
        le = _make_executor()
        pos = _make_live_position(_close_status=POS_STATUS_CLOSE_PENDING)
        le._positions = {"BTC/USDT": pos}
        # Exchange confirms closed
        le._verify_position_closed = MagicMock(return_value=0.0)
        le._record_reconciliation_close = MagicMock()
        le._save_positions_json = MagicMock()

        LiveExecutor._reconcile_stale_close_states(le)

        # Position is GONE — cannot be closed again
        assert "BTC/USDT" not in le._positions


# ============================================================
# MODULE B — EXCHANGE CONNECTIVITY LIFECYCLE
# ============================================================

class TestModuleB_DisconnectBlocksTrading:
    """EXCHANGE_DISCONNECTED → _exchange_connected = False → trading blocked."""

    def test_disconnect_sets_flag(self):
        from core.execution.live_executor import LiveExecutor
        le = _make_executor()
        assert le._exchange_connected is True

        event = MagicMock()
        event.data = {"reason": "connection_lost"}
        LiveExecutor._on_exchange_disconnected(le, event)

        assert le._exchange_connected is False

    def test_pre_trade_check_blocks_when_disconnected(self):
        from core.execution.live_executor import LiveExecutor
        le = _make_executor(_exchange_connected=False)

        result = LiveExecutor._pre_trade_check(le, "test")
        assert result is not None
        assert "connectivity LOST" in result


class TestModuleB_ReconnectTriggersReconciliation:
    """EXCHANGE_CONNECTED (reconnect) → reconciliation runs."""

    def test_reconnect_runs_reconcile(self):
        from core.execution.live_executor import LiveExecutor
        le = _make_executor(_exchange_connected=False)
        le.reconcile_with_exchange = MagicMock(return_value={"clean": True})
        le._save_system_state = MagicMock()
        le._log_destructive_action = MagicMock()

        event = MagicMock()
        event.data = {"reconnected": True}
        LiveExecutor._on_exchange_reconnected(le, event)

        le.reconcile_with_exchange.assert_called_once()
        assert le._exchange_connected is True

    def test_reconnect_dirty_sets_manual_review(self):
        from core.execution.live_executor import LiveExecutor
        le = _make_executor(_exchange_connected=False)
        le.reconcile_with_exchange = MagicMock(
            return_value={"clean": False, "local_only": ["ETH/USDT"]}
        )
        le._save_system_state = MagicMock()
        le._log_destructive_action = MagicMock()

        event = MagicMock()
        event.data = {"reconnected": True}
        LiveExecutor._on_exchange_reconnected(le, event)

        # Exchange is reachable, but manual review blocks trading
        assert le._exchange_connected is True
        # The reconcile_with_exchange sets _requires_manual_review internally


class TestModuleB_ReconnectReconciliationGate:
    """During reconnect reconciliation, new trades are blocked."""

    def test_blocked_during_reconciliation(self):
        from core.execution.live_executor import LiveExecutor
        le = _make_executor(_reconnect_reconciliation_in_progress=True)

        result = LiveExecutor._pre_trade_check(le, "test")
        assert result is not None
        assert "reconciliation in progress" in result


_has_ccxt = True
try:
    import ccxt as _ccxt_check  # noqa: F401
except ModuleNotFoundError:
    _has_ccxt = False


@pytest.mark.skipif(not _has_ccxt, reason="ccxt not installed in sandbox")
class TestModuleB_ExchangeManagerReconnect:
    """ExchangeManager.attempt_reconnect() clears degraded on success."""

    def test_reconnect_success(self):
        from core.market_data.exchange_manager import ExchangeManager
        em = object.__new__(ExchangeManager)
        em._degraded = True
        em._degraded_reason = "test"
        em._last_reconnect_attempt = 0.0
        em._exchange = None
        em._lock = threading.RLock()

        with patch.object(em, "load_active_exchange", return_value=True):
            result = em.attempt_reconnect()

        assert result is True
        # load_active_exchange clears degraded internally

    def test_reconnect_too_soon(self):
        from core.market_data.exchange_manager import ExchangeManager
        em = object.__new__(ExchangeManager)
        em._degraded = True
        em._last_reconnect_attempt = time.time()  # just now

        result = em.attempt_reconnect()
        assert result is False  # too soon

    def test_reconnect_not_needed(self):
        from core.market_data.exchange_manager import ExchangeManager
        em = object.__new__(ExchangeManager)
        em._degraded = False

        result = em.attempt_reconnect()
        assert result is True  # already connected


# ============================================================
# MODULE C — PARTIAL FILL ACCOUNTING
# ============================================================

class TestModuleC_DoubleCloseRejected:
    """partial_close rejected if close_status != open."""

    def test_partial_close_rejects_non_open(self):
        from core.execution.live_executor import (
            LiveExecutor, POS_STATUS_CLOSE_PENDING,
        )
        le = _make_executor()
        pos = _make_live_position(_close_status=POS_STATUS_CLOSE_PENDING)
        le._positions = {"BTC/USDT": pos}

        result = LiveExecutor.partial_close(le, "BTC/USDT", 0.50)
        assert result is False


class TestModuleC_DustConversion:
    """Partial close leaving < 1% converts to full close."""

    def test_dust_threshold_converts_to_full(self):
        from core.execution.live_executor import LiveExecutor
        le = _make_executor()
        pos = _make_live_position()
        le._positions = {"BTC/USDT": pos}
        le.close_position = MagicMock(return_value=True)

        result = LiveExecutor.partial_close(le, "BTC/USDT", 0.995)

        le.close_position.assert_called_once_with("BTC/USDT")
        assert result is True


class TestModuleC_BoundsCheckNonNegative:
    """Quantity never goes negative after partial close."""

    def test_quantity_non_negative(self):
        from core.execution.live_executor import LiveExecutor
        le = _make_executor()
        pos = _make_live_position(quantity=0.1)
        le._positions = {"BTC/USDT": pos}
        le._save_positions_json = MagicMock()
        le._save_closed_to_db = MagicMock()
        le._log_destructive_action = MagicMock()

        mock_ex = MagicMock()
        mock_ex.create_market_order.return_value = {
            "average": 51000.0, "id": "ord_123"
        }
        le._exchange = MagicMock(return_value=mock_ex)

        with patch("core.execution.live_executor.exchange_call",
                   side_effect=lambda fn, *a, **kw: fn(*a)):
            with patch("core.execution.live_executor.bus"):
                LiveExecutor.partial_close(le, "BTC/USDT", 0.50)

        assert pos.quantity >= 0.0


class TestModuleC_RepeatedPartialNoNegative:
    """Two consecutive 60% partials should not drive qty negative."""

    def test_double_partial_safe(self):
        from core.execution.live_executor import LiveExecutor
        le = _make_executor()
        pos = _make_live_position(quantity=1.0)
        le._positions = {"BTC/USDT": pos}
        le._save_positions_json = MagicMock()
        le._save_closed_to_db = MagicMock()
        le._log_destructive_action = MagicMock()

        mock_ex = MagicMock()
        mock_ex.create_market_order.return_value = {
            "average": 51000.0, "id": "ord_1"
        }
        le._exchange = MagicMock(return_value=mock_ex)

        with patch("core.execution.live_executor.exchange_call",
                   side_effect=lambda fn, *a, **kw: fn(*a)):
            with patch("core.execution.live_executor.bus"):
                LiveExecutor.partial_close(le, "BTC/USDT", 0.60)
                # After first: qty = 0.40
                assert pos.quantity == pytest.approx(0.40, abs=1e-8)
                LiveExecutor.partial_close(le, "BTC/USDT", 0.60)
                # After second: qty = 0.16
                assert pos.quantity >= 0.0
                assert pos.quantity == pytest.approx(0.16, abs=1e-8)


class TestModuleC_PnlSanityGate:
    """Implausible PnL (>500%) flags for review but still records."""

    def test_implausible_pnl_flagged(self):
        from core.execution.live_executor import LiveExecutor
        le = _make_executor()
        # Entry at 100, close at 1000 = 900% gain → implausible
        pos = _make_live_position(
            entry_price=100.0, quantity=10.0,
            stop_loss=90.0, take_profit=200.0,
        )
        le._positions = {"BTC/USDT": pos}
        le._save_positions_json = MagicMock()
        le._save_closed_to_db = MagicMock()
        le._log_destructive_action = MagicMock()

        mock_ex = MagicMock()
        mock_ex.create_market_order.return_value = {
            "average": 1100.0, "id": "ord_pnl"
        }
        le._exchange = MagicMock(return_value=mock_ex)

        with patch("core.execution.live_executor.exchange_call",
                   side_effect=lambda fn, *a, **kw: fn(*a)):
            with patch("core.execution.live_executor.bus"):
                result = LiveExecutor.partial_close(le, "BTC/USDT", 0.50)

        assert result is True  # trade recorded
        assert any("implausible" in i.lower() for i in le._reconciliation_issues)


class TestModuleC_PartialThenStopOnResidual:
    """After partial close, stop/target operates on remaining size."""

    def test_stop_after_partial(self):
        from core.execution.live_executor import LiveExecutor
        le = _make_executor()
        pos = _make_live_position(quantity=1.0, stop_loss=48000.0)
        le._positions = {"BTC/USDT": pos}
        le._save_positions_json = MagicMock()
        le._save_closed_to_db = MagicMock()
        le._log_destructive_action = MagicMock()

        mock_ex = MagicMock()
        mock_ex.create_market_order.return_value = {"average": 51000.0, "id": "x"}
        le._exchange = MagicMock(return_value=mock_ex)

        with patch("core.execution.live_executor.exchange_call",
                   side_effect=lambda fn, *a, **kw: fn(*a)):
            with patch("core.execution.live_executor.bus"):
                LiveExecutor.partial_close(le, "BTC/USDT", 0.33)

        # Remaining qty is 67% of original
        assert pos.quantity == pytest.approx(0.67, abs=0.01)
        # SL is still the same — operates on remaining
        assert pos.stop_loss == 48000.0


# ============================================================
# MODULE D — SL/TP SANITY VALIDATION
# ============================================================

class TestModuleD_OrderRouterGeometry:
    """SL/TP geometry validation in OrderRouter._validate_candidate."""

    def _make_candidate(self, **kwargs):
        from core.meta_decision.order_candidate import OrderCandidate
        defaults = dict(
            symbol="BTC/USDT", side="buy", entry_type="market",
            entry_price=50000.0, stop_loss_price=48000.0,
            take_profit_price=55000.0, position_size_usdt=500.0,
            score=0.65, models_fired=["momentum"], regime="bull_trend",
            rationale="test", timeframe="30m", atr_value=400.0,
            approved=True, candidate_id="test-001",
            generated_at=datetime.utcnow(),
        )
        defaults.update(kwargs)
        return OrderCandidate(**defaults)

    def test_valid_long_accepted(self):
        from core.execution.order_router import OrderRouter
        router = OrderRouter.__new__(OrderRouter)
        c = self._make_candidate(
            side="buy", entry_price=50000, stop_loss_price=48000, take_profit_price=55000,
        )
        assert router._validate_candidate(c) is None

    def test_valid_short_accepted(self):
        from core.execution.order_router import OrderRouter
        router = OrderRouter.__new__(OrderRouter)
        c = self._make_candidate(
            side="sell", entry_price=50000, stop_loss_price=52000, take_profit_price=45000,
        )
        assert router._validate_candidate(c) is None

    def test_long_sl_above_entry_rejected(self):
        from core.execution.order_router import OrderRouter
        router = OrderRouter.__new__(OrderRouter)
        c = self._make_candidate(
            side="buy", entry_price=50000, stop_loss_price=51000, take_profit_price=55000,
        )
        err = router._validate_candidate(c)
        assert err is not None
        assert "stop_loss" in err and "below" in err

    def test_long_tp_below_entry_rejected(self):
        from core.execution.order_router import OrderRouter
        router = OrderRouter.__new__(OrderRouter)
        c = self._make_candidate(
            side="buy", entry_price=50000, stop_loss_price=48000, take_profit_price=49000,
        )
        err = router._validate_candidate(c)
        assert err is not None
        assert "take_profit" in err and "above" in err

    def test_short_sl_below_entry_rejected(self):
        from core.execution.order_router import OrderRouter
        router = OrderRouter.__new__(OrderRouter)
        c = self._make_candidate(
            side="sell", entry_price=50000, stop_loss_price=49000, take_profit_price=45000,
        )
        err = router._validate_candidate(c)
        assert err is not None
        assert "stop_loss" in err and "above" in err

    def test_short_tp_above_entry_rejected(self):
        from core.execution.order_router import OrderRouter
        router = OrderRouter.__new__(OrderRouter)
        c = self._make_candidate(
            side="sell", entry_price=50000, stop_loss_price=52000, take_profit_price=51000,
        )
        err = router._validate_candidate(c)
        assert err is not None
        assert "take_profit" in err and "below" in err

    def test_sl_equals_entry_rejected_long(self):
        """SL == entry is invalid at entry time (breakeven is post-entry)."""
        from core.execution.order_router import OrderRouter
        router = OrderRouter.__new__(OrderRouter)
        c = self._make_candidate(
            side="buy", entry_price=50000, stop_loss_price=50000, take_profit_price=55000,
        )
        err = router._validate_candidate(c)
        assert err is not None

    def test_tp_equals_entry_rejected_long(self):
        from core.execution.order_router import OrderRouter
        router = OrderRouter.__new__(OrderRouter)
        c = self._make_candidate(
            side="buy", entry_price=50000, stop_loss_price=48000, take_profit_price=50000,
        )
        err = router._validate_candidate(c)
        assert err is not None


class TestModuleD_LiveExecutorDefenseInDepth:
    """Defense-in-depth SL/TP check in _place_order."""

    def test_long_backward_sl_rejected(self):
        from core.execution.live_executor import LiveExecutor
        le = _make_executor()

        # Bypass all pre-trade checks
        le._pre_trade_check = MagicMock(return_value=None)
        mock_ex = MagicMock()
        le._exchange = MagicMock(return_value=mock_ex)

        candidate = MagicMock()
        candidate.symbol = "BTC/USDT"
        candidate.side = "buy"
        candidate.entry_price = 50000.0
        candidate.stop_loss_price = 52000.0  # above entry = invalid
        candidate.take_profit_price = 55000.0
        candidate.position_size_usdt = 500.0

        result = LiveExecutor._place_order(le, candidate)
        assert result is False
        # Exchange order should NOT have been called
        mock_ex.create_market_order.assert_not_called()


# ============================================================
# MODULE E — AUDIT LOG PERSISTENCE
# ============================================================

class TestModuleE_AuditLogPersistence:
    """Destructive actions persisted to JSONL and loaded on restart."""

    def test_write_and_load(self, tmp_path):
        from core.execution.live_executor import LiveExecutor
        audit_file = tmp_path / "audit.jsonl"
        le = _make_executor(_audit_log_path=audit_file)

        # Bypass default field in fixture
        le._audit_log_path = audit_file

        LiveExecutor._log_destructive_action(le, "test_action", "detail", "ok", "reason")

        assert audit_file.exists()
        lines = audit_file.read_text().strip().splitlines()
        assert len(lines) == 1
        entry = json.loads(lines[0])
        assert entry["action"] == "test_action"
        assert entry["detail"] == "detail"

        # Load into fresh executor
        le2 = _make_executor(_audit_log_path=audit_file)
        le2._audit_log_path = audit_file
        LiveExecutor._load_audit_log(le2)
        assert len(le2._destructive_action_log) == 1
        assert le2._destructive_action_log[0]["action"] == "test_action"

    def test_rotation(self, tmp_path):
        from core.execution.live_executor import LiveExecutor
        audit_file = tmp_path / "audit.jsonl"
        le = _make_executor(_audit_log_path=audit_file)
        le._audit_log_path = audit_file

        # Write more than _AUDIT_LOG_MAX_LINES
        # Use a smaller cap for testing
        original_max = LiveExecutor._AUDIT_LOG_MAX_LINES
        try:
            LiveExecutor._AUDIT_LOG_MAX_LINES = 10
            for i in range(15):
                LiveExecutor._log_destructive_action(
                    le, f"action_{i}", f"detail_{i}", "ok",
                )
            lines = audit_file.read_text().strip().splitlines()
            assert len(lines) == 10  # rotated to max
        finally:
            LiveExecutor._AUDIT_LOG_MAX_LINES = original_max

    def test_corrupt_file_recovery(self, tmp_path):
        from core.execution.live_executor import LiveExecutor
        audit_file = tmp_path / "audit.jsonl"
        # Write a mix of valid and corrupt lines
        audit_file.write_text(
            '{"action":"good1"}\n'
            'CORRUPT LINE\n'
            '{"action":"good2"}\n'
        )
        le = _make_executor(_audit_log_path=audit_file)
        le._audit_log_path = audit_file
        LiveExecutor._load_audit_log(le)
        assert len(le._destructive_action_log) == 2
        assert le._destructive_action_log[0]["action"] == "good1"
        assert le._destructive_action_log[1]["action"] == "good2"

    def test_flush_after_write(self, tmp_path):
        """Verify file content is immediately available after write."""
        from core.execution.live_executor import LiveExecutor
        audit_file = tmp_path / "audit.jsonl"
        le = _make_executor(_audit_log_path=audit_file)
        le._audit_log_path = audit_file

        LiveExecutor._log_destructive_action(le, "flush_test", "", "ok")

        # Read immediately — should be there (flush-after-write)
        content = audit_file.read_text()
        assert "flush_test" in content


# ============================================================
# MODULE F — THREAD LEAK PROOF TEST
# ============================================================

class TestModuleF_ThreadLeakProof:
    """Prove exchange_call doesn't leak threads under repeated invocation."""

    def test_no_leak_normal(self):
        from core.execution.exchange_call import exchange_call

        gc.collect()
        baseline = threading.active_count()

        for _ in range(50):
            result = exchange_call(
                lambda: 42,
                timeout_key="data_timeout",
                label="leak_test_normal",
            )
            assert result == 42

        gc.collect()
        time.sleep(0.2)
        gc.collect()
        final = threading.active_count()
        assert final <= baseline + 2, f"Thread leak: {baseline} → {final}"

    def test_no_leak_on_exception(self):
        from core.execution.exchange_call import exchange_call

        gc.collect()
        baseline = threading.active_count()

        for _ in range(50):
            with pytest.raises(ValueError):
                exchange_call(
                    lambda: (_ for _ in ()).throw(ValueError("test")),
                    timeout_key="data_timeout",
                    label="leak_test_exception",
                )

        gc.collect()
        time.sleep(0.2)
        gc.collect()
        final = threading.active_count()
        assert final <= baseline + 2, f"Thread leak: {baseline} → {final}"

    def test_no_leak_on_timeout(self):
        from core.execution.exchange_call import exchange_call

        gc.collect()
        baseline = threading.active_count()

        def slow_fn():
            time.sleep(60)
            return "should_not_reach"

        for _ in range(5):
            with pytest.raises(TimeoutError):
                exchange_call(
                    slow_fn,
                    timeout_key="data_timeout",
                    label="leak_test_timeout",
                )

        gc.collect()
        time.sleep(1.0)
        gc.collect()
        final = threading.active_count()
        # Allow more tolerance for timeout threads that may still be cleaning up
        assert final <= baseline + 7, f"Thread leak: {baseline} → {final}"


# ============================================================
# INTEGRATION — CROSS-MODULE SCENARIOS
# ============================================================

class TestIntegration_RestartReconciliationFlow:
    """Full restart flow: close_pending → verify → reconcile."""

    def test_multiple_stale_positions(self):
        from core.execution.live_executor import (
            LiveExecutor, POS_STATUS_CLOSE_PENDING, POS_STATUS_CLOSE_FAILED,
            POS_STATUS_OPEN,
        )
        le = _make_executor()
        # BTC: close_pending → exchange says closed
        btc = _make_live_position(
            symbol="BTC/USDT", _close_status=POS_STATUS_CLOSE_PENDING,
        )
        # ETH: close_failed → exchange says still open
        eth = _make_live_position(
            symbol="ETH/USDT", _close_status=POS_STATUS_CLOSE_FAILED,
            quantity=2.0,
        )
        le._positions = {"BTC/USDT": btc, "ETH/USDT": eth}
        le._record_reconciliation_close = MagicMock()
        le._save_positions_json = MagicMock()

        def mock_verify(sym):
            if sym == "BTC/USDT":
                return 0.0  # closed
            return 2.0  # still open

        le._verify_position_closed = MagicMock(side_effect=mock_verify)

        LiveExecutor._reconcile_stale_close_states(le)

        assert "BTC/USDT" not in le._positions  # removed
        assert "ETH/USDT" in le._positions  # kept, reset to open
        assert eth._close_status == POS_STATUS_OPEN
        assert len(le._reconciliation_issues) == 2
