# ============================================================
# NEXUS TRADER — Paper Trading Page
#
# Surfaces the PaperExecutor state in real time:
#   • Account stats bar   — capital, P&L, win rate, drawdown
#   • Open Positions table — live mark-to-market with stop/TP
#   • Trade History table  — closed trades with full metadata
#   • Manual controls      — close individual / close all / reset
# ============================================================
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QFrame,
    QTableWidget, QTableWidgetItem, QHeaderView,
    QAbstractItemView, QPushButton, QSplitter,
    QMessageBox, QMenu,
)
from PySide6.QtCore import Qt, QTimer, Slot
from PySide6.QtGui import QColor, QAction

from gui.main_window import PageHeader
from core.event_bus import bus, Topics

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────
# Shared styles
# ─────────────────────────────────────────────────────────────
_TABLE_STYLE = (
    "QTableWidget { background:#0A0E1A; color:#E8EBF0; "
    "gridline-color:#141E2E; font-size:13px; border:none; }"
    "QTableWidget::item:selected { background:#1A2D4A; }"
    "QTableWidget::item:alternate { background:#0C1018; }"
    "QHeaderView::section { background:#0D1320; color:#8899AA; "
    "padding:6px 8px; border:none; "
    "border-bottom:1px solid #1A2332; font-size:13px; font-weight:600; }"
)
_CARD_STYLE = (
    "QFrame#card { background:#0D1320; border:1px solid #1A2332; border-radius:6px; }"
)
_BTN_DANGER = (
    "QPushButton { background:#1A0A0A; color:#FF3355; border:1px solid #440011; "
    "border-radius:5px; font-size:13px; font-weight:700; padding:0 14px; }"
    "QPushButton:hover { background:#2A1010; }"
    "QPushButton:disabled { color:#3A1A1A; border-color:#1A0A0A; }"
)
_BTN_NEUTRAL = (
    "QPushButton { background:#0D1320; color:#8899AA; border:1px solid #2A3A52; "
    "border-radius:5px; font-size:13px; font-weight:600; padding:0 14px; }"
    "QPushButton:hover { background:#1A2332; color:#E8EBF0; }"
)
_SECT_STYLE = "color:#8899AA; font-size:13px; font-weight:600;"


# ─────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────
def _ci(text: str, color: str = "#E8EBF0",
        align: Qt.AlignmentFlag = Qt.AlignCenter) -> QTableWidgetItem:
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
    if v == 0:
        return "—"
    if v >= 1_000:
        return f"{v:,.2f}"
    if v >= 1:
        return f"{v:.4f}"
    return f"{v:.6f}"


def _fmt_duration(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60}s"
    if seconds < 86400:
        h = seconds // 3600
        m = (seconds % 3600) // 60
        return f"{h}h {m}m"
    d = seconds // 86400
    h = (seconds % 86400) // 3600
    return f"{d}d {h}h"


def _fmt_age(opened_at_iso: str) -> str:
    try:
        dt = datetime.fromisoformat(opened_at_iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        delta = int((datetime.now(timezone.utc) - dt).total_seconds())
        return _fmt_duration(delta)
    except Exception:
        return "—"


def _exit_reason_label(reason: str) -> tuple[str, str]:
    """Returns (display text, color) for an exit reason."""
    labels = {
        "stop_loss":     ("Stop Loss",     "#FF3355"),
        "take_profit":   ("Take Profit",   "#00CC77"),
        "manual_close":  ("Manual",        "#8899AA"),
        "partial_close": ("Partial Close", "#FFB300"),
        "time_exit":     ("Time Exit",     "#8899AA"),
    }
    return labels.get(reason, (reason.replace("_", " ").title(), "#8899AA"))


# ─────────────────────────────────────────────────────────────
# Mini stat label (for the account stats bar)
# ─────────────────────────────────────────────────────────────
class StatLabel(QWidget):
    def __init__(self, title: str, value: str = "—", value_color: str = "#E8EBF0"):
        super().__init__()
        v = QVBoxLayout(self)
        v.setContentsMargins(16, 10, 16, 10)
        v.setSpacing(4)

        t = QLabel(title)
        t.setStyleSheet(_SECT_STYLE)
        t.setWordWrap(False)        # never wrap — elide instead
        t.setSizePolicy(
            t.sizePolicy().horizontalPolicy(),
            t.sizePolicy().verticalPolicy(),
        )

        self._val = QLabel(value)
        self._val.setStyleSheet(
            f"font-size:16px; font-weight:700; color:{value_color};"
        )
        v.addWidget(t)
        v.addWidget(self._val)

    def set(self, text: str, color: str = "#E8EBF0"):
        self._val.setText(text)
        self._val.setStyleSheet(
            f"font-size:16px; font-weight:700; color:{color};"
        )


# ─────────────────────────────────────────────────────────────
# Paper Trading Page
# ─────────────────────────────────────────────────────────────
_POS_COLS   = ["Symbol", "Side", "Entry", "Mark", "Unreal. P&L",
               "Stop", "Target", "Size", "Score", "Age"]
_HIST_COLS  = ["Symbol", "Side", "Entry", "Exit",
               "Entry Size", "Exit Size",
               "P&L %", "P&L $",
               "Exit Reason", "Duration", "Score", "Opened At", "Closed At"]


class PaperTradingPage(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._age_timer: Optional[QTimer] = None
        self._build()
        self._subscribe()
        # Defer first refresh so executor singleton is fully initialised
        QTimer.singleShot(300, self._full_refresh)

    # ── layout ─────────────────────────────────────────────
    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        root.addWidget(PageHeader(
            "Paper Trading",
            "Simulated live trading powered by the IDSS AI scanner"
        ))

        body = QWidget()
        bv = QVBoxLayout(body)
        bv.setContentsMargins(16, 12, 16, 12)
        bv.setSpacing(8)

        # ── Account stats bar ────────────────────────────────
        bv.addWidget(self._build_stats_bar())

        # ── Splitter: positions (top) + history (bottom) ─────
        splitter = QSplitter(Qt.Vertical)
        splitter.setHandleWidth(5)
        splitter.setStyleSheet(
            "QSplitter::handle{background:#1A2332;}"
            "QSplitter::handle:hover{background:#2A3A52;}"
        )

        splitter.addWidget(self._build_positions_panel())
        splitter.addWidget(self._build_history_panel())
        splitter.setStretchFactor(0, 2)
        splitter.setStretchFactor(1, 3)

        bv.addWidget(splitter, 1)
        root.addWidget(body, 1)

    # ── stats bar ───────────────────────────────────────────
    def _build_stats_bar(self) -> QFrame:
        bar = QFrame()
        bar.setObjectName("card")
        bar.setStyleSheet(_CARD_STYLE)
        bar.setFixedHeight(84)
        h = QHBoxLayout(bar)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(0)

        self._stat_capital   = StatLabel("PORTFOLIO VALUE", "—")
        self._stat_cash      = StatLabel("AVAILABLE CASH",  "—")
        self._stat_pnl       = StatLabel("TODAY'S P&L",    "+$0.00", "#00CC77")
        self._stat_winrate   = StatLabel("WIN RATE",        "—")
        self._stat_trades    = StatLabel("TOTAL TRADES",    "0")
        self._stat_drawdown  = StatLabel("DRAWDOWN",        "0.00%", "#00CC77")

        for i, stat in enumerate([self._stat_capital, self._stat_cash, self._stat_pnl,
                                   self._stat_winrate, self._stat_trades, self._stat_drawdown]):
            if i > 0:
                div = QFrame()
                div.setFrameShape(QFrame.VLine)
                div.setStyleSheet("QFrame{color:#1A2332;}")
                h.addWidget(div)
            h.addWidget(stat, 1)

        return bar

    # ── open positions panel ────────────────────────────────
    def _build_positions_panel(self) -> QFrame:
        frame = QFrame()
        frame.setObjectName("card")
        frame.setStyleSheet(_CARD_STYLE)
        fv = QVBoxLayout(frame)
        fv.setContentsMargins(12, 10, 12, 10)
        fv.setSpacing(6)

        # Header row
        hdr = QHBoxLayout()
        self._pos_hdr_lbl = QLabel("OPEN POSITIONS")
        self._pos_hdr_lbl.setStyleSheet(_SECT_STYLE)
        hdr.addWidget(self._pos_hdr_lbl)
        hdr.addStretch()

        # 🧪 Test Position button removed for production demo.
        # Handler _on_open_test_position() is kept in code for future testing.

        self._close_all_btn = QPushButton("✕  Close All")
        self._close_all_btn.setFixedHeight(28)
        self._close_all_btn.setStyleSheet(_BTN_DANGER)
        self._close_all_btn.clicked.connect(self._on_close_all)
        hdr.addWidget(self._close_all_btn)

        fv.addLayout(hdr)

        # Positions table
        self._pos_table = QTableWidget(0, len(_POS_COLS))
        self._pos_table.setHorizontalHeaderLabels(_POS_COLS)
        ph = self._pos_table.horizontalHeader()
        ph.setSectionResizeMode(0, QHeaderView.Stretch)
        for c in range(1, len(_POS_COLS)):
            ph.setSectionResizeMode(c, QHeaderView.ResizeToContents)
        ph.setMinimumSectionSize(65)
        self._pos_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._pos_table.setAlternatingRowColors(True)
        self._pos_table.setSortingEnabled(True)
        self._pos_table.verticalHeader().setVisible(False)
        self._pos_table.setShowGrid(True)
        self._pos_table.setStyleSheet(_TABLE_STYLE)
        self._pos_table.setContextMenuPolicy(Qt.CustomContextMenu)
        self._pos_table.customContextMenuRequested.connect(self._pos_context_menu)
        self._pos_table.doubleClicked.connect(self._on_pos_double_click)
        fv.addWidget(self._pos_table, 1)

        return frame

    # ── trade history panel ─────────────────────────────────
    def _build_history_panel(self) -> QFrame:
        frame = QFrame()
        frame.setObjectName("card")
        frame.setStyleSheet(_CARD_STYLE)
        fv = QVBoxLayout(frame)
        fv.setContentsMargins(12, 10, 12, 10)
        fv.setSpacing(6)

        # Header row
        hdr = QHBoxLayout()
        self._hist_hdr_lbl = QLabel("TRADE HISTORY")
        self._hist_hdr_lbl.setStyleSheet(_SECT_STYLE)
        hdr.addWidget(self._hist_hdr_lbl)
        hdr.addStretch()

        self._reset_btn = QPushButton("↺  Reset Account")
        self._reset_btn.setFixedHeight(28)
        self._reset_btn.setStyleSheet(_BTN_NEUTRAL)
        self._reset_btn.clicked.connect(self._on_reset)
        hdr.addWidget(self._reset_btn)

        fv.addLayout(hdr)

        # History table
        self._hist_table = QTableWidget(0, len(_HIST_COLS))
        self._hist_table.setHorizontalHeaderLabels(_HIST_COLS)
        hh = self._hist_table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.Stretch)
        for c in range(1, len(_HIST_COLS)):
            hh.setSectionResizeMode(c, QHeaderView.ResizeToContents)
        hh.setMinimumSectionSize(65)
        self._hist_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._hist_table.setAlternatingRowColors(True)
        self._hist_table.setSortingEnabled(True)
        self._hist_table.verticalHeader().setVisible(False)
        self._hist_table.setShowGrid(True)
        self._hist_table.setStyleSheet(_TABLE_STYLE)
        self._hist_table.doubleClicked.connect(self._on_hist_double_click)
        fv.addWidget(self._hist_table, 1)

        return frame

    # ── EventBus wiring ─────────────────────────────────────
    def _subscribe(self):
        bus.subscribe(Topics.TRADE_OPENED,     self._on_trade_opened)
        bus.subscribe(Topics.TRADE_CLOSED,     self._on_trade_closed)
        bus.subscribe(Topics.POSITION_UPDATED, self._on_position_updated)

    # ── EventBus handlers ───────────────────────────────────
    @Slot(object)
    def _on_trade_opened(self, event):
        self._full_refresh()

    @Slot(object)
    def _on_trade_closed(self, event):
        self._full_refresh()

    @Slot(object)
    def _on_position_updated(self, event):
        """Lightweight: only refresh positions table on mark updates."""
        self._refresh_positions()

    # ── data refresh ────────────────────────────────────────
    def _full_refresh(self):
        """Refresh everything from the executor singleton."""
        try:
            from core.execution.paper_executor import paper_executor as _pe
            self._refresh_stats(_pe)
            self._refresh_positions_from(_pe)
            self._refresh_history_from(_pe)
        except Exception as exc:
            logger.debug("PaperTradingPage full refresh: %s", exc)

    def _refresh_positions(self):
        try:
            from core.execution.paper_executor import paper_executor as _pe
            self._refresh_positions_from(_pe)
            self._refresh_stats(_pe)
        except Exception as exc:
            logger.debug("PaperTradingPage positions refresh: %s", exc)

    def _refresh_stats(self, pe) -> None:
        stats = pe.get_stats()

        # Portfolio value
        used = sum(
            p.get("size_usdt", 0) * (1 + p.get("unrealized_pnl", 0) / 100)
            for p in pe.get_open_positions()
        )
        total = pe.available_capital + used
        self._stat_capital.set(f"${total:,.2f}")
        self._stat_cash.set(f"${pe.available_capital:,.2f}")

        # Drawdown
        dd = stats["drawdown_pct"]
        dd_color = "#FF3355" if dd > 10 else ("#FFB300" if dd > 3 else "#00CC77")
        self._stat_drawdown.set(f"{dd:.2f}%", dd_color)

        # Win rate
        n = stats["total_trades"]
        if n:
            wr = stats["win_rate"]
            wr_color = "#00CC77" if wr >= 50 else "#FF3355"
            self._stat_winrate.set(f"{wr:.1f}%", wr_color)
        else:
            self._stat_winrate.set("—")

        self._stat_trades.set(str(n))

        # Today's P&L
        today = datetime.now(timezone.utc).date().isoformat()
        today_pnl = sum(
            t.get("pnl_usdt", 0)
            for t in pe._closed_trades
            if t.get("closed_at", "")[:10] == today
        )
        pnl_color = "#00CC77" if today_pnl >= 0 else "#FF3355"
        self._stat_pnl.set(
            f"{'+' if today_pnl >= 0 else ''}${today_pnl:.2f}", pnl_color
        )

    def _refresh_positions_from(self, pe) -> None:
        positions = pe.get_open_positions()
        n = len(positions)
        try:
            from config.settings import settings as _s
            max_pos = int(_s.get("risk.max_concurrent_positions", 5))
        except Exception:
            max_pos = 5
        self._pos_hdr_lbl.setText(
            f"OPEN POSITIONS  ({n}  /  {max_pos} max)"
        )
        self._close_all_btn.setEnabled(n > 0)

        self._pos_table.setSortingEnabled(False)
        self._pos_table.setRowCount(n)

        for ri, p in enumerate(positions):
            side       = p["side"].upper()
            side_label = "LONG" if side == "BUY" else "SHORT"
            side_color = "#00CC77" if side == "BUY" else "#FF3355"
            upnl       = p.get("unrealized_pnl", 0.0)
            upnl_color = "#00CC77" if upnl >= 0 else "#FF3355"
            score      = p.get("score", 0.0)

            self._pos_table.setItem(ri, 0, _ci(
                p["symbol"], "#E8EBF0", Qt.AlignLeft | Qt.AlignVCenter
            ))
            self._pos_table.setItem(ri, 1, _ci(side_label, side_color))
            self._pos_table.setItem(ri, 2, _ci(_fmt_price(p["entry_price"]), "#8899AA"))
            self._pos_table.setItem(ri, 3, _ci(_fmt_price(p["current_price"]), "#E8EBF0"))
            self._pos_table.setItem(ri, 4, _ni(
                upnl, f"{upnl:+.3f}%", upnl_color
            ))
            self._pos_table.setItem(ri, 5, _ci(_fmt_price(p["stop_loss"]),   "#FF3355"))
            self._pos_table.setItem(ri, 6, _ci(_fmt_price(p["take_profit"]), "#00CC77"))
            self._pos_table.setItem(ri, 7, _ci(f"${p['size_usdt']:.0f}", "#8899AA"))
            self._pos_table.setItem(ri, 8, _ni(score, f"{score:.3f}", "#FFB300"))
            self._pos_table.setItem(ri, 9, _ci(
                _fmt_age(p["opened_at"]), "#4A6A8A"
            ))

        self._pos_table.setSortingEnabled(True)

        # Kick off age-timer if we have open positions
        if n > 0 and (self._age_timer is None or not self._age_timer.isActive()):
            self._age_timer = QTimer(self)
            self._age_timer.setInterval(30_000)   # refresh age every 30 s
            self._age_timer.timeout.connect(self._refresh_positions)
            self._age_timer.start()
        elif n == 0 and self._age_timer:
            self._age_timer.stop()

    def _refresh_history_from(self, pe) -> None:
        trades = list(reversed(pe._closed_trades))   # most recent first
        n = len(trades)
        self._hist_hdr_lbl.setText(f"TRADE HISTORY  ({n})")

        self._hist_table.setSortingEnabled(False)
        self._hist_table.setRowCount(n)

        for ri, t in enumerate(trades):
            side       = t["side"].upper()
            side_label = "LONG" if side == "BUY" else "SHORT"
            side_color = "#00CC77" if side == "BUY" else "#FF3355"
            pnl_pct    = t.get("pnl_pct", 0.0)
            pnl_usdt   = t.get("pnl_usdt", 0.0)
            pnl_color  = "#00CC77" if pnl_pct >= 0 else "#FF3355"
            reason_lbl, reason_color = _exit_reason_label(t.get("exit_reason", ""))
            duration_s = t.get("duration_s", 0)
            score      = t.get("score", 0.0)

            # ── Position sizing transparency (Session 30) ─────────────────
            # entry_size_usdt: original USDT deployed when the position was opened.
            # exit_size_usdt:  USDT actually closed in this record.
            # For full closes they are equal; for partial closes they differ.
            # Both fall back to size_usdt for historical records written before
            # these fields existed (backward compatible).
            entry_sz = float(t.get("entry_size_usdt") or t.get("size_usdt") or 0.0)
            exit_sz  = float(t.get("exit_size_usdt")  or t.get("size_usdt") or 0.0)
            # Colour the exit size: partial closes (< entry) render amber as a
            # visual cue, full closes render the standard grey.
            exit_sz_color = "#FFB300" if exit_sz < entry_sz - 0.01 else "#8899AA"

            # Friendly opened-at and closed-at times (local clock)
            try:
                opened_dt  = datetime.fromisoformat(t["opened_at"])
                opened_str = opened_dt.strftime("%m/%d  %H:%M")
            except Exception:
                opened_str = "—"
            try:
                closed_dt  = datetime.fromisoformat(t["closed_at"])
                closed_str = closed_dt.strftime("%m/%d  %H:%M")
            except Exception:
                closed_str = "—"

            # col 0  Symbol
            self._hist_table.setItem(ri, 0, _ci(
                t["symbol"], "#E8EBF0", Qt.AlignLeft | Qt.AlignVCenter
            ))
            # col 1  Side
            self._hist_table.setItem(ri, 1, _ci(side_label, side_color))
            # col 2  Entry Price
            self._hist_table.setItem(ri, 2, _ci(_fmt_price(t["entry_price"]), "#8899AA"))
            # col 3  Exit Price
            self._hist_table.setItem(ri, 3, _ci(_fmt_price(t["exit_price"]),  "#E8EBF0"))
            # col 4  Entry Size (USDT) ── NEW
            self._hist_table.setItem(ri, 4, _ni(
                entry_sz, f"${entry_sz:,.2f}", "#8899AA"
            ))
            # col 5  Exit Size (USDT) ── NEW
            self._hist_table.setItem(ri, 5, _ni(
                exit_sz, f"${exit_sz:,.2f}", exit_sz_color
            ))
            # col 6  P&L %
            self._hist_table.setItem(ri, 6, _ni(
                pnl_pct, f"{pnl_pct:+.3f}%", pnl_color
            ))
            # col 7  P&L $
            self._hist_table.setItem(ri, 7, _ni(
                pnl_usdt, f"{'+'if pnl_usdt>=0 else'-'}${abs(pnl_usdt):.2f}", pnl_color
            ))
            # col 8  Exit Reason
            self._hist_table.setItem(ri, 8, _ci(reason_lbl, reason_color))
            # col 9  Duration
            self._hist_table.setItem(ri, 9, _ni(
                duration_s, _fmt_duration(duration_s), "#8899AA"
            ))
            # col 10 Score
            self._hist_table.setItem(ri, 10, _ni(score, f"{score:.3f}", "#FFB300"))
            # col 11 Opened At
            self._hist_table.setItem(ri, 11, _ci(opened_str, "#4A6A8A"))
            # col 12 Closed At
            self._hist_table.setItem(ri, 12, _ci(closed_str, "#4A6A8A"))

        self._hist_table.setSortingEnabled(True)

    # ── Context menu for positions table ────────────────────
    def _pos_context_menu(self, pos):
        row = self._pos_table.rowAt(pos.y())
        if row < 0:
            return
        sym_item = self._pos_table.item(row, 0)
        if not sym_item:
            return
        symbol = sym_item.text()

        menu = QMenu(self)
        menu.setStyleSheet(
            "QMenu{background:#0D1320;color:#E8EBF0;border:1px solid #2A3A52;}"
            "QMenu::item{padding:6px 20px;}"
            "QMenu::item:selected{background:#1A2D4A;}"
        )
        close_act  = QAction(f"✕  Close {symbol}", self)
        sl_act     = QAction(f"🛑  Adjust Stop-Loss…", self)
        tp_act     = QAction(f"🎯  Adjust Take-Profit…", self)
        chart_act  = QAction(f"⋈  Open in Chart",  self)
        close_act.triggered.connect(lambda: self._close_symbol(symbol))
        sl_act.triggered.connect(lambda: self._adjust_stop(symbol))
        tp_act.triggered.connect(lambda: self._adjust_target(symbol))
        chart_act.triggered.connect(lambda: self._open_in_chart(symbol))
        menu.addAction(close_act)
        menu.addSeparator()
        menu.addAction(sl_act)
        menu.addAction(tp_act)
        menu.addSeparator()
        menu.addAction(chart_act)
        menu.exec(self._pos_table.viewport().mapToGlobal(pos))

    # ── Button handlers ─────────────────────────────────────
    def _close_symbol(self, symbol: str):
        reply = QMessageBox.question(
            self,
            "Close Position",
            f"Close the open {symbol} position at market price?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return
        try:
            from core.execution.paper_executor import paper_executor as _pe
            _pe.close_position(symbol)
        except Exception as exc:
            logger.error("close_position %s: %s", symbol, exc)

    def _adjust_stop(self, symbol: str):
        """Prompt user for a new stop-loss price and apply it (tighten only)."""
        try:
            from core.execution.paper_executor import paper_executor as _pe
            positions = _pe.get_open_positions()
            pos = next((p for p in positions if p.get("symbol") == symbol), None)
            if not pos:
                return
            current_sl = pos.get("stop_loss", 0.0)
            mark = pos.get("current_price") or pos.get("entry_price", 0.0)
        except Exception as exc:
            logger.error("_adjust_stop lookup %s: %s", symbol, exc)
            return

        from PySide6.QtWidgets import QInputDialog
        new_sl, ok = QInputDialog.getDouble(
            self,
            f"Adjust Stop-Loss — {symbol}",
            f"Current stop: {current_sl:.4f}   Mark: {mark:.4f}\nNew stop-loss price:",
            current_sl,
            0.0001,
            999999.0,
            4,
        )
        if not ok:
            return
        try:
            from core.execution.paper_executor import paper_executor as _pe
            ok = _pe.adjust_stop(symbol, new_stop_loss=new_sl)
            if not ok:
                QMessageBox.warning(self, "Adjust Stop",
                    "Could not adjust stop — new level must be tighter than current stop.")
            else:
                logger.info("Stop-loss adjusted: %s → %.4f", symbol, new_sl)
        except Exception as exc:
            logger.error("adjust_stop %s: %s", symbol, exc)
            QMessageBox.warning(self, "Adjust Stop", f"Could not adjust stop:\n{exc}")

    def _adjust_target(self, symbol: str):
        """Prompt user for a new take-profit price and apply it."""
        try:
            from core.execution.paper_executor import paper_executor as _pe
            positions = _pe.get_open_positions()
            pos = next((p for p in positions if p.get("symbol") == symbol), None)
            if not pos:
                return
            current_tp = pos.get("take_profit", 0.0)
            mark = pos.get("current_price") or pos.get("entry_price", 0.0)
        except Exception as exc:
            logger.error("_adjust_target lookup %s: %s", symbol, exc)
            return

        from PySide6.QtWidgets import QInputDialog
        new_tp, ok = QInputDialog.getDouble(
            self,
            f"Adjust Take-Profit — {symbol}",
            f"Current target: {current_tp:.4f}   Mark: {mark:.4f}\nNew take-profit price:",
            current_tp,
            0.0001,
            999999.0,
            4,
        )
        if not ok:
            return
        try:
            from core.execution.paper_executor import paper_executor as _pe
            _pe.adjust_target(symbol, new_take_profit=new_tp)
            logger.info("Take-profit adjusted: %s → %.4f", symbol, new_tp)
        except Exception as exc:
            logger.error("adjust_target %s: %s", symbol, exc)
            QMessageBox.warning(self, "Adjust Target", f"Could not adjust target:\n{exc}")

    def _on_open_test_position(self):
        """Open a manually-specified test position for AT-12/AT-13 validation."""
        from PySide6.QtWidgets import QDialog, QFormLayout, QComboBox, QDialogButtonBox, QDoubleSpinBox
        dlg = QDialog(self)
        dlg.setWindowTitle("Open Test Position")
        dlg.setMinimumWidth(340)
        form = QFormLayout(dlg)

        sym_cb = QComboBox()
        sym_cb.addItems(["BNB/USDT", "BTC/USDT", "ETH/USDT", "SOL/USDT", "XRP/USDT"])
        side_cb = QComboBox()
        side_cb.addItems(["sell", "buy"])

        def _spin(val, lo, hi, dec=4):
            s = QDoubleSpinBox()
            s.setDecimals(dec)
            s.setRange(lo, hi)
            s.setValue(val)
            return s

        entry_sp  = _spin(656.0, 0.0001, 999999.0)
        stop_sp   = _spin(661.0, 0.0001, 999999.0)
        target_sp = _spin(649.0, 0.0001, 999999.0)
        size_sp   = _spin(100.0, 1.0, 10000.0, 2)

        form.addRow("Symbol:",      sym_cb)
        form.addRow("Side:",        side_cb)
        form.addRow("Entry Price:", entry_sp)
        form.addRow("Stop Loss:",   stop_sp)
        form.addRow("Take Profit:", target_sp)
        form.addRow("Size (USDT):", size_sp)

        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.accepted.connect(dlg.accept)
        btns.rejected.connect(dlg.reject)
        form.addRow(btns)

        if dlg.exec() != QDialog.Accepted:
            return

        symbol = sym_cb.currentText()
        side   = side_cb.currentText()
        entry  = entry_sp.value()
        stop   = stop_sp.value()
        target = target_sp.value()
        size   = size_sp.value()

        try:
            from core.meta_decision.order_candidate import OrderCandidate
            from core.execution.paper_executor import paper_executor as _pe
            from datetime import datetime
            candidate = OrderCandidate(
                symbol=symbol, side=side,
                entry_type="market", entry_price=entry,
                stop_loss_price=stop, take_profit_price=target,
                position_size_usdt=size, score=0.5,
                models_fired=["test"], regime="test",
                rationale="Manual test position", timeframe="test",
                atr_value=0.0, approved=True,
            )
            ok = _pe.submit(candidate)
            if not ok:
                QMessageBox.warning(self, "Test Position",
                    f"Could not open position — {symbol} may already be open or capital insufficient.")
            else:
                logger.info("Test position opened: %s %s @ %.4f SL=%.4f TP=%.4f",
                            side, symbol, entry, stop, target)
        except Exception as exc:
            logger.error("_on_open_test_position: %s", exc)
            QMessageBox.critical(self, "Test Position", f"Error:\n{exc}")

    def _on_close_all(self):
        try:
            from core.execution.paper_executor import paper_executor as _pe
            n = len(_pe.get_open_positions())
        except Exception:
            n = 0
        if n == 0:
            return
        reply = QMessageBox.question(
            self,
            "Close All Positions",
            f"Close all {n} open position{'s' if n > 1 else ''} at their current mark price?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return
        try:
            from core.execution.paper_executor import paper_executor as _pe
            closed = _pe.close_all()
            logger.info("PaperTradingPage: manually closed %d positions", closed)
        except Exception as exc:
            logger.error("close_all: %s", exc)

    def _on_reset(self):
        reply = QMessageBox.question(
            self,
            "Reset Paper Account",
            "This will close all open positions and clear trade history.\n"
            "The account will be reset to $100,000 USDT virtual capital.\n\n"
            "Continue?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return
        try:
            from core.execution.paper_executor import paper_executor as _pe
            _pe.reset()
            self._full_refresh()
        except Exception as exc:
            logger.error("account reset: %s", exc)

    # ── Double-click → chart ────────────────────────────────
    def _on_pos_double_click(self, index):
        sym_item = self._pos_table.item(index.row(), 0)
        if sym_item:
            self._open_in_chart(sym_item.text())

    def _on_hist_double_click(self, index):
        sym_item = self._hist_table.item(index.row(), 0)
        if sym_item:
            self._open_in_chart(sym_item.text())

    def _open_in_chart(self, symbol: str):
        try:
            main = self.window()
            if hasattr(main, "_pages") and "chart_workspace" in main._pages:
                main._pages["chart_workspace"]._symbol_combo.setCurrentText(symbol)
                main._navigate_to("chart_workspace")
        except Exception as exc:
            logger.debug("PaperTradingPage chart nav: %s", exc)
