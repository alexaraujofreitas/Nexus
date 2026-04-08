# ============================================================
# Phase 2 Tests — Pure-Python EventBus
# 8 tests per PHASE1E_TEST_PLAN.md
# ============================================================
import sys
import threading
import time

import pytest

from core.event_bus import EventBus, Event, Topics, QtBridge


@pytest.fixture
def eb():
    """Fresh EventBus for each test."""
    bus = EventBus()
    yield bus
    bus.clear_subscribers()


class TestEventBusPure:
    """Verify EventBus works without any PySide6/Qt dependency."""

    def test_subscribe_and_publish(self, eb):
        """Basic subscribe → publish → callback fires."""
        received = []
        eb.subscribe("test.topic", lambda evt: received.append(evt))
        eb.publish("test.topic", {"key": "value"}, source="test")
        assert len(received) == 1
        assert received[0].topic == "test.topic"
        assert received[0].data == {"key": "value"}
        assert received[0].source == "test"

    def test_wildcard_subscriber(self, eb):
        """Wildcard '*' subscriber receives events from any topic."""
        received = []
        eb.subscribe("*", lambda evt: received.append(evt))
        eb.publish("topic.a", {"a": 1})
        eb.publish("topic.b", {"b": 2})
        assert len(received) == 2
        assert received[0].data == {"a": 1}
        assert received[1].data == {"b": 2}

    def test_unsubscribe(self, eb):
        """Unsubscribed callback no longer fires."""
        received = []
        cb = lambda evt: received.append(evt)
        eb.subscribe("test.unsub", cb)
        eb.publish("test.unsub", {})
        assert len(received) == 1

        eb.unsubscribe("test.unsub", cb)
        eb.publish("test.unsub", {})
        assert len(received) == 1  # no change

    def test_thread_safety_concurrent_publish(self, eb):
        """Multiple threads publishing concurrently — no crashes, no lost events."""
        received = []
        lock = threading.Lock()

        def _handler(evt):
            with lock:
                received.append(evt)

        eb.subscribe("stress", _handler)

        def _publisher(n):
            for i in range(100):
                eb.publish("stress", {"thread": n, "i": i})

        threads = [threading.Thread(target=_publisher, args=(t,)) for t in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(received) == 500

    def test_no_pyside6_import(self, eb):
        """EventBus module does NOT import PySide6 at module level."""
        import core.event_bus as mod
        # Check that the module source does not have a top-level PySide6 import
        import inspect
        source = inspect.getsource(mod)
        # Should not have "from PySide6" at top level (only inside QtBridge.attach)
        lines = source.split('\n')
        top_level_imports = [
            line for line in lines
            if line.startswith('from PySide6') or line.startswith('import PySide6')
        ]
        assert len(top_level_imports) == 0, f"Found top-level PySide6 imports: {top_level_imports}"

    def test_event_history(self, eb):
        """get_history() returns recent events in reverse chronological order."""
        eb.publish("h.1", {"seq": 1})
        eb.publish("h.2", {"seq": 2})
        eb.publish("h.1", {"seq": 3})

        all_hist = eb.get_history(limit=10)
        assert len(all_hist) == 3
        assert all_hist[0].data == {"seq": 3}  # most recent first

        topic_hist = eb.get_history(topic="h.1", limit=10)
        assert len(topic_hist) == 2

    def test_callback_error_isolation(self, eb):
        """One callback raising an exception does not prevent others from firing."""
        results = []

        def bad_cb(evt):
            raise ValueError("intentional test error")

        def good_cb(evt):
            results.append(evt.data)

        eb.subscribe("error.test", bad_cb)
        eb.subscribe("error.test", good_cb)

        eb.publish("error.test", {"ok": True})
        assert results == [{"ok": True}]

    def test_qt_bridge_adapter_optional(self, eb):
        """QtBridge.attach() is a no-op when PySide6 is not available."""
        # Even if PySide6 IS available in this test env, the bridge should
        # work correctly and be detachable
        original_bridge = eb._qt_bridge

        # Detach any existing bridge
        QtBridge.detach(eb)
        assert eb._qt_bridge is None

        # Publish still works without bridge
        received = []
        eb.subscribe("no.bridge", lambda evt: received.append(evt))
        eb.publish("no.bridge", {"works": True})
        assert len(received) == 1

        # Restore
        eb._qt_bridge = original_bridge
