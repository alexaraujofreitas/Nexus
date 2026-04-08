# ============================================================
# NEXUS TRADER — Orders & Positions Page
#
# Trade journal with full IDSS metadata:
#   • Filter bar    — symbol, side, regime, exit reason, score
#   • Summary strip — aggregate stats across filtered trades
#   • Positions tab — live open positions (read-only)
#   • Journal tab   — complete closed-trade history, sortable
#   • Detail panel  — per-trade rationale, IDSS breakdown
# ============================================================
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QFrame,
    QTableWidget, QTableWidgetItem, QHeaderView,
    QAbstractItemView, QPushButton, QComboBox,
    QLineEdit, QTabWidget, QTextEdit, QSplitter,
)
from PySide6.QtCore import Qt, QTimer, Slot
from PySide6.QtGui import QColor

from gui.main_window import PageHeader
from core.event_bus import bus, Topics

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────
# Shared style constants
# ─────────────────────────────────────────────────────────────
_TABLE_STYLE = (
    "QTableWidget { background:#0A0E1A; color:#E8EBF0; "
    "gridline-color:#141E2E; font-size:13px; border:none; }"
    "QTableWidget::item:selected { background:#1A2D4A; }"
    "QTableWidget::item:alternate { background:#0C1018; }"
    "QHeaderView::section { background:#0D1320; color:#C8D0E0; "
    "padding:6px 8px; border:none; "
    "border-bottom:1px solid #1A2332; font-size:13px; font-weight:600; }"
)
_CARD_STYLE = (
    "QFrame#card { background:#0D1320; border:1px solid #1A2332; border-radius:6px; }"
)
_COMBO_STYLE = (
    "QComboBox { background:#0F1623; color:#E8EBF0; border:1px solid #2A3A52; "
    "border-radius:4px; padding:2px 8px; font-size:13px; min-height:26px; }"
    "QComboBox QAbstractItemView { background:#0F1623; color:#E8EBF0; "
    "selection-background-color:#1A2D4A; border:1px solid #2A3A52; }"
)
_EDIT_STYLE = (
    "QLineEdit { background:#0F1623; color:#E8EBF0; border:1px solid #2A3A52; "
    "border-radius:4px; padding:2px 8px; font-size:13px; min-height:26px; }"
    "QLineEdit:focus { border-color:#1E90FF; }"
)
_TAB_STYLE = (
    "QTabWidget::pane { border:none; background:#080C16; }"
    "QTabBar::tab { background:#0D1320; color:#8899AA; padding:7px 16px; "
    "font-size:13px; font-weight:600; border:none; "
    "border-bottom:2px solid transparent; }"
    "QTabBar::tab:selected { color:#E8EBF0; border-bottom:2px solid #1E90FF; }"
    "QTabBar::tab:hover { color:#C8D0E0; }"
)
_SECT_STYLE  = "color:#C8D0E0; font-size:13px; font-weight:600;"
_LBL_STYLE   = "color:#C8D0E0; font-size:13px; font-weight:600;"


# ─────────────────────────────────────────────────────────────
# Regime / model metadata (duplicated here to keep page self-contained)
# ─────────────────────────────────────────────────────────────
REGIME_COLORS = {
    "bull_trend":            "#00CC77",
    "bear_trend":            "#FF3355",
    "ranging":               "#FFB300",
    "volatility_expansion":  "#1E90FF",
    "volatility_compression":"#8899AA",
    "uncertain":             "#4A6A8A",
}
REGIME_LABELS = {
    "bull_trend":            "Bull Trend",
    "bear_trend":            "Bear Trend",
    "ranging":               "Ranging",
    "volatility_expansion":  "Vol Expansion",
    "volatility_compression":"Vol Compress",
    "uncertain":             "Uncertain",
    "":                      "—",
}
MODEL_ABBREVS = {
    "trend":              "TRD",
    "mean_reversion":     "MRV",
    "momentum_breakout":  "MOM",
    "liquidity_sweep":    "LIQ",
}
EXIT_LABELS = {
    "stop_loss":    ("Stop Loss",    "#FF3355"),
    "take_profit":  ("Take Profit",  "#00CC77"),
    "manual_close": ("Manual",       "#8899AA"),
}


# ─────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────
def _ci(text: str, color: str = "#E8EBF0",
        align=Qt.AlignCenter) -> QTableWidgetItem:
    item = QTableWidgetItem(text)
    item.setForeground(QColor(color))
    item.setTextAlignment(align)
    item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
    return item


def _ni(value: float, text: str, color: str = "#E8EBF0") -> QTableWidgetItem:
    item = QTableWidgetItem(text)
    item.setData(Qt.UserRole, value)
    item.setForeground(QColor(color))
    item.setTextAlignment(Qt.AlignCenter)
    item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
    return item


def _fmt_price(v: float) -> str:
    if not v:
        return "—"
    return f"{v:,.2f}" if v >= 1_000 else (f"{v:.4f}" if v >= 1 else f"{v:.6f}")


def _fmt_dur(s: int) -> str:
    if s < 60:   return f"{s}s"
    if s < 3600: return f"{s // 60}m"
    if s < 86400:
        h = s // 3600; m = (s % 3600) // 60
        return f"{h}h {m}m"
    d = s // 86400; h = (s % 86400) // 3600
    return f"{d}d {h}h"


def _fmt_age(iso: str) -> str:
    try:
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return _fmt_dur(int((datetime.now(timezone.utc) - dt).total_seconds()))
    except Exception:
        return "—"


def _models_text(models: list) -> str:
    return "  ".join(MODEL_ABBREVS.get(m, m[:3].upper()) for m in models) or "—"


def _score_color(s: float) -> str:
    return "#00CC77" if s >= 0.75 else ("#FFB300" if s >= 0.55 else "#FF9800")


def _bar(score: float, width: int = 20) -> str:
    """ASCII progress bar for a 0-100 score."""
    filled = round(score / 100 * width)
    return "█" * filled + "░" * (width - filled)


def _format_analysis_block(analysis: dict, is_open: bool = False) -> list[str]:
    """
    Format an analysis dict into plain-text lines for the detail panel.
    Returns list of lines (not joined).
    """
    if not analysis:
        return []

    lines: list[str] = []
    sep = "─" * 42

    cls_    = analysis.get("classification", "NEUTRAL")
    emoji   = {"GOOD": "✅", "BAD": "❌", "NEUTRAL": "⚖️"}.get(cls_, "⚖️")
    overall = analysis.get("overall_score", 0.0)
    setup   = analysis.get("setup_score",   0.0)
    risk    = analysis.get("risk_score",    0.0)
    exec_   = analysis.get("execution_score", 0.0)
    dec_    = analysis.get("decision_score",  0.0)
    rr      = analysis.get("rr_ratio")

    lines.append("")
    lines.append(sep)
    if is_open:
        lines.append(f"  AI ENTRY QUALITY  ·  Setup: {setup:.0f}/100  Risk: {risk:.0f}/100")
        rr_str = f"{rr:.2f}" if rr else "—"
        lines.append(f"  R:R: {rr_str}   |   Regime affinity: "
                     f"{['-', '=', '+'][analysis.get('regime_affinity', 0) + 1]}")
    else:
        lines.append(f"  AI QUALITY SCORECARD  {emoji} {cls_}  |  Overall: {overall:.0f}/100")
        lines.append(f"  {_bar(overall)}")
        lines.append(f"  Setup:     {setup:.0f:>3}  {_bar(setup, 12)}")
        lines.append(f"  Risk:      {risk:.0f:>3}  {_bar(risk, 12)}")
        lines.append(f"  Execution: {exec_:.0f:>3}  {_bar(exec_, 12)}")
        lines.append(f"  Decision:  {dec_:.0f:>3}  {_bar(dec_, 12)}")
        rr_str = f"{rr:.2f}" if rr else "—"
        lines.append(f"  R:R ratio: {rr_str}")

    hard = analysis.get("hard_overrides") or []
    if hard:
        lines.append("")
        lines.append("  ⚠  HARD OVERRIDES:")
        for h in hard:
            lines.append(f"     • {h}")

    root_causes = analysis.get("root_causes") or []
    if root_causes:
        lines.append("")
        lines.append("  ROOT CAUSES:")
        for rc in root_causes[:5]:
            sev  = rc.get("severity", "minor").upper()[:3]
            desc = rc.get("description", "")
            lines.append(f"  [{sev}] {desc}")

    recs = analysis.get("recommendations") or []
    if recs and not is_open:
        lines.append("")
        lines.append("  RECOMMENDATIONS:")
        for i, rec in enumerate(recs[:3], 1):
            action = rec.get("action", "")
            safe   = "✓" if rec.get("auto_tune_safe") else "⚑"
            # Wrap long action text
            if len(action) > 70:
                lines.append(f"  {i}. [{safe}] {action[:70]}…")
            else:
                lines.append(f"  {i}. [{safe}] {action}")

    ai_exp = analysis.get("ai_explanation")
    if ai_exp:
        lines.append("")
        lines.append("  AI EXPLANATION:")
        # Word-wrap at ~70 chars
        words = ai_exp.split()
        current_line = "  "
        for word in words:
            if len(current_line) + len(word) + 1 > 72:
                lines.append(current_line)
                current_line = f"  {word}"
            else:
                current_line += f" {word}" if current_line.strip() else f"  {word}"
        if current_line.strip():
            lines.append(current_line)

    lines.append(sep)
    return lines


# ─────────────────────────────────────────────────────────────
# Summary strip widget
# ─────────────────────────────────────────────────────────────
class _Stat(QWidget):
    def __init__(self, title: str):
        super().__init__()
        v = QVBoxLayout(self)
        v.setContentsMargins(8, 6, 8, 6)
        v.setSpacing(3)
        t = QLabel(title)
        t.setStyleSheet(
            "color:#C8D0E0; font-size:13px; font-weight:700; letter-spacing:0.5px;"
        )
        self._val = QLabel("—")
        self._val.setStyleSheet("font-size:15px; font-weight:700; color:#E8EBF0;")
        self._val.setMinimumWidth(60)
        v.addStretch()
        v.addWidget(t)
        v.addWidget(self._val)
        v.addStretch()

    def set(self, text: str, color: str = "#E8EBF0"):
        self._val.setText(text)
        self._val.setStyleSheet(f"font-size:15px; font-weight:700; color:{color};")


# ─────────────────────────────────────────────────────────────
# Column definitions
# ─────────────────────────────────────────────────────────────
_POS_COLS  = ["Symbol", "Side", "Regime", "TF", "Entry", "Mark",
              "Unreal. P&L", "Stop", "Target", "Score", "Models", "Age"]

_JOUR_COLS = ["Symbol", "Side", "Regime", "TF", "Entry", "Exit",
              "P&L %", "P&L $", "Exit Reason", "Score",
              "Models", "Duration", "Date"]


# ─────────────────────────────────────────────────────────────
# Orders & Positions Page
# ─────────────────────────────────────────────────────────────
class OrdersPositionsPage(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._all_trades: list[dict] = []   # full unfiltered history
        self._age_timer: Optional[QTimer] = None
        self._build()
        self._subscribe()
        QTimer.singleShot(400, self._full_refresh)

    # ── layout ─────────────────────────────────────────────
    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        root.addWidget(PageHeader(
            "Orders & Positions",
            "Trade journal with IDSS regime labels, model attribution, and rationale"
        ))

        body = QWidget()
        bv = QVBoxLayout(body)
        bv.setContentsMargins(16, 10, 16, 10)
        bv.setSpacing(8)

        bv.addWidget(self._build_filter_bar())
        bv.addWidget(self._build_summary_strip())

        # Main content: tabs + detail panel in a splitter
        splitter = QSplitter(Qt.Vertical)
        splitter.setHandleWidth(5)
        splitter.setStyleSheet(
            "QSplitter::handle{background:#1A2332;}"
            "QSplitter::handle:hover{background:#2A3A52;}"
        )

        # Tabs
        self._tabs = QTabWidget()
        self._tabs.setStyleSheet(_TAB_STYLE)
        self._tabs.addTab(self._build_positions_tab(), "  ◉  Open Positions  ")
        self._tabs.addTab(self._build_journal_tab(),   "  ≡  Trade Journal  ")
        splitter.addWidget(self._tabs)

        # Detail panel
        splitter.addWidget(self._build_detail_panel())
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 1)

        bv.addWidget(splitter, 1)
        root.addWidget(body, 1)

    # ── filter bar ──────────────────────────────────────────
    def _build_filter_bar(self) -> QFrame:
        bar = QFrame()
        bar.setObjectName("card")
        bar.setStyleSheet(_CARD_STYLE)
        h = QHBoxLayout(bar)
        h.setContentsMargins(16, 10, 16, 10)
        h.setSpacing(8)

        def _lbl(t: str) -> QLabel:
            l = QLabel(t)
            l.setStyleSheet(_LBL_STYLE)
            return l

        # Symbol search
        h.addWidget(_lbl("Symbol:"))
        self._sym_search = QLineEdit()
        self._sym_search.setPlaceholderText("BTC, ETH…")
        self._sym_search.setFixedWidth(110)
        self._sym_search.setStyleSheet(_EDIT_STYLE)
        self._sym_search.textChanged.connect(self._apply_filters)
        h.addWidget(self._sym_search)

        # Side filter
        h.addWidget(_lbl("Side:"))
        self._side_cb = QComboBox()
        self._side_cb.addItems(["All", "Buy", "Sell"])
        self._side_cb.setFixedWidth(80)
        self._side_cb.setStyleSheet(_COMBO_STYLE)
        self._side_cb.currentTextChanged.connect(self._apply_filters)
        h.addWidget(self._side_cb)

        # Regime filter
        h.addWidget(_lbl("Regime:"))
        self._regime_cb = QComboBox()
        self._regime_cb.addItems([
            "All", "Bull Trend", "Bear Trend", "Ranging",
            "Vol Expansion", "Vol Compress", "Uncertain",
        ])
        self._regime_cb.setFixedWidth(120)
        self._regime_cb.setStyleSheet(_COMBO_STYLE)
        self._regime_cb.currentTextChanged.connect(self._apply_filters)
        h.addWidget(self._regime_cb)

        # Exit reason filter
        h.addWidget(_lbl("Exit:"))
        self._exit_cb = QComboBox()
        self._exit_cb.addItems(["All", "Take Profit", "Stop Loss", "Manual"])
        self._exit_cb.setFixedWidth(110)
        self._exit_cb.setStyleSheet(_COMBO_STYLE)
        self._exit_cb.currentTextChanged.connect(self._apply_filters)
        h.addWidget(self._exit_cb)

        # Min score filter
        h.addWidget(_lbl("Min Score:"))
        self._score_cb = QComboBox()
        self._score_cb.addItems(["Any", "≥ 0.55", "≥ 0.65", "≥ 0.75", "≥ 0.85"])
        self._score_cb.setFixedWidth(90)
        self._score_cb.setStyleSheet(_COMBO_STYLE)
        self._score_cb.currentTextChanged.connect(self._apply_filters)
        h.addWidget(self._score_cb)

        h.addStretch()

        # Clear filters button
        clr = QPushButton("✕ Clear")
        clr.setFixedHeight(26)
        clr.setStyleSheet(
            "QPushButton{background:#0D1320;color:#8899AA;border:1px solid #2A3A52;"
            "border-radius:4px;font-size:13px;padding:0 10px;}"
            "QPushButton:hover{color:#E8EBF0;}"
        )
        clr.clicked.connect(self._clear_filters)
        h.addWidget(clr)

        return bar

    # ── summary strip ────────────────────────────────────────
    def _build_summary_strip(self) -> QFrame:
        frame = QFrame()
        frame.setObjectName("card")
        frame.setStyleSheet(_CARD_STYLE)
        frame.setMinimumHeight(78)
        h = QHBoxLayout(frame)
        h.setContentsMargins(8, 0, 8, 0)
        h.setSpacing(0)

        self._s_total    = _Stat("SHOWING")
        self._s_wins     = _Stat("WINS")
        self._s_losses   = _Stat("LOSSES")
        self._s_winrate  = _Stat("WIN RATE")
        self._s_pnl      = _Stat("TOTAL P&L")
        self._s_avg_dur  = _Stat("AVG DURATION")
        self._s_best     = _Stat("BEST TRADE")
        self._s_worst    = _Stat("WORST TRADE")

        for i, stat in enumerate([self._s_total, self._s_wins, self._s_losses,
                                   self._s_winrate, self._s_pnl,
                                   self._s_avg_dur, self._s_best, self._s_worst]):
            if i > 0:
                div = QFrame()
                div.setFrameShape(QFrame.VLine)
                div.setFixedWidth(1)
                div.setStyleSheet("QFrame { background: #1A2332; border: none; }")
                h.addWidget(div)
            h.addWidget(stat, 1)

        return frame

    # ── open positions tab ───────────────────────────────────
    def _build_positions_tab(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 4, 0, 0)
        v.setSpacing(0)

        self._pos_table = QTableWidget(0, len(_POS_COLS))
        self._pos_table.setHorizontalHeaderLabels(_POS_COLS)
        ph = self._pos_table.horizontalHeader()
        ph.setSectionResizeMode(0, QHeaderView.Stretch)
        for c in range(1, len(_POS_COLS)):
            ph.setSectionResizeMode(c, QHeaderView.ResizeToContents)
        ph.setMinimumSectionSize(60)
        self._pos_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._pos_table.setAlternatingRowColors(True)
        self._pos_table.setSortingEnabled(True)
        self._pos_table.verticalHeader().setVisible(False)
        self._pos_table.verticalHeader().setDefaultSectionSize(30)
        self._pos_table.setShowGrid(True)
        self._pos_table.setStyleSheet(_TABLE_STYLE)
        self._pos_table.currentCellChanged.connect(lambda row, _c, _pr, _pc: self._on_pos_selected(row))
        self._pos_table.doubleClicked.connect(self._on_table_double_click)
        v.addWidget(self._pos_table)
        return w

    # ── journal tab ──────────────────────────────────────────
    def _build_journal_tab(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 4, 0, 0)
        v.setSpacing(0)

        self._jour_table = QTableWidget(0, len(_JOUR_COLS))
        self._jour_table.setHorizontalHeaderLabels(_JOUR_COLS)
        jh = self._jour_table.horizontalHeader()
        jh.setSectionResizeMode(0, QHeaderView.Stretch)
        for c in range(1, len(_JOUR_COLS)):
            jh.setSectionResizeMode(c, QHeaderView.ResizeToContents)
        jh.setMinimumSectionSize(60)
        self._jour_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._jour_table.setAlternatingRowColors(True)
        self._jour_table.setSortingEnabled(True)
        self._jour_table.verticalHeader().setVisible(False)
        self._jour_table.verticalHeader().setDefaultSectionSize(30)
        self._jour_table.setShowGrid(True)
        self._jour_table.setStyleSheet(_TABLE_STYLE)
        self._jour_table.currentCellChanged.connect(lambda row, _c, _pr, _pc: self._on_jour_selected(row))
        self._jour_table.doubleClicked.connect(self._on_table_double_click)
        v.addWidget(self._jour_table)
        return w

    # ── detail panel ─────────────────────────────────────────
    def _build_detail_panel(self) -> QFrame:
        frame = QFrame()
        frame.setObjectName("card")
        frame.setStyleSheet(_CARD_STYLE)
        frame.setMinimumHeight(80)
        v = QVBoxLayout(frame)
        v.setContentsMargins(12, 8, 12, 8)
        v.setSpacing(4)

        # Header row with label and classification badge
        hdr_row = QHBoxLayout()
        hdr_row.setSpacing(8)
        hdr = QLabel("TRADE DETAIL  /  AI RATIONALE")
        hdr.setStyleSheet(_SECT_STYLE)
        hdr_row.addWidget(hdr)
        hdr_row.addStretch()
        self._detail_badge = QLabel("")
        self._detail_badge.setStyleSheet(
            "font-size:12px;font-weight:700;padding:2px 8px;"
            "border-radius:4px;background:transparent;color:#8899AA;"
        )
        hdr_row.addWidget(self._detail_badge)
        self._detail_thesis_label = QLabel("")
        self._detail_thesis_label.setStyleSheet(
            "font-size:11px;color:#7A8BA8;margin-left:8px;"
        )
        hdr_row.addWidget(self._detail_thesis_label)
        v.addLayout(hdr_row)

        self._detail_txt = QTextEdit()
        self._detail_txt.setReadOnly(True)
        self._detail_txt.setPlaceholderText(
            "Select any row in Open Positions or Trade Journal to view IDSS details and AI analysis…"
        )
        self._detail_txt.setStyleSheet(
            "QTextEdit{background:#080C16;color:#C0D0E0;border:none;"
            "font-size:13px;}"
            "QScrollBar:vertical{width:6px;background:#0A0E1A;}"
            "QScrollBar::handle:vertical{background:#2A3A52;border-radius:3px;}"
        )
        v.addWidget(self._detail_txt, 1)
        return frame

    # ── EventBus wiring ─────────────────────────────────────
    def _subscribe(self):
        bus.subscribe(Topics.TRADE_OPENED,     self._on_trade_event)
        bus.subscribe(Topics.TRADE_CLOSED,     self._on_trade_event)
        bus.subscribe(Topics.POSITION_UPDATED, self._on_position_updated)
        bus.subscribe(Topics.ACCOUNT_RESET,    self._on_trade_event)

    @Slot(object)
    def _on_trade_event(self, _event):
        self._full_refresh()

    @Slot(object)
    def _on_position_updated(self, _event):
        self._refresh_positions()

    # ── data refresh ────────────────────────────────────────
    def _full_refresh(self):
        try:
            from core.execution.order_router import order_router
            _pe = order_router.active_executor
            self._all_trades = list(reversed(_pe._closed_trades))
            self._refresh_positions_from(_pe)
            self._apply_filters()
        except Exception as exc:
            logger.debug("OrdersPositionsPage refresh: %s", exc)

    def _refresh_positions(self):
        try:
            from core.execution.order_router import order_router
            _pe = order_router.active_executor
            self._refresh_positions_from(_pe)
        except Exception as exc:
            logger.debug("OrdersPositionsPage positions: %s", exc)

    def _refresh_positions_from(self, pe) -> None:
        positions = pe.get_open_positions()
        n = len(positions)
        self._tabs.setTabText(0, f"  ◉  Open Positions  ({n})  ")

        self._pos_table.setSortingEnabled(False)
        self._pos_table.setRowCount(n)

        for ri, p in enumerate(positions):
            side       = p["side"].upper()
            side_label = "LONG" if side == "BUY" else "SHORT"
            side_c     = "#00CC77" if side == "BUY" else "#FF3355"
            upnl       = p.get("unrealized_pnl", 0.0)
            upnl_c     = "#00CC77" if upnl >= 0 else "#FF3355"
            regime     = p.get("regime", "")
            reg_label  = REGIME_LABELS.get(regime, "—")
            reg_color  = REGIME_COLORS.get(regime, "#8899AA")
            score      = p.get("score", 0.0)
            models     = p.get("models_fired", [])

            self._pos_table.setItem(ri, 0,  _ci(p["symbol"], "#E8EBF0", Qt.AlignLeft | Qt.AlignVCenter))
            self._pos_table.setItem(ri, 1,  _ci(side_label, side_c))
            self._pos_table.setItem(ri, 2,  _ci(reg_label, reg_color))
            self._pos_table.setItem(ri, 3,  _ci(p.get("timeframe", "—"), "#C8D0E0"))
            self._pos_table.setItem(ri, 4,  _ci(_fmt_price(p["entry_price"]), "#C8D0E0"))
            self._pos_table.setItem(ri, 5,  _ci(_fmt_price(p["current_price"]), "#E8EBF0"))
            self._pos_table.setItem(ri, 6,  _ni(upnl, f"{upnl:+.3f}%", upnl_c))
            self._pos_table.setItem(ri, 7,  _ci(_fmt_price(p["stop_loss"]),  "#FF3355"))
            self._pos_table.setItem(ri, 8,  _ci(_fmt_price(p["take_profit"]), "#00CC77"))
            self._pos_table.setItem(ri, 9,  _ni(score, f"{score:.3f}", _score_color(score)))
            self._pos_table.setItem(ri, 10, _ci(_models_text(models), "#C8D0E0"))
            self._pos_table.setItem(ri, 11, _ci(_fmt_age(p["opened_at"]), "#8899AA"))

        self._pos_table.setSortingEnabled(True)

        if n > 0 and (self._age_timer is None or not self._age_timer.isActive()):
            self._age_timer = QTimer(self)
            self._age_timer.setInterval(30_000)
            self._age_timer.timeout.connect(self._refresh_positions)
            self._age_timer.start()
        elif n == 0 and self._age_timer:
            self._age_timer.stop()

    def _apply_filters(self):
        """Filter self._all_trades and repopulate journal + summary."""
        sym_q    = self._sym_search.text().strip().upper()
        side_q   = self._side_cb.currentText()
        regime_q = self._regime_cb.currentText()
        exit_q   = self._exit_cb.currentText()
        score_q  = self._score_cb.currentText()

        # Map combo display values back to raw dict values
        regime_key_map = {
            "Bull Trend":   "bull_trend",
            "Bear Trend":   "bear_trend",
            "Ranging":      "ranging",
            "Vol Expansion":"volatility_expansion",
            "Vol Compress": "volatility_compression",
            "Uncertain":    "uncertain",
        }
        exit_key_map = {
            "Take Profit": "take_profit",
            "Stop Loss":   "stop_loss",
            "Manual":      "manual_close",
        }
        score_min_map = {
            "≥ 0.55": 0.55, "≥ 0.65": 0.65,
            "≥ 0.75": 0.75, "≥ 0.85": 0.85,
        }

        trades = self._all_trades

        if sym_q:
            trades = [t for t in trades if sym_q in t["symbol"].upper()]
        if side_q != "All":
            trades = [t for t in trades if t["side"].lower() == side_q.lower()]
        if regime_q != "All":
            rk = regime_key_map.get(regime_q, "")
            trades = [t for t in trades if t.get("regime", "") == rk]
        if exit_q != "All":
            ek = exit_key_map.get(exit_q, "")
            trades = [t for t in trades if t.get("exit_reason", "") == ek]
        if score_q != "Any":
            min_s = score_min_map.get(score_q, 0.0)
            trades = [t for t in trades if t.get("score", 0.0) >= min_s]

        self._populate_journal(trades)
        self._update_summary(trades)

    def _populate_journal(self, trades: list[dict]) -> None:
        n = len(trades)
        self._tabs.setTabText(1, f"  ≡  Trade Journal  ({n})  ")

        self._jour_table.setSortingEnabled(False)
        self._jour_table.setRowCount(n)

        for ri, t in enumerate(trades):
            side      = t["side"].upper()
            side_label = "LONG" if side == "BUY" else "SHORT"
            side_c    = "#00CC77" if side == "BUY" else "#FF3355"
            pnl_pct   = t.get("pnl_pct", 0.0)
            pnl_usdt  = t.get("pnl_usdt", 0.0)
            pnl_c     = "#00CC77" if pnl_pct >= 0 else "#FF3355"
            regime    = t.get("regime", "")
            reg_label = REGIME_LABELS.get(regime, "—")
            reg_color = REGIME_COLORS.get(regime, "#8899AA")
            score     = t.get("score", 0.0)
            models    = t.get("models_fired", [])
            dur_s     = t.get("duration_s", 0)
            reason, reason_c = EXIT_LABELS.get(
                t.get("exit_reason", ""), (t.get("exit_reason", "—"), "#8899AA")
            )
            try:
                closed_dt  = datetime.fromisoformat(t["closed_at"])
                closed_str = closed_dt.strftime("%m/%d  %H:%M")
            except Exception:
                closed_str = "—"

            self._jour_table.setItem(ri, 0,  _ci(t["symbol"], "#E8EBF0", Qt.AlignLeft | Qt.AlignVCenter))
            self._jour_table.setItem(ri, 1,  _ci(side_label, side_c))
            self._jour_table.setItem(ri, 2,  _ci(reg_label, reg_color))
            self._jour_table.setItem(ri, 3,  _ci(t.get("timeframe", "—"), "#C8D0E0"))
            self._jour_table.setItem(ri, 4,  _ci(_fmt_price(t["entry_price"]), "#C8D0E0"))
            self._jour_table.setItem(ri, 5,  _ci(_fmt_price(t["exit_price"]),  "#E8EBF0"))
            self._jour_table.setItem(ri, 6,  _ni(pnl_pct,  f"{pnl_pct:+.3f}%",  pnl_c))
            self._jour_table.setItem(ri, 7,  _ni(pnl_usdt, f"{'+'if pnl_usdt>=0 else'-'}${abs(pnl_usdt):.2f}", pnl_c))
            self._jour_table.setItem(ri, 8,  _ci(reason,   reason_c))
            self._jour_table.setItem(ri, 9,  _ni(score,    f"{score:.3f}", _score_color(score)))
            self._jour_table.setItem(ri, 10, _ci(_models_text(models), "#C8D0E0"))
            self._jour_table.setItem(ri, 11, _ni(dur_s, _fmt_dur(dur_s), "#C8D0E0"))
            self._jour_table.setItem(ri, 12, _ci(closed_str, "#8899AA"))

            # Store full dict for detail panel
            self._jour_table.item(ri, 0).setData(Qt.UserRole + 1, t)

        self._jour_table.setSortingEnabled(True)

    def _update_summary(self, trades: list[dict]) -> None:
        n = len(trades)
        if n == 0:
            for s in [self._s_total, self._s_wins, self._s_losses,
                      self._s_winrate, self._s_pnl, self._s_avg_dur,
                      self._s_best, self._s_worst]:
                s.set("—")
            self._s_total.set("0")
            return

        wins      = sum(1 for t in trades if t.get("pnl_pct", 0) > 0)
        losses    = n - wins
        win_rate  = wins / n * 100
        total_pnl = sum(t.get("pnl_usdt", 0) for t in trades)
        pnl_list  = [t.get("pnl_pct", 0) for t in trades]
        best      = max(pnl_list)
        worst     = min(pnl_list)
        avg_dur   = sum(t.get("duration_s", 0) for t in trades) // max(n, 1)

        wr_c  = "#00CC77" if win_rate >= 50 else "#FF3355"
        pnl_c = "#00CC77" if total_pnl >= 0 else "#FF3355"

        self._s_total.set(str(n))
        self._s_wins.set(str(wins), "#00CC77")
        self._s_losses.set(str(losses), "#FF3355")
        self._s_winrate.set(f"{win_rate:.1f}%", wr_c)
        self._s_pnl.set(f"{'+'if total_pnl>=0 else'-'}${abs(total_pnl):.2f}", pnl_c)
        self._s_avg_dur.set(_fmt_dur(avg_dur))
        self._s_best.set(f"{best:+.2f}%", "#00CC77")
        self._s_worst.set(f"{worst:+.2f}%", "#FF3355")

    # ── row selection → detail panel ────────────────────────
    def _on_pos_selected(self, row: int):
        try:
            from core.execution.order_router import order_router
            _pe = order_router.active_executor
            positions = _pe.get_open_positions()
            if 0 <= row < len(positions):
                self._show_position_detail(positions[row])
        except Exception:
            pass

    def _on_jour_selected(self, row: int):
        item = self._jour_table.item(row, 0)
        if item:
            trade = item.data(Qt.UserRole + 1)
            if trade:
                self._show_trade_detail(trade)

    def _show_position_detail(self, p: dict) -> None:
        regime    = REGIME_LABELS.get(p.get("regime", ""), "—")
        models    = _models_text(p.get("models_fired", []))
        upnl      = p.get("unrealized_pnl", 0.0)
        age       = _fmt_age(p.get("opened_at", ""))

        lines = [
            f"▸  {p['symbol']}  •  {'LONG' if p['side'].upper()=='BUY' else 'SHORT'}  •  {regime}  •  TF: {p.get('timeframe','—')}",
            f"   Score: {p.get('score', 0):.3f}   |   Models: {models}   |   Age: {age}",
            f"   Entry: {_fmt_price(p['entry_price'])}   "
            f"Mark: {_fmt_price(p['current_price'])}   "
            f"Unreal. P&L: {upnl:+.3f}%",
            f"   Stop: {_fmt_price(p['stop_loss'])}   "
            f"Target: {_fmt_price(p['take_profit'])}   "
            f"Size: ${p.get('size_usdt', 0):.0f}",
            "",
        ]
        rationale = p.get("rationale", "No rationale stored.")
        if rationale:
            lines.append(rationale)

        # Phase 2: Full analyst review using canonical renderer
        try:
            from core.analysis.trade_analysis_service import trade_analysis_service
            from core.analysis.canonical_renderer import render_for_channel, MODE_UI_OPEN

            analysis = trade_analysis_service.build_open_trade_analysis(p)
            rendered = render_for_channel(analysis, mode=MODE_UI_OPEN, trade=p)

            # Update badge
            cls_ = analysis.get("classification", "NEUTRAL")
            thesis = (analysis.get("thesis") or {})
            thesis_status = thesis.get("thesis_status", "")
            badge_color = {"GOOD": "#00CC77", "BAD": "#FF3355", "NEUTRAL": "#FFB300"}.get(cls_, "#8899AA")
            emoji = analysis.get("classification_emoji", "⚖️")
            badge_text = f"{emoji} {cls_}"
            if thesis_status:
                badge_text += f"  [{thesis_status.upper()}]"
            self._detail_badge.setText(badge_text)
            self._detail_badge.setStyleSheet(
                f"color: {badge_color}; font-weight: bold; font-size: 13px;"
            )

            # Update thesis label
            if thesis_status:
                self._detail_thesis_label.setText(f"Thesis: {thesis_status}")

            # Append analysis lines
            for line in rendered.get("text_lines", []):
                lines.append(line)
        except Exception as exc:
            import logging
            logging.getLogger(__name__).debug("open position analysis failed: %s", exc)

        self._detail_txt.setPlainText("\n".join(lines))

    def _show_trade_detail(self, t: dict) -> None:
        regime   = REGIME_LABELS.get(t.get("regime", ""), "—")
        models   = _models_text(t.get("models_fired", []))
        reason, _ = EXIT_LABELS.get(t.get("exit_reason", ""), (t.get("exit_reason", "—"), ""))
        dur      = _fmt_dur(t.get("duration_s", 0))
        pnl_pct  = t.get("pnl_pct", 0.0)
        pnl_usd  = t.get("pnl_usdt", 0.0)

        try:
            closed_dt  = datetime.fromisoformat(t["closed_at"])
            closed_str = closed_dt.strftime("%Y-%m-%d  %H:%M:%S UTC")
        except Exception:
            closed_str = "—"

        lines = [
            f"▸  {t['symbol']}  •  {'LONG' if t['side'].upper()=='BUY' else 'SHORT'}  •  {regime}  •  TF: {t.get('timeframe','—')}",
            f"   Score: {t.get('score', 0):.3f}   |   Models: {models}",
            f"   Entry: {_fmt_price(t['entry_price'])}   "
            f"Exit: {_fmt_price(t['exit_price'])}   "
            f"P&L: {pnl_pct:+.3f}%  ({'+'if pnl_usd>=0 else'-'}${abs(pnl_usd):.2f})",
            f"   Stop: {_fmt_price(t.get('stop_loss', 0))}   "
            f"Target: {_fmt_price(t.get('take_profit', 0))}   "
            f"Size: ${t.get('size_usdt', 0):.0f}",
            f"   Exit Reason: {reason}   |   Duration: {dur}   |   Closed: {closed_str}",
            "",
        ]
        rationale = t.get("rationale", "No rationale stored.")
        if rationale:
            lines.append(rationale)

        # Phase 2: Full analyst review using canonical renderer
        try:
            from core.analysis.trade_analysis_service import trade_analysis_service
            from core.analysis.canonical_renderer import render_for_channel, MODE_UI_CLOSED

            # Try loading persisted analysis first, then build if not found
            stored = trade_analysis_service.load_analysis(t)
            if stored:
                analysis = stored
            else:
                analysis = trade_analysis_service.build_closed_trade_analysis(t)

            rendered = render_for_channel(analysis, mode=MODE_UI_CLOSED, trade=t)

            # Update badge
            cls_ = analysis.get("classification", "NEUTRAL")
            emoji = analysis.get("classification_emoji", "⚖️")
            badge_color = {"GOOD": "#00CC77", "BAD": "#FF3355", "NEUTRAL": "#FFB300"}.get(cls_, "#8899AA")
            badge_text = f"{emoji} {cls_}  {analysis.get('overall_score', 0):.0f}/100"
            self._detail_badge.setText(badge_text)
            self._detail_badge.setStyleSheet(
                f"color: {badge_color}; font-weight: bold; font-size: 13px;"
            )

            # Append all rendered lines
            for line in rendered.get("text_lines", []):
                lines.append(line)
        except Exception as exc:
            import logging
            logging.getLogger(__name__).debug("closed trade analysis failed: %s", exc)

        self._detail_txt.setPlainText("\n".join(lines))

    # ── filter controls ─────────────────────────────────────
    def _clear_filters(self):
        self._sym_search.blockSignals(True)
        self._side_cb.blockSignals(True)
        self._regime_cb.blockSignals(True)
        self._exit_cb.blockSignals(True)
        self._score_cb.blockSignals(True)

        self._sym_search.clear()
        self._side_cb.setCurrentIndex(0)
        self._regime_cb.setCurrentIndex(0)
        self._exit_cb.setCurrentIndex(0)
        self._score_cb.setCurrentIndex(0)

        self._sym_search.blockSignals(False)
        self._side_cb.blockSignals(False)
        self._regime_cb.blockSignals(False)
        self._exit_cb.blockSignals(False)
        self._score_cb.blockSignals(False)

        self._apply_filters()

    # ── double-click → chart ────────────────────────────────
    def _on_table_double_click(self, index):
        table = self.sender()
        item  = table.item(index.row(), 0)
        if not item:
            return
        symbol = item.text()
        try:
            main = self.window()
            if hasattr(main, "_pages") and "chart_workspace" in main._pages:
                main._pages["chart_workspace"]._symbol_combo.setCurrentText(symbol)
                main._navigate_to("chart_workspace")
        except Exception as exc:
            logger.debug("OrdersPositionsPage chart nav: %s", exc)
