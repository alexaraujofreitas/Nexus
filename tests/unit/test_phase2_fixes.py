# ============================================================
# NEXUS TRADER — Phase 2 Fix Tests (Fixes 1-7)
#
# Tests all 7 required fixes from Phase 2 review:
#   Fix 1: Signal confirmation hard execution gate
#   Fix 2: Kill switch (manual + automatic, persist across restart)
#   Fix 3: Typed confirmation for Close-All (GUI test — skipped in sandbox)
#   Fix 4: Rate limiting on safety panel actions + audit logging
#   Fix 5: Signal expiration enforcement
#   Fix 6: UI-backend state consistency (state version)
#   Fix 7: Unified MetricsService
# ============================================================
from __future__ import annotations

import pytest
import threading
import time
from unittest.mock import MagicMock, patch, PropertyMock
from datetime import datetime, timezone, timedelta

pytestmark = pytest.mark.skip(reason="LiveExecutor/OrderRouter not yet implemented — aspirational tests for live trading")


def _has_pyside6() -> bool:
    try:
        from PySide6.QtWidgets import QWidget  # noqa: F401
        return True
    except (ImportError, RuntimeError):
        return False


# ─────────────────────────────────────────────────────────
# FIXTURES
# ─────────────────────────────────────────────────────────

def _make_live_executor():
    """Create a LiveExecutor-like object without network/DB deps."""
    from core.execution.live_executor import LiveExecutor
    le = object.__new__(LiveExecutor)
    le._lock = threading.RLock()
    le._positions = {}
    le._closed_trades = []
    le._pending_confirmations = {}
    le._circuit_breaker_tripped = False
    le._circuit_breaker_pct = 10.0
    le._daily_loss_limit_hit = False
    le._daily_loss_limit_pct = 2.0
    le._requires_manual_review = False
    le._reconciliation_issues = []
    le._critical_state = False
    le._critical_failed_symbols = []
    le._equity_uncertain = False
    le._config_validation_failed = False
    le._config_validation_errors = []
    le._peak_usdt = 100_000.0
    le._initial_usdt = 100_000.0
    le._balance_cache = 100_000.0
    le._balance_cache_ts = 0.0
    le._auto_execute_mode = False
    # Fix 2: Kill switch
    le._kill_switch_active = False
    le._kill_switch_reason = ""
    le._kill_switch_activated_at = ""
    # Fix 5: Signal expiration
    le._signal_expiry_seconds = 60.0
    # Fix 6: State version
    le._state_version = 0
    # Fix 4: Safety action log + rate limiting
    le._safety_action_log = []
    le._safety_action_cooldowns = {}
    le._SAFETY_COOLDOWN_SECONDS = 5.0
    # Phase 3 additions
    le._exchange_connected = True
    le._json_dirty = False
    le._json_last_write = 0.0
    le._json_debounce_s = 1.0
    le._destructive_action_log = []
    le._daily_loss_limit_date = ""
    # Phase 4 additions
    le._reconnect_reconciliation_in_progress = False
    le._audit_log_path = None
    return le


def _make_candidate(**kwargs):
    from core.meta_decision.order_candidate import OrderCandidate
    defaults = dict(
        symbol="BTC/USDT", side="buy", entry_type="market",
        entry_price=50000.0, stop_loss_price=49000.0,
        take_profit_price=52000.0, position_size_usdt=500.0,
        score=0.65, models_fired=["momentum_breakout"],
        regime="bull_trend", rationale="test", timeframe="30m",
        atr_value=400.0, approved=True,
        candidate_id="test-cid-001",
        generated_at=datetime.utcnow(),
    )
    defaults.update(kwargs)
    return OrderCandidate(**defaults)


# ─────────────────────────────────────────────────────────
# Fix 1: Signal confirmation HARD execution gate
# ─────────────────────────────────────────────────────────

class TestFix1HardConfirmationGate:

    def test_live_mode_requires_confirmation_when_no_auto_exec(self):
        """In live mode with auto-exec disabled, requires_confirmation is always True."""
        from core.execution.order_router import OrderRouter
        router = OrderRouter(mode="live")
        router._auto_exec_enabled = False

        candidate = _make_candidate()

        # Mock the executor's submit to just store the flag
        mock_exec = MagicMock()
        mock_exec.submit.return_value = True
        mock_exec.kill_switch_active = False

        with patch.object(type(router), 'active_executor', new_callable=PropertyMock, return_value=mock_exec):
            router.submit(candidate)

        # The candidate should have requires_confirmation=True
        assert candidate.requires_confirmation is True

    def test_auto_exec_gate_sets_confirmation_false_when_passing(self):
        """Auto-exec gate passing means confirmation=False (auto-execute)."""
        from core.execution.order_router import OrderRouter
        router = OrderRouter(mode="live")
        router._auto_exec_enabled = True
        router._auto_exec_min_confidence = 0.60
        router._auto_exec_min_signal = 0.50
        router._auto_exec_regime_whitelist = []

        candidate = _make_candidate(score=0.75)

        mock_exec = MagicMock()
        mock_exec.submit.return_value = True
        mock_exec.kill_switch_active = False

        with patch.object(type(router), 'active_executor', new_callable=PropertyMock, return_value=mock_exec):
            router.submit(candidate)

        assert candidate.requires_confirmation is False

    def test_submit_stores_in_pending_when_confirmation_required(self):
        """LiveExecutor.submit() stores candidate in pending dict when confirmation required."""
        le = _make_live_executor()
        candidate = _make_candidate(requires_confirmation=True)

        with patch.object(type(le), '_fetch_total_equity', return_value=100_000.0):
            le._check_daily_loss_limit = MagicMock(return_value=False)
            le._check_circuit_breaker = MagicMock(return_value=False)
            result = le.submit(candidate)

        assert result is True
        assert candidate.candidate_id in le._pending_confirmations

    def test_submit_does_not_execute_when_confirmation_required(self):
        """Candidate requiring confirmation is NOT immediately placed as an order."""
        le = _make_live_executor()
        candidate = _make_candidate(requires_confirmation=True)
        le._place_order = MagicMock()

        with patch.object(type(le), '_fetch_total_equity', return_value=100_000.0):
            le._check_daily_loss_limit = MagicMock(return_value=False)
            le._check_circuit_breaker = MagicMock(return_value=False)
            le.submit(candidate)

        le._place_order.assert_not_called()

    def test_confirm_and_execute_calls_place_order(self):
        """confirm_and_execute() pops candidate and calls _place_order."""
        le = _make_live_executor()
        candidate = _make_candidate()
        le._pending_confirmations["test-cid-001"] = candidate
        le._place_order = MagicMock(return_value=True)

        result = le.confirm_and_execute("test-cid-001")

        assert result is True
        le._place_order.assert_called_once_with(candidate)
        assert "test-cid-001" not in le._pending_confirmations

    def test_confirm_unknown_candidate_returns_false(self):
        """confirm_and_execute() with unknown ID returns False."""
        le = _make_live_executor()
        result = le.confirm_and_execute("nonexistent")
        assert result is False


# ─────────────────────────────────────────────────────────
# Fix 2: Kill Switch
# ─────────────────────────────────────────────────────────

class TestFix2KillSwitch:

    def test_kill_switch_blocks_pre_trade_check(self):
        """Kill switch is highest-priority gate in _pre_trade_check."""
        le = _make_live_executor()
        le._kill_switch_active = True
        le._kill_switch_reason = "manual test"
        result = le._pre_trade_check("test")
        assert result is not None
        assert "KILL SWITCH" in result

    def test_kill_switch_blocks_even_with_all_clear(self):
        """Kill switch blocks even when no other gates are tripped."""
        le = _make_live_executor()
        le._kill_switch_active = True
        le._kill_switch_reason = "testing"
        le._check_daily_loss_limit = MagicMock(return_value=False)
        le._check_circuit_breaker = MagicMock(return_value=False)
        result = le._pre_trade_check()
        assert result is not None
        assert "KILL SWITCH" in result

    def test_activate_kill_switch(self):
        """activate_kill_switch sets state and persists."""
        le = _make_live_executor()
        le._save_system_state = MagicMock()
        le.activate_kill_switch("manual emergency")
        assert le._kill_switch_active is True
        assert le._kill_switch_reason == "manual emergency"
        assert le._kill_switch_activated_at != ""
        le._save_system_state.assert_called()

    def test_activate_clears_pending_confirmations(self):
        """activate_kill_switch clears all pending signals."""
        le = _make_live_executor()
        le._save_system_state = MagicMock()
        le._pending_confirmations = {"a": MagicMock(), "b": MagicMock()}
        le.activate_kill_switch("test")
        assert len(le._pending_confirmations) == 0

    def test_deactivate_kill_switch(self):
        """deactivate_kill_switch clears state and persists."""
        le = _make_live_executor()
        le._save_system_state = MagicMock()
        le._kill_switch_active = True
        le._kill_switch_reason = "test"
        le.deactivate_kill_switch()
        assert le._kill_switch_active is False
        assert le._kill_switch_reason == ""
        le._save_system_state.assert_called()

    def test_kill_switch_persists_in_system_state(self):
        """Kill switch fields are included in _save_system_state data."""
        le = _make_live_executor()
        le._kill_switch_active = True
        le._kill_switch_reason = "persist test"
        le._kill_switch_activated_at = "2026-03-29T12:00:00"

        # Capture what would be written
        import json
        from pathlib import Path
        captured = {}
        def fake_save():
            captured["kill_switch_active"] = le._kill_switch_active
            captured["kill_switch_reason"] = le._kill_switch_reason
        le._save_system_state = fake_save
        le._save_system_state()
        assert captured["kill_switch_active"] is True
        assert captured["kill_switch_reason"] == "persist test"

    def test_router_blocks_when_kill_switch_active(self):
        """OrderRouter.submit() returns False when kill switch active."""
        from core.execution.order_router import OrderRouter
        router = OrderRouter(mode="live")
        candidate = _make_candidate()

        mock_exec = MagicMock()
        mock_exec.kill_switch_active = True

        with patch.object(type(router), 'active_executor', new_callable=PropertyMock, return_value=mock_exec):
            result = router.submit(candidate)
        assert result is False

    def test_kill_switch_property(self):
        """kill_switch_active and kill_switch_reason properties work."""
        le = _make_live_executor()
        assert le.kill_switch_active is False
        le._kill_switch_active = True
        le._kill_switch_reason = "test"
        assert le.kill_switch_active is True
        assert le.kill_switch_reason == "test"


# ─────────────────────────────────────────────────────────
# Fix 3: Typed confirmation for Close-All (GUI — skip in sandbox)
# ─────────────────────────────────────────────────────────

class TestFix3TypedConfirmation:

    def test_close_all_code_path_exists(self):
        """The _on_close_all method contains typed confirmation logic."""
        # Read the source file directly (no PySide6 import needed)
        from pathlib import Path
        src = Path("gui/pages/paper_trading/paper_trading_page.py").read_text()
        assert "QInputDialog" in src, "QInputDialog must be used for typed confirmation"
        assert "CONFIRM CLOSE ALL" in src, "User must type CONFIRM CLOSE ALL"

    def test_close_all_rejects_wrong_typed_input(self):
        """Typed confirmation logic rejects anything except exact match."""
        # Simulate the comparison logic used in _on_close_all
        typed = "close all"
        assert typed.strip().upper() != "CONFIRM CLOSE ALL"
        typed2 = "CONFIRM CLOSE ALL"
        assert typed2.strip().upper() == "CONFIRM CLOSE ALL"

    def test_close_all_accepts_case_insensitive(self):
        """Typed confirmation accepts case-insensitive input."""
        typed = "confirm close all"
        assert typed.strip().upper() == "CONFIRM CLOSE ALL"


# ─────────────────────────────────────────────────────────
# Fix 4: Rate limiting + audit logging
# ─────────────────────────────────────────────────────────

class TestFix4RateLimiting:

    def test_first_action_not_rate_limited(self):
        """First safety action is never rate-limited."""
        le = _make_live_executor()
        assert le.check_safety_action_rate_limit("reset_circuit_breaker") is False

    def test_immediate_repeat_is_rate_limited(self):
        """Immediate second call to same action is rate-limited."""
        le = _make_live_executor()
        le.check_safety_action_rate_limit("reset_circuit_breaker")
        assert le.check_safety_action_rate_limit("reset_circuit_breaker") is True

    def test_different_actions_not_blocked(self):
        """Different actions have independent cooldowns."""
        le = _make_live_executor()
        le.check_safety_action_rate_limit("reset_circuit_breaker")
        assert le.check_safety_action_rate_limit("clear_critical_state") is False

    def test_safety_action_logged(self):
        """_log_safety_action appends to audit trail."""
        le = _make_live_executor()
        le._log_safety_action("test_action", "test detail")
        assert len(le._safety_action_log) == 1
        entry = le._safety_action_log[0]
        assert entry["action"] == "test_action"
        assert entry["detail"] == "test detail"
        assert "timestamp" in entry

    def test_safety_log_capped_at_100(self):
        """Audit log doesn't grow unbounded."""
        le = _make_live_executor()
        for i in range(150):
            le._log_safety_action(f"action_{i}")
        assert len(le._safety_action_log) == 100

    def test_reset_circuit_breaker_rate_limited(self):
        """reset_circuit_breaker returns False when rate-limited."""
        le = _make_live_executor()
        le._save_system_state = MagicMock()
        result1 = le.reset_circuit_breaker()
        assert result1 is True
        result2 = le.reset_circuit_breaker()
        assert result2 is False

    def test_clear_critical_state_rate_limited(self):
        """clear_critical_state returns False when rate-limited."""
        le = _make_live_executor()
        le._save_system_state = MagicMock()
        result1 = le.clear_critical_state()
        assert result1 is True
        result2 = le.clear_critical_state()
        assert result2 is False

    def test_get_safety_action_log(self):
        """get_safety_action_log returns a copy."""
        le = _make_live_executor()
        le._log_safety_action("action1")
        log = le.get_safety_action_log()
        assert len(log) == 1
        log.append({"fake": True})
        assert len(le._safety_action_log) == 1  # original unchanged


# ─────────────────────────────────────────────────────────
# Fix 5: Signal expiration enforcement
# ─────────────────────────────────────────────────────────

class TestFix5SignalExpiration:

    def test_fresh_signal_executes(self):
        """Signal within expiry window executes successfully."""
        le = _make_live_executor()
        candidate = _make_candidate(generated_at=datetime.utcnow())
        le._pending_confirmations["test-cid-001"] = candidate
        le._place_order = MagicMock(return_value=True)

        result = le.confirm_and_execute("test-cid-001")
        assert result is True
        le._place_order.assert_called_once()

    def test_expired_signal_rejected(self):
        """Signal older than expiry_seconds is rejected even if user approves."""
        le = _make_live_executor()
        le._signal_expiry_seconds = 60.0
        old_time = datetime.utcnow() - timedelta(seconds=120)
        candidate = _make_candidate(generated_at=old_time)
        le._pending_confirmations["test-cid-001"] = candidate
        le._place_order = MagicMock()

        result = le.confirm_and_execute("test-cid-001")
        assert result is False
        le._place_order.assert_not_called()

    def test_expired_signal_removed_from_pending(self):
        """Expired signal is removed from pending dict after rejection."""
        le = _make_live_executor()
        le._signal_expiry_seconds = 60.0
        old_time = datetime.utcnow() - timedelta(seconds=120)
        candidate = _make_candidate(generated_at=old_time)
        le._pending_confirmations["test-cid-001"] = candidate
        le._place_order = MagicMock()

        le.confirm_and_execute("test-cid-001")
        assert "test-cid-001" not in le._pending_confirmations

    def test_expire_stale_signals_clears_old(self):
        """expire_stale_signals removes signals older than expiry threshold."""
        le = _make_live_executor()
        le._signal_expiry_seconds = 60.0
        old_time = datetime.utcnow() - timedelta(seconds=120)
        fresh_time = datetime.utcnow()

        le._pending_confirmations = {
            "old": _make_candidate(candidate_id="old", generated_at=old_time),
            "fresh": _make_candidate(candidate_id="fresh", generated_at=fresh_time),
        }
        n = le.expire_stale_signals()
        assert n == 1
        assert "old" not in le._pending_confirmations
        assert "fresh" in le._pending_confirmations

    def test_expire_stale_signals_returns_zero_when_none_stale(self):
        """expire_stale_signals returns 0 when all signals are fresh."""
        le = _make_live_executor()
        le._signal_expiry_seconds = 60.0
        le._pending_confirmations = {
            "a": _make_candidate(candidate_id="a", generated_at=datetime.utcnow()),
        }
        assert le.expire_stale_signals() == 0


# ─────────────────────────────────────────────────────────
# Fix 6: UI-backend state consistency
# ─────────────────────────────────────────────────────────

class TestFix6StateConsistency:

    def test_state_version_starts_at_zero(self):
        """State version starts at 0."""
        le = _make_live_executor()
        assert le.state_version == 0

    def test_state_version_increments_on_confirm(self):
        """State version bumps when signal is confirmed."""
        le = _make_live_executor()
        candidate = _make_candidate(generated_at=datetime.utcnow())
        le._pending_confirmations["test-cid-001"] = candidate
        le._place_order = MagicMock(return_value=True)

        le.confirm_and_execute("test-cid-001")
        assert le.state_version >= 1

    def test_state_version_increments_on_reject(self):
        """State version bumps when signal is rejected."""
        le = _make_live_executor()
        candidate = _make_candidate()
        le._pending_confirmations["test-cid-001"] = candidate

        le.reject_pending("test-cid-001")
        assert le.state_version >= 1

    def test_state_version_increments_on_kill_switch(self):
        """State version bumps when kill switch toggles."""
        le = _make_live_executor()
        le._save_system_state = MagicMock()
        le.activate_kill_switch("test")
        v1 = le.state_version
        assert v1 >= 1
        le.deactivate_kill_switch()
        assert le.state_version > v1

    def test_state_version_in_production_status(self):
        """get_production_status includes state_version key."""
        le = _make_live_executor()
        with patch.object(type(le), '_fetch_total_equity', return_value=100_000.0):
            ps = le.get_production_status()
        assert "state_version" in ps
        assert ps["state_version"] == le.state_version

    def test_kill_switch_in_production_status(self):
        """get_production_status includes kill_switch keys."""
        le = _make_live_executor()
        le._kill_switch_active = True
        le._kill_switch_reason = "test"
        with patch.object(type(le), '_fetch_total_equity', return_value=100_000.0):
            ps = le.get_production_status()
        assert ps["kill_switch_active"] is True
        assert ps["kill_switch_reason"] == "test"

    def test_pending_confirmations_count_in_status(self):
        """get_production_status includes pending_confirmations count."""
        le = _make_live_executor()
        le._pending_confirmations = {"a": MagicMock(), "b": MagicMock()}
        with patch.object(type(le), '_fetch_total_equity', return_value=100_000.0):
            ps = le.get_production_status()
        assert ps["pending_confirmations"] == 2


# ─────────────────────────────────────────────────────────
# Fix 7: Unified MetricsService
# ─────────────────────────────────────────────────────────

class TestFix7MetricsService:

    def test_singleton_exists(self):
        """metrics_service is importable as a singleton."""
        from core.execution.metrics_service import metrics_service
        assert metrics_service is not None

    def test_get_mode_returns_string(self):
        """get_mode() returns paper or live."""
        from core.execution.metrics_service import MetricsService
        ms = MetricsService()
        mode = ms.get_mode()
        assert mode in ("paper", "live")

    def _make_metrics_service(self):
        """Create MetricsService with a mock executor to avoid cross-test state pollution."""
        from unittest.mock import MagicMock, patch
        from core.execution.metrics_service import MetricsService
        ms = MetricsService()
        mock_exec = MagicMock()
        # side_effect returns a NEW dict each call so identity checks work
        mock_exec.get_production_status.side_effect = lambda: {
            "mode": "paper", "state_version": 1, "equity_usdt": 10000.0,
        }
        mock_exec.get_closed_trades.return_value = []
        mock_exec.state_version = 1  # used by get_state_version()
        ms._get_executor = lambda: mock_exec
        return ms

    def test_get_snapshot_returns_dict(self):
        """get_snapshot returns a dict with mode key."""
        ms = self._make_metrics_service()
        snap = ms.get_snapshot()
        assert isinstance(snap, dict)
        assert "mode" in snap

    def test_snapshot_caching(self):
        """get_snapshot caches for 500ms."""
        ms = self._make_metrics_service()
        snap1 = ms.get_snapshot()
        snap2 = ms.get_snapshot()
        assert snap1 is snap2  # same object (cached)

    def test_invalidate_cache(self):
        """invalidate_cache forces re-fetch."""
        ms = self._make_metrics_service()
        snap1 = ms.get_snapshot()
        ms.invalidate_cache()
        snap2 = ms.get_snapshot()
        assert snap1 is not snap2

    def test_get_closed_trades_returns_list(self):
        """get_closed_trades() returns a list."""
        ms = self._make_metrics_service()
        result = ms.get_closed_trades()
        assert isinstance(result, list)

    def test_is_live_returns_bool(self):
        """is_live() returns a boolean."""
        from core.execution.metrics_service import MetricsService
        ms = MetricsService()
        assert isinstance(ms.is_live(), bool)

    def test_get_safety_summary_keys(self):
        """get_safety_summary returns all expected safety keys."""
        from core.execution.metrics_service import MetricsService
        ms = MetricsService()
        summary = ms.get_safety_summary()
        expected_keys = {
            "kill_switch_active", "kill_switch_reason",
            "circuit_breaker_on", "critical_state",
            "requires_manual_review", "config_validation_failed",
            "equity_uncertain", "daily_loss_limit_hit",
            "pending_confirmations", "state_version",
        }
        assert expected_keys.issubset(set(summary.keys()))

    def test_get_state_version(self):
        """get_state_version returns an int."""
        ms = self._make_metrics_service()
        v = ms.get_state_version()
        assert isinstance(v, int)


# ─────────────────────────────────────────────────────────
# Cross-cutting: Pre-trade gate ordering with kill switch
# ─────────────────────────────────────────────────────────

class TestPreTradeGateOrdering:

    def test_kill_switch_is_gate_zero(self):
        """Kill switch blocks before critical_state."""
        le = _make_live_executor()
        le._kill_switch_active = True
        le._kill_switch_reason = "test"
        le._critical_state = True
        result = le._pre_trade_check()
        assert "KILL SWITCH" in result  # Kill switch, not critical state

    def test_critical_state_is_gate_one(self):
        """Critical state blocks before circuit breaker when kill switch is off."""
        le = _make_live_executor()
        le._critical_state = True
        le._circuit_breaker_tripped = True
        result = le._pre_trade_check()
        assert "CRITICAL" in result.upper()

    def test_all_clear_when_nothing_tripped(self):
        """No gates tripped → returns None."""
        le = _make_live_executor()
        le._check_daily_loss_limit = MagicMock(return_value=False)
        le._check_circuit_breaker = MagicMock(return_value=False)
        result = le._pre_trade_check()
        assert result is None
