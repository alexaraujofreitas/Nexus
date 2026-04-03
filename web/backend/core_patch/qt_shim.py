# ============================================================
# NEXUS TRADER — Qt Compatibility Shim (Web/Headless Edition)
#
# Provides drop-in replacements for PySide6 classes used in
# core/ modules, allowing them to run without Qt installed.
#
# This shim is injected via sys.modules so that:
#   from PySide6.QtCore import QObject, QThread, Signal, QTimer
# resolves to these pure-Python equivalents without modifying
# any core/ source files.
#
# Pattern replacements:
#   QObject   → plain Python class (no-op)
#   QThread   → threading.Thread subclass with run() contract
#   Signal    → callback-based signal emitter
#   QTimer    → threading-based timer
#   Slot      → no-op decorator
#   Qt        → namespace with connection type constants
# ============================================================
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


class _Signal:
    """
    Pure-Python replacement for PySide6 Signal.

    Supports:
      - signal.connect(callback)
      - signal.disconnect(callback)
      - signal.emit(*args)

    Note: Unlike Qt Signals, these are instance-level (not class-level
    descriptors).  The ShimSignalDescriptor below bridges the gap.
    """

    def __init__(self, *types):
        self._callbacks: list[Callable] = []
        self._lock = threading.Lock()
        self._types = types  # kept for documentation only

    def connect(self, callback: Callable) -> None:
        with self._lock:
            if callback not in self._callbacks:
                self._callbacks.append(callback)

    def disconnect(self, callback: Optional[Callable] = None) -> None:
        with self._lock:
            if callback is None:
                self._callbacks.clear()
            else:
                self._callbacks = [c for c in self._callbacks if c is not callback]

    def emit(self, *args) -> None:
        with self._lock:
            callbacks = list(self._callbacks)
        for cb in callbacks:
            try:
                cb(*args)
            except Exception as e:
                logger.error("Signal callback error: %s", e, exc_info=True)


class ShimSignalDescriptor:
    """
    Descriptor that creates per-instance _Signal objects, mimicking
    Qt's class-level Signal definition → instance-level bound signal.

    Usage in class body:
        class Foo(QObject):
            my_signal = Signal(str, int)

    At instance access:
        foo = Foo()
        foo.my_signal.connect(handler)
        foo.my_signal.emit("hello", 42)
    """

    def __init__(self, *types):
        self._types = types
        self._attr_name: Optional[str] = None

    def __set_name__(self, owner, name):
        self._attr_name = f"_shimsg_{name}"

    def __get__(self, obj, objtype=None):
        if obj is None:
            return self  # class-level access returns the descriptor
        # Lazy-create per-instance _Signal
        sig = getattr(obj, self._attr_name, None)
        if sig is None:
            sig = _Signal(*self._types)
            setattr(obj, self._attr_name, sig)
        return sig


# Public alias: used exactly like PySide6's Signal() in class bodies
def Signal(*types):
    """Create a signal descriptor. Usage: my_signal = Signal(str, int)"""
    return ShimSignalDescriptor(*types)


class QObject:
    """No-op replacement for PySide6.QtCore.QObject."""

    def __init__(self, parent=None):
        pass


class QThread(threading.Thread):
    """
    Drop-in replacement for PySide6.QtCore.QThread.

    Subclasses override run() just like PySide6.  The key differences:
      - start() is inherited from threading.Thread
      - isRunning() maps to is_alive()
      - quit() / requestInterruption() sets a stop flag
      - No event loop (exec_() is a no-op)
    """

    # Signals that QThread subclasses commonly define as class-level
    started_signal = Signal()
    finished_signal = Signal()

    def __init__(self, parent=None):
        super().__init__(daemon=True)
        self._stop_requested = False

    def run(self):
        """Override this in subclasses."""
        pass

    def start(self, priority=None):
        """Start the thread. priority is ignored (Qt concept)."""
        self._stop_requested = False
        try:
            super().start()
        except RuntimeError:
            # Thread already started / cannot start twice
            pass

    def isRunning(self) -> bool:
        return self.is_alive()

    def quit(self):
        """Request thread to stop."""
        self._stop_requested = True

    def requestInterruption(self):
        """Request thread to stop (alias for quit)."""
        self._stop_requested = True

    def isInterruptionRequested(self) -> bool:
        return self._stop_requested

    def wait(self, timeout_ms: int = 0) -> bool:
        """Wait for thread to finish. Returns True if finished."""
        timeout_s = timeout_ms / 1000.0 if timeout_ms > 0 else None
        self.join(timeout=timeout_s)
        return not self.is_alive()

    def msleep(self, ms: int):
        time.sleep(ms / 1000.0)


class QTimer:
    """
    Pure-Python replacement for PySide6.QtCore.QTimer.

    Supports:
      - timer.start(interval_ms)
      - timer.stop()
      - timer.timeout.connect(callback)
      - QTimer.singleShot(ms, callback)
    """

    def __init__(self, parent=None):
        self._interval_ms = 0
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self.timeout = _Signal()

    def start(self, interval_ms: Optional[int] = None) -> None:
        if interval_ms is not None:
            self._interval_ms = interval_ms
        self.stop()  # stop any existing timer
        self._stop_event.clear()
        self._running = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2)
        self._thread = None

    def isActive(self) -> bool:
        return self._running

    def setInterval(self, ms: int) -> None:
        self._interval_ms = ms

    def interval(self) -> int:
        return self._interval_ms

    def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            self._stop_event.wait(self._interval_ms / 1000.0)
            if not self._stop_event.is_set():
                self.timeout.emit()

    @staticmethod
    def singleShot(ms: int, callback: Callable) -> None:
        """Fire callback once after ms milliseconds."""
        def _delayed():
            time.sleep(ms / 1000.0)
            try:
                callback()
            except Exception as e:
                logger.error("QTimer.singleShot callback error: %s", e, exc_info=True)

        t = threading.Thread(target=_delayed, daemon=True)
        t.start()


def Slot(*types, **kwargs):
    """No-op decorator replacing PySide6.QtCore.Slot."""
    def decorator(func):
        return func
    return decorator


class _Qt:
    """Namespace for Qt constants."""
    QueuedConnection = 1
    AutoConnection = 0
    DirectConnection = 2

Qt = _Qt()


class _QMetaObject:
    """No-op replacement for QMetaObject."""
    @staticmethod
    def invokeMethod(*args, **kwargs):
        pass

QMetaObject = _QMetaObject()
