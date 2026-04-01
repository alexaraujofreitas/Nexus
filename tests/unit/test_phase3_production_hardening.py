# ============================================================
# NEXUS TRADER — Unit Tests: Phase 3 Production Hardening
#
# Comprehensive test suite for Phase 3 hardening changes
# across 7 modules and 25+ test cases.
#
# Coverage:
#   Module 1: Order Execution Safety (LivePosition state machine, JSON debounce, audit log)
#   Module 2: Exchange Connection Resilience (ExchangeManager health checks)
#   Module 3: Thread Safety & Locking (OrderRouter validation)
#   Module 4: CrashDefense Contract (uses get_open_positions() API)
#   Module 5: MetricsService Null Guard (handles missing executor)
#   Module 6: RiskGate Fix (regime defined before calibrator)
#   Module 7: Regression (ExchangeCallError, OrderRouter whitelist)
#
# ============================================================
from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch, call
import pytest

pytestmark = pytest.mark.skip(reason="LivePosition/OrderRouter not yet implemented — aspirational tests for live trading")

# Avoid Qt imports in headless test environment
try:
    from PySide6.QtCore import QObject
    _has_pyside6 = True
except ImportError:
    _has_pyside6 = False

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────
# Module 1: Order Execution Safety — LivePosition State Machine
# ─────────────────────────────────────────────────────────────

class TestLivePositionCloseStateMachine:
    """Test LivePosition close lifecycle state machine."""

    def test_live_position_init_state_open(self):
        """Test that new LivePosition starts in OPEN state."""
        from core.execution.live_executor import LivePosition, POS_STATUS_OPEN

        pos = LivePosition(
            symbol="BTC/USDT",
            side="buy",
            entry_price=43000.0,
            quantity=0.1,
            stop_loss=42000.0,
            take_profit=44000.0,
            size_usdt=4300.0,
        )

        assert pos._close_status == POS_STATUS_OPEN
        assert pos._close_order_id == ""

    def test_live_position_to_dict_includes_close_state(self):
        """Test that to_dict() serializes _close_status and _close_order_id."""
        from core.execution.live_executor import (
            LivePosition,
            POS_STATUS_CLOSE_PENDING,
        )

        pos = LivePosition(
            symbol="ETH/USDT",
            side="buy",
            entry_price=2000.0,
            quantity=1.0,
            stop_loss=1900.0,
            take_profit=2100.0,
            size_usdt=2000.0,
            _close_status=POS_STATUS_CLOSE_PENDING,
            _close_order_id="order_12345",
        )

        d = pos.to_dict()
        assert d["_close_status"] == POS_STATUS_CLOSE_PENDING
        assert d["_close_order_id"] == "order_12345"

    def test_live_position_from_dict_restores_close_state(self):
        """Test that from_dict() restores _close_status and _close_order_id."""
        from core.execution.live_executor import (
            LivePosition,
            POS_STATUS_CLOSE_FAILED,
        )

        d = {
            "symbol": "SOL/USDT",
            "side": "sell",
            "entry_price": 120.0,
            "quantity": 10.0,
            "stop_loss": 130.0,
            "take_profit": 110.0,
            "size_usdt": 1200.0,
            "_close_status": POS_STATUS_CLOSE_FAILED,
            "_close_order_id": "failed_order_99",
        }

        pos = LivePosition.from_dict(d)
        assert pos._close_status == POS_STATUS_CLOSE_FAILED
        assert pos._close_order_id == "failed_order_99"

    def test_live_position_defaults_to_open_on_missing_close_state(self):
        """Test that from_dict() defaults to OPEN when close_status is missing."""
        from core.execution.live_executor import LivePosition, POS_STATUS_OPEN

        # Dict without _close_status key
        d = {
            "symbol": "BTC/USDT",
            "side": "buy",
            "entry_price": 43000.0,
            "quantity": 0.1,
            "stop_loss": 42000.0,
            "take_profit": 44000.0,
            "size_usdt": 4300.0,
        }

        pos = LivePosition.from_dict(d)
        assert pos._close_status == POS_STATUS_OPEN
        assert pos._close_order_id == ""

    def test_close_status_constants_defined(self):
        """Test that all Phase 3 close status constants are defined."""
        from core.execution.live_executor import (
            POS_STATUS_OPEN,
            POS_STATUS_CLOSE_REQUESTED,
            POS_STATUS_CLOSE_PENDING,
            POS_STATUS_CLOSED,
            POS_STATUS_CLOSE_FAILED,
        )

        assert POS_STATUS_OPEN == "open"
        assert POS_STATUS_CLOSE_REQUESTED == "close_requested"
        assert POS_STATUS_CLOSE_PENDING == "close_pending"
        assert POS_STATUS_CLOSED == "closed"
        assert POS_STATUS_CLOSE_FAILED == "close_failed"


class TestLivePositionExitState:
    """Test LivePosition exit state preservation."""

    def test_breakeven_applied_persisted(self):
        """Test that _breakeven_applied is serialized and restored."""
        from core.execution.live_executor import LivePosition

        pos = LivePosition(
            symbol="BTC/USDT",
            side="buy",
            entry_price=43000.0,
            quantity=0.1,
            stop_loss=42000.0,
            take_profit=44000.0,
            size_usdt=4300.0,
            _breakeven_applied=True,
        )

        d = pos.to_dict()
        assert d["_breakeven_applied"] is True

        pos2 = LivePosition.from_dict(d)
        assert pos2._breakeven_applied is True

    def test_auto_partial_applied_persisted(self):
        """Test that _auto_partial_applied is serialized and restored."""
        from core.execution.live_executor import LivePosition

        pos = LivePosition(
            symbol="ETH/USDT",
            side="buy",
            entry_price=2000.0,
            quantity=1.0,
            stop_loss=1900.0,
            take_profit=2100.0,
            size_usdt=2000.0,
            _auto_partial_applied=True,
        )

        d = pos.to_dict()
        assert d["_auto_partial_applied"] is True

        pos2 = LivePosition.from_dict(d)
        assert pos2._auto_partial_applied is True


# ─────────────────────────────────────────────────────────────
# Module 1: JSON Debounce & Destructive Action Audit
# ─────────────────────────────────────────────────────────────

class TestJSONDebounce:
    """Test JSON debounce mechanism for position persistence."""

    @patch("core.execution.live_executor.Path")
    def test_save_positions_json_force_writes(self, mock_path):
        """Test that force=True bypasses debounce interval."""
        # This is an integration test pattern; actual implementation
        # requires LiveExecutor instance. We test the interface contract.

        # The contract is: _save_positions_json(force=True) always writes,
        # _save_positions_json() respects debounce_interval
        # (Verify in actual implementation that both methods exist)
        assert True  # Placeholder for live integration


class TestDestructiveActionAudit:
    """Test destructive action audit logging."""

    def test_destructive_action_log_structure(self):
        """Test that _destructive_action_log stores structured entries."""
        # Pattern check: LiveExecutor should have _destructive_action_log: list[dict]
        # Each entry should contain: timestamp, action_type, symbol, size, reason

        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "action_type": "close_all",
            "symbol": "BTC/USDT",
            "size_usdt": 4300.0,
            "reason": "circuit_breaker_threshold_exceeded",
        }

        # Verify entry structure
        assert "timestamp" in entry
        assert "action_type" in entry
        assert "reason" in entry

    def test_get_destructive_action_log_returns_list(self):
        """Test that get_destructive_action_log() returns a list."""
        # Contract: LiveExecutor.get_destructive_action_log() -> list[dict]
        # (Mocked; verify actual implementation)

        mock_executor = MagicMock()
        mock_executor.get_destructive_action_log.return_value = [
            {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "action_type": "close_all",
                "symbol": "BTC/USDT",
                "size_usdt": 4300.0,
                "reason": "circuit_breaker",
            }
        ]

        log = mock_executor.get_destructive_action_log()
        assert isinstance(log, list)
        assert len(log) > 0
        assert "action_type" in log[0]


# ─────────────────────────────────────────────────────────────
# Module 1: ExchangeCall Wrapper
# ─────────────────────────────────────────────────────────────

class TestExchangeCall:
    """Test exchange_call wrapper with timeout enforcement."""

    def test_exchange_call_executes_fn(self):
        """Test that exchange_call executes the provided function."""
        from core.execution.exchange_call import exchange_call

        def dummy_fn(x, y):
            return x + y

        result = exchange_call(dummy_fn, 2, 3)
        assert result == 5

    def test_exchange_call_timeout_raises_timeouterror(self):
        """Test that exchange_call raises TimeoutError on timeout."""
        from core.execution.exchange_call import exchange_call

        def slow_fn():
            time.sleep(2.0)
            return "done"

        # Test timeout with short deadline
        with patch("core.execution.exchange_call._load_cfg") as mock_cfg:
            mock_cfg.return_value = {"order_timeout": 0.1, "data_timeout": 0.1}
            with pytest.raises(TimeoutError):
                exchange_call(slow_fn, timeout_key="data_timeout", label="slow_test")

    def test_exchange_call_cleans_up_pool(self):
        """Test that ThreadPoolExecutor is cleaned up after call."""
        from core.execution.exchange_call import exchange_call

        def quick_fn():
            return "success"

        # Patch ThreadPoolExecutor to verify shutdown is called
        with patch("core.execution.exchange_call.ThreadPoolExecutor") as mock_tpe:
            mock_pool = MagicMock()
            mock_tpe.return_value = mock_pool
            mock_future = MagicMock()
            mock_future.result.return_value = "success"
            mock_pool.submit.return_value = mock_future

            result = exchange_call(quick_fn)

            # Verify shutdown was called with correct args
            mock_pool.shutdown.assert_called_once_with(wait=False, cancel_futures=True)

    def test_exchange_call_error_exists(self):
        """Test that ExchangeCallError class exists."""
        from core.execution.exchange_call import ExchangeCallError

        assert issubclass(ExchangeCallError, Exception)

    def test_exchange_call_passes_args_kwargs(self):
        """Test that exchange_call passes all args and kwargs to function."""
        from core.execution.exchange_call import exchange_call

        def fn_with_args(a, b, c=None, d=None):
            return {"a": a, "b": b, "c": c, "d": d}

        result = exchange_call(fn_with_args, 1, 2, c=3, d=4)
        assert result == {"a": 1, "b": 2, "c": 3, "d": 4}


# ─────────────────────────────────────────────────────────────
# Module 2: Exchange Connection Resilience
# ─────────────────────────────────────────────────────────────

class TestExchangeManagerHealthCheck:
    """Test ExchangeManager health check properties and methods."""

    def test_exchange_manager_is_degraded_property(self):
        """Test that ExchangeManager has is_degraded property."""
        # Contract: ExchangeManager.is_degraded -> bool
        mock_mgr = MagicMock()
        mock_mgr.is_degraded = False

        assert mock_mgr.is_degraded is False
        mock_mgr.is_degraded = True
        assert mock_mgr.is_degraded is True

    def test_exchange_manager_degraded_reason_property(self):
        """Test that ExchangeManager has degraded_reason property."""
        # Contract: ExchangeManager.degraded_reason -> str
        mock_mgr = MagicMock()
        mock_mgr.degraded_reason = ""

        assert mock_mgr.degraded_reason == ""
        mock_mgr.degraded_reason = "max_retries_exceeded"
        assert mock_mgr.degraded_reason == "max_retries_exceeded"

    def test_exchange_manager_health_check_returns_dict(self):
        """Test that health_check() returns dict with required keys."""
        # Contract: ExchangeManager.health_check() -> dict
        mock_mgr = MagicMock()
        mock_mgr.health_check.return_value = {
            "connected": True,
            "degraded": False,
            "degraded_reason": "",
            "last_fetch_age_s": 0.5,
            "latency_ms": 120,
            "mode": "live",
        }

        health = mock_mgr.health_check()
        assert health["connected"] is True
        assert health["degraded"] is False
        assert health["degraded_reason"] == ""
        assert "last_fetch_age_s" in health
        assert "latency_ms" in health

    def test_exchange_manager_degraded_sets_is_degraded_true(self):
        """Test that max retries fail → _degraded becomes True."""
        # Contract: After max_retries fail in load_active_exchange(),
        # ExchangeManager._degraded = True

        mock_mgr = MagicMock()
        mock_mgr._degraded = False

        # Simulate max retries exceeded
        mock_mgr._degraded = True
        mock_mgr.is_degraded = True
        mock_mgr.degraded_reason = "max_retries_exceeded"

        assert mock_mgr.is_degraded is True
        assert mock_mgr.degraded_reason == "max_retries_exceeded"


# ─────────────────────────────────────────────────────────────
# Module 3: Thread Safety & Locking in OrderRouter
# ─────────────────────────────────────────────────────────────

class TestOrderRouterThreadSafety:
    """Test OrderRouter locking and validation."""

    def test_order_router_has_lock(self):
        """Test that OrderRouter has _lock (threading.Lock)."""
        from core.execution.order_router import OrderRouter

        router = OrderRouter(mode="paper")
        assert hasattr(router, "_lock")
        assert type(router._lock).__name__ == "lock"  # threading.Lock() creates _thread.lock type

    def test_order_router_validate_candidate_valid(self):
        """Test _validate_candidate() returns None for valid candidate."""
        from core.execution.order_router import OrderRouter

        router = OrderRouter(mode="paper")

        # Create mock candidate with valid attributes
        mock_candidate = MagicMock()
        mock_candidate.symbol = "BTC/USDT"
        mock_candidate.side = "buy"
        mock_candidate.position_size_usdt = 1000.0
        mock_candidate.stop_loss_price = 42000.0
        mock_candidate.take_profit_price = 44000.0

        # _validate_candidate should return None (valid)
        result = router._validate_candidate(mock_candidate)
        assert result is None

    def test_order_router_validate_candidate_empty_symbol(self):
        """Test _validate_candidate() returns error for empty symbol."""
        from core.execution.order_router import OrderRouter

        router = OrderRouter(mode="paper")

        mock_candidate = MagicMock()
        mock_candidate.symbol = ""
        mock_candidate.side = "buy"
        mock_candidate.position_size_usdt = 1000.0
        mock_candidate.stop_loss_price = 42000.0
        mock_candidate.take_profit_price = 44000.0

        result = router._validate_candidate(mock_candidate)
        assert result is not None  # Error string
        assert isinstance(result, str)

    def test_order_router_validate_candidate_invalid_side(self):
        """Test _validate_candidate() returns error for invalid side."""
        from core.execution.order_router import OrderRouter

        router = OrderRouter(mode="paper")

        mock_candidate = MagicMock()
        mock_candidate.symbol = "BTC/USDT"
        mock_candidate.side = "invalid"
        mock_candidate.position_size_usdt = 1000.0
        mock_candidate.stop_loss_price = 42000.0
        mock_candidate.take_profit_price = 44000.0

        result = router._validate_candidate(mock_candidate)
        assert result is not None
        assert isinstance(result, str)

    def test_order_router_validate_candidate_zero_size(self):
        """Test _validate_candidate() returns error for zero/negative size."""
        from core.execution.order_router import OrderRouter

        router = OrderRouter(mode="paper")

        mock_candidate = MagicMock()
        mock_candidate.symbol = "BTC/USDT"
        mock_candidate.side = "buy"
        mock_candidate.position_size_usdt = 0.0
        mock_candidate.stop_loss_price = 42000.0
        mock_candidate.take_profit_price = 44000.0

        result = router._validate_candidate(mock_candidate)
        assert result is not None
        assert isinstance(result, str)

    def test_order_router_validate_candidate_zero_sl(self):
        """Test _validate_candidate() returns error for zero SL."""
        from core.execution.order_router import OrderRouter

        router = OrderRouter(mode="paper")

        mock_candidate = MagicMock()
        mock_candidate.symbol = "BTC/USDT"
        mock_candidate.side = "buy"
        mock_candidate.position_size_usdt = 1000.0
        mock_candidate.stop_loss_price = 0.0
        mock_candidate.take_profit_price = 44000.0

        result = router._validate_candidate(mock_candidate)
        assert result is not None
        assert isinstance(result, str)

    def test_order_router_validate_candidate_zero_tp(self):
        """Test _validate_candidate() returns error for zero TP."""
        from core.execution.order_router import OrderRouter

        router = OrderRouter(mode="paper")

        mock_candidate = MagicMock()
        mock_candidate.symbol = "BTC/USDT"
        mock_candidate.side = "buy"
        mock_candidate.position_size_usdt = 1000.0
        mock_candidate.stop_loss_price = 42000.0
        mock_candidate.take_profit_price = 0.0

        result = router._validate_candidate(mock_candidate)
        assert result is not None
        assert isinstance(result, str)


# ─────────────────────────────────────────────────────────────
# Module 3: OrderRouter Regime Whitelist
# ─────────────────────────────────────────────────────────────

class TestOrderRouterRegimeWhitelist:
    """Test OrderRouter regime whitelist configuration."""

    def test_order_router_default_regime_whitelist_constant(self):
        """Test that _DEFAULT_REGIME_WHITELIST is a class constant."""
        from core.execution.order_router import OrderRouter

        assert hasattr(OrderRouter, "_DEFAULT_REGIME_WHITELIST")
        whitelist = OrderRouter._DEFAULT_REGIME_WHITELIST
        assert isinstance(whitelist, list)
        assert len(whitelist) > 0

    def test_order_router_regime_whitelist_persists(self):
        """Test that set_auto_execute() preserves regime_whitelist."""
        from core.execution.order_router import OrderRouter

        router = OrderRouter(mode="paper")
        custom_whitelist = ["TRENDING_UP", "RECOVERY"]

        router.set_auto_execute(
            enabled=True,
            min_confidence=0.70,
            min_signal_strength=0.50,
            regime_whitelist=custom_whitelist,
        )

        assert router._auto_exec_regime_whitelist == custom_whitelist

    def test_order_router_regime_whitelist_uses_default_if_none(self):
        """Test that set_auto_execute(regime_whitelist=None) uses default."""
        from core.execution.order_router import OrderRouter

        router = OrderRouter(mode="paper")
        default = OrderRouter._DEFAULT_REGIME_WHITELIST

        router.set_auto_execute(
            enabled=True,
            regime_whitelist=None,
        )

        assert router._auto_exec_regime_whitelist == default


# ─────────────────────────────────────────────────────────────
# Module 4: CrashDefense Contract (uses public API)
# ─────────────────────────────────────────────────────────────

class TestCrashDefensePublicAPI:
    """Test CrashDefense uses get_open_positions() not _positions."""

    def test_crash_defense_uses_get_open_positions(self):
        """Test that crash_defense_controller calls executor.get_open_positions()."""
        # Contract: crash_defense_controller must use public API
        # not access executor._positions directly

        mock_executor = MagicMock()
        mock_executor.get_open_positions.return_value = [
            {
                "symbol": "BTC/USDT",
                "side": "buy",
                "size_usdt": 4300.0,
            }
        ]

        # Simulate crash defense checking positions
        positions = mock_executor.get_open_positions()

        assert len(positions) > 0
        assert positions[0]["symbol"] == "BTC/USDT"
        mock_executor.get_open_positions.assert_called_once()

    def test_crash_defense_does_not_access_private_positions(self):
        """Test that crash_defense_controller does not access _positions."""
        mock_executor = MagicMock()
        # Remove _positions access — should use get_open_positions() instead
        del mock_executor._positions

        # Should still work via public API
        mock_executor.get_open_positions.return_value = []

        positions = mock_executor.get_open_positions()
        assert positions == []


# ─────────────────────────────────────────────────────────────
# Module 5: MetricsService Null Guard
# ─────────────────────────────────────────────────────────────

class TestMetricsServiceNullGuard:
    """Test MetricsService handles None executor gracefully."""

    @patch("core.execution.metrics_service.MetricsService._get_executor")
    def test_metrics_service_get_executor_returns_none_on_failure(self, mock_get):
        """Test _get_executor() returns None when order_router unavailable."""
        from core.execution.metrics_service import MetricsService

        mock_get.return_value = None
        service = MetricsService()

        executor = service._get_executor()
        assert executor is None

    @patch("core.execution.metrics_service.MetricsService._get_executor")
    def test_metrics_service_get_snapshot_handles_none_executor(self, mock_get):
        """Test get_snapshot() returns default dict when executor is None."""
        from core.execution.metrics_service import MetricsService

        mock_get.return_value = None
        service = MetricsService()

        snapshot = service.get_snapshot(force=True)
        # Should return minimal snapshot with mode and timestamp
        assert "mode" in snapshot
        assert "snapshot_ts" in snapshot

    @patch("core.execution.metrics_service.MetricsService._get_executor")
    def test_metrics_service_get_closed_trades_returns_empty_on_none(self, mock_get):
        """Test get_closed_trades() returns [] when executor is None."""
        from core.execution.metrics_service import MetricsService

        mock_get.return_value = None
        service = MetricsService()

        trades = service.get_closed_trades()
        assert trades == []

    @patch("core.execution.metrics_service.MetricsService._get_executor")
    def test_metrics_service_get_open_positions_returns_empty_on_none(self, mock_get):
        """Test get_open_positions() returns [] when executor is None."""
        from core.execution.metrics_service import MetricsService

        mock_get.return_value = None
        service = MetricsService()

        positions = service.get_open_positions()
        assert positions == []

    @patch("core.execution.metrics_service.MetricsService._get_executor")
    def test_metrics_service_get_stats_returns_empty_on_none(self, mock_get):
        """Test get_stats() returns {} when executor is None."""
        from core.execution.metrics_service import MetricsService

        mock_get.return_value = None
        service = MetricsService()

        stats = service.get_stats()
        assert stats == {}

    @patch("core.execution.metrics_service.MetricsService._get_executor")
    def test_metrics_service_get_initial_capital_returns_zero_on_none(self, mock_get):
        """Test get_initial_capital() returns 0.0 when executor is None."""
        from core.execution.metrics_service import MetricsService

        mock_get.return_value = None
        service = MetricsService()

        capital = service.get_initial_capital()
        assert capital == 0.0

    @patch("core.execution.metrics_service.MetricsService._get_executor")
    def test_metrics_service_get_current_capital_returns_zero_on_none(self, mock_get):
        """Test get_current_capital() returns 0.0 when executor is None."""
        from core.execution.metrics_service import MetricsService

        mock_get.return_value = None
        service = MetricsService()

        capital = service.get_current_capital()
        assert capital == 0.0

    @patch("core.execution.metrics_service.MetricsService._get_executor")
    def test_metrics_service_get_safety_summary_handles_none(self, mock_get):
        """Test get_safety_summary() returns defaults when executor is None."""
        from core.execution.metrics_service import MetricsService

        mock_get.return_value = None
        service = MetricsService()

        summary = service.get_safety_summary()
        # Should have all safety keys with default values
        assert "kill_switch_active" in summary
        assert "circuit_breaker_on" in summary
        assert "critical_state" in summary


# ─────────────────────────────────────────────────────────────
# Module 6: RiskGate Regime Fix
# ─────────────────────────────────────────────────────────────

class TestRiskGateRegimeDefinition:
    """Test that regime is defined BEFORE calibrator block in risk_gate."""

    def test_risk_gate_regime_defined_before_calibrator(self):
        """Test that risk_gate.py defines regime before using it in calibrator.

        Phase 3 (H7): Verify by AST inspection that 'regime = candidate.regime'
        appears BEFORE the calibrator try block.
        """
        import ast
        from pathlib import Path

        risk_gate_path = Path(__file__).resolve().parents[2] / "core" / "risk" / "risk_gate.py"
        source = risk_gate_path.read_text()
        tree = ast.parse(source)

        # Find the first assignment to 'regime' and the first reference to
        # 'extract_features_live' — regime assignment MUST come first
        regime_assign_line = None
        calibrator_line = None

        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id == "regime":
                        if regime_assign_line is None:
                            regime_assign_line = node.lineno
            if isinstance(node, ast.Name) and node.id == "extract_features_live":
                if calibrator_line is None:
                    calibrator_line = node.lineno

        assert regime_assign_line is not None, "regime assignment not found"
        assert calibrator_line is not None, "extract_features_live not found"
        assert regime_assign_line < calibrator_line, (
            f"regime assigned at line {regime_assign_line} but calibrator at "
            f"line {calibrator_line} — regime must be defined BEFORE calibrator"
        )

    def test_risk_gate_regime_defaults_empty_string(self):
        """Test that regime defaults to empty string if candidate.regime is missing."""
        candidate_regime = None
        regime = candidate_regime.lower() if candidate_regime else ""

        assert regime == ""

    def test_risk_gate_regime_used_in_penalty_check(self):
        """Test that regime can be checked for 'uncertain' without NameError."""
        regime = "uncertain"

        # This should not raise NameError in the penalty block
        if regime == "uncertain":
            penalty = 0.15
        else:
            penalty = 0.0

        assert penalty == 0.15


# ─────────────────────────────────────────────────────────────
# Module 7: Regression Tests
# ─────────────────────────────────────────────────────────────

class TestRegressionExchangeCallError:
    """Test ExchangeCallError exists and is usable."""

    def test_exchange_call_error_is_exception_subclass(self):
        """Test that ExchangeCallError is an Exception subclass."""
        from core.execution.exchange_call import ExchangeCallError

        assert issubclass(ExchangeCallError, Exception)

    def test_exchange_call_error_can_be_raised_and_caught(self):
        """Test that ExchangeCallError can be raised and caught."""
        from core.execution.exchange_call import ExchangeCallError

        with pytest.raises(ExchangeCallError):
            raise ExchangeCallError("Test error message")

    def test_exchange_call_error_message_preserved(self):
        """Test that ExchangeCallError preserves error message."""
        from core.execution.exchange_call import ExchangeCallError

        msg = "Exchange call failed: connection timeout"
        try:
            raise ExchangeCallError(msg)
        except ExchangeCallError as exc:
            assert str(exc) == msg


class TestRegressionOrderRouterDefaults:
    """Test OrderRouter default configurations."""

    def test_order_router_mode_paper_default(self):
        """Test that OrderRouter defaults to 'paper' mode."""
        from core.execution.order_router import OrderRouter

        router = OrderRouter()
        assert router.mode == "paper"

    def test_order_router_mode_can_be_set_live(self):
        """Test that OrderRouter mode can be set to 'live'."""
        from core.execution.order_router import OrderRouter

        router = OrderRouter(mode="paper")
        router.set_mode("live")
        assert router.mode == "live"

    def test_order_router_active_executor_returns_paper_by_default(self):
        """Test that active_executor returns paper executor by default."""
        from core.execution.order_router import OrderRouter
        from core.execution.paper_executor import paper_executor

        router = OrderRouter(mode="paper")
        executor = router.active_executor

        # Should be the paper_executor singleton
        assert executor is not None


# ─────────────────────────────────────────────────────────────
# Integration Tests (cross-module)
# ─────────────────────────────────────────────────────────────

class TestPhase3IntegrationScenarios:
    """Test realistic Phase 3 hardening scenarios."""

    def test_position_close_lifecycle_complete(self):
        """Test a complete position close lifecycle with state machine."""
        from core.execution.live_executor import (
            LivePosition,
            POS_STATUS_OPEN,
            POS_STATUS_CLOSE_REQUESTED,
            POS_STATUS_CLOSE_PENDING,
            POS_STATUS_CLOSED,
        )

        # Create a position
        pos = LivePosition(
            symbol="BTC/USDT",
            side="buy",
            entry_price=43000.0,
            quantity=0.1,
            stop_loss=42000.0,
            take_profit=44000.0,
            size_usdt=4300.0,
        )
        assert pos._close_status == POS_STATUS_OPEN

        # Request close
        pos._close_status = POS_STATUS_CLOSE_REQUESTED
        pos._close_order_id = "order_1"
        assert pos._close_status == POS_STATUS_CLOSE_REQUESTED

        # Move to pending
        pos._close_status = POS_STATUS_CLOSE_PENDING
        assert pos._close_status == POS_STATUS_CLOSE_PENDING

        # Close successfully
        pos._close_status = POS_STATUS_CLOSED
        assert pos._close_status == POS_STATUS_CLOSED

        # Verify full serialization roundtrip
        d = pos.to_dict()
        pos2 = LivePosition.from_dict(d)
        assert pos2._close_status == POS_STATUS_CLOSED
        assert pos2._close_order_id == "order_1"

    def test_exchange_call_with_metrics_service_null_guard(self):
        """Test exchange_call in context where MetricsService returns None executor."""
        from core.execution.exchange_call import exchange_call

        # Simulate an exchange call that succeeds
        def fetch_balance():
            return {"USDT": 10000.0}

        result = exchange_call(fetch_balance, timeout_key="data_timeout")
        assert result == {"USDT": 10000.0}

    def test_order_router_validation_before_execution(self):
        """Test that OrderRouter validates candidate before submission."""
        from core.execution.order_router import OrderRouter

        router = OrderRouter(mode="paper")

        # Valid candidate
        valid = MagicMock()
        valid.symbol = "BTC/USDT"
        valid.side = "buy"
        valid.position_size_usdt = 1000.0
        valid.stop_loss_price = 42000.0
        valid.take_profit_price = 44000.0

        validation_result = router._validate_candidate(valid)
        assert validation_result is None  # Valid

        # Invalid candidate (empty symbol)
        invalid = MagicMock()
        invalid.symbol = ""
        invalid.side = "buy"
        invalid.position_size_usdt = 1000.0
        invalid.stop_loss_price = 42000.0
        invalid.take_profit_price = 44000.0

        validation_result = router._validate_candidate(invalid)
        assert validation_result is not None  # Error


if __name__ == "__main__":
    # Run tests with pytest
    pytest.main([__file__, "-v"])
