# ============================================================
# Phase 2 Tests — AgentCoordinator Headless
# 4 tests per PHASE1E_TEST_PLAN.md
# ============================================================
import pytest
from unittest.mock import patch, MagicMock


class TestAgentCoordinatorHeadless:

    def test_starts_exactly_4_agents(self):
        """AgentCoordinator.start_all() starts exactly 4 agents."""
        # Patch all 4 agent imports to return mock agents
        mock_agents = []

        def _make_mock_agent(name):
            agent = MagicMock()
            agent._name = name
            agent.is_alive.return_value = True
            agent.isRunning.return_value = True
            agent.last_signal = {"signal": 0.0, "confidence": 0.0, "has_data": False}
            agent.is_stale = False
            agent._consecutive_errors = 0
            mock_agents.append(agent)
            return agent

        with patch("core.agents.funding_rate_agent.FundingRateAgent", side_effect=lambda: _make_mock_agent("funding_rate")), \
             patch("core.agents.order_book_agent.OrderBookAgent", side_effect=lambda: _make_mock_agent("order_book")), \
             patch("core.agents.liquidation_flow_agent.LiquidationFlowAgent", side_effect=lambda: _make_mock_agent("liquidation_flow")), \
             patch("core.agents.crash_detection_agent.CrashDetectionAgent", side_effect=lambda: _make_mock_agent("crash_detection")), \
             patch("core.scanning.btc_priority.get_btc_priority_filter", return_value=MagicMock()):

            from core.agents.agent_coordinator import AgentCoordinator
            coord = AgentCoordinator()
            coord.start_all()

            assert len(coord._agents) == 4
            assert coord.is_running is True
            for agent in mock_agents:
                agent.start.assert_called_once()

    def test_stop_all_cleans_up(self):
        """stop_all() calls stop() and wait() on each agent."""
        mock_agent = MagicMock()
        mock_agent._name = "test"
        mock_agent.last_signal = {}
        mock_agent.is_stale = False
        mock_agent._consecutive_errors = 0

        from core.agents.agent_coordinator import AgentCoordinator
        coord = AgentCoordinator()
        coord._agents = [mock_agent]
        coord._running = True

        coord.stop_all()

        mock_agent.stop.assert_called_once()
        mock_agent.wait.assert_called_once_with(3000)
        assert coord.is_running is False
        assert len(coord._agents) == 0

    def test_no_pyside6_import(self):
        """agent_coordinator module does NOT import PySide6 at top level."""
        import core.agents.agent_coordinator as mod
        import inspect
        source = inspect.getsource(mod)
        lines = source.split('\n')
        top_level_imports = [
            line for line in lines
            if line.startswith('from PySide6') or line.startswith('import PySide6')
        ]
        assert len(top_level_imports) == 0, f"Found PySide6 imports: {top_level_imports}"

    def test_agent_status_reporting(self):
        """get_status() returns status for all running agents."""
        mock_agent = MagicMock()
        mock_agent._name = "funding_rate"
        mock_agent.isRunning.return_value = True
        mock_agent.is_stale = False
        mock_agent._consecutive_errors = 0
        mock_agent.last_signal = {
            "signal": 0.3,
            "confidence": 0.7,
            "has_data": True,
            "updated_at": "2026-04-06T12:00:00+00:00",
        }

        from core.agents.agent_coordinator import AgentCoordinator
        coord = AgentCoordinator()
        coord._agents = [mock_agent]

        status = coord.get_status()
        assert "funding_rate" in status
        assert status["funding_rate"]["running"] is True
        assert status["funding_rate"]["signal"] == 0.3
