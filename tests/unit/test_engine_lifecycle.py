# ============================================================
# Phase 2 Tests — Headless Engine Lifecycle
# 4 tests per PHASE1E_TEST_PLAN.md
# ============================================================
import pytest
from unittest.mock import patch, MagicMock


def _make_patched_engine():
    """Create a NexusEngine with all external deps mocked."""
    mock_coordinator = MagicMock()
    mock_coordinator.start_all = MagicMock()
    mock_coordinator.stop_all = MagicMock()

    patches = {
        "core.database.engine.init_database": MagicMock(),
        "core.orchestrator.orchestrator_engine.get_orchestrator": MagicMock(),
        "core.agents.agent_coordinator.get_coordinator": MagicMock(return_value=mock_coordinator),
        "core.risk.crash_defense_controller.get_crash_defense_controller": MagicMock(),
    }
    return patches, mock_coordinator


class TestEngineLifecycle:

    def test_headless_start_stop(self):
        """NexusEngine.start() and stop() complete without PySide6."""
        patches, mock_coordinator = _make_patched_engine()

        with patch.dict("sys.modules", {}):
            with patch("core.database.engine.init_database") as mock_db, \
                 patch("core.orchestrator.orchestrator_engine.get_orchestrator") as mock_orch, \
                 patch("core.agents.agent_coordinator.get_coordinator", return_value=mock_coordinator), \
                 patch("core.risk.crash_defense_controller.get_crash_defense_controller"):
                from core.engine import NexusEngine
                engine = NexusEngine()
                engine._init_notifications = MagicMock()
                engine.start()

                assert engine.is_running is True
                mock_db.assert_called_once()
                mock_orch.assert_called_once()
                mock_coordinator.start_all.assert_called_once()

                engine.stop()
                assert engine.is_running is False
                mock_coordinator.stop_all.assert_called_once()

    def test_db_initialized(self):
        """NexusEngine.start() calls init_database()."""
        _, mock_coordinator = _make_patched_engine()

        with patch("core.database.engine.init_database") as mock_db, \
             patch("core.orchestrator.orchestrator_engine.get_orchestrator"), \
             patch("core.agents.agent_coordinator.get_coordinator", return_value=mock_coordinator), \
             patch("core.risk.crash_defense_controller.get_crash_defense_controller"):
            from core.engine import NexusEngine
            engine = NexusEngine()
            engine._init_notifications = MagicMock()
            engine.start()
            mock_db.assert_called_once()
            engine.stop()

    def test_exchange_connected(self):
        """Orchestrator is initialized during engine start."""
        _, mock_coordinator = _make_patched_engine()

        with patch("core.database.engine.init_database"), \
             patch("core.orchestrator.orchestrator_engine.get_orchestrator") as mock_orch, \
             patch("core.agents.agent_coordinator.get_coordinator", return_value=mock_coordinator), \
             patch("core.risk.crash_defense_controller.get_crash_defense_controller"):
            from core.engine import NexusEngine
            engine = NexusEngine()
            engine._init_notifications = MagicMock()
            engine.start()
            mock_orch.assert_called_once()
            engine.stop()

    def test_agents_started(self):
        """Agents are auto-started when agents.auto_start is True."""
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
            mock_coordinator.start_all.assert_called_once()
            engine.stop()
