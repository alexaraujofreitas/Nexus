# ============================================================
# NEXUS TRADER — Demo Live Monitor Widget
#
# Compact real-time panel for Bybit Demo trading sessions.
# Shows all critical health metrics in one view — no drilling
# into sub-pages needed during an active demo session.
#
# Metrics displayed:
#   1. Capital (current + P&L delta from $100k start)
#   2. Portfolio heat %
#   3. Open trades (count + symbol list)
#   4. Total exposure (sum of open size_usdt)
#   5. Last 10 trades — W/L + realized R multiple
#   6. Losing streak (current + max)
#   7. Drawdown % (current + peak)
#
# Plus: Live vs Study 4 comparison (WR, PF, avg_R).
#
# Auto-refreshes every 5 seconds via QTimer.
# ============================================================
from __future__ import annotations

import logging
from typing import Optional

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QFrame,
    QGridLayout, QSizePolicy, QTableWidget, QTableWidgetItem,
    QHeaderView, QScrollArea, QAbstractItemView,
)
from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont

logger = logging.getLogger(__name__)

# ── Colour palette (shared with quant_dashboard_page) ────────
_BG_BASE   = "#060b14"
_BG_PANEL  = "#0b1220"
_BG_CARD   = "#0f1a2e"
_BORDER    = "#1a2d4a"
_BORDER_DIM= "#0f1e30"
_TEXT_PRI  = "#E2E8F0"
_TEXT_MUT  = "#C8D0E0"
_TEXT_DIM  = "#8899AA"
_CYAN      = "#00D4FF"
_BLUE      = "#1E90FF"
_BULL      = "#00CC77"
_BEAR      = "#FF3355"
_WARN      = "#F59E0B"
_PURPLE    = "#A855F7"
_AMBER     = "#FFB300"

_START_CAPITAL = 100_000.0   # demo baseline — updated if different

_REFRESH_MS = 5_000   # 5-second auto-refresh interval


def _panel(title: str = "") -> tuple[QFrame, QVBoxLayout]:
    """Return a styled panel frame + its inner VBoxLayout."""
    frame = QFrame()
    frame.setObjectName("panel")
    frame.setStyleSheet(f"""
        QFrame#panel {{
            background: {_BG_PANEL};
            border: 1px solid {_BORDER};
            border-radius: 6px;
        }}
    """)
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(12, 10, 12, 10)
    layout.setSpacing(6)
    if title:
        lbl = QLabel(title.upper())
        lbl.setStyleSheet(f"color:{_TEXT_DIM}; font-size:11px; font-weight:700; letter-spacing:1px;")
        layout.addWidget(lbl)
    return frame, layout


def _stat_card(label: str, value: str = "—", color: str = _TEXT_PRI) -> tuple[QFrame, QLabel]:
    """Return a mini stat card (label above, value below)."""
    card = QFrame()
    card.setObjectName("card")
    card.setStyleSheet(f"""
        QFrame#card {{
            background: {_BG_CARD};
            border: 1px solid {_BORDER_DIM};
            border-radius: 4px;
        }}
    """)
    vl = QVBoxLayout(card)
    vl.setContentsMargins(10, 8, 10, 8)
    vl.setSpacing(3)
    lbl = QLabel(label)
    lbl.setStyleSheet(f"color:{_TEXT_DIM}; font-size:11px;")
    lbl.setAlignment(Qt.AlignCenter)
    val = QLabel(value)
    val.setStyleSheet(f"color:{color}; font-size:16px; font-weight:700;")
    val.setAlignment(Qt.AlignCenter)
    vl.addWidget(lbl)
    vl.addWidget(val)
    return card, val


class DemoMonitorWidget(QWidget):
    """
    Compact Bybit Demo health monitor.

    Wired to paper_executor.get_production_status() and
    get_stats() for live data, and LiveVsBacktestTracker for
    comparison against Study 4 baselines.
    """

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setStyleSheet(f"background: {_BG_BASE};")
        self._setup_ui()
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._refresh)
        self._timer.start(_REFRESH_MS)
        self._refresh()   # immediate first paint

    # ── Layout ────────────────────────────────────────────────

    def _setup_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(10)

        # ── Row 1: 7 stat cards across the top ───────────────────
        cards_row = QHBoxLayout()
        cards_row.setSpacing(8)

        self._card_capital, self._val_capital    = _stat_card("Capital", color=_CYAN)
        self._card_pnl,     self._val_pnl        = _stat_card("P&L",     color=_TEXT_PRI)
        self._card_heat,    self._val_heat        = _stat_card("Heat",    color=_TEXT_PRI)
        self._card_open,    self._val_open        = _stat_card("Open",    color=_TEXT_PRI)
        self._card_exp,     self._val_exposure    = _stat_card("Exposure",color=_TEXT_PRI)
        self._card_streak,  self._val_streak      = _stat_card("Streak",  color=_TEXT_PRI)
        self._card_dd,      self._val_dd          = _stat_card("Drawdown",color=_TEXT_PRI)

        for card in (self._card_capital, self._card_pnl, self._card_heat,
                     self._card_open, self._card_exp, self._card_streak, self._card_dd):
            card.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            cards_row.addWidget(card)
        root.addLayout(cards_row)

        # ── Row 2: Phase pill + Pause banner ─────────────────────
        status_row = QHBoxLayout()
        status_row.setSpacing(8)

        # Phase pill
        self._phase_lbl = QLabel("Phase 1 — 0.5% risk")
        self._phase_lbl.setStyleSheet(
            f"color:{_CYAN}; font-size:12px; font-weight:600; "
            f"background:{_BG_CARD}; border:1px solid {_BORDER_DIM}; "
            "border-radius:4px; padding:4px 10px;"
        )
        status_row.addWidget(self._phase_lbl)

        # RAG portfolio pill
        self._rag_lbl = QLabel("RAG: ⚪ —")
        self._rag_lbl.setStyleSheet(
            f"color:{_TEXT_MUT}; font-size:12px; font-weight:600; "
            f"background:{_BG_CARD}; border:1px solid {_BORDER_DIM}; "
            "border-radius:4px; padding:4px 10px;"
        )
        status_row.addWidget(self._rag_lbl)
        status_row.addStretch()

        # Pause banner (hidden by default)
        self._pause_banner = QLabel("⚠️  PAUSE RECOMMENDED — review RAG status")
        self._pause_banner.setStyleSheet(
            f"color:{_BEAR}; font-size:13px; font-weight:700; "
            f"background:#1a0a0a; border:1px solid {_BEAR}; "
            "border-radius:4px; padding:4px 12px;"
        )
        self._pause_banner.setVisible(False)
        status_row.addWidget(self._pause_banner)

        root.addLayout(status_row)

        # ── Row 3: Last 10 trades | Live vs Backtest ─────────────
        row2 = QHBoxLayout()
        row2.setSpacing(10)

        # Last 10 trades table
        trades_frame, trades_vl = _panel("Last 10 Trades")
        self._trades_table = self._build_trades_table()
        trades_vl.addWidget(self._trades_table)
        trades_frame.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        row2.addWidget(trades_frame, stretch=3)

        # Live vs Backtest comparison (now 6 columns with RAG)
        lvb_frame, lvb_vl = _panel("Live vs Study 4  |  RAG Status")
        self._lvb_table = self._build_lvb_table()
        lvb_vl.addWidget(self._lvb_table)
        lvb_frame.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        row2.addWidget(lvb_frame, stretch=2)

        row2_container = QWidget()
        row2_container.setLayout(row2)
        row2_container.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        root.addWidget(row2_container, stretch=1)

        # ── Row 4: Open positions strip ───────────────────────────
        open_frame, open_vl = _panel("Open Positions")
        self._open_positions_lbl = QLabel("—")
        self._open_positions_lbl.setStyleSheet(f"color:{_TEXT_MUT}; font-size:13px;")
        self._open_positions_lbl.setWordWrap(True)
        open_vl.addWidget(self._open_positions_lbl)
        root.addWidget(open_frame)

    def _build_trades_table(self) -> QTableWidget:
        t = QTableWidget(10, 5)
        t.setHorizontalHeaderLabels(["Symbol", "Side", "Exit", "R", "Outcome"])
        t.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        t.verticalHeader().setVisible(False)
        t.setEditTriggers(QAbstractItemView.NoEditTriggers)
        t.setSelectionMode(QAbstractItemView.NoSelection)
        t.setStyleSheet(f"""
            QTableWidget {{
                background: transparent; border: none;
                gridline-color: {_BORDER_DIM};
                font-size: 12px; color: {_TEXT_PRI};
            }}
            QTableWidget::item {{ padding: 3px 6px; border-bottom: 1px solid {_BORDER_DIM}; }}
            QHeaderView::section {{
                background: {_BG_PANEL}; color: {_TEXT_DIM};
                font-size: 11px; font-weight: 700; letter-spacing: 1px;
                padding: 4px 6px; border: none;
                border-bottom: 1px solid {_BORDER_DIM};
            }}
        """)
        return t

    def _build_lvb_table(self) -> QTableWidget:
        t = QTableWidget(6, 6)
        t.setHorizontalHeaderLabels(["Model", "Trades", "WR Live", "WR Base", "Δ WR", "RAG"])
        t.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        t.verticalHeader().setVisible(False)
        t.setEditTriggers(QAbstractItemView.NoEditTriggers)
        t.setSelectionMode(QAbstractItemView.NoSelection)
        t.setStyleSheet(f"""
            QTableWidget {{
                background: transparent; border: none;
                gridline-color: {_BORDER_DIM};
                font-size: 12px; color: {_TEXT_PRI};
            }}
            QTableWidget::item {{ padding: 3px 6px; border-bottom: 1px solid {_BORDER_DIM}; }}
            QHeaderView::section {{
                background: {_BG_PANEL}; color: {_TEXT_DIM};
                font-size: 11px; font-weight: 700; letter-spacing: 1px;
                padding: 4px 6px; border: none;
                border-bottom: 1px solid {_BORDER_DIM};
            }}
        """)
        return t

    # ── Refresh ───────────────────────────────────────────────

    def _refresh(self) -> None:
        try:
            self._refresh_stats()
        except Exception as exc:
            logger.debug("DemoMonitorWidget: refresh error: %s", exc)

    def _refresh_stats(self) -> None:
        try:
            from core.execution.paper_executor import paper_executor as _pe
            status = _pe.get_production_status()
            stats  = _pe.get_stats()
        except Exception:
            return

        # ── Capital & P&L ────────────────────────────────────────
        capital = float(status.get("capital_usdt") or stats.get("available_capital") or _START_CAPITAL)
        pnl_usdt = capital - _START_CAPITAL
        pnl_pct  = (pnl_usdt / _START_CAPITAL) * 100

        self._val_capital.setText(f"${capital:,.0f}")
        pnl_color = _BULL if pnl_usdt >= 0 else _BEAR
        sign = "+" if pnl_usdt >= 0 else ""
        self._val_pnl.setText(f"{sign}${pnl_usdt:,.0f} ({sign}{pnl_pct:.1f}%)")
        self._val_pnl.setStyleSheet(f"color:{pnl_color}; font-size:16px; font-weight:700;")

        # ── Heat ─────────────────────────────────────────────────
        heat = float(status.get("portfolio_heat_pct") or 0.0)
        heat_color = _BEAR if heat > 5.0 else (_WARN if heat > 3.0 else _BULL)
        self._val_heat.setText(f"{heat:.1f}%")
        self._val_heat.setStyleSheet(f"color:{heat_color}; font-size:16px; font-weight:700;")

        # ── Open trades ───────────────────────────────────────────
        open_pos = status.get("open_positions") or []
        n_open   = len(open_pos) if isinstance(open_pos, list) else int(status.get("open_trades", 0) or 0)
        self._val_open.setText(str(n_open))
        open_color = _WARN if n_open >= 5 else _TEXT_PRI
        self._val_open.setStyleSheet(f"color:{open_color}; font-size:16px; font-weight:700;")

        # ── Exposure ──────────────────────────────────────────────
        exposure = 0.0
        if isinstance(open_pos, list):
            for p in open_pos:
                exposure += float(p.get("size_usdt") or 0.0)
        self._val_exposure.setText(f"${exposure:,.0f}")

        # ── Losing streak ─────────────────────────────────────────
        streak = int(status.get("current_losing_streak") or 0)
        max_streak = int(stats.get("max_consecutive_losses") or stats.get("current_losing_streak") or 0)
        streak_color = _BEAR if streak >= 4 else (_WARN if streak >= 2 else _BULL)
        self._val_streak.setText(f"{streak} (max {max_streak})")
        self._val_streak.setStyleSheet(f"color:{streak_color}; font-size:16px; font-weight:700;")

        # ── Drawdown ─────────────────────────────────────────────
        dd_pct = float(stats.get("drawdown_pct") or 0.0)
        dd_color = _BEAR if dd_pct >= 8.0 else (_WARN if dd_pct >= 4.0 else _BULL)
        self._val_dd.setText(f"{dd_pct:.1f}%")
        self._val_dd.setStyleSheet(f"color:{dd_color}; font-size:16px; font-weight:700;")

        # Circuit breaker warning
        cb_on = bool(status.get("circuit_breaker_on"))
        if cb_on:
            self._val_dd.setText(f"{dd_pct:.1f}% ⛔")

        # ── Open positions strip ──────────────────────────────────
        if isinstance(open_pos, list) and open_pos:
            parts = []
            for p in open_pos:
                sym  = p.get("symbol", "?")
                side = p.get("side", "?").upper()[:1]
                upnl = float(p.get("unrealized_pnl") or 0.0)
                sign = "+" if upnl >= 0 else ""
                clr  = "green" if upnl >= 0 else "red"
                parts.append(f'<span style="color:{clr}">{sym} {side} {sign}${upnl:.1f}</span>')
            self._open_positions_lbl.setText("  |  ".join(parts))
        else:
            self._open_positions_lbl.setText('<span style="color:#4A6A8A">No open positions</span>')

        # ── Last 10 trades table ──────────────────────────────────
        last10 = status.get("last_10_outcomes") or []
        # Supplement with richer data from stats if available
        try:
            from core.execution.paper_executor import paper_executor as _pe2
            closed = list(_pe2._closed_trades)[-10:]
        except Exception:
            closed = []

        self._populate_trades_table(last10, closed)

        # ── Live vs Backtest table ────────────────────────────────
        self._populate_lvb_table()

        # ── RAG status + phase pill ───────────────────────────────
        self._refresh_rag_and_phase()

    def _populate_trades_table(self, last10_outcomes: list, closed_trades: list) -> None:
        """Fill the last-10-trades table. Uses closed_trades if rich data available."""
        table = self._trades_table
        # Use closed_trades if available (richer), else last10_outcomes
        source = list(reversed(closed_trades)) if closed_trades else last10_outcomes

        for row in range(10):
            if row < len(source):
                t = source[row]
                if isinstance(t, dict):
                    sym    = str(t.get("symbol", "—"))
                    side   = str(t.get("side", "")).upper()[:1]
                    exit_p = t.get("exit_price")
                    exit_s = f"{exit_p:,.4f}" if exit_p else "—"
                    rlzd_r = t.get("realized_r")
                    pnl    = t.get("pnl_usdt", 0.0) or 0.0
                    if rlzd_r is not None:
                        r_str = f"{rlzd_r:+.2f}R"
                    else:
                        r_str = f"{'+' if pnl >= 0 else ''}{pnl:.0f}$"
                    outcome = "✓" if (pnl >= 0) else "✗"
                    color   = _BULL if pnl >= 0 else _BEAR
                else:
                    # last10_outcomes is a list of "W"/"L" strings
                    sym    = "—"; side = "—"; exit_s = "—"
                    is_win = (str(t) == "W")
                    r_str  = "+R" if is_win else "-R"
                    outcome = "✓" if is_win else "✗"
                    color  = _BULL if is_win else _BEAR

                for col, text in enumerate([sym, side, exit_s, r_str, outcome]):
                    item = QTableWidgetItem(text)
                    item.setTextAlignment(Qt.AlignCenter)
                    if col in (3, 4):
                        item.setForeground(QColor(color))
                    table.setItem(row, col, item)
            else:
                for col in range(5):
                    item = QTableWidgetItem("—")
                    item.setTextAlignment(Qt.AlignCenter)
                    item.setForeground(QColor(_TEXT_DIM))
                    table.setItem(row, col, item)

    def _populate_lvb_table(self) -> None:
        """Fill the live vs Study 4 comparison table with RAG pills."""
        try:
            from core.monitoring.live_vs_backtest import get_live_vs_backtest_tracker
            comparison = get_live_vs_backtest_tracker().get_comparison()
        except Exception:
            return

        # Fetch RAG assessment for pill column (non-fatal)
        rag_per_model: dict = {}
        rag_portfolio = "INSUFFICIENT_DATA"
        try:
            from core.monitoring.performance_thresholds import get_threshold_evaluator
            assessment    = get_threshold_evaluator().evaluate()
            rag_per_model = {k: v.overall.value for k, v in assessment.per_model.items()}
            rag_portfolio = assessment.portfolio.overall.value
        except Exception:
            pass

        table = self._lvb_table
        rows  = []   # (model_str, trades, wr_live, wr_base, delta, rag_status_str)

        # Portfolio-level row first
        port       = comparison.get("portfolio", {})
        port_live  = port.get("live",     {})
        port_base  = port.get("baseline", {})
        port_delta = port.get("delta",    {})
        rows.append((
            "Portfolio",
            str(port_live.get("trades", 0)),
            _fmt_pct(port_live.get("win_rate")),
            _fmt_pct(port_base.get("win_rate")),
            _fmt_delta_pct(port_delta.get("wr_delta")),
            rag_portfolio,
        ))

        # Per-model rows
        per_model = comparison.get("per_model", {})
        for model_key, comp in per_model.items():
            if model_key.startswith("_") or model_key == "unknown":
                continue
            live  = comp.get("live", {})
            base  = comp.get("baseline") or {}
            delta = comp.get("delta") or {}
            rows.append((
                _fmt_model_name(model_key),
                str(live.get("trades", 0)),
                _fmt_pct(live.get("win_rate")),
                _fmt_pct(base.get("win_rate")),
                _fmt_delta_pct(delta.get("wr_delta")),
                rag_per_model.get(model_key, "INSUFFICIENT_DATA"),
            ))

        _rag_color = {
            "GREEN":             _BULL,
            "AMBER":             _AMBER,
            "RED":               _BEAR,
            "INSUFFICIENT_DATA": _TEXT_DIM,
        }
        _rag_pill = {
            "GREEN":             "🟢 GREEN",
            "AMBER":             "🟡 AMBER",
            "RED":               "🔴 RED",
            "INSUFFICIENT_DATA": "⚪ —",
        }

        table.setRowCount(max(len(rows), 1))
        for row, (model, trades, wr_live, wr_base, delta, rag) in enumerate(rows[:table.rowCount()]):
            delta_color = _BULL if (delta or "").startswith("+") else (
                _BEAR if (delta or "").startswith("-") else _TEXT_DIM
            )
            rag_text = _rag_pill.get(rag, "⚪ —")
            rag_clr  = _rag_color.get(rag,  _TEXT_DIM)
            for col, (text, color) in enumerate([
                (model,    _TEXT_PRI),
                (trades,   _TEXT_MUT),
                (wr_live,  _TEXT_PRI),
                (wr_base,  _TEXT_DIM),
                (delta,    delta_color),
                (rag_text, rag_clr),
            ]):
                item = QTableWidgetItem(text)
                item.setTextAlignment(Qt.AlignCenter)
                item.setForeground(QColor(color))
                table.setItem(row, col, item)

    def _refresh_rag_and_phase(self) -> None:
        """Update the phase pill, RAG pill, and pause banner."""
        # ── Phase pill ────────────────────────────────────────────
        try:
            from core.monitoring.scale_manager import get_scale_manager
            phase_summary = get_scale_manager().get_phase_summary()
            self._phase_lbl.setText(
                f"⚙  {phase_summary['description']}  ({phase_summary['risk_pct_str']})"
            )
        except Exception:
            pass   # keep last text

        # ── RAG portfolio pill + pause banner ─────────────────────
        try:
            from core.monitoring.performance_thresholds import get_threshold_evaluator
            assessment = get_threshold_evaluator().evaluate()
            rag_val    = assessment.portfolio.overall.value
            _pill_map  = {
                "GREEN":             ("🟢 GREEN",             _BULL),
                "AMBER":             ("🟡 AMBER",             _AMBER),
                "RED":               ("🔴 RED",               _BEAR),
                "INSUFFICIENT_DATA": ("⚪ Insufficient data", _TEXT_DIM),
            }
            pill_text, pill_color = _pill_map.get(rag_val, ("⚪ —", _TEXT_DIM))
            self._rag_lbl.setText(f"RAG: {pill_text}")
            self._rag_lbl.setStyleSheet(
                f"color:{pill_color}; font-size:12px; font-weight:600; "
                f"background:{_BG_CARD}; border:1px solid {_BORDER_DIM}; "
                "border-radius:4px; padding:4px 10px;"
            )
            # Pause banner
            if assessment.should_pause:
                reason = assessment.pause_reason or "Review RAG status"
                self._pause_banner.setText(f"⚠️  PAUSE RECOMMENDED — {reason}")
                self._pause_banner.setVisible(True)
            else:
                self._pause_banner.setVisible(False)
        except Exception:
            pass   # keep last state


# ── Helper formatters — imported from pure-Python helpers module ──────────────
# Kept here as module-level aliases so external code can do:
#   from gui.widgets.demo_monitor_widget import _fmt_pct
# without the helpers module being the canonical source.
from gui.widgets.demo_monitor_helpers import (  # noqa: E402
    fmt_pct      as _fmt_pct,
    fmt_delta_pct as _fmt_delta_pct,
    fmt_model_name as _fmt_model_name,
)
