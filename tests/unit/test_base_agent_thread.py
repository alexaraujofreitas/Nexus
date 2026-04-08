# ============================================================
# Phase 2 Tests — BaseAgent Threading Decouple
# 4 tests per PHASE1E_TEST_PLAN.md
#
# NOTE: bus is accessed via the base_agent module's own reference
# (not a separate module-level import) to guarantee the test and
# the agent share the same EventBus instance.  This avoids
# sys.modules contamination from other test files that patch
# core.event_bus at collection time.
# ============================================================
import time
import threading

import pytest


class _TestAgent:
    """Minimal concrete agent for testing BaseAgent."""

    @staticmethod
    def create(poll_interval=1, fetch_data=None, process_result=None):
        """Factory that creates a BaseAgent subclass instance."""
        from core.agents.base_agent import BaseAgent

        class ConcreteAgent(BaseAgent):
            @property
            def event_topic(self) -> str:
                return "test.agent.signal"

            @property
            def poll_interval_seconds(self) -> int:
                return poll_interval

            def fetch(self):
                return fetch_data or {"raw": True}

            def process(self, raw):
                return process_result or {
                    "signal": 0.5,
                    "confidence": 0.8,
                    "has_data": True,
                }

        return ConcreteAgent(name="test_agent")


def _get_bus():
    """
    Return the bus object that BaseAgent actually uses at runtime.

    We pull it from the base_agent module itself so the test subscriber
    and the agent publisher are guaranteed to share the same EventBus
    instance — even if sys.modules["core.event_bus"] was overwritten
    by another test file at collection time.
    """
    import core.agents.base_agent as _ba
    return _ba.bus


class TestBaseAgentThread:

    def test_agent_starts_and_stops(self):
        """Agent thread starts, runs at least one cycle, then stops cleanly."""
        bus = _get_bus()
        agent = _TestAgent.create(poll_interval=1)
        received = []
        bus.subscribe("test.agent.signal", lambda evt: received.append(evt))

        agent.start()
        time.sleep(2.5)  # allow at least 1 cycle
        agent.stop()
        agent.wait(3000)

        assert not agent.is_alive()
        assert len(received) >= 1
        assert received[0].data["signal"] == 0.5

        bus.clear_subscribers("test.agent.signal")

    def test_fetch_process_publish_cycle(self):
        """fetch() → process() → publish() cycle runs correctly."""
        bus = _get_bus()
        agent = _TestAgent.create(
            poll_interval=1,
            fetch_data={"price": 65000},
            process_result={"signal": 0.75, "confidence": 0.9, "has_data": True},
        )
        received = []
        bus.subscribe("test.agent.signal", lambda evt: received.append(evt))

        agent.start()
        time.sleep(2.5)
        agent.stop()
        agent.wait(3000)

        assert len(received) >= 1
        assert received[0].data["signal"] == 0.75
        assert received[0].data["confidence"] == 0.9
        assert received[0].data["source"] == "test_agent"
        assert received[0].data["stale"] is False

        bus.clear_subscribers("test.agent.signal")

    def test_error_backoff(self):
        """Errors trigger backoff but don't crash the thread."""
        from core.agents import base_agent as ba_mod
        from core.agents.base_agent import BaseAgent

        # Temporarily reduce backoff constants for fast test
        orig_base = ba_mod._BASE_BACKOFF_S
        ba_mod._BASE_BACKOFF_S = 1

        call_count = 0

        class ErrorAgent(BaseAgent):
            @property
            def event_topic(self):
                return "test.error.signal"

            @property
            def poll_interval_seconds(self):
                return 1

            def fetch(self):
                nonlocal call_count
                call_count += 1
                if call_count <= 2:
                    raise RuntimeError("test error")
                return {"ok": True}

            def process(self, raw):
                return {"signal": 0.1, "confidence": 0.5, "has_data": True}

        try:
            agent = ErrorAgent(name="error_agent")
            agent.start()
            time.sleep(8)  # enough for 2 errors (1s each) + recovery cycle
            agent.stop()
            agent.wait(3000)

            assert not agent.is_alive()
            assert agent._consecutive_errors == 0  # should have recovered
        finally:
            ba_mod._BASE_BACKOFF_S = orig_base

    def test_no_pyside6_import(self):
        """base_agent module does NOT import PySide6 at top level."""
        import core.agents.base_agent as mod
        import inspect
        source = inspect.getsource(mod)
        lines = source.split('\n')
        top_level_imports = [
            line for line in lines
            if line.startswith('from PySide6') or line.startswith('import PySide6')
        ]
        assert len(top_level_imports) == 0, f"Found PySide6 imports: {top_level_imports}"
