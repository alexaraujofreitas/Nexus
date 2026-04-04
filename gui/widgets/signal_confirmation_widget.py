# ============================================================
# NEXUS TRADER — Signal Confirmation Widget  (Phase 2)
#
# Displays pending trade signals awaiting user approval in live mode.
# Subscribes to SIGNAL_PENDING_CONFIRMATION events from the LiveExecutor
# and provides Approve / Reject buttons per candidate.
#
# Auto-expires stale candidates after a configurable timeout (60s default).
# Only visible when order_router.mode == "live" and pending signals exist.
# ============================================================
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QFrame,
    QPushButton, QScrollArea, QSizePolicy,
)
from PySide6.QtCore import Qt, QTimer, Slot

from core.event_bus import bus, Topics

logger = logging.getLogger(__name__)

_CARD_STYLE = (
    "QFrame#sig_card { background:#0D1320; border:1px solid #1A2332; border-radius:6px; }"
)
_CARD_LIVE = (
    "QFrame#sig_card { background:#1A0A0A; border:1px solid #440011; border-radius:6px; }"
)
_BTN_APPROVE = (
    "QPushButton { background:#0A2A0A; color:#00CC77; border:1px solid #004400; "
    "border-radius:4px; font-size:12px; font-weight:700; padding:4px 12px; }"
    "QPushButton:hover { background:#0A3A0A; border-color:#00CC77; }"
)
_BTN_REJECT = (
    "QPushButton { background:#1A0A0A; color:#FF3355; border:1px solid #440011; "
    "border-radius:4px; font-size:12px; font-weight:700; padding:4px 12px; }"
    "QPushButton:hover { background:#2A1010; border-color:#FF3355; }"
)
_EXPIRE_SECONDS = 60  # Auto-reject after this many seconds


class _SignalCard(QFrame):
    """Single pending signal card with approve/reject buttons."""

    def __init__(self, candidate_data: dict, parent=None):
        super().__init__(parent)
        self.setObjectName("sig_card")
        self.setStyleSheet(_CARD_LIVE)
        self.candidate_id = candidate_data.get("candidate_id", "")
        self._created_at = datetime.now(timezone.utc)

        v = QVBoxLayout(self)
        v.setContentsMargins(12, 8, 12, 8)
        v.setSpacing(4)

        # Row 1: Symbol + Side + Score
        r1 = QHBoxLayout()
        sym = candidate_data.get("symbol", "?")
        side = candidate_data.get("side", "?")
        score = candidate_data.get("score", 0)
        side_color = "#00CC77" if side == "buy" else "#FF3355"

        sym_lbl = QLabel(f"<b>{sym}</b>")
        sym_lbl.setStyleSheet("color:#E8EBF0; font-size:14px;")
        sym_lbl.setTextFormat(Qt.RichText)
        r1.addWidget(sym_lbl)

        side_lbl = QLabel(f"<b style='color:{side_color}'>{side.upper()}</b>")
        side_lbl.setTextFormat(Qt.RichText)
        r1.addWidget(side_lbl)

        r1.addStretch()
        score_lbl = QLabel(f"Score: {score:.2f}")
        score_lbl.setStyleSheet("color:#8899AA; font-size:12px;")
        r1.addWidget(score_lbl)
        v.addLayout(r1)

        # Row 2: Size, SL, TP
        r2 = QHBoxLayout()
        size = candidate_data.get("position_size_usdt", 0)
        sl = candidate_data.get("stop_loss_price", 0)
        tp = candidate_data.get("take_profit_price", 0)

        for label, val in [("Size", f"${size:,.2f}"), ("SL", f"{sl:.4f}"), ("TP", f"{tp:.4f}")]:
            pair = QLabel(f"<span style='color:#667788'>{label}:</span> {val}")
            pair.setTextFormat(Qt.RichText)
            pair.setStyleSheet("color:#B0B8C8; font-size:12px;")
            r2.addWidget(pair)
        r2.addStretch()
        v.addLayout(r2)

        # Row 3: Models + Regime
        models = ", ".join(candidate_data.get("models_fired", [])[:3])
        regime = candidate_data.get("regime", "")
        r3_lbl = QLabel(f"<span style='color:#667788'>Models:</span> {models}  "
                        f"<span style='color:#667788'>Regime:</span> {regime}")
        r3_lbl.setTextFormat(Qt.RichText)
        r3_lbl.setStyleSheet("color:#8899AA; font-size:11px;")
        v.addWidget(r3_lbl)

        # Row 4: Buttons + Timer
        r4 = QHBoxLayout()
        self._timer_lbl = QLabel(f"{_EXPIRE_SECONDS}s")
        self._timer_lbl.setStyleSheet("color:#FF9933; font-size:11px; font-weight:700;")
        r4.addWidget(self._timer_lbl)
        r4.addStretch()

        self._reject_btn = QPushButton("Reject")
        self._reject_btn.setStyleSheet(_BTN_REJECT)
        self._reject_btn.setFixedHeight(26)
        r4.addWidget(self._reject_btn)

        self._approve_btn = QPushButton("Approve")
        self._approve_btn.setStyleSheet(_BTN_APPROVE)
        self._approve_btn.setFixedHeight(26)
        r4.addWidget(self._approve_btn)

        v.addLayout(r4)

    @property
    def age_seconds(self) -> float:
        return (datetime.now(timezone.utc) - self._created_at).total_seconds()

    def update_timer(self):
        remaining = max(0, _EXPIRE_SECONDS - int(self.age_seconds))
        self._timer_lbl.setText(f"{remaining}s")
        if remaining <= 10:
            self._timer_lbl.setStyleSheet("color:#FF3355; font-size:11px; font-weight:700;")


class SignalConfirmationWidget(QWidget):
    """
    Panel showing pending live trade signals awaiting user confirmation.

    Subscribes to SIGNAL_PENDING_CONFIRMATION events from LiveExecutor.
    Auto-rejects expired candidates. Only relevant in live mode.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._cards: dict[str, _SignalCard] = {}
        self._build()
        self._subscribe()

        # Tick timer for countdown updates + expiry
        self._tick_timer = QTimer(self)
        self._tick_timer.setInterval(1000)
        self._tick_timer.timeout.connect(self._on_tick)

    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # Header
        hdr = QHBoxLayout()
        self._title = QLabel("PENDING SIGNALS (0)")
        self._title.setStyleSheet(
            "color:#FF9933; font-size:13px; font-weight:700; padding:4px 8px;"
        )
        hdr.addWidget(self._title)
        hdr.addStretch()

        self._reject_all_btn = QPushButton("Reject All")
        self._reject_all_btn.setStyleSheet(_BTN_REJECT)
        self._reject_all_btn.setFixedHeight(24)
        self._reject_all_btn.clicked.connect(self._on_reject_all)
        self._reject_all_btn.setVisible(False)
        hdr.addWidget(self._reject_all_btn)

        root.addLayout(hdr)

        # Scrollable card container
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet(
            "QScrollArea { background:transparent; border:none; }"
            "QScrollBar:vertical { width:6px; }"
        )
        scroll.setMaximumHeight(300)

        self._container = QWidget()
        self._container_layout = QVBoxLayout(self._container)
        self._container_layout.setContentsMargins(4, 4, 4, 4)
        self._container_layout.setSpacing(6)
        self._container_layout.addStretch()

        scroll.setWidget(self._container)
        root.addWidget(scroll)

        # Start hidden
        self.setVisible(False)

    def _subscribe(self):
        bus.subscribe(Topics.SIGNAL_PENDING_CONFIRMATION, self._on_signal_pending)
        bus.subscribe(Topics.MODE_CHANGED, self._on_mode_changed)

    @Slot(object)
    def _on_signal_pending(self, event):
        """New pending signal from LiveExecutor."""
        data = event.data if hasattr(event, "data") else event
        if isinstance(data, dict):
            cid = data.get("candidate_id", "")
            if cid and cid not in self._cards:
                self._add_card(data)

    @Slot(object)
    def _on_mode_changed(self, event):
        """Hide widget when switching to paper mode."""
        try:
            from core.execution.order_router import order_router
            if order_router.mode != "live":
                self._clear_all()
                self.setVisible(False)
        except Exception:
            pass

    def _add_card(self, candidate_data: dict):
        cid = candidate_data.get("candidate_id", "")
        card = _SignalCard(candidate_data, self)
        card._approve_btn.clicked.connect(lambda: self._on_approve(cid))
        card._reject_btn.clicked.connect(lambda: self._on_reject(cid))
        self._cards[cid] = card

        # Insert before the stretch
        count = self._container_layout.count()
        self._container_layout.insertWidget(count - 1, card)

        self._update_header()
        self.setVisible(True)
        if not self._tick_timer.isActive():
            self._tick_timer.start()

    def _remove_card(self, cid: str):
        card = self._cards.pop(cid, None)
        if card:
            self._container_layout.removeWidget(card)
            card.deleteLater()
        self._update_header()
        if not self._cards:
            self.setVisible(False)
            self._tick_timer.stop()

    def _update_header(self):
        n = len(self._cards)
        self._title.setText(f"PENDING SIGNALS ({n})")
        self._reject_all_btn.setVisible(n > 1)

    def _on_approve(self, cid: str):
        try:
            from core.execution.order_router import order_router
            executor = order_router.active_executor
            if hasattr(executor, "confirm_and_execute"):
                executor.confirm_and_execute(cid)
                logger.info("SignalConfirmation: user approved %s", cid)
        except Exception as exc:
            logger.error("SignalConfirmation: approve error: %s", exc)
        self._remove_card(cid)

    def _on_reject(self, cid: str):
        try:
            from core.execution.order_router import order_router
            executor = order_router.active_executor
            if hasattr(executor, "reject_pending"):
                executor.reject_pending(cid)
                logger.info("SignalConfirmation: user rejected %s", cid)
        except Exception as exc:
            logger.error("SignalConfirmation: reject error: %s", exc)
        self._remove_card(cid)

    def _on_reject_all(self):
        for cid in list(self._cards.keys()):
            self._on_reject(cid)

    @Slot()
    def _on_tick(self):
        """Update countdown timers and expire old candidates."""
        expired = []
        for cid, card in self._cards.items():
            card.update_timer()
            if card.age_seconds >= _EXPIRE_SECONDS:
                expired.append(cid)
        for cid in expired:
            logger.info("SignalConfirmation: auto-expired %s (>%ds)", cid, _EXPIRE_SECONDS)
            self._on_reject(cid)

    def _clear_all(self):
        for cid in list(self._cards.keys()):
            self._remove_card(cid)
