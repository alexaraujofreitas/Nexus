# ============================================================
# NEXUS TRADER — Candlestick Chart Widget
# pyqtgraph-based professional OHLCV chart with indicators
# ============================================================

import logging
import numpy as np
import pandas as pd

import pyqtgraph as pg
from pyqtgraph import QtCore, QtGui
from PySide6.QtWidgets import QWidget, QVBoxLayout, QSizePolicy
from PySide6.QtCore import Qt
from PySide6.QtGui import QPainter, QColor, QPen, QBrush

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


# ── Date Axis ────────────────────────────────────────────────
class DateAxisItem(pg.AxisItem):
    """
    X-axis that shows human-readable, TradingView-style dates.

    Stores raw pd.Timestamp objects; adaptively chooses format in
    tickStrings() based on 'spacing' (bars between ticks) AND the
    current timeframe:

        spacing >= 180            →  "%b '%y"       e.g. "Mar '26"   (monthly)
        spacing >= 14             →  "%b %d"        e.g. "Mar 08"    (weekly)
        spacing >= 1, daily TF    →  "%b %d"        e.g. "Mar 08"    (daily)
        spacing >= 1, intraday TF →  "%b %d %H:%M"  e.g. "Mar 08 14:00"
        spacing <  1              →  "%H:%M"         e.g. "14:00"    (very zoomed)
    """

    def __init__(self, orientation="bottom"):
        super().__init__(orientation=orientation)
        self._ts: list = []          # list of pd.Timestamp (or None)
        self._timeframe: str = "1h"
        self.setStyle(tickTextOffset=4)

    def set_timestamps(self, timestamps: list):
        """Accept a list of pd.Timestamp objects, one per candle."""
        self._ts = timestamps

    def set_timeframe(self, timeframe: str):
        """Tell the axis which timeframe is loaded so it can adapt labels."""
        self._timeframe = timeframe

    def tickStrings(self, values, scale, spacing):
        # spacing = number of data units (bars) between adjacent tick marks
        is_intraday = self._timeframe in _INTRADAY_TF

        if spacing >= 180:
            fmt = "%b '%y"          # "Mar '26"  — monthly
        elif spacing >= 14:
            fmt = "%b %d"           # "Mar 08"   — weekly / multi-day
        elif spacing >= 1:
            # Show time component only for intraday timeframes
            fmt = "%b %d %H:%M" if is_intraday else "%b %d"
        else:
            fmt = "%H:%M"           # "14:00"    — very tight zoom

        result = []
        for v in values:
            idx = int(round(v))
            if 0 <= idx < len(self._ts):
                ts = self._ts[idx]
                try:
                    result.append(ts.strftime(fmt))
                except Exception:
                    result.append(str(ts))
            else:
                result.append("")
        return result


# ── Y-Axis with comma-separated numbers ──────────────────────
class PriceAxisItem(pg.AxisItem):
    """Left Y-axis for the price chart — adds comma thousands separators."""

    def tickStrings(self, values, scale, spacing):
        result = []
        for v in values:
            vs = v * scale
            if vs >= 1_000:
                result.append(f"{vs:,.2f}")
            elif vs >= 1:
                result.append(f"{vs:,.4f}")
            elif vs >= 0.01:
                result.append(f"{vs:.6f}")
            else:
                result.append(f"{vs:.6g}")
        return result


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
        # Zoom-state tracking — set True when the user manually pans/zooms
        # so that periodic data refreshes don't reset the viewport
        self._user_zoomed: bool = False
        self._build()

    def _build(self):
        pg.setConfigOption("background", C_BG)
        pg.setConfigOption("foreground", C_TEXT)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(1)

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

        # Crosshair
        self._crosshair = Crosshair(self.price_plot)
        self.price_plot.scene().sigMouseMoved.connect(self._on_mouse_move)

        # Detect manual zoom / pan so we can preserve the viewport on refresh
        self.price_plot.getPlotItem().vb.sigRangeChangedManually.connect(
            self._on_manual_range_change
        )

        # OHLCV label overlay
        self._ohlcv_label = pg.LabelItem(justify="left")
        self._ohlcv_label.setParentItem(self.price_plot.getPlotItem())
        self._ohlcv_label.anchor(itemPos=(0, 0), parentPos=(0, 0), offset=(10, 10))

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
            view_pos = self.price_plot.getPlotItem().vb.mapSceneToView(pos)
            x, y = view_pos.x(), view_pos.y()
            self._crosshair.move(x, y)

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
                self._ohlcv_label.setText(label, size="13pt")
        except Exception:
            pass
