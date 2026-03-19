# ============================================================
# NEXUS TRADER — PageBase  (audit 9.3 – UI Architecture)
#
# Provides common infrastructure for all page widgets:
#
#   • Background task runner (QThreadPool) so DB/network calls
#     never block the main thread.
#   • Unified status bar helper with themed colours.
#   • Loading overlay skeleton that any page can show/hide.
#   • Consistent error-display pattern.
#
# Usage
# -----
#   class MyPage(PageBase):
#       def showEvent(self, e):
#           super().showEvent(e)
#           self.run_background(self._load_data, self._on_data_ready)
#
#       def _load_data(self):           # runs in thread pool
#           return expensive_db_query()
#
#       def _on_data_ready(self, result, error):  # runs in main thread
#           if error:
#               self.set_status(str(error), "error")
#           else:
#               self._populate(result)
# ============================================================
from __future__ import annotations

import logging
import traceback
from typing import Any, Callable, Optional

from PySide6.QtCore import QObject, QRunnable, Qt, QThreadPool, Signal, Slot
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

logger = logging.getLogger(__name__)

# ── Palette ───────────────────────────────────────────────────
_MUTED  = "#8899AA"
_GREEN  = "#00CC77"
_ORANGE = "#FF9800"
_RED    = "#FF3355"
_BLUE   = "#1E90FF"
_BORDER = "#1A2332"
_CARD   = "#0D1320"


# ─────────────────────────────────────────────────────────────
# Worker infrastructure (QThreadPool)
# ─────────────────────────────────────────────────────────────
class _WorkerSignals(QObject):
    finished = Signal(object, object)  # (result, error)


class _Worker(QRunnable):
    """
    Generic runnable that executes *fn* in a thread pool thread,
    then emits finished(result, error) back to the main thread.
    """

    def __init__(self, fn: Callable, *args, **kwargs):
        super().__init__()
        self._fn     = fn
        self._args   = args
        self._kwargs = kwargs
        self.signals = _WorkerSignals()

    def run(self):
        try:
            result = self._fn(*self._args, **self._kwargs)
            self.signals.finished.emit(result, None)
        except Exception as exc:
            logger.debug("Background worker error: %s", exc)
            self.signals.finished.emit(None, exc)


# ─────────────────────────────────────────────────────────────
# Status bar widget
# ─────────────────────────────────────────────────────────────
class PageStatusBar(QFrame):
    """
    Thin horizontal bar rendered at the bottom of a page section.
    Displays a message with colour-coded severity.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(22)
        self.setStyleSheet(f"QFrame {{ background:{_CARD}; border-top:1px solid {_BORDER}; }}")
        h = QHBoxLayout(self)
        h.setContentsMargins(10, 0, 10, 0)
        h.setSpacing(6)
        self._lbl = QLabel("")
        self._lbl.setStyleSheet(f"color:{_MUTED}; font-size:13px;")
        h.addWidget(self._lbl)
        h.addStretch()
        self._spinner = QLabel("")
        self._spinner.setStyleSheet(f"color:{_MUTED}; font-size:13px;")
        h.addWidget(self._spinner)

    def set(self, message: str, level: str = "info"):
        """
        level: "info" | "success" | "warning" | "error" | "loading"
        """
        color_map = {
            "info":    _MUTED,
            "success": _GREEN,
            "warning": _ORANGE,
            "error":   _RED,
            "loading": _BLUE,
        }
        color = color_map.get(level, _MUTED)
        prefix_map = {
            "success": "✓ ",
            "warning": "⚠ ",
            "error":   "✗ ",
            "loading": "⟳ ",
        }
        prefix = prefix_map.get(level, "")
        self._lbl.setStyleSheet(f"color:{color}; font-size:13px;")
        self._lbl.setText(f"{prefix}{message}")
        self._spinner.setText("⟳" if level == "loading" else "")

    def clear(self):
        self._lbl.setText("")
        self._spinner.setText("")


# ─────────────────────────────────────────────────────────────
# PageBase widget
# ─────────────────────────────────────────────────────────────
class PageBase(QWidget):
    """
    Base class for NexusTrader page widgets.
    Provides:
      • run_background(fn, callback)  — non-blocking thread pool task
      • set_status(msg, level)        — thread-safe status bar update
      • _status_bar                   — embedded PageStatusBar (add to layout if needed)
    """

    # Emitted from background threads to update status on main thread
    _status_signal = Signal(str, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._pool = QThreadPool.globalInstance()
        self._status_bar = PageStatusBar(self)
        self._status_signal.connect(self._status_bar.set)

    # ── Background task runner ────────────────────────────────
    def run_background(
        self,
        fn: Callable,
        callback: Callable[[Any, Optional[Exception]], None],
        *args,
        **kwargs,
    ) -> None:
        """
        Run *fn* in a thread pool thread.
        When done, call *callback(result, error)* on the main thread.

        Parameters
        ----------
        fn       : Callable — the (potentially slow) function to run
        callback : Callable(result, error) — main-thread completion handler
        *args, **kwargs : forwarded to fn
        """
        worker = _Worker(fn, *args, **kwargs)
        worker.signals.finished.connect(
            lambda result, error: callback(result, error)
        )
        self._pool.start(worker)

    # ── Status helpers (thread-safe) ──────────────────────────
    def set_status(self, message: str, level: str = "info") -> None:
        """Update the page status bar from any thread."""
        self._status_signal.emit(message, level)

    def clear_status(self) -> None:
        self._status_signal.emit("", "info")

    # ── Convenience: show error dialog from main thread ───────
    @Slot()
    def _show_error(self, message: str) -> None:
        from PySide6.QtWidgets import QMessageBox
        QMessageBox.critical(self, "Error", message)
