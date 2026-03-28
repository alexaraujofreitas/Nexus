# ============================================================
# NEXUS TRADER — Performance Analytics Page (v2)
#
# Full demo-trading evaluation dashboard. Provides:
#   • Overview strip    — 10 summary stat cards
#   • Equity Curve      — capital-over-time + drawdown overlay
#   • By Regime         — regime-level performance breakdown
#   • By Model          — per-IDSS-model attribution
#   • By Asset          — per-symbol performance breakdown
#   • By Side           — long vs short breakdown
#   • Trade Distributions — size / duration / R:R histograms
#   • Learning Activity — adaptive model weight status
#   • Demo Readiness    — DemoPerformanceEvaluator output
# ============================================================
from __future__ import annotations

import logging
import math
from collections import defaultdict
from datetime import datetime, timezone
from typing import Optional

import pyqtgraph as pg

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QFrame,
    QTableWidget, QTableWidgetItem, QHeaderView,
    QAbstractItemView, QPushButton, QTabWidget,
    QSizePolicy, QScrollArea, QGridLayout,
    QProgressBar, QTextEdit,
)
from PySide6.QtCore import Qt, QTimer, Slot
from PySide6.QtGui import QColor, QFont

from gui.main_window import PageHeader
from core.event_bus import bus, Topics

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────
# Colour palette
# ─────────────────────────────────────────────────────────────
_C_BG      = "#060B14"
_C_CARD    = "#0D1320"
_C_BORDER  = "#1A2332"
_C_MUTED   = "#8899AA"
_C_TEXT    = "#E8EBF0"
_C_GREEN   = "#00CC77"
_C_RED     = "#FF3355"
_C_YELLOW  = "#FFB300"
_C_BLUE    = "#1E90FF"
_C_ORANGE  = "#FF6600"
_C_PURPLE  = "#AA55FF"

_REGIME_COLORS = {
    "bull_trend":     _C_GREEN,
    "bear_trend":     _C_RED,
    "ranging":        _C_YELLOW,
    "vol_expansion":  _C_ORANGE,
    "vol_compress":   "#5599FF",
    "uncertain":      _C_MUTED,
    "accumulation":   _C_PURPLE,
    "distribution":   "#FF9966",
    "squeeze":        "#66AAFF",
}

_STATUS_COLORS = {
    "NOT_READY":         (_C_RED,    "⛔"),
    "NEEDS_IMPROVEMENT": (_C_YELLOW, "⚠"),
    "READY_FOR_LIVE":    (_C_GREEN,  "✅"),
}

_MODEL_ABBREVS = {
    "trend":               "TREND",
    "mean_reversion":      "MR",
    "momentum_breakout":   "MOM",
    "vwap_reversion":      "VWAP",
    "liquidity_sweep":     "LIQ",
    "funding_rate":        "FUND",
    "order_book":          "OB",
    "sentiment":           "SENT",
}

_SECT_STYLE  = "color:#8899AA; font-size:13px; font-weight:600;"
_CARD_STYLE  = (
    "QFrame#card { background:#0D1320; border:1px solid #1A2332; border-radius:6px; }"
)
_TABLE_STYLE = (
    "QTableWidget { background:#0A0E1A; color:#E8EBF0; "
    "gridline-color:#141E2E; font-size:13px; border:none; }"
    "QTableWidget::item:selected { background:#1A2D4A; }"
    "QTableWidget::item:alternate { background:#0C1018; }"
    "QHeaderView::section { background:#0D1320; color:#8899AA; "
    "padding:6px 8px; border:none; "
    "border-bottom:1px solid #1A2332; font-size:13px; font-weight:600; }"
)
_BTN_NEUTRAL = (
    "QPushButton { background:#0D1320; color:#8899AA; border:1px solid #2A3A52; "
    "border-radius:5px; font-size:13px; font-weight:600; padding:5px 16px; }"
    "QPushButton:hover { background:#1A2332; color:#E8EBF0; }"
)


# ─────────────────────────────────────────────────────────────
# Helper factories
# ─────────────────────────────────────────────────────────────
def _ci(text: str, color: str = _C_TEXT,
        align: Qt.AlignmentFlag = Qt.AlignCenter) -> QTableWidgetItem:
    item = QTableWidgetItem(text)
    item.setForeground(QColor(color))
    item.setTextAlignment(align)
    item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
    return item


def _ni(value: float, text: str, color: str = _C_TEXT) -> QTableWidgetItem:
    item = QTableWidgetItem(text)
    item.setData(Qt.UserRole, value)
    item.setForeground(QColor(color))
    item.setTextAlignment(Qt.AlignCenter)
    item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
    return item


def _fmt_duration(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60}s"
    h = seconds // 3600
    m = (seconds % 3600) // 60
    return f"{h}h {m}m"


def _iso_to_ts(iso: str) -> float:
    try:
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        return 0.0


def _pnl_color(val: float) -> str:
    return _C_GREEN if val > 0 else (_C_RED if val < 0 else _C_MUTED)


def _section_label(text: str) -> QLabel:
    lbl = QLabel(text)
    lbl.setStyleSheet(f"color:{_C_MUTED}; font-size:12px; font-weight:600;")
    return lbl


# ─────────────────────────────────────────────────────────────
# Stat card
# ─────────────────────────────────────────────────────────────
class _StatCard(QWidget):
    def __init__(self, title: str, value: str = "—", color: str = _C_TEXT):
        super().__init__()
        v = QVBoxLayout(self)
        v.setContentsMargins(14, 10, 14, 10)
        v.setSpacing(2)
        t = QLabel(title)
        t.setStyleSheet(_SECT_STYLE)
        self._val = QLabel(value)
        self._val.setStyleSheet(f"font-size:17px; font-weight:700; color:{color};")
        self._sub = QLabel("")
        self._sub.setStyleSheet(f"font-size:11px; color:{_C_MUTED};")
        v.addWidget(t)
        v.addWidget(self._val)
        v.addWidget(self._sub)

    def set(self, text: str, color: str = _C_TEXT):
        self._val.setText(text)
        self._val.setStyleSheet(f"font-size:17px; font-weight:700; color:{color};")

    def set_sub(self, text: str):
        """Set a small secondary line below the main value (e.g. 'N closed · N open')."""
        self._sub.setText(text)


# ─────────────────────────────────────────────────────────────
# pyqtgraph axis helpers
# ─────────────────────────────────────────────────────────────
class _DateAxis(pg.AxisItem):
    _MIN_TS = 946684800  # 2000-01-01

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.setHeight(40)

    def tickStrings(self, values, scale, spacing):
        from datetime import datetime as _dt
        result = []
        for v in values:
            if v < self._MIN_TS:
                result.append("")
                continue
            try:
                result.append(_dt.utcfromtimestamp(float(v)).strftime("%b %d\n%H:%M"))
            except Exception:
                result.append("")
        return result


class _DollarAxis(pg.AxisItem):
    def tickStrings(self, values, scale, spacing):
        return [f"${v:,.0f}" for v in values]


class _PctAxis(pg.AxisItem):
    def tickStrings(self, values, scale, spacing):
        return [f"{v:.1f}%" for v in values]


# ─────────────────────────────────────────────────────────────
# Tab 1: Equity Curve + Drawdown
# ─────────────────────────────────────────────────────────────
class _EquityTab(QWidget):
    def __init__(self):
        super().__init__()
        v = QVBoxLayout(self)
        v.setContentsMargins(12, 12, 12, 12)
        v.setSpacing(8)

        lbl_row = QHBoxLayout()
        self._info_lbl = QLabel("Equity curve will appear here once trades are closed.")
        self._info_lbl.setStyleSheet(f"color:{_C_MUTED}; font-size:13px;")
        lbl_row.addWidget(self._info_lbl)
        lbl_row.addStretch()
        v.addLayout(lbl_row)

        # Equity curve
        self._equity_plot = pg.PlotWidget(
            axisItems={
                "bottom": _DateAxis(orientation="bottom"),
                "left":   _DollarAxis(orientation="left"),
            }
        )
        self._equity_plot.setBackground(_C_BG)
        self._equity_plot.showGrid(x=True, y=True, alpha=0.12)
        self._equity_plot.getAxis("left").setTextPen(pg.mkPen(_C_MUTED))
        self._equity_plot.getAxis("bottom").setTextPen(pg.mkPen(_C_MUTED))
        self._equity_plot.setLabel("left",   "Capital (USDT)", color=_C_MUTED)
        self._equity_plot.setLabel("bottom", "Trade Close Time", color=_C_MUTED)
        self._equity_plot.setMinimumHeight(200)
        v.addWidget(self._equity_plot, 2)

        # Drawdown chart
        v.addWidget(_section_label("  Rolling Drawdown"))
        self._dd_plot = pg.PlotWidget(
            axisItems={
                "bottom": _DateAxis(orientation="bottom"),
                "left":   _PctAxis(orientation="left"),
            }
        )
        self._dd_plot.setBackground(_C_BG)
        self._dd_plot.showGrid(x=True, y=True, alpha=0.12)
        self._dd_plot.getAxis("left").setTextPen(pg.mkPen(_C_MUTED))
        self._dd_plot.getAxis("bottom").setTextPen(pg.mkPen(_C_MUTED))
        self._dd_plot.setLabel("left",   "Drawdown %", color=_C_MUTED)
        self._dd_plot.setLabel("bottom", "Trade Close Time", color=_C_MUTED)
        self._dd_plot.setMinimumHeight(110)
        self._dd_plot.setYRange(-30, 2)
        self._dd_plot.invertY(False)
        v.addWidget(self._dd_plot, 1)

    def refresh(self, closed_trades: list[dict], initial_capital: float):
        self._equity_plot.clear()
        self._dd_plot.clear()

        if not closed_trades:
            self._info_lbl.setText("No closed trades yet.")
            return

        trades = sorted(closed_trades, key=lambda t: t.get("closed_at", ""))

        first_open_ts = _iso_to_ts(trades[0].get("opened_at", "")) or \
                        _iso_to_ts(trades[0].get("closed_at", ""))
        xs = [first_open_ts]
        ys = [initial_capital]

        running = initial_capital
        for t in trades:
            ts = _iso_to_ts(t.get("closed_at", ""))
            running += t.get("pnl_usdt", 0.0)
            xs.append(ts)
            ys.append(running)

        n         = len(trades)
        total_pnl = running - initial_capital
        pnl_sign  = "+" if total_pnl >= 0 else ""
        self._info_lbl.setText(
            f"{n} closed trade(s)   │   "
            f"Start: ${initial_capital:,.2f}   │   "
            f"Current: ${running:,.2f}   │   "
            f"P&L: {pnl_sign}${total_pnl:,.2f}"
        )

        line_color = _C_GREEN if running >= initial_capital else _C_RED
        pen = pg.mkPen(color=line_color, width=2)
        self._equity_plot.plot(
            xs, ys, pen=pen,
            symbol="o", symbolSize=5,
            symbolBrush=pg.mkBrush(line_color),
            symbolPen=pg.mkPen(None),
        )
        baseline_pen = pg.mkPen("#2A3A52", width=1, style=Qt.DashLine)
        self._equity_plot.addItem(
            pg.InfiniteLine(pos=initial_capital, angle=0, pen=baseline_pen)
        )

        # Drawdown series
        peak   = ys[0]
        dd_ys  = [0.0]
        for y in ys[1:]:
            if y > peak:
                peak = y
            dd_ys.append(-((peak - y) / peak * 100) if peak > 0 else 0.0)
        fill_pen  = pg.mkPen(_C_RED, width=1)
        fill_brush = pg.mkBrush(255, 51, 85, 60)
        dd_curve  = self._dd_plot.plot(xs, dd_ys, pen=fill_pen)
        fill_item = pg.FillBetweenItem(
            dd_curve,
            self._dd_plot.plot(xs, [0.0] * len(xs), pen=pg.mkPen(None)),
            brush=fill_brush,
        )
        self._dd_plot.addItem(fill_item)


# ─────────────────────────────────────────────────────────────
# Tab 2: Regime Breakdown
# ─────────────────────────────────────────────────────────────
_REG_COLS = [
    "Regime", "Trades", "Wins", "Losses",
    "Win Rate", "Total P&L", "Avg P&L", "Avg Duration",
]


class _RegimeTab(QWidget):
    def __init__(self):
        super().__init__()
        v = QVBoxLayout(self)
        v.setContentsMargins(12, 12, 12, 12)
        self._table = _make_table(_REG_COLS, stretch_col=0)
        v.addWidget(self._table)

    def refresh(self, closed_trades: list[dict]):
        groups: dict[str, list] = defaultdict(list)
        for t in closed_trades:
            regime = t.get("regime") or "unknown"
            groups[regime].append(t)

        self._table.setSortingEnabled(False)
        self._table.setRowCount(0)

        for regime, trades in sorted(groups.items()):
            n      = len(trades)
            wins   = sum(1 for t in trades if t.get("pnl_usdt", 0) > 0)
            losses = n - wins
            wr     = wins / n * 100 if n else 0.0
            total  = sum(t.get("pnl_usdt", 0) for t in trades)
            avg    = total / n if n else 0.0
            avg_d  = sum(t.get("duration_s", 0) for t in trades) / n if n else 0.0

            row = self._table.rowCount()
            self._table.insertRow(row)

            reg_color = _REGIME_COLORS.get(regime, _C_MUTED)
            reg_disp  = regime.replace("_", " ").title()
            wr_col    = _C_GREEN if wr >= 55 else (_C_YELLOW if wr >= 45 else _C_RED)

            self._table.setItem(row, 0, _ci(reg_disp, reg_color))
            self._table.setItem(row, 1, _ni(float(n),      str(n)))
            self._table.setItem(row, 2, _ni(float(wins),   str(wins),   _C_GREEN))
            self._table.setItem(row, 3, _ni(float(losses), str(losses), _C_RED if losses else _C_MUTED))
            self._table.setItem(row, 4, _ni(wr,    f"{wr:.1f}%",            wr_col))
            self._table.setItem(row, 5, _ni(total, f"{'+'if total>=0 else'-'}${abs(total):.2f}",        _pnl_color(total)))
            self._table.setItem(row, 6, _ni(avg,   f"{'+'if avg>=0 else'-'}${abs(avg):.2f}",          _pnl_color(avg)))
            self._table.setItem(row, 7, _ni(avg_d, _fmt_duration(int(avg_d))))

        self._table.setSortingEnabled(True)


# ─────────────────────────────────────────────────────────────
# Tab 3: Model Attribution
# ─────────────────────────────────────────────────────────────
_MOD_COLS = [
    "Model", "Trades", "Wins", "Win Rate",
    "Total P&L", "Avg P&L", "Avg Score", "Adaptive Adj.",
]


class _ModelTab(QWidget):
    def __init__(self):
        super().__init__()
        v = QVBoxLayout(self)
        v.setContentsMargins(12, 12, 12, 12)
        v.setSpacing(6)

        note = QLabel(
            "Performance of trades where each model was in the firing set. "
            "A trade may be attributed to multiple models simultaneously. "
            "Adaptive Adj. = current weight multiplier from the learning loop (1.0 = neutral, >1.0 = boosted, <1.0 = penalised)."
        )
        note.setStyleSheet(f"color:{_C_MUTED}; font-size:13px;")
        note.setWordWrap(True)
        v.addWidget(note)

        self._table = _make_table(_MOD_COLS, stretch_col=0)
        v.addWidget(self._table)

    def refresh(self, closed_trades: list[dict]):
        model_groups: dict[str, list] = defaultdict(list)
        for t in closed_trades:
            for model in t.get("models_fired") or []:
                model_groups[model].append(t)

        if not model_groups and closed_trades:
            model_groups["(no model data)"] = closed_trades

        # Fetch adaptive adjustments from the learning loop
        adj_map: dict[str, str] = {}
        try:
            from core.meta_decision.confluence_scorer import get_outcome_tracker
            tracker = get_outcome_tracker()
            for m in model_groups:
                adj = tracker.get_weight_adjustment(m)
                wr  = tracker.get_win_rate(m)
                if wr is not None:
                    adj_map[m] = f"×{adj:.3f}  ({wr*100:.0f}% WR)"
                else:
                    adj_map[m] = "warming up"
        except Exception:
            pass

        self._table.setSortingEnabled(False)
        self._table.setRowCount(0)

        for model, trades in sorted(model_groups.items()):
            n     = len(trades)
            wins  = sum(1 for t in trades if t.get("pnl_usdt", 0) > 0)
            wr    = wins / n * 100 if n else 0.0
            total = sum(t.get("pnl_usdt", 0) for t in trades)
            avg   = total / n if n else 0.0
            avg_s = sum(t.get("score", 0) for t in trades) / n if n else 0.0

            abbr    = _MODEL_ABBREVS.get(model, model.upper()[:6])
            disp    = f"{abbr}  ({model})"
            wr_col  = _C_GREEN if wr >= 55 else (_C_YELLOW if wr >= 45 else _C_RED)
            adj_txt = adj_map.get(model, "—")
            adj_col = _C_GREEN if "×" in adj_txt and float(adj_txt[1:6]) > 1.0 else (
                      _C_RED if "×" in adj_txt and float(adj_txt[1:6]) < 1.0 else _C_MUTED)

            row = self._table.rowCount()
            self._table.insertRow(row)
            self._table.setItem(row, 0, _ci(disp,  _C_BLUE))
            self._table.setItem(row, 1, _ni(float(n),    str(n)))
            self._table.setItem(row, 2, _ni(float(wins), str(wins), _C_GREEN))
            self._table.setItem(row, 3, _ni(wr,    f"{wr:.1f}%",   wr_col))
            self._table.setItem(row, 4, _ni(total, f"{'+'if total>=0 else'-'}${abs(total):.2f}", _pnl_color(total)))
            self._table.setItem(row, 5, _ni(avg,   f"{'+'if avg>=0 else'-'}${abs(avg):.2f}",   _pnl_color(avg)))
            self._table.setItem(row, 6, _ni(avg_s, f"{avg_s:.3f}"))
            self._table.setItem(row, 7, _ci(adj_txt, adj_col))

        self._table.setSortingEnabled(True)


# ─────────────────────────────────────────────────────────────
# Tab 4: Asset Breakdown
# ─────────────────────────────────────────────────────────────
_ASSET_COLS = [
    "Asset", "Trades", "Wins", "Losses",
    "Win Rate", "Total P&L", "Avg P&L", "Avg R:R", "Share %",
]


class _AssetTab(QWidget):
    def __init__(self):
        super().__init__()
        v = QVBoxLayout(self)
        v.setContentsMargins(12, 12, 12, 12)
        self._table = _make_table(_ASSET_COLS, stretch_col=0)
        v.addWidget(self._table)

    def refresh(self, closed_trades: list[dict]):
        total_n = len(closed_trades)
        groups: dict[str, list] = defaultdict(list)
        for t in closed_trades:
            groups[t.get("symbol", "?")].append(t)

        self._table.setSortingEnabled(False)
        self._table.setRowCount(0)

        for symbol, trades in sorted(groups.items(), key=lambda x: -len(x[1])):
            n      = len(trades)
            wins   = sum(1 for t in trades if t.get("pnl_usdt", 0) > 0)
            losses = n - wins
            wr     = wins / n * 100 if n else 0.0
            total  = sum(t.get("pnl_usdt", 0) for t in trades)
            avg    = total / n if n else 0.0
            share  = n / total_n * 100 if total_n else 0.0

            rr_list = []
            for t in trades:
                entry, sl, tp, side = (
                    t.get("entry_price", 0), t.get("stop_loss", 0),
                    t.get("take_profit", 0), t.get("side", "buy"),
                )
                if entry > 0 and sl > 0 and tp > 0:
                    risk   = (entry - sl)  if side == "buy" else (sl - entry)
                    reward = (tp - entry)  if side == "buy" else (entry - tp)
                    if risk > 0:
                        rr_list.append(reward / risk)
            avg_rr = sum(rr_list) / len(rr_list) if rr_list else 0.0

            wr_col = _C_GREEN if wr >= 55 else (_C_YELLOW if wr >= 45 else _C_RED)
            row = self._table.rowCount()
            self._table.insertRow(row)
            self._table.setItem(row, 0, _ci(symbol, _C_TEXT))
            self._table.setItem(row, 1, _ni(float(n),      str(n)))
            self._table.setItem(row, 2, _ni(float(wins),   str(wins),   _C_GREEN))
            self._table.setItem(row, 3, _ni(float(losses), str(losses), _C_RED if losses else _C_MUTED))
            self._table.setItem(row, 4, _ni(wr,    f"{wr:.1f}%",   wr_col))
            self._table.setItem(row, 5, _ni(total, f"{'+'if total>=0 else'-'}${abs(total):.2f}", _pnl_color(total)))
            self._table.setItem(row, 6, _ni(avg,   f"{'+'if avg>=0 else'-'}${abs(avg):.2f}",   _pnl_color(avg)))
            self._table.setItem(row, 7, _ni(avg_rr, f"{avg_rr:.2f}",
                                           _C_GREEN if avg_rr >= 1 else _C_YELLOW))
            self._table.setItem(row, 8, _ni(share, f"{share:.1f}%"))

        self._table.setSortingEnabled(True)


# ─────────────────────────────────────────────────────────────
# Tab 5: Side Breakdown (Long vs Short)
# ─────────────────────────────────────────────────────────────
class _SideTab(QWidget):
    _COLS = [
        "Side", "Trades", "Wins", "Losses",
        "Win Rate", "Total P&L", "Avg P&L", "Avg R:R", "Profit Factor",
    ]

    def __init__(self):
        super().__init__()
        v = QVBoxLayout(self)
        v.setContentsMargins(12, 12, 12, 12)
        self._table = _make_table(self._COLS, stretch_col=0)
        v.addWidget(self._table)

    def refresh(self, closed_trades: list[dict]):
        groups: dict[str, list] = defaultdict(list)
        for t in closed_trades:
            groups[t.get("side", "?")].append(t)

        self._table.setSortingEnabled(False)
        self._table.setRowCount(0)

        for side, trades in sorted(groups.items()):
            n      = len(trades)
            wins   = sum(1 for t in trades if t.get("pnl_usdt", 0) > 0)
            losses = n - wins
            wr     = wins / n * 100 if n else 0.0
            pnl_l  = [t.get("pnl_usdt", 0) for t in trades]
            total  = sum(pnl_l)
            avg    = total / n if n else 0.0
            gw     = sum(p for p in pnl_l if p > 0)
            gl     = abs(sum(p for p in pnl_l if p < 0))
            pf     = round(gw / gl, 2) if gl > 0 else (999.0 if gw > 0 else 0.0)

            rr_list = []
            for t in trades:
                entry, sl, tp = (
                    t.get("entry_price", 0), t.get("stop_loss", 0), t.get("take_profit", 0),
                )
                if entry > 0 and sl > 0 and tp > 0:
                    risk   = (entry - sl)  if side == "buy" else (sl - entry)
                    reward = (tp - entry)  if side == "buy" else (entry - tp)
                    if risk > 0:
                        rr_list.append(reward / risk)
            avg_rr = sum(rr_list) / len(rr_list) if rr_list else 0.0

            wr_col    = _C_GREEN if wr >= 55 else (_C_YELLOW if wr >= 45 else _C_RED)
            side_disp = "Long (Buy)" if side == "buy" else "Short (Sell)"
            side_col  = _C_GREEN if side == "buy" else _C_RED

            row = self._table.rowCount()
            self._table.insertRow(row)
            self._table.setItem(row, 0, _ci(side_disp, side_col))
            self._table.setItem(row, 1, _ni(float(n),      str(n)))
            self._table.setItem(row, 2, _ni(float(wins),   str(wins),   _C_GREEN))
            self._table.setItem(row, 3, _ni(float(losses), str(losses), _C_RED if losses else _C_MUTED))
            self._table.setItem(row, 4, _ni(wr,   f"{wr:.1f}%",  wr_col))
            self._table.setItem(row, 5, _ni(total, f"{'+'if total>=0 else'-'}${abs(total):.2f}", _pnl_color(total)))
            self._table.setItem(row, 6, _ni(avg,  f"{'+'if avg>=0 else'-'}${abs(avg):.2f}",  _pnl_color(avg)))
            self._table.setItem(row, 7, _ni(avg_rr, f"{avg_rr:.2f}",
                                           _C_GREEN if avg_rr >= 1 else _C_YELLOW))
            self._table.setItem(row, 8, _ni(pf, f"{pf:.2f}" if pf < 900 else "∞",
                                           _C_GREEN if pf >= 1 else _C_RED))

        self._table.setSortingEnabled(True)


# ─────────────────────────────────────────────────────────────
# Tab 6: Trade Distributions
# ─────────────────────────────────────────────────────────────
class _DistTab(QWidget):
    def __init__(self):
        super().__init__()
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet(f"QScrollArea {{ background:{_C_BG}; border:none; }}")

        inner = QWidget()
        inner.setStyleSheet(f"background:{_C_BG};")
        v = QVBoxLayout(inner)
        v.setContentsMargins(12, 12, 12, 12)
        v.setSpacing(14)

        def _plot_widget(title: str, x_label: str, y_label: str = "Count"):
            pw = pg.PlotWidget()
            pw.setBackground(_C_BG)
            pw.showGrid(x=True, y=True, alpha=0.10)
            pw.getAxis("left").setTextPen(pg.mkPen(_C_MUTED))
            pw.getAxis("bottom").setTextPen(pg.mkPen(_C_MUTED))
            pw.setLabel("left", y_label, color=_C_MUTED)
            pw.setLabel("bottom", x_label, color=_C_MUTED)
            pw.setTitle(title, color=_C_MUTED, size="13pt")
            pw.setMinimumHeight(160)
            return pw

        self._pnl_plot  = _plot_widget("P&L Distribution (per trade)", "P&L (USDT)")
        self._rr_plot   = _plot_widget("R:R Distribution (configured SL/TP)", "Reward:Risk Ratio")
        self._size_plot = _plot_widget("Position Size Distribution", "Size (USDT)")
        self._dur_plot  = _plot_widget("Trade Duration Distribution", "Duration (hours)")
        self._score_plot = _plot_widget("Confluence Score Distribution", "Score (0–1)")

        v.addWidget(_section_label("Trade P&L"))
        v.addWidget(self._pnl_plot)
        v.addWidget(_section_label("Reward:Risk Ratio"))
        v.addWidget(self._rr_plot)
        v.addWidget(_section_label("Position Size"))
        v.addWidget(self._size_plot)
        v.addWidget(_section_label("Trade Duration"))
        v.addWidget(self._dur_plot)
        v.addWidget(_section_label("Confluence Score at Entry"))
        v.addWidget(self._score_plot)

        scroll.setWidget(inner)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(scroll)

    def refresh(self, closed_trades: list[dict]):
        for pw in (self._pnl_plot, self._rr_plot, self._size_plot,
                   self._dur_plot, self._score_plot):
            pw.clear()

        if not closed_trades:
            return

        def _bar_chart(plot_widget: pg.PlotWidget, values: list, color: str, bins: int = 20):
            if not values:
                return
            lo, hi = min(values), max(values)
            if lo == hi:
                hi = lo + 1
            width = (hi - lo) / bins
            counts = [0] * bins
            edges  = [lo + i * width for i in range(bins + 1)]
            for v in values:
                idx = min(int((v - lo) / width), bins - 1)
                counts[idx] += 1
            x = [(edges[i] + edges[i + 1]) / 2 for i in range(bins)]
            bg = pg.BarGraphItem(x=x, height=counts, width=width * 0.85,
                                 brush=color, pen=pg.mkPen(None))
            plot_widget.addItem(bg)

        pnl_vals   = [t.get("pnl_usdt", 0) for t in closed_trades]
        size_vals  = [t.get("size_usdt", 0) for t in closed_trades]
        dur_vals   = [t.get("duration_s", 0) / 3600 for t in closed_trades]
        score_vals = [t.get("score", 0)     for t in closed_trades]

        rr_vals = []
        for t in closed_trades:
            entry, sl, tp, side = (
                t.get("entry_price", 0), t.get("stop_loss", 0),
                t.get("take_profit", 0), t.get("side", "buy"),
            )
            if entry > 0 and sl > 0 and tp > 0:
                risk   = (entry - sl) if side == "buy" else (sl - entry)
                reward = (tp - entry) if side == "buy" else (entry - tp)
                if risk > 0:
                    rr_vals.append(reward / risk)

        _bar_chart(self._pnl_plot,   pnl_vals,   _C_BLUE)
        _bar_chart(self._rr_plot,    rr_vals,     _C_GREEN)
        _bar_chart(self._size_plot,  size_vals,   _C_YELLOW)
        _bar_chart(self._dur_plot,   dur_vals,    _C_BLUE)
        _bar_chart(self._score_plot, score_vals,  _C_PURPLE)


# ─────────────────────────────────────────────────────────────
# Tab 7: Learning Activity
# ─────────────────────────────────────────────────────────────
_LEARN_COLS = [
    "Model", "Outcomes Recorded", "Win Rate",
    "Weight Adj.", "Status", "Effect",
]
_L2_REGIME_COLS  = ["Model", "Regime", "Trades", "Win Rate", "Adj", "Tier"]
_L2_ASSET_COLS   = ["Model", "Asset",  "Trades", "Win Rate", "Adj", "Tier"]
_L2_SCORE_COLS   = ["Score Bucket", "Trades", "Win Rate", "Calibrated?"]
_L2_EXIT_COLS    = ["Model", "TP", "SL", "Other", "TP Rate", "Avg TP R", "Tgt Cap%", "Total"]

_C_PARTIAL = "#B8860B"   # dark goldenrod — partial activation indicator


class _LearningTab(QWidget):
    """
    Learning Loop tab — shows Level-1 (global) and Level-2 (contextual)
    adaptive weight information across five sub-panels:
      L1 Overview · By Regime · By Asset · Score Calibration · Exit Efficiency
    """
    def __init__(self):
        super().__init__()
        v = QVBoxLayout(self)
        v.setContentsMargins(12, 12, 12, 12)
        v.setSpacing(10)

        # Status banner
        self._status_lbl = QLabel("Learning loop status: initialising…")
        self._status_lbl.setStyleSheet(
            f"background:#0D1320; border:1px solid #1A2332; "
            f"border-radius:5px; color:{_C_TEXT}; "
            f"font-size:13px; padding:8px 12px;"
        )
        self._status_lbl.setWordWrap(True)
        v.addWidget(self._status_lbl)

        # Sub-tabs for L1 / L2 panels
        self._sub_tabs = QTabWidget()
        self._sub_tabs.setStyleSheet(
            f"QTabWidget::pane {{ border:1px solid {_C_BORDER}; background:{_C_BG}; }}"
            f"QTabBar::tab {{ background:#0D1320; color:{_C_MUTED}; padding:5px 12px; border:1px solid {_C_BORDER}; }}"
            f"QTabBar::tab:selected {{ background:#101C30; color:{_C_TEXT}; }}"
        )
        v.addWidget(self._sub_tabs)

        # ── L1 Overview sub-tab ─────────────────────────────────
        l1_widget = QWidget()
        l1_v = QVBoxLayout(l1_widget)
        l1_v.setContentsMargins(6, 6, 6, 6)
        l1_v.setSpacing(8)
        explain_l1 = QLabel(
            "Level-1 learning: global per-model rolling win-rate (30 trades) → ±15% "
            "weight multiplier.  Active when ≥5 outcomes recorded per model."
        )
        explain_l1.setStyleSheet(f"color:{_C_MUTED}; font-size:12px;")
        explain_l1.setWordWrap(True)
        l1_v.addWidget(explain_l1)
        self._l1_table = _make_table(_LEARN_COLS, stretch_col=0)
        l1_v.addWidget(self._l1_table)
        l1_v.addWidget(_section_label("Recent Trade Outcome Log"))
        self._log = QTextEdit()
        self._log.setReadOnly(True)
        self._log.setStyleSheet(
            f"QTextEdit {{ background:#0A0E1A; color:{_C_MUTED}; "
            f"border:1px solid {_C_BORDER}; font-size:12px; }}"
        )
        self._log.setMaximumHeight(130)
        l1_v.addWidget(self._log)
        self._sub_tabs.addTab(l1_widget, "L1 Overview")

        # ── L2 By Regime sub-tab ────────────────────────────────
        regime_widget = QWidget()
        regime_v = QVBoxLayout(regime_widget)
        regime_v.setContentsMargins(6, 6, 6, 6)
        regime_v.setSpacing(8)
        explain_regime = QLabel(
            "Level-2 regime learning: each (model, regime) cell maintains a rolling "
            "50-trade win-rate.  Cells with ≥10 trades adjust the model's weight by up "
            "to ±10%.  Hard-clamped at the combined [0.70, 1.30] bound."
        )
        explain_regime.setStyleSheet(f"color:{_C_MUTED}; font-size:12px;")
        explain_regime.setWordWrap(True)
        regime_v.addWidget(explain_regime)
        self._regime_table = _make_table(_L2_REGIME_COLS, stretch_col=0)
        regime_v.addWidget(self._regime_table)
        self._sub_tabs.addTab(regime_widget, "By Regime")

        # ── L2 By Asset sub-tab ─────────────────────────────────
        asset_widget = QWidget()
        asset_v = QVBoxLayout(asset_widget)
        asset_v.setContentsMargins(6, 6, 6, 6)
        asset_v.setSpacing(8)
        explain_asset = QLabel(
            "Level-2 asset learning: each (model, asset) cell tracks a rolling 50-trade "
            "win-rate.  Cells with ≥10 trades adjust the model's weight by up to ±8%."
        )
        explain_asset.setStyleSheet(f"color:{_C_MUTED}; font-size:12px;")
        explain_asset.setWordWrap(True)
        asset_v.addWidget(explain_asset)
        self._asset_table = _make_table(_L2_ASSET_COLS, stretch_col=0)
        asset_v.addWidget(self._asset_table)
        self._sub_tabs.addTab(asset_widget, "By Asset")

        # ── Score Calibration sub-tab ───────────────────────────
        score_widget = QWidget()
        score_v = QVBoxLayout(score_widget)
        score_v.setContentsMargins(6, 6, 6, 6)
        score_v.setSpacing(8)
        explain_score = QLabel(
            "Score calibration: tracks real win-rate per confluence score decile.  "
            "Well-calibrated → higher-score trades should win more often.  "
            "Used diagnostically; does not directly adjust model weights."
        )
        explain_score.setStyleSheet(f"color:{_C_MUTED}; font-size:12px;")
        explain_score.setWordWrap(True)
        score_v.addWidget(explain_score)
        self._score_table = _make_table(_L2_SCORE_COLS, stretch_col=0)
        score_v.addWidget(self._score_table)
        self._sub_tabs.addTab(score_widget, "Score Calibration")

        # ── Exit Efficiency sub-tab ─────────────────────────────
        exit_widget = QWidget()
        exit_v = QVBoxLayout(exit_widget)
        exit_v.setContentsMargins(6, 6, 6, 6)
        exit_v.setSpacing(8)
        explain_exit = QLabel(
            "Exit efficiency: TP / SL / other counts, TP rate, average realized R on TP exits, "
            "and target capture % (avg TP realized-R / avg expected-RR \u00d7 100).  "
            "Tgt Cap% < 70% may indicate targets are set too far; "
            "stop tightness flag triggers when SL rate > 60%."
        )
        explain_exit.setStyleSheet(f"color:{_C_MUTED}; font-size:12px;")
        explain_exit.setWordWrap(True)
        exit_v.addWidget(explain_exit)
        self._exit_table = _make_table(_L2_EXIT_COLS, stretch_col=0)
        exit_v.addWidget(self._exit_table)
        self._sub_tabs.addTab(exit_widget, "Exit Efficiency")

    # ── helpers ───────────────────────────────────────────────────────────

    @staticmethod
    def _adj_color(adj: float) -> str:
        if adj > 1.005:
            return _C_GREEN
        if adj < 0.995:
            return _C_RED
        return _C_MUTED

    # ── refresh ───────────────────────────────────────────────────────────

    def refresh(self, closed_trades: list[dict]):
        self._refresh_l1(closed_trades)
        self._refresh_l2_regime()
        self._refresh_l2_asset()
        self._refresh_score_cal()
        self._refresh_exit_eff()
        self._refresh_status(closed_trades)

    def _refresh_l1(self, closed_trades: list[dict]):
        self._l1_table.setSortingEnabled(False)
        self._l1_table.setRowCount(0)
        all_models = [
            "trend", "mean_reversion", "momentum_breakout",
            "vwap_reversion", "liquidity_sweep",
            "funding_rate", "order_book", "sentiment",
        ]
        try:
            from core.meta_decision.confluence_scorer import get_outcome_tracker
            tracker = get_outcome_tracker()
            for model in all_models:
                outcomes = tracker._outcomes.get(model, [])
                n_out    = len(outcomes)
                wr       = tracker.get_win_rate(model)
                adj      = tracker.get_weight_adjustment(model)
                if wr is not None:
                    status_txt, status_col = "Active", _C_GREEN
                elif n_out > 0:
                    status_txt, status_col = f"Warming ({n_out}/5)", _C_YELLOW
                else:
                    status_txt, status_col = "No data", _C_MUTED
                wr_txt  = f"{wr*100:.0f}%" if wr is not None else "—"
                adj_txt = f"×{adj:.3f}"
                adj_col = self._adj_color(adj)
                effect  = (f"+{(adj-1)*100:.0f}% boost" if adj > 1.0 else
                           f"−{(1-adj)*100:.0f}% penalty" if adj < 1.0 else "neutral")
                disp    = _MODEL_ABBREVS.get(model, model)
                row = self._l1_table.rowCount()
                self._l1_table.insertRow(row)
                self._l1_table.setItem(row, 0, _ci(f"{disp} ({model})", _C_BLUE))
                self._l1_table.setItem(row, 1, _ni(float(n_out), str(n_out)))
                self._l1_table.setItem(row, 2, _ci(wr_txt,
                    _C_GREEN if wr and wr >= 0.55 else
                    _C_RED if wr and wr < 0.45 else _C_TEXT))
                self._l1_table.setItem(row, 3, _ci(adj_txt, adj_col))
                self._l1_table.setItem(row, 4, _ci(status_txt, status_col))
                self._l1_table.setItem(row, 5, _ci(effect, adj_col))
        except Exception as exc:
            logger.debug("LearningTab L1 refresh error: %s", exc)
        self._l1_table.setSortingEnabled(True)

        # Recent outcome log
        recent = sorted(closed_trades, key=lambda t: t.get("closed_at", ""), reverse=True)[:20]
        lines  = []
        for tr in recent:
            won  = tr.get("pnl_pct", 0) > 0
            icon = "✅" if won else "❌"
            sym  = tr.get("symbol", "?")
            side = tr.get("side", "?")
            pnl  = tr.get("pnl_pct", 0)
            mods = ", ".join(tr.get("models_fired") or []) or "—"
            ts   = tr.get("closed_at", "")[:16] if tr.get("closed_at") else ""
            lines.append(f"{icon} {ts}  {sym} {'LONG' if side.upper()=='BUY' else 'SHORT'}  {pnl:+.2f}%  [{mods}]")
        self._log.setPlainText("\n".join(lines) if lines else "No closed trades yet.")

    def _refresh_l2_regime(self):
        self._regime_table.setSortingEnabled(False)
        self._regime_table.setRowCount(0)
        try:
            from core.learning.level2_tracker import get_level2_tracker
            rows = get_level2_tracker().get_regime_table()
            for r in rows:
                wr    = r.get("win_rate")
                adj   = r.get("adj", 1.0)
                tier  = r.get("activation_tier", "warming")
                wr_tx = f"{wr*100:.1f}%" if wr is not None else "—"
                adj_tx= f"×{adj:.3f}"
                if tier == "active":
                    tier_tx, tier_col = "✅ Active",  _C_GREEN
                elif tier == "partial":
                    tier_tx, tier_col = "◑ Partial",  _C_PARTIAL
                else:
                    tier_tx, tier_col = "⏳ Warming",  _C_YELLOW
                row   = self._regime_table.rowCount()
                self._regime_table.insertRow(row)
                self._regime_table.setItem(row, 0, _ci(r.get("model", "?"),  _C_BLUE))
                self._regime_table.setItem(row, 1, _ci(r.get("regime", "?"), _C_MUTED))
                self._regime_table.setItem(row, 2, _ni(float(r.get("trades", 0)), str(r.get("trades", 0))))
                self._regime_table.setItem(row, 3, _ci(wr_tx,
                    _C_GREEN if wr and wr >= 0.55 else
                    _C_RED if wr and wr < 0.45 else _C_TEXT))
                self._regime_table.setItem(row, 4, _ci(adj_tx, self._adj_color(adj)))
                self._regime_table.setItem(row, 5, _ci(tier_tx, tier_col))
        except Exception as exc:
            logger.debug("LearningTab L2 regime refresh error: %s", exc)
        self._regime_table.setSortingEnabled(True)

    def _refresh_l2_asset(self):
        self._asset_table.setSortingEnabled(False)
        self._asset_table.setRowCount(0)
        try:
            from core.learning.level2_tracker import get_level2_tracker
            rows = get_level2_tracker().get_asset_table()
            for r in rows:
                wr    = r.get("win_rate")
                adj   = r.get("adj", 1.0)
                tier  = r.get("activation_tier", "warming")
                wr_tx = f"{wr*100:.1f}%" if wr is not None else "—"
                adj_tx= f"×{adj:.3f}"
                if tier == "active":
                    tier_tx, tier_col = "✅ Active",  _C_GREEN
                elif tier == "partial":
                    tier_tx, tier_col = "◑ Partial",  _C_PARTIAL
                else:
                    tier_tx, tier_col = "⏳ Warming",  _C_YELLOW
                row   = self._asset_table.rowCount()
                self._asset_table.insertRow(row)
                self._asset_table.setItem(row, 0, _ci(r.get("model",  "?"), _C_BLUE))
                self._asset_table.setItem(row, 1, _ci(r.get("symbol", "?"), _C_MUTED))
                self._asset_table.setItem(row, 2, _ni(float(r.get("trades", 0)), str(r.get("trades", 0))))
                self._asset_table.setItem(row, 3, _ci(wr_tx,
                    _C_GREEN if wr and wr >= 0.55 else
                    _C_RED if wr and wr < 0.45 else _C_TEXT))
                self._asset_table.setItem(row, 4, _ci(adj_tx, self._adj_color(adj)))
                self._asset_table.setItem(row, 5, _ci(tier_tx, tier_col))
        except Exception as exc:
            logger.debug("LearningTab L2 asset refresh error: %s", exc)
        self._asset_table.setSortingEnabled(True)

    def _refresh_score_cal(self):
        self._score_table.setSortingEnabled(False)
        self._score_table.setRowCount(0)
        try:
            from core.learning.level2_tracker import get_level2_tracker
            l2  = get_level2_tracker()
            cal = l2.get_score_calibration()
            # Calibration quality header row
            quality = l2.get_score_calibration_quality()
            mono = quality.get("monotonicity_score")
            if mono is not None:
                self._score_table.insertRow(0)
                mono_tx  = f"Calibration quality: {mono*100:.0f}% monotonic — {quality.get('description','')}"
                self._score_table.setItem(0, 0, _ci("📊 Quality", _C_BLUE))
                qual_col = _C_GREEN if mono >= 0.75 else _C_YELLOW if mono >= 0.50 else _C_RED
                self._score_table.setItem(0, 1, _ci("", _C_MUTED))
                self._score_table.setItem(0, 2, _ci(f"{mono*100:.0f}%", qual_col))
                self._score_table.setItem(0, 3, _ci(quality.get("description", "")[:40], qual_col))
            for bucket, info in sorted(cal.items()):
                wr  = info.get("win_rate")
                cnt = info.get("count", 0)
                wr_tx  = f"{wr*100:.1f}%" if wr is not None else "—"
                cal_tx = "Yes" if wr is not None else f"Warming ({cnt}/5)"
                row = self._score_table.rowCount()
                self._score_table.insertRow(row)
                self._score_table.setItem(row, 0, _ci(bucket, _C_BLUE))
                self._score_table.setItem(row, 1, _ni(float(cnt), str(cnt)))
                self._score_table.setItem(row, 2, _ci(wr_tx,
                    _C_GREEN if wr and wr >= 0.55 else
                    _C_RED if wr and wr < 0.45 else _C_TEXT))
                self._score_table.setItem(row, 3, _ci(cal_tx,
                    _C_GREEN if wr is not None else _C_YELLOW))
        except Exception as exc:
            logger.debug("LearningTab score cal refresh error: %s", exc)
        self._score_table.setSortingEnabled(True)

    def _refresh_exit_eff(self):
        self._exit_table.setSortingEnabled(False)
        self._exit_table.setRowCount(0)
        try:
            from core.learning.level2_tracker import get_level2_tracker
            diag = get_level2_tracker().get_exit_diagnostics()
            eff  = diag.get("by_model", {})
            ovr  = diag.get("overall", {})
            for model, data in sorted(eff.items()):
                tp      = data.get("tp",    0)
                sl      = data.get("sl",    0)
                other   = data.get("other", 0)
                total   = data.get("total", 0)
                tp_rt   = data.get("tp_rate")
                avg_tp  = data.get("avg_tp_r")
                tgt_cap = data.get("target_capture_pct")
                tp_tx   = f"{tp_rt*100:.1f}%" if tp_rt  is not None else "—"
                atp_tx  = f"×{avg_tp:.2f}"    if avg_tp  is not None else "—"
                tgt_tx  = f"{tgt_cap:.0f}%"   if tgt_cap is not None else "—"
                row = self._exit_table.rowCount()
                self._exit_table.insertRow(row)
                self._exit_table.setItem(row, 0, _ci(model, _C_BLUE))
                self._exit_table.setItem(row, 1, _ni(float(tp),    str(tp)))
                self._exit_table.setItem(row, 2, _ni(float(sl),    str(sl)))
                self._exit_table.setItem(row, 3, _ni(float(other), str(other)))
                self._exit_table.setItem(row, 4, _ci(tp_tx,
                    _C_GREEN if tp_rt and tp_rt >= 0.55 else
                    _C_RED if tp_rt and tp_rt < 0.40 else _C_TEXT))
                self._exit_table.setItem(row, 5, _ci(atp_tx,
                    _C_GREEN if avg_tp and avg_tp > 0 else _C_RED if avg_tp else _C_MUTED))
                self._exit_table.setItem(row, 6, _ci(tgt_tx,
                    _C_GREEN if tgt_cap and tgt_cap >= 90 else
                    _C_YELLOW if tgt_cap and tgt_cap >= 70 else
                    _C_RED if tgt_cap else _C_MUTED))
                self._exit_table.setItem(row, 7, _ni(float(total), str(total)))
            # Overall diagnostic row
            if ovr:
                tp_pct = ovr.get("tp_rate_pct")
                flag   = ovr.get("stop_tightness_flag", False)
                diag_txt = "⚠ High SL rate — stops may be too tight" if flag else "Exit balance OK"
                diag_col = _C_RED if flag else _C_GREEN
                row = self._exit_table.rowCount()
                self._exit_table.insertRow(row)
                self._exit_table.setItem(row, 0, _ci("Overall", _C_MUTED))
                self._exit_table.setItem(row, 1, _ci(f"{tp_pct:.0f}% TP" if tp_pct else "—", _C_MUTED))
                self._exit_table.setItem(row, 2, _ci("", _C_MUTED))
                self._exit_table.setItem(row, 3, _ci("", _C_MUTED))
                self._exit_table.setItem(row, 4, _ci(diag_txt, diag_col))
                self._exit_table.setItem(row, 5, _ci("", _C_MUTED))
                self._exit_table.setItem(row, 6, _ci("", _C_MUTED))
                self._exit_table.setItem(row, 7, _ci("", _C_MUTED))
        except Exception as exc:
            logger.debug("LearningTab exit eff refresh error: %s", exc)
        self._exit_table.setSortingEnabled(True)

    def _refresh_status(self, closed_trades: list[dict]):
        try:
            from core.meta_decision.confluence_scorer import get_outcome_tracker
            from core.learning.level2_tracker import get_level2_tracker
            tracker = get_outcome_tracker()
            l2 = get_level2_tracker()
            all_models = [
                "trend", "mean_reversion", "momentum_breakout",
                "vwap_reversion", "liquidity_sweep",
                "funding_rate", "order_book", "sentiment",
            ]
            active_count  = sum(1 for m in all_models if tracker.get_win_rate(m) is not None)
            warming_count = sum(1 for m in all_models
                                if tracker.get_win_rate(m) is None
                                and len(tracker._outcomes.get(m, [])) > 0)
            l2_summary    = l2.get_summary()
            total_wired   = len([t for t in closed_trades if t.get("models_fired")])
            l2_active     = (l2_summary.get("regime_cells_active", 0)
                             + l2_summary.get("asset_cells_active", 0))
            l2_partial    = (l2_summary.get("regime_cells_partial", 0)
                             + l2_summary.get("asset_cells_partial", 0))
            l2_warming    = (l2_summary.get("regime_cells_warming", 0)
                             + l2_summary.get("asset_cells_warming", 0))

            if active_count > 0 or l2_active > 0:
                partial_note = f"  {l2_partial} partial." if l2_partial else ""
                self._status_lbl.setText(
                    f"✅  L1+L2 Learning ACTIVE — L1: {active_count} model(s) active, "
                    f"{warming_count} warming.  L2: {l2_active} cells active, "
                    f"{l2_partial} partial, {l2_warming} warming.  "
                    f"{total_wired}/{len(closed_trades)} trades recorded."
                )
                self._status_lbl.setStyleSheet(
                    f"background:#0D2010; border:1px solid {_C_GREEN}; "
                    f"border-radius:5px; color:{_C_TEXT}; font-size:13px; padding:8px 12px;"
                )
            elif l2_partial > 0 or warming_count > 0 or l2_warming > 0:
                self._status_lbl.setText(
                    f"◑  Learning PARTIALLY ACTIVE — L1: {warming_count} model(s) accumulating.  "
                    f"L2: {l2_partial} partial (5–9 trades), {l2_warming} warming.  "
                    f"{total_wired}/{len(closed_trades)} trades recorded."
                )
                self._status_lbl.setStyleSheet(
                    f"background:#1A1400; border:1px solid {_C_YELLOW}; "
                    f"border-radius:5px; color:{_C_TEXT}; font-size:13px; padding:8px 12px;"
                )
            else:
                self._status_lbl.setText(
                    "⏸  Learning loop INACTIVE — complete demo trades to activate L1 + L2 adaptive weighting."
                )
                self._status_lbl.setStyleSheet(
                    f"background:#0D1320; border:1px solid {_C_BORDER}; "
                    f"border-radius:5px; color:{_C_MUTED}; font-size:13px; padding:8px 12px;"
                )
        except Exception as exc:
            self._status_lbl.setText(f"Learning loop status unavailable: {exc}")
            logger.warning("LearningTab status refresh error: %s", exc)


# ─────────────────────────────────────────────────────────────
# Tab 8: Demo Readiness Panel
# ─────────────────────────────────────────────────────────────
class _ReadinessTab(QWidget):
    def __init__(self):
        super().__init__()
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet(f"QScrollArea {{ background:{_C_BG}; border:none; }}")

        inner = QWidget()
        inner.setStyleSheet(f"background:{_C_BG};")
        v = QVBoxLayout(inner)
        v.setContentsMargins(12, 12, 12, 12)
        v.setSpacing(12)

        # Verdict banner
        self._verdict_lbl = QLabel("Awaiting evaluation…")
        self._verdict_lbl.setStyleSheet(
            f"background:#0D1320; border:1px solid {_C_BORDER}; "
            f"border-radius:6px; color:{_C_TEXT}; font-size:20px; "
            f"font-weight:700; padding:14px 20px;"
        )
        self._verdict_lbl.setWordWrap(True)
        v.addWidget(self._verdict_lbl)

        # Score bar
        score_row = QHBoxLayout()
        score_lbl = QLabel("Readiness Score:")
        score_lbl.setStyleSheet(f"color:{_C_MUTED}; font-size:13px; font-weight:600;")
        score_row.addWidget(score_lbl)
        self._score_bar = QProgressBar()
        self._score_bar.setRange(0, 100)
        self._score_bar.setValue(0)
        self._score_bar.setStyleSheet(
            "QProgressBar { background:#0A0E1A; border:1px solid #1A2332; "
            "border-radius:4px; height:18px; text-align:center; "
            "color:#E8EBF0; font-size:13px; font-weight:700; }"
            "QProgressBar::chunk { background:#1E90FF; border-radius:3px; }"
        )
        score_row.addWidget(self._score_bar, 1)
        self._score_val = QLabel("0/100")
        self._score_val.setStyleSheet(f"color:{_C_TEXT}; font-size:14px; font-weight:700;")
        score_row.addWidget(self._score_val)
        v.addLayout(score_row)

        # Explanation
        self._explanation = QTextEdit()
        self._explanation.setReadOnly(True)
        self._explanation.setStyleSheet(
            f"QTextEdit {{ background:#0A0E1A; color:{_C_TEXT}; "
            f"border:1px solid {_C_BORDER}; font-size:13px; "
            f"border-radius:5px; padding:8px; }}"
        )
        self._explanation.setMinimumHeight(90)
        self._explanation.setMaximumHeight(160)
        v.addWidget(self._explanation)

        # Safety note
        safety = QLabel(
            "⚠  SAFETY — NexusTrader cannot and will not automatically switch to live trading.  "
            "This panel presents evidence only.  The final decision requires manual user approval."
        )
        safety.setStyleSheet(
            f"background:#14100A; border:1px solid {_C_ORANGE}; "
            f"border-radius:5px; color:{_C_ORANGE}; font-size:12px; padding:8px 12px;"
        )
        safety.setWordWrap(True)
        v.addWidget(safety)

        # Checks table
        v.addWidget(_section_label("Individual Check Results"))
        _CHECK_COLS = ["#", "Check", "Threshold", "Actual", "Weight", "Result"]
        self._checks_table = _make_table(_CHECK_COLS, stretch_col=1)
        self._checks_table.setMinimumHeight(300)
        v.addWidget(self._checks_table, 1)

        # Key metrics snapshot
        v.addWidget(_section_label("Key Metrics Snapshot"))
        _MET_COLS = ["Metric", "Value"]
        self._met_table = _make_table(_MET_COLS, stretch_col=0)
        v.addWidget(self._met_table)

        # Timestamp
        self._ts_lbl = QLabel("")
        self._ts_lbl.setStyleSheet(f"color:{_C_MUTED}; font-size:12px;")
        v.addWidget(self._ts_lbl)

        scroll.setWidget(inner)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(scroll)

    def refresh(self, closed_trades: list[dict]):
        try:
            from core.evaluation.demo_performance_evaluator import get_evaluator
            assessment = get_evaluator().evaluate(closed_trades)
        except Exception as exc:
            self._verdict_lbl.setText(f"Evaluation failed: {exc}")
            logger.warning("ReadinessTab: evaluator error: %s", exc)
            return

        # Verdict banner
        color, icon = _STATUS_COLORS.get(
            assessment.status, (_C_MUTED, "?")
        )
        status_pretty = assessment.status.replace("_", " ")
        self._verdict_lbl.setText(f"{icon}  {status_pretty}")
        self._verdict_lbl.setStyleSheet(
            f"background:{'#0D2010' if color == _C_GREEN else '#140800' if color == _C_YELLOW else '#14000A'}; "
            f"border:2px solid {color}; "
            f"border-radius:6px; color:{color}; font-size:20px; "
            f"font-weight:700; padding:14px 20px;"
        )

        # Score bar
        s = assessment.score
        self._score_bar.setValue(s)
        bar_chunk_color = _C_GREEN if s >= 80 else (_C_YELLOW if s >= 55 else _C_RED)
        self._score_bar.setStyleSheet(
            "QProgressBar { background:#0A0E1A; border:1px solid #1A2332; "
            "border-radius:4px; height:18px; text-align:center; "
            "color:#E8EBF0; font-size:13px; font-weight:700; }"
            f"QProgressBar::chunk {{ background:{bar_chunk_color}; border-radius:3px; }}"
        )
        self._score_val.setText(
            f"{s}/100  ({assessment.checks_passed}/{assessment.checks_total} checks passed)"
        )
        self._score_val.setStyleSheet(
            f"color:{bar_chunk_color}; font-size:14px; font-weight:700;"
        )

        # Explanation
        self._explanation.setPlainText(assessment.explanation)

        # Checks table
        self._checks_table.setSortingEnabled(False)
        self._checks_table.setRowCount(0)
        for i, chk in enumerate(assessment.check_details, 1):
            row = self._checks_table.rowCount()
            self._checks_table.insertRow(row)
            wt_star = "★" * chk.weight
            res_txt = "✅ PASS" if chk.passed else "❌ FAIL"
            res_col = _C_GREEN if chk.passed else _C_RED
            self._checks_table.setItem(row, 0, _ci(str(i), _C_MUTED))
            self._checks_table.setItem(row, 1, _ci(chk.name, _C_TEXT, Qt.AlignLeft))
            self._checks_table.setItem(row, 2, _ci(chk.threshold, _C_MUTED))
            self._checks_table.setItem(row, 3, _ci(chk.actual,
                _C_GREEN if chk.passed else _C_RED))
            self._checks_table.setItem(row, 4, _ci(wt_star, _C_YELLOW))
            self._checks_table.setItem(row, 5, _ci(res_txt, res_col))
        self._checks_table.setSortingEnabled(True)

        # Metrics snapshot
        self._met_table.setSortingEnabled(False)
        self._met_table.setRowCount(0)
        met = assessment.metrics
        snapshot = [
            ("Trade count",           str(met.get("trade_count", "—"))),
            ("Win rate",              f"{met.get('win_rate_pct', 0):.1f}%"),
            ("Profit factor",         f"{met.get('profit_factor', 0):.2f}"),
            ("Total P&L",             f"{'+'if met.get('total_pnl_usdt',0)>=0 else'-'}${abs(met.get('total_pnl_usdt',0)):.2f}"),
            ("Avg R:R",               f"{met.get('avg_rr', 0):.2f}"),
            ("Max drawdown",          f"{met.get('max_drawdown_pct', 0):.2f}%"),
            ("Rolling drawdown",      f"{met.get('rolling_dd_pct', 0):.2f}%"),
            ("Regimes covered",       str(len(met.get("regimes_covered", [])))),
            ("Assets covered",        str(len(met.get("assets_covered", [])))),
            ("Trades per day",        f"{met.get('trades_per_day', 0):.1f}"),
            ("Span (days)",           f"{met.get('span_days', 0):.1f}"),
            ("Avg slippage",          f"{met.get('avg_slippage_pct', 0):.4f}%"),
            ("Learning models active", str(met.get("learning_models", 0))),
        ]
        for key, val in snapshot:
            row = self._met_table.rowCount()
            self._met_table.insertRow(row)
            self._met_table.setItem(row, 0, _ci(key, _C_MUTED, Qt.AlignLeft))
            self._met_table.setItem(row, 1, _ci(val, _C_TEXT))

        self._ts_lbl.setText(f"Last evaluated: {assessment.generated_at}")


# ─────────────────────────────────────────────────────────────
# Tab 9: Edge Analysis (EdgeEvaluator)
# ─────────────────────────────────────────────────────────────

_EDGE_STATUS_COLORS = {
    "NOT_READY":         (_C_RED,    "⛔"),
    "NEEDS_IMPROVEMENT": (_C_YELLOW, "⚠"),
    "READY_FOR_LIVE":    (_C_GREEN,  "✅"),
}

_EDGE_OVERVIEW_COLS  = ["Metric", "Value"]
_EDGE_CHECKS_COLS    = ["Check", "Result"]
_EDGE_CONTEXT_COLS   = ["Dimension", "Trades", "Win Rate", "Avg Win R", "Avg Loss R", "Expectancy R", "Edge"]
_EDGE_BUCKET_COLS    = ["Score Bucket", "Trades", "Win Rate", "Avg R", "Expectancy R"]
_EDGE_PF_LABEL_COLORS = {
    "Stable":             _C_GREEN,
    "Moderate":           _C_YELLOW,
    "Unstable":           _C_RED,
    "Insufficient data":  _C_MUTED,
}
_EDGE_LABEL_COLORS = {
    "Strong":    _C_GREEN,
    "Meaningful": _C_GREEN,
    "Weak":      _C_YELLOW,
    "Marginal":  _C_YELLOW,
    "Losing":    _C_RED,
}


class _EdgeTab(QWidget):
    """
    Edge Analysis tab — displays EdgeEvaluator output across five sub-panels:
      Overview  ·  R Over Time  ·  Rolling PF  ·  By Context  ·  Score Buckets
    """

    def __init__(self):
        super().__init__()
        v = QVBoxLayout(self)
        v.setContentsMargins(12, 12, 12, 12)
        v.setSpacing(10)

        # Verdict banner
        self._verdict_lbl = QLabel("Edge evaluation will appear once trades are closed.")
        self._verdict_lbl.setStyleSheet(
            f"background:#0D1320; border:1px solid #1A2332; "
            f"border-radius:5px; color:{_C_TEXT}; "
            f"font-size:13px; padding:8px 12px;"
        )
        self._verdict_lbl.setWordWrap(True)
        v.addWidget(self._verdict_lbl)

        # Sub-tabs
        self._sub_tabs = QTabWidget()
        self._sub_tabs.setStyleSheet(
            f"QTabWidget::pane {{ border:1px solid {_C_BORDER}; background:{_C_BG}; }}"
            f"QTabBar::tab {{ background:#0D1320; color:{_C_MUTED}; padding:5px 12px; "
            f"border:1px solid {_C_BORDER}; }}"
            f"QTabBar::tab:selected {{ background:#101C30; color:{_C_TEXT}; }}"
        )
        v.addWidget(self._sub_tabs, 1)

        self._build_overview_tab()
        self._build_r_over_time_tab()
        self._build_rolling_pf_tab()
        self._build_by_context_tab()
        self._build_score_buckets_tab()

    # ── Sub-tab builders ───────────────────────────────────────────────────

    def _build_overview_tab(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(8)

        # Stat cards grid
        grid_frame = QFrame()
        grid_lay = QGridLayout(grid_frame)
        grid_lay.setContentsMargins(0, 0, 0, 0)
        grid_lay.setSpacing(8)

        self._e_expectancy   = _StatCard("EXPECTANCY (R)",   "—")
        self._e_pf_overall   = _StatCard("PROFIT FACTOR",    "—")
        self._e_pfs          = _StatCard("PF STABILITY",     "—", _C_MUTED)
        self._e_win_rate     = _StatCard("WIN RATE",         "—")
        self._e_avg_win_r    = _StatCard("AVG WIN R",        "—", _C_GREEN)
        self._e_avg_loss_r   = _StatCard("AVG LOSS R",       "—", _C_RED)
        self._e_drawdown_r   = _StatCard("DRAWDOWN (R)",     "0.00R", _C_GREEN)
        self._e_edge_label   = _StatCard("EDGE LABEL",       "—", _C_MUTED)

        cards = [
            self._e_expectancy, self._e_pf_overall, self._e_pfs,  self._e_edge_label,
            self._e_win_rate,   self._e_avg_win_r,  self._e_avg_loss_r, self._e_drawdown_r,
        ]
        for i, card in enumerate(cards):
            card.setStyleSheet(
                f"background:#0D1320; border:1px solid #1A2332; border-radius:6px;"
            )
            grid_lay.addWidget(card, i // 4, i % 4)
        lay.addWidget(grid_frame)

        # Checks passed/failed
        chk_h = QHBoxLayout()
        chk_h.setSpacing(12)

        passed_frame = QFrame()
        pf_v = QVBoxLayout(passed_frame)
        pf_v.setContentsMargins(0, 0, 0, 0)
        pf_v.setSpacing(4)
        pf_v.addWidget(_section_label("✅  Checks Passed"))
        self._chk_pass_tbl = _make_table(_EDGE_CHECKS_COLS, stretch_col=0)
        self._chk_pass_tbl.setMaximumHeight(200)
        pf_v.addWidget(self._chk_pass_tbl)
        chk_h.addWidget(passed_frame, 1)

        failed_frame = QFrame()
        ff_v = QVBoxLayout(failed_frame)
        ff_v.setContentsMargins(0, 0, 0, 0)
        ff_v.setSpacing(4)
        ff_v.addWidget(_section_label("❌  Checks Failed"))
        self._chk_fail_tbl = _make_table(_EDGE_CHECKS_COLS, stretch_col=0)
        self._chk_fail_tbl.setMaximumHeight(200)
        ff_v.addWidget(self._chk_fail_tbl)
        chk_h.addWidget(failed_frame, 1)

        lay.addLayout(chk_h)

        # Explanation text
        self._explanation_lbl = QLabel("")
        self._explanation_lbl.setStyleSheet(
            f"background:#080E1C; border:1px solid #141E2E; border-radius:4px; "
            f"color:{_C_MUTED}; font-size:12px; padding:8px 12px;"
        )
        self._explanation_lbl.setWordWrap(True)
        lay.addWidget(self._explanation_lbl)
        lay.addStretch()

        self._sub_tabs.addTab(w, "Overview")

    def _build_r_over_time_tab(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(8)

        lay.addWidget(_section_label("  Cumulative R (all trades)"))
        self._cum_r_plot = pg.PlotWidget()
        self._cum_r_plot.setBackground(_C_BG)
        self._cum_r_plot.showGrid(x=True, y=True, alpha=0.12)
        self._cum_r_plot.getAxis("left").setTextPen(pg.mkPen(_C_MUTED))
        self._cum_r_plot.getAxis("bottom").setTextPen(pg.mkPen(_C_MUTED))
        self._cum_r_plot.setLabel("left",   "Cumulative R", color=_C_MUTED)
        self._cum_r_plot.setLabel("bottom", "Trade #",      color=_C_MUTED)
        self._cum_r_plot.setMinimumHeight(180)
        lay.addWidget(self._cum_r_plot, 2)

        lay.addWidget(_section_label("  Rolling-20 Expectancy (R)"))
        self._roll_exp_plot = pg.PlotWidget()
        self._roll_exp_plot.setBackground(_C_BG)
        self._roll_exp_plot.showGrid(x=True, y=True, alpha=0.12)
        self._roll_exp_plot.getAxis("left").setTextPen(pg.mkPen(_C_MUTED))
        self._roll_exp_plot.getAxis("bottom").setTextPen(pg.mkPen(_C_MUTED))
        self._roll_exp_plot.setLabel("left",   "E[R]",    color=_C_MUTED)
        self._roll_exp_plot.setLabel("bottom", "Trade #", color=_C_MUTED)
        self._roll_exp_plot.setMinimumHeight(140)
        lay.addWidget(self._roll_exp_plot, 1)

        self._sub_tabs.addTab(w, "R Over Time")

    def _build_rolling_pf_tab(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(8)

        # PFS indicator row
        pfs_row = QHBoxLayout()
        pfs_row.addWidget(_section_label("  PF Stability:"))
        self._pfs_indicator = QLabel("—")
        self._pfs_indicator.setStyleSheet(
            f"font-size:14px; font-weight:700; color:{_C_MUTED}; padding:2px 8px;"
        )
        pfs_row.addWidget(self._pfs_indicator)
        pfs_row.addStretch()
        self._pfs_score_lbl = QLabel("")
        self._pfs_score_lbl.setStyleSheet(f"font-size:13px; color:{_C_MUTED};")
        pfs_row.addWidget(self._pfs_score_lbl)
        lay.addLayout(pfs_row)

        self._pf_plot = pg.PlotWidget()
        self._pf_plot.setBackground(_C_BG)
        self._pf_plot.showGrid(x=True, y=True, alpha=0.12)
        self._pf_plot.getAxis("left").setTextPen(pg.mkPen(_C_MUTED))
        self._pf_plot.getAxis("bottom").setTextPen(pg.mkPen(_C_MUTED))
        self._pf_plot.setLabel("left",   "Profit Factor", color=_C_MUTED)
        self._pf_plot.setLabel("bottom", "Trade #",       color=_C_MUTED)
        self._pf_plot.setMinimumHeight(260)
        lay.addWidget(self._pf_plot, 1)

        # Legend
        legend_lbl = QLabel(
            f'<span style="color:{_C_BLUE};">━━</span>  Rolling-20 PF'
            f'  &nbsp;&nbsp;'
            f'<span style="color:{_C_PURPLE};">━━</span>  Rolling-40 PF'
            f'  &nbsp;&nbsp;'
            f'<span style="color:{_C_BORDER};">- -</span>  PF = 1.35 / 1.40 targets'
        )
        legend_lbl.setStyleSheet(f"color:{_C_MUTED}; font-size:12px;")
        lay.addWidget(legend_lbl)

        self._sub_tabs.addTab(w, "Rolling PF")

    def _build_by_context_tab(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(6)

        ctx_tabs = QTabWidget()
        ctx_tabs.setStyleSheet(
            f"QTabWidget::pane {{ border:1px solid {_C_BORDER}; background:{_C_BG}; }}"
            f"QTabBar::tab {{ background:#0A0E1A; color:{_C_MUTED}; padding:4px 10px; "
            f"border:1px solid {_C_BORDER}; }}"
            f"QTabBar::tab:selected {{ background:{_C_CARD}; color:{_C_TEXT}; }}"
        )

        # By Regime
        self._ctx_regime_tbl = _make_table(_EDGE_CONTEXT_COLS, stretch_col=0)
        regime_w = QWidget()
        regime_v = QVBoxLayout(regime_w)
        regime_v.setContentsMargins(4, 4, 4, 4)
        regime_v.addWidget(self._ctx_regime_tbl)
        ctx_tabs.addTab(regime_w, "By Regime")

        # By Model
        self._ctx_model_tbl = _make_table(_EDGE_CONTEXT_COLS, stretch_col=0)
        model_w = QWidget()
        model_v = QVBoxLayout(model_w)
        model_v.setContentsMargins(4, 4, 4, 4)
        model_v.addWidget(self._ctx_model_tbl)
        ctx_tabs.addTab(model_w, "By Model")

        # By Asset
        self._ctx_asset_tbl = _make_table(_EDGE_CONTEXT_COLS, stretch_col=0)
        asset_w = QWidget()
        asset_v = QVBoxLayout(asset_w)
        asset_v.setContentsMargins(4, 4, 4, 4)
        asset_v.addWidget(self._ctx_asset_tbl)
        ctx_tabs.addTab(asset_w, "By Asset")

        lay.addWidget(ctx_tabs)
        self._sub_tabs.addTab(w, "By Context")

    def _build_score_buckets_tab(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(6)

        explain = QLabel(
            "Score calibration: purely diagnostic.  Shows win-rate and expectancy "
            "per confluence-score bucket.  Well-calibrated systems have higher win-rates "
            "in the 0.80–1.00 buckets.  Does NOT influence model weights."
        )
        explain.setStyleSheet(f"color:{_C_MUTED}; font-size:12px;")
        explain.setWordWrap(True)
        lay.addWidget(explain)

        self._bucket_tbl = _make_table(_EDGE_BUCKET_COLS, stretch_col=0)
        lay.addWidget(self._bucket_tbl)
        lay.addStretch()

        self._sub_tabs.addTab(w, "Score Buckets")

    # ── refresh ───────────────────────────────────────────────────────────────

    def refresh(self, closed_trades: list[dict]):
        try:
            from core.evaluation.edge_evaluator import get_edge_evaluator
            ea = get_edge_evaluator().evaluate(closed_trades)
        except Exception as exc:
            logger.warning("_EdgeTab: evaluation failed — %s", exc)
            return

        self._refresh_verdict_banner(ea)
        self._refresh_overview(ea)
        self._refresh_r_over_time(ea)
        self._refresh_rolling_pf(ea)
        self._refresh_by_context(ea)
        self._refresh_score_buckets(ea)

    def _refresh_verdict_banner(self, ea):
        col, icon = _EDGE_STATUS_COLORS.get(ea.verdict, (_C_MUTED, "?"))
        text = (
            f"{icon}  Edge Verdict: <b>{ea.verdict.replace('_', ' ')}</b>"
            f"  |  Score: {ea.score}/100"
            f"  |  Trades: {ea.trade_count}"
        )
        self._verdict_lbl.setText(text)
        self._verdict_lbl.setStyleSheet(
            f"background:#0D1320; border:1px solid {col}; "
            f"border-radius:5px; color:{_C_TEXT}; "
            f"font-size:13px; padding:8px 12px;"
        )

    def _refresh_overview(self, ea):
        exp = ea.overall_expectancy
        pf  = ea.profit_factor_metrics

        if exp:
            exp_col = _C_GREEN if exp.expectancy_r > 0.10 else (
                _C_YELLOW if exp.expectancy_r > 0 else _C_RED
            )
            self._e_expectancy.set(f"{exp.expectancy_r:+.3f}R", exp_col)
            wr_col = _C_GREEN if exp.win_rate >= 0.55 else (
                _C_YELLOW if exp.win_rate >= 0.45 else _C_RED
            )
            self._e_win_rate.set(f"{exp.win_rate*100:.1f}%", wr_col)
            self._e_avg_win_r.set(f"+{exp.avg_win_r:.3f}R", _C_GREEN)
            self._e_avg_loss_r.set(f"−{exp.avg_loss_r:.3f}R", _C_RED)
            edge_col = _EDGE_LABEL_COLORS.get(exp.edge_label, _C_MUTED)
            self._e_edge_label.set(exp.edge_label, edge_col)
        else:
            for card in (
                self._e_expectancy, self._e_win_rate,
                self._e_avg_win_r, self._e_avg_loss_r, self._e_edge_label,
            ):
                card.set("—", _C_MUTED)

        pf_col = _C_GREEN if pf.overall >= 1.40 else (
            _C_YELLOW if pf.overall >= 1.0 else _C_RED
        ) if pf.overall > 0 else _C_MUTED
        self._e_pf_overall.set(f"{pf.overall:.2f}", pf_col)

        pfs_col = _EDGE_PF_LABEL_COLORS.get(pf.pfs_label, _C_MUTED)
        self._e_pfs.set(f"{pf.pfs_label} ({pf.pfs_score:.0f})", pfs_col)

        dd_col = _C_RED if ea.drawdown_r >= 10 else (
            _C_YELLOW if ea.drawdown_r >= 5 else _C_GREEN
        )
        self._e_drawdown_r.set(f"{ea.drawdown_r:.2f}R", dd_col if ea.trade_count else _C_GREEN)

        # Checks passed
        self._chk_pass_tbl.setSortingEnabled(False)
        self._chk_pass_tbl.setRowCount(0)
        for chk in ea.checks_passed:
            r = self._chk_pass_tbl.rowCount()
            self._chk_pass_tbl.insertRow(r)
            self._chk_pass_tbl.setItem(r, 0, _ci(chk, _C_TEXT, Qt.AlignLeft))
            self._chk_pass_tbl.setItem(r, 1, _ci("✅", _C_GREEN))
        self._chk_pass_tbl.setSortingEnabled(True)

        # Checks failed
        self._chk_fail_tbl.setSortingEnabled(False)
        self._chk_fail_tbl.setRowCount(0)
        for chk in ea.checks_failed:
            r = self._chk_fail_tbl.rowCount()
            self._chk_fail_tbl.insertRow(r)
            self._chk_fail_tbl.setItem(r, 0, _ci(chk, _C_TEXT, Qt.AlignLeft))
            self._chk_fail_tbl.setItem(r, 1, _ci("❌", _C_RED))
        self._chk_fail_tbl.setSortingEnabled(True)

        self._explanation_lbl.setText(ea.explanation)

    def _refresh_r_over_time(self, ea):
        self._cum_r_plot.clear()
        self._roll_exp_plot.clear()

        if not ea.cumulative_r_history:
            return

        xs_cum = list(range(1, len(ea.cumulative_r_history) + 1))
        cum_col = _C_GREEN if ea.cumulative_r_history[-1] >= 0 else _C_RED
        pen = pg.mkPen(color=cum_col, width=2)
        self._cum_r_plot.plot(xs_cum, ea.cumulative_r_history, pen=pen)
        self._cum_r_plot.addItem(
            pg.InfiniteLine(pos=0, angle=0, pen=pg.mkPen("#2A3A52", width=1, style=Qt.DashLine))
        )

        if ea.rolling_20_exp_history:
            # x-axis starts at trade #20 (index 19 → label 20)
            xs_exp = list(range(20, 20 + len(ea.rolling_20_exp_history)))
            zero_line_color = pg.mkPen("#2A3A52", width=1, style=Qt.DashLine)
            self._roll_exp_plot.plot(
                xs_exp, ea.rolling_20_exp_history,
                pen=pg.mkPen(color=_C_BLUE, width=2),
            )
            self._roll_exp_plot.addItem(
                pg.InfiniteLine(pos=0, angle=0, pen=zero_line_color)
            )
            threshold_pen = pg.mkPen(_C_GREEN, width=1, style=Qt.DashLine)
            self._roll_exp_plot.addItem(
                pg.InfiniteLine(pos=0.25, angle=0, pen=threshold_pen)
            )

    def _refresh_rolling_pf(self, ea):
        self._pf_plot.clear()
        pf = ea.profit_factor_metrics

        # PFS indicator
        pfs_col = _EDGE_PF_LABEL_COLORS.get(pf.pfs_label, _C_MUTED)
        self._pfs_indicator.setText(pf.pfs_label)
        self._pfs_indicator.setStyleSheet(
            f"font-size:14px; font-weight:700; color:{pfs_col}; padding:2px 8px;"
        )
        cv_text = f"  CV={pf.pfs_cv:.3f}" if pf.pfs_cv > 0 else ""
        self._pfs_score_lbl.setText(f"PFS Score: {pf.pfs_score:.0f}/100{cv_text}")

        if not pf.rolling_20_history and not pf.rolling_40_history:
            return

        # Rolling-20 line
        if pf.rolling_20_history:
            xs_20 = list(range(20, 20 + len(pf.rolling_20_history)))
            self._pf_plot.plot(
                xs_20, pf.rolling_20_history,
                pen=pg.mkPen(color=_C_BLUE, width=2),
            )

        # Rolling-40 line
        if pf.rolling_40_history:
            xs_40 = list(range(40, 40 + len(pf.rolling_40_history)))
            self._pf_plot.plot(
                xs_40, pf.rolling_40_history,
                pen=pg.mkPen(color=_C_PURPLE, width=2),
            )

        # Baseline PF = 1.0
        self._pf_plot.addItem(
            pg.InfiniteLine(pos=1.0, angle=0, pen=pg.mkPen("#2A3A52", width=1))
        )
        # Target lines
        dash = Qt.DashLine
        self._pf_plot.addItem(
            pg.InfiniteLine(pos=1.35, angle=0, pen=pg.mkPen(_C_YELLOW, width=1, style=dash))
        )
        self._pf_plot.addItem(
            pg.InfiniteLine(pos=1.40, angle=0, pen=pg.mkPen(_C_GREEN, width=1, style=dash))
        )

    def _refresh_by_context(self, ea):
        datasets = [
            (self._ctx_regime_tbl, ea.expectancy_by_regime),
            (self._ctx_model_tbl,  ea.expectancy_by_model),
            (self._ctx_asset_tbl,  ea.expectancy_by_asset),
        ]
        for tbl, mapping in datasets:
            tbl.setSortingEnabled(False)
            tbl.setRowCount(0)
            for dim, em in sorted(mapping.items(), key=lambda x: -x[1].trade_count):
                r = tbl.rowCount()
                tbl.insertRow(r)
                wr_pct = f"{em.win_rate * 100:.1f}%"
                wr_col = _C_GREEN if em.win_rate >= 0.55 else (
                    _C_YELLOW if em.win_rate >= 0.45 else _C_RED
                )
                exp_col = _C_GREEN if em.expectancy_r > 0.10 else (
                    _C_YELLOW if em.expectancy_r > 0 else _C_RED
                )
                edge_col = _EDGE_LABEL_COLORS.get(em.edge_label, _C_MUTED)
                tbl.setItem(r, 0, _ci(dim.upper(),                 _C_TEXT,  Qt.AlignLeft))
                tbl.setItem(r, 1, _ni(em.trade_count, str(em.trade_count)))
                tbl.setItem(r, 2, _ni(em.win_rate,    wr_pct,  wr_col))
                tbl.setItem(r, 3, _ni(em.avg_win_r,   f"+{em.avg_win_r:.3f}R",    _C_GREEN))
                tbl.setItem(r, 4, _ni(em.avg_loss_r,  f"−{em.avg_loss_r:.3f}R",   _C_RED))
                tbl.setItem(r, 5, _ni(em.expectancy_r, f"{em.expectancy_r:+.3f}R", exp_col))
                tbl.setItem(r, 6, _ci(em.edge_label,  edge_col))
            tbl.setSortingEnabled(True)

    def _refresh_score_buckets(self, ea):
        self._bucket_tbl.setSortingEnabled(False)
        self._bucket_tbl.setRowCount(0)
        for label, bm in sorted(ea.score_calibration.items()):
            r = self._bucket_tbl.rowCount()
            self._bucket_tbl.insertRow(r)
            wr_col = _C_GREEN if bm.win_rate >= 0.55 else (
                _C_YELLOW if bm.win_rate >= 0.45 else _C_RED
            )
            exp_col = _C_GREEN if bm.expectancy_r > 0.10 else (
                _C_YELLOW if bm.expectancy_r > 0 else _C_RED
            )
            self._bucket_tbl.setItem(r, 0, _ci(label, _C_TEXT, Qt.AlignLeft))
            self._bucket_tbl.setItem(r, 1, _ni(bm.trade_count, str(bm.trade_count)))
            self._bucket_tbl.setItem(r, 2, _ni(bm.win_rate,    f"{bm.win_rate*100:.1f}%", wr_col))
            self._bucket_tbl.setItem(r, 3, _ni(bm.avg_r,       f"{bm.avg_r:+.3f}R",
                                               _C_GREEN if bm.avg_r > 0 else _C_RED))
            self._bucket_tbl.setItem(r, 4, _ni(bm.expectancy_r, f"{bm.expectancy_r:+.3f}R", exp_col))
        self._bucket_tbl.setSortingEnabled(True)


# ─────────────────────────────────────────────────────────────
# Shared table factory
# ─────────────────────────────────────────────────────────────
def _make_table(cols: list[str], stretch_col: int = 0) -> QTableWidget:
    tbl = QTableWidget(0, len(cols))
    tbl.setHorizontalHeaderLabels(cols)
    tbl.setStyleSheet(_TABLE_STYLE)
    tbl.setSelectionBehavior(QAbstractItemView.SelectRows)
    tbl.setAlternatingRowColors(True)
    tbl.setEditTriggers(QAbstractItemView.NoEditTriggers)
    tbl.setSortingEnabled(True)
    tbl.verticalHeader().setVisible(False)
    tbl.horizontalHeader().setSectionResizeMode(stretch_col, QHeaderView.Stretch)
    for i in range(len(cols)):
        if i != stretch_col:
            tbl.horizontalHeader().setSectionResizeMode(
                i, QHeaderView.ResizeToContents
            )
    return tbl


# ─────────────────────────────────────────────────────────────
# Performance Analytics Page
# ─────────────────────────────────────────────────────────────
# ─────────────────────────────────────────────────────────────
# 🔬  Validation Summary Tab (Session 23)
# ─────────────────────────────────────────────────────────────
class _ValidationTab(QWidget):
    """
    Consolidated validation dashboard.  Shows:
      1. System Readiness level (STILL_LEARNING / IMPROVING / READY)
      2. Per-model expectancy table (from ModelPerformanceTracker)
      3. Per-regime performance (top 6 regimes)
      4. Filter effectiveness summary (from FilterStatsTracker)
      5. OI/Liq impact (fires + score delta)
      6. Calibrator status (AUC, Brier, drift)
      7. Correlation dampening activity
    """

    def __init__(self):
        super().__init__()
        self._build()

    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 14, 16, 14)
        root.setSpacing(14)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet("QScrollArea { border:none; background:transparent; }")
        inner = QWidget()
        inner.setStyleSheet(f"background:{_C_BG};")
        v = QVBoxLayout(inner)
        v.setSpacing(14)
        v.setContentsMargins(0, 0, 0, 0)

        # ── 1. System Readiness banner ──────────────────────────────
        self._readiness_banner = QLabel("System Readiness: —")
        self._readiness_banner.setAlignment(Qt.AlignCenter)
        self._readiness_banner.setStyleSheet(
            f"font-size:16px; font-weight:700; padding:12px; "
            f"border-radius:6px; background:{_C_CARD}; color:{_C_TEXT};"
        )
        v.addWidget(self._readiness_banner)

        self._readiness_action = QLabel("")
        self._readiness_action.setAlignment(Qt.AlignCenter)
        self._readiness_action.setWordWrap(True)
        self._readiness_action.setStyleSheet(f"color:{_C_MUTED}; font-size:13px; padding:4px 0 8px 0;")
        v.addWidget(self._readiness_action)

        # ── 2 + 3. Side-by-side: Model Expectancy + Regime Performance ──
        row1 = QHBoxLayout()
        row1.setSpacing(14)

        # Per-model expectancy table
        model_frame = QFrame()
        model_frame.setStyleSheet(_CARD_STYLE)
        mf_v = QVBoxLayout(model_frame)
        mf_v.setContentsMargins(12, 10, 12, 10)
        mf_v.addWidget(_section_label("Model Expectancy (rolling 50 trades)"))
        self._model_tbl = QTableWidget(0, 5)
        self._model_tbl.setHorizontalHeaderLabels(["Model", "Trades", "WR", "E[R]", "PF"])
        self._model_tbl.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self._model_tbl.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._model_tbl.setAlternatingRowColors(True)
        self._model_tbl.setStyleSheet(
            f"QTableWidget {{ background:{_C_CARD}; color:{_C_TEXT}; "
            f"gridline-color:{_C_BORDER}; font-size:12px; border:none; }}"
            f"QHeaderView::section {{ background:#0A0E1A; color:{_C_MUTED}; "
            f"font-size:11px; font-weight:600; border:none; padding:4px; }}"
            f"QTableWidget::item:alternate {{ background:#0E1525; }}"
        )
        self._model_tbl.setMaximumHeight(200)
        mf_v.addWidget(self._model_tbl)
        row1.addWidget(model_frame, 1)

        # Per-regime table
        regime_frame = QFrame()
        regime_frame.setStyleSheet(_CARD_STYLE)
        rf_v = QVBoxLayout(regime_frame)
        rf_v.setContentsMargins(12, 10, 12, 10)
        rf_v.addWidget(_section_label("Regime Performance (top 6)"))
        self._regime_tbl = QTableWidget(0, 4)
        self._regime_tbl.setHorizontalHeaderLabels(["Regime", "Trades", "WR", "P&L $"])
        self._regime_tbl.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self._regime_tbl.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._regime_tbl.setAlternatingRowColors(True)
        self._regime_tbl.setStyleSheet(
            f"QTableWidget {{ background:{_C_CARD}; color:{_C_TEXT}; "
            f"gridline-color:{_C_BORDER}; font-size:12px; border:none; }}"
            f"QHeaderView::section {{ background:#0A0E1A; color:{_C_MUTED}; "
            f"font-size:11px; font-weight:600; border:none; padding:4px; }}"
            f"QTableWidget::item:alternate {{ background:#0E1525; }}"
        )
        self._regime_tbl.setMaximumHeight(200)
        rf_v.addWidget(self._regime_tbl)
        row1.addWidget(regime_frame, 1)

        v.addLayout(row1)

        # ── 4 + 5. Filter Stats + OI/Calibrator ─────────────────────────
        row2 = QHBoxLayout()
        row2.setSpacing(14)

        filter_frame = QFrame()
        filter_frame.setStyleSheet(_CARD_STYLE)
        ff_v = QVBoxLayout(filter_frame)
        ff_v.setContentsMargins(12, 10, 12, 10)
        ff_v.addWidget(_section_label("Filter Effectiveness"))
        self._filter_tbl = QTableWidget(0, 3)
        self._filter_tbl.setHorizontalHeaderLabels(["Filter", "Block Rate", "Top Blocked"])
        self._filter_tbl.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self._filter_tbl.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._filter_tbl.setStyleSheet(
            f"QTableWidget {{ background:{_C_CARD}; color:{_C_TEXT}; "
            f"gridline-color:{_C_BORDER}; font-size:12px; border:none; }}"
            f"QHeaderView::section {{ background:#0A0E1A; color:{_C_MUTED}; "
            f"font-size:11px; font-weight:600; border:none; padding:4px; }}"
        )
        self._filter_tbl.setMaximumHeight(150)
        ff_v.addWidget(self._filter_tbl)
        row2.addWidget(filter_frame, 1)

        # OI + Calibrator + Dampener info panel
        info_frame = QFrame()
        info_frame.setStyleSheet(_CARD_STYLE)
        if_v = QVBoxLayout(info_frame)
        if_v.setContentsMargins(12, 10, 12, 10)
        if_v.addWidget(_section_label("Signal Quality Diagnostics"))
        self._diag_text = QTextEdit()
        self._diag_text.setReadOnly(True)
        self._diag_text.setMaximumHeight(150)
        self._diag_text.setStyleSheet(
            f"QTextEdit {{ background:#0A0E1A; color:{_C_TEXT}; "
            f"font-size:12px; border:1px solid {_C_BORDER}; border-radius:4px; padding:6px; }}"
        )
        if_v.addWidget(self._diag_text)
        row2.addWidget(info_frame, 1)

        v.addLayout(row2)
        v.addStretch()
        scroll.setWidget(inner)
        root.addWidget(scroll)

    def refresh(self, closed: list[dict]) -> None:
        """Refresh all sections from live data."""
        try:
            self._refresh_readiness(closed)
        except Exception as exc:
            logger.debug("ValidationTab: readiness refresh error: %s", exc)
        try:
            self._refresh_model_table()
        except Exception as exc:
            logger.debug("ValidationTab: model table error: %s", exc)
        try:
            self._refresh_regime_table(closed)
        except Exception as exc:
            logger.debug("ValidationTab: regime table error: %s", exc)
        try:
            self._refresh_filter_table()
        except Exception as exc:
            logger.debug("ValidationTab: filter table error: %s", exc)
        try:
            self._refresh_diagnostics()
        except Exception as exc:
            logger.debug("ValidationTab: diagnostics error: %s", exc)

    # ── Refresh helpers ────────────────────────────────────────────────────

    def _refresh_readiness(self, closed: list[dict]) -> None:
        from core.evaluation.system_readiness_evaluator import (
            get_system_readiness_evaluator, SystemReadinessLevel,
        )
        assessment = get_system_readiness_evaluator().evaluate(closed)
        level = assessment.level

        _LEVEL_COLORS = {
            SystemReadinessLevel.STILL_LEARNING:          (_C_RED,    "🔴"),
            SystemReadinessLevel.IMPROVING:               (_C_YELLOW, "🟡"),
            SystemReadinessLevel.READY_FOR_CAUTIOUS_LIVE: (_C_GREEN,  "🟢"),
        }
        col, icon = _LEVEL_COLORS.get(level, (_C_MUTED, "?"))
        self._readiness_banner.setStyleSheet(
            f"font-size:16px; font-weight:700; padding:12px; border-radius:6px; "
            f"background:{_C_CARD}; color:{col};"
        )
        self._readiness_banner.setText(
            f"{icon}  {level.value.replace('_', ' ')}  (score {assessment.score:.0f}/100)"
        )
        self._readiness_action.setText(assessment.action)

    def _refresh_model_table(self) -> None:
        from core.analytics.model_performance_tracker import get_model_performance_tracker
        tracker = get_model_performance_tracker()
        all_stats = tracker.get_all_stats()

        rows = sorted(all_stats.items(), key=lambda x: x[1].get("trades", 0), reverse=True)
        self._model_tbl.setRowCount(len(rows))
        for r, (model, s) in enumerate(rows):
            trades = s.get("trades", 0)
            wr     = s.get("win_rate")
            exp_r  = s.get("expectancy_r")
            pf     = s.get("profit_factor")
            wr_str  = f"{wr:.0%}" if wr is not None else "—"
            exp_str = f"{exp_r:+.3f}R" if exp_r is not None else "—"
            pf_str  = f"{pf:.2f}" if pf is not None else "—"

            exp_col = _C_GREEN if (exp_r or 0) > 0 else (_C_RED if (exp_r or 0) < 0 else _C_MUTED)
            wr_col  = _C_GREEN if (wr or 0) >= 0.45 else (_C_RED if (wr or 0) < 0.40 and trades >= 20 else _C_TEXT)

            self._model_tbl.setItem(r, 0, _ci(model, _C_TEXT, Qt.AlignLeft))
            self._model_tbl.setItem(r, 1, _ci(str(trades)))
            self._model_tbl.setItem(r, 2, _ci(wr_str, wr_col))
            self._model_tbl.setItem(r, 3, _ci(exp_str, exp_col))
            self._model_tbl.setItem(r, 4, _ci(pf_str, _C_GREEN if (pf or 0) >= 1.10 else (_C_RED if (pf or 0) < 1.0 and pf is not None else _C_TEXT)))

    def _refresh_regime_table(self, closed: list[dict]) -> None:
        regime_data: dict[str, dict] = {}
        for t in closed:
            reg = (t.get("regime") or "unknown").lower()
            if reg not in regime_data:
                regime_data[reg] = {"trades": 0, "wins": 0, "pnl": 0.0}
            regime_data[reg]["trades"] += 1
            pnl_pct = float(t.get("pnl_pct") or 0)
            if pnl_pct > 0:
                regime_data[reg]["wins"] += 1
            regime_data[reg]["pnl"] += float(t.get("pnl_usdt") or 0)

        rows = sorted(regime_data.items(), key=lambda x: x[1]["trades"], reverse=True)[:6]
        self._regime_tbl.setRowCount(len(rows))
        for r, (reg, s) in enumerate(rows):
            trades = s["trades"]
            wr = s["wins"] / trades if trades > 0 else 0
            pnl = s["pnl"]
            wr_col  = _C_GREEN if wr >= 0.50 else (_C_RED if wr < 0.40 and trades >= 10 else _C_TEXT)
            pnl_col = _C_GREEN if pnl > 0 else (_C_RED if pnl < 0 else _C_MUTED)
            self._regime_tbl.setItem(r, 0, _ci(reg, _C_TEXT, Qt.AlignLeft))
            self._regime_tbl.setItem(r, 1, _ci(str(trades)))
            self._regime_tbl.setItem(r, 2, _ci(f"{wr:.0%}", wr_col))
            self._regime_tbl.setItem(r, 3, _ci(f"{'+'if pnl>=0 else'-'}${abs(pnl):,.2f}", pnl_col))

    def _refresh_filter_table(self) -> None:
        try:
            from core.analytics.filter_stats import get_filter_stats_tracker
            # get_all_summaries() returns list[dict] (each dict has a "filter" key)
            summaries: list = get_filter_stats_tracker().get_all_summaries()
        except Exception:
            summaries = []

        self._filter_tbl.setRowCount(len(summaries))
        for r, s in enumerate(summaries):
            fname      = s.get("filter", "—")
            block_rate = s.get("block_rate_pct") or 0.0
            # top_blocked_symbols is a list of (symbol, count) tuples
            top_blocked = s.get("top_blocked_symbols", [])
            top_str = ", ".join(sym for sym, _ in top_blocked[:2]) if top_blocked else "—"
            br_col = _C_YELLOW if block_rate > 50 else _C_TEXT
            self._filter_tbl.setItem(r, 0, _ci(fname, _C_TEXT, Qt.AlignLeft))
            self._filter_tbl.setItem(r, 1, _ci(f"{block_rate:.1f}%", br_col))
            self._filter_tbl.setItem(r, 2, _ci(top_str, _C_MUTED, Qt.AlignLeft))

    def _refresh_diagnostics(self) -> None:
        lines: list[str] = []

        # Calibrator status
        try:
            from core.learning.calibrator_monitor import get_calibrator_monitor
            status = get_calibrator_monitor().get_status()
            auc = status.get("auc")
            brier = status.get("brier")
            n_pred = status.get("prediction_count", 0)
            drift = status.get("drift_detected", False)
            auc_str   = f"{auc:.3f}" if auc is not None else "insufficient data"
            brier_str = f"{brier:.3f}" if brier is not None else "—"
            drift_str = "⚠ DRIFT DETECTED" if drift else "stable"
            lines.append(f"Calibrator AUC: {auc_str}  |  Brier: {brier_str}  |  {n_pred} predictions  |  {drift_str}")
            if status.get("fallback_recommended"):
                lines.append("  → Sigmoid fallback currently ACTIVE (calibrator AUC degraded)")
        except Exception:
            lines.append("Calibrator: unavailable")

        # OI data quality summary
        try:
            from core.signals.oi_signal import _oi_history
            n_syms = len(_oi_history)
            lines.append(f"OI history tracked: {n_syms} symbol(s)")
        except Exception:
            pass

        # Correlation dampener summary
        try:
            from core.analytics.correlation_dampener import CORRELATION_CLUSTERS
            cluster_desc = [f"{k} ({', '.join(v)})" for k, v in CORRELATION_CLUSTERS.items()]
            lines.append(f"Corr clusters: {len(CORRELATION_CLUSTERS)} defined")
            lines.append(f"  {' | '.join(cluster_desc)}")
        except Exception:
            pass

        # Portfolio guard status
        try:
            from core.analytics.portfolio_guard import CORRELATION_GROUPS
            lines.append(f"Portfolio guard: {len(CORRELATION_GROUPS)} groups tracked")
        except Exception:
            pass

        self._diag_text.setPlainText("\n".join(lines) if lines else "No diagnostic data yet.")


class PerformanceAnalyticsPage(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._build()
        self._subscribe()
        QTimer.singleShot(500, self._refresh)

    # ── Layout ─────────────────────────────────────────────
    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(PageHeader(
            "Performance Analytics",
            "Equity curve · Regime · Model · Asset · Side · Distributions · Learning · Readiness · Edge Analysis · Validation"
        ))

        content = QWidget()
        content.setStyleSheet(f"background:{_C_BG};")
        v = QVBoxLayout(content)
        v.setContentsMargins(24, 20, 24, 20)
        v.setSpacing(14)

        # ── Stat strip (10 cards) ─────────────────────────
        strip = QFrame()
        strip.setObjectName("card")
        strip.setStyleSheet(_CARD_STYLE)
        sl = QHBoxLayout(strip)
        sl.setContentsMargins(0, 0, 0, 0)
        sl.setSpacing(0)

        self._s_trades  = _StatCard("TOTAL TRADES",   "0")
        self._s_winrate = _StatCard("WIN RATE",        "—")
        self._s_pnl     = _StatCard("TOTAL P&L",      "+$0.00", _C_GREEN)
        self._s_pf      = _StatCard("PROFIT FACTOR",  "—")
        self._s_rr      = _StatCard("AVG R:R",        "—")
        self._s_best    = _StatCard("BEST TRADE",     "—",      _C_GREEN)
        self._s_worst   = _StatCard("WORST TRADE",    "—",      _C_RED)
        self._s_avgdur  = _StatCard("AVG DURATION",   "—")
        self._s_dd      = _StatCard("MAX DD",          "0.00%",  _C_GREEN)
        self._s_ready   = _StatCard("READINESS",       "—",      _C_MUTED)

        cards = [
            self._s_trades, self._s_winrate, self._s_pnl,   self._s_pf,
            self._s_rr,     self._s_best,    self._s_worst, self._s_avgdur,
            self._s_dd,     self._s_ready,
        ]
        for i, w in enumerate(cards):
            if i:
                div = QFrame()
                div.setFrameShape(QFrame.VLine)
                div.setStyleSheet(f"color:{_C_BORDER};")
                sl.addWidget(div)
            sl.addWidget(w, 1)
        v.addWidget(strip)

        # ── Tabs ─────────────────────────────────────────
        self._tabs = QTabWidget()
        self._tabs.setStyleSheet(
            f"QTabWidget::pane {{ background:{_C_CARD}; border:1px solid {_C_BORDER}; "
            f"border-radius:6px; }}"
            f"QTabBar::tab {{ background:#0A0E1A; color:{_C_MUTED}; "
            f"padding:7px 16px; border:1px solid {_C_BORDER}; "
            f"border-bottom:none; border-radius:4px 4px 0 0; margin-right:2px; "
            f"font-size:12px; }}"
            f"QTabBar::tab:selected {{ background:{_C_CARD}; color:{_C_TEXT}; "
            f"font-weight:700; }}"
        )

        self._equity_tab      = _EquityTab()
        self._regime_tab      = _RegimeTab()
        self._model_tab       = _ModelTab()
        self._asset_tab       = _AssetTab()
        self._side_tab        = _SideTab()
        self._dist_tab        = _DistTab()
        self._learning_tab    = _LearningTab()
        self._readiness_tab   = _ReadinessTab()
        self._edge_tab        = _EdgeTab()
        self._validation_tab  = _ValidationTab()

        self._tabs.addTab(self._equity_tab,      "📈  Equity / DD")
        self._tabs.addTab(self._regime_tab,      "🗂  By Regime")
        self._tabs.addTab(self._model_tab,       "🧠  By Model")
        self._tabs.addTab(self._asset_tab,       "🪙  By Asset")
        self._tabs.addTab(self._side_tab,        "↕  By Side")
        self._tabs.addTab(self._dist_tab,        "📊  Distributions")
        self._tabs.addTab(self._learning_tab,    "🔄  Learning Loop")
        self._tabs.addTab(self._readiness_tab,   "🎯  Demo Readiness")
        self._tabs.addTab(self._edge_tab,        "⚡  Edge Analysis")
        self._tabs.addTab(self._validation_tab,  "🔬  Validation")

        v.addWidget(self._tabs, 1)

        # ── Footer ───────────────────────────────────────
        footer = QHBoxLayout()
        self._updated_lbl = QLabel("Not yet refreshed")
        self._updated_lbl.setStyleSheet(f"color:{_C_MUTED}; font-size:13px;")
        footer.addWidget(self._updated_lbl)
        footer.addStretch()

        refresh_btn = QPushButton("↺  Refresh")
        refresh_btn.setStyleSheet(_BTN_NEUTRAL)
        refresh_btn.setFixedHeight(28)
        refresh_btn.clicked.connect(self._refresh)
        footer.addWidget(refresh_btn)

        v.addLayout(footer)
        root.addWidget(content, 1)

    # ── EventBus subscriptions ─────────────────────────
    def _subscribe(self):
        bus.subscribe(Topics.TRADE_CLOSED,  self._on_trade_closed)
        # Paper account wiped — full analytics refresh so stale trade history is cleared
        bus.subscribe(Topics.ACCOUNT_RESET, self._on_account_reset)

    # ── Refresh ────────────────────────────────────────
    @Slot()
    def _refresh(self):
        try:
            from core.execution.paper_executor import paper_executor as _pe

            stats          = _pe.get_stats()
            closed         = _pe.get_closed_trades()
            initial_capital = max(_pe._initial_capital, 0.01)

            n    = stats["total_trades"]
            wr   = stats["win_rate"]
            pnl  = stats["total_pnl_usdt"]
            pf   = stats["profit_factor"]
            rr   = stats.get("avg_rr", 0.0)
            best = stats["best_trade_usdt"]
            worst = stats["worst_trade_usdt"]
            dur  = stats["avg_duration_s"]
            dd   = stats["drawdown_pct"]

            # ── Unrealized P&L from open positions ─────
            open_positions = _pe.get_open_positions()
            n_open = len(open_positions)
            unrealized_usdt = sum(
                p.get("size_usdt", 0) * p.get("unrealized_pnl", 0) / 100
                for p in open_positions
            )
            combined_pnl = pnl + unrealized_usdt

            # ── Stat strip ─────────────────────────────
            self._s_trades.set(str(n + n_open))
            self._s_trades.set_sub(
                f"{n} closed · {n_open} open" if n_open else f"{n} closed"
            )

            wr_col = (_C_GREEN if wr >= 55 else
                      _C_YELLOW if wr >= 45 else
                      (_C_RED if n else _C_TEXT))
            self._s_winrate.set(f"{wr:.1f}%" if n else "—", wr_col)

            self._s_pnl.set(
                f"{'+'if combined_pnl>=0 else'-'}${abs(combined_pnl):,.2f}" if (n or n_open) else "+$0.00",
                _C_GREEN if combined_pnl >= 0 else _C_RED,
            )
            if n_open:
                self._s_pnl.set_sub(
                    f"{'+'if pnl>=0 else'-'}${abs(pnl):.2f} realized · {'+'if unrealized_usdt>=0 else'-'}${abs(unrealized_usdt):.2f} open"
                )
            else:
                self._s_pnl.set_sub("realized only" if n else "")
            self._s_pf.set(
                f"{pf:.2f}" if n else "—",
                _C_GREEN if pf >= 1 else (_C_RED if n else _C_TEXT),
            )
            self._s_rr.set(
                f"{rr:.2f}" if n else "—",
                _C_GREEN if rr >= 1 else (_C_YELLOW if rr >= 0.8 else (_C_RED if n else _C_TEXT)),
            )
            self._s_best.set(
                f"{'+'if best>=0 else'-'}${abs(best):,.2f}" if n else "—",
                _C_GREEN if best >= 0 else _C_RED,
            )
            self._s_worst.set(
                f"{'+'if worst>=0 else'-'}${abs(worst):,.2f}" if n else "—",
                _C_RED if worst < 0 else _C_GREEN,
            )
            self._s_avgdur.set(_fmt_duration(dur) if n else "—")

            dd_col = _C_RED if dd >= 15 else (_C_YELLOW if dd >= 10 else _C_GREEN)
            self._s_dd.set(f"{dd:.2f}%", dd_col if n else _C_GREEN)

            # Readiness quick-status from last assessment
            try:
                from core.evaluation.demo_performance_evaluator import get_evaluator
                last = get_evaluator().last_assessment()
                if last:
                    col, icon = _STATUS_COLORS.get(last.status, (_C_MUTED, "?"))
                    self._s_ready.set(f"{icon} {last.score}/100", col)
                else:
                    self._s_ready.set("—", _C_MUTED)
            except Exception:
                self._s_ready.set("—", _C_MUTED)

            # ── All tabs ───────────────────────────────
            self._equity_tab.refresh(closed, initial_capital)
            self._regime_tab.refresh(closed)
            self._model_tab.refresh(closed)
            self._asset_tab.refresh(closed)
            self._side_tab.refresh(closed)
            self._dist_tab.refresh(closed)
            self._learning_tab.refresh(closed)
            self._readiness_tab.refresh(closed)
            self._edge_tab.refresh(closed)
            self._validation_tab.refresh(closed)

            now = datetime.utcnow().strftime("%H:%M:%S UTC")
            open_note = f"   |   {n_open} open position(s)" if n_open else ""
            self._updated_lbl.setText(
                f"Last refreshed: {now}   |   {n} closed trade(s){open_note}"
            )

        except Exception as exc:
            logger.warning("PerformanceAnalyticsPage refresh error: %s", exc)

    def _on_account_reset(self, event):
        """Paper account wiped — all analytics must re-query from the cleared executor."""
        QTimer.singleShot(0, self._refresh)

    def _on_trade_closed(self, event):
        """Refresh shortly after a trade closes so data is fully committed."""
        QTimer.singleShot(200, self._refresh)
