# ============================================================
# Phase 4 Final Remediation — Fail-Fast Startup Tests
#
# Proves that NexusEngine CANNOT silently continue in a healthy
# state if the intraday strategy engine fails to start.
#
# Option A (Fail Fast) is implemented:
#   - Startup raises IntradayEngineStartupError
#   - Engine._running remains False
#   - Subsystems already started are torn down
#   - Failure reason is logged at CRITICAL level
#   - strategy_bus property is None
#
# ZERO PySide6 imports. Pure Python.
# ============================================================
import pytest
import logging
from unittest.mock import patch, MagicMock

from core.engine import NexusEngine, IntradayEngineStartupError


class TestFailFastStartup:
    """NexusEngine must NOT silently continue if intraday engine fails."""

    @patch("core.intraday.engine_integration.start_intraday_engine")
    @patch("core.database.engine.init_database")
    def test_startup_raises_on_intraday_failure(self, mock_db, mock_start):
        """If start_intraday_engine() raises, NexusEngine.start() raises too."""
        mock_start.side_effect = RuntimeError("StrategyBus init crashed")

        eng = NexusEngine()
        with pytest.raises(IntradayEngineStartupError, match="StrategyBus init crashed"):
            eng.start()

    @patch("core.intraday.engine_integration.start_intraday_engine")
    @patch("core.database.engine.init_database")
    def test_engine_not_running_after_failure(self, mock_db, mock_start):
        """Engine._running must be False after failed startup."""
        mock_start.side_effect = RuntimeError("boom")

        eng = NexusEngine()
        with pytest.raises(IntradayEngineStartupError):
            eng.start()

        assert eng.is_running is False

    @patch("core.intraday.engine_integration.start_intraday_engine")
    @patch("core.database.engine.init_database")
    def test_strategy_bus_none_after_failure(self, mock_db, mock_start):
        """strategy_bus property must be None after failed startup."""
        mock_start.side_effect = RuntimeError("fail")

        eng = NexusEngine()
        with pytest.raises(IntradayEngineStartupError):
            eng.start()

        assert eng.strategy_bus is None

    @patch("core.intraday.engine_integration.start_intraday_engine")
    @patch("core.database.engine.init_database")
    def test_returns_none_raises(self, mock_db, mock_start):
        """If start_intraday_engine() returns None, startup must fail."""
        mock_start.return_value = None

        eng = NexusEngine()
        with pytest.raises(IntradayEngineStartupError, match="returned None"):
            eng.start()

    @patch("core.intraday.engine_integration.start_intraday_engine")
    @patch("core.database.engine.init_database")
    def test_failure_is_logged_critical(self, mock_db, mock_start, caplog):
        """Failure must be logged at CRITICAL level."""
        mock_start.side_effect = ValueError("broken strategy")

        eng = NexusEngine()
        with pytest.raises(IntradayEngineStartupError):
            with caplog.at_level(logging.CRITICAL, logger="core.engine"):
                eng.start()

        assert any("FATAL" in r.message for r in caplog.records if r.levelno >= logging.CRITICAL)

    @patch("core.intraday.engine_integration.start_intraday_engine")
    @patch("core.database.engine.init_database")
    def test_error_is_chained_from_original(self, mock_db, mock_start):
        """IntradayEngineStartupError must chain from the original exception."""
        original = ValueError("root cause")
        mock_start.side_effect = original

        eng = NexusEngine()
        with pytest.raises(IntradayEngineStartupError) as exc_info:
            eng.start()

        assert exc_info.value.__cause__ is original


class TestFailFastTeardown:
    """Subsystems started before the failure must be torn down."""

    @patch("core.intraday.engine_integration.start_intraday_engine")
    @patch("core.intraday.engine_integration.stop_intraday_engine")
    @patch("core.database.engine.init_database")
    def test_teardown_calls_stop_intraday(self, mock_db, mock_stop, mock_start):
        """stop_intraday_engine() called during teardown after failure."""
        mock_start.side_effect = RuntimeError("fail")

        eng = NexusEngine()
        with pytest.raises(IntradayEngineStartupError):
            eng.start()

        mock_stop.assert_called_once()

    @patch("core.intraday.engine_integration.start_intraday_engine")
    @patch("core.agents.agent_coordinator.get_coordinator")
    @patch("core.database.engine.init_database")
    def test_coordinator_stopped_on_failure(self, mock_db, mock_get_coord, mock_start):
        """AgentCoordinator.stop_all() called during teardown."""
        mock_coordinator = MagicMock()
        mock_get_coord.return_value = mock_coordinator
        mock_start.side_effect = RuntimeError("fail")

        eng = NexusEngine()

        with pytest.raises(IntradayEngineStartupError):
            eng.start()

        mock_coordinator.stop_all.assert_called()


class TestFailFastShutdownSafety:
    """Shutdown must remain safe even after failed startup."""

    @patch("core.intraday.engine_integration.start_intraday_engine")
    @patch("core.database.engine.init_database")
    def test_stop_after_failed_start_is_safe(self, mock_db, mock_start):
        """Calling stop() after failed start() must not raise."""
        mock_start.side_effect = RuntimeError("fail")

        eng = NexusEngine()
        with pytest.raises(IntradayEngineStartupError):
            eng.start()

        # stop() on a non-running engine should be a no-op
        eng.stop()  # Must not raise
        assert eng.is_running is False

    @patch("core.intraday.engine_integration.start_intraday_engine")
    @patch("core.database.engine.init_database")
    def test_successful_start_still_works(self, mock_db, mock_start):
        """Normal startup path still works when intraday engine succeeds."""
        mock_sb = MagicMock()
        mock_sb._strategies = [MagicMock()] * 5
        mock_start.return_value = mock_sb

        eng = NexusEngine()
        eng.start()

        assert eng.is_running is True
        assert eng.strategy_bus is mock_sb

        eng.stop()
        assert eng.is_running is False
