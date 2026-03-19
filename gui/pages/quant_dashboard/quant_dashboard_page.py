# ============================================================
# NEXUS TRADER — Quant Trading Command Center
# Full-featured native Qt dashboard mirroring the HTML design
# ============================================================

import logging
import random
from datetime import datetime, timezone, timedelta
from typing import Optional

import pyqtgraph as pg
import numpy as np

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QFrame,
    QGridLayout, QPushButton, QSizePolicy, QTableWidget,
    QTableWidgetItem, QHeaderView, QAbstractItemView,
    QScrollArea, QProgressBar, QSpacerItem
)
from PySide6.QtCore import Qt, QTimer, Signal, Slot
from PySide6.QtGui import QColor, QFont, QPainter, QBrush, QPen, QLinearGradient

from gui.main_window import PageHeader
from core.event_bus import bus, Topics
from core.execution.paper_executor import paper_executor as _pe

logger = logging.getLogger(__name__)

# ── Color palette (matches HTML dashboard) ───────────────────
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

_PANEL_STYLE = f"""
    QFrame#panel {{
        background: {_BG_PANEL};
        border: 1px solid {_BORDER};
        border-radius: 6px;
    }}
"""
_CARD_STYLE = f"""
    QFrame#card {{
        background: {_BG_CARD};
        border: 1px solid {_BORDER_DIM};
        border-radius: 4px;
    }}
"""
_TABLE_STYLE = f"""
    QTableWidget {{
        background: transparent;
        border: none;
        gridline-color: {_BORDER_DIM};
        selection-background-color: #152035;
        font-size: 13px;
        color: {_TEXT_PRI};
    }}
    QTableWidget::item {{ padding: 5px 8px; border-bottom: 1px solid {_BORDER_DIM}; }}
    QTableWidget::item:selected {{ background: #152035; }}
    QHeaderView::section {{
        background: {_BG_PANEL};
        color: {_TEXT_DIM};
        font-size: 13px; font-weight: 700;
        letter-spacing: 1px; text-transform: uppercase;
        padding: 5px 8px;
        border: none;
        border-bottom: 1px solid {_BORDER_DIM};
    }}
"""

# ── Helpers ─────────────────────────────────────────────────

def _lbl(text: str, style: str = "") -> QLabel:
    l = QLabel(text)
    if style:
        l.setStyleSheet(style)
    return l


def _badge(text: str, bg: str, fg: str) -> QLabel:
    l = QLabel(text)
    l.setStyleSheet(
        f"background:{bg}; color:{fg}; font-size:13px; font-weight:700;"
        f"letter-spacing:.5px; padding:2px 7px; border-radius:3px;"
    )
    l.setFixedHeight(20)
    return l


def _side_badge(side: str) -> QLabel:
    if side.upper() in ("LONG", "BUY"):
        return _badge(side.upper(), "#003322", _BULL)
    elif side.upper() in ("SHORT", "SELL"):
        return _badge(side.upper(), "#330011", _BEAR)
    return _badge(side.upper(), "#1a2840", _BLUE)


def _sep_h() -> QFrame:
    f = QFrame()
    f.setFrameShape(QFrame.HLine)
    f.setFixedHeight(1)
    f.setStyleSheet(f"background: {_BORDER_DIM}; border: none;")
    return f


def _sep_v() -> QFrame:
    f = QFrame()
    f.setFrameShape(QFrame.VLine)
    f.setFixedWidth(1)
    f.setStyleSheet(f"background: {_BORDER}; border: none;")
    return f


def _section_header(title: str, accent: str = "") -> QFrame:
    """Standard panel header bar."""
    f = QFrame()
    f.setFixedHeight(32)
    f.setStyleSheet(f"background:{_BG_CARD}; border-bottom:1px solid {_BORDER_DIM};")
    h = QHBoxLayout(f)
    h.setContentsMargins(12, 0, 10, 0)
    h.setSpacing(6)
    parts = title.split("|", 1)
    main_lbl = _lbl(parts[0],
        f"font-size:13px; font-weight:700; letter-spacing:1.2px;"
        f"color:{_TEXT_MUT}; text-transform:uppercase;")
    h.addWidget(main_lbl)
    if len(parts) > 1:
        acc_lbl = _lbl(parts[1],
            f"font-size:13px; font-weight:700; color:{_CYAN};")
        h.addWidget(acc_lbl)
    h.addStretch()
    if accent:
        tag = _lbl(accent,
            f"font-size:13px; color:{_TEXT_DIM}; background:{_BG_BASE};"
            f"border:1px solid {_BORDER_DIM}; padding:1px 5px; border-radius:3px;")
        h.addWidget(tag)
    return f


# ── Live Ticker Strip ────────────────────────────────────────
class TickerStrip(QFrame):
    """Top bar with live price chips."""

    COINS = [
        ("BTC", "#f7931a", "BTC/USDT"),
        ("ETH", "#627eea", "ETH/USDT"),
        ("SOL", "#9945ff", "SOL/USDT"),
        ("BNB", "#f3ba2f", "BNB/USDT"),
        ("XRP", "#00aae4", "XRP/USDT"),
    ]

    # Placeholder prices shown before the live feed connects.
    # These are intentionally set to 0 so it's obvious they're not live yet.
    # The live feed (REST polling via exchange_manager) will overwrite these
    # within a few seconds once KuCoin connects.
    _BASE_PRICES = {
        "BTC/USDT": 0.0,
        "ETH/USDT": 0.0,
        "SOL/USDT": 0.0,
        "BNB/USDT": 0.0,
        "XRP/USDT": 0.0,
    }

    _live_data_received = False  # class-level flag: stop sim once real data arrives

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("ticker_strip")
        self.setFixedHeight(46)
        self.setStyleSheet(
            f"QFrame#ticker_strip {{ background:{_BG_PANEL}; border-bottom:1px solid {_BORDER}; }}"
        )
        self._price_lbls: dict[str, QLabel] = {}
        self._chg_lbls:   dict[str, QLabel] = {}
        self._prices = dict(self._BASE_PRICES)
        self._changes: dict[str, float] = {k: 0.0 for k in self._BASE_PRICES}
        self._build()
        # Subscribe to real prices
        try:
            bus.subscribe(Topics.TICK_UPDATE, self._on_ticker)
        except Exception:
            pass
        # Fallback sim
        self._sim = QTimer()
        self._sim.timeout.connect(self._tick_sim)
        self._sim.start(1500)

    def _build(self):
        h = QHBoxLayout(self)
        h.setContentsMargins(16, 0, 16, 0)
        h.setSpacing(0)

        # Logo
        logo = _lbl("NEXUS<span style='color:#637a96;font-weight:400;'>TRADER</span>",
            f"font-size:14px; font-weight:700; letter-spacing:2px; color:{_CYAN};")
        logo.setTextFormat(Qt.RichText)
        h.addWidget(logo)

        h.addSpacing(24)

        # Price chips
        for coin, color, symbol in self.COINS:
            chip = QFrame()
            chip.setStyleSheet("background:transparent;")
            ch = QHBoxLayout(chip)
            ch.setContentsMargins(0, 0, 0, 0)
            ch.setSpacing(6)

            icon = _lbl(coin[:1],
                f"background:{color}22; color:{color}; font-size:13px; font-weight:700;"
                f"padding:1px 5px; border-radius:8px; min-width:18px;")
            icon.setAlignment(Qt.AlignCenter)

            name_lbl = _lbl(coin,
                f"color:{_TEXT_MUT}; font-size:13px; font-weight:600;")

            p = self._prices[symbol]
            price_lbl = _lbl(f"${p:,.2f}" if p > 10 else f"${p:.4f}",
                f"color:{_TEXT_PRI}; font-size:13px; font-weight:600;")
            chg_lbl = _lbl("  0.00%",
                f"color:{_TEXT_MUT}; font-size:13px;")

            self._price_lbls[symbol] = price_lbl
            self._chg_lbls[symbol]   = chg_lbl

            ch.addWidget(icon)
            ch.addWidget(name_lbl)
            ch.addWidget(price_lbl)
            ch.addWidget(chg_lbl)
            h.addWidget(chip)
            h.addSpacing(20)

        h.addStretch()

        # Status dot + clock
        status_row = QHBoxLayout()
        status_row.setSpacing(6)
        dot = _lbl("●",
            f"color:{_BULL}; font-size:13px;")
        status_lbl = _lbl("LIVE",
            f"color:{_TEXT_MUT}; font-size:13px;")
        self._clock_lbl = _lbl("—",
            f"color:{_TEXT_MUT}; font-size:13px;")
        status_row.addWidget(dot)
        status_row.addWidget(status_lbl)
        status_row.addSpacing(12)
        status_row.addWidget(self._clock_lbl)
        h.addLayout(status_row)

        # Clock timer
        self._clock_timer = QTimer()
        self._clock_timer.timeout.connect(self._update_clock)
        self._clock_timer.start(1000)
        self._update_clock()

    def _update_clock(self):
        self._clock_lbl.setText(datetime.utcnow().strftime("%H:%M:%S UTC"))

    def _on_ticker(self, event):
        """Handle TICK_UPDATE events from the live data feed.

        The event data is a dict of {symbol: {last, change, volume, ...}}.
        Called from the data-feed thread — we marshal to main thread via QTimer.
        """
        try:
            tickers = event.data if hasattr(event, "data") else event
            if not isinstance(tickers, dict):
                return
            # Build update list first (thread-safe read), then schedule UI update
            updates = []
            for sym, ticker in tickers.items():
                if sym in self._price_lbls and isinstance(ticker, dict):
                    price = ticker.get("last", 0)
                    chg   = ticker.get("change", 0.0)
                    if price and price > 0:
                        updates.append((sym, float(price), float(chg)))
            if updates:
                QTimer.singleShot(0, lambda u=updates: self._apply_ticker_updates(u))
        except Exception as exc:
            logger.debug("TickerStrip._on_ticker error: %s", exc)

    def _apply_ticker_updates(self, updates: list):
        """Apply live ticker updates on the main thread."""
        TickerStrip._live_data_received = True
        for sym, price, chg in updates:
            self._prices[sym]  = price
            self._changes[sym] = chg
            self._update_chip(sym, price, chg)

    def _tick_sim(self):
        # Stop simulation once real live data has been received
        if TickerStrip._live_data_received:
            return
        # Only simulate if we have non-zero base prices to work from
        for sym in self._prices:
            if self._prices[sym] <= 0:
                continue
            move = random.gauss(0, 0.0003)
            self._prices[sym] *= (1 + move)
            self._changes[sym] += move * 100
            self._update_chip(sym, self._prices[sym], self._changes[sym])

    def _update_chip(self, sym: str, price: float, chg_pct: float):
        if sym not in self._price_lbls:
            return
        fmt = f"${price:,.2f}" if price > 10 else f"${price:.4f}"
        self._price_lbls[sym].setText(fmt)
        sign = "+" if chg_pct >= 0 else ""
        color = _BULL if chg_pct >= 0 else _BEAR
        self._chg_lbls[sym].setText(f" {sign}{chg_pct:.2f}%")
        self._chg_lbls[sym].setStyleSheet(f"color:{color}; font-size:13px;")


# ── Mini Candlestick Chart ───────────────────────────────────
class _CmdDateAxis(pg.AxisItem):
    """Maps integer bar indices → pre-formatted date label strings.

    Avoids pyqtgraph's built-in DateAxisItem which breaks at Unix timestamp
    scale (~1.74e9 seconds) and renders labels like "00.050", "00.100".
    """

    def __init__(self):
        super().__init__(orientation="bottom")
        self._labels: list = []
        self.setStyle(tickTextOffset=4)

    def set_labels(self, labels: list):
        self._labels = labels

    def tickStrings(self, values, scale, spacing):
        result = []
        for v in values:
            idx = int(round(v))
            if 0 <= idx < len(self._labels):
                result.append(self._labels[idx])
            else:
                result.append("")
        return result


class CommandChart(QFrame):
    """OHLCV candlestick chart for the command center.

    Uses integer bar indices (0, 1, 2 …) on the x-axis to avoid pyqtgraph's
    float-precision problems with Unix timestamps (~1.74e9 seconds).
    Date strings are stored in _CmdDateAxis and mapped at paint time.
    Fetches 120 candles from KuCoin; falls back to realistic demo data.
    """

    TF_BUTTONS = ["1m", "5m", "15m", "1h", "4h", "1d"]
    _TF_SECS   = {"1m": 60, "5m": 300, "15m": 900,
                  "1h": 3600, "4h": 14400, "1d": 86400}
    _DEMO_PRICE = {"BTC/USDT": 70_000.0, "ETH/USDT": 3_500.0, "BNB/USDT": 650.0, "SOL/USDT": 140.0, "XRP/USDT": 2.0}

    # Signal for safe cross-thread data delivery — background fetch thread
    # emits this; Qt automatically dispatches it to the main thread.
    _candles_ready = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("panel")
        self.setStyleSheet(_PANEL_STYLE)
        self._symbol = "BTC/USDT"
        self._tf     = "1h"
        self._build()

        # Wire cross-thread signal → slot (always runs on main thread)
        self._candles_ready.connect(self._render_real)

        # First attempt after 3 s (exchange may still be loading markets)
        QTimer.singleShot(3000, self._load_chart)

        # Periodic refresh every 30 s — reloads OHLCV chart data from exchange
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setInterval(30_000)
        self._refresh_timer.timeout.connect(self._load_chart)
        self._refresh_timer.start()

        try:
            bus.subscribe(Topics.EXCHANGE_CONNECTED,
                          lambda _ev: QTimer.singleShot(500, self._load_chart))
        except Exception:
            pass

    def _build(self):
        vlay = QVBoxLayout(self)
        vlay.setContentsMargins(0, 0, 0, 0)
        vlay.setSpacing(0)

        # ── Header ─────────────────────────────────
        hdr = QFrame()
        hdr.setFixedHeight(36)
        hdr.setStyleSheet(
            f"background:transparent; border-bottom:1px solid {_BORDER_DIM};"
        )
        hh = QHBoxLayout(hdr)
        hh.setContentsMargins(12, 0, 10, 0)
        hh.setSpacing(8)

        self._sym_btns: dict = {}
        for sym in ["BTC/USDT", "ETH/USDT", "BNB/USDT", "SOL/USDT", "XRP/USDT"]:
            b = QPushButton(sym.replace("/USDT", ""))
            b.setFixedHeight(22)
            b.setStyleSheet(self._btn_style(sym == self._symbol))
            b.clicked.connect(lambda _, s=sym: self._set_symbol(s))
            self._sym_btns[sym] = b
            hh.addWidget(b)

        hh.addWidget(_sep_v())

        self._tf_btns: dict = {}
        for tf in self.TF_BUTTONS:
            b = QPushButton(tf)
            b.setFixedHeight(22)
            b.setStyleSheet(self._btn_style(tf == self._tf))
            b.clicked.connect(lambda _, t=tf: self._set_tf(t))
            self._tf_btns[tf] = b
            hh.addWidget(b)

        hh.addStretch()

        self._price_lbl = _lbl("—",
            f"font-size:16px; font-weight:700; color:{_TEXT_PRI};")
        self._chg_lbl = _lbl("",
            f"font-size:13px; color:{_TEXT_MUT};")
        hh.addWidget(self._price_lbl)
        hh.addWidget(self._chg_lbl)
        vlay.addWidget(hdr)

        # ── Chart ──────────────────────────────────
        self._date_axis = _CmdDateAxis()
        self._chart = pg.PlotWidget(
            background=_BG_BASE,
            axisItems={"bottom": self._date_axis},
        )
        self._chart.setMenuEnabled(True)
        self._chart.showGrid(x=False, y=True, alpha=0.12)
        for ax in ("bottom", "left", "right", "top"):
            self._chart.getAxis(ax).setTextPen(pg.mkPen(_TEXT_MUT))
            self._chart.getAxis(ax).setPen(pg.mkPen(_BORDER))
        self._chart.getAxis("bottom").setStyle(tickTextOffset=6)
        self._chart.getAxis("left").setStyle(tickTextOffset=4)
        self._chart.setMinimumHeight(180)

        # Disable auto-range BEFORE any items are added.
        # pyqtgraph's _autoRangeNeedsUpdate flag (set by addItem) would otherwise
        # override explicit setXRange/setYRange calls via prepareForPaint().
        self._chart.getViewBox().disableAutoRange()

        vlay.addWidget(self._chart, 1)

    # ── Button helpers ──────────────────────────────────────────
    def _btn_style(self, active: bool) -> str:
        if active:
            return (
                f"QPushButton {{ background:{_BLUE}; color:#fff; border:1px solid {_BLUE};"
                f"border-radius:3px; font-size:13px; padding:2px 10px; }}"
            )
        return (
            f"QPushButton {{ background:transparent; color:{_TEXT_MUT};"
            f"border:1px solid {_BORDER}; border-radius:3px; font-size:13px; padding:2px 10px; }}"
            f"QPushButton:hover {{ background:{_BLUE}; color:#fff; border-color:{_BLUE}; }}"
        )

    def _set_symbol(self, sym: str):
        self._symbol = sym
        for s, b in self._sym_btns.items():
            b.setStyleSheet(self._btn_style(s == sym))
        self._load_chart()

    def _set_tf(self, tf: str):
        self._tf = tf
        for t, b in self._tf_btns.items():
            b.setStyleSheet(self._btn_style(t == tf))
        self._load_chart()

    # ── Data loading ────────────────────────────────────────────
    def _load_chart(self):
        """Fetch OHLCV on a background thread; render on the main thread."""
        import threading
        symbol, tf = self._symbol, self._tf

        def _fetch():
            try:
                from core.market_data.exchange_manager import exchange_manager
                if not exchange_manager.is_connected():
                    QTimer.singleShot(0, self._load_demo)
                    return
                candles = exchange_manager.fetch_ohlcv(symbol, tf, limit=120)
                n = len(candles) if candles else 0
                logger.info("CommandChart: %d candles for %s %s", n, symbol, tf)
                if candles and n >= 10:
                    # Emit signal — Qt marshals this to the main thread safely
                    self._candles_ready.emit(candles)
                else:
                    QTimer.singleShot(0, self._load_demo)
            except Exception as exc:
                logger.warning("CommandChart fetch failed (%s %s): %s", symbol, tf, exc)
                QTimer.singleShot(0, self._load_demo)

        threading.Thread(target=_fetch, daemon=True).start()

    @Slot(object)
    def _render_real(self, candles: list):
        """Convert raw CCXT candles [[ts_ms, o, h, l, c, v], …] → draw.
        Called on the main thread via _candles_ready signal.
        """
        try:
            if not candles:
                return
            daily = self._tf in ("1d", "3d", "1w")
            rows, labels = [], []
            for i, c in enumerate(candles):
                rows.append({
                    "t": i,
                    "o": float(c[1]), "h": float(c[2]),
                    "l": float(c[3]), "c": float(c[4]),
                })
                dt = datetime.fromtimestamp(int(c[0]) / 1000.0, tz=timezone.utc)
                labels.append(
                    dt.strftime("%b %d '%y") if daily else dt.strftime("%b %d %H:%M")
                )
            self._draw(rows, labels)
        except Exception:
            logger.exception("CommandChart._render_real failed")

    def _load_demo(self):
        """Placeholder candles (realistic prices) shown when exchange is offline."""
        import time as _time
        n          = 120
        interval_s = self._TF_SECS.get(self._tf, 3600)
        now_s      = int(_time.time())
        p          = self._DEMO_PRICE.get(self._symbol, 1_000.0)
        daily      = self._tf in ("1d", "3d", "1w")

        closes = [p]
        for _ in range(n - 1):
            closes.append(closes[-1] * (1 + random.gauss(0, 0.004)))

        rows, labels = [], []
        for i, c in enumerate(closes):
            ts_s = now_s - (n - 1 - i) * interval_s
            o = closes[i - 1] if i > 0 else c * (1 + random.gauss(0, 0.002))
            h = max(o, c) * (1 + abs(random.gauss(0, 0.003)))
            l = min(o, c) * (1 - abs(random.gauss(0, 0.003)))
            rows.append({"t": i, "o": o, "h": h, "l": l, "c": c})
            dt = datetime.fromtimestamp(ts_s, tz=timezone.utc)
            labels.append(
                dt.strftime("%b %d '%y") if daily else dt.strftime("%b %d %H:%M")
            )

        logger.info("CommandChart demo: %s p=%.2f n=%d", self._symbol, p, n)
        self._draw(rows, labels)

    # ── Core renderer ───────────────────────────────────────────
    def _draw(self, rows: list, labels: list):
        """Render candles using integer bar indices — no float-precision issues."""
        try:
            n = len(rows)
            if n == 0:
                return

            vb = self._chart.getViewBox()
            # Re-disable before every draw: clear() can re-enable auto-range
            vb.disableAutoRange()
            self._chart.clear()
            self._date_axis.set_labels(labels)

            xs   = np.array([d["t"] for d in rows], dtype=np.float64)
            op_a = np.array([d["o"] for d in rows], dtype=np.float64)
            hi_a = np.array([d["h"] for d in rows], dtype=np.float64)
            lo_a = np.array([d["l"] for d in rows], dtype=np.float64)
            cl_a = np.array([d["c"] for d in rows], dtype=np.float64)

            # ── Wicks: high-low line per candle, NaN-separated ──
            wx = np.empty(n * 3, dtype=np.float64)
            wy = np.empty(n * 3, dtype=np.float64)
            wx[0::3] = xs;    wx[1::3] = xs;    wx[2::3] = np.nan
            wy[0::3] = lo_a;  wy[1::3] = hi_a;  wy[2::3] = np.nan
            self._chart.addItem(
                pg.PlotDataItem(
                    x=wx, y=wy,
                    pen=pg.mkPen(color=_TEXT_DIM, width=1),
                    connect="finite",
                )
            )

            # ── Bodies: bull (green) and bear (red) BarGraphItems ──
            bull = cl_a >= op_a
            bear = ~bull
            bar_w = 0.6

            if bull.any():
                self._chart.addItem(pg.BarGraphItem(
                    x=xs[bull],
                    height=np.abs(cl_a[bull] - op_a[bull]),
                    width=bar_w,
                    y0=np.minimum(op_a[bull], cl_a[bull]),
                    brush=pg.mkBrush(_BULL),
                    pen=pg.mkPen(_BULL),
                ))

            if bear.any():
                self._chart.addItem(pg.BarGraphItem(
                    x=xs[bear],
                    height=np.abs(cl_a[bear] - op_a[bear]),
                    width=bar_w,
                    y0=np.minimum(op_a[bear], cl_a[bear]),
                    brush=pg.mkBrush(_BEAR),
                    pen=pg.mkPen(_BEAR),
                ))

            # ── Lock viewport — single atomic call prevents auto-range revert ──
            y_min = float(lo_a.min())
            y_max = float(hi_a.max())
            y_pad = max((y_max - y_min) * 0.06, y_max * 0.001)
            x_pad = (n - 1) * 0.02
            vb.setRange(
                xRange=(-x_pad, n - 1 + x_pad),
                yRange=(y_min - y_pad, y_max + y_pad),
                padding=0,
                disableAutoRange=True,
            )

            logger.info("CommandChart drawn: n=%d  y=[%.2f..%.2f]", n, y_min, y_max)

            # ── Price readout in header ──
            last = float(cl_a[-1])
            prev = float(cl_a[-2]) if n >= 2 else last
            chg  = (last - prev) / prev * 100 if prev else 0
            fmt  = f"${last:,.2f}" if last > 10 else f"${last:.4f}"
            self._price_lbl.setText(fmt)
            sign  = "+" if chg >= 0 else ""
            color = _BULL if chg >= 0 else _BEAR
            self._chg_lbl.setText(f"  {sign}{chg:.2f}%")
            self._chg_lbl.setStyleSheet(f"color:{color}; font-size:13px;")

        except Exception:
            logger.exception("CommandChart._draw failed")


# ── Bot Decision Engine ──────────────────────────────────────
class BotDecisionPanel(QFrame):
    """Cards showing latest bot trade decisions from real SIGNAL_CONFIRMED events."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("panel")
        self.setStyleSheet(_PANEL_STYLE)
        self._build()
        try:
            bus.subscribe(Topics.SIGNAL_CONFIRMED, self._on_signal)
        except Exception:
            pass

    def _build(self):
        vlay = QVBoxLayout(self)
        vlay.setContentsMargins(0, 0, 0, 0)
        vlay.setSpacing(0)
        vlay.addWidget(_section_header("Bot Decision |Engine", "LIVE"))

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setStyleSheet(
            "QScrollArea{background:transparent;border:none;}"
            f"QScrollBar:vertical{{background:{_BG_PANEL};width:4px;border-radius:2px;}}"
            f"QScrollBar::handle:vertical{{background:{_BORDER};border-radius:2px;}}"
            "QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical{height:0;}"
        )
        self._inner = QWidget()
        self._inner.setStyleSheet("background:transparent;")
        self._inner_lay = QVBoxLayout(self._inner)
        self._inner_lay.setContentsMargins(10, 8, 10, 8)
        self._inner_lay.setSpacing(8)

        # Show placeholder if no signals yet
        self._placeholder = _lbl("Awaiting AI signals from IDSS scanner...",
            f"font-size:13px; color:{_TEXT_DIM}; font-style:italic;")
        self._inner_lay.addWidget(self._placeholder)
        self._inner_lay.addStretch()
        scroll.setWidget(self._inner)
        vlay.addWidget(scroll, 1)

    @Slot(object)
    def _on_signal(self, event):
        """Process real SIGNAL_CONFIRMED event."""
        data = event.data if hasattr(event, "data") else {}
        if not data:
            return
        # Hide placeholder on first signal
        if self._placeholder and self._placeholder.parent():
            self._inner_lay.removeWidget(self._placeholder)
            self._placeholder.deleteLater()
            self._placeholder = None
        # Create card from signal data
        card = self._make_card(data)
        self._inner_lay.insertWidget(0, card)
        # Keep max 10 cards
        while self._inner_lay.count() > 11:  # +1 for stretch
            old = self._inner_lay.takeAt(self._inner_lay.count() - 2)
            if old and old.widget():
                old.widget().deleteLater()

    def _make_card(self, d: dict) -> QFrame:
        """Create a signal card from signal data dict."""
        card = QFrame()
        card.setObjectName("card")
        card.setStyleSheet(_CARD_STYLE)
        vlay = QVBoxLayout(card)
        vlay.setContentsMargins(10, 10, 10, 10)
        vlay.setSpacing(6)

        # Top row: symbol + action badge
        top = QHBoxLayout()
        sym = d.get("symbol", "?")
        action = d.get("action", "HOLD")
        sym_lbl = _lbl(sym,
            f"font-size:13px; font-weight:700; color:{_TEXT_PRI};")
        score = d.get("score", 0.5)
        score_lbl = _lbl(f"{score:.2f}",
            f"font-size:13px; color:{_TEXT_MUT}; letter-spacing:.5px;")
        action_badge = _side_badge(action)
        top.addWidget(sym_lbl)
        top.addSpacing(6)
        top.addWidget(score_lbl)
        top.addStretch()
        top.addWidget(action_badge)
        vlay.addLayout(top)

        # Timeframe + indicators
        tf = d.get("timeframe", "1h")
        indicators = d.get("indicators", {})
        info_text = f"TF: {tf}"
        if indicators:
            parts = []
            if indicators.get("ema"):
                parts.append(f"EMA:{indicators['ema']}")
            if indicators.get("rsi"):
                parts.append(f"RSI:{indicators['rsi']:.0f}")
            if parts:
                info_text += " · " + " · ".join(parts)
        row = QHBoxLayout()
        row.setSpacing(5)
        info_lbl = _lbl(info_text, f"font-size:13px; color:{_TEXT_MUT};")
        row.addWidget(info_lbl)
        row.addStretch()
        vlay.addLayout(row)

        # Condition/reason
        condition = d.get("condition", d.get("reason", ""))
        if condition:
            cond_lbl = _lbl(condition, f"font-size:13px; color:{_TEXT_DIM};")
            vlay.addWidget(cond_lbl)

        vlay.addWidget(_sep_h())

        # Footer: score + timestamp
        footer = QHBoxLayout()
        score_pct = int(score * 100)
        footer.addWidget(_lbl("Signal Confirmed",
            f"font-size:13px; color:{_TEXT_MUT};"))
        footer.addStretch()
        footer.addWidget(_lbl(f"{score_pct}%",
            f"font-size:13px; font-weight:700; color:{_CYAN};"))
        vlay.addLayout(footer)

        return card


# ── AI Signal Analysis Table ─────────────────────────────────
class AISignalPanel(QFrame):
    """Multi-model AI signal grid populated from real SIGNAL_CONFIRMED events."""

    _COLS = ["Symbol", "TF", "Action", "Score", "EMA", "RSI", "MACD", "BB", "HMM", "Condition"]

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("panel")
        self.setStyleSheet(_PANEL_STYLE)
        self._signals = []
        self._tbl = None
        self._build()
        try:
            bus.subscribe(Topics.SIGNAL_CONFIRMED, self._on_signal)
        except Exception:
            pass

    def _build(self):
        vlay = QVBoxLayout(self)
        vlay.setContentsMargins(0, 0, 0, 0)
        vlay.setSpacing(0)
        vlay.addWidget(_section_header("AI Signal |Analysis", "MULTI-MODEL"))

        self._tbl = QTableWidget(0, len(self._COLS))
        self._tbl.setHorizontalHeaderLabels(self._COLS)
        self._tbl.setStyleSheet(_TABLE_STYLE)
        self._tbl.horizontalHeader().setStretchLastSection(True)
        self._tbl.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self._tbl.verticalHeader().setVisible(False)
        self._tbl.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._tbl.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._tbl.setAlternatingRowColors(False)
        self._tbl.setShowGrid(False)

        vlay.addWidget(self._tbl, 1)

    @Slot(object)
    def _on_signal(self, event):
        """Process SIGNAL_CONFIRMED event and add row to table."""
        data = event.data if hasattr(event, "data") else {}
        if not data:
            return
        # Add to internal list (keep latest 12)
        self._signals.insert(0, data)
        if len(self._signals) > 12:
            self._signals.pop()
        # Refresh table
        QTimer.singleShot(0, self._refresh_table)

    def _refresh_table(self):
        """Rebuild table with current signals."""
        self._tbl.setRowCount(len(self._signals))
        for r, sig_data in enumerate(self._signals):
            sym = sig_data.get("symbol", "?")
            tf = sig_data.get("timeframe", "1h")
            action = sig_data.get("action", "HOLD")
            score = sig_data.get("score", 0.5)
            indicators = sig_data.get("indicators", {})
            ema_val = indicators.get("ema", "→")
            rsi_val = indicators.get("rsi", 50)
            macd_val = indicators.get("macd", "→")
            bb_val = indicators.get("bb", "Mid")
            hmm_val = indicators.get("hmm", "Range")
            cond = sig_data.get("condition", sig_data.get("reason", ""))

            vals = [sym, tf, action, f"{score:.2f}", ema_val, f"{rsi_val:.0f}", macd_val, bb_val, hmm_val, cond]
            colors = [
                _TEXT_PRI, _TEXT_MUT,
                _BULL if action == "BUY" else (_BEAR if action == "SELL" else _BLUE),
                _WARN if score < 0.65 else (_BULL if score >= 0.80 else _TEXT_PRI),
                _BULL if ema_val == "↑" else (_BEAR if ema_val == "↓" else _TEXT_MUT),
                _BEAR if rsi_val > 65 else (_BULL if rsi_val < 40 else _TEXT_PRI),
                _BULL if macd_val == "↑" else (_BEAR if macd_val == "↓" else _TEXT_MUT),
                _BEAR if bb_val == "Upper" else (_BULL if bb_val == "Lower" else _TEXT_MUT),
                _BULL if hmm_val == "Bull" else (_BEAR if hmm_val == "Bear" else _WARN),
                _TEXT_MUT,
            ]
            for c, (val, col) in enumerate(zip(vals, colors)):
                item = QTableWidgetItem(val)
                item.setForeground(QColor(col))
                item.setFlags(Qt.ItemIsSelectable | Qt.ItemIsEnabled)
                self._tbl.setItem(r, c, item)

        self._tbl.resizeRowsToContents()


# ── Current Positions ─────────────────────────────────────────
class PositionsPanel(QFrame):
    """Open positions table from real paper executor."""

    _COLS = ["Symbol", "Side", "Entry", "Mark", "Unreal P&L", "Stop", "Target", "Score", "Age"]

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("panel")
        self.setStyleSheet(_PANEL_STYLE)
        self._tbl = None
        self._header = None
        self._build()
        try:
            bus.subscribe(Topics.TRADE_OPENED, self._on_position_change)
            bus.subscribe(Topics.TRADE_CLOSED, self._on_position_change)
            bus.subscribe(Topics.POSITION_UPDATED, self._on_position_change)
        except Exception:
            pass
        # Initial load
        QTimer.singleShot(0, self._refresh)

    def _build(self):
        vlay = QVBoxLayout(self)
        vlay.setContentsMargins(0, 0, 0, 0)
        vlay.setSpacing(0)
        self._header = _section_header("Current |Positions", "0 OPEN")
        vlay.addWidget(self._header)

        self._tbl = QTableWidget(0, len(self._COLS))
        self._tbl.setHorizontalHeaderLabels(self._COLS)
        self._tbl.setStyleSheet(_TABLE_STYLE)
        self._tbl.horizontalHeader().setStretchLastSection(False)
        self._tbl.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self._tbl.verticalHeader().setVisible(False)
        self._tbl.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._tbl.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._tbl.setShowGrid(False)

        vlay.addWidget(self._tbl, 1)

    @Slot(object)
    def _on_position_change(self, event):
        """Handle position change events."""
        QTimer.singleShot(0, self._refresh)

    def _refresh(self):
        """Refresh table with current positions from paper executor."""
        positions = _pe.get_open_positions()
        self._tbl.setRowCount(len(positions))

        # Update header count
        count_text = f"{len(positions)} OPEN"
        header_widget = self._header
        if header_widget.children():
            for child in header_widget.children():
                if isinstance(child, QHBoxLayout):
                    continue
        # Find and update the tag label (accent part) in header
        for child in header_widget.findChildren(QLabel):
            if "OPEN" in child.text() or child.text().isdigit():
                child.setText(count_text)
                break

        for r, pos in enumerate(positions):
            sym = pos.get("symbol", "?")
            side = pos.get("side", "LONG")
            entry = pos.get("entry_price", 0.0)
            mark = pos.get("mark_price", entry)
            upnl_pct = ((mark - entry) / entry * 100) if entry else 0
            if side.upper() in ("SHORT", "SELL"):
                upnl_pct = -upnl_pct
            sl = pos.get("stop_loss", 0.0)
            tp = pos.get("take_profit", 0.0)
            score = pos.get("score", 0.5)
            opened_at = pos.get("opened_at")
            age = "—"
            if opened_at:
                try:
                    if isinstance(opened_at, str):
                        from datetime import datetime as dt
                        opened = dt.fromisoformat(opened_at.replace('Z', '+00:00'))
                        now = dt.now(opened.tzinfo) if opened.tzinfo else dt.utcnow()
                        delta = now - opened
                        hours = delta.total_seconds() // 3600
                        mins = (delta.total_seconds() % 3600) // 60
                        if hours > 0:
                            age = f"{int(hours)}h {int(mins):02d}m"
                        else:
                            age = f"{int(mins)}m"
                except Exception:
                    age = "—"

            items = [
                (sym,                           _TEXT_PRI),
                (side.upper(),                  _BULL if side.upper() in ("LONG", "BUY") else _BEAR),
                (f"${entry:,.2f}" if entry > 1 else f"${entry:.6f}",        _TEXT_MUT),
                (f"${mark:,.2f}" if mark > 1 else f"${mark:.6f}",         _TEXT_PRI),
                (f"{upnl_pct:+.3f}%",           _BULL if upnl_pct >= 0 else _BEAR),
                (f"${sl:,.2f}" if sl > 1 else ("—" if sl == 0 else f"${sl:.6f}"),           _BEAR),
                (f"${tp:,.2f}" if tp > 1 else ("—" if tp == 0 else f"${tp:.6f}"),           _BULL),
                (f"{score:.2f}",                _WARN if score < 0.65 else _BULL),
                (age,                           _TEXT_MUT),
            ]
            for c, (text, col) in enumerate(items):
                item = QTableWidgetItem(text)
                item.setForeground(QColor(col))
                item.setFlags(Qt.ItemIsSelectable | Qt.ItemIsEnabled)
                self._tbl.setItem(r, c, item)

        self._tbl.resizeRowsToContents()


# ── Portfolio Summary ─────────────────────────────────────────
class PortfolioPanel(QFrame):
    """Portfolio metrics from paper executor + risk bars."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("panel")
        self.setStyleSheet(_PANEL_STYLE)
        self._metric_labels = {}
        self._build()
        try:
            bus.subscribe(Topics.TRADE_OPENED, self._on_update)
            bus.subscribe(Topics.TRADE_CLOSED, self._on_update)
        except Exception:
            pass
        # Initial load
        QTimer.singleShot(0, self._refresh)

    def _build(self):
        vlay = QVBoxLayout(self)
        vlay.setContentsMargins(0, 0, 0, 0)
        vlay.setSpacing(0)
        vlay.addWidget(_section_header("Portfolio |Summary"))

        body = QWidget()
        body.setStyleSheet("background:transparent;")
        blay = QVBoxLayout(body)
        blay.setContentsMargins(10, 8, 10, 8)
        blay.setSpacing(8)

        # 2×2 metric grid
        grid = QGridLayout()
        grid.setSpacing(6)
        metric_keys = [
            ("EQUITY",      _TEXT_PRI),
            ("DAY P&L",     _BULL),
            ("OPEN P&L",    _BULL),
            ("DRAWDOWN",    _BEAR),
            ("WIN RATE",    _BULL),
            ("TOTAL TRADES",_TEXT_PRI),
        ]
        for i, (label, color) in enumerate(metric_keys):
            card = QFrame()
            card.setObjectName("card")
            card.setStyleSheet(_CARD_STYLE)
            cl = QVBoxLayout(card)
            cl.setContentsMargins(10, 8, 10, 8)
            cl.setSpacing(2)
            cl.addWidget(_lbl(label, f"font-size:13px; color:{_TEXT_MUT}; letter-spacing:.5px;"))
            val_lbl = _lbl("—", f"font-size:17px; font-weight:700; color:{color};")
            self._metric_labels[label] = (val_lbl, color)
            cl.addWidget(val_lbl)
            grid.addWidget(card, i // 2, i % 2)
        blay.addLayout(grid)

        # Risk bars
        blay.addWidget(_sep_h())
        blay.addWidget(_lbl("RISK EXPOSURE",
            f"font-size:13px; letter-spacing:1px; color:{_TEXT_MUT};"))
        blay.addSpacing(4)

        risk_bars = [
            ("PORTFOLIO RISK",  _BLUE),
            ("LEVERAGE",        _WARN),
            ("VOLATILITY EXP.", _BULL),
            ("CORRELATION",     _BEAR),
        ]
        self._risk_bars = {}
        for label, color in risk_bars:
            row = QVBoxLayout()
            row.setSpacing(2)
            lrow = QHBoxLayout()
            lrow.addWidget(_lbl(label, f"font-size:13px; color:{_TEXT_MUT};"))
            lrow.addStretch()
            pct_lbl = _lbl("—", f"font-size:13px; font-weight:700; color:{color};")
            lrow.addWidget(pct_lbl)
            row.addLayout(lrow)
            bar = QProgressBar()
            bar.setRange(0, 100)
            bar.setValue(0)
            bar.setFixedHeight(6)
            bar.setTextVisible(False)
            bar.setStyleSheet(
                f"QProgressBar{{background:{_BORDER};border-radius:3px;border:none;}}"
                f"QProgressBar::chunk{{background:{color};border-radius:3px;}}"
            )
            row.addWidget(bar)
            self._risk_bars[label] = (bar, pct_lbl)
            blay.addLayout(row)
            blay.addSpacing(2)

        # Overall risk score
        score_frame = QFrame()
        score_frame.setObjectName("card")
        score_frame.setStyleSheet(_CARD_STYLE)
        sf = QHBoxLayout(score_frame)
        sf.setContentsMargins(10, 8, 10, 8)
        sf.addWidget(_lbl("OVERALL RISK SCORE", f"font-size:13px; color:{_TEXT_MUT};"))
        sf.addStretch()
        self._risk_score_lbl = _lbl("—", f"font-size:20px; font-weight:700; color:{_WARN};")
        sf.addWidget(self._risk_score_lbl)
        blay.addWidget(score_frame)

        blay.addStretch()

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setStyleSheet(
            "QScrollArea{background:transparent;border:none;}"
            f"QScrollBar:vertical{{background:{_BG_PANEL};width:4px;}}"
            f"QScrollBar::handle:vertical{{background:{_BORDER};border-radius:2px;}}"
            "QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical{height:0;}"
        )
        scroll.setWidget(body)
        vlay.addWidget(scroll, 1)

    @Slot(object)
    def _on_update(self, event):
        """Handle trade events."""
        QTimer.singleShot(0, self._refresh)

    def _refresh(self):
        """Refresh metrics from paper executor."""
        positions = _pe.get_open_positions()
        capital = _pe.available_capital

        # Compute equity: capital + open position value
        open_pnl = sum(p.get("unrealized_pnl", 0.0) for p in positions)
        equity = capital + open_pnl
        self._metric_labels["EQUITY"][0].setText(f"${equity:,.0f}")

        # Compute win rate from closed trades
        closed = _pe._closed_trades
        wins = sum(1 for t in closed if t.get("pnl_usdt", 0) > 0)
        total = len(closed)
        win_rate = (wins / total * 100) if total > 0 else 0
        self._metric_labels["WIN RATE"][0].setText(f"{win_rate:.1f}%")

        # Total trades
        self._metric_labels["TOTAL TRADES"][0].setText(str(total))

        # Day P&L and Open P&L - set to placeholder for now (no real-time day tracking yet)
        self._metric_labels["DAY P&L"][0].setText("—")
        self._metric_labels["OPEN P&L"][0].setText(f"${open_pnl:+,.0f}")

        # Drawdown
        dd = _pe.drawdown_pct
        self._metric_labels["DRAWDOWN"][0].setText(f"{dd:+.2f}%")

        # Risk bars - set all to 0 since no real data available yet
        for label, (bar, pct_lbl) in self._risk_bars.items():
            bar.setValue(0)
            pct_lbl.setText("—")

        # Overall risk score - placeholder
        self._risk_score_lbl.setText("—")


# ── Strategy Performance ──────────────────────────────────────
class StrategyPanel(QFrame):
    """Strategy performance panel - shows empty state."""

    _COLS = ["Strategy", "Trades", "Win%", "Avg P&L", "Max DD", "Sharpe", "Active"]

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("panel")
        self.setStyleSheet(_PANEL_STYLE)
        self._build()

    def _build(self):
        vlay = QVBoxLayout(self)
        vlay.setContentsMargins(0, 0, 0, 0)
        vlay.setSpacing(0)
        vlay.addWidget(_section_header("Strategy |Performance"))

        tbl = QTableWidget(0, len(self._COLS))
        tbl.setHorizontalHeaderLabels(self._COLS)
        tbl.setStyleSheet(_TABLE_STYLE)
        tbl.horizontalHeader().setStretchLastSection(False)
        tbl.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        tbl.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        tbl.verticalHeader().setVisible(False)
        tbl.setEditTriggers(QAbstractItemView.NoEditTriggers)
        tbl.setSelectionBehavior(QAbstractItemView.SelectRows)
        tbl.setShowGrid(False)

        # Show empty state
        placeholder = QLabel("No strategy data")
        placeholder.setStyleSheet(f"color:{_TEXT_DIM}; font-style:italic; padding:20px;")
        placeholder.setAlignment(Qt.AlignCenter)

        vlay.addWidget(tbl, 0)
        vlay.addWidget(placeholder, 1)


# ── Trade History ─────────────────────────────────────────────
class TradeHistoryPanel(QFrame):
    """Trade history from paper executor closed trades."""

    _COLS = ["Symbol", "Side", "Entry", "Exit", "P&L%", "P&L$", "Duration", "Date"]

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("panel")
        self.setStyleSheet(_PANEL_STYLE)
        self._tbl = None
        self._header = None
        self._build()
        try:
            bus.subscribe(Topics.TRADE_CLOSED, self._on_trade_closed)
        except Exception:
            pass
        # Initial load
        QTimer.singleShot(0, self._refresh)

    def _build(self):
        vlay = QVBoxLayout(self)
        vlay.setContentsMargins(0, 0, 0, 0)
        vlay.setSpacing(0)
        self._header = _section_header("Trade |History", "LAST 12")
        vlay.addWidget(self._header)

        self._tbl = QTableWidget(0, len(self._COLS))
        self._tbl.setHorizontalHeaderLabels(self._COLS)
        self._tbl.setStyleSheet(_TABLE_STYLE)
        self._tbl.horizontalHeader().setStretchLastSection(False)
        self._tbl.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self._tbl.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self._tbl.verticalHeader().setVisible(False)
        self._tbl.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._tbl.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._tbl.setShowGrid(False)

        vlay.addWidget(self._tbl, 1)

    @Slot(object)
    def _on_trade_closed(self, event):
        """Handle trade closed event."""
        QTimer.singleShot(0, self._refresh)

    def _refresh(self):
        """Refresh table with closed trades from paper executor."""
        trades = _pe._closed_trades[-12:] if _pe._closed_trades else []
        # Reverse to show most recent first
        trades = list(reversed(trades))
        self._tbl.setRowCount(len(trades))

        for r, trade in enumerate(trades):
            sym = trade.get("symbol", "?")
            side = trade.get("side", "LONG")
            entry = trade.get("entry_price", 0.0)
            exit_p = trade.get("exit_price", entry)
            pnl_pct = trade.get("pnl_pct", 0.0)
            pnl_usd = trade.get("pnl_usdt", 0.0)
            dur_m = trade.get("duration_min", 0)
            h, m = divmod(int(dur_m), 60)
            dur_str = f"{h}h {m:02d}m" if h else f"{m}m"
            closed_at = trade.get("closed_at", "")
            date_str = "—"
            if closed_at:
                try:
                    if isinstance(closed_at, str):
                        from datetime import datetime as dt
                        closed = dt.fromisoformat(closed_at.replace('Z', '+00:00'))
                        date_str = closed.strftime("%m/%d %H:%M")
                except Exception:
                    pass

            items = [
                (sym,                               _TEXT_PRI),
                (side.upper(),                      _BULL if side.upper() in ("LONG", "BUY") else _BEAR),
                (f"${entry:,.2f}" if entry > 1 else f"${entry:.6f}",       _TEXT_MUT),
                (f"${exit_p:,.2f}" if exit_p > 1 else f"${exit_p:.6f}",      _TEXT_PRI),
                (f"{pnl_pct:+.2f}%",                _BULL if pnl_pct >= 0 else _BEAR),
                (f"${pnl_usd:+.2f}",                _BULL if pnl_usd >= 0 else _BEAR),
                (dur_str,                           _TEXT_MUT),
                (date_str,                          _TEXT_DIM),
            ]
            for c, (val, col) in enumerate(items):
                item = QTableWidgetItem(val)
                item.setForeground(QColor(col))
                item.setFlags(Qt.ItemIsSelectable | Qt.ItemIsEnabled)
                self._tbl.setItem(r, c, item)

        self._tbl.resizeRowsToContents()

        # Update header count
        if len(trades) == 0:
            header_widget = self._header
            for child in header_widget.findChildren(QLabel):
                if "LAST" in child.text():
                    child.setText("LAST 12")


# ── Market Regime Panel ───────────────────────────────────────
class RegimeSummaryPanel(QFrame):

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("panel")
        self.setStyleSheet(_PANEL_STYLE)
        self._build()
        try:
            bus.subscribe(Topics.REGIME_CHANGED, self._on_regime)
        except Exception:
            pass

    def _build(self):
        vlay = QVBoxLayout(self)
        vlay.setContentsMargins(0, 0, 0, 0)
        vlay.setSpacing(0)
        vlay.addWidget(_section_header("Market |Regime"))

        body = QWidget()
        body.setStyleSheet("background:transparent;")
        blay = QVBoxLayout(body)
        blay.setContentsMargins(10, 10, 10, 10)
        blay.setSpacing(8)

        # Current regime card
        reg_card = QFrame()
        reg_card.setObjectName("card")
        reg_card.setStyleSheet(_CARD_STYLE)
        rc = QVBoxLayout(reg_card)
        rc.setContentsMargins(12, 10, 12, 10)
        rc.setSpacing(4)
        # Initial state: placeholder until REGIME_CHANGED event arrives
        self._regime_lbl = _lbl("—",
            f"font-size:18px; font-weight:700; color:{_TEXT_MUT};")
        self._regime_conf = _lbl("Awaiting regime data",
            f"font-size:13px; color:{_TEXT_MUT};")
        rc.addWidget(self._regime_lbl)
        rc.addWidget(self._regime_conf)
        blay.addWidget(reg_card)

        # HMM probabilities (store bar + label refs for live updates)
        blay.addWidget(_lbl("HMM PROBABILITY DISTRIBUTION",
            f"font-size:13px; letter-spacing:1px; color:{_TEXT_MUT};"))

        self._hmm_bars:   dict[str, QProgressBar] = {}
        self._hmm_labels: dict[str, QLabel]       = {}

        hmm_defs = [
            ("Bull Trend", _BULL),
            ("Bear Trend", _BEAR),
            ("Ranging",    _WARN),
        ]
        for name, color in hmm_defs:
            rrow = QHBoxLayout()
            rrow.setSpacing(6)
            nl = _lbl(name, f"font-size:13px; color:{_TEXT_MUT};")
            nl.setFixedWidth(90)
            bar = QProgressBar()
            bar.setRange(0, 100)
            bar.setValue(0)   # start at 0 — real values come from REGIME_CHANGED
            bar.setFixedHeight(4)
            bar.setTextVisible(False)
            bar.setStyleSheet(
                f"QProgressBar{{background:{_BORDER};border-radius:2px;border:none;}}"
                f"QProgressBar::chunk{{background:{color};border-radius:2px;}}"
            )
            pl = _lbl("—",
                f"font-size:13px; font-weight:700; color:{color};")
            pl.setFixedWidth(50)
            self._hmm_bars[name]   = bar
            self._hmm_labels[name] = pl
            rrow.addWidget(nl)
            rrow.addWidget(bar, 1)
            rrow.addWidget(pl)
            blay.addLayout(rrow)

        blay.addWidget(_sep_h())

        # Regime stats (store label refs for live updates)
        stat_defs = ["Regime Duration", "Transitions Today", "Trend Strength", "Momentum"]
        self._stat_lbls: dict[str, QLabel] = {}
        for label in stat_defs:
            rr = QHBoxLayout()
            rr.addWidget(_lbl(label, f"font-size:13px; color:{_TEXT_MUT};"))
            rr.addStretch()
            val_lbl = _lbl("—", f"font-size:13px; font-weight:700; color:{_TEXT_PRI};")
            self._stat_lbls[label] = val_lbl
            rr.addWidget(val_lbl)
            blay.addLayout(rr)

        blay.addStretch()

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setStyleSheet("QScrollArea{background:transparent;border:none;}"
            f"QScrollBar:vertical{{background:{_BG_PANEL};width:4px;}}"
            f"QScrollBar::handle:vertical{{background:{_BORDER};border-radius:2px;}}"
            "QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical{height:0;}")
        scroll.setWidget(body)
        vlay.addWidget(scroll, 1)

    def _on_regime(self, event):
        """Handle REGIME_CHANGED event — marshal to main thread."""
        data = event.data if hasattr(event, "data") else (event if isinstance(event, dict) else {})
        QTimer.singleShot(0, lambda d=data: self._apply_regime(d))

    def _apply_regime(self, data: dict):
        """Update all regime widgets on the main thread."""
        regime = data.get("new_regime", data.get("regime", "—"))
        conf   = float(data.get("confidence", 0.0))
        color  = _BULL if "bull" in regime.lower() else (
                 _BEAR if "bear" in regime.lower() else _WARN)

        self._regime_lbl.setText(regime.upper() if regime != "—" else "—")
        self._regime_lbl.setStyleSheet(f"font-size:18px; font-weight:700; color:{color};")
        self._regime_conf.setText(f"Confidence: {conf*100:.1f}%")

        # HMM probability bars — try multiple key formats for compatibility
        probs = data.get("regime_probs", data.get("hmm_probs", {}))
        # Map display names to possible key names in the probs dict
        bar_key_map = {
            "Bull Trend": ["bull_trend", "bull", "trend_bull"],
            "Bear Trend": ["bear_trend", "bear", "trend_bear"],
            "Ranging":    ["ranging", "range"],
        }
        for display_name, keys in bar_key_map.items():
            prob = 0.0
            for k in keys:
                if k in probs:
                    prob = float(probs[k])
                    break
            if display_name in self._hmm_bars:
                self._hmm_bars[display_name].setValue(int(prob * 100))
                self._hmm_labels[display_name].setText(f"{prob*100:.0f}%")

        # Regime stats
        stat_map = {
            "Regime Duration":   data.get("duration",   "—"),
            "Transitions Today": str(data.get("transitions", "—")),
            "Trend Strength":    data.get("strength",   "—"),
            "Momentum":          data.get("momentum",   "—"),
        }
        for label, val in stat_map.items():
            if label in self._stat_lbls:
                self._stat_lbls[label].setText(str(val))


# ── Market Intelligence ───────────────────────────────────────
class IntelligencePanel(QFrame):
    """Market intelligence panel - shows placeholder values (no external APIs available yet)."""

    _DATA = [
        ("Fear & Greed Index",  "—"),
        ("BTC Dominance",       "—"),
        ("Crypto Market Cap",   "—"),
        ("24h Volume",          "—"),
        ("Funding Rate (BTC)",  "—"),
        ("Open Interest (BTC)", "—"),
        ("Stablecoin Inflow",   "—"),
        ("News Sentiment",      "—"),
    ]

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("panel")
        self.setStyleSheet(_PANEL_STYLE)
        self._build()

    def _build(self):
        vlay = QVBoxLayout(self)
        vlay.setContentsMargins(0, 0, 0, 0)
        vlay.setSpacing(0)
        vlay.addWidget(_section_header("Market |Intelligence", "ON-CHAIN"))

        body = QWidget()
        body.setStyleSheet("background:transparent;")
        blay = QVBoxLayout(body)
        blay.setContentsMargins(10, 6, 10, 6)
        blay.setSpacing(0)

        for label, val in self._DATA:
            row = QFrame()
            row.setStyleSheet("background:transparent;")
            rlay = QHBoxLayout(row)
            rlay.setContentsMargins(0, 5, 0, 5)
            rlay.addWidget(_lbl(label, f"font-size:13px; color:{_TEXT_MUT};"))
            rlay.addStretch()
            rlay.addWidget(_lbl(val, f"font-size:13px; font-weight:700; color:{_TEXT_DIM};"))
            blay.addWidget(row)
            blay.addWidget(_sep_h())

        blay.addStretch()

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setStyleSheet("QScrollArea{background:transparent;border:none;}"
            f"QScrollBar:vertical{{background:{_BG_PANEL};width:4px;}}"
            f"QScrollBar::handle:vertical{{background:{_BORDER};border-radius:2px;}}"
            "QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical{height:0;}")
        scroll.setWidget(body)
        vlay.addWidget(scroll, 1)


# ── Live Alerts ───────────────────────────────────────────────
class AlertsPanel(QFrame):
    """Live alerts panel populated from real event sources."""

    _TYPE_COLORS = {
        "TRADE":  ("#003322", _BULL),
        "RISK":   ("#330011", _BEAR),
        "SIGNAL": ("#1a2840", _BLUE),
        "REGIME": ("#2a1f00", _WARN),
    }

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("panel")
        self.setStyleSheet(_PANEL_STYLE)
        self._build()
        try:
            bus.subscribe(Topics.SIGNAL_CONFIRMED, self._on_signal)
            bus.subscribe(Topics.RISK_LIMIT_HIT, self._on_alert)
            bus.subscribe(Topics.TRADE_OPENED, self._on_trade_opened)
            bus.subscribe(Topics.TRADE_CLOSED, self._on_trade_closed)
        except Exception:
            pass

    def _build(self):
        vlay = QVBoxLayout(self)
        vlay.setContentsMargins(0, 0, 0, 0)
        vlay.setSpacing(0)
        vlay.addWidget(_section_header("Live |Alerts", "REAL-TIME"))

        self._feed = QWidget()
        self._feed.setStyleSheet("background:transparent;")
        self._feed_lay = QVBoxLayout(self._feed)
        self._feed_lay.setContentsMargins(8, 6, 8, 6)
        self._feed_lay.setSpacing(4)

        # Start empty with placeholder
        self._placeholder = _lbl("No alerts yet",
            f"font-size:13px; color:{_TEXT_DIM}; font-style:italic; padding:20px;")
        self._placeholder.setAlignment(Qt.AlignCenter)
        self._feed_lay.addWidget(self._placeholder)
        self._feed_lay.addStretch()

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setStyleSheet("QScrollArea{background:transparent;border:none;}"
            f"QScrollBar:vertical{{background:{_BG_PANEL};width:4px;}}"
            f"QScrollBar::handle:vertical{{background:{_BORDER};border-radius:2px;}}"
            "QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical{height:0;}")
        scroll.setWidget(self._feed)
        vlay.addWidget(scroll, 1)

    def _make_alert(self, alert_type: str, msg: str, age: str) -> QFrame:
        item = QFrame()
        item.setObjectName("card")
        item.setStyleSheet(_CARD_STYLE)
        h = QHBoxLayout(item)
        h.setContentsMargins(8, 6, 8, 6)
        h.setSpacing(8)

        bg, fg = self._TYPE_COLORS.get(alert_type, (_BG_CARD, _TEXT_MUT))
        type_lbl = _lbl(alert_type,
            f"background:{bg}; color:{fg}; font-size:13px; font-weight:700;"
            f"letter-spacing:.5px; padding:2px 6px; border-radius:2px;")
        type_lbl.setFixedHeight(20)

        msg_lbl = _lbl(msg,
            f"font-size:13px; color:{_TEXT_MUT}; line-height:1.4;")
        msg_lbl.setWordWrap(True)

        age_lbl = _lbl(age,
            f"font-size:13px; color:{_TEXT_DIM};")
        age_lbl.setFixedWidth(80)
        age_lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)

        h.addWidget(type_lbl)
        h.addWidget(msg_lbl, 1)
        h.addWidget(age_lbl)
        return item

    @Slot(object)
    def _on_signal(self, event):
        """Handle SIGNAL_CONFIRMED event."""
        data = event.data if hasattr(event, "data") else {}
        if not data:
            return
        sym = data.get("symbol", "?")
        action = data.get("action", "SIGNAL")
        score = data.get("score", 0.5)
        msg = f"{sym} {action} signal (score {score:.2f})"
        self._prepend_alert("SIGNAL", msg, "just now")

    @Slot(object)
    def _on_alert(self, event):
        """Handle RISK_LIMIT_HIT event."""
        data = event.data if hasattr(event, "data") else {}
        msg = data.get("message", "Risk alert triggered")
        self._prepend_alert("RISK", msg, "just now")

    @Slot(object)
    def _on_trade_opened(self, event):
        """Handle TRADE_OPENED event."""
        data = event.data if hasattr(event, "data") else {}
        if not data:
            return
        sym = data.get("symbol", "?")
        side = data.get("side", "LONG")
        entry = data.get("entry_price", 0.0)
        score = data.get("score", 0.5)
        msg = f"{sym} {side} opened @ ${entry:,.2f} — score {score:.2f}"
        self._prepend_alert("TRADE", msg, "just now")

    @Slot(object)
    def _on_trade_closed(self, event):
        """Handle TRADE_CLOSED event."""
        data = event.data if hasattr(event, "data") else {}
        if not data:
            return
        sym = data.get("symbol", "?")
        pnl_pct = data.get("pnl_pct", 0.0)
        exit_price = data.get("exit_price", 0.0)
        side = data.get("side", "LONG")
        msg = f"{sym} {side} closed {pnl_pct:+.2f}% @ ${exit_price:,.2f}"
        self._prepend_alert("TRADE", msg, "just now")

    def _prepend_alert(self, alert_type: str, msg: str, age: str):
        # Hide placeholder on first alert
        if self._placeholder and self._placeholder.parent():
            self._feed_lay.removeWidget(self._placeholder)
            self._placeholder.deleteLater()
            self._placeholder = None
        item = self._make_alert(alert_type, msg, age)
        self._feed_lay.insertWidget(0, item)
        # Keep max 20 alerts
        while self._feed_lay.count() > 21:
            old = self._feed_lay.takeAt(self._feed_lay.count() - 2)
            if old and old.widget():
                old.widget().deleteLater()


# ── Main Quant Dashboard Page ─────────────────────────────────
class QuantDashboardPage(QWidget):
    """
    Quant Trading Command Center — full native Qt implementation
    mirroring the NexusTrader HTML dashboard design.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setStyleSheet(
            f"QuantDashboardPage {{ background: {_BG_BASE}; }}"
            f"QWidget {{ font-family: 'SF Mono', 'Fira Code', 'Consolas', monospace; }}"
        )
        self._build()

    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ── Ticker strip ──────────────────────────────────────
        self._ticker = TickerStrip()
        root.addWidget(self._ticker)

        # ── Scrollable content ────────────────────────────────
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setStyleSheet(
            f"QScrollArea{{background:{_BG_BASE};border:none;}}"
            f"QScrollBar:vertical{{background:{_BG_BASE};width:6px;border-radius:3px;}}"
            f"QScrollBar::handle:vertical{{background:{_BORDER};border-radius:3px;min-height:20px;}}"
            "QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical{height:0;}"
        )

        content = QWidget()
        content.setStyleSheet(f"background:{_BG_BASE};")
        clay = QVBoxLayout(content)
        clay.setContentsMargins(6, 6, 6, 6)
        clay.setSpacing(6)

        # ── Row 1: Full-width chart ───────────────────────────
        chart = CommandChart()
        chart.setMinimumHeight(260)
        clay.addWidget(chart)

        # ── Row 2: Bot Decisions + AI Signals ─────────────────
        row2 = QHBoxLayout()
        row2.setSpacing(6)
        bot = BotDecisionPanel()
        bot.setFixedWidth(380)
        bot.setMinimumHeight(320)
        sigs = AISignalPanel()
        sigs.setMinimumHeight(320)
        row2.addWidget(bot)
        row2.addWidget(sigs, 1)
        clay.addLayout(row2)

        # ── Row 3: Positions + Portfolio ──────────────────────
        row3 = QHBoxLayout()
        row3.setSpacing(6)
        pos = PositionsPanel()
        pos.setMinimumHeight(220)
        port = PortfolioPanel()
        port.setFixedWidth(300)
        port.setMinimumHeight(220)
        row3.addWidget(pos, 1)
        row3.addWidget(port)
        clay.addLayout(row3)

        # ── Rows 4+5: grid so Intelligence aligns with Strategy ─
        # Col 0 = Strategy / Intelligence (stretch 1)
        # Col 1 = Trade History / Alerts (stretch 1)
        # Col 2 = Market Regime (fixed 300px)
        lower_grid = QGridLayout()
        lower_grid.setSpacing(6)
        lower_grid.setColumnStretch(0, 1)
        lower_grid.setColumnStretch(1, 1)
        lower_grid.setColumnStretch(2, 0)

        strat = StrategyPanel()
        strat.setMinimumHeight(240)
        hist  = TradeHistoryPanel()
        hist.setMinimumHeight(240)
        regime = RegimeSummaryPanel()
        regime.setFixedWidth(300)
        regime.setMinimumHeight(240)
        lower_grid.addWidget(strat,  0, 0)
        lower_grid.addWidget(hist,   0, 1)
        lower_grid.addWidget(regime, 0, 2, 2, 1)  # span 2 rows

        intel  = IntelligencePanel()
        intel.setMinimumHeight(200)
        alerts = AlertsPanel()
        alerts.setMinimumHeight(200)
        lower_grid.addWidget(intel,  1, 0)
        lower_grid.addWidget(alerts, 1, 1)

        clay.addLayout(lower_grid)

        scroll.setWidget(content)
        root.addWidget(scroll, 1)
