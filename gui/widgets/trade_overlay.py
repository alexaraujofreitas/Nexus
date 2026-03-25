# ============================================================
# NEXUS TRADER — Advanced Trade Analysis Overlay
# Renders trade lifecycle (entry, exit, lines, duration bars,
# exit quality) as a read-only pyqtgraph overlay on the chart.
# ============================================================

import logging
from typing import Optional

import numpy as np
import pandas as pd

import pyqtgraph as pg
from pyqtgraph import QtCore
from PySide6.QtWidgets import QLabel, QWidget
from PySide6.QtCore import Qt, QPoint, QRectF, QPointF
from PySide6.QtGui import QPainter, QColor, QPen, QBrush, QPainterPath

logger = logging.getLogger(__name__)


# ── Color palette ──────────────────────────────────────────────────────────
# 16 colours with maximum hue distance between adjacent indices so that
# sequential trade IDs (0,1,2,…) land on visually distinct colours.
_PALETTE: list[str] = [
    "#1E90FF",  # 0  dodger-blue
    "#FF3366",  # 1  rose
    "#00CC77",  # 2  emerald
    "#FFB300",  # 3  amber
    "#CC44FF",  # 4  violet
    "#FF6B35",  # 5  orange
    "#00DDCC",  # 6  teal
    "#FFDD33",  # 7  yellow
    "#FF66AA",  # 8  pink
    "#66BB44",  # 9  lime
    "#44BBFF",  # 10 sky
    "#FFAA44",  # 11 gold
    "#FF8844",  # 12 coral
    "#44FFAA",  # 13 mint
    "#DD66FF",  # 14 lavender
    "#E8D080",  # 15 straw
]

# Exit-quality dot colours
_QUALITY_COLOR: dict[str, str] = {
    "optimal":    "#00CC77",
    "early":      "#FFB300",
    "very_early": "#FF3366",
}


# ── Helper functions ───────────────────────────────────────────────────────

def _trade_color(trade_id: int) -> str:
    """Deterministic, stable colour assignment from trade_id."""
    return _PALETTE[hash(str(trade_id)) % len(_PALETTE)]


def _r_adjusted_color(base_hex: str, realized_r: Optional[float]) -> str:
    """
    Brighten winning trades, desaturate / darken losing ones based on
    R-multiple magnitude.  Returns a hex colour string.
    """
    if realized_r is None:
        return base_hex
    c = QColor(base_hex)
    h, s, v, a = c.getHsvF()
    if realized_r > 0:
        v = min(1.0, 0.65 + realized_r * 0.12)   # brighter as R grows
    else:
        abs_r = abs(realized_r)
        s = max(0.20, s * (1.0 - abs_r * 0.18))   # desaturate
        v = max(0.30, v * (1.0 - abs_r * 0.10))   # darken
    c.setHsvF(h, s, v, a)
    return c.name()


def _parse_ts(s: str) -> Optional[pd.Timestamp]:
    """Parse an ISO-8601 string to a tz-naive UTC pd.Timestamp."""
    try:
        ts = pd.Timestamp(s)
        if ts.tzinfo is not None:
            ts = ts.tz_convert("UTC").tz_localize(None)
        return ts
    except Exception:
        return None


def _nearest_bar(ts: pd.Timestamp, df_index: pd.DatetimeIndex) -> int:
    """Return the index of the bar in df_index whose timestamp is closest to ts."""
    try:
        idx = df_index
        if hasattr(idx, "tz") and idx.tz is not None:
            # tz_localize(None) raises TypeError on already-tz-aware indexes.
            # Must tz_convert to UTC first, then strip timezone info.
            idx = idx.tz_convert("UTC").tz_localize(None)
        arr = idx.values.astype("datetime64[ns]")
        tgt = np.datetime64(ts, "ns")
        pos = int(np.searchsorted(arr, tgt))
        n = len(arr)
        if pos >= n:
            return n - 1
        if pos == 0:
            return 0
        d_prev = abs(int(arr[pos - 1]) - int(tgt))
        d_curr = abs(int(arr[pos])     - int(tgt))
        return pos - 1 if d_prev <= d_curr else pos
    except Exception:
        return 0


def _compute_realized_r(trade: dict) -> Optional[float]:
    """R = (exit − entry) / |entry − stop_loss|, direction-aware."""
    try:
        entry  = float(trade.get("entry_price") or 0)
        exit_p = float(trade.get("exit_price")  or 0)
        sl     = float(trade.get("stop_loss")    or 0)
        side   = trade.get("side", "buy")
        if entry <= 0 or exit_p <= 0 or sl <= 0:
            return None
        risk = abs(entry - sl)
        if risk <= 0:
            return None
        return (exit_p - entry) / risk if side == "buy" else (entry - exit_p) / risk
    except Exception:
        return None


def _compute_exit_quality(trade: dict, df: pd.DataFrame) -> str:
    """
    Assess whether the exit was early by comparing the exit price against the
    maximum favourable price movement in the 5 bars following exit.

    Returns: "optimal" | "early" | "very_early" | "unknown"
    """
    try:
        exit_bar = trade.get("_exit_bar")
        if exit_bar is None or exit_bar + 1 >= len(df):
            return "unknown"

        # ATR proxy: rolling 14-period (H-L) mean
        atr_series = (df["high"] - df["low"]).rolling(14).mean()
        atr = float(atr_series.iloc[exit_bar])
        if pd.isna(atr) or atr <= 0:
            return "unknown"

        exit_px = float(trade.get("exit_price") or 0)
        if exit_px <= 0:
            return "unknown"

        future = df.iloc[exit_bar + 1 : exit_bar + 6]
        if future.empty:
            return "unknown"

        side = trade.get("side", "buy")
        if side == "buy":
            continuation = float(future["high"].max()) - exit_px
        else:
            continuation = exit_px - float(future["low"].min())

        if continuation > 2.0 * atr:
            return "very_early"
        if continuation > 0.8 * atr:
            return "early"
        return "optimal"
    except Exception:
        return "unknown"


# ── Trade Overlay Item ─────────────────────────────────────────────────────

class TradeOverlayItem(pg.GraphicsObject):
    """
    Batched pyqtgraph GraphicsObject that renders the complete visual history
    of all trades for a symbol/timeframe.

    Coordinate system: data coords  (x = integer bar index, y = price).
    Marker sizes are derived from the painter transform so they appear at a
    fixed pixel size regardless of zoom level.

    Drawing order (back → front):
        1. Duration bars   — thin horizontal rect from entry_bar to exit_bar
        2. Connection lines — entry→exit line (solid=closed, dashed=open)
        3. Markers          — filled entry triangle + hollow exit triangle
        4. Exit quality dot — small coloured dot beside the exit marker
    """

    # Triangle sizing — zoom-adaptive:
    #   Target = BAR_FRAC × bar_width_px, clamped to [MIN_PX, MAX_PX].
    #   This keeps the triangle at a fixed fraction of each bar when zoomed in
    #   and prevents it from getting too tiny when zoomed out.
    _M_MIN_PX  = 9    # minimum triangle half-width in screen pixels
    _M_MAX_PX  = 32   # maximum triangle half-width in screen pixels
    _M_BAR_FRAC = 0.42 # fraction of bar width to use as half-width
    _M_ASPECT  = 1.55  # height = half-width × aspect  (≈ equilateral)
    # Legacy constants kept for the non-triangle elements
    _BAR_H_PX = 3    # duration bar height
    _BAR_O_PX = 16   # duration bar vertical offset from entry price (in px)
    _DOT_R_PX = 4    # exit-quality dot radius

    def __init__(self):
        super().__init__()
        self._items:    list[dict] = []
        self._bounds:   QRectF     = QRectF(0, 0, 1, 1)
        self._opts:     dict       = {
            "show_duration": True,
            "show_lines":    True,
            "show_quality":  True,
        }
        self._selected_id: Optional[int] = None

    # ── Public ──────────────────────────────────────────────────────

    def set_data(self, items: list[dict]):
        self._items = items
        self._recompute_bounds()
        self.prepareGeometryChange()
        self.update()

    def set_options(self, **opts):
        self._opts.update(opts)
        self.update()

    def set_selected(self, trade_id: Optional[int]):
        self._selected_id = trade_id
        self.update()

    def get_items(self) -> list[dict]:
        return self._items

    # ── Bounds ──────────────────────────────────────────────────────

    def _recompute_bounds(self):
        if not self._items:
            self._bounds = QRectF(0, 0, 1, 1)
            return
        xs = [t["entry_x"] for t in self._items]
        ys = [t["entry_y"] for t in self._items]
        for t in self._items:
            if t.get("exit_x") is not None:
                xs.append(t["exit_x"])
            if t.get("exit_y") is not None:
                ys.append(t["exit_y"])
        x0, x1 = min(xs) - 2, max(xs) + 2
        y0, y1 = min(ys),      max(ys)
        pad = max((y1 - y0) * 0.05, 1.0)
        self._bounds = QRectF(x0, y0 - pad, x1 - x0, (y1 - y0) + 2 * pad)

    def boundingRect(self) -> QRectF:
        return self._bounds

    # ── paint ────────────────────────────────────────────────────────

    def paint(self, painter: QPainter, option, widget=None):
        if not self._items:
            return

        # Derive pixel-to-data-unit scale from the current painter transform.
        # m11 = pixels per bar,  m22 = pixels per price unit (always positive here).
        tf  = painter.transform()
        m11 = max(abs(tf.m11()), 1e-6)
        m22 = max(abs(tf.m22()), 1e-6)

        # ── Zoom-adaptive triangle size ───────────────────────────────
        # Target half-width = BAR_FRAC × bar_width_px, clamped to [MIN, MAX].
        # This means: zoomed-out → minimum pixel size (never disappear);
        # zoomed-in → scale with bar width (stay proportional and visible).
        w_px = max(self._M_MIN_PX, min(self._M_MAX_PX, m11 * self._M_BAR_FRAC))
        mw   = w_px / m11              # half-width in data (bar-index) units
        mh   = (w_px * self._M_ASPECT) / m22  # height in price units, same screen aspect

        bh  = self._BAR_H_PX / m22
        off = self._BAR_O_PX / m22
        dr  = self._DOT_R_PX / m22

        painter.setRenderHint(QPainter.Antialiasing, True)

        # ── Pass 1: duration bars (back layer) ───────────────────────
        if self._opts.get("show_duration", True):
            for it in self._items:
                if it.get("exit_x") is not None:
                    self._draw_duration_bar(
                        painter,
                        it["entry_x"], it["entry_y"],
                        it["exit_x"],  off, bh,
                        it["color"],   it["is_open"],
                    )

        # ── Pass 2: connection lines ─────────────────────────────────
        if self._opts.get("show_lines", True):
            for it in self._items:
                if it.get("exit_x") is not None:
                    self._draw_connection_line(
                        painter,
                        it["entry_x"], it["entry_y"],
                        it["exit_x"],  it["exit_y"],
                        it["color"],   it["is_open"],
                    )

        # ── Pass 3: entry + exit markers (front layer) ───────────────
        for it in self._items:
            selected = (it["trade_id"] == self._selected_id)
            self._draw_entry_marker(
                painter, it["entry_x"], it["entry_y"],
                it["side"], it["display_color"], mw, mh, selected,
            )
            if it.get("exit_x") is not None:
                self._draw_exit_marker(
                    painter, it["exit_x"], it["exit_y"],
                    it["side"], it["display_color"], mw, mh,
                )

        # ── Pass 4: exit quality dots ────────────────────────────────
        if self._opts.get("show_quality", True):
            for it in self._items:
                q = it.get("exit_quality")
                if it.get("exit_x") is not None and q and q != "unknown":
                    self._draw_quality_dot(
                        painter, it["exit_x"], it["exit_y"],
                        q, mw, mh, dr,
                    )

    # ── Triangle path helpers ────────────────────────────────────────

    @staticmethod
    def _up_path(cx: float, cy: float, w: float, h: float) -> QPainterPath:
        """
        Triangle path with apex at cy-h (lower price).

        ⚠ pyqtgraph price-chart Y-axis is inverted: lower price → lower on screen.
        Therefore this path renders as a DOWNWARD-POINTING ▽ on screen.
        Use for SELL / SHORT entries.
        """
        p = QPainterPath()
        p.moveTo(cx,      cy - h * 0.67)   # apex: lower price = lower on screen
        p.lineTo(cx + w,  cy + h * 0.33)   # base: higher price = higher on screen
        p.lineTo(cx - w,  cy + h * 0.33)
        p.closeSubpath()
        return p

    @staticmethod
    def _down_path(cx: float, cy: float, w: float, h: float) -> QPainterPath:
        """
        Triangle path with apex at cy+h (higher price).

        ⚠ pyqtgraph price-chart Y-axis is inverted: higher price → higher on screen.
        Therefore this path renders as an UPWARD-POINTING ▲ on screen.
        Use for BUY / LONG entries.
        """
        p = QPainterPath()
        p.moveTo(cx,      cy + h * 0.67)   # apex: higher price = higher on screen
        p.lineTo(cx + w,  cy - h * 0.33)   # base: lower price = lower on screen
        p.lineTo(cx - w,  cy - h * 0.33)
        p.closeSubpath()
        return p

    # ── Drawing sub-routines ─────────────────────────────────────────

    def _draw_entry_marker(
        self, painter: QPainter,
        x: float, y: float, side: str,
        color: str, mw: float, mh: float, selected: bool,
    ):
        """
        Filled entry triangle with a white border so it is always visible against
        any candle colour.

        Direction convention (price-chart Y-axis is inverted in pyqtgraph):
          buy  → _down_path → renders as ▲ UP on screen   (long entry, bullish)
          sell → _up_path   → renders as ▽ DOWN on screen  (short entry, bearish)
        """
        is_buy = (side == "buy")
        # Choose correct screen-direction (see docstrings on _up_path / _down_path)
        path_fn = self._down_path if is_buy else self._up_path

        # ── 1. White border (drawn slightly larger, no fill) ─────────────
        # Always present — makes the triangle legible on any candle colour.
        border_w = 2.5 if not selected else 3.5
        white_pen = QPen(QColor("#FFFFFF"))
        white_pen.setCosmetic(True)
        white_pen.setWidthF(border_w)
        painter.setPen(white_pen)
        painter.setBrush(Qt.NoBrush)
        painter.drawPath(path_fn(x, y, mw, mh))

        # ── 2. Filled triangle in trade colour ───────────────────────────
        c = QColor(color)
        fill_pen = QPen(c)
        fill_pen.setCosmetic(True)
        fill_pen.setWidthF(1.0)
        painter.setPen(fill_pen)
        painter.setBrush(QBrush(c))
        painter.drawPath(path_fn(x, y, mw, mh))

        # ── 3. Extra highlight ring when trade is selected ───────────────
        if selected:
            ring = QPen(QColor("#FFD700"))   # gold ring for selected
            ring.setCosmetic(True)
            ring.setWidthF(2.5)
            painter.setPen(ring)
            painter.setBrush(Qt.NoBrush)
            painter.drawPath(path_fn(x, y, mw * 1.6, mh * 1.6))

    def _draw_exit_marker(
        self, painter: QPainter,
        x: float, y: float, side: str,
        color: str, mw: float, mh: float,
    ):
        """
        Hollow (outline-only) exit triangle — same direction as entry, slightly smaller.
        White border first so it reads against any candle colour.
        """
        is_buy = (side == "buy")
        path_fn = self._down_path if is_buy else self._up_path
        sm = 0.78   # slightly smaller than entry marker

        # White border
        white_pen = QPen(QColor("#FFFFFF"))
        white_pen.setCosmetic(True)
        white_pen.setWidthF(2.0)
        painter.setPen(white_pen)
        painter.setBrush(Qt.NoBrush)
        painter.drawPath(path_fn(x, y, mw * sm, mh * sm))

        # Coloured outline (hollow)
        c = QColor(color)
        pen = QPen(c)
        pen.setCosmetic(True)
        pen.setWidthF(1.5)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)
        painter.drawPath(path_fn(x, y, mw * sm, mh * sm))

    def _draw_connection_line(
        self, painter: QPainter,
        ex: float, ey: float,
        xx: float, xy: float,
        color: str, is_open: bool,
    ):
        """Entry→exit line in trade colour.  Solid = closed trade; dashed = open."""
        c = QColor(color)
        c.setAlphaF(0.60)
        pen = QPen(c);  pen.setCosmetic(True);  pen.setWidthF(1.0)
        if is_open:
            pen.setStyle(Qt.DashLine)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)
        painter.drawLine(QPointF(ex, ey), QPointF(xx, xy))

    def _draw_duration_bar(
        self, painter: QPainter,
        ex: float, ey: float,
        xx: float, off: float, bh: float,
        color: str, is_open: bool,
    ):
        """
        Thin horizontal bar from entry_bar to exit_bar offset below entry price.
        Shows how long the trade was held.  Semi-transparent fill.
        """
        bar_w = xx - ex
        if abs(bar_w) < 0.05:
            return
        c = QColor(color)
        c.setAlphaF(0.30 if not is_open else 0.18)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(c))
        bar_y = ey - off           # offset below entry price
        painter.drawRect(QRectF(ex, bar_y, bar_w, bh))

    def _draw_quality_dot(
        self, painter: QPainter,
        xx: float, xy: float,
        quality: str, mw: float, mh: float, dr: float,
    ):
        """Small coloured dot to the right of the exit marker indicating exit quality."""
        fg = _QUALITY_COLOR.get(quality, "#4A6A8A")
        c  = QColor(fg)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(c))
        dot_x = xx + mw * 1.5
        dot_y = xy - mh * 0.6
        painter.drawEllipse(QPointF(dot_x, dot_y), dr, dr)

    # ── Hit-testing ──────────────────────────────────────────────────

    def hit_test(
        self,
        data_x: float, data_y: float,
        px_per_bar: float, px_per_price: float,
        threshold_px: float = 16.0,
    ) -> Optional[dict]:
        """
        Return the trade dict whose entry or exit marker falls within
        threshold_px screen-pixels of (data_x, data_y), or None.
        """
        tx = threshold_px / max(px_per_bar,   1e-6)
        ty = threshold_px / max(px_per_price, 1e-6)
        best_dist = 1.0   # normalised, must be < 1 to match
        best: Optional[dict] = None
        for it in self._items:
            candidates = [(it["entry_x"], it["entry_y"])]
            if it.get("exit_x") is not None and it.get("exit_y") is not None:
                candidates.append((it["exit_x"], it["exit_y"]))
            for cx, cy in candidates:
                dx = (data_x - cx) / tx
                dy = (data_y - cy) / ty
                dist = dx * dx + dy * dy
                if dist < best_dist:
                    best_dist = dist
                    best = it
        return best


# ── Tooltip widget ─────────────────────────────────────────────────────────

class TradeTooltip(QLabel):
    """
    Floating HTML tooltip that appears over the chart when hovering a trade
    marker.  It is a child of the price_plot widget so it stays within the
    chart frame.
    """

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setTextFormat(Qt.RichText)
        self.setWordWrap(False)
        self.setStyleSheet(
            "background:#0C1829; color:#C8D8E8; "
            "border:1px solid #1E90FF; border-radius:6px; "
            "font-size:12px; padding:8px 10px;"
        )
        self.hide()
        self.setAttribute(Qt.WA_TransparentForMouseEvents)

    def show_for(self, trade: dict, plot_pos: QPoint, plot_size):
        """Build tooltip content and position it near plot_pos."""
        side     = trade.get("side", "buy")
        side_lbl = "▲ LONG" if side == "buy" else "▼ SHORT"
        side_c   = "#00CC77" if side == "buy" else "#FF3366"

        def _f(v, fmt=".4f"):
            try:
                return f"{float(v):{fmt}}"
            except Exception:
                return "—"

        r       = trade.get("realized_r")
        r_str   = f"{r:+.2f}R" if r is not None else "—"
        r_c     = "#00CC77" if (r or 0) > 0 else ("#FF3366" if (r or 0) < 0 else "#8899AA")

        pnl_u   = trade.get("pnl_usdt")
        pnl_str = (f"{'+'if float(pnl_u)>=0 else'-'}${abs(float(pnl_u)):,.2f}") if pnl_u is not None else "—"
        pnl_c   = "#00CC77" if (pnl_u or 0) >= 0 else "#FF3366"

        dur_s   = int(trade.get("duration_s") or 0)
        if dur_s >= 3600:
            dur_str = f"{dur_s // 3600}h {(dur_s % 3600) // 60}m"
        elif dur_s >= 60:
            dur_str = f"{dur_s // 60}m {dur_s % 60}s"
        else:
            dur_str = f"{dur_s}s" if dur_s else "open"

        models  = ", ".join(trade.get("models_fired") or []) or "—"
        score   = float(trade.get("score") or 0)
        q_key   = trade.get("exit_quality") or "unknown"
        q_text  = {
            "optimal":    "✓ Optimal",
            "early":      "⚠ Early",
            "very_early": "✗ Very Early",
            "unknown":    "—",
        }.get(q_key, "—")
        q_c = _QUALITY_COLOR.get(q_key, "#8899AA")

        sw  = trade.get("symbol_weight")
        sw_str = f"{sw:.2f}" if sw is not None else "—"

        is_open = trade.get("is_open", False)
        status  = "<span style='color:#FFB300'>OPEN</span>" if is_open \
                  else "<span style='color:#8899AA'>CLOSED</span>"

        html = (
            f"<b style='color:{side_c}; font-size:13px'>{side_lbl}</b>"
            f"&nbsp;&nbsp;<span style='color:#E8EBF0; font-size:13px'>"
            f"  {trade.get('symbol','')}</span>"
            f"&nbsp;&nbsp;{status}"
            f"<hr style='border:0;border-top:1px solid #1A2D4A;margin:5px 0'/>"
            f"<table style='border-spacing:0 3px; min-width:200px'>"
            f"<tr><td style='color:#8899AA;padding-right:14px'>Entry</td>"
            f"    <td style='color:#E8EBF0'>{_f(trade.get('entry_price'))}</td></tr>"
            f"<tr><td style='color:#8899AA'>Exit</td>"
            f"    <td style='color:#E8EBF0'>{_f(trade.get('exit_price'))}</td></tr>"
            f"<tr><td style='color:#8899AA'>Realized R</td>"
            f"    <td style='color:{r_c};font-weight:bold'>{r_str}</td></tr>"
            f"<tr><td style='color:#8899AA'>P&amp;L</td>"
            f"    <td style='color:{pnl_c};font-weight:bold'>{pnl_str}</td></tr>"
            f"<tr><td style='color:#8899AA'>Duration</td>"
            f"    <td style='color:#E8EBF0'>{dur_str}</td></tr>"
            f"<tr><td style='color:#8899AA'>Score</td>"
            f"    <td style='color:#E8EBF0'>{score:.2f}</td></tr>"
            f"<tr><td style='color:#8899AA'>Exit Quality</td>"
            f"    <td style='color:{q_c}'>{q_text}</td></tr>"
            f"<tr><td style='color:#8899AA'>Sym.Weight</td>"
            f"    <td style='color:#E8EBF0'>{sw_str}</td></tr>"
            f"<tr><td style='color:#8899AA'>Models</td>"
            f"    <td style='color:#A0B4C8;font-size:11px'>{models}</td></tr>"
            f"</table>"
        )
        self.setText(html)
        self.adjustSize()

        # Position near cursor; flip to stay within plot bounds
        margin = 12
        x = plot_pos.x() + margin
        y = plot_pos.y() + margin
        pw = plot_size.width()
        ph = plot_size.height()
        if x + self.width()  > pw - margin:
            x = plot_pos.x() - self.width()  - margin
        if y + self.height() > ph - margin:
            y = plot_pos.y() - self.height() - margin
        self.move(max(0, x), max(0, y))
        self.show()
        self.raise_()


# ── Trade Overlay Manager ──────────────────────────────────────────────────

class TradeOverlayManager:
    """
    Top-level coordinator for the Advanced Trade Analysis Overlay.

    Responsibilities:
    - Holds raw closed-trade + open-position lists for the current symbol
    - Pre-processes data into bar-aligned render dicts (timestamps → bar indices,
      R-multiple computation, exit quality, colour assignment)
    - Delegates all rendering to TradeOverlayItem (pyqtgraph GraphicsObject)
    - Shows / hides the TradeTooltip in response to mouse-move events
    - Highlights a selected trade on mouse-click
    - Applies filter (all / open / closed / last-N) without modifying source data
    """

    def __init__(self, price_plot: pg.PlotWidget):
        self._price_plot   = price_plot
        self._overlay_item = TradeOverlayItem()
        self._tooltip      = TradeTooltip(price_plot)

        # ignoreBounds=True: overlay must not affect chart auto-range
        price_plot.addItem(self._overlay_item, ignoreBounds=True)

        self._visible        = False
        self._raw_closed:    list[dict] = []
        self._raw_open:      list[dict] = []
        self._df:            Optional[pd.DataFrame] = None
        self._symbol         = ""
        self._opts           = {
            "show_duration":  True,
            "show_lines":     True,
            "show_quality":   True,
            "filter_mode":    "all",   # "all" | "open" | "closed"
            "last_n":         0,       # 0 = no limit
        }
        self._overlay_item.setVisible(False)

    # ── Public API ────────────────────────────────────────────────────

    def set_visible(self, v: bool):
        self._visible = v
        self._overlay_item.setVisible(v)
        if not v:
            self._tooltip.hide()

    def set_options(self, **kwargs):
        self._opts.update(kwargs)
        self._overlay_item.set_options(
            show_duration = self._opts.get("show_duration", True),
            show_lines    = self._opts.get("show_lines",    True),
            show_quality  = self._opts.get("show_quality",  True),
        )
        self._rebuild()

    def set_data(
        self,
        closed_trades:  list[dict],
        open_positions: list[dict],
        df:             pd.DataFrame,
        symbol:         str,
    ):
        self._raw_closed = closed_trades
        self._raw_open   = open_positions
        self._df         = df
        self._symbol     = symbol
        self._rebuild()

    def on_mouse_move(
        self,
        view_x: float, view_y: float,
        px_per_bar: float, px_per_price: float,
        plot_pos: QPoint,
    ):
        """Called each mouse-move; shows tooltip if a marker is nearby."""
        if not self._visible:
            return
        hit = self._overlay_item.hit_test(view_x, view_y, px_per_bar, px_per_price)
        if hit:
            self._tooltip.show_for(hit, plot_pos, self._price_plot.size())
        else:
            self._tooltip.hide()

    def on_click(
        self,
        view_x: float, view_y: float,
        px_per_bar: float, px_per_price: float,
    ):
        """Called on left-click; highlights the nearest trade (toggle)."""
        if not self._visible:
            return
        hit = self._overlay_item.hit_test(
            view_x, view_y, px_per_bar, px_per_price, threshold_px=20.0
        )
        current = self._overlay_item._selected_id
        new_id  = hit["trade_id"] if hit else None
        # Toggle: clicking same trade deselects it
        self._overlay_item.set_selected(None if new_id == current else new_id)

    def hide_tooltip(self):
        self._tooltip.hide()

    # ── Private ───────────────────────────────────────────────────────

    def _rebuild(self):
        """Re-process raw trade data into render-ready dicts and push to item."""
        if self._df is None or self._df.empty:
            self._overlay_item.set_data([])
            return

        df     = self._df
        symbol = self._symbol

        # Require DatetimeIndex — if not, bail gracefully
        if not hasattr(df.index, "dtype") or "datetime" not in str(df.index.dtype):
            logger.debug("TradeOverlayManager: df.index is not DatetimeIndex — overlay skipped")
            self._overlay_item.set_data([])
            return

        idx = df.index
        items: list[dict] = []

        # ── Closed trades ─────────────────────────────────────────────
        for i, t in enumerate(self._raw_closed):
            if t.get("symbol") != symbol:
                continue
            entry_ts = _parse_ts(str(t.get("opened_at") or ""))
            if entry_ts is None:
                continue
            entry_bar  = _nearest_bar(entry_ts, idx)
            entry_y    = float(t.get("entry_price") or 0)
            if entry_y <= 0:
                continue

            exit_ts  = _parse_ts(str(t.get("closed_at") or ""))
            exit_bar = _nearest_bar(exit_ts, idx) if exit_ts else None
            exit_y   = float(t.get("exit_price") or 0) if t.get("exit_price") else None

            r_val   = _compute_realized_r(t)
            t_id    = int(t.get("id") or i + 1)
            base_c  = _trade_color(t_id)
            disp_c  = _r_adjusted_color(base_c, r_val)

            # Compute exit quality using a temp dict that carries _exit_bar
            t_aug = dict(t, _exit_bar=exit_bar)
            eq    = _compute_exit_quality(t_aug, df) if exit_bar is not None else "unknown"

            items.append({
                "trade_id":     t_id,
                "symbol":       symbol,
                "side":         t.get("side", "buy"),
                "is_open":      False,
                "entry_x":      float(entry_bar),
                "entry_y":      entry_y,
                "exit_x":       float(exit_bar) if exit_bar is not None else None,
                "exit_y":       exit_y,
                "color":        base_c,
                "display_color": disp_c,
                "realized_r":   r_val,
                "pnl_usdt":     t.get("pnl_usdt"),
                "pnl_pct":      t.get("pnl_pct"),
                "score":        float(t.get("score") or 0),
                "duration_s":   int(t.get("duration_s") or 0),
                "models_fired": t.get("models_fired") or [],
                "opened_at":    t.get("opened_at"),
                "closed_at":    t.get("closed_at"),
                "exit_quality": eq,
                "size_usdt":    t.get("size_usdt"),
                "stop_loss":    t.get("stop_loss"),
                "take_profit":  t.get("take_profit"),
                "entry_price":  entry_y,
                "exit_price":   exit_y,
                "symbol_weight": t.get("symbol_weight"),
            })

        # ── Open positions ─────────────────────────────────────────────
        for i, p in enumerate(self._raw_open):
            if p.get("symbol") != symbol:
                continue
            entry_ts = _parse_ts(str(p.get("opened_at") or ""))
            if entry_ts is None:
                continue
            entry_bar = _nearest_bar(entry_ts, idx)
            entry_y   = float(p.get("entry_price") or 0)
            if entry_y <= 0:
                continue

            # Use a negative ID so open positions never collide with closed trade IDs
            t_id   = -(i + 1)
            base_c = _trade_color(t_id)

            items.append({
                "trade_id":     t_id,
                "symbol":       symbol,
                "side":         p.get("side", "buy"),
                "is_open":      True,
                "entry_x":      float(entry_bar),
                "entry_y":      entry_y,
                "exit_x":       None,
                "exit_y":       None,
                "color":        base_c,
                "display_color": base_c,
                "realized_r":   None,
                "pnl_usdt":     None,
                "pnl_pct":      p.get("unrealized_pnl"),
                "score":        float(p.get("score") or 0),
                "duration_s":   0,
                "models_fired": p.get("models_fired") or [],
                "opened_at":    p.get("opened_at"),
                "closed_at":    None,
                "exit_quality": None,
                "size_usdt":    p.get("size_usdt"),
                "stop_loss":    p.get("stop_loss"),
                "take_profit":  p.get("take_profit"),
                "entry_price":  entry_y,
                "exit_price":   None,
                "symbol_weight": p.get("symbol_weight"),
            })

        # ── Filter ────────────────────────────────────────────────────
        mode = self._opts.get("filter_mode", "all")
        if mode == "open":
            items = [it for it in items if it["is_open"]]
        elif mode == "closed":
            items = [it for it in items if not it["is_open"]]

        # Sort chronologically so "last N" takes the newest trades
        items.sort(key=lambda it: it.get("opened_at") or "")

        last_n = int(self._opts.get("last_n") or 0)
        if last_n > 0:
            items = items[-last_n:]

        self._overlay_item.set_data(items)
