# ============================================================
# NEXUS TRADER — Notifications Page  (Phase 12)
#
# Dedicated page for viewing notification history and managing
# notification preferences. Shows:
#   - Recent notification log (what was sent, when, to which channels)
#   - Channel status (configured, last sent)
#   - Quick preference toggles
#   - Test buttons for each channel
# ============================================================
from __future__ import annotations

import logging
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QFrame,
    QScrollArea, QGroupBox, QTableWidget, QTableWidgetItem,
    QHeaderView, QPushButton, QCheckBox, QGridLayout, QComboBox,
)
from PySide6.QtCore import Qt, QTimer, QMetaObject
from PySide6.QtGui import QColor

from gui.main_window import PageHeader
from core.event_bus import bus, Topics, Event

logger = logging.getLogger(__name__)

_GREEN  = "#00FF88"
_RED    = "#FF3355"
_YELLOW = "#F6AD55"
_BLUE   = "#4299E1"
_GRAY   = "#4A5568"
_LIGHT  = "#C8D0E0"


class NotificationsPage(QWidget):
    """
    Notification history and preferences page.
    """

    _HEALTH_INTERVAL_OPTIONS = (1, 2, 3, 4, 6, 12, 24)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._pref_checkboxes: dict[str, QCheckBox] = {}
        self._health_interval_combo: QComboBox | None = None
        self._build()

        # Try to subscribe to notification events; fallback to 30-second timer if not available
        self._has_notification_events = False
        try:
            if hasattr(Topics, 'NOTIFICATION_SENT'):
                bus.subscribe(Topics.NOTIFICATION_SENT, self._on_notification_sent)
                self._has_notification_events = True
        except Exception:
            pass

        # Fallback timer (30 seconds if events available, otherwise keep as needed)
        self._timer = QTimer(self)
        self._timer.setInterval(30000)  # refresh every 30 seconds
        self._timer.timeout.connect(self._refresh_history)
        self._timer.start()

        # Initial refresh
        self._refresh()

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        root.addWidget(PageHeader(
            "Notifications",
            "Real-time trade and system notifications — history, channels, preferences"
        ))

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        content = QWidget()
        cv = QVBoxLayout(content)
        cv.setContentsMargins(16, 16, 16, 16)
        cv.setSpacing(16)

        # Row 1: channel status cards
        cv.addWidget(self._build_channel_cards())

        # Row 2: preferences + test buttons
        row2 = QHBoxLayout()
        row2.setSpacing(16)
        row2.addWidget(self._build_preferences_section(), 1)
        row2.addWidget(self._build_test_section(), 1)
        cv.addLayout(row2)

        # Notification history table
        cv.addWidget(self._build_history_section())

        cv.addStretch()
        scroll.setWidget(content)
        root.addWidget(scroll, 1)

    def _build_channel_cards(self) -> QWidget:
        """Status cards for each configured channel."""
        widget = QWidget()
        h = QHBoxLayout(widget)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(12)

        channels = [
            ("WhatsApp", "whatsapp", "💬"),
            ("Telegram",  "telegram", "✈️"),
            ("Email",     "email",    "📧"),
            ("SMS",       "sms",      "📱"),
        ]

        self._channel_cards: dict[str, dict] = {}

        for name, key, icon in channels:
            frame = QFrame()
            frame.setObjectName("ChannelCard")
            frame.setFixedHeight(80)
            frame.setStyleSheet(
                "QFrame#ChannelCard { background:#0D1B2A; border:1px solid #1E3A5F;"
                " border-radius:8px; }"
            )
            fv = QVBoxLayout(frame)
            fv.setContentsMargins(12, 8, 12, 8)
            fv.setSpacing(4)

            fh = QHBoxLayout()
            icon_lbl = QLabel(icon)
            icon_lbl.setStyleSheet("font-size:18px;")
            fh.addWidget(icon_lbl)
            name_lbl = QLabel(name)
            name_lbl.setStyleSheet(f"color:{_LIGHT}; font-weight:600; font-size:13px;")
            fh.addWidget(name_lbl)
            fh.addStretch()
            dot = QLabel("●")
            dot.setStyleSheet(f"color:{_GRAY}; font-size:13px;")
            fh.addWidget(dot)
            fv.addLayout(fh)

            status_lbl = QLabel("Not configured")
            status_lbl.setStyleSheet(f"color:{_GRAY}; font-size:13px;")
            fv.addWidget(status_lbl)

            self._channel_cards[key] = {"dot": dot, "status": status_lbl}
            h.addWidget(frame, 1)

        return widget

    def _build_preferences_section(self) -> QGroupBox:
        box = QGroupBox("Notification Preferences")
        box.setStyleSheet(self._box_style())
        grid = QGridLayout(box)
        grid.setSpacing(8)

        pref_items = [
            ("trade_opened",     "Trade Opened",    True,  0, 0),
            ("trade_closed",     "Trade Closed",    True,  0, 1),
            ("trade_stopped",    "Stop-Loss Hit",   True,  1, 0),
            ("trade_rejected",   "Signal Rejected", False, 1, 1),
            ("strategy_signal",  "Strategy Signal", False, 2, 0),
            ("risk_warning",     "Risk Warning",    True,  2, 1),
            ("market_condition", "Market Alert",    False, 3, 0),
            ("system_error",     "System Errors",   True,  3, 1),
            ("emergency_stop",   "Emergency Stop",  True,  4, 0),
            ("daily_summary",    "Daily Summary",   True,  4, 1),
        ]

        for key, label, default, r, c in pref_items:
            cb = QCheckBox(label)
            cb.setChecked(default)
            cb.setStyleSheet(f"color:{_LIGHT}; font-size:13px;")
            cb.stateChanged.connect(
                lambda state, k=key: self._on_pref_changed(k, bool(state))
            )
            self._pref_checkboxes[key] = cb
            grid.addWidget(cb, r, c)

        # ── Health Check row (checkbox + interval combo) ───────
        hc_cb = QCheckBox("Health Check")
        hc_cb.setChecked(True)
        hc_cb.setStyleSheet(f"color:{_LIGHT}; font-size:13px;")
        hc_cb.stateChanged.connect(
            lambda state: self._on_pref_changed("health_check", bool(state))
        )
        self._pref_checkboxes["health_check"] = hc_cb
        grid.addWidget(hc_cb, 5, 0)

        interval_widget = QWidget()
        ih = QHBoxLayout(interval_widget)
        ih.setContentsMargins(0, 0, 0, 0)
        ih.setSpacing(6)
        every_lbl = QLabel("Every")
        every_lbl.setStyleSheet(f"color:{_GRAY}; font-size:13px;")
        ih.addWidget(every_lbl)

        self._health_interval_combo = QComboBox()
        self._health_interval_combo.setFixedWidth(60)
        self._health_interval_combo.setStyleSheet(
            f"color:{_LIGHT}; background:#0D1B2A; border:1px solid #1E3A5F;"
            " border-radius:4px; padding:2px 4px; font-size:13px;"
        )
        for h in self._HEALTH_INTERVAL_OPTIONS:
            self._health_interval_combo.addItem(f"{h}h", userData=h)
        # Default: 6h
        self._health_interval_combo.setCurrentIndex(
            self._HEALTH_INTERVAL_OPTIONS.index(6)
        )
        self._health_interval_combo.currentIndexChanged.connect(
            self._on_health_interval_changed
        )
        ih.addWidget(self._health_interval_combo)
        ih.addStretch()
        grid.addWidget(interval_widget, 5, 1)
        # ── end health check row ───────────────────────────────

        save_btn = QPushButton("Save Preferences")
        save_btn.clicked.connect(self._save_preferences)
        grid.addWidget(save_btn, 6, 0, 1, 2)

        return box

    def _build_test_section(self) -> QGroupBox:
        box = QGroupBox("Test Channels")
        box.setStyleSheet(self._box_style())
        v = QVBoxLayout(box)
        v.setSpacing(10)

        info = QLabel(
            "Send a test notification to each configured channel.\n"
            "Channels must be configured in Settings → Notifications first."
        )
        info.setWordWrap(True)
        info.setStyleSheet(f"color:{_GRAY}; font-size:13px;")
        v.addWidget(info)

        for name, key in [("WhatsApp", "whatsapp"), ("Telegram", "telegram"),
                           ("Email", "email"), ("SMS", "sms")]:
            btn = QPushButton(f"Test {name}")
            btn.clicked.connect(lambda checked=False, k=key: self._test_channel(k))
            v.addWidget(btn)

        v.addWidget(QLabel(""))  # spacer

        test_all_btn = QPushButton("Test All Channels")
        test_all_btn.setStyleSheet(
            "background:#1E3A5F; color:#4299E1; font-weight:700; padding:8px;"
        )
        test_all_btn.clicked.connect(self._test_all)
        v.addWidget(test_all_btn)

        v.addStretch()
        return box

    def _build_history_section(self) -> QGroupBox:
        box = QGroupBox("Notification History")
        box.setStyleSheet(self._box_style())
        v = QVBoxLayout(box)

        # Controls row
        h = QHBoxLayout()
        self._total_lbl = QLabel("0 notifications sent this session")
        self._total_lbl.setStyleSheet(f"color:{_GRAY}; font-size:13px;")
        h.addWidget(self._total_lbl)
        h.addStretch()
        refresh_btn = QPushButton("↻ Refresh")
        refresh_btn.setFixedWidth(90)
        refresh_btn.clicked.connect(self._refresh)
        h.addWidget(refresh_btn)
        v.addLayout(h)

        self._history_table = QTableWidget(0, 5)
        self._history_table.setHorizontalHeaderLabels([
            "Time", "Type", "Key", "Channels", "Status"
        ])
        self._history_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self._history_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._history_table.setAlternatingRowColors(True)
        self._history_table.setMaximumHeight(300)
        self._history_table.setStyleSheet(
            "QTableWidget { gridline-color:#1E2A3A; }"
            "QHeaderView::section { background:#0D1B2A; color:#8899AA;"
            " border:none; padding:4px; font-size:13px; }"
        )
        v.addWidget(self._history_table)

        return box

    # ── Refresh ───────────────────────────────────────────────

    def _refresh(self) -> None:
        self._refresh_channel_cards()
        self._refresh_preferences()
        self._refresh_history()

    def _refresh_channel_cards(self) -> None:
        try:
            from core.notifications.notification_manager import notification_manager
            from config.settings import settings

            channels_config = {
                "whatsapp": settings.get("notifications.whatsapp.enabled", False),
                "telegram": settings.get("notifications.telegram.enabled", False),
                "email":    settings.get("notifications.email.enabled",    False),
                "sms":      settings.get("notifications.sms.enabled",      False),
            }

            history = notification_manager.get_history(limit=100)
            last_sent_by_channel: dict[str, str] = {}
            for h in history.get("notifications", []):
                for ch in h.get("channels", []):
                    if ch not in last_sent_by_channel:
                        ts = h["sent_at"]
                        if "T" in ts:
                            ts = ts.split("T")[1][:8]
                        last_sent_by_channel[ch] = ts

            for key, card in self._channel_cards.items():
                enabled = channels_config.get(key, False)
                last = last_sent_by_channel.get(key, "never")
                if enabled:
                    card["dot"].setStyleSheet(f"color:{_GREEN}; font-size:13px;")
                    card["status"].setText(f"Enabled • Last: {last}")
                    card["status"].setStyleSheet(f"color:{_GREEN}; font-size:13px;")
                else:
                    card["dot"].setStyleSheet(f"color:{_GRAY}; font-size:13px;")
                    card["status"].setText("Disabled")
                    card["status"].setStyleSheet(f"color:{_GRAY}; font-size:13px;")

        except Exception:
            pass

    def _refresh_preferences(self) -> None:
        try:
            from config.settings import settings
            for key, cb in self._pref_checkboxes.items():
                val = settings.get(f"notifications.preferences.{key}", cb.isChecked())
                cb.setChecked(bool(val))

            # Restore health-check interval combo without triggering the save handler
            if self._health_interval_combo is not None:
                saved_h = int(
                    settings.get("notifications.preferences.health_check_interval_hours", 6)
                )
                self._health_interval_combo.blockSignals(True)
                idx = self._HEALTH_INTERVAL_OPTIONS.index(saved_h) \
                    if saved_h in self._HEALTH_INTERVAL_OPTIONS else \
                    self._HEALTH_INTERVAL_OPTIONS.index(6)
                self._health_interval_combo.setCurrentIndex(idx)
                self._health_interval_combo.blockSignals(False)
        except Exception:
            pass

    def _refresh_history(self) -> None:
        """Refresh the entire notification history table."""
        try:
            from core.notifications.notification_manager import notification_manager
            history = notification_manager.get_history(limit=100)
            items = history.get("notifications", [])
            self._total_lbl.setText(f"{len(items)} notification(s) sent this session")

            self._history_table.setRowCount(len(items))
            for r, item in enumerate(items):
                ts = item["sent_at"]
                if "T" in ts:
                    ts = ts.split("T")[1][:8]

                ok = item.get("success", False)
                status_str = "✓ Sent" if ok else "✗ Failed"
                status_color = _GREEN if ok else _RED

                channels_str = ", ".join(item.get("channels", [])) or "—"

                cells = [
                    (ts,                                              _GRAY),
                    (item["template"].replace("_", " ").title(),      _BLUE),
                    (item.get("dedup_key", "—"),                      _LIGHT),
                    (channels_str,                                    _LIGHT),
                    (status_str,                                      status_color),
                ]
                for c, (val, color) in enumerate(cells):
                    cell = QTableWidgetItem(val)
                    cell.setForeground(QColor(color))
                    self._history_table.setItem(r, c, cell)

        except Exception:
            pass

    def _on_notification_sent(self, event: Event) -> None:
        """Handle notification sent event — append to history table directly."""
        data = event.data or {}
        try:
            # Insert at top of table instead of refreshing entire table
            current_rows = self._history_table.rowCount()
            self._history_table.insertRow(0)

            ts = data.get("sent_at", "")
            if "T" in ts:
                ts = ts.split("T")[1][:8]

            ok = data.get("success", False)
            status_str = "✓ Sent" if ok else "✗ Failed"
            status_color = _GREEN if ok else _RED

            channels_str = ", ".join(data.get("channels", [])) or "—"

            cells = [
                (ts,                                        _GRAY),
                (data.get("template", "").replace("_", " ").title(), _BLUE),
                (data.get("dedup_key", "—"),               _LIGHT),
                (channels_str,                             _LIGHT),
                (status_str,                               status_color),
            ]
            for c, (val, color) in enumerate(cells):
                cell = QTableWidgetItem(val)
                cell.setForeground(QColor(color))
                self._history_table.setItem(0, c, cell)

            # Keep table at max 100 rows
            if current_rows >= 100:
                self._history_table.removeRow(100)

            # Update total count
            total = self._history_table.rowCount()
            self._total_lbl.setText(f"{total} notification(s) sent this session")

        except Exception as e:
            logger.debug("Failed to append notification row: %s", e)

    # ── Actions ───────────────────────────────────────────────

    def _on_pref_changed(self, key: str, enabled: bool) -> None:
        try:
            from core.notifications.notification_manager import notification_manager
            notification_manager.set_preference(key, enabled)
        except Exception:
            pass

    def _on_health_interval_changed(self, _index: int) -> None:
        """User picked a new health-check interval — apply immediately and persist."""
        if self._health_interval_combo is None:
            return
        hours = self._health_interval_combo.currentData()
        if hours is None:
            return
        try:
            from core.notifications.notification_manager import notification_manager
            from config.settings import settings
            notification_manager.set_health_check_interval(int(hours))
            settings.set("notifications.preferences.health_check_interval_hours", int(hours))
            settings.save()
        except Exception as exc:
            logger.debug("Failed to apply health check interval: %s", exc)

    def _save_preferences(self) -> None:
        try:
            from config.settings import settings
            for key, cb in self._pref_checkboxes.items():
                settings.set(f"notifications.preferences.{key}", cb.isChecked())
            settings.save()
            logger.info("Notification preferences saved")
        except Exception as exc:
            logger.error("Failed to save preferences: %s", exc)

    def _test_channel(self, channel_key: str) -> None:
        try:
            from core.notifications.notification_manager import notification_manager
            results = notification_manager.test_all_channels()
            ok = results.get(channel_key)
            if ok is None:
                logger.info("Channel '%s' not configured", channel_key)
            elif ok:
                logger.info("Test successful for channel: %s", channel_key)
            else:
                logger.warning("Test failed for channel: %s", channel_key)
            self._refresh_history()
        except Exception as exc:
            logger.error("Test channel failed: %s", exc)

    def _test_all(self) -> None:
        try:
            from core.notifications.notification_manager import notification_manager
            results = notification_manager.test_all_channels()
            logger.info("Test all channels: %s", results)
            self._refresh_history()
        except Exception as exc:
            logger.error("Test all failed: %s", exc)

    @staticmethod
    def _box_style() -> str:
        return (
            "QGroupBox { color:#C8D0E0; font-weight:700; font-size:13px;"
            " border:1px solid #1E3A5F; border-radius:6px;"
            " margin-top:8px; padding-top:12px; }"
        )
