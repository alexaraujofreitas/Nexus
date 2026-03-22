# ============================================================
# NEXUS TRADER — Candlestick Chart Widget
# pyqtgraph-based professional OHLCV chart with indicators
# ============================================================

import logging
import math
import numpy as np
import pandas as pd

import pyqtgraph as pg
from pyqtgraph import QtCore, QtGui
from PySide6.QtWidgets import QWidget, QVBoxLayout, QSizePolicy, QLabel
from PySide6.QtCore import Qt
from PySide6.QtGui import QPainter, QColor, QPen, QBrush

# Trade overlay — imported lazily inside methods to avoid circular imports at
# module level; the manager is created once in _build() and stored on self.
_overlay_mod = None

def _get_overlay_mod():
    global _overlay_mod
    if _overlay_mod is None:
        try:
            import gui.widgets.trade_overlay as _m
            _overlay_mod = _m
        except Exception:
            pass
    return _overlay_mod

logger = logging.getLogger(__name__)

# ── Color constants ───────────────────────────────────────────
C_BG        = "#0A0E1A"
C_PANEL     = "#0F1623"
C_GRID      = "#1A2332"
C_TEXT      = "#8899AA"
C_BULL      = "#00CC77"   # Green candles
C_BEAR      = "#FF3355"   # Red candles
C_WICK      = "#4A5568"
C_VOLUME    = "#1E3A6B"
C_VOL_BULL  = "#004433"
C_VOL_BEAR  = "#440011"
C_EMA_9     = "#FFB300"
C_EMA_20    = "#1E90FF"
C_EMA_50    = "#FF6B00"
C_EMA_200   = "#AA44FF"
C_BB_UPPER  = "#2D4A6B"
C_BB_LOWER  = "#2D4A6B"
C_BB_MID    = "#3A5A7B"


# Timeframes shorter than 1 day — these need HH:MM in tick labels when zoomed
_INTRADAY_TF = {"1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h"}


def _fmt_price(v: float) -> str:
    """Format a price with comma thousands separator and sensible decimals."""
    if v >= 1_000:
        return f"{v:,.2f}"
    elif v >= 1:
        return f"{v:,.4f}"
    elif v >= 0.01:
        return f"{v:.6f}"
    else:
        return f"{v:.8g}"


def _fmt_volume(v: float) -> str:
    """Format a volume value with comma thousands separator."""
    if v >= 1_000_000_000:
        return f"{v/1_000_000_000:,.2f}B"
    elif v >= 1_000_000:
        return f"{v/1_000_000:,.2f}M"
    elif v >= 1_000:
        return f"{v/1_000:,.2f}K"
    else:
        return f"{v:,.0f}"


# ── Axis helper functions (module-level) ─────────────────────

def _fmt_day_label(ts) -> str:
    """'Dec 21' — abbreviated month + day without leading zero."""
    return f"{ts.strftime('%b')} {ts.day}"


def _is_aligned(ts, interval_s: int) -> bool:
    """
    True when ts falls on an interval_s boundary.
    Component-based (no .timestamp() call) → timezone-safe for tz-naive UTC.
    """
    if interval_s >= 86_400:
        return ts.hour == 0 and ts.minute == 0 and ts.second == 0
    if interval_s >= 3_600:
        return ts.minute == 0 and ts.second == 0 and (ts.hour % (interval_s // 3_600) == 0)
    if interval_s >= 60:
        return ts.second == 0 and (ts.minute % (interval_s // 60) == 0)
    return ts.second % interval_s == 0


def _prune_ticks(ticks: list, px_per_bar: float, min_gap_px: float) -> list:
    """Remove bar-index ticks whose labels would overlap (< min_gap_px apart)."""
    if not ticks or px_per_bar <= 0:
        return ticks
    out, last_px = [], -min_gap_px * 10
    for bar in sorted(ticks):
        px = bar * px_per_bar
        if px - last_px >= min_gap_px:
            out.append(bar)
            last_px = px
    return out


# ── Day Session Separator ─────────────────────────────────────
class DaySeparatorItem(pg.GraphicsObject):
    """
    Subtle dashed vertical lines drawn at UTC midnight (day boundaries).
    Added to price_plot with ignoreBounds=True so it never affects auto-range.
    """

    def __init__(self):
        super().__init__()
        self._bars: list = []
        self._pen = pg.mkPen(color="#1B3050", width=1,
                             style=QtCore.Qt.DashLine)

    def set_day_boundaries(self, day_bars: list):
        self._bars = list(day_bars)
        self.prepareGeometryChange()
        self.update()

    def boundingRect(self):
        if self._bars:
            return QtCore.QRectF(min(self._bars) - 1, -1e8,
                                 max(self._bars) - min(self._bars) + 2, 2e8)
        return QtCore.QRectF(0, -1e8, 1, 2e8)

    def paint(self, painter, option, widget=None):
        if not self._bars:
            return
        vb = self.getViewBox()
        if vb is not None:
            vr   = vb.viewRange()
            y0   = vr[1][0] - 1.0
            y1   = vr[1][1] + 1.0
        else:
            y0, y1 = -1e8, 1e8
        painter.setPen(self._pen)
        for bar in self._bars:
            painter.drawLine(QtCore.QPointF(bar, y0),
                             QtCore.QPointF(bar, y1))


# ── Date Axis — Adaptive, Hierarchical ───────────────────────
class DateAxisItem(pg.AxisItem):
    """
    TradingView-quality adaptive time axis with hierarchical labels.

    Five rendering levels based on visible time range:
    ─────────────────────────────────────────────────────────────
    Level 0  < 4 h    → "00:00  01:00  02:00 …"
    Level 1  < 24 h   → "Dec 21  06:00  12:00  18:00  Dec 22"
    Level 2  < 3 d    → "Dec 21  06:00  18:00  Dec 22"
    Level 3  < 14 d   → "Dec 21  22  23  24  Jan 1 …"
    Level 4  < 90 d   → "Dec 1  8  15  22  Jan 1 …"
    Level 5  ≥ 90 d   → "Dec '24  Jan '25  Feb '25 …"
    ─────────────────────────────────────────────────────────────

    Day-transition labels ("Dec 21") are visually distinct from time
    labels ("06:00") purely by content — both rendered via tickStrings().
    Labels never overlap: _prune_ticks() enforces minimum pixel gap.
    """

    # Candidate intraday label intervals in seconds (ascending)
    _NICE_S = [
        60, 2*60, 5*60, 10*60, 15*60, 30*60,
        3600, 2*3600, 3*3600, 4*3600, 6*3600, 8*3600, 12*3600,
    ]

    # Minimum screen-pixel gap between adjacent tick labels
    _MIN_GAP_PX = 80

    # Map timeframe string → seconds per bar
    _TF_S = {
        "1m": 60,    "3m": 180,   "5m": 300,   "10m": 600,
        "15m": 900,  "30m": 1800, "1h": 3600,  "2h": 7200,
        "4h": 14400, "6h": 21600, "8h": 28800, "12h": 43200,
        "1d": 86400, "3d": 259200,"1w": 604800,
    }

    def __init__(self, orientation="bottom"):
        super().__init__(orientation=orientation)
        self._ts: list  = []    # pd.Timestamp per bar (tz-naive UTC)
        self._timeframe = "1h"
        self._lbl: dict = {}    # bar_idx → label string
        self.setStyle(tickTextOffset=5)

    # ── External API ─────────────────────────────────────────
    def set_timestamps(self, timestamps: list):
        self._ts = timestamps
        self._lbl.clear()

    def set_timeframe(self, timeframe: str):
        self._timeframe = timeframe
        self._lbl.clear()

    # ── pyqtgraph hooks ──────────────────────────────────────
    def tickValues(self, minVal, maxVal, size):
        """Compute adaptive tick positions; populate _lbl for tickStrings."""
        self._lbl.clear()
        if not self._ts or size <= 0:
            return super().tickValues(minVal, maxVal, size)

        i0 = max(0, int(math.floor(minVal)))
        i1 = min(len(self._ts) - 1, int(math.ceil(maxVal)))
        if i0 >= i1:
            return super().tickValues(minVal, maxVal, size)

        ts0, ts1 = self._ts[i0], self._ts[i1]
        if ts0 is None or ts1 is None:
            return super().tickValues(minVal, maxVal, size)

        visible_s = (ts1 - ts0).total_seconds()
        if visible_s <= 0:
            return super().tickValues(minVal, maxVal, size)

        px_per_bar = size / max(i1 - i0, 1)
        ticks = self._build_ticks(i0, i1, visible_s / 3600.0,
                                   visible_s, px_per_bar)
        if not ticks:
            return super().tickValues(minVal, maxVal, size)

        avg_spc = (maxVal - minVal) / max(len(ticks), 1)
        return [(avg_spc, ticks)]

    def tickStrings(self, values, scale, spacing):
        return [self._lbl.get(int(round(v)), "") for v in values]

    # ── Adaptive dispatch ────────────────────────────────────
    def _build_ticks(self, i0, i1, visible_h, visible_s, px_per_bar):
        if visible_h < 4:
            return self._ticks_intraday(i0, i1, visible_s, px_per_bar,
                                         day_labels=False)
        if visible_h < 24:
            return self._ticks_intraday(i0, i1, visible_s, px_per_bar,
                                         day_labels=True)
        if visible_h < 3 * 24:
            return self._ticks_intraday(i0, i1, visible_s, px_per_bar,
                                         day_labels=True, min_itvl_s=3 * 3600)
        if visible_h < 14 * 24:
            return self._ticks_daily(i0, i1)
        if visible_h < 90 * 24:
            return self._ticks_daily(i0, i1, week_align=True)
        return self._ticks_monthly(i0, i1)

    # ── Intraday ticks (< ~3 days) ───────────────────────────
    def _best_interval(self, px_per_bar: float) -> int:
        """Nicest interval that keeps labels ≥ _MIN_GAP_PX apart."""
        tf_s          = self._TF_S.get(self._timeframe, 3600)
        sec_per_label = (self._MIN_GAP_PX / max(px_per_bar, 0.01)) * tf_s
        for s in self._NICE_S:
            if s >= sec_per_label:
                return s
        return 12 * 3600

    def _ticks_intraday(self, i0, i1, visible_s, px_per_bar,
                         day_labels=True, min_itvl_s=None):
        itvl = self._best_interval(px_per_bar)
        if min_itvl_s and itvl < min_itvl_s:
            itvl = min_itvl_s

        ticks      = []
        seen_dates = set()

        for i in range(i0, i1 + 1):
            ts = self._ts[i]
            if ts is None:
                continue

            date       = ts.date()
            is_new_day = date not in seen_dates

            if is_new_day:
                seen_dates.add(date)
                if day_labels:
                    if i > i0:
                        # Day transition — "Dec 21" replaces "00:00"
                        self._lbl[i] = _fmt_day_label(ts)
                        ticks.append(i)
                        continue
                    # First visible bar at midnight → show day label
                    if ts.hour == 0 and ts.minute == 0:
                        self._lbl[i] = _fmt_day_label(ts)
                        ticks.append(i)
                        continue

            if _is_aligned(ts, itvl):
                # Suppress midnight when day labels are on (already placed)
                if day_labels and ts.hour == 0 and ts.minute == 0:
                    continue
                self._lbl[i] = ts.strftime("%H:%M")
                ticks.append(i)

        return _prune_ticks(ticks, px_per_bar, self._MIN_GAP_PX)

    # ── Daily ticks (3 d – 90 d) ─────────────────────────────
    def _ticks_daily(self, i0, i1, week_align=False):
        """
        One label per day (week_align=False) or per Monday (week_align=True).
        Month boundaries show "Mon D"; other days show just "D".
        """
        ticks      = []
        seen_dates = set()
        prev_month = None

        for i in range(i0, i1 + 1):
            ts = self._ts[i]
            if ts is None:
                continue
            date = ts.date()
            if date in seen_dates:
                continue
            seen_dates.add(date)

            if week_align and date.weekday() != 0:   # 0 = Monday
                continue

            if ts.month != prev_month:
                # Month boundary: "Dec 21"
                self._lbl[i] = _fmt_day_label(ts)
                prev_month = ts.month
            else:
                # Same month: plain day number "22"
                self._lbl[i] = str(ts.day)
            ticks.append(i)

        return ticks

    # ── Monthly ticks (≥ 90 d) ───────────────────────────────
    def _ticks_monthly(self, i0, i1):
        """One label per month: "Dec '24"."""
        ticks       = []
        seen_months = set()

        for i in range(i0, i1 + 1):
            ts = self._ts[i]
            if ts is None:
                continue
            key = (ts.year, ts.month)
            if key in seen_months:
                continue
            seen_months.add(key)
            self._lbl[i] = ts.strftime("%b '%y")
            ticks.append(i)

        return ticks


# ── Y-Axis — dynamic precision + thousands separators ────────
class PriceAxisItem(pg.AxisItem):
    """
    Left Y-axis for the price chart.
    • Thousands separators for all values (68,750.25)
    • Decimal precision auto-tuned to the tick spacing:
        gap ≥ 5,000  → 0 decimals    (BTC whole-dollar view)
        gap ≥ 100    → 1 decimal
        gap ≥ 1      → 2 decimals    (standard crypto)
        gap ≥ 0.1    → 3 decimals
        gap ≥ 0.01   → 4 decimals    (ETH, SOL)
        gap ≥ 0.001  → 5 decimals
        else         → 6 decimals    (micro-cap assets)
    """

    def tickStrings(self, values, scale, spacing):
        gap = abs(spacing * scale) if spacing else 0.0
        if   gap >= 5_000:  decimals = 0
        elif gap >= 1_000:  decimals = 0
        elif gap >= 100:    decimals = 1
        elif gap >= 10:     decimals = 2
        elif gap >= 1:      decimals = 2
        elif gap >= 0.1:    decimals = 3
        elif gap >= 0.01:   decimals = 4
        elif gap >= 0.001:  decimals = 5
        else:               decimals = 6

        result = []
        for v in values:
            vs = v * scale
            try:
                result.append(f"{vs:,.{decimals}f}")
            except (ValueError, OverflowError):
                result.append(str(vs))
        return result


# ── Volume Y-Axis ─────────────────────────────────────────────
class VolumeAxisItem(pg.AxisItem):
    """Left Y-axis for the volume panel — compact K/M/B notation."""

    def tickStrings(self, values, scale, spacing):
        result = []
        for v in values:
            vs = abs(v * scale)
            if vs >= 1_000_000_000:
                result.append(f"{v*scale/1_000_000_000:,.1f}B")
            elif vs >= 1_000_000:
                result.append(f"{v*scale/1_000_000:,.1f}M")
            elif vs >= 1_000:
                result.append(f"{v*scale/1_000:,.1f}K")
            else:
                result.append(f"{v*scale:,.0f}")
        return result


# ── Candlestick Graphics Item ─────────────────────────────────
class CandlestickItem(pg.GraphicsObject):
    """
    Renders OHLCV candles by drawing directly in paint().

    Unlike QPicture-based approaches, this preserves full floating-point
    precision for any price magnitude (including sub-cent pairs like XRP).
    Candle body width adapts to the current zoom level so bars never
    overlap when zoomed in and remain visible when zoomed out.
    """

    # Width as a fraction of the bar spacing (0.8 = 80% of 1-unit spacing)
    _WIDTH_FRAC = 0.80
    # Minimum body width in screen pixels so candles remain visible when zoomed out
    _MIN_PX = 1.0

    def __init__(self):
        super().__init__()
        self._data: list[dict] = []
        self._bounds = QtCore.QRectF()

    def set_data(self, data: list[dict]):
        """
        data: list of {t: int, o: float, h: float, l: float, c: float}
        't' is a bar index (0 = oldest).
        """
        self._data = data
        if data:
            xs = [d["t"] for d in data]
            ys_lo = [d["l"] for d in data]
            ys_hi = [d["h"] for d in data]
            w = self._WIDTH_FRAC / 2
            self._bounds = QtCore.QRectF(
                min(xs) - w, min(ys_lo),
                max(xs) - min(xs) + 2 * w,
                max(ys_hi) - min(ys_lo) or 1.0
            )
        else:
            self._bounds = QtCore.QRectF(0, 0, 1, 1)
        self.prepareGeometryChange()
        self.update()

    def paint(self, painter, option, widget=None):
        if not self._data:
            return

        painter.setRenderHint(QPainter.Antialiasing, False)

        # ---------- compute dynamic half-width in DATA coordinates ----------
        # m11() = how many screen pixels equal 1 data unit in X.
        # We want the body to occupy _WIDTH_FRAC of the bar spacing, but
        # never render narrower than _MIN_PX on screen.
        m11 = abs(painter.transform().m11())
        if m11 > 0:
            # half-width in data units that equals _MIN_PX pixels
            min_data_w = self._MIN_PX / m11
            w = max(self._WIDTH_FRAC / 2, min_data_w)
            # Cap at 0.48 so there's always a 1-unit gap between neighbouring candles
            w = min(w, 0.48)
        else:
            w = self._WIDTH_FRAC / 2

        # Pre-build pens / brushes
        bull_body = QBrush(QColor(C_BULL))
        bear_body = QBrush(QColor(C_BEAR))
        bull_pen  = QPen(QColor(C_BULL)); bull_pen.setCosmetic(True); bull_pen.setWidthF(1.0)
        bear_pen  = QPen(QColor(C_BEAR)); bear_pen.setCosmetic(True); bear_pen.setWidthF(1.0)
        wick_pen  = QPen(QColor(C_WICK)); wick_pen.setCosmetic(True); wick_pen.setWidthF(1.0)

        for d in self._data:
            t, o, h, l, c = d["t"], d["o"], d["h"], d["l"], d["c"]
            is_bull = c >= o

            # --- Wick (high-low line) ---
            painter.setPen(wick_pen)
            painter.setBrush(Qt.NoBrush)
            painter.drawLine(QtCore.QPointF(t, l), QtCore.QPointF(t, h))

            # --- Body ---
            body_top    = max(o, c)
            body_bottom = min(o, c)
            body_height = body_top - body_bottom
            if body_height < 1e-12:
                # Doji — draw a horizontal line at close price
                doji_pen = QPen(QColor(C_BULL if is_bull else C_BEAR))
                doji_pen.setCosmetic(True)
                doji_pen.setWidthF(1.0)
                painter.setPen(doji_pen)
                painter.drawLine(
                    QtCore.QPointF(t - w, c),
                    QtCore.QPointF(t + w, c)
                )
                continue

            if is_bull:
                painter.setPen(bull_pen)
                painter.setBrush(bull_body)
            else:
                painter.setPen(bear_pen)
                painter.setBrush(bear_body)

            painter.drawRect(QtCore.QRectF(t - w, body_bottom, 2 * w, body_height))

    def boundingRect(self):
        return self._bounds


# ── Volume Bar Graphics Item ──────────────────────────────────
class VolumeBarItem(pg.GraphicsObject):
    """
    Renders volume bars with bull/bear coloring, drawn directly in paint()
    for full floating-point precision and zoom-adaptive bar width.
    """

    _WIDTH_FRAC = 0.80
    _MIN_PX     = 1.0

    def __init__(self):
        super().__init__()
        self._data: list[dict] = []
        self._bounds = QtCore.QRectF()

    def set_data(self, data: list[dict]):
        self._data = data
        if data:
            xs    = [d["t"] for d in data]
            max_v = max(d["v"] for d in data) if data else 1
            w = self._WIDTH_FRAC / 2
            self._bounds = QtCore.QRectF(
                min(xs) - w, 0,
                max(xs) - min(xs) + 2 * w,
                max_v or 1.0
            )
        else:
            self._bounds = QtCore.QRectF(0, 0, 1, 1)
        self.prepareGeometryChange()
        self.update()

    def paint(self, painter, option, widget=None):
        if not self._data:
            return

        painter.setRenderHint(QPainter.Antialiasing, False)

        m11 = abs(painter.transform().m11())
        if m11 > 0:
            min_data_w = self._MIN_PX / m11
            w = max(self._WIDTH_FRAC / 2, min_data_w)
            w = min(w, 0.48)
        else:
            w = self._WIDTH_FRAC / 2

        bull_brush = QBrush(QColor(C_VOL_BULL))
        bear_brush = QBrush(QColor(C_VOL_BEAR))
        bull_pen   = QPen(QColor(C_BULL)); bull_pen.setCosmetic(True); bull_pen.setWidthF(0.8)
        bear_pen   = QPen(QColor(C_BEAR)); bear_pen.setCosmetic(True); bear_pen.setWidthF(0.8)

        for d in self._data:
            t, v, is_bull = d["t"], d["v"], d["bull"]
            if is_bull:
                painter.setPen(bull_pen)
                painter.setBrush(bull_brush)
            else:
                painter.setPen(bear_pen)
                painter.setBrush(bear_brush)
            painter.drawRect(QtCore.QRectF(t - w, 0, 2 * w, v))

    def boundingRect(self):
        return self._bounds


# ── Crosshair ─────────────────────────────────────────────────
class Crosshair:
    def __init__(self, plot: pg.PlotWidget):
        pen = pg.mkPen(color="#2D4A6B", style=QtCore.Qt.DashLine, width=1)
        self.vline = pg.InfiniteLine(angle=90, movable=False, pen=pen)
        self.hline = pg.InfiniteLine(angle=0,  movable=False, pen=pen)
        plot.addItem(self.vline, ignoreBounds=True)
        plot.addItem(self.hline, ignoreBounds=True)

    def move(self, x: float, y: float):
        self.vline.setPos(x)
        self.hline.setPos(y)


# ── Main Chart Widget ─────────────────────────────────────────
class ChartWidget(QWidget):
    """
    Full candlestick chart with:
    - OHLCV candles (direct paint — correct for any price magnitude)
    - Volume panel (lower)
    - Indicator overlays (EMA, BB, etc.)
    - TradingView-style adaptive date axis
    - Crosshair
    - Zoom / pan
    - Buy/sell markers
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._df: pd.DataFrame = pd.DataFrame()
        self._symbol    = ""
        self._timeframe = "1h"
        self._active_indicators: list[str] = ["ema_20", "ema_50", "bb_upper", "bb_lower", "bb_mid"]
        self._indicator_lines: dict[str, pg.PlotDataItem] = {}
        self._ichimoku_fill = None   # track cloud fill so we can remove it
        self._candle_item  = CandlestickItem()
        self._volume_item  = VolumeBarItem()
        self._day_sep      = DaySeparatorItem()   # vertical dashed lines at midnight
        # Zoom-state tracking — set True when the user manually pans/zooms
        # so that periodic data refreshes don't reset the viewport
        self._user_zoomed: bool = False
        # Trade overlay manager — created after price_plot exists
        self._overlay_mgr = None
        self._build()

    def _build(self):
        pg.setConfigOption("background", C_BG)
        pg.setConfigOption("foreground", C_TEXT)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # ── OHLCV info bar (above chart — never overlaps price data) ──
        self._ohlcv_label = QLabel("")
        self._ohlcv_label.setTextFormat(Qt.RichText)
        self._ohlcv_label.setFixedHeight(26)
        self._ohlcv_label.setContentsMargins(8, 2, 8, 2)
        self._ohlcv_label.setStyleSheet(
            f"background:{C_PANEL}; border-bottom:1px solid {C_GRID};"
        )
        layout.addWidget(self._ohlcv_label)

        # Date axes — share the same Timestamp list
        self._date_axis     = DateAxisItem(orientation="bottom")
        self._vol_date_axis = DateAxisItem(orientation="bottom")

        # Y-axes with comma formatting
        self._price_y_axis  = PriceAxisItem(orientation="left")
        self._vol_y_axis    = VolumeAxisItem(orientation="left")

        # ── Main price plot ───────────────────────────────────
        self.price_plot = pg.PlotWidget(
            axisItems={"bottom": self._date_axis, "left": self._price_y_axis}
        )
        self.price_plot.setBackground(C_BG)
        self.price_plot.showGrid(x=True, y=True, alpha=0.15)
        self.price_plot.getAxis("left").setTextPen(pg.mkPen(C_TEXT))
        self.price_plot.getAxis("left").setPen(pg.mkPen(C_GRID))
        self.price_plot.getAxis("bottom").setTextPen(pg.mkPen(C_TEXT))
        self.price_plot.getAxis("bottom").setPen(pg.mkPen(C_GRID))
        self.price_plot.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.price_plot.addItem(self._candle_item)
        # Day separator lines — added before crosshair so candles paint on top
        self.price_plot.addItem(self._day_sep, ignoreBounds=True)

        # ── Floating axis-stick cursor tooltips ──────────────
        # Price label: right edge of chart, tracks cursor Y
        self._price_cursor_lbl = QLabel(self.price_plot)
        self._price_cursor_lbl.setStyleSheet(
            "background:#1E3A6B; color:#E8EBF0; font-size:11px; "
            "font-weight:bold; border-radius:2px; padding:1px 6px;"
        )
        self._price_cursor_lbl.setAlignment(Qt.AlignCenter)
        self._price_cursor_lbl.hide()
        self._price_cursor_lbl.setAttribute(Qt.WA_TransparentForMouseEvents, True)

        # Time label: bottom edge of chart, tracks cursor X (snapped)
        self._time_cursor_lbl = QLabel(self.price_plot)
        self._time_cursor_lbl.setStyleSheet(
            "background:#1E3A6B; color:#E8EBF0; font-size:11px; "
            "font-weight:bold; border-radius:2px; padding:1px 6px;"
        )
        self._time_cursor_lbl.setAlignment(Qt.AlignCenter)
        self._time_cursor_lbl.hide()
        self._time_cursor_lbl.setAttribute(Qt.WA_TransparentForMouseEvents, True)

        # Crosshair
        self._crosshair = Crosshair(self.price_plot)
        self.price_plot.scene().sigMouseMoved.connect(self._on_mouse_move)

        # Detect manual zoom / pan so we can preserve the viewport on refresh
        self.price_plot.getPlotItem().vb.sigRangeChangedManually.connect(
            self._on_manual_range_change
        )

        # Trade Analysis Overlay — attach to price_plot
        try:
            mod = _get_overlay_mod()
            if mod:
                self._overlay_mgr = mod.TradeOverlayManager(self.price_plot)
        except Exception:
            pass

        # Click-to-select trade
        self.price_plot.scene().sigMouseClicked.connect(self._on_scene_click)

        # ── Volume plot ───────────────────────────────────────
        self.vol_plot = pg.PlotWidget(
            axisItems={"bottom": self._vol_date_axis, "left": self._vol_y_axis}
        )
        self.vol_plot.setBackground(C_BG)
        self.vol_plot.showGrid(x=True, y=False, alpha=0.10)
        self.vol_plot.getAxis("left").setTextPen(pg.mkPen(C_TEXT))
        self.vol_plot.getAxis("bottom").setTextPen(pg.mkPen(C_TEXT))
        self.vol_plot.setMaximumHeight(120)
        self.vol_plot.setMinimumHeight(80)
        self.vol_plot.addItem(self._volume_item)

        # Link x-axes
        self.vol_plot.setXLink(self.price_plot)

        layout.addWidget(self.price_plot, 4)
        layout.addWidget(self.vol_plot,   1)

    # ── Public API ─────────────────────────────────────────────
    def load_dataframe(self, df: pd.DataFrame, symbol: str = "", timeframe: str = "1h"):
        """Load an OHLCV DataFrame and render the chart."""
        if df is None or df.empty:
            return

        # Detect whether this is a brand-new symbol/timeframe or just a refresh
        new_context = (symbol != self._symbol) or (timeframe != self._timeframe)
        if new_context:
            # Reset zoom flag so the new dataset gets a proper auto-range
            self._user_zoomed = False

        self._df        = df
        self._symbol    = symbol
        self._timeframe = timeframe

        # Clear Ichimoku cloud fill from previous symbol before rendering
        if self._ichimoku_fill is not None:
            try:
                self.price_plot.removeItem(self._ichimoku_fill)
            except Exception:
                pass
            self._ichimoku_fill = None

        self._render_candles()
        self._render_volume()
        self._render_indicators()

        # Push raw Timestamps and current timeframe to both date axes so that
        # tickStrings() can format labels adaptively at any zoom level.
        self._update_date_axis(df)
        self._date_axis.set_timeframe(timeframe)
        self._vol_date_axis.set_timeframe(timeframe)

        # Update day-boundary separator lines
        self._update_day_separators(df)

        # Only auto-range when the user hasn't manually zoomed/panned.
        # On a periodic data refresh for the same symbol, preserve the
        # current viewport so the user's zoom isn't wiped out.
        if not self._user_zoomed:
            try:
                n = len(df)
                y_min = float(df["low"].min())
                y_max = float(df["high"].max())
                pad   = (y_max - y_min) * 0.04
                self.price_plot.setXRange(0, n - 1, padding=0.01)
                self.price_plot.setYRange(y_min - pad, y_max + pad, padding=0)
                self.vol_plot.setXRange(0, n - 1, padding=0.01)
            except Exception:
                self.price_plot.autoRange()

    def set_indicators(self, indicators: list[str]):
        """Update which indicators are displayed."""
        self._active_indicators = indicators
        if not self._df.empty:
            self._render_indicators()

    def add_trade_markers(self, trades: list[dict]):
        """
        trades: list of {x: int, price: float, side: 'buy'|'sell', label: str}
        """
        for t in trades:
            color  = C_BULL if t["side"] == "buy" else C_BEAR
            symbol = "t1" if t["side"] == "buy" else "t"
            scatter = pg.ScatterPlotItem(
                x=[t["x"]], y=[t["price"]],
                symbol=symbol, size=12,
                pen=pg.mkPen(color), brush=pg.mkBrush(color)
            )
            self.price_plot.addItem(scatter)

    # ── Date axis ──────────────────────────────────────────────
    def _update_date_axis(self, df: pd.DataFrame):
        """
        Extract raw pd.Timestamp objects from the DataFrame index and push
        them to both DateAxisItem instances. The axis items handle formatting
        adaptively at render time via tickStrings().
        """
        try:
            idx = df.index
            if hasattr(idx, "to_pydatetime"):
                # DatetimeIndex — convert each element to pd.Timestamp
                timestamps = [pd.Timestamp(ts) for ts in idx]
            elif len(idx) > 0 and isinstance(idx[0], (int, float)):
                # Numeric millisecond timestamps
                timestamps = [pd.Timestamp(ts, unit="ms") for ts in idx]
            else:
                # Try generic conversion
                timestamps = [pd.Timestamp(ts) for ts in idx]
        except Exception:
            timestamps = [None] * len(df)

        self._date_axis.set_timestamps(timestamps)
        self._vol_date_axis.set_timestamps(timestamps)

    # ── Rendering ──────────────────────────────────────────────
    def _render_candles(self):
        data = []
        for i, (ts, row) in enumerate(self._df.iterrows()):
            data.append({
                "t": i,
                "o": float(row["open"]),
                "h": float(row["high"]),
                "l": float(row["low"]),
                "c": float(row["close"]),
            })
        self._candle_item.set_data(data)

    def _render_volume(self):
        data = []
        for i, (ts, row) in enumerate(self._df.iterrows()):
            data.append({
                "t":    i,
                "v":    float(row["volume"]),
                "bull": float(row["close"]) >= float(row["open"]),
            })
        self._volume_item.set_data(data)

    def _render_indicators(self):
        # Remove old indicator lines
        for item in self._indicator_lines.values():
            self.price_plot.removeItem(item)
        self._indicator_lines.clear()

        # Remove old Ichimoku cloud fill
        if self._ichimoku_fill is not None:
            try:
                self.price_plot.removeItem(self._ichimoku_fill)
            except Exception:
                pass
            self._ichimoku_fill = None

        indicator_styles = {
            "ema_9":    (C_EMA_9,   1, Qt.SolidLine),
            "ema_20":   (C_EMA_20,  1, Qt.SolidLine),
            "ema_50":   (C_EMA_50,  1, Qt.SolidLine),
            "ema_200":  (C_EMA_200, 2, Qt.SolidLine),
            "sma_20":   (C_EMA_20,  1, Qt.DashLine),
            "sma_50":   (C_EMA_50,  1, Qt.DashLine),
            "bb_upper": (C_BB_UPPER,1, Qt.DashLine),
            "bb_mid":   (C_BB_MID,  1, Qt.DotLine),
            "bb_lower": (C_BB_LOWER,1, Qt.DashLine),
            "vwap":     ("#FF6B00", 1, Qt.DashLine),
            "supertrend": ("#FF6B00", 1, Qt.SolidLine),
            "ichi_conversion": ("#1E90FF", 1, Qt.SolidLine),
            "ichi_base":       ("#FF3355", 1, Qt.SolidLine),
        }

        xs = np.arange(len(self._df))

        for col in self._active_indicators:
            if col not in self._df.columns:
                continue
            style = indicator_styles.get(col, ("#FFFFFF", 1, Qt.SolidLine))
            color, width, line_style = style

            y = self._df[col].values.astype(float)
            valid = ~np.isnan(y)
            if valid.sum() < 2:
                continue

            pen = pg.mkPen(color=color, width=width, style=line_style)
            line = self.price_plot.plot(xs[valid], y[valid], pen=pen, name=col)
            self._indicator_lines[col] = line

        # Ichimoku cloud fill
        if "ichi_a" in self._df.columns and "ichi_b" in self._df.columns:
            self._render_ichimoku_cloud(xs)

    def _render_ichimoku_cloud(self, xs):
        try:
            a = self._df["ichi_a"].values.astype(float)
            b = self._df["ichi_b"].values.astype(float)
            valid = ~(np.isnan(a) | np.isnan(b))
            if valid.sum() < 2:
                return
            fill = pg.FillBetweenItem(
                pg.PlotDataItem(xs[valid], a[valid]),
                pg.PlotDataItem(xs[valid], b[valid]),
                brush=pg.mkBrush(30, 100, 60, 30)
            )
            self.price_plot.addItem(fill)
            self._ichimoku_fill = fill   # track for removal on next load
        except Exception:
            pass

    # ── Range / Zoom Events ────────────────────────────────────
    def _on_manual_range_change(self, *_):
        """
        Called by pyqtgraph when the user manually zooms or pans.
        Once set, periodic data refreshes will no longer reset the viewport.
        The flag is cleared automatically when a new symbol/timeframe is loaded.
        """
        self._user_zoomed = True

    # ── Mouse Events ───────────────────────────────────────────
    def _on_mouse_move(self, pos):
        try:
            vb       = self.price_plot.getPlotItem().vb
            view_pos = vb.mapSceneToView(pos)
            x, y     = view_pos.x(), view_pos.y()

            # Snap crosshair vertical line to nearest bar centre
            snapped_x = float(int(round(x)))
            self._crosshair.move(snapped_x, y)

            # Show OHLCV values for hovered candle
            idx = int(round(x))
            if 0 <= idx < len(self._df):
                row = self._df.iloc[idx]
                ts  = self._df.index[idx]
                ts_str = ts.strftime("%Y-%m-%d %H:%M") if hasattr(ts, "strftime") else str(ts)
                o = _fmt_price(float(row["open"]))
                h = _fmt_price(float(row["high"]))
                l = _fmt_price(float(row["low"]))
                c = _fmt_price(float(row["close"]))
                v = _fmt_volume(float(row["volume"]))
                sep = "&nbsp;&nbsp;&nbsp;"
                label = (
                    f"<span style='color:{C_TEXT};font-size:13px'>"
                    f"{self._symbol}&nbsp;{self._timeframe}"
                    f"</span>"
                    f"<span style='color:#5A7A9A;font-size:13px'>"
                    f"&nbsp;&nbsp;{ts_str}"
                    f"</span>"
                    f"{sep}"
                    f"<span style='color:#8899AA;font-size:13px'>O\u202f</span>"
                    f"<span style='color:#E8EBF0;font-size:13px'>{o}</span>"
                    f"{sep}"
                    f"<span style='color:#8899AA;font-size:13px'>H\u202f</span>"
                    f"<span style='color:{C_BULL};font-size:13px'>{h}</span>"
                    f"{sep}"
                    f"<span style='color:#8899AA;font-size:13px'>L\u202f</span>"
                    f"<span style='color:{C_BEAR};font-size:13px'>{l}</span>"
                    f"{sep}"
                    f"<span style='color:#8899AA;font-size:13px'>C\u202f</span>"
                    f"<span style='color:#E8EBF0;font-size:13px'>{c}</span>"
                    f"{sep}"
                    f"<span style='color:#8899AA;font-size:13px'>V\u202f</span>"
                    f"<span style='color:{C_TEXT};font-size:13px'>{v}</span>"
                )
                self._ohlcv_label.setText(label)

            # Axis-stick floating cursor labels
            self._update_axis_cursors(snapped_x, y, pos, vb)

            # Trade overlay — tooltip on hover
            if self._overlay_mgr is not None:
                try:
                    px = vb.viewPixelSize()   # (data_units_per_px_x, data_units_per_px_y)
                    px_bar   = 1.0 / max(abs(px[0]), 1e-9)
                    px_price = 1.0 / max(abs(px[1]), 1e-9)
                    plot_pt  = self.price_plot.mapFromScene(pos).toPoint()
                    self._overlay_mgr.on_mouse_move(x, y, px_bar, px_price, plot_pt)
                except Exception:
                    pass
        except Exception:
            pass

    def _on_scene_click(self, event):
        """Pass left-clicks to the overlay manager for trade selection."""
        try:
            if self._overlay_mgr is None:
                return
            if event.button() != QtCore.Qt.LeftButton:
                return
            pos = event.scenePos()
            if not self.price_plot.sceneBoundingRect().contains(pos):
                return
            vb  = self.price_plot.getPlotItem().vb
            vp  = vb.mapSceneToView(pos)
            px  = vb.viewPixelSize()
            px_bar   = 1.0 / max(abs(px[0]), 1e-9)
            px_price = 1.0 / max(abs(px[1]), 1e-9)
            self._overlay_mgr.on_click(vp.x(), vp.y(), px_bar, px_price)
        except Exception:
            pass

    # ── Trade Overlay Public API ────────────────────────────────
    def set_trade_data(self, closed_trades: list, open_positions: list):
        """
        Push closed trade history and current open positions to the overlay.
        The overlay rebuilds its internal item list and repaints.

        closed_trades  — list of PaperTrade.to_dict() dicts (with 'id' key)
        open_positions — list of Position.to_dict() dicts
        """
        if self._overlay_mgr is None or self._df.empty:
            return
        try:
            self._overlay_mgr.set_data(closed_trades, open_positions,
                                       self._df, self._symbol)
        except Exception as exc:
            logger.debug("ChartWidget.set_trade_data error: %s", exc)

    def set_overlay_visible(self, visible: bool):
        """Show or hide the trade overlay without discarding data."""
        if self._overlay_mgr is not None:
            try:
                self._overlay_mgr.set_visible(visible)
            except Exception:
                pass

    def set_overlay_options(self, **kwargs):
        """
        Forward display options to the overlay manager.
        Accepted keys:
            show_duration_bars  bool   (default True)
            show_connections    bool   (default True)
            show_exit_quality   bool   (default True)
            filter_mode         str    'all' | 'open' | 'closed'
            last_n              int    0 = all
        """
        if self._overlay_mgr is not None:
            try:
                self._overlay_mgr.set_options(**kwargs)
            except Exception:
                pass

    # ── Axis-stick cursor tooltips ──────────────────────────────
    def _update_axis_cursors(self, snapped_x: float, y: float, scene_pos, vb):
        """
        Position and update the floating price label (right edge) and time
        label (bottom edge).  Both are QLabel children of price_plot so they
        move with the widget and stay on top of the chart canvas.
        """
        try:
            plot_rect = self.price_plot.rect()   # widget-local rect
            w = plot_rect.width()
            h = plot_rect.height()

            # ── Price label (right-edge, tracks Y) ───────────
            price_text = _fmt_price(y)
            self._price_cursor_lbl.setText(price_text)
            self._price_cursor_lbl.adjustSize()
            lbl_w = self._price_cursor_lbl.width()
            lbl_h = self._price_cursor_lbl.height()

            # Convert data-Y to scene, then to plot-widget local coords
            scene_pt   = vb.mapViewToScene(QtCore.QPointF(snapped_x, y))
            local_pt   = self.price_plot.mapFromScene(scene_pt)
            label_y    = int(local_pt.y() - lbl_h // 2)
            label_y    = max(0, min(label_y, h - lbl_h))
            label_x    = w - lbl_w - 2     # right edge, 2px gap from border
            self._price_cursor_lbl.move(label_x, label_y)
            self._price_cursor_lbl.show()

            # ── Time label (bottom-edge, tracks snapped X) ───
            idx = int(round(snapped_x))
            if 0 <= idx < len(self._df):
                ts = self._df.index[idx]
                if hasattr(ts, "strftime"):
                    time_text = ts.strftime("%Y-%m-%d %H:%M")
                else:
                    time_text = str(ts)
            else:
                time_text = ""

            if time_text:
                self._time_cursor_lbl.setText(time_text)
                self._time_cursor_lbl.adjustSize()
                tl_w = self._time_cursor_lbl.width()
                tl_h = self._time_cursor_lbl.height()

                scene_xpt = vb.mapViewToScene(QtCore.QPointF(snapped_x, y))
                local_xpt = self.price_plot.mapFromScene(scene_xpt)
                label_tx  = int(local_xpt.x() - tl_w // 2)
                label_tx  = max(0, min(label_tx, w - tl_w))
                label_ty  = h - tl_h - 2    # bottom edge, 2px gap
                self._time_cursor_lbl.move(label_tx, label_ty)
                self._time_cursor_lbl.show()
            else:
                self._time_cursor_lbl.hide()
        except Exception:
            pass

    # ── Day separator helpers ───────────────────────────────────
    def _update_day_separators(self, df: pd.DataFrame):
        """
        Compute bar indices where UTC date changes (midnight crossings) and
        send them to DaySeparatorItem so dashed vertical lines are drawn.
        """
        try:
            day_bars: list[int] = []
            prev_date = None
            for i, ts in enumerate(df.index):
                try:
                    d = pd.Timestamp(ts).date()
                except Exception:
                    continue
                if prev_date is not None and d != prev_date:
                    day_bars.append(i)
                prev_date = d
            self._day_sep.set_day_boundaries(day_bars)
        except Exception:
            pass

    # ── Leave event — hide floating labels ─────────────────────
    def leaveEvent(self, event):
        """Hide cursor tooltip labels when the mouse leaves the widget."""
        try:
            self._price_cursor_lbl.hide()
            self._time_cursor_lbl.hide()
        except Exception:
            pass
        if self._overlay_mgr is not None:
            try:
                self._overlay_mgr.hide_tooltip()
            except Exception:
                pass
        super().leaveEvent(event)
