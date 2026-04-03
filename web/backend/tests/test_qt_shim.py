# ============================================================
# Tests for Qt Shim — validates that the pure-Python replacements
# correctly emulate the PySide6 APIs used by core/ modules.
# ============================================================
import sys
import os
import threading
import time
import pytest

# Ensure core_patch is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Install shim BEFORE any PySide6 imports
from core_patch import install_qt_shim
install_qt_shim()


class TestSignalShim:
    """Test that Signal() works as a descriptor and as an emitter."""

    def test_signal_descriptor_creates_per_instance(self):
        from PySide6.QtCore import QObject, Signal

        class Foo(QObject):
            my_signal = Signal(str)

        a = Foo()
        b = Foo()
        # Each instance should have its own signal
        assert a.my_signal is not b.my_signal

    def test_signal_connect_and_emit(self):
        from PySide6.QtCore import QObject, Signal

        class Foo(QObject):
            data_ready = Signal(int, str)

        results = []
        foo = Foo()
        foo.data_ready.connect(lambda val, msg: results.append((val, msg)))
        foo.data_ready.emit(42, "hello")
        assert results == [(42, "hello")]

    def test_signal_disconnect(self):
        from PySide6.QtCore import QObject, Signal

        class Foo(QObject):
            tick = Signal()

        count = [0]
        def handler():
            count[0] += 1

        foo = Foo()
        foo.tick.connect(handler)
        foo.tick.emit()
        assert count[0] == 1

        foo.tick.disconnect(handler)
        foo.tick.emit()
        assert count[0] == 1  # should not have incremented

    def test_signal_multiple_subscribers(self):
        from PySide6.QtCore import QObject, Signal

        class Foo(QObject):
            event = Signal(str)

        results = []
        foo = Foo()
        foo.event.connect(lambda s: results.append(f"a:{s}"))
        foo.event.connect(lambda s: results.append(f"b:{s}"))
        foo.event.emit("test")
        assert results == ["a:test", "b:test"]

    def test_signal_error_in_callback_does_not_crash(self):
        from PySide6.QtCore import QObject, Signal

        class Foo(QObject):
            tick = Signal()

        results = []
        foo = Foo()
        foo.tick.connect(lambda: 1/0)  # will raise
        foo.tick.connect(lambda: results.append("ok"))
        foo.tick.emit()
        assert results == ["ok"]  # second callback still ran


class TestQThreadShim:
    """Test that QThread works as a threading.Thread replacement."""

    def test_basic_thread_run(self):
        from PySide6.QtCore import QThread

        result = []

        class Worker(QThread):
            def run(self):
                result.append("ran")

        w = Worker()
        w.start()
        w.wait(5000)
        assert result == ["ran"]

    def test_thread_is_running(self):
        from PySide6.QtCore import QThread

        class Sleeper(QThread):
            def run(self):
                time.sleep(0.5)

        w = Sleeper()
        assert not w.isRunning()
        w.start()
        time.sleep(0.1)
        assert w.isRunning()
        w.wait(2000)
        assert not w.isRunning()

    def test_thread_quit(self):
        from PySide6.QtCore import QThread

        class LoopWorker(QThread):
            def run(self):
                while not self._stop_requested:
                    time.sleep(0.05)

        w = LoopWorker()
        w.start()
        time.sleep(0.1)
        assert w.isRunning()
        w.quit()
        w.wait(2000)
        assert not w.isRunning()


class TestQTimerShim:
    """Test that QTimer fires callbacks at intervals."""

    def test_timer_fires(self):
        from PySide6.QtCore import QTimer

        results = []
        timer = QTimer()
        timer.timeout.connect(lambda: results.append(1))
        timer.start(100)  # 100ms interval
        time.sleep(0.55)
        timer.stop()
        # Should have fired ~5 times
        assert len(results) >= 3

    def test_timer_stop(self):
        from PySide6.QtCore import QTimer

        results = []
        timer = QTimer()
        timer.timeout.connect(lambda: results.append(1))
        timer.start(50)
        time.sleep(0.2)
        timer.stop()
        count_at_stop = len(results)
        time.sleep(0.2)
        assert len(results) == count_at_stop  # no more fires after stop

    def test_single_shot(self):
        from PySide6.QtCore import QTimer

        results = []
        QTimer.singleShot(100, lambda: results.append("fired"))
        time.sleep(0.05)
        assert results == []
        time.sleep(0.15)
        assert results == ["fired"]


class TestSlotShim:
    """Test that Slot is a no-op decorator."""

    def test_slot_decorator(self):
        from PySide6.QtCore import Slot

        @Slot(str)
        def handler(msg):
            return msg

        assert handler("hello") == "hello"


class TestEventBus:
    """Test the de-Qt-ified EventBus."""

    def test_publish_subscribe(self):
        from core_patch.event_bus import EventBus

        bus = EventBus()
        results = []
        bus.subscribe("test.topic", lambda e: results.append(e.data))
        bus.publish("test.topic", data={"key": "value"})
        assert results == [{"key": "value"}]

    def test_wildcard_subscriber(self):
        from core_patch.event_bus import EventBus

        bus = EventBus()
        results = []
        bus.subscribe("*", lambda e: results.append(e.topic))
        bus.publish("a.b", data=1)
        bus.publish("c.d", data=2)
        assert results == ["a.b", "c.d"]

    def test_unsubscribe(self):
        from core_patch.event_bus import EventBus

        bus = EventBus()
        results = []
        cb = lambda e: results.append(1)
        bus.subscribe("t", cb)
        bus.publish("t")
        assert len(results) == 1
        bus.unsubscribe("t", cb)
        bus.publish("t")
        assert len(results) == 1

    def test_event_history(self):
        from core_patch.event_bus import EventBus

        bus = EventBus()
        for i in range(5):
            bus.publish("h.test", data=i)
        history = bus.get_history("h.test", limit=3)
        assert len(history) == 3
        assert history[0].data == 4  # most recent first

    def test_thread_safety(self):
        from core_patch.event_bus import EventBus

        bus = EventBus()
        count = [0]
        lock = threading.Lock()

        def handler(e):
            with lock:
                count[0] += 1

        bus.subscribe("mt", handler)

        def publisher():
            for _ in range(100):
                bus.publish("mt")

        threads = [threading.Thread(target=publisher) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert count[0] == 1000

    def test_event_serialisation(self):
        from core_patch.event_bus import Event

        e = Event(topic="test", data={"a": 1}, source="unit_test")
        d = e.to_dict()
        e2 = Event.from_dict(d)
        assert e2.topic == "test"
        assert e2.data == {"a": 1}
        assert e2.source == "unit_test"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
