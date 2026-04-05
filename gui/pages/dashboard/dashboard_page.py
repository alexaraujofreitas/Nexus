# ============================================================
# NEXUS TRADER — Dashboard Page (Phase 2)
# Live metrics, price ticker, and system status
# ============================================================

import logging
from datetime import datetime, timezone

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QFrame, QGridLayout, QPushButton, QSizePolicy,
    QTableWidget, QTableWidgetItem, QHeaderView, QAbstractItemView,
    QScrollArea
)
from PySide6.QtCore import Qt, QTimer, Slot, Signal
from PySide6.QtGui import QColor

from gui.main_window import PageHeader
from gui.widgets.crash_score_widget import CrashScoreWidget
from core.event_bus import bus, Topics

logger = logging.getLogger(__name__)


# ── Metric Card ────────────────────────────────────────────
class MetricCard(QFrame):
    def __init__(self, title: str, value: str = "—",
                 sub: str = "", value_color: str = "#E8EBF0"):
        super().__init__()
        self.setObjectName("card")
        self.setMinimumHeight(70)
        self.setMinimumWidth(120)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(1)

        self._title_lbl = QLabel(title)
        self._title_lbl.setObjectName("card_title")
        self._title_lbl.setStyleSheet(
            "font-size: 10px; font-weight: 700; color: #8899AA; letter-spacing: 1px;"
        )
        self._title_lbl.setWordWrap(True)

        self._value_lbl = QLabel(value)
        self._value_lbl.setObjectName("card_value")
        self._value_color = value_color
        self._value_lbl.setStyleSheet(
            f"font-size: 15px; font-weight: 700; color: {value_color};"
        )
        self._value_lbl.setWordWrap(True)

        self._sub_lbl = QLabel(sub)
        self._sub_lbl.setObjectName("card_sub")
        self._sub_lbl.setStyleSheet("font-size: 10px; color: #4A5568;")
        self._sub_lbl.setWordWrap(True)

        layout.addWidget(self._title_lbl)
        layout.addWidget(self._value_lbl)
        layout.addWidget(self._sub_lbl)
        layout.addStretch()

    def set_value(self, value: str, color: str = "#E8EBF0"):
        self._value_lbl.setText(value)
        self._value_lbl.setStyleSheet(
            f"font-size: 15px; font-weight: 700; color: {color};"
        )

    def set_sub(self, sub: str):
        self._sub_lbl.setText(sub)


# ── Live Ticker Row ────────────────────────────────────────
class TickerTable(QFrame):
    """Mini live price table for top symbols."""

    WATCH_SYMBOLS = [
        "BTC/USDT", "ETH/USDT", "BNB/USDT", "XRP/USDT", "SOL/USDT",
        "TRX/USDT", "DOGE/USDT", "ADA/USDT", "BCH/USDT", "HYPE/USDT",
        "LINK/USDT", "XLM/USDT", "AVAX/USDT", "HBAR/USDT", "SUI/USDT",
        "NEAR/USDT", "ICP/USDT", "ONDO/USDT", "ALGO/USDT", "RENDER/USDT",
    ]
    COLUMNS = ["Symbol", "Last Price", "24h Change", "Volume", "High", "Low"]

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("card")
        self._build()

    def _build(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(8)

        title_row = QHBoxLayout()
        title = QLabel("LIVE PRICES")
        title.setObjectName("card_title")
        title_row.addWidget(title)
        self._feed_dot = QLabel("⬤ Feed inactive")
        self._feed_dot.setStyleSheet("color: #4A5568; font-size: 13px;")
        title_row.addStretch()
        title_row.addWidget(self._feed_dot)
        layout.addLayout(title_row)

        self._table = QTableWidget(len(self.WATCH_SYMBOLS), len(self.COLUMNS))
        self._table.setHorizontalHeaderLabels(self.COLUMNS)
        self._table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        for c in range(1, len(self.COLUMNS)):
            self._table.horizontalHeader().setSectionResizeMode(
                c, QHeaderView.ResizeToContents
            )
        self._table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._table.verticalHeader().setVisible(False)
        self._table.setAlternatingRowColors(True)
        self._table.setFixedHeight(160)
        self._table.setStyleSheet(
            "QTableWidget { background: #0A0E1A; color: #E8EBF0; "
            "gridline-color: #1A2332; font-size: 13px; border: none; }"
            "QTableWidget::item:alternate { background: #0D1220; }"
            "QHeaderView::section { background: #0F1623; color: #8899AA; "
            "padding: 4px; border: none; border-bottom: 1px solid #1A2332; font-size: 13px; }"
        )

        # Pre-fill symbol column
        for i, sym in enumerate(self.WATCH_SYMBOLS):
            self._set_cell(i, 0, sym, "#E8EBF0")
            for c in range(1, len(self.COLUMNS)):
                self._set_cell(i, c, "—", "#4A5568")

        layout.addWidget(self._table)

    def _set_cell(self, row: int, col: int, text: str, color: str = "#E8EBF0"):
        item = QTableWidgetItem(text)
        item.setForeground(QColor(color))
        item.setTextAlignment(Qt.AlignCenter)
        item.setFlags(Qt.ItemIsEnabled)
        self._table.setItem(row, col, item)

    def update_tickers(self, tickers: dict):
        self._feed_dot.setText("⬤ Live")
        self._feed_dot.setStyleSheet("color: #00CC77; font-size: 13px;")
        for i, sym in enumerate(self.WATCH_SYMBOLS):
            if sym not in tickers:
                continue
            t = tickers[sym]
            last   = t.get("last", 0)
            change = t.get("change", 0)
            volume = t.get("volume", 0)
            high   = t.get("high", 0)
            low    = t.get("low", 0)

            ch_color = "#00CC77" if change > 0 else ("#FF3355" if change < 0 else "#8899AA")
            self._set_cell(i, 1, f"{last:.4f}", "#E8EBF0")
            self._set_cell(i, 2, f"{change:+.2f}%", ch_color)
            self._set_cell(i, 3, f"{volume:,.0f}", "#8899AA")
            self._set_cell(i, 4, f"{high:.4f}", "#00CC77")
            self._set_cell(i, 5, f"{low:.4f}", "#FF3355")


# ── Status Row ─────────────────────────────────────────────
class StatusRow(QWidget):
    def __init__(self, label: str, status: str, color: str):
        super().__init__()
        h = QHBoxLayout(self)
        h.setContentsMargins(0, 0, 0, 0)
        lbl = QLabel(label)
        lbl.setStyleSheet("color: #8899AA; font-size: 13px;")
        self._status_lbl = QLabel(f"⬤ {status}")
        self._status_lbl.setStyleSheet(f"color: {color}; font-size: 13px; font-weight: 600;")
        self._status_lbl.setAlignment(Qt.AlignRight)
        h.addWidget(lbl)
        h.addStretch()
        h.addWidget(self._status_lbl)

    def set_status(self, status: str, color: str):
        self._status_lbl.setText(f"⬤ {status}")
        self._status_lbl.setStyleSheet(f"color: {color}; font-size: 13px; font-weight: 600;")


# ── Dashboard Page ─────────────────────────────────────────
class DashboardPage(QWidget):
    # Thread-safe signals — EventBus callbacks may arrive from background threads.
    # These signals marshal updates onto the main thread before touching any widget.
    _sig_exchange_connected = Signal(str, bool)   # name, connected
    _sig_exchange_error     = Signal(str, str)    # name, reason
    _sig_feed_active        = Signal(bool)
    _sig_tickers            = Signal(object)      # tickers dict
    _sig_signal_confirmed   = Signal(object)      # event data dict
    _sig_scanner_status     = Signal(str, str)    # status text, color
    _sig_last_trade         = Signal(str, str)    # status text, color

    def __init__(self, parent=None):
        super().__init__(parent)
        # Session-level counters for the signals card
        self._signals_today      = 0
        self._confirmed_today    = 0
        self._trades_opened_today = 0
        self._build()
        self._wire_signals()
        self._subscribe()
        # Populate cards from executor state at startup (deferred so executor is ready)
        QTimer.singleShot(500,  self._refresh_from_executor)
        # Start live feed for default watch symbols
        QTimer.singleShot(1500, self._start_feed)
        # Populate scanner + last trade rows from current runtime state
        QTimer.singleShot(10000, self._update_scanner_row_startup)

    def _build(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        layout.addWidget(PageHeader("Dashboard", "Portfolio overview and system status"))

        # ── Scrollable content area ────────────────────────────
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        scroll.setStyleSheet(
            "QScrollArea { background: transparent; border: none; }"
            "QScrollBar:vertical { background: #0A0E1A; width: 6px; border-radius: 3px; }"
            "QScrollBar::handle:vertical { background: #2A3A52; border-radius: 3px; min-height: 20px; }"
            "QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0px; }"
            "QScrollBar:horizontal { background: #0A0E1A; height: 6px; border-radius: 3px; }"
            "QScrollBar::handle:horizontal { background: #2A3A52; border-radius: 3px; min-width: 20px; }"
            "QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0px; }"
        )

        content = QWidget()
        content.setObjectName("scroll_content")
        content.setMinimumWidth(900)
        v = QVBoxLayout(content)
        v.setContentsMargins(24, 20, 24, 24)
        v.setSpacing(14)

        # ── Metric Cards ──────────────────────────────────────
        grid = QGridLayout()
        grid.setSpacing(12)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(2, 1)
        grid.setColumnStretch(3, 1)

        self._card_portfolio  = MetricCard("PORTFOLIO VALUE", "—", "Paper mode")
        self._card_pnl        = MetricCard("TODAY'S P&L", "+$0.00", "0.00% today", "#00CC77")
        self._card_positions  = MetricCard("OPEN POSITIONS", "0", "IDSS scanner")
        self._card_strategies = MetricCard("ACTIVE STRATEGIES", "0", "0 active · 0 disabled")
        self._card_winrate    = MetricCard("WIN RATE (30D)", "—", "No trades yet")
        self._card_heat       = MetricCard("PORTFOLIO HEAT", "0.0%", "% capital at risk")
        self._card_drawdown   = MetricCard("MAX DRAWDOWN", "0.00%", "Current drawdown")
        self._card_signals    = MetricCard("SIGNALS TODAY", "0", "0 evaluated · 0 traded")

        grid.addWidget(self._card_portfolio,  0, 0)
        grid.addWidget(self._card_pnl,        0, 1)
        grid.addWidget(self._card_positions,  0, 2)
        grid.addWidget(self._card_strategies, 0, 3)
        grid.addWidget(self._card_winrate,    1, 0)
        grid.addWidget(self._card_heat,       1, 1)
        grid.addWidget(self._card_drawdown,   1, 2)
        grid.addWidget(self._card_signals,    1, 3)
        v.addLayout(grid)

        # ── Crash Risk Monitor ─────────────────────────────────
        self._crash_widget = CrashScoreWidget()
        v.addWidget(self._crash_widget)

        # ── Live Prices ───────────────────────────────────────
        self._ticker_table = TickerTable()
        v.addWidget(self._ticker_table)

        # ── Bottom row: System Status (full width) ─────────────
        status_card = QFrame()
        status_card.setObjectName("card")
        sl = QVBoxLayout(status_card)
        sl.setContentsMargins(16, 14, 16, 14)
        sl.setSpacing(8)
        lbl_t = QLabel("SYSTEM STATUS")
        lbl_t.setObjectName("card_title")
        lbl_t.setStyleSheet(
            "font-size: 13px; font-weight: 700; color: #8899AA; letter-spacing: 1px;"
        )
        sl.addWidget(lbl_t)

        self._row_scanner  = StatusRow("IDSS Scanner",     "Starting…",    "#4A5568")
        self._row_last_trade = StatusRow("Last Trade",      "No trades yet","#4A5568")
        self._row_ml       = StatusRow("ML Models",           "Not Loaded",   "#4A5568")
        self._row_strategy = StatusRow("Strategy Engine",     "Standby",      "#4A5568")
        self._row_risk     = StatusRow("Risk Manager",        "Active",       "#00FF88")

        for row in [self._row_scanner, self._row_last_trade,
                    self._row_ml, self._row_strategy, self._row_risk]:
            sl.addWidget(row)

        v.addWidget(status_card)
        v.addStretch()

        scroll.setWidget(content)
        layout.addWidget(scroll, 1)

    # ── Thread-safe signal wiring ──────────────────────────
    def _wire_signals(self):
        """Connect internal Qt signals to UI slots (runs on main thread)."""
        self._sig_exchange_connected.connect(self._update_exchange_connected)
        self._sig_exchange_error.connect(self._update_exchange_error)
        self._sig_feed_active.connect(self._update_feed_active)
        self._sig_tickers.connect(self._update_tickers)
        self._sig_signal_confirmed.connect(self._update_signal_confirmed)
        self._sig_scanner_status.connect(self._update_scanner_row)
        self._sig_last_trade.connect(self._update_last_trade_row)

    # ── EventBus subscriptions ─────────────────────────────
    def _subscribe(self):
        # Callbacks may come from background threads — they emit Qt signals
        # which are automatically marshalled to the main thread.
        bus.subscribe(Topics.TICK_UPDATE,        self._on_tick_update)
        bus.subscribe(Topics.EXCHANGE_CONNECTED, self._on_exchange_status)
        bus.subscribe(Topics.FEED_STATUS,        self._on_feed_status)
        bus.subscribe(Topics.EXCHANGE_ERROR,     self._on_exchange_error)
        # PaperExecutor lifecycle events
        bus.subscribe(Topics.TRADE_OPENED,       self._on_trade_opened)
        bus.subscribe(Topics.TRADE_CLOSED,       self._on_trade_closed)
        bus.subscribe(Topics.POSITION_UPDATED,   self._on_position_updated)
        # IDSS signal events
        bus.subscribe(Topics.SIGNAL_CONFIRMED,   self._on_signal_confirmed)
        # Scanner lifecycle — update Strategy Engine + Scanner status rows
        bus.subscribe(Topics.SCAN_CYCLE_COMPLETE, self._on_scan_cycle_complete)
        # Paper account wiped — reset session counters and re-pull executor state
        bus.subscribe(Topics.ACCOUNT_RESET,      self._on_account_reset)

    # ── PaperExecutor handlers ─────────────────────────────
    def _refresh_from_executor(self):
        """Pull initial / refreshed values from the paper executor singleton."""
        try:
            from core.execution.paper_executor import paper_executor as _pe
            self._refresh_portfolio_cards(_pe)
        except Exception as exc:
            logger.debug("Dashboard executor refresh: %s", exc)

    def _refresh_portfolio_cards(self, pe) -> None:
        """Update all executor-driven metric cards from a PaperExecutor instance."""
        # Portfolio value = cash + marked-to-market positions
        used_capital = sum(
            p.get("size_usdt", 0) * (1 + p.get("unrealized_pnl", 0) / 100)
            for p in pe.get_open_positions()
        )
        total_value = pe.available_capital + used_capital
        self._card_portfolio.set_value(f"${total_value:,.2f}")
        self._card_portfolio.set_sub("Paper mode")

        # Open positions
        n_pos = len(pe.get_open_positions())
        self._card_positions.set_value(str(n_pos))
        self._card_positions.set_sub(
            "IDSS scanner" if n_pos == 0 else
            f"{n_pos} position{'s' if n_pos > 1 else ''} open"
        )

        # Drawdown
        dd = pe.drawdown_pct
        if dd > 0:
            dd_color = "#FF3355" if dd > 10 else "#FFB300"
            self._card_drawdown.set_value(f"-{dd:.2f}%", dd_color)
        else:
            self._card_drawdown.set_value("0.00%", "#00CC77")
        self._card_drawdown.set_sub("Current drawdown")

        # Win rate from closed trades
        closed = pe._closed_trades
        if closed:
            wins = sum(1 for t in closed if t.get("pnl_pct", 0) > 0)
            wr   = wins / len(closed) * 100
            wr_color = "#00CC77" if wr >= 50 else "#FF3355"
            self._card_winrate.set_value(f"{wr:.1f}%", wr_color)
            self._card_winrate.set_sub(f"{len(closed)} closed trades")
        else:
            self._card_winrate.set_value("—")
            self._card_winrate.set_sub("No trades yet")

        # Today's P&L = realised P&L closed today + current unrealized P&L on open positions.
        # This matches what the Demo Live Monitor and Health Check show.
        today = datetime.now(timezone.utc).date().isoformat()
        closed_pnl_today = sum(
            float(t.get("pnl_usdt") or 0)
            for t in closed
            if t.get("closed_at", "")[:10] == today
        )
        unrealized_usdt = sum(
            float(p.get("size_usdt") or 0) * float(p.get("unrealized_pnl") or 0) / 100
            for p in pe.get_open_positions()
        )
        today_pnl = closed_pnl_today + unrealized_usdt
        pnl_color = "#00CC77" if today_pnl >= 0 else "#FF3355"
        initial   = pe._initial_capital or max(1.0, total_value)
        pnl_pct   = today_pnl / initial * 100
        self._card_pnl.set_value(
            f"{'+' if today_pnl >= 0 else ''}${today_pnl:.2f}",
            pnl_color,
        )
        self._card_pnl.set_sub(f"{pnl_pct:+.3f}% today (incl. unrealised)")

        # Active Strategies — count enabled signal models vs disabled
        try:
            from config.settings import settings as _s
            _all_models = [
                "trend", "momentum_breakout", "mean_reversion", "vwap_reversion",
                "liquidity_sweep", "order_book", "funding_rate", "sentiment", "rl_ensemble",
            ]
            # v1.3 PBL/SLC models gated by their own flag (not disabled_models)
            if _s.get("mr_pbl_slc.enabled", False):
                _all_models += ["pullback_long", "swing_low_continuation"]
            _disabled = list(_s.get("disabled_models", []) or [])
            _active_n  = len([m for m in _all_models if m not in _disabled])
            _disabled_n = len([m for m in _all_models if m in _disabled])
            strat_color = "#00CC77" if _active_n >= 5 else "#FFB300"
            self._card_strategies.set_value(str(_active_n), strat_color)
            self._card_strategies.set_sub(f"{_active_n} active · {_disabled_n} disabled")
        except Exception:
            pass

        # Portfolio Heat — % of capital currently at risk across open positions
        try:
            _status = pe.get_production_status()
            _heat   = float(_status.get("portfolio_heat_pct") or 0.0)
            heat_color = "#FF3355" if _heat > 5.0 else ("#FFB300" if _heat > 3.0 else "#00CC77")
            self._card_heat.set_value(f"{_heat:.1f}%", heat_color)
            self._card_heat.set_sub("% capital at risk")
        except Exception:
            pass

    def _on_account_reset(self, event):
        """Paper account wiped — reset session counters and do a full executor refresh."""
        self._trades_opened_today = 0
        self._signals_today       = 0
        QTimer.singleShot(0, self._refresh_from_executor)

    def _on_trade_opened(self, event):
        # Increment trades-opened counter and refresh signals card
        self._trades_opened_today += 1
        self._card_signals.set_sub(
            f"{self._signals_today} evaluated · {self._trades_opened_today} traded"
        )
        QTimer.singleShot(0, self._refresh_from_executor)

    def _on_trade_closed(self, event):
        QTimer.singleShot(0, self._refresh_from_executor)
        # Update Last Trade row from the closed trade data
        try:
            data  = event.data if hasattr(event, "data") else {}
            sym   = data.get("symbol", "?")
            side  = str(data.get("side", "")).upper()[:1]
            pnl   = float(data.get("pnl_usdt") or 0.0)
            sign  = "+" if pnl >= 0 else ""
            color = "#00CC77" if pnl >= 0 else "#FF3355"
            self._sig_last_trade.emit(f"{sym} {side} | {sign}${pnl:.2f}", color)
        except Exception:
            pass

    def _on_position_updated(self, event):
        from PySide6.QtCore import QTimer
        QTimer.singleShot(0, self._do_position_update)

    def _do_position_update(self):
        """Lightweight update — only refreshes portfolio value and drawdown."""
        try:
            from core.execution.paper_executor import paper_executor as _pe
            dd = _pe.drawdown_pct
            if dd > 0:
                dd_color = "#FF3355" if dd > 10 else "#FFB300"
                self._card_drawdown.set_value(f"-{dd:.2f}%", dd_color)
            else:
                self._card_drawdown.set_value("0.00%", "#00CC77")
            n_pos = len(_pe.get_open_positions())
            self._card_positions.set_value(str(n_pos))
        except Exception as exc:
            logger.debug("Dashboard position update: %s", exc)

    # ── EventBus callbacks — emit signals, never touch widgets directly ──

    def _on_signal_confirmed(self, event):
        data = event.data if hasattr(event, "data") else {}
        self._sig_signal_confirmed.emit(data if isinstance(data, dict) else {})

    def _on_tick_update(self, event):
        tickers = event.data if hasattr(event, "data") else event
        if isinstance(tickers, dict):
            self._sig_tickers.emit(tickers)

    def _on_exchange_status(self, event):
        data = event.data if hasattr(event, "data") else {}
        self._sig_exchange_connected.emit(
            data.get("name", "Exchange"),
            bool(data.get("connected", False)),
        )

    def _on_feed_status(self, event):
        data = event.data if hasattr(event, "data") else {}
        self._sig_feed_active.emit(bool(data.get("active", False)))

    def _on_exchange_error(self, event):
        data = event.data if hasattr(event, "data") else {}
        name   = data.get("name",   "Exchange")
        reason = data.get("reason", "Connection failed")
        self._sig_exchange_error.emit(name, reason)

    # ── Qt Slots — always run on main thread ───────────────

    @Slot(str, bool)
    def _update_exchange_connected(self, name: str, connected: bool):
        if connected:
            # Start the live feed now that the exchange is confirmed connected
            self._start_feed()
            # Update ML, strategy, and scanner status now that exchange is ready
            QTimer.singleShot(3000, self._update_ml_and_strategy_status)
            QTimer.singleShot(10000, self._update_scanner_row_startup)
        else:
            self._sig_scanner_status.emit("Waiting for exchange…", "#4A5568")

    @Slot(str, str)
    def _update_exchange_error(self, name: str, reason: str):
        # Exchange error visible in status bar; no duplicate row here
        pass

    @Slot(bool)
    def _update_feed_active(self, active: bool):
        # Feed status visible in status bar; no duplicate row here
        pass

    @Slot(str, str)
    def _update_scanner_row(self, text: str, color: str):
        self._row_scanner.set_status(text, color)

    @Slot(str, str)
    def _update_last_trade_row(self, text: str, color: str):
        self._row_last_trade.set_status(text, color)

    @Slot(object)
    def _update_tickers(self, tickers: dict):
        self._ticker_table.update_tickers(tickers)

    @Slot(object)
    def _update_signal_confirmed(self, data: dict):
        n = data.get("count", 0)
        self._confirmed_today += n
        self._signals_today   += n
        self._card_signals.set_value(str(self._signals_today))
        # _trades_opened_today = signals that passed risk gate and were auto-executed
        self._card_signals.set_sub(
            f"{self._signals_today} evaluated · {self._trades_opened_today} traded"
        )

    # ── ML Models & Strategy Engine dynamic status ─────────

    def _on_scan_cycle_complete(self, event):
        """Update strategy engine and scanner status rows when a scan cycle completes."""
        try:
            self._row_strategy.set_status("Active — IDSS Scanning", "#00FF88")
        except Exception:
            pass
        # Update scanner row with last scan freshness
        try:
            from core.scanning.scanner import scanner as _sc
            last = _sc._last_scan_at
            if last is not None:
                age_s = (datetime.now(timezone.utc) - last.replace(tzinfo=timezone.utc)).total_seconds()
                if age_s < 120:
                    ago = "just now"
                elif age_s < 3600:
                    ago = f"{int(age_s // 60)}m ago"
                else:
                    ago = f"{int(age_s // 3600)}h {int((age_s % 3600) // 60)}m ago"
                self._sig_scanner_status.emit(f"Running | Last scan: {ago}", "#00FF88")
            else:
                self._sig_scanner_status.emit("Running | No scan yet", "#FFB300")
        except Exception:
            pass

    def _update_scanner_row_startup(self):
        """Probe scanner state on startup to populate the Scanner row."""
        try:
            from core.scanning.scanner import scanner as _sc
            if _sc._running:
                last = _sc._last_scan_at
                if last is not None:
                    age_s = (datetime.now(timezone.utc) - last.replace(tzinfo=timezone.utc)).total_seconds()
                    ago = f"{int(age_s // 60)}m ago" if age_s < 3600 else f"{int(age_s // 3600)}h ago"
                    self._sig_scanner_status.emit(f"Running | Last scan: {ago}", "#00FF88")
                else:
                    self._sig_scanner_status.emit("Running | No scan yet", "#FFB300")
            else:
                self._sig_scanner_status.emit("Stopped", "#FF3355")
        except Exception:
            self._sig_scanner_status.emit("Unknown", "#4A5568")

        # Populate Last Trade row from closed trade history
        try:
            from core.execution.paper_executor import paper_executor as _pe
            closed = _pe._closed_trades
            if closed:
                t     = closed[-1]
                sym   = t.get("symbol", "?")
                side  = str(t.get("side", "")).upper()[:1]
                pnl   = float(t.get("pnl_usdt") or 0.0)
                sign  = "+" if pnl >= 0 else ""
                color = "#00CC77" if pnl >= 0 else "#FF3355"
                # Time ago
                closed_at = t.get("closed_at", "")
                try:
                    if isinstance(closed_at, str) and closed_at:
                        ts = datetime.fromisoformat(closed_at.replace("Z", "+00:00"))
                        age_s = (datetime.now(timezone.utc) - ts).total_seconds()
                        ago_str = f"{int(age_s // 60)}m ago" if age_s < 3600 else f"{int(age_s // 3600)}h ago"
                        self._sig_last_trade.emit(f"{sym} {side} | {ago_str} | {sign}${pnl:.2f}", color)
                    else:
                        self._sig_last_trade.emit(f"{sym} {side} | {sign}${pnl:.2f}", color)
                except Exception:
                    self._sig_last_trade.emit(f"{sym} {side} | {sign}${pnl:.2f}", color)
        except Exception:
            pass

    def _update_ml_and_strategy_status(self):
        """Probe actual ML model and strategy engine state and update status rows."""
        # ── ML Models ──
        ml_parts = []
        try:
            from config.settings import settings
            if settings.get("rl.enabled", False):
                ml_parts.append("RL Ensemble")
            if settings.get("finbert.enabled", True):
                ml_parts.append("FinBERT")
            # Check if RL trainer is actually running
            try:
                from core.scanning.scanner import rl_trainer
                if rl_trainer is not None:
                    ml_parts.append("RL Trainer")
            except Exception:
                pass
        except Exception:
            pass

        if ml_parts:
            self._row_ml.set_status(f"Loaded ({', '.join(ml_parts)})", "#00FF88")
        else:
            self._row_ml.set_status("Not Loaded", "#4A5568")

        # ── Strategy Engine ──
        try:
            from core.scanning.scanner import scanner as _scanner
            if _scanner._running:
                self._row_strategy.set_status("Active — IDSS Scanning", "#00FF88")
            else:
                self._row_strategy.set_status("Standby", "#4A5568")
        except Exception:
            self._row_strategy.set_status("Standby", "#4A5568")

        # ── Risk Manager — always active if we got this far ──
        self._row_risk.set_status("Active", "#00FF88")

    def _start_feed(self):
        """Start live data feed for watch symbols."""
        try:
            from core.market_data.data_feed import live_feed
            from core.market_data.exchange_manager import exchange_manager
            if exchange_manager.is_connected():
                live_feed.set_symbols(TickerTable.WATCH_SYMBOLS)
                if not live_feed.isRunning():
                    live_feed.start_feed()
        except Exception as e:
            logger.debug("Dashboard feed start: %s", e)
