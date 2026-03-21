# ============================================================
# NEXUS TRADER — Demo Live Monitor Page
#
# Full-page wrapper around DemoMonitorWidget.
# Shows real-time demo trading health: capital, heat, open
# positions, last 10 trades, streak, drawdown, and live vs
# Study 4 backtest comparison.
# ============================================================
from __future__ import annotations

from PySide6.QtWidgets import QWidget, QVBoxLayout

from gui.main_window import PageHeader
from gui.widgets.demo_monitor_widget import DemoMonitorWidget


class DemoMonitorPage(QWidget):
    """
    Sidebar page for the Demo Live Monitor.
    Contains a PageHeader and the DemoMonitorWidget.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setStyleSheet("background: #060b14;")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        header = PageHeader(
            title    = "Demo Live Monitor",
            subtitle = "Real-time Bybit Demo health · Capital · Heat · Drawdown · vs Study 4",
        )
        lay.addWidget(header)

        self._monitor = DemoMonitorWidget()
        lay.addWidget(self._monitor, 1)
