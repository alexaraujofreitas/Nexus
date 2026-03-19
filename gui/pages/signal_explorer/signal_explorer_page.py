# ============================================================
# NEXUS TRADER — Signal Explorer  (Phase 12)
#
# Browse and analyze historical IDSS signals from the database.
# Shows:
#   - Filterable signal table (symbol, strategy, regime, date)
#   - Signal detail panel (all ModelSignal fields)
#   - Signal performance tracking (P&L per signal if trade was taken)
#   - Strategy signal distribution chart
# ============================================================
from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QFrame,
    QScrollArea, QGroupBox, QTableWidget, QTableWidgetItem,
    QHeaderView, QPushButton, QComboBox, QDateEdit,
    QLineEdit, QSplitter, QTextEdit, QSizePolicy,
)
from PySide6.QtCore import Qt, QDate, QThread, Signal
from PySide6.QtGui import QColor, QFont

from gui.main_window import PageHeader

logger = logging.getLogger(__name__)

_GREEN  = "#00FF88"
_RED    = "#FF3355"
_BLUE   = "#4299E1"
_YELLOW = "#F6AD55"
_GRAY   = "#4A5568"
_LIGHT  = "#C8D0E0"

_DIR_COLORS = {"long": _GREEN, "short": _RED}


class _LoadWorker(QThread):
    """Load signals from DB in background thread with pagination."""
    data_ready = Signal(dict)  # Changed to dict to include total_count
    error_occurred = Signal(str)

    def __init__(self, filters: dict, page: int = 0, page_size: int = 50, parent=None):
        super().__init__(parent)
        self._filters = filters
        self._page = page
        self._page_size = page_size

    def run(self):
        try:
            rows, total_count = self._load(self._filters, self._page, self._page_size)
            self.data_ready.emit({"rows": rows, "total_count": total_count})
        except Exception as exc:
            self.error_occurred.emit(str(exc))

    def _load(self, filters: dict, page: int, page_size: int) -> tuple[list[dict], int]:
        from core.database.engine import get_session
        from core.database.models import SignalLog
        from sqlalchemy import desc, func

        symbol    = filters.get("symbol", "")
        strategy  = filters.get("strategy", "")
        direction = filters.get("direction", "")
        regime    = filters.get("regime", "")
        days      = int(filters.get("days", 30))

        since = datetime.now(timezone.utc) - timedelta(days=days)

        with get_session() as session:
            q = session.query(SignalLog).filter(
                SignalLog.timestamp >= since
            )
            if symbol:
                q = q.filter(SignalLog.symbol.ilike(f"%{symbol}%"))
            if strategy:
                q = q.filter(SignalLog.strategy_name.ilike(f"%{strategy}%"))
            if direction in ("long", "short"):
                q = q.filter(SignalLog.direction == direction)
            if regime:
                q = q.filter(SignalLog.regime == regime)

            # Get total count before pagination
            total_count = session.query(func.count(SignalLog.id)).filter(
                SignalLog.timestamp >= since
            )
            if symbol:
                total_count = total_count.filter(SignalLog.symbol.ilike(f"%{symbol}%"))
            if strategy:
                total_count = total_count.filter(SignalLog.strategy_name.ilike(f"%{strategy}%"))
            if direction in ("long", "short"):
                total_count = total_count.filter(SignalLog.direction == direction)
            if regime:
                total_count = total_count.filter(SignalLog.regime == regime)
            total_count = total_count.scalar() or 0

            # Apply pagination
            rows = q.order_by(desc(SignalLog.timestamp)).offset(
                page * page_size
            ).limit(page_size).all()

            # Build plain dicts while the session is still open.
            # Accessing ORM attributes after the session closes triggers a
            # DetachedInstanceError because SQLAlchemy tries to lazy-reload.
            result = []
            for r in rows:
                result.append({
                    "id":          r.id,
                    "timestamp":   r.timestamp.isoformat() if r.timestamp else "—",
                    "symbol":      r.symbol or "—",
                    "strategy":    r.strategy_name or "—",
                    "direction":   r.direction or "—",
                    "strength":    round(float(r.strength or 0), 4),
                    "entry_price": round(float(r.entry_price or 0), 4),
                    "stop_loss":   round(float(r.stop_loss or 0), 4),
                    "take_profit": round(float(r.take_profit or 0), 4),
                    "regime":      r.regime or "—",
                    "timeframe":   r.timeframe or "—",
                    "rationale":   r.rationale or "—",
                })

        return result, total_count


class SignalExplorerPage(QWidget):
    """
    Browse and filter all historical IDSS signals.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._rows: list[dict] = []
        self._worker = None
        self._page_size: int = 50
        self._current_page: int = 0
        self._total_count: int = 0
        self._build()
        self._load_signals()

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        root.addWidget(PageHeader(
            "Signal Explorer",
            "Browse, filter, and analyze historical IDSS trade signals"
        ))

        # Filter bar
        root.addWidget(self._build_filter_bar())

        # Main content — table + detail pane
        splitter = QSplitter(Qt.Horizontal)
        splitter.setHandleWidth(4)

        # Left: signal table
        left_widget = QWidget()
        lv = QVBoxLayout(left_widget)
        lv.setContentsMargins(16, 8, 8, 16)
        lv.setSpacing(8)

        lv.addWidget(self._build_table())
        left_widget.setMinimumWidth(500)
        splitter.addWidget(left_widget)

        # Right: detail panel
        right_widget = QWidget()
        rv = QVBoxLayout(right_widget)
        rv.setContentsMargins(8, 8, 16, 16)
        rv.setSpacing(8)
        rv.addWidget(self._build_detail_panel())
        splitter.addWidget(right_widget)
        splitter.setSizes([600, 320])

        root.addWidget(splitter, 1)

    def _build_filter_bar(self) -> QFrame:
        bar = QFrame()
        bar.setObjectName("FilterBar")
        bar.setFixedHeight(100)  # Increased height for pagination controls
        bar.setStyleSheet(
            "QFrame#FilterBar { background:#0D1B2A; border-bottom:1px solid #1E3A5F; }"
        )
        v = QVBoxLayout(bar)
        v.setContentsMargins(16, 8, 16, 8)
        v.setSpacing(8)

        # Filter row
        h = QHBoxLayout()
        h.setSpacing(12)

        # Symbol filter — fixed dropdown of supported pairs
        h.addWidget(QLabel("Symbol:"))
        self._sym_filter = QComboBox()
        self._sym_filter.addItems([
            "All",
            "BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT", "XRP/USDT",
        ])
        self._sym_filter.setFixedWidth(110)
        h.addWidget(self._sym_filter)

        # Strategy filter
        h.addWidget(QLabel("Strategy:"))
        self._strat_filter = QComboBox()
        self._strat_filter.addItems([
            "All", "trend", "mean_reversion", "momentum_breakout",
            "liquidity_sweep", "funding_rate", "order_book", "sentiment",
        ])
        self._strat_filter.setFixedWidth(140)
        h.addWidget(self._strat_filter)

        # Direction filter
        h.addWidget(QLabel("Direction:"))
        self._dir_filter = QComboBox()
        self._dir_filter.addItems(["All", "long", "short"])
        self._dir_filter.setFixedWidth(80)
        h.addWidget(self._dir_filter)

        # Days back
        h.addWidget(QLabel("Last:"))
        self._days_filter = QComboBox()
        self._days_filter.addItems(["7 days", "14 days", "30 days", "60 days", "90 days"])
        self._days_filter.setCurrentIndex(2)
        self._days_filter.setFixedWidth(80)
        h.addWidget(self._days_filter)

        h.addStretch()

        # Status label
        self._status_lbl = QLabel("")
        self._status_lbl.setStyleSheet(f"color:{_GRAY}; font-size:13px;")
        h.addWidget(self._status_lbl)

        # Load button
        load_btn = QPushButton("⊡  Load Signals")
        load_btn.setFixedWidth(130)
        load_btn.clicked.connect(self._load_signals)
        h.addWidget(load_btn)

        v.addLayout(h)

        # Pagination row
        h2 = QHBoxLayout()
        h2.setSpacing(8)

        # Previous button
        self._prev_btn = QPushButton("← Previous")
        self._prev_btn.setFixedWidth(100)
        self._prev_btn.clicked.connect(self._prev_page)
        h2.addWidget(self._prev_btn)

        # Page indicator
        self._page_indicator = QLabel("Page 1 of 1 (0 total)")
        self._page_indicator.setStyleSheet(f"color:{_GRAY}; font-size:13px;")
        self._page_indicator.setMinimumWidth(200)
        h2.addWidget(self._page_indicator)

        # Next button
        self._next_btn = QPushButton("Next →")
        self._next_btn.setFixedWidth(100)
        self._next_btn.clicked.connect(self._next_page)
        h2.addWidget(self._next_btn)

        h2.addStretch()
        v.addLayout(h2)

        return bar

    def _build_table(self) -> QGroupBox:
        box = QGroupBox("Signal History")
        box.setStyleSheet(
            "QGroupBox { color:#C8D0E0; font-weight:700; font-size:13px;"
            " border:1px solid #1E3A5F; border-radius:6px; margin-top:8px; padding-top:12px; }"
        )
        v = QVBoxLayout(box)

        self._table = QTableWidget(0, 7)
        self._table.setHorizontalHeaderLabels([
            "Time", "Symbol", "Strategy", "Direction", "Strength", "Entry", "Regime"
        ])
        self._table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self._table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._table.setSelectionBehavior(QTableWidget.SelectRows)
        self._table.setAlternatingRowColors(True)
        self._table.setStyleSheet(
            "QTableWidget { gridline-color:#1E2A3A; }"
            "QHeaderView::section { background:#0D1B2A; color:#8899AA;"
            " border:none; padding:4px; font-size:13px; }"
        )
        self._table.selectionModel().selectionChanged.connect(self._on_selection_changed)
        v.addWidget(self._table)

        return box

    def _build_detail_panel(self) -> QGroupBox:
        box = QGroupBox("Signal Detail")
        box.setStyleSheet(
            "QGroupBox { color:#C8D0E0; font-weight:700; font-size:13px;"
            " border:1px solid #1E3A5F; border-radius:6px; margin-top:8px; padding-top:12px; }"
        )
        v = QVBoxLayout(box)

        self._detail_text = QTextEdit()
        self._detail_text.setReadOnly(True)
        self._detail_text.setStyleSheet(
            "background:#0A0E1A; color:#C8D0E0; font-family:monospace; font-size:13px;"
            " border:none;"
        )
        self._detail_text.setPlaceholderText("Select a signal row to view details…")
        v.addWidget(self._detail_text)

        return box

    # ── Data loading ──────────────────────────────────────────

    def _load_signals(self) -> None:
        """Load signals from the current page."""
        # Reset to page 0 when filters change
        self._current_page = 0

        days_map = {0: 7, 1: 14, 2: 30, 3: 60, 4: 90}
        days = days_map.get(self._days_filter.currentIndex(), 30)

        strat = self._strat_filter.currentText()
        if strat == "All":
            strat = ""

        direction = self._dir_filter.currentText()
        if direction == "All":
            direction = ""

        filters = {
            "symbol":    "" if self._sym_filter.currentText() == "All" else self._sym_filter.currentText(),
            "strategy":  strat,
            "direction": direction,
            "days":      days,
        }

        self._status_lbl.setText("Loading…")
        self._worker = _LoadWorker(filters, self._current_page, self._page_size, self)
        self._worker.data_ready.connect(self._on_data_ready)
        self._worker.error_occurred.connect(self._on_error)
        self._worker.start()

    def _prev_page(self) -> None:
        """Go to previous page."""
        if self._current_page > 0:
            self._current_page -= 1
            self._load_signals()

    def _next_page(self) -> None:
        """Go to next page."""
        max_page = (self._total_count - 1) // self._page_size
        if self._current_page < max_page:
            self._current_page += 1
            self._load_signals()

    def _update_pagination_controls(self) -> None:
        """Update pagination button states and page indicator."""
        max_page = (self._total_count - 1) // self._page_size if self._total_count > 0 else 0
        current_page_display = self._current_page + 1

        # Update page indicator
        self._page_indicator.setText(
            f"Page {current_page_display} of {max_page + 1} ({self._total_count} total)"
        )

        # Update button states
        self._prev_btn.setEnabled(self._current_page > 0)
        self._next_btn.setEnabled(self._current_page < max_page)

    def _on_data_ready(self, result: dict) -> None:
        """Handle data ready from worker."""
        self._rows = result.get("rows", [])
        self._total_count = result.get("total_count", 0)
        self._populate_table(self._rows)
        self._update_pagination_controls()
        self._status_lbl.setText(f"{len(self._rows)} signals on page")

    def _on_error(self, msg: str) -> None:
        self._status_lbl.setText(f"Error: {msg}")
        logger.warning("SignalExplorer: load failed — %s", msg)

    def _populate_table(self, rows: list[dict]) -> None:
        self._table.setRowCount(len(rows))
        for r, row in enumerate(rows):
            ts = row["timestamp"]
            if "T" in ts:
                ts = ts.split("T")[1][:8]

            direction = row["direction"]
            dir_color = _DIR_COLORS.get(direction, _LIGHT)

            cells = [
                (ts, _GRAY),
                (row["symbol"], _LIGHT),
                (row["strategy"], _BLUE),
                (direction.upper(), dir_color),
                (f"{row['strength']:.4f}", _YELLOW),
                (f"{row['entry_price']:.4f}", _LIGHT),
                (row["regime"].replace("_", " ").title(), _GRAY),
            ]

            for c, (val, color) in enumerate(cells):
                item = QTableWidgetItem(val)
                item.setForeground(QColor(color))
                self._table.setItem(r, c, item)

    def _on_selection_changed(self) -> None:
        rows = self._table.selectionModel().selectedRows()
        if not rows or rows[0].row() >= len(self._rows):
            return
        row = self._rows[rows[0].row()]
        self._show_detail(row)

    def _show_detail(self, row: dict) -> None:
        text = (
            f"{'='*44}\n"
            f"  SIGNAL DETAIL\n"
            f"{'='*44}\n"
            f"  ID:          {row['id']}\n"
            f"  Timestamp:   {row['timestamp']}\n"
            f"  Symbol:      {row['symbol']}\n"
            f"  Strategy:    {row['strategy']}\n"
            f"  Direction:   {row['direction'].upper()}\n"
            f"  Strength:    {row['strength']:.4f}\n"
            f"{'─'*44}\n"
            f"  Entry:       {row['entry_price']}\n"
            f"  Stop Loss:   {row['stop_loss']}\n"
            f"  Take Profit: {row['take_profit']}\n"
            f"{'─'*44}\n"
            f"  Regime:      {row['regime']}\n"
            f"  Timeframe:   {row['timeframe']}\n"
            f"{'─'*44}\n"
            f"  Rationale:\n"
            f"  {row['rationale']}\n"
            f"{'='*44}"
        )
        self._detail_text.setPlainText(text)
