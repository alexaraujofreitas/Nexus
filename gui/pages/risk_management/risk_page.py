# ============================================================
# NEXUS TRADER — Risk Management Page
#
# Real-time risk dashboard wired to PaperExecutor + RiskGate:
#   • Portfolio overview strip   — capital, positions, drawdown
#   • Live risk gauges           — drawdown & position bars
#   • RiskGate parameter panel   — current enforcement limits
#   • Kill Switch                — stops scanner + closes all positions
# ============================================================
from __future__ import annotations

import logging
from typing import Optional

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QFrame,
    QProgressBar, QPushButton, QMessageBox, QSizePolicy,
    QGridLayout, QSpacerItem, QSpinBox, QDoubleSpinBox,
    QScrollArea,
)
from PySide6.QtCore import Qt, QTimer, Slot
from PySide6.QtGui import QColor

from gui.main_window import PageHeader
from core.event_bus import bus, Topics

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────
# Styles
# ─────────────────────────────────────────────────────────────
_CARD_STYLE = (
    "QFrame#card { background:#0D1320; border:1px solid #1A2332; border-radius:6px; }"
)
_SECT_STYLE = "color:#8899AA; font-size:13px; font-weight:600;"

_BTN_KILL = (
    "QPushButton { background:#2A0008; color:#FF1144; "
    "border:2px solid #660022; border-radius:6px; "
    "font-size:14px; font-weight:800; padding:14px 32px; }"
    "QPushButton:hover { background:#3A0010; border-color:#FF1144; }"
    "QPushButton:disabled { background:#0D1320; color:#3A1A1A; "
    "border-color:#1A0A0A; }"
)
_BTN_NEUTRAL = (
    "QPushButton { background:#0D1320; color:#8899AA; border:1px solid #2A3A52; "
    "border-radius:5px; font-size:13px; font-weight:600; padding:6px 18px; }"
    "QPushButton:hover { background:#1A2332; color:#E8EBF0; }"
)

_GAUGE_GREEN = (
    "QProgressBar { background:#0A1018; border:1px solid #1A2332; "
    "border-radius:4px; height:18px; text-align:right; color:#8899AA; "
    "font-size:13px; padding-right:6px; }"
    "QProgressBar::chunk { background:#00CC77; border-radius:3px; }"
)
_GAUGE_YELLOW = (
    "QProgressBar { background:#0A1018; border:1px solid #1A2332; "
    "border-radius:4px; height:18px; text-align:right; color:#8899AA; "
    "font-size:13px; padding-right:6px; }"
    "QProgressBar::chunk { background:#FFB300; border-radius:3px; }"
)
_GAUGE_RED = (
    "QProgressBar { background:#0A1018; border:1px solid #330011; "
    "border-radius:4px; height:18px; text-align:right; color:#FF3355; "
    "font-size:13px; padding-right:6px; }"
    "QProgressBar::chunk { background:#FF3355; border-radius:3px; }"
)

_PARAM_LABEL  = "color:#C0CCE0; font-size:13px;"
_PARAM_VALUE  = "color:#E8EBF0; font-size:13px; font-weight:700;"
_ALERT_ACTIVE = (
    "QFrame { background:#1A0800; border:1px solid #FF6600; border-radius:6px; }"
)
_ALERT_CLEAR  = (
    "QFrame { background:#0A1A0A; border:1px solid #1A3322; border-radius:6px; }"
)


# ─────────────────────────────────────────────────────────────
# Reusable stat card (same family as PaperTradingPage)
# ─────────────────────────────────────────────────────────────
class _StatCard(QWidget):
    def __init__(self, title: str, value: str = "—", color: str = "#E8EBF0"):
        super().__init__()
        v = QVBoxLayout(self)
        v.setContentsMargins(14, 10, 14, 10)
        v.setSpacing(2)

        t = QLabel(title)
        t.setStyleSheet(_SECT_STYLE)

        self._val = QLabel(value)
        self._val.setStyleSheet(f"font-size:18px; font-weight:700; color:{color};")

        v.addWidget(t)
        v.addWidget(self._val)

    def set(self, text: str, color: str = "#E8EBF0"):
        self._val.setText(text)
        self._val.setStyleSheet(f"font-size:18px; font-weight:700; color:{color};")


# ─────────────────────────────────────────────────────────────
# Gauge row: label + progress bar + limit annotation
# ─────────────────────────────────────────────────────────────
class _GaugeRow(QWidget):
    """A labelled progress bar that colour-codes itself against a limit."""

    def __init__(self, title: str, limit: float, unit: str = "%"):
        super().__init__()
        self._limit = max(limit, 0.001)
        self._unit  = unit

        grid = QGridLayout(self)
        grid.setContentsMargins(0, 4, 0, 4)
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(2)

        self._title_lbl = QLabel(title)
        self._title_lbl.setStyleSheet("color:#C0CCE0; font-size:13px; font-weight:600;")
        grid.addWidget(self._title_lbl, 0, 0)

        self._limit_lbl = QLabel(f"LIMIT  {limit}{unit}")
        self._limit_lbl.setStyleSheet("color:#8899AA; font-size:13px; font-weight:600;")
        self._limit_lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        grid.addWidget(self._limit_lbl, 0, 1)

        self._bar = QProgressBar()
        self._bar.setRange(0, 100)
        self._bar.setValue(0)
        self._bar.setFixedHeight(18)
        self._bar.setStyleSheet(_GAUGE_GREEN)
        self._bar.setFormat("")
        grid.addWidget(self._bar, 1, 0, 1, 2)

        self._val_lbl = QLabel("0.00" + unit)
        self._val_lbl.setStyleSheet("color:#00CC77; font-size:13px; font-weight:700;")
        grid.addWidget(self._val_lbl, 2, 0)

        self._status_lbl = QLabel("✓ Within limits")
        self._status_lbl.setStyleSheet("color:#8899AA; font-size:13px;")
        self._status_lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        grid.addWidget(self._status_lbl, 2, 1)

        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)

    def update_value(self, value: float, label_override: str = ""):
        pct = min(100, int(value / self._limit * 100))
        self._bar.setValue(pct)

        disp = label_override if label_override else f"{value:.2f}{self._unit}"
        self._val_lbl.setText(disp)

        if value >= self._limit:
            self._bar.setStyleSheet(_GAUGE_RED)
            self._val_lbl.setStyleSheet("color:#FF3355; font-size:13px; font-weight:700;")
            self._status_lbl.setText("⚠ LIMIT REACHED")
            self._status_lbl.setStyleSheet("color:#FF3355; font-size:13px; font-weight:700;")
        elif value >= self._limit * 0.7:
            self._bar.setStyleSheet(_GAUGE_YELLOW)
            self._val_lbl.setStyleSheet("color:#FFB300; font-size:13px; font-weight:700;")
            self._status_lbl.setText("⚡ Approaching limit")
            self._status_lbl.setStyleSheet("color:#FFB300; font-size:13px;")
        else:
            self._bar.setStyleSheet(_GAUGE_GREEN)
            self._val_lbl.setStyleSheet("color:#00CC77; font-size:13px; font-weight:700;")
            self._status_lbl.setText("✓ Within limits")
            self._status_lbl.setStyleSheet("color:#8899AA; font-size:13px;")

    def set_limit(self, limit: float):
        self._limit = max(limit, 0.001)
        self._limit_lbl.setText(f"LIMIT  {limit}{self._unit}")


# ─────────────────────────────────────────────────────────────
# Parameter row for the RiskGate panel
# ─────────────────────────────────────────────────────────────
class _ParamRow(QWidget):
    def __init__(self, label: str, value: str, note: str = ""):
        super().__init__()
        h = QHBoxLayout(self)
        h.setContentsMargins(0, 4, 0, 4)

        lbl = QLabel(label)
        lbl.setStyleSheet(_PARAM_LABEL)
        lbl.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        h.addWidget(lbl)

        self._val = QLabel(value)
        self._val.setStyleSheet(_PARAM_VALUE)
        self._val.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        h.addWidget(self._val)

        if note:
            n = QLabel(note)
            n.setStyleSheet("color:#445566; font-size:13px;")
            n.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            h.addWidget(n)

    def set(self, value: str):
        self._val.setText(value)


# ─────────────────────────────────────────────────────────────
# Risk Management Page
# ─────────────────────────────────────────────────────────────
class RiskManagementPage(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._kill_active = False
        self._build()
        self._subscribe()
        QTimer.singleShot(400, self._refresh)

    # ── layout ─────────────────────────────────────────────
    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(PageHeader(
            "Risk Management",
            "Real-time drawdown monitoring, position limits, and emergency controls"
        ))

        # ── scroll area content ──────────────────────────────
        content = QWidget()
        content.setStyleSheet("background:#060B14;")
        v = QVBoxLayout(content)
        v.setContentsMargins(24, 20, 24, 24)
        v.setSpacing(16)

        # ── Row 1: stat strip ────────────────────────────────
        strip = QFrame()
        strip.setObjectName("card")
        strip.setStyleSheet(_CARD_STYLE)
        sl = QHBoxLayout(strip)
        sl.setContentsMargins(0, 0, 0, 0)
        sl.setSpacing(0)

        self._stat_value    = _StatCard("PORTFOLIO VALUE",  "—")
        self._stat_cash     = _StatCard("AVAILABLE CAPITAL","—")
        self._stat_pos      = _StatCard("OPEN POSITIONS",   "0 / 3")
        self._stat_drawdown = _StatCard("DRAWDOWN",         "0.00%", "#00CC77")

        for i, w in enumerate([
            self._stat_value, self._stat_cash,
            self._stat_pos, self._stat_drawdown,
        ]):
            if i:
                div = QFrame()
                div.setFrameShape(QFrame.VLine)
                div.setStyleSheet("color:#1A2332;")
                sl.addWidget(div)
            sl.addWidget(w, 1)

        v.addWidget(strip)

        # ── Row 2: gauges + params ───────────────────────────
        body = QHBoxLayout()
        body.setSpacing(16)

        # left: gauges
        gauge_card = QFrame()
        gauge_card.setObjectName("card")
        gauge_card.setStyleSheet(_CARD_STYLE)
        gv = QVBoxLayout(gauge_card)
        gv.setContentsMargins(20, 16, 20, 16)
        gv.setSpacing(8)

        gt = QLabel("LIVE RISK GAUGES")
        gt.setStyleSheet(_SECT_STYLE)
        gv.addWidget(gt)

        sep0 = QFrame()
        sep0.setFrameShape(QFrame.HLine)
        sep0.setStyleSheet("color:#1A2332;")
        gv.addWidget(sep0)

        self._gauge_drawdown  = _GaugeRow("Portfolio Drawdown",   15.0, "%")
        self._gauge_positions = _GaugeRow("Positions Used",        3.0, "")
        self._gauge_capital   = _GaugeRow("Capital Deployed (%)", 100.0, "%")

        gv.addWidget(self._gauge_drawdown)
        gv.addWidget(_hsep())
        gv.addWidget(self._gauge_positions)
        gv.addWidget(_hsep())
        gv.addWidget(self._gauge_capital)
        gv.addStretch()

        body.addWidget(gauge_card, 3)

        # right: RiskGate params (configurable)
        param_card = QFrame()
        param_card.setObjectName("card")
        param_card.setStyleSheet(_CARD_STYLE)
        pv = QVBoxLayout(param_card)
        pv.setContentsMargins(20, 16, 20, 16)
        pv.setSpacing(4)

        pt = QLabel("RISKGATE PARAMETERS")
        pt.setStyleSheet(_SECT_STYLE)
        pv.addWidget(pt)

        sep1 = QFrame()
        sep1.setFrameShape(QFrame.HLine)
        sep1.setStyleSheet("color:#1A2332;")
        pv.addWidget(sep1)

        spin_style = (
            "QSpinBox, QDoubleSpinBox { background:#0D1B2A; color:#E8EBF0; "
            "border:1px solid #1E3A5F; border-radius:4px; padding:2px 6px; "
            "font-size:13px; font-weight:700; }"
            "QSpinBox::up-button, QDoubleSpinBox::up-button,"
            "QSpinBox::down-button, QDoubleSpinBox::down-button { width:16px; }"
        )

        def _spin_row(label_text, widget):
            h = QHBoxLayout()
            h.setContentsMargins(0, 4, 0, 4)
            lbl = QLabel(label_text)
            lbl.setStyleSheet(_PARAM_LABEL)
            lbl.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
            lbl.setWordWrap(False)
            h.addWidget(lbl, 1)
            widget.setFixedWidth(120)
            widget.setStyleSheet(spin_style)
            h.addWidget(widget)
            return h

        # Max Concurrent Positions
        self._spin_max_pos = QSpinBox()
        self._spin_max_pos.setRange(1, 50)
        self._spin_max_pos.setValue(3)
        pv.addLayout(_spin_row("Max Concurrent Positions", self._spin_max_pos))
        pv.addWidget(_hsep())

        # Max Portfolio Drawdown %
        self._spin_max_dd = QDoubleSpinBox()
        self._spin_max_dd.setRange(1.0, 100.0)
        self._spin_max_dd.setSingleStep(0.5)
        self._spin_max_dd.setDecimals(1)
        self._spin_max_dd.setSuffix(" %")
        self._spin_max_dd.setValue(15.0)
        pv.addLayout(_spin_row("Max Portfolio Drawdown", self._spin_max_dd))
        pv.addWidget(_hsep())

        # Max Position Size % of capital
        self._spin_max_pos_pct = QDoubleSpinBox()
        self._spin_max_pos_pct.setRange(1.0, 100.0)
        self._spin_max_pos_pct.setSingleStep(1.0)
        self._spin_max_pos_pct.setDecimals(0)
        self._spin_max_pos_pct.setSuffix(" %")
        self._spin_max_pos_pct.setValue(25.0)
        pv.addLayout(_spin_row("Max Position Size", self._spin_max_pos_pct))
        pv.addWidget(_hsep())

        # Max Bid-Ask Spread %
        self._spin_max_spread = QDoubleSpinBox()
        self._spin_max_spread.setRange(0.01, 5.0)
        self._spin_max_spread.setSingleStep(0.01)
        self._spin_max_spread.setDecimals(2)
        self._spin_max_spread.setSuffix(" %")
        self._spin_max_spread.setValue(0.30)
        pv.addLayout(_spin_row("Max Bid-Ask Spread", self._spin_max_spread))
        pv.addWidget(_hsep())

        # Min Risk:Reward Ratio
        self._spin_min_rr = QDoubleSpinBox()
        self._spin_min_rr.setRange(0.5, 10.0)
        self._spin_min_rr.setSingleStep(0.1)
        self._spin_min_rr.setDecimals(1)
        self._spin_min_rr.setValue(1.3)
        pv.addLayout(_spin_row("Min Risk:Reward Ratio", self._spin_min_rr))
        pv.addWidget(_hsep())

        # Max Capital Allowed for Trading (USDT) — 0 = unlimited
        self._spin_max_capital = QDoubleSpinBox()
        self._spin_max_capital.setRange(0.0, 10_000_000.0)
        self._spin_max_capital.setSingleStep(100.0)
        self._spin_max_capital.setDecimals(0)
        self._spin_max_capital.setPrefix("$ ")
        self._spin_max_capital.setValue(0.0)
        self._spin_max_capital.setSpecialValueText("Unlimited")
        cap_row = _spin_row("Max Capital for Trading", self._spin_max_capital)
        pv.addLayout(cap_row)
        pv.addWidget(_hsep())

        # Apply button
        self._apply_btn = QPushButton("✓  Apply Changes")
        self._apply_btn.setStyleSheet(
            "QPushButton { background:#0A2A1A; color:#00CC77; "
            "border:1px solid #1A6644; border-radius:5px; "
            "font-size:13px; font-weight:700; padding:7px 16px; }"
            "QPushButton:hover { background:#103520; border-color:#00CC77; }"
            "QPushButton:pressed { background:#062010; }"
        )
        self._apply_btn.clicked.connect(self._apply_risk_params)
        pv.addWidget(self._apply_btn)

        self._apply_status_lbl = QLabel("")
        self._apply_status_lbl.setStyleSheet("color:#00CC77; font-size:13px;")
        self._apply_status_lbl.setAlignment(Qt.AlignCenter)
        pv.addWidget(self._apply_status_lbl)

        pv.addStretch()
        body.addWidget(param_card, 2)
        v.addLayout(body)

        # ── Row 3: alert banner ──────────────────────────────
        self._alert_frame = QFrame()
        self._alert_frame.setStyleSheet(_ALERT_CLEAR)
        al = QHBoxLayout(self._alert_frame)
        al.setContentsMargins(16, 10, 16, 10)
        self._alert_lbl = QLabel("✓  All risk limits within acceptable range.")
        self._alert_lbl.setStyleSheet("color:#3CAA6C; font-size:13px;")
        self._alert_lbl.setWordWrap(True)
        al.addWidget(self._alert_lbl)
        v.addWidget(self._alert_frame)

        # ── Row 4: kill switch ────────────────────────────────
        kill_card = QFrame()
        kill_card.setObjectName("card")
        kill_card.setStyleSheet(_CARD_STYLE)
        kv = QVBoxLayout(kill_card)
        kv.setContentsMargins(20, 16, 20, 16)
        kv.setSpacing(10)

        kt = QLabel("EMERGENCY CONTROLS")
        kt.setStyleSheet(_SECT_STYLE)
        kv.addWidget(kt)

        sep2 = QFrame()
        sep2.setFrameShape(QFrame.HLine)
        sep2.setStyleSheet("color:#1A2332;")
        kv.addWidget(sep2)

        # ── Execution mode row ─────────────────────────────────
        mode_row = QHBoxLayout()
        mode_lbl = QLabel("Execution Mode")
        mode_lbl.setStyleSheet(_PARAM_LABEL)
        mode_row.addWidget(mode_lbl)
        mode_row.addStretch()

        self._mode_badge = QLabel("● PAPER")
        self._mode_badge.setStyleSheet("color:#00CC77; font-size:13px; font-weight:700;")
        mode_row.addWidget(self._mode_badge)

        self._mode_toggle_btn = QPushButton("Switch to LIVE")
        self._mode_toggle_btn.setFixedHeight(28)
        self._mode_toggle_btn.setToolTip(
            "Switch execution mode between Paper (simulated) and Live (real CCXT orders).\n"
            "Requires an active exchange connection.\n"
            "⚠  LIVE MODE PLACES REAL ORDERS WITH REAL MONEY ⚠"
        )
        self._mode_toggle_btn.setStyleSheet(
            "QPushButton { background:#1A1A2A; color:#FF9933; border:1px solid #AA5500; "
            "border-radius:4px; font-size:13px; font-weight:700; padding:0 12px; }"
            "QPushButton:hover { background:#2A1A10; border-color:#FF7700; color:#FFBB44; }"
        )
        self._mode_toggle_btn.clicked.connect(self._on_mode_toggle)
        mode_row.addWidget(self._mode_toggle_btn)

        kv.addLayout(mode_row)

        # Scanner status row
        sc_row = QHBoxLayout()

        sc_lbl = QLabel("Scanner Status")
        sc_lbl.setStyleSheet(_PARAM_LABEL)
        sc_row.addWidget(sc_lbl)
        sc_row.addStretch()

        self._scanner_dot = QLabel("● UNKNOWN")
        self._scanner_dot.setStyleSheet("color:#445566; font-size:13px; font-weight:700;")
        sc_row.addWidget(self._scanner_dot)

        kv.addLayout(sc_row)

        # Kill switch button + description
        kb = QHBoxLayout()
        kb.setSpacing(16)

        self._kill_btn = QPushButton("⚠  EMERGENCY STOP")
        self._kill_btn.setStyleSheet(_BTN_KILL)
        self._kill_btn.setMinimumHeight(52)
        self._kill_btn.setToolTip(
            "Stops the IDSS scanner and immediately closes all open paper positions "
            "at their last known mark price. This action cannot be undone."
        )
        self._kill_btn.clicked.connect(self._on_kill_clicked)
        kb.addWidget(self._kill_btn, 1)

        kill_info = QLabel(
            "<b>What this does:</b> Immediately halts the IDSS scanner (no new signals), "
            "then closes all open paper positions at their last known mark price. "
            "Realized P&L is booked. Use this if you detect runaway losses or wish to "
            "pause all activity instantly."
        )
        kill_info.setStyleSheet("color:#8899AA; font-size:13px;")
        kill_info.setWordWrap(True)
        kill_info.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        kb.addWidget(kill_info, 2)

        kv.addLayout(kb)

        # Reset kill switch row
        reset_row = QHBoxLayout()
        reset_row.addStretch()

        self._reset_kill_btn = QPushButton("↺  Re-arm")
        self._reset_kill_btn.setStyleSheet(_BTN_NEUTRAL)
        self._reset_kill_btn.setEnabled(False)
        self._reset_kill_btn.setToolTip("Re-enable the emergency stop button after it has been activated.")
        self._reset_kill_btn.clicked.connect(self._on_rearm_clicked)
        reset_row.addWidget(self._reset_kill_btn)

        kv.addLayout(reset_row)
        v.addWidget(kill_card)

        # ── Row 5: Correlation Matrix ─────────────────────────────
        corr_group = QFrame()
        corr_group.setObjectName("card")
        corr_group.setStyleSheet(_CARD_STYLE)
        corr_layout = QVBoxLayout(corr_group)
        corr_layout.setContentsMargins(20, 16, 20, 16)
        corr_layout.setSpacing(8)

        corr_title = QLabel("PORTFOLIO CORRELATION MATRIX")
        corr_title.setStyleSheet(_SECT_STYLE)
        corr_layout.addWidget(corr_title)

        sep_corr = QFrame()
        sep_corr.setFrameShape(QFrame.HLine)
        sep_corr.setStyleSheet("color:#1A2332;")
        corr_layout.addWidget(sep_corr)

        # Simple 4x4 correlation grid
        symbols = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT", "XRP/USDT"]
        grid = QGridLayout()
        grid.setSpacing(2)

        # Header row
        for j, sym in enumerate(symbols):
            lbl = QLabel(sym.split("/")[0])
            lbl.setAlignment(Qt.AlignCenter)
            lbl.setStyleSheet("color: #8899AA; font-size: 13px; font-weight: 700;")
            grid.addWidget(lbl, 0, j + 1)

        # Data rows
        try:
            from core.portfolio.correlation_controller import get_pair_correlation
        except ImportError:
            get_pair_correlation = None

        for i, sym_a in enumerate(symbols):
            row_lbl = QLabel(sym_a.split("/")[0])
            row_lbl.setStyleSheet("color: #8899AA; font-size: 13px; font-weight: 700;")
            grid.addWidget(row_lbl, i + 1, 0)
            for j, sym_b in enumerate(symbols):
                if get_pair_correlation:
                    corr = get_pair_correlation(sym_a, sym_b)
                else:
                    # Fallback correlation values for demo
                    corr = 0.85 if i == j else (0.70 + (i - j) * 0.05)

                cell = QLabel(f"{corr:.2f}")
                cell.setAlignment(Qt.AlignCenter)
                cell.setFixedSize(60, 28)
                # Color code: red = high, orange = moderate, green = medium, blue = low
                if corr >= 0.90:
                    bg = "#7F1D1D"  # dark red
                elif corr >= 0.75:
                    bg = "#B45309"  # orange
                elif corr >= 0.60:
                    bg = "#065F46"  # green
                else:
                    bg = "#1E3A5F"  # blue
                cell.setStyleSheet(
                    f"background: {bg}; color: #E8EBF0; font-size: 13px; "
                    "font-weight: 700; border-radius: 4px;"
                )
                grid.addWidget(cell, i + 1, j + 1)

        corr_layout.addLayout(grid)

        cap_lbl = QLabel("Red: >=0.90 (highly correlated)  Orange: >=0.75  Green: >=0.60  Blue: <0.60 (low)")
        cap_lbl.setStyleSheet("color: #4A5568; font-size: 13px;")
        corr_layout.addWidget(cap_lbl)

        v.addWidget(corr_group)

        v.addStretch()

        # ── Scroll area — wraps all content below the header ──
        scroll = QScrollArea()
        scroll.setWidget(content)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        scroll.setStyleSheet(
            "QScrollArea { background:#060B14; border:none; }"
            "QScrollBar:vertical {"
            "  background:#060B14; width:8px; margin:0; border-radius:4px;"
            "}"
            "QScrollBar::handle:vertical {"
            "  background:#2A3A52; border-radius:4px; min-height:24px;"
            "}"
            "QScrollBar::handle:vertical:hover { background:#3D5270; }"
            "QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {"
            "  height:0; width:0; border:none; background:none;"
            "}"
            "QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {"
            "  background:none;"
            "}"
        )
        root.addWidget(scroll, 1)

    # ── EventBus subscriptions ─────────────────────────────
    def _subscribe(self):
        bus.subscribe(Topics.TRADE_OPENED,     self._on_portfolio_event)
        bus.subscribe(Topics.TRADE_CLOSED,     self._on_portfolio_event)
        bus.subscribe(Topics.POSITION_UPDATED, self._on_position_updated)
        bus.subscribe(Topics.FEED_STATUS,      self._on_feed_status)
        bus.subscribe(Topics.SCAN_CYCLE_COMPLETE, self._on_scan_cycle)
        bus.subscribe(Topics.MODE_CHANGED,     self._on_mode_changed)

    # ── Refresh ────────────────────────────────────────────
    @Slot()
    def _refresh(self):
        try:
            from core.execution.order_router import order_router as _router
            _pe       = _router.active_executor
            stats     = _pe.get_stats() if hasattr(_pe, "get_stats") else {}
            positions = _pe.get_open_positions()
            drawdown  = _pe.drawdown_pct
            available = _pe.available_capital

            # Try to get RiskGate limits from scanner singleton
            try:
                from core.scanning.scanner import scanner as _sc
                gate = _sc._risk_gate
                max_pos    = gate.max_concurrent_positions
                max_dd     = gate.max_portfolio_drawdown_pct
                max_pp     = gate.max_position_capital_pct * 100
                max_spread = gate.max_spread_pct
                min_rr     = gate.min_risk_reward
                max_capital = getattr(gate, 'max_capital_usdt', 0.0)
            except Exception:
                max_pos    = 3
                max_dd     = 15.0
                max_pp     = 25.0
                max_spread = 0.30
                min_rr     = 1.3
                max_capital = 0.0

            # ── stat strip ──────────────────────────────────
            total_value = available + sum(
                p.get("size_usdt", 0) * (1 + p.get("unrealized_pnl", 0) / 100)
                for p in positions
            )
            self._stat_value.set(f"${total_value:,.2f}")
            self._stat_cash.set(f"${available:,.2f}")

            open_count = len(positions)
            pos_color = "#FF3355" if open_count >= max_pos else (
                "#FFB300" if open_count >= max_pos - 1 else "#E8EBF0"
            )
            self._stat_pos.set(f"{open_count} / {max_pos}", pos_color)

            dd_color = (
                "#FF3355" if drawdown >= max_dd else
                "#FFB300" if drawdown >= max_dd * 0.7 else
                "#00CC77"
            )
            self._stat_drawdown.set(f"{drawdown:.2f}%", dd_color)

            # ── gauges ──────────────────────────────────────
            self._gauge_drawdown.set_limit(max_dd)
            self._gauge_drawdown.update_value(drawdown)

            self._gauge_positions.set_limit(float(max_pos))
            self._gauge_positions.update_value(
                float(open_count),
                label_override=f"{open_count} of {max_pos}"
            )

            # Capital deployed = 1 - (available / total_value)
            if total_value > 0:
                deployed_pct = (1 - available / total_value) * 100
            else:
                deployed_pct = 0.0
            self._gauge_capital.update_value(deployed_pct)

            # ── Update spinners to match current gate state ──────
            self._spin_max_pos.setValue(max_pos)
            self._spin_max_dd.setValue(max_dd)
            self._spin_max_pos_pct.setValue(max_pp)
            self._spin_max_spread.setValue(max_spread)
            self._spin_min_rr.setValue(min_rr)
            self._spin_max_capital.setValue(max_capital)

            # ── Alert banner ─────────────────────────────────
            alerts = []
            if drawdown >= max_dd:
                alerts.append(f"DRAWDOWN LIMIT HIT: {drawdown:.2f}% ≥ {max_dd:.1f}%")
                bus.publish(Topics.DRAWDOWN_ALERT,
                            {"drawdown": drawdown, "limit": max_dd},
                            source="risk_page")
            if open_count >= max_pos:
                alerts.append(f"MAX POSITIONS REACHED: {open_count} / {max_pos}")
                bus.publish(Topics.RISK_LIMIT_HIT,
                            {"type": "max_positions", "current": open_count, "limit": max_pos},
                            source="risk_page")
            elif drawdown >= max_dd * 0.7:
                alerts.append(
                    f"Drawdown warning: {drawdown:.2f}% is approaching the {max_dd:.1f}% limit."
                )

            if alerts:
                self._alert_frame.setStyleSheet(_ALERT_ACTIVE)
                self._alert_lbl.setStyleSheet("color:#FF6600; font-size:13px; font-weight:700;")
                self._alert_lbl.setText("⚠  " + "   |   ".join(alerts))
            else:
                self._alert_frame.setStyleSheet(_ALERT_CLEAR)
                self._alert_lbl.setStyleSheet("color:#3CAA6C; font-size:13px;")
                self._alert_lbl.setText("✓  All risk limits within acceptable range.")

        except Exception as exc:
            logger.debug("RiskPage refresh error: %s", exc)

    def _refresh_scanner_status(self):
        try:
            from core.scanning.scanner import scanner as _sc
            if getattr(_sc, "_running", False):
                self._scanner_dot.setText("● RUNNING")
                self._scanner_dot.setStyleSheet(
                    "color:#00CC77; font-size:13px; font-weight:700;"
                )
            else:
                self._scanner_dot.setText("● STOPPED")
                self._scanner_dot.setStyleSheet(
                    "color:#8899AA; font-size:13px; font-weight:700;"
                )
        except Exception:
            self._scanner_dot.setText("● UNKNOWN")
            self._scanner_dot.setStyleSheet(
                "color:#445566; font-size:13px; font-weight:700;"
            )

    # ── Apply risk parameters ─────────────────────────────
    @Slot()
    def _apply_risk_params(self) -> None:
        """Apply spinner values to the live RiskGate and persist to settings.yaml."""
        from config.settings import settings

        max_pos     = self._spin_max_pos.value()
        max_dd      = self._spin_max_dd.value()
        max_pp      = self._spin_max_pos_pct.value() / 100.0
        max_spread  = self._spin_max_spread.value()
        min_rr      = self._spin_min_rr.value()
        max_capital = self._spin_max_capital.value()

        # ── Apply to live scanner's RiskGate ──────────────────
        try:
            from core.scanning.scanner import scanner as _sc
            gate = _sc._risk_gate
            gate.max_concurrent_positions   = max_pos
            gate.max_portfolio_drawdown_pct = max_dd
            gate.max_position_capital_pct   = max_pp
            gate.max_spread_pct             = max_spread
            gate.min_risk_reward            = min_rr
            gate.max_capital_usdt           = max_capital
            logger.info("RiskPage: RiskGate parameters updated via UI")
        except Exception as exc:
            logger.warning("RiskPage: could not update live RiskGate: %s", exc)

        # ── Persist to settings.yaml ───────────────────────────
        try:
            risk_cfg = {
                "max_concurrent_positions": max_pos,
                "max_portfolio_drawdown_pct": max_dd,
                "max_position_capital_pct": max_pp * 100.0,
                "max_spread_pct": max_spread,
                "min_risk_reward": min_rr,
                "max_capital_usdt": max_capital,
            }
            settings.set("risk", risk_cfg)
            logger.info("RiskPage: Risk parameters saved to settings.yaml")
        except Exception as exc:
            logger.warning("RiskPage: could not persist risk params: %s", exc)

        # ── Update gauges to reflect new limits ───────────────
        self._gauge_drawdown.set_limit(max_dd)
        self._gauge_positions.set_limit(float(max_pos))

        # ── Feedback message ──────────────────────────────────
        self._apply_status_lbl.setText("✓ Parameters applied and saved")
        self._apply_status_lbl.setStyleSheet("color:#00CC77; font-size:13px;")
        QTimer.singleShot(3000, lambda: self._apply_status_lbl.setText(""))

    # ── Kill switch ────────────────────────────────────────
    @Slot()
    def _on_kill_clicked(self):
        try:
            from core.execution.order_router import order_router as _router
            _executor  = _router.active_executor
            open_count = len(_executor.get_open_positions())
            mode_label = "LIVE" if _router.mode == "live" else "paper"
        except Exception:
            open_count = 0
            mode_label = "paper"

        msg = QMessageBox(self)
        msg.setWindowTitle("Emergency Stop — Confirm")
        msg.setText(
            "<b style='color:#FF1144;'>⚠  EMERGENCY STOP</b>"
        )
        detail = (
            "This will:\n\n"
            "  1.  Stop the IDSS scanner immediately (no new signals)\n"
            f"  2.  Close {open_count} open {mode_label} position(s) at last mark price\n\n"
            f"Realized P&L will be booked to your {mode_label} account.\n"
            "The scanner can be restarted from the Market Scanner page."
        )
        msg.setInformativeText(detail)
        msg.setStandardButtons(QMessageBox.Yes | QMessageBox.Cancel)
        msg.setDefaultButton(QMessageBox.Cancel)
        msg.button(QMessageBox.Yes).setText("⚠  Execute Emergency Stop")
        msg.setStyleSheet(
            "QMessageBox { background:#0A0E1A; color:#E8EBF0; }"
            "QLabel { color:#E8EBF0; font-size:13px; }"
            "QPushButton { background:#1A0A0A; color:#FF3355; "
            "border:1px solid #440011; border-radius:4px; "
            "min-width:180px; padding:8px; font-weight:700; }"
        )

        if msg.exec() != QMessageBox.Yes:
            return

        closed = 0
        stopped = False

        # 1. Stop scanner
        try:
            from core.scanning.scanner import scanner as _sc
            _sc.stop()
            stopped = True
            logger.info("RiskPage: Scanner stopped via emergency stop")
        except Exception as exc:
            logger.warning("RiskPage: could not stop scanner: %s", exc)

        # 2. Close all positions via active executor
        try:
            from core.execution.order_router import order_router as _router
            closed = _router.active_executor.close_all()
            logger.info("RiskPage: closed %d positions via emergency stop", closed)
        except Exception as exc:
            logger.warning("RiskPage: could not close positions: %s", exc)

        # 3. Publish emergency stop event
        bus.publish(Topics.EMERGENCY_STOP, {
            "positions_closed": closed,
            "scanner_stopped":  stopped,
        }, source="risk_page")

        # 4. Update UI
        self._kill_btn.setEnabled(False)
        self._kill_btn.setText("✓  EMERGENCY STOP EXECUTED")
        self._kill_btn.setStyleSheet(
            "QPushButton { background:#0A0A0A; color:#445566; "
            "border:1px solid #1A1A1A; border-radius:6px; "
            "font-size:14px; font-weight:800; padding:14px 32px; }"
        )
        self._reset_kill_btn.setEnabled(True)
        self._kill_active = True

        self._scanner_dot.setText("● STOPPED")
        self._scanner_dot.setStyleSheet(
            "color:#FF3355; font-size:13px; font-weight:700;"
        )

        # 5. Refresh metrics
        self._refresh()

        logger.info(
            "RiskPage: Emergency stop complete — scanner_stopped=%s, positions_closed=%d",
            stopped, closed,
        )

    @Slot()
    def _on_rearm_clicked(self):
        self._kill_btn.setEnabled(True)
        self._kill_btn.setText("⚠  EMERGENCY STOP")
        self._kill_btn.setStyleSheet(_BTN_KILL)
        self._reset_kill_btn.setEnabled(False)
        self._kill_active = False
        self._refresh_scanner_status()

    # ── Execution mode toggle ───────────────────────────────
    def _on_mode_toggle(self):
        """Switch between paper and live execution modes."""
        from core.execution.order_router import order_router
        current_mode = order_router.mode

        if current_mode == "paper":
            # Switching to LIVE — show a serious confirmation dialog
            from PySide6.QtWidgets import QMessageBox
            msg = QMessageBox(self)
            msg.setWindowTitle("⚠  Switch to LIVE Trading Mode")
            msg.setIcon(QMessageBox.Warning)
            msg.setText(
                "<b style='color:#FF3355; font-size:14px;'>⚠  You are about to enable LIVE trading.</b><br><br>"
                "In LIVE mode, the IDSS scanner will place <b>real orders</b> on your connected exchange "
                "using <b>real funds</b>. This cannot be undone without closing positions.<br><br>"
                "Confirm that:<br>"
                "• An exchange is configured and active<br>"
                "• You understand this risks real capital<br>"
                "• API keys have Read + Trade (no Withdrawal) permissions<br>"
            )
            msg.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
            msg.setDefaultButton(QMessageBox.No)
            yes_btn = msg.button(QMessageBox.Yes)
            yes_btn.setText("⚠  Enable LIVE Mode")
            yes_btn.setStyleSheet("background:#AA1122; color:#FFF; font-weight:700; padding:6px 16px;")
            if msg.exec() != QMessageBox.Yes:
                return

            # Verify exchange is connected
            try:
                from core.market_data.exchange_manager import exchange_manager
                if not exchange_manager.is_connected():
                    QMessageBox.warning(
                        self, "No Exchange Connected",
                        "Please configure and activate an exchange in Exchange Management first."
                    )
                    return
            except Exception:
                pass

            order_router.set_mode("live")

        else:
            # Switching back to paper — simpler confirm
            from PySide6.QtWidgets import QMessageBox
            reply = QMessageBox.question(
                self, "Switch to Paper Mode",
                "Switch to PAPER mode? No new live orders will be placed.\n"
                "Existing open positions on the exchange are NOT automatically closed.",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.Yes,
            )
            if reply == QMessageBox.Yes:
                order_router.set_mode("paper")

    def _on_mode_changed(self, event):
        """Update the mode badge and toggle button when mode changes."""
        from PySide6.QtCore import QTimer
        QTimer.singleShot(0, self._refresh_mode_ui)

    def _refresh_mode_ui(self):
        """Sync mode badge + toggle button to the current router mode."""
        try:
            from core.execution.order_router import order_router
            mode = order_router.mode
            if mode == "live":
                self._mode_badge.setText("● LIVE")
                self._mode_badge.setStyleSheet("color:#FF3355; font-size:13px; font-weight:700;")
                self._mode_toggle_btn.setText("Switch to PAPER")
                self._mode_toggle_btn.setStyleSheet(
                    "QPushButton { background:#1A0A0A; color:#FF6688; border:1px solid #882244; "
                    "border-radius:4px; font-size:13px; font-weight:700; padding:0 12px; }"
                    "QPushButton:hover { background:#2A0A10; border-color:#CC4466; }"
                )
                self._kill_btn.setToolTip(
                    "Stops the IDSS scanner and closes all open LIVE positions "
                    "on the exchange via market orders."
                )
            else:
                self._mode_badge.setText("● PAPER")
                self._mode_badge.setStyleSheet("color:#00CC77; font-size:13px; font-weight:700;")
                self._mode_toggle_btn.setText("Switch to LIVE")
                self._mode_toggle_btn.setStyleSheet(
                    "QPushButton { background:#1A1A2A; color:#FF9933; border:1px solid #AA5500; "
                    "border-radius:4px; font-size:13px; font-weight:700; padding:0 12px; }"
                    "QPushButton:hover { background:#2A1A10; border-color:#FF7700; color:#FFBB44; }"
                )
                self._kill_btn.setToolTip(
                    "Stops the IDSS scanner and closes all open paper positions "
                    "at their last known mark price."
                )
        except Exception:
            pass

    # ── Event handlers ─────────────────────────────────────
    def _on_portfolio_event(self, event):
        """Full refresh on any trade lifecycle event."""
        QTimer.singleShot(0, self._refresh)

    def _on_position_updated(self, event):
        """Lightweight: only refresh gauges + strip on mark-price tick."""
        QTimer.singleShot(0, self._refresh)

    def _on_feed_status(self, event):
        """Update scanner dot when feed starts/stops."""
        QTimer.singleShot(0, self._refresh_scanner_status)

    def _on_scan_cycle(self, event):
        """Mark scanner as running when a cycle completes."""
        self._scanner_dot.setText("● RUNNING")
        self._scanner_dot.setStyleSheet(
            "color:#00CC77; font-size:13px; font-weight:700;"
        )


# ─────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────
def _hsep() -> QFrame:
    f = QFrame()
    f.setFrameShape(QFrame.HLine)
    f.setStyleSheet("color:#1A2332;")
    return f
