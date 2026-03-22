# ============================================================
# NEXUS TRADER — System Health Monitor  (Phase 12)
#
# Real-time monitoring of all critical system components:
#   - Exchange connection status + latency
#   - Intelligence agent health (running/stale/errors)
#   - Database connectivity and record counts
#   - Market data feed status
#   - Notification channel status
#   - Memory/CPU usage overview
#   - Recent system events log
# ============================================================
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QFrame,
    QScrollArea, QGroupBox, QTableWidget, QTableWidgetItem,
    QHeaderView, QPushButton, QGridLayout,
)
from PySide6.QtCore import Qt, QTimer, Signal, Slot
from PySide6.QtGui import QColor, QFont

from gui.main_window import PageHeader
from core.event_bus import bus, Topics, Event

logger = logging.getLogger(__name__)

_GREEN  = "#00FF88"
_RED    = "#FF3355"
_ORANGE = "#FF6B00"
_YELLOW = "#F6AD55"
_BLUE   = "#4299E1"
_GRAY   = "#8899AA"   # was #4A5568 — too dark to read on dark background
_LIGHT  = "#E2E8F0"   # was #C8D0E0 — slightly brighter white


def _status_color(ok: bool, warn: bool = False) -> str:
    if ok:
        return _GREEN
    if warn:
        return _YELLOW
    return _RED


class _CrashStatusPanel(QFrame):
    """Compact crash detection status panel for SystemHealth page."""

    # Signals for safe cross-thread dispatch
    _sig_refresh  = Signal(object)   # crash score data dict
    _sig_defense  = Signal(str)      # tier string

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("CrashStatusPanel")
        self.setStyleSheet(
            "QFrame#CrashStatusPanel { background: #0D1B2A; border: 1px solid #1E3A5F; border-radius: 8px; }"
        )
        self.setFixedHeight(90)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 10)
        layout.setSpacing(20)

        # Score section
        score_v = QVBoxLayout()
        score_title = QLabel("CRASH RISK SCORE")
        score_title.setStyleSheet("color: #8899AA; font-size: 13px; font-weight: 700; letter-spacing: 1px;")
        self._score_lbl = QLabel("0.0 / 10")
        self._score_lbl.setStyleSheet("color: #00FF88; font-size: 18px; font-weight: 900;")
        score_v.addWidget(score_title)
        score_v.addWidget(self._score_lbl)
        layout.addLayout(score_v)

        # Tier section
        tier_v = QVBoxLayout()
        tier_title = QLabel("TIER")
        tier_title.setStyleSheet("color: #8899AA; font-size: 13px; font-weight: 700; letter-spacing: 1px;")
        self._tier_lbl = QLabel("✅ NORMAL")
        self._tier_lbl.setStyleSheet("color: #00FF88; font-size: 14px; font-weight: 700;")
        tier_v.addWidget(tier_title)
        tier_v.addWidget(self._tier_lbl)
        layout.addLayout(tier_v)

        # Defense mode indicator
        def_v = QVBoxLayout()
        def_title = QLabel("DEFENSE MODE")
        def_title.setStyleSheet("color: #8899AA; font-size: 13px; font-weight: 700; letter-spacing: 1px;")
        self._defense_lbl = QLabel("INACTIVE")
        self._defense_lbl.setStyleSheet("color: #00FF88; font-size: 13px; font-weight: 700;")
        def_v.addWidget(def_title)
        def_v.addWidget(self._defense_lbl)
        layout.addLayout(def_v)
        layout.addStretch()

        # Wire signals → slots (always dispatched on main thread)
        self._sig_refresh.connect(self._refresh)
        self._sig_defense.connect(self._set_defense_active)

        # Subscribe
        bus.subscribe(Topics.CRASH_SCORE_UPDATED, self._on_crash_event)
        bus.subscribe(Topics.CRASH_TIER_CHANGED,  self._on_tier_changed)
        bus.subscribe(Topics.DEFENSIVE_MODE_ACTIVATED, self._on_defense_activated)

    def _on_crash_event(self, event) -> None:
        try:
            data = event.data if hasattr(event, "data") else {}
            self._sig_refresh.emit(data)
        except Exception:
            pass

    def _on_tier_changed(self, event) -> None:
        self._on_crash_event(event)

    def _on_defense_activated(self, event) -> None:
        try:
            data = event.data if hasattr(event, "data") else {}
            tier = data.get("tier", "DEFENSIVE")
            self._sig_defense.emit(tier)
        except Exception:
            pass

    @Slot(object)
    def _refresh(self, data: dict) -> None:
        score = data.get("crash_score", 0.0)
        tier  = data.get("tier", "NORMAL")
        _COLORS = {
            "NORMAL": "#00FF88", "DEFENSIVE": "#F6AD55",
            "HIGH_ALERT": "#FF6B00", "EMERGENCY": "#FF3355", "SYSTEMIC": "#FF0040"
        }
        color = _COLORS.get(tier, "#00FF88")
        self._score_lbl.setText(f"{score:.1f} / 10")
        self._score_lbl.setStyleSheet(f"color: {color}; font-size: 18px; font-weight: 900;")
        _LABELS = {
            "NORMAL": "✅ NORMAL", "DEFENSIVE": "⚠️ DEFENSIVE",
            "HIGH_ALERT": "🔴 HIGH ALERT", "EMERGENCY": "🚨 EMERGENCY",
            "SYSTEMIC": "‼️ SYSTEMIC"
        }
        self._tier_lbl.setText(_LABELS.get(tier, tier))
        self._tier_lbl.setStyleSheet(f"color: {color}; font-size: 14px; font-weight: 700;")
        if tier == "NORMAL":
            self._defense_lbl.setText("INACTIVE")
            self._defense_lbl.setStyleSheet("color: #00FF88; font-size: 13px; font-weight: 700;")

    @Slot(object)
    def _set_defense_active(self, tier: str) -> None:
        _COLORS = {
            "DEFENSIVE": "#F6AD55", "HIGH_ALERT": "#FF6B00",
            "EMERGENCY": "#FF3355", "SYSTEMIC": "#FF0040"
        }
        color = _COLORS.get(tier, "#FF6B00")
        self._defense_lbl.setText(f"ACTIVE — {tier}")
        self._defense_lbl.setStyleSheet(f"color: {color}; font-size: 13px; font-weight: 700;")


class _StatusCard(QFrame):
    """A compact status indicator card."""

    def __init__(self, title: str, parent=None):
        super().__init__(parent)
        self.setObjectName("StatusCard")
        self.setFrameShape(QFrame.StyledPanel)
        self.setFixedHeight(80)
        self.setMinimumWidth(180)
        self.setStyleSheet(
            "QFrame#StatusCard { background:#0D1B2A; border:1px solid #1E3A5F; border-radius:8px; }"
        )

        v = QVBoxLayout(self)
        v.setContentsMargins(12, 8, 12, 8)
        v.setSpacing(4)

        h = QHBoxLayout()
        self._dot = QLabel("●")
        self._dot.setStyleSheet(f"color:{_GRAY}; font-size:13px;")
        h.addWidget(self._dot)
        title_lbl = QLabel(title)
        title_lbl.setStyleSheet(f"color:{_GRAY}; font-size:13px; font-weight:700; letter-spacing:1px;")
        h.addWidget(title_lbl)
        h.addStretch()
        v.addLayout(h)

        self._value_lbl = QLabel("—")
        self._value_lbl.setStyleSheet(f"color:{_LIGHT}; font-size:13px; font-weight:700;")
        v.addWidget(self._value_lbl)

        self._sub_lbl = QLabel("")
        self._sub_lbl.setStyleSheet(f"color:{_GRAY}; font-size:13px;")
        v.addWidget(self._sub_lbl)

    def set_ok(self, value: str, sub: str = "", ok: bool = True, warn: bool = False):
        color = _status_color(ok, warn)
        self._dot.setStyleSheet(f"color:{color}; font-size:13px;")
        self._value_lbl.setText(value)
        self._value_lbl.setStyleSheet(f"color:{color}; font-size:13px; font-weight:700;")
        self._sub_lbl.setText(sub)

    def set_unknown(self):
        self._dot.setStyleSheet(f"color:{_GRAY}; font-size:13px;")
        self._value_lbl.setText("—")
        self._value_lbl.setStyleSheet(f"color:{_GRAY}; font-size:13px;")
        self._sub_lbl.setText("")


class SystemHealthPage(QWidget):
    """
    System health monitoring dashboard.
    """

    # Thread-safe signals for event-bus callbacks
    _sig_refresh_exchange      = Signal()
    _sig_refresh_notifications = Signal()
    _sig_refresh_agent_row     = Signal(str, object)   # agent_source, data dict

    def __init__(self, parent=None):
        super().__init__(parent)
        self._event_log: list[dict] = []
        self._start_time = time.time()
        self._scanner_warn_last: float = 0.0  # debounce for "Scanner not running" log
        self._build()

        # Periodic refresh timer — updates ALL sections every 5 seconds
        self._timer = QTimer(self)
        self._timer.setInterval(5000)
        self._timer.timeout.connect(self._refresh_all)
        self._timer.start()

        # Wire thread-safe signals to UI slots
        self._sig_refresh_exchange.connect(self._refresh_exchange)
        self._sig_refresh_notifications.connect(self._refresh_notifications)
        self._sig_refresh_agent_row.connect(self._refresh_agent_row)

        # Listen for system events
        bus.subscribe(Topics.EXCHANGE_CONNECTED,    self._on_exchange_connected)
        bus.subscribe(Topics.EXCHANGE_DISCONNECTED, self._on_exchange_disconnected)
        bus.subscribe(Topics.EXCHANGE_ERROR,        self._on_exchange_error)
        bus.subscribe(Topics.SYSTEM_ALERT,          self._on_system_alert)
        bus.subscribe(Topics.AGENT_SIGNAL,          self._on_agent_signal)
        bus.subscribe(Topics.AGENT_ERROR,           self._on_agent_error)
        bus.subscribe(Topics.AGENT_STALE,           self._on_agent_stale)
        bus.subscribe(Topics.FEED_STATUS,           self._on_feed_status)

        # Initial full refresh after a short delay (let components initialize)
        QTimer.singleShot(2000, self._refresh_all)

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        root.addWidget(PageHeader(
            "System Health",
            "Real-time monitoring — exchange, agents, database, data feeds, notifications"
        ))

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        scroll.setStyleSheet(
            "QScrollArea { background: transparent; border: none; }"
            "QScrollBar:vertical { background:#0A0E1A; width:8px; border-radius:4px; }"
            "QScrollBar::handle:vertical { background:#2A3A52; border-radius:4px; min-height:30px; }"
            "QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height:0; }"
        )
        content = QWidget()
        content.setMinimumHeight(900)
        cv = QVBoxLayout(content)
        cv.setContentsMargins(16, 16, 16, 16)
        cv.setSpacing(16)

        # Crash detection status panel
        self._crash_panel = _CrashStatusPanel()
        cv.addWidget(self._crash_panel)

        # Quick-look status cards row
        cv.addWidget(self._build_status_cards())

        # Detailed sections
        row2 = QHBoxLayout()
        row2.setSpacing(16)
        row2.addWidget(self._build_exchange_section(), 1)
        row2.addWidget(self._build_agents_section(), 2)
        cv.addLayout(row2)

        row3 = QHBoxLayout()
        row3.setSpacing(16)
        row3.addWidget(self._build_database_section(), 1)
        row3.addWidget(self._build_notifications_section(), 1)
        cv.addLayout(row3)

        # System event log
        cv.addWidget(self._build_event_log())

        cv.addStretch()
        scroll.setWidget(content)
        root.addWidget(scroll, 1)

    def _build_status_cards(self) -> QWidget:
        widget = QWidget()
        h = QHBoxLayout(widget)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(12)

        self._card_exchange  = _StatusCard("EXCHANGE")
        self._card_agents    = _StatusCard("AGENTS")
        self._card_database  = _StatusCard("DATABASE")
        self._card_feeds     = _StatusCard("DATA FEEDS")
        self._card_notif     = _StatusCard("NOTIFICATIONS")
        self._card_uptime    = _StatusCard("UPTIME")

        for card in [
            self._card_exchange, self._card_agents, self._card_database,
            self._card_feeds, self._card_notif, self._card_uptime
        ]:
            h.addWidget(card, 1)

        return widget

    def _build_exchange_section(self) -> QGroupBox:
        box = QGroupBox("Exchange Connection")
        box.setStyleSheet(self._box_style())
        grid = QGridLayout(box)
        grid.setSpacing(8)

        def _row(label, attr, row):
            lbl = QLabel(label)
            lbl.setStyleSheet(f"color:{_GRAY}; font-size:13px;")
            grid.addWidget(lbl, row, 0)
            val = QLabel("—")
            val.setStyleSheet(f"color:{_LIGHT}; font-size:13px;")
            setattr(self, attr, val)
            grid.addWidget(val, row, 1)

        _row("Status:", "_ex_status_lbl", 0)
        _row("Exchange:", "_ex_name_lbl", 1)
        _row("Mode:", "_ex_mode_lbl", 2)
        _row("Last Tick:", "_ex_tick_lbl", 3)
        _row("Latency:", "_ex_lat_lbl", 4)

        return box

    def _build_agents_section(self) -> QGroupBox:
        box = QGroupBox("Intelligence Agents")
        box.setStyleSheet(self._box_style())
        v = QVBoxLayout(box)

        self._agents_table = QTableWidget(0, 5)
        self._agents_table.setHorizontalHeaderLabels([
            "Agent", "Status", "Signal", "Confidence", "Errors"
        ])
        self._agents_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self._agents_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._agents_table.setMaximumHeight(220)
        self._agents_table.setAlternatingRowColors(True)
        self._agents_table.setStyleSheet(
            "QTableWidget { gridline-color:#1E2A3A; color:#E2E8F0; font-size:13px; }"
            "QHeaderView::section { background:#0D1B2A; color:#C8D0E0; "
            "border:none; padding:5px 4px; font-size:13px; font-weight:600; }"
        )
        v.addWidget(self._agents_table)

        return box

    def _build_database_section(self) -> QGroupBox:
        box = QGroupBox("Database")
        box.setStyleSheet(self._box_style())
        grid = QGridLayout(box)
        grid.setSpacing(8)

        def _row(label, attr, row):
            lbl = QLabel(label)
            lbl.setStyleSheet(f"color:{_GRAY}; font-size:13px;")
            grid.addWidget(lbl, row, 0)
            val = QLabel("—")
            val.setStyleSheet(f"color:{_LIGHT}; font-size:13px;")
            setattr(self, attr, val)
            grid.addWidget(val, row, 1)

        _row("Status:", "_db_status_lbl", 0)
        _row("Signals (30d):", "_db_signals_lbl", 1)
        _row("Trades:", "_db_trades_lbl", 2)
        _row("Agent Signals:", "_db_agent_lbl", 3)

        return box

    def _build_notifications_section(self) -> QGroupBox:
        box = QGroupBox("Notification Channels")
        box.setStyleSheet(self._box_style())
        v = QVBoxLayout(box)

        self._notif_table = QTableWidget(0, 3)
        self._notif_table.setHorizontalHeaderLabels(["Channel", "Status", "Last Sent"])
        self._notif_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self._notif_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._notif_table.setMaximumHeight(160)
        self._notif_table.setAlternatingRowColors(True)
        self._notif_table.setStyleSheet(
            "QTableWidget { gridline-color:#1E2A3A; color:#E2E8F0; font-size:13px; }"
            "QHeaderView::section { background:#0D1B2A; color:#C8D0E0; "
            "border:none; padding:5px 4px; font-size:13px; font-weight:600; }"
        )
        v.addWidget(self._notif_table)

        return box

    def _build_event_log(self) -> QGroupBox:
        box = QGroupBox("Recent System Events")
        box.setStyleSheet(self._box_style())
        v = QVBoxLayout(box)

        h = QHBoxLayout()
        h.addStretch()
        clear_btn = QPushButton("Clear")
        clear_btn.setFixedWidth(80)
        clear_btn.clicked.connect(self._clear_event_log)
        h.addWidget(clear_btn)
        refresh_btn = QPushButton("↻ Refresh")
        refresh_btn.setFixedWidth(90)
        refresh_btn.clicked.connect(self._refresh_uptime)
        h.addWidget(refresh_btn)
        v.addLayout(h)

        self._event_table = QTableWidget(0, 4)
        self._event_table.setHorizontalHeaderLabels(["Time", "Level", "Component", "Message"])
        self._event_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self._event_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._event_table.setMaximumHeight(200)
        self._event_table.setAlternatingRowColors(True)
        self._event_table.setStyleSheet(
            "QTableWidget { gridline-color:#1E2A3A; color:#E2E8F0; font-size:13px; }"
            "QHeaderView::section { background:#0D1B2A; color:#C8D0E0; "
            "border:none; padding:5px 4px; font-size:13px; font-weight:600; }"
        )
        v.addWidget(self._event_table)

        return box

    # ── Refresh ───────────────────────────────────────────────

    def _refresh_exchange(self) -> None:
        try:
            from core.market_data.exchange_manager import exchange_manager
            connected = exchange_manager.is_connected

            # Retrieve exchange name from the underlying ccxt exchange object
            ex = exchange_manager.get_exchange()
            name = ex.id.upper() if ex and hasattr(ex, "id") else "—"
            mode = "Testnet/Paper" if (ex and getattr(ex, "urls", {}).get("test")) else "Live/Mainnet"

            # Mode: read from exchange_manager instead of guessing via URL presence
            try:
                mode = exchange_manager.mode  # "Live", "Demo Trading", "Testnet", "Unknown"
            except Exception:
                pass

            status_text = "Connected" if connected else "Disconnected"
            self._ex_status_lbl.setText(status_text)
            self._ex_status_lbl.setStyleSheet(
                f"color:{_GREEN}; font-size:13px; font-weight:700;" if connected
                else f"color:{_RED}; font-size:13px; font-weight:700;"
            )
            self._ex_name_lbl.setText(name)
            self._ex_mode_lbl.setText(mode)

            # Last tick: actual time of last successful fetch_tickers() call
            try:
                lfa = exchange_manager.last_fetch_at
                if lfa > 0 and connected:
                    tick_str = datetime.fromtimestamp(lfa, tz=timezone.utc).strftime("%m-%d %H:%M:%S UTC")
                    self._ex_tick_lbl.setText(tick_str)
                else:
                    self._ex_tick_lbl.setText("No data yet" if connected else "—")
            except Exception:
                pass

            # Latency: measured round-trip from last fetch_tickers() call
            try:
                lat_ms = exchange_manager.last_latency_ms
                if lat_ms > 0 and connected:
                    self._ex_lat_lbl.setText(f"{lat_ms}ms (REST)")
                elif connected:
                    self._ex_lat_lbl.setText("Measuring…")
                else:
                    self._ex_lat_lbl.setText("—")
            except Exception:
                pass

            self._card_exchange.set_ok(status_text, name, ok=connected)
        except Exception:
            self._card_exchange.set_unknown()

    def _refresh_agents(self) -> None:
        """Rebuild the entire agents table from coordinator status."""
        try:
            from core.agents.agent_coordinator import get_coordinator
            coord = get_coordinator()
            if not coord.is_running:
                self._card_agents.set_ok("Stopped", "", ok=False)
                self._agents_table.setRowCount(0)
                return

            status = coord.get_status()
            running_count = sum(1 for s in status.values() if s.get("running"))
            total_count   = len(status)
            error_count   = sum(s.get("errors", 0) for s in status.values())

            ok = running_count == total_count and error_count == 0
            warn = running_count > 0 and (running_count < total_count or error_count > 0)
            self._card_agents.set_ok(
                f"{running_count}/{total_count} Running",
                f"{error_count} errors" if error_count else "All healthy",
                ok=ok, warn=warn,
            )

            # Populate agent table
            self._agents_table.setRowCount(len(status))
            for r, (name, s) in enumerate(status.items()):
                running   = s.get("running", False)
                stale     = s.get("stale", True)
                sig       = float(s.get("signal", 0.0))
                conf      = float(s.get("confidence", 0.0))
                errors    = s.get("errors", 0)

                if not running:
                    status_str, st_color = "Stopped", _GRAY
                elif stale:
                    status_str, st_color = "Stale", _YELLOW
                else:
                    status_str, st_color = "Live", _GREEN

                sig_str = f"{sig:+.3f}" if conf > 0 else "—"
                sig_color = _GREEN if sig > 0.05 else _RED if sig < -0.05 else _GRAY
                err_str = str(errors) if errors else "—"

                cells = [
                    (name.replace("_", " ").title(), _LIGHT),
                    (status_str, st_color),
                    (sig_str, sig_color),
                    (f"{conf:.2f}" if conf > 0 else "—", _LIGHT),
                    (err_str, _RED if errors > 0 else _GRAY),
                ]
                for c, (val, color) in enumerate(cells):
                    item = QTableWidgetItem(val)
                    item.setForeground(QColor(color))
                    self._agents_table.setItem(r, c, item)

        except Exception:
            self._card_agents.set_unknown()

    def _refresh_agent_row(self, agent_source: str, signal_data: dict) -> None:
        """Update a specific agent row instead of rebuilding entire table."""
        try:
            from core.agents.agent_coordinator import get_coordinator
            coord = get_coordinator()
            status = coord.get_status()

            # Find which row corresponds to this agent
            for r in range(self._agents_table.rowCount()):
                cell_text = self._agents_table.item(r, 0).text() if self._agents_table.item(r, 0) else ""
                agent_name = agent_source.replace("_", " ").title()
                if agent_name in cell_text or agent_source in cell_text.lower().replace(" ", "_"):
                    # Found matching row, update it
                    if agent_source in status:
                        s = status[agent_source]
                        running   = s.get("running", False)
                        stale     = s.get("stale", True)
                        sig       = float(s.get("signal", 0.0))
                        conf      = float(s.get("confidence", 0.0))
                        errors    = s.get("errors", 0)

                        if not running:
                            status_str, st_color = "Stopped", _GRAY
                        elif stale:
                            status_str, st_color = "Stale", _YELLOW
                        else:
                            status_str, st_color = "Live", _GREEN

                        sig_str = f"{sig:+.3f}" if conf > 0 else "—"
                        sig_color = _GREEN if sig > 0.05 else _RED if sig < -0.05 else _GRAY
                        err_str = str(errors) if errors else "—"

                        cells = [
                            (agent_name, _LIGHT),
                            (status_str, st_color),
                            (sig_str, sig_color),
                            (f"{conf:.2f}" if conf > 0 else "—", _LIGHT),
                            (err_str, _RED if errors > 0 else _GRAY),
                        ]
                        for c, (val, color) in enumerate(cells):
                            item = QTableWidgetItem(val)
                            item.setForeground(QColor(color))
                            self._agents_table.setItem(r, c, item)
                    return

            # If row not found, rebuild entire table
            self._refresh_agents()
        except Exception:
            pass

    def _refresh_database(self) -> None:
        try:
            from core.database.engine import get_session
            with get_session() as session:
                # Quick connectivity test
                session.execute(__import__("sqlalchemy").text("SELECT 1"))
            self._db_status_lbl.setText("Connected")
            self._db_status_lbl.setStyleSheet(f"color:{_GREEN}; font-size:13px; font-weight:700;")
            self._card_database.set_ok("Connected", "", ok=True)

            # Counts (best-effort)
            try:
                from core.database.engine import get_session
                from core.database.models import SignalLog, Trade
                from datetime import timedelta
                from sqlalchemy import func
                cutoff = datetime.now(timezone.utc) - timedelta(days=30)

                with get_session() as session:
                    sig_count   = session.query(func.count(SignalLog.id)).scalar() or 0
                    trade_count = session.query(func.count(Trade.id)).scalar() or 0

                self._db_signals_lbl.setText(str(sig_count))
                self._db_trades_lbl.setText(str(trade_count))
            except Exception:
                pass

            # Agent signals
            try:
                from core.database.models import AgentSignal
                from sqlalchemy import func
                with get_session() as session:
                    agent_count = session.query(func.count(AgentSignal.id)).scalar() or 0
                self._db_agent_lbl.setText(str(agent_count))
            except Exception:
                self._db_agent_lbl.setText("—")

        except Exception:
            self._db_status_lbl.setText("Error")
            self._db_status_lbl.setStyleSheet(f"color:{_RED}; font-size:13px; font-weight:700;")
            self._card_database.set_ok("Error", "", ok=False)

    def _refresh_notifications(self) -> None:
        """Refresh notification channels status."""
        try:
            from core.notifications.notification_manager import notification_manager
            history_dict = notification_manager.get_history(limit=20)
            channel_count = notification_manager.get_channel_count()
            # get_history() returns {"notifications": [...], "stats": {...}}
            notifications = history_dict.get("notifications", [])

            self._card_notif.set_ok(
                f"{channel_count} Channel{'s' if channel_count != 1 else ''}",
                f"{len(notifications)} recent",
                ok=channel_count > 0,
                warn=channel_count == 0,
            )

            # Build channel status table from notification history
            channels_seen: dict[str, str] = {}
            for h in notifications:
                for ch in h.get("channels", []):
                    if ch not in channels_seen:
                        channels_seen[ch] = h.get("sent_at", "")

            self._notif_table.setRowCount(len(channels_seen) if channels_seen else 1)
            if channels_seen:
                for r, (ch, last_sent) in enumerate(channels_seen.items()):
                    ts = last_sent.split("T")[1][:8] if "T" in last_sent else last_sent[:16]
                    cells = [(ch.title(), _LIGHT), ("Active", _GREEN), (ts, _GRAY)]
                    for c, (val, color) in enumerate(cells):
                        item = QTableWidgetItem(val)
                        item.setForeground(QColor(color))
                        self._notif_table.setItem(r, c, item)
            else:
                # Span all 3 columns so the message is visible in the table
                self._notif_table.setSpan(0, 0, 1, 3)
                item = QTableWidgetItem("No notifications sent yet")
                item.setForeground(QColor(_GRAY))
                self._notif_table.setItem(0, 0, item)

        except Exception:
            self._card_notif.set_unknown()

    def _refresh_uptime(self) -> None:
        elapsed = int(time.time() - self._start_time)
        h = elapsed // 3600
        m = (elapsed % 3600) // 60
        s = elapsed % 60
        uptime = f"{h:02d}:{m:02d}:{s:02d}"
        self._card_uptime.set_ok(uptime, "since app start", ok=True)

    def _refresh_feeds(self) -> None:
        """Refresh data feeds status card."""
        try:
            from core.market_data.data_feed import data_feed
            if data_feed and data_feed.is_active:
                n_symbols = len(data_feed.symbols) if hasattr(data_feed, 'symbols') else 0
                self._card_feeds.set_ok("Active", f"{n_symbols} symbols", ok=True)
            else:
                self._card_feeds.set_ok("Inactive", "", ok=False)
        except Exception:
            try:
                from core.market_data.exchange_manager import exchange_manager
                if exchange_manager.is_connected:
                    self._card_feeds.set_ok("Active", "REST polling", ok=True)
                else:
                    self._card_feeds.set_ok("Inactive", "", ok=False)
            except Exception:
                self._card_feeds.set_unknown()

    def _refresh_scanner_health(self) -> None:
        """Check scanner, feed, signal freshness, and circuit breaker — log issues."""
        import time as _time
        try:
            from core.scanning.scanner import scanner as _sc
            if not getattr(_sc, "_running", False):
                # Debounce: only log once per 5 minutes and not within 30s of startup
                _now = _time.time()
                _grace = _now - self._start_time < 30
                _cooldown = _now - self._scanner_warn_last < 300
                if not _grace and not _cooldown:
                    self._log_event("WARN", "SCANNER", "Scanner not running — check IDSS page")
                    self._scanner_warn_last = _now
                return

            last_scan = getattr(_sc, "_last_scan_completed_at", None)
            if last_scan:
                age_s = _time.time() - last_scan
                tf_s = {"1h": 3600, "4h": 14400, "15m": 900}.get(
                    getattr(_sc, "_timeframe", "1h"), 3600)
                if age_s > tf_s * 1.8:
                    self._log_event("WARN", "SCANNER",
                        f"Stale: last scan {age_s/3600:.1f}h ago (expected <{tf_s*1.2/3600:.1f}h)")
        except Exception:
            pass

        try:
            from core.execution.paper_executor import get_paper_executor
            _pe = get_paper_executor()
            if hasattr(_pe, "get_production_status"):
                ps = _pe.get_production_status()
                if ps.get("circuit_breaker_on", False):
                    self._log_event("WARN", "CIRCUIT_BRK",
                        f"ACTIVE: drawdown {ps['drawdown_pct']:.2f}% >= 10% — no new entries")
                streak = ps.get("current_losing_streak", 0)
                if streak >= 5:
                    self._log_event("WARN", "TRADE_HEALTH",
                        f"Losing streak: {streak} consecutive losses — review signal quality")
        except Exception:
            pass

    def _refresh_all(self) -> None:
        """Refresh all sections — called periodically (every 5s) and on initial load."""
        try:
            self._refresh_exchange()
        except Exception:
            pass
        try:
            self._refresh_agents()
        except Exception:
            pass
        try:
            self._refresh_database()
        except Exception:
            pass
        try:
            self._refresh_feeds()
        except Exception:
            pass
        try:
            self._refresh_notifications()
        except Exception:
            pass
        try:
            self._refresh_uptime()
        except Exception:
            pass
        try:
            self._refresh_scanner_health()
        except Exception:
            pass

    # ── Event log ─────────────────────────────────────────────

    def _log_event(self, level: str, component: str, message: str) -> None:
        now = datetime.now(timezone.utc).strftime("%m-%d %H:%M:%S")
        self._event_log.insert(0, {
            "time": now, "level": level, "component": component, "message": message
        })
        if len(self._event_log) > 100:
            self._event_log.pop()
        self._update_event_table()

    def _update_event_table(self) -> None:
        items = self._event_log[:50]
        self._event_table.setRowCount(len(items))
        level_colors = {"INFO": _BLUE, "WARN": _YELLOW, "ERROR": _RED, "OK": _GREEN}
        for r, item in enumerate(items):
            color = level_colors.get(item["level"], _GRAY)
            cells = [
                (item["time"], _LIGHT),
                (item["level"], color),
                (item["component"], _LIGHT),
                (item["message"], _LIGHT),
            ]
            for c, (val, col) in enumerate(cells):
                cell = QTableWidgetItem(val)
                cell.setForeground(QColor(col))
                self._event_table.setItem(r, c, cell)

    def _clear_event_log(self) -> None:
        self._event_log.clear()
        self._event_table.setRowCount(0)

    # ── Bus handlers ──────────────────────────────────────────

    def _on_exchange_connected(self, event: Event) -> None:
        # May be called from background thread — defer UI updates
        from PySide6.QtCore import QTimer
        data = event.data or {}
        msg = f"Connected — {data.get('exchange_id','')}"
        QTimer.singleShot(0, lambda: self._log_event("OK", "Exchange", msg))
        self._sig_refresh_exchange.emit()

    def _on_exchange_disconnected(self, event: Event) -> None:
        from PySide6.QtCore import QTimer
        QTimer.singleShot(0, lambda: self._log_event("WARN", "Exchange", "Disconnected"))
        self._sig_refresh_exchange.emit()

    def _on_exchange_error(self, event: Event) -> None:
        data = event.data or {}
        self._log_event("ERROR", "Exchange", str(data.get("error", "Unknown error")))
        self._sig_refresh_exchange.emit()

    def _on_system_alert(self, event: Event) -> None:
        data = event.data or {}
        self._log_event("INFO", "System", data.get("message", data.get("title", "")))

    def _on_agent_signal(self, event: Event) -> None:
        """Handle agent signal update — update that specific agent row."""
        data = event.data or {}
        agent_source = data.get("agent_source", "")
        if agent_source:
            self._sig_refresh_agent_row.emit(agent_source, data)

    def _on_agent_stale(self, event: Event) -> None:
        """Handle agent stale status — update that specific agent row."""
        data = event.data or {}
        agent_source = data.get("agent_source", "")
        if agent_source:
            self._sig_refresh_agent_row.emit(agent_source, data)

    def _on_agent_error(self, event: Event) -> None:
        data = event.data or {}
        agent = data.get("agent_name", "agent")
        self._log_event("ERROR", f"Agent:{agent}", str(data.get("error", "")))
        agent_source = data.get("agent_source", agent)
        if agent_source:
            self._sig_refresh_agent_row.emit(agent_source, data)

    def _on_feed_status(self, event: Event) -> None:
        data = event.data or {}
        self._log_event("INFO", "Feed", str(data.get("status", "")))
        self._sig_refresh_notifications.emit()

    @staticmethod
    def _box_style() -> str:
        return (
            "QGroupBox { color:#E2E8F0; font-weight:700; font-size:13px;"
            " border:1px solid #1E3A5F; border-radius:6px;"
            " margin-top:8px; padding-top:12px; }"
        )
