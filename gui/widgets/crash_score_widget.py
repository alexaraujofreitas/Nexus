# ============================================================
# NEXUS TRADER — Crash Score Widget  (Sprint 13)
#
# Real-time crash score gauge displayed in the Dashboard sidebar.
# Shows:
#   - Crash Score (0–10) with color-coded gauge bar
#   - Current severity tier (NORMAL/DEFENSIVE/HIGH_ALERT/EMERGENCY/SYSTEMIC)
#   - 7 category breakdown bars
#   - BTC Dominance + Fear & Greed snapshot
#   - Last updated timestamp
#
# Updates via EventBus subscription to Topics.CRASH_SCORE_UPDATED.
# ============================================================
from __future__ import annotations

import logging
from datetime import datetime, timezone

from PySide6.QtWidgets import (
    QFrame, QVBoxLayout, QHBoxLayout, QLabel, QProgressBar,
    QWidget, QSizePolicy,
)
from PySide6.QtCore import Qt, Signal, Slot
from PySide6.QtGui import QColor, QFont

from core.event_bus import bus, Topics

logger = logging.getLogger(__name__)

# Tier colors
_TIER_COLORS = {
    "NORMAL":     "#00FF88",
    "DEFENSIVE":  "#F6AD55",
    "HIGH_ALERT": "#FF6B00",
    "EMERGENCY":  "#FF3355",
    "SYSTEMIC":   "#FF0040",
}

_TIER_LABELS = {
    "NORMAL":     "✅ NORMAL",
    "DEFENSIVE":  "⚠️ DEFENSIVE",
    "HIGH_ALERT": "🔴 HIGH ALERT",
    "EMERGENCY":  "🚨 EMERGENCY",
    "SYSTEMIC":   "‼️ SYSTEMIC",
}

_CATEGORY_NAMES = {
    "derivatives":   "Derivatives",
    "liquidity":     "Liquidity",
    "whale_onchain": "Whale/On-Chain",
    "stablecoin":    "Stablecoin",
    "technical":     "Technical",
    "sentiment":     "Sentiment",
    "macro":         "Macro",
}


class _MiniBar(QFrame):
    """A small labeled progress bar for category breakdown."""

    def __init__(self, label: str, parent=None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 1, 0, 1)
        layout.setSpacing(6)

        self._lbl = QLabel(label)
        self._lbl.setFixedWidth(90)
        self._lbl.setStyleSheet("color: #8899AA; font-size: 13px;")

        self._bar = QProgressBar()
        self._bar.setRange(0, 100)
        self._bar.setValue(0)
        self._bar.setFixedHeight(6)
        self._bar.setTextVisible(False)
        self._bar.setStyleSheet(
            "QProgressBar { background: #1A2332; border-radius: 3px; }"
            "QProgressBar::chunk { background: #FF6B00; border-radius: 3px; }"
        )

        self._val_lbl = QLabel("0%")
        self._val_lbl.setFixedWidth(28)
        self._val_lbl.setStyleSheet("color: #8899AA; font-size: 13px;")
        self._val_lbl.setAlignment(Qt.AlignRight)

        layout.addWidget(self._lbl)
        layout.addWidget(self._bar, 1)
        layout.addWidget(self._val_lbl)

    def update(self, value: float) -> None:
        """Update bar with value 0.0–1.0."""
        pct = int(min(100, max(0, value * 100)))
        self._bar.setValue(pct)
        self._val_lbl.setText(f"{pct}%")

        # Color based on severity
        if pct >= 70:
            color = "#FF3355"
        elif pct >= 45:
            color = "#FF6B00"
        elif pct >= 20:
            color = "#F6AD55"
        else:
            color = "#00C853"

        self._bar.setStyleSheet(
            "QProgressBar { background: #1A2332; border-radius: 3px; }"
            f"QProgressBar::chunk {{ background: {color}; border-radius: 3px; }}"
        )


class CrashScoreWidget(QFrame):
    """
    Real-time crash risk gauge widget for the Dashboard sidebar.

    Subscribe to CRASH_SCORE_UPDATED events and updates all displays.
    """

    # Signals for safe cross-thread dispatch from event bus callbacks
    _sig_update  = Signal(object)   # crash score data dict
    _sig_flash   = Signal(str)      # border color string

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("CrashScoreWidget")
        self.setFrameShape(QFrame.StyledPanel)
        self.setStyleSheet(
            "QFrame#CrashScoreWidget {"
            "  background: #0D1B2A;"
            "  border: 1px solid #1E3A5F;"
            "  border-radius: 10px;"
            "}"
        )
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._build()
        # Wire signals to slots (always dispatched on main thread)
        self._sig_update.connect(self._update_display)
        self._sig_flash.connect(self._flash_border)
        bus.subscribe(Topics.CRASH_SCORE_UPDATED, self._on_crash_updated)
        bus.subscribe(Topics.CRASH_TIER_CHANGED, self._on_tier_changed)

    def _build(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(8)

        # ── Header row ────────────────────────────────────────
        header = QHBoxLayout()
        title = QLabel("CRASH RISK MONITOR")
        title.setStyleSheet("color: #FF6B00; font-size: 13px; font-weight: 700; letter-spacing: 1px;")
        header.addWidget(title)
        header.addStretch()

        self._updated_lbl = QLabel("—")
        self._updated_lbl.setStyleSheet("color: #4A5568; font-size: 13px;")
        header.addWidget(self._updated_lbl)
        layout.addLayout(header)

        # ── Score display ─────────────────────────────────────
        score_row = QHBoxLayout()

        self._score_lbl = QLabel("0.0")
        self._score_lbl.setStyleSheet(
            "font-size: 36px; font-weight: 900; color: #00FF88;"
        )
        score_row.addWidget(self._score_lbl)

        score_meta = QVBoxLayout()
        max_lbl = QLabel("/ 10")
        max_lbl.setStyleSheet("color: #4A5568; font-size: 16px; font-weight: 700;")
        self._tier_lbl = QLabel("✅ NORMAL")
        self._tier_lbl.setStyleSheet("color: #00FF88; font-size: 13px; font-weight: 700;")
        score_meta.addWidget(max_lbl)
        score_meta.addWidget(self._tier_lbl)
        score_meta.addStretch()
        score_row.addLayout(score_meta)
        score_row.addStretch()
        layout.addLayout(score_row)

        # ── Main gauge bar ────────────────────────────────────
        self._main_bar = QProgressBar()
        self._main_bar.setRange(0, 100)
        self._main_bar.setValue(0)
        self._main_bar.setFixedHeight(12)
        self._main_bar.setTextVisible(False)
        self._main_bar.setStyleSheet(
            "QProgressBar { background: #1A2332; border-radius: 6px; }"
            "QProgressBar::chunk { background: #00FF88; border-radius: 6px; }"
        )
        layout.addWidget(self._main_bar)

        # ── Threshold markers label ───────────────────────────
        thresholds = QLabel("│50%  DEFENSIVE    │70%  HIGH ALERT    │80%  EMERGENCY    │90%  SYSTEMIC")
        thresholds.setStyleSheet("color: #2A3A4A; font-size: 13px;")
        layout.addWidget(thresholds)

        # ── Divider ───────────────────────────────────────────
        div = QFrame()
        div.setFrameShape(QFrame.HLine)
        div.setStyleSheet("color: #1E2D40;")
        layout.addWidget(div)

        # ── Category breakdown ────────────────────────────────
        breakdown_lbl = QLabel("SIGNAL BREAKDOWN")
        breakdown_lbl.setStyleSheet("color: #4A5568; font-size: 13px; font-weight: 700; letter-spacing: 1px;")
        layout.addWidget(breakdown_lbl)

        self._category_bars: dict[str, _MiniBar] = {}
        for key, label in _CATEGORY_NAMES.items():
            bar = _MiniBar(label)
            self._category_bars[key] = bar
            layout.addWidget(bar)

        # ── Macro snapshot row ────────────────────────────────
        div2 = QFrame()
        div2.setFrameShape(QFrame.HLine)
        div2.setStyleSheet("color: #1E2D40;")
        layout.addWidget(div2)

        snapshot_row = QHBoxLayout()
        self._fng_lbl = QLabel("F&G: —")
        self._fng_lbl.setStyleSheet("color: #8899AA; font-size: 13px;")
        self._btcd_lbl = QLabel("BTC.D: —%")
        self._btcd_lbl.setStyleSheet("color: #8899AA; font-size: 13px;")
        snapshot_row.addWidget(self._fng_lbl)
        snapshot_row.addStretch()
        snapshot_row.addWidget(self._btcd_lbl)
        layout.addLayout(snapshot_row)

    # ── Event handlers ────────────────────────────────────────

    def _on_crash_updated(self, event) -> None:
        """Handle CRASH_SCORE_UPDATED event (may come from non-UI thread)."""
        try:
            data = event.data if hasattr(event, "data") else {}
            self._sig_update.emit(data)
        except Exception as exc:
            logger.debug("CrashScoreWidget: emit error — %s", exc)

    def _on_tier_changed(self, event) -> None:
        """Flash the border when tier escalates."""
        try:
            data = event.data if hasattr(event, "data") else {}
            new_tier = data.get("new_tier", "NORMAL")
            color = _TIER_COLORS.get(new_tier, "#00FF88")
            self._sig_flash.emit(color)
        except Exception:
            pass

    @Slot(object)
    def _update_display(self, data: dict) -> None:
        """Update all UI elements from crash score data."""
        score      = data.get("crash_score", 0.0)
        tier       = data.get("tier", "NORMAL")
        components = data.get("components", {})

        # Score display
        color = _TIER_COLORS.get(tier, "#00FF88")
        self._score_lbl.setText(f"{score:.1f}")
        self._score_lbl.setStyleSheet(
            f"font-size: 36px; font-weight: 900; color: {color};"
        )
        self._tier_lbl.setText(_TIER_LABELS.get(tier, tier))
        self._tier_lbl.setStyleSheet(f"color: {color}; font-size: 13px; font-weight: 700;")

        # Gauge bar
        gauge_val = int(score * 10)
        self._main_bar.setValue(gauge_val)
        self._main_bar.setStyleSheet(
            "QProgressBar { background: #1A2332; border-radius: 6px; }"
            f"QProgressBar::chunk {{ background: {color}; border-radius: 6px; }}"
        )

        # Category bars
        for key, bar in self._category_bars.items():
            cat_data = components.get(key, {})
            bar.update(cat_data.get("score", 0.0))

        # Sentiment snapshot
        sentiment_details = components.get("sentiment", {}).get("details", {})
        fng = sentiment_details.get("fear_greed", "—")
        self._fng_lbl.setText(f"F&G: {fng}")

        stablecoin_details = components.get("stablecoin", {}).get("details", {})
        btcd = stablecoin_details.get("btc_dominance_pct", "—")
        self._btcd_lbl.setText(f"BTC.D: {btcd}%" if btcd != "—" else "BTC.D: —%")

        # Timestamp
        self._updated_lbl.setText(datetime.now(timezone.utc).strftime("%H:%M:%S"))

    @Slot(object)
    def _flash_border(self, color: str) -> None:
        """Flash border color on tier change."""
        self.setStyleSheet(
            f"QFrame#CrashScoreWidget {{"
            f"  background: #0D1B2A;"
            f"  border: 2px solid {color};"
            f"  border-radius: 10px;"
            f"}}"
        )

    def cleanup(self) -> None:
        """Unsubscribe from events on close."""
        try:
            bus.unsubscribe(Topics.CRASH_SCORE_UPDATED, self._on_crash_updated)
            bus.unsubscribe(Topics.CRASH_TIER_CHANGED, self._on_tier_changed)
        except Exception:
            pass
