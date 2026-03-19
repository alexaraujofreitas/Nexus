# ============================================================
# NEXUS TRADER — Logs Page + Logging System Setup
# ============================================================

import logging
import logging.handlers
from datetime import datetime
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QFrame, QTableWidget, QTableWidgetItem, QComboBox,
    QLineEdit, QHeaderView, QAbstractItemView
)
from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont

from gui.main_window import PageHeader
from config.constants import LOG_FILE, LOG_MAX_BYTES, LOG_BACKUP_COUNT
from core.database.engine import get_session
from core.database.models import SystemLog
from core.event_bus import bus, Topics

logger = logging.getLogger(__name__)

# ── Logging System Setup ──────────────────────────────────────
class DatabaseLogHandler(logging.Handler):
    """Python log handler that writes to the SQLite system_logs table."""

    def emit(self, record: logging.LogRecord):
        try:
            with get_session() as session:
                session.add(SystemLog(
                    timestamp=datetime.utcfromtimestamp(record.created),
                    level=record.levelname,
                    module=record.name,
                    message=self.format(record),
                    details={"exc_info": str(record.exc_info)} if record.exc_info else None,
                ))
        except Exception:
            pass  # Never let log handler crash the app


def setup_logging():
    """Configure the application-wide logging system."""
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    fmt = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-8s | %(name)-35s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )

    # File handler (rotating)
    try:
        file_handler = logging.handlers.RotatingFileHandler(
            filename=str(LOG_FILE),
            maxBytes=LOG_MAX_BYTES,
            backupCount=LOG_BACKUP_COUNT,
            encoding="utf-8",
        )
        file_handler.setFormatter(fmt)
        file_handler.setLevel(logging.DEBUG)
        root.addHandler(file_handler)
    except Exception as e:
        print(f"[Logging] Could not set up file handler: {e}")

    # Console handler (startup only)
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(fmt)
    console_handler.setLevel(logging.INFO)
    root.addHandler(console_handler)

    # Database handler
    db_handler = DatabaseLogHandler()
    db_handler.setFormatter(fmt)
    db_handler.setLevel(logging.INFO)
    root.addHandler(db_handler)

    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
    logging.getLogger("ccxt").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("asyncio").setLevel(logging.WARNING)

    logger.info("Nexus Trader logging initialized — log file: %s", LOG_FILE)


# ── Log Level Colors ──────────────────────────────────────────
LEVEL_COLORS = {
    "DEBUG":    "#4A5568",
    "INFO":     "#8899AA",
    "WARNING":  "#FFB300",
    "ERROR":    "#FF3355",
    "CRITICAL": "#FF0044",
}


class LogsPage(QWidget):
    """Real-time log viewer with filtering and search."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._build()
        self._start_refresh_timer()

    def _build(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Header
        header = PageHeader("System Logs", "Real-time application event log")
        btn_clear = QPushButton("Clear View")
        btn_clear.setObjectName("btn_ghost")
        btn_clear.setFixedHeight(32)
        btn_clear.setCursor(Qt.PointingHandCursor)
        btn_clear.clicked.connect(self._clear_table)

        btn_refresh = QPushButton("⟳  Refresh")
        btn_refresh.setObjectName("btn_ghost")
        btn_refresh.setFixedHeight(32)
        btn_refresh.setCursor(Qt.PointingHandCursor)
        btn_refresh.clicked.connect(self._refresh)

        header.add_action(btn_clear)
        header.add_action(btn_refresh)
        layout.addWidget(header)

        # Filters
        filter_bar = QFrame()
        filter_bar.setStyleSheet("background: #0F1623; border-bottom: 1px solid #1E2D40;")
        filter_bar.setFixedHeight(52)
        filter_layout = QHBoxLayout(filter_bar)
        filter_layout.setContentsMargins(24, 8, 24, 8)
        filter_layout.setSpacing(12)

        # Level filter
        lbl = QLabel("Level:")
        lbl.setStyleSheet("color: #8899AA; font-size: 13px;")
        filter_layout.addWidget(lbl)

        self.combo_level = QComboBox()
        self.combo_level.setFixedWidth(120)
        for level in ["ALL", "DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]:
            self.combo_level.addItem(level)
        self.combo_level.setCurrentText("INFO")
        self.combo_level.currentTextChanged.connect(self._refresh)
        filter_layout.addWidget(self.combo_level)

        # Module filter
        lbl2 = QLabel("Module:")
        lbl2.setStyleSheet("color: #8899AA; font-size: 13px;")
        filter_layout.addWidget(lbl2)

        self.txt_module = QLineEdit()
        self.txt_module.setPlaceholderText("Filter by module name...")
        self.txt_module.setFixedWidth(200)
        self.txt_module.textChanged.connect(self._refresh)
        filter_layout.addWidget(self.txt_module)

        # Search
        lbl3 = QLabel("Search:")
        lbl3.setStyleSheet("color: #8899AA; font-size: 13px;")
        filter_layout.addWidget(lbl3)

        self.txt_search = QLineEdit()
        self.txt_search.setPlaceholderText("Search log messages...")
        self.txt_search.setFixedWidth(280)
        self.txt_search.textChanged.connect(self._refresh)
        filter_layout.addWidget(self.txt_search)

        filter_layout.addStretch()

        # Auto-scroll
        self.chk_autoscroll = QPushButton("⬇ Auto-scroll: ON")
        self.chk_autoscroll.setCheckable(True)
        self.chk_autoscroll.setChecked(True)
        self.chk_autoscroll.setObjectName("btn_ghost")
        self.chk_autoscroll.setFixedHeight(30)
        filter_layout.addWidget(self.chk_autoscroll)

        layout.addWidget(filter_bar)

        # Table
        self.table = QTableWidget()
        self.table.setColumnCount(4)
        self.table.setHorizontalHeaderLabels(["Timestamp", "Level", "Module", "Message"])
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        self.table.horizontalHeader().resizeSection(0, 170)
        self.table.horizontalHeader().resizeSection(1, 80)
        self.table.horizontalHeader().resizeSection(2, 200)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setWordWrap(False)

        layout.addWidget(self.table, 1)

        # Bottom stats
        self._stats_lbl = QLabel("Loading logs...")
        self._stats_lbl.setStyleSheet(
            "background: #0F1623; border-top: 1px solid #1E2D40; "
            "color: #4A5568; font-size: 13px; padding: 4px 24px;"
        )
        self._stats_lbl.setFixedHeight(26)
        layout.addWidget(self._stats_lbl)

    def _start_refresh_timer(self):
        self._timer = QTimer()
        self._timer.timeout.connect(self._refresh)
        self._timer.start(3000)  # Refresh every 3 seconds
        self._refresh()

    def _refresh(self):
        level_filter = self.combo_level.currentText()
        module_filter = self.txt_module.text().strip().lower()
        search_filter = self.txt_search.text().strip().lower()

        try:
            with get_session() as session:
                query = session.query(SystemLog).order_by(SystemLog.timestamp.desc())

                if level_filter != "ALL":
                    level_order = {"DEBUG": 0, "INFO": 1, "WARNING": 2, "ERROR": 3, "CRITICAL": 4}
                    min_level = level_order.get(level_filter, 0)
                    query = query.filter(
                        SystemLog.level.in_([
                            l for l, v in level_order.items() if v >= min_level
                        ])
                    )

                if module_filter:
                    query = query.filter(SystemLog.module.ilike(f"%{module_filter}%"))

                if search_filter:
                    query = query.filter(SystemLog.message.ilike(f"%{search_filter}%"))

                # Convert to plain dicts INSIDE the session before it closes
                logs = [
                    {
                        "timestamp": r.timestamp,
                        "level":     r.level,
                        "module":    r.module,
                        "message":   r.message,
                    }
                    for r in query.limit(500).all()
                ]

        except Exception as e:
            logger.warning("Could not load logs from DB: %s", e)
            return

        self.table.setRowCount(0)
        self.table.setRowCount(len(logs))

        for row, log in enumerate(logs):
            ts = log["timestamp"].strftime("%Y-%m-%d %H:%M:%S") if log["timestamp"] else ""
            color = QColor(LEVEL_COLORS.get(log["level"], "#8899AA"))

            items = [ts, log["level"], log["module"], log["message"]]
            for col, text in enumerate(items):
                item = QTableWidgetItem(str(text or ""))
                item.setForeground(color)
                if col == 1:  # Level column - bold
                    font = QFont()
                    font.setBold(True)
                    item.setFont(font)
                self.table.setItem(row, col, item)

        if self.chk_autoscroll.isChecked() and self.table.rowCount() > 0:
            self.table.scrollToBottom()

        self._stats_lbl.setText(
            f"Showing {len(logs)} log entries  |  "
            f"Filter: {level_filter}  |  Auto-refresh: every 3s"
        )

    def _clear_table(self):
        self.table.setRowCount(0)
