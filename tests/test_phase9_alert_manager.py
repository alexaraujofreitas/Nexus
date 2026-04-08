"""
Phase 9 Test Suite — Alert Manager

Tests:
- Alert creation and storage
- Dedup suppression within 60s window
- Event-to-alert mapping for all supported topics
- Severity filtering
- Listener callbacks
- Thread safety (concurrent raise_alert)
- Diagnostics (get_state)
- Replay consistency: same inputs → same alerts
"""

import pytest
import threading
from core.intraday.monitoring.views.alert_manager import (
    AlertManager,
    MAX_ALERT_HISTORY,
    DEDUP_WINDOW_MS,
)
from core.intraday.monitoring.views.view_contracts import AlertRecord, AlertSeverity


# ══════════════════════════════════════════════════════════════
# 1. ALERT CREATION
# ══════════════════════════════════════════════════════════════

class TestAlertCreation:
    def test_raise_alert_returns_record(self):
        mgr = AlertManager(now_ms_fn=lambda: 1000)
        alert = mgr.raise_alert(
            severity=AlertSeverity.WARNING,
            reason_code="test_reason",
            message="Test message",
            source="test_source",
        )
        assert alert is not None
        assert isinstance(alert, AlertRecord)
        assert alert.severity == AlertSeverity.WARNING
        assert alert.reason_code == "test_reason"
        assert alert.message == "Test message"
        assert alert.source == "test_source"
        assert alert.timestamp_ms == 1000

    def test_raise_alert_with_symbol_and_metadata(self):
        mgr = AlertManager(now_ms_fn=lambda: 2000)
        alert = mgr.raise_alert(
            severity=AlertSeverity.CRITICAL,
            reason_code="high_slippage",
            message="Slippage exceeded",
            source="execution",
            symbol="BTCUSDT",
            metadata='{"slippage_bps": 15.5}',
        )
        assert alert.symbol == "BTCUSDT"
        assert alert.metadata == '{"slippage_bps": 15.5}'

    def test_alert_stored_in_history(self):
        mgr = AlertManager(now_ms_fn=lambda: 1000)
        mgr.raise_alert(
            severity=AlertSeverity.INFO,
            reason_code="test",
            message="msg",
            source="src",
        )
        assert mgr.total_alerts == 1
        alerts = mgr.get_recent_alerts()
        assert len(alerts) == 1
        assert alerts[0].reason_code == "test"

    def test_multiple_alerts_ordered_newest_first(self):
        t = [1000]
        mgr = AlertManager(now_ms_fn=lambda: t[0])

        mgr.raise_alert(severity=AlertSeverity.INFO, reason_code="first",
                         message="1st", source="a")
        t[0] = 2000
        mgr.raise_alert(severity=AlertSeverity.WARNING, reason_code="second",
                         message="2nd", source="b")
        t[0] = 3000
        mgr.raise_alert(severity=AlertSeverity.CRITICAL, reason_code="third",
                         message="3rd", source="c")

        alerts = mgr.get_recent_alerts()
        assert len(alerts) == 3
        assert alerts[0].reason_code == "third"
        assert alerts[1].reason_code == "second"
        assert alerts[2].reason_code == "first"

    def test_max_history_respected(self):
        t = [0]
        mgr = AlertManager(now_ms_fn=lambda: t[0])
        for i in range(MAX_ALERT_HISTORY + 50):
            t[0] = i * 100_000  # Spread beyond dedup window
            mgr.raise_alert(
                severity=AlertSeverity.INFO,
                reason_code=f"reason_{i}",
                message=f"msg {i}",
                source=f"src_{i}",
            )
        assert mgr.total_alerts == MAX_ALERT_HISTORY

    def test_alert_id_unique(self):
        t = [1000]
        mgr = AlertManager(now_ms_fn=lambda: t[0])
        a1 = mgr.raise_alert(severity=AlertSeverity.INFO, reason_code="r1",
                              message="m1", source="s1")
        t[0] = 2000
        a2 = mgr.raise_alert(severity=AlertSeverity.INFO, reason_code="r2",
                              message="m2", source="s2")
        assert a1.alert_id != a2.alert_id


# ══════════════════════════════════════════════════════════════
# 2. DEDUP SUPPRESSION
# ══════════════════════════════════════════════════════════════

class TestDedup:
    def test_duplicate_within_window_suppressed(self):
        t = [1000]
        mgr = AlertManager(now_ms_fn=lambda: t[0])
        a1 = mgr.raise_alert(severity=AlertSeverity.WARNING, reason_code="dup",
                              message="msg", source="src")
        assert a1 is not None

        # Same alert 30s later — within 60s window
        t[0] = 31_000
        a2 = mgr.raise_alert(severity=AlertSeverity.WARNING, reason_code="dup",
                              message="msg", source="src")
        assert a2 is None
        assert mgr.total_alerts == 1

    def test_duplicate_after_window_allowed(self):
        t = [1000]
        mgr = AlertManager(now_ms_fn=lambda: t[0])
        a1 = mgr.raise_alert(severity=AlertSeverity.WARNING, reason_code="dup",
                              message="msg", source="src")
        assert a1 is not None

        # Same alert 61s later — outside 60s window
        t[0] = 1000 + DEDUP_WINDOW_MS + 1
        a2 = mgr.raise_alert(severity=AlertSeverity.WARNING, reason_code="dup",
                              message="msg", source="src")
        assert a2 is not None
        assert mgr.total_alerts == 2

    def test_different_reason_not_deduped(self):
        mgr = AlertManager(now_ms_fn=lambda: 1000)
        a1 = mgr.raise_alert(severity=AlertSeverity.WARNING, reason_code="r1",
                              message="m", source="src")
        a2 = mgr.raise_alert(severity=AlertSeverity.WARNING, reason_code="r2",
                              message="m", source="src")
        assert a1 is not None
        assert a2 is not None
        assert mgr.total_alerts == 2

    def test_different_symbol_not_deduped(self):
        mgr = AlertManager(now_ms_fn=lambda: 1000)
        a1 = mgr.raise_alert(severity=AlertSeverity.WARNING, reason_code="r",
                              message="m", source="src", symbol="BTCUSDT")
        a2 = mgr.raise_alert(severity=AlertSeverity.WARNING, reason_code="r",
                              message="m", source="src", symbol="ETHUSDT")
        assert a1 is not None
        assert a2 is not None
        assert mgr.total_alerts == 2

    def test_different_source_not_deduped(self):
        mgr = AlertManager(now_ms_fn=lambda: 1000)
        a1 = mgr.raise_alert(severity=AlertSeverity.WARNING, reason_code="r",
                              message="m", source="src_a")
        a2 = mgr.raise_alert(severity=AlertSeverity.WARNING, reason_code="r",
                              message="m", source="src_b")
        assert a1 is not None
        assert a2 is not None


# ══════════════════════════════════════════════════════════════
# 3. EVENT PROCESSING
# ══════════════════════════════════════════════════════════════

class TestEventProcessing:
    def test_kill_switch_event(self):
        mgr = AlertManager(now_ms_fn=lambda: 1000)
        alert = mgr.process_event("kill_switch.disarmed", {"reason": "drawdown"})
        assert alert is not None
        assert alert.severity == AlertSeverity.EMERGENCY
        assert alert.reason_code == "kill_switch_activated"
        assert "drawdown" in alert.message

    def test_emergency_stop_event(self):
        mgr = AlertManager(now_ms_fn=lambda: 1000)
        alert = mgr.process_event("emergency.stop", {"reason": "manual"})
        assert alert is not None
        assert alert.severity == AlertSeverity.EMERGENCY

    def test_circuit_breaker_tripped(self):
        mgr = AlertManager(now_ms_fn=lambda: 1000)
        alert = mgr.process_event("circuit_breaker.tripped", {"reason": "too many losses"})
        assert alert is not None
        assert alert.severity == AlertSeverity.CRITICAL
        assert alert.reason_code == "circuit_breaker_tripped"

    def test_circuit_breaker_warning(self):
        mgr = AlertManager(now_ms_fn=lambda: 1000)
        alert = mgr.process_event("circuit_breaker.warning", {"reason": "approaching limit"})
        assert alert is not None
        assert alert.severity == AlertSeverity.WARNING

    def test_reconciliation_failure(self):
        mgr = AlertManager(now_ms_fn=lambda: 1000)
        alert = mgr.process_event("reconciliation.failure", {"mismatch_count": 3})
        assert alert is not None
        assert alert.severity == AlertSeverity.CRITICAL
        assert "3 mismatches" in alert.message

    def test_high_slippage(self):
        mgr = AlertManager(now_ms_fn=lambda: 1000)
        alert = mgr.process_event("execution.high_slippage", {
            "symbol": "BTCUSDT", "slippage_bps": 15.5
        })
        assert alert is not None
        assert alert.severity == AlertSeverity.WARNING
        assert alert.symbol == "BTCUSDT"
        assert "15.5" in alert.message

    def test_failure_mode_escalation_degraded(self):
        mgr = AlertManager(now_ms_fn=lambda: 1000)
        alert = mgr.process_event("failure_mode.escalation", {"tier": "degraded"})
        assert alert is not None
        assert alert.severity == AlertSeverity.CRITICAL

    def test_failure_mode_escalation_warning_tier(self):
        mgr = AlertManager(now_ms_fn=lambda: 1000)
        alert = mgr.process_event("failure_mode.escalation", {"tier": "caution"})
        assert alert is not None
        assert alert.severity == AlertSeverity.WARNING

    def test_recovery_failed(self):
        mgr = AlertManager(now_ms_fn=lambda: 1000)
        alert = mgr.process_event("recovery.failed", {"reason": "exchange unreachable"})
        assert alert is not None
        assert alert.severity == AlertSeverity.CRITICAL
        assert alert.reason_code == "recovery_failure"

    def test_system_alert_info(self):
        mgr = AlertManager(now_ms_fn=lambda: 1000)
        alert = mgr.process_event("system.alert", {
            "level": "info", "message": "System started", "source": "init"
        })
        assert alert is not None
        assert alert.severity == AlertSeverity.INFO

    def test_system_alert_error(self):
        mgr = AlertManager(now_ms_fn=lambda: 1000)
        alert = mgr.process_event("system.alert", {
            "level": "error", "message": "DB write failed", "source": "database"
        })
        assert alert is not None
        assert alert.severity == AlertSeverity.CRITICAL

    def test_unknown_topic_returns_none(self):
        mgr = AlertManager(now_ms_fn=lambda: 1000)
        alert = mgr.process_event("unknown.topic", {"data": "irrelevant"})
        assert alert is None

    def test_non_dict_data_handled(self):
        mgr = AlertManager(now_ms_fn=lambda: 1000)
        # Non-dict data should not crash
        alert = mgr.process_event("kill_switch.disarmed", "just a string")
        assert alert is not None
        assert alert.severity == AlertSeverity.EMERGENCY


# ══════════════════════════════════════════════════════════════
# 4. QUERIES & FILTERING
# ══════════════════════════════════════════════════════════════

class TestQueries:
    def _populate(self, mgr, t):
        """Create a set of alerts at different severities."""
        mgr.raise_alert(severity=AlertSeverity.INFO, reason_code="info1",
                         message="i", source="a")
        t[0] += 100_000
        mgr.raise_alert(severity=AlertSeverity.WARNING, reason_code="warn1",
                         message="w", source="b")
        t[0] += 100_000
        mgr.raise_alert(severity=AlertSeverity.CRITICAL, reason_code="crit1",
                         message="c", source="c")
        t[0] += 100_000
        mgr.raise_alert(severity=AlertSeverity.EMERGENCY, reason_code="emrg1",
                         message="e", source="d")

    def test_filter_by_severity(self):
        t = [1000]
        mgr = AlertManager(now_ms_fn=lambda: t[0])
        self._populate(mgr, t)

        crits = mgr.get_alerts_by_severity(AlertSeverity.CRITICAL)
        assert len(crits) == 1
        assert crits[0].reason_code == "crit1"

    def test_get_alerts_since(self):
        t = [1000]
        mgr = AlertManager(now_ms_fn=lambda: t[0])
        self._populate(mgr, t)

        # Alerts at 1000, 101000, 201000, 301000
        since = mgr.get_alerts_since(200_000)
        assert len(since) == 2  # crit1 at 201000 and emrg1 at 301000

    def test_get_alert_count_by_severity(self):
        t = [1000]
        mgr = AlertManager(now_ms_fn=lambda: t[0])
        self._populate(mgr, t)

        counts = mgr.get_alert_count_by_severity()
        assert counts["info"] == 1
        assert counts["warning"] == 1
        assert counts["critical"] == 1
        assert counts["emergency"] == 1

    def test_limit_respected(self):
        t = [0]
        mgr = AlertManager(now_ms_fn=lambda: t[0])
        for i in range(10):
            t[0] = i * 100_000
            mgr.raise_alert(severity=AlertSeverity.INFO, reason_code=f"r_{i}",
                             message=f"m{i}", source=f"s_{i}")

        alerts = mgr.get_recent_alerts(limit=3)
        assert len(alerts) == 3
        # Most recent first
        assert alerts[0].reason_code == "r_9"


# ══════════════════════════════════════════════════════════════
# 5. LISTENERS
# ══════════════════════════════════════════════════════════════

class TestListeners:
    def test_listener_called_on_alert(self):
        received = []
        mgr = AlertManager(now_ms_fn=lambda: 1000)
        mgr.add_listener(lambda a: received.append(a))

        mgr.raise_alert(severity=AlertSeverity.INFO, reason_code="test",
                         message="m", source="s")
        assert len(received) == 1
        assert received[0].reason_code == "test"

    def test_listener_not_called_on_dedup(self):
        received = []
        mgr = AlertManager(now_ms_fn=lambda: 1000)
        mgr.add_listener(lambda a: received.append(a))

        mgr.raise_alert(severity=AlertSeverity.INFO, reason_code="dup",
                         message="m", source="s")
        mgr.raise_alert(severity=AlertSeverity.INFO, reason_code="dup",
                         message="m", source="s")
        # Only 1 callback — second was deduped
        assert len(received) == 1

    def test_remove_listener(self):
        received = []
        cb = lambda a: received.append(a)
        t = [1000]
        mgr = AlertManager(now_ms_fn=lambda: t[0])
        mgr.add_listener(cb)

        mgr.raise_alert(severity=AlertSeverity.INFO, reason_code="r1",
                         message="m", source="s1")
        mgr.remove_listener(cb)
        t[0] = 200_000
        mgr.raise_alert(severity=AlertSeverity.INFO, reason_code="r2",
                         message="m", source="s2")

        assert len(received) == 1

    def test_listener_exception_does_not_crash(self):
        def bad_listener(a):
            raise RuntimeError("boom")

        mgr = AlertManager(now_ms_fn=lambda: 1000)
        mgr.add_listener(bad_listener)
        # Should not raise
        alert = mgr.raise_alert(severity=AlertSeverity.INFO, reason_code="test",
                                 message="m", source="s")
        assert alert is not None


# ══════════════════════════════════════════════════════════════
# 6. THREAD SAFETY
# ══════════════════════════════════════════════════════════════

class TestThreadSafety:
    def test_concurrent_raise_alert(self):
        t = [0]
        lock = threading.Lock()
        mgr = AlertManager(now_ms_fn=lambda: t[0])

        errors = []

        def raise_many(thread_id):
            try:
                for i in range(20):
                    with lock:
                        t[0] = thread_id * 10000 + i * 200_000
                    mgr.raise_alert(
                        severity=AlertSeverity.INFO,
                        reason_code=f"t{thread_id}_r{i}",
                        message=f"thread {thread_id} alert {i}",
                        source=f"thread_{thread_id}_{i}",
                    )
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=raise_many, args=(tid,)) for tid in range(4)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()

        assert len(errors) == 0
        assert mgr.total_alerts > 0


# ══════════════════════════════════════════════════════════════
# 7. DIAGNOSTICS
# ══════════════════════════════════════════════════════════════

class TestDiagnostics:
    def test_get_state(self):
        t = [1000]
        mgr = AlertManager(now_ms_fn=lambda: t[0])
        mgr.raise_alert(severity=AlertSeverity.WARNING, reason_code="r",
                         message="m", source="s")

        state = mgr.get_state()
        assert state["total_alerts"] == 1
        assert "by_severity" in state
        assert state["dedup_cache_size"] == 1
        assert state["listener_count"] == 0


# ══════════════════════════════════════════════════════════════
# 8. REPLAY CONSISTENCY
# ══════════════════════════════════════════════════════════════

class TestReplayConsistency:
    def test_same_inputs_same_outputs(self):
        """Identical event sequences produce identical alert lists."""
        def run_sequence():
            t = [1000]
            mgr = AlertManager(now_ms_fn=lambda: t[0])
            mgr.process_event("kill_switch.disarmed", {"reason": "drawdown"})
            t[0] = 200_000
            mgr.process_event("circuit_breaker.tripped", {"reason": "losses"})
            t[0] = 300_000
            mgr.process_event("execution.high_slippage", {"symbol": "BTCUSDT", "slippage_bps": 10})
            return mgr.get_recent_alerts()

        run1 = run_sequence()
        run2 = run_sequence()

        assert len(run1) == len(run2)
        for a1, a2 in zip(run1, run2):
            assert a1.alert_id == a2.alert_id
            assert a1.severity == a2.severity
            assert a1.reason_code == a2.reason_code
            assert a1.message == a2.message
            assert a1.timestamp_ms == a2.timestamp_ms
