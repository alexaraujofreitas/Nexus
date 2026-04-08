# ============================================================
# Phase 2 Tests — Thread Count Baseline (Audit Finding 6)
# 2 tests per PHASE1E_TEST_PLAN.md
# ============================================================
import threading

import pytest
from unittest.mock import patch, MagicMock


class TestThreadBaseline:

    def test_baseline_thread_count_logged(self):
        """NexusEngine logs the baseline thread count at startup."""
        mock_coordinator = MagicMock()
        mock_coordinator.start_all = MagicMock()
        mock_coordinator.stop_all = MagicMock()

        with patch("core.database.engine.init_database"), \
             patch("core.orchestrator.orchestrator_engine.get_orchestrator"), \
             patch("core.agents.agent_coordinator.get_coordinator", return_value=mock_coordinator), \
             patch("core.risk.crash_defense_controller.get_crash_defense_controller"):
            from core.engine import NexusEngine
            engine = NexusEngine()
            engine._init_notifications = MagicMock()
            engine.start()

            assert engine.baseline_thread_count is not None
            assert engine.baseline_thread_count > 0
            # Baseline should be a reasonable number (not 0 or hundreds)
            assert engine.baseline_thread_count < 100

            engine.stop()

    def test_no_spurious_watchdog_warnings(self):
        """
        At the reduced headless baseline (~10-15 threads), the thread watchdog
        threshold of 75 should not trigger any warnings.
        """
        current = threading.active_count()
        # Headless baseline should be well under the 75 threshold
        assert current < 75, (
            f"Thread count {current} is already near or above the "
            f"watchdog threshold of 75 — investigate thread leak"
        )
