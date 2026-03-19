# ============================================================
# NEXUS TRADER — Walk-Forward Validation Report Generator
#
# Generates:
#   • Matplotlib charts (equity curve, rolling expectancy,
#     rolling PF, drawdown, regime/asset/model breakdowns)
#   • Standalone HTML report with embedded charts (base64)
#
# This module is STRICTLY an evaluation tool.
# It does NOT modify any trading or model logic.
# ============================================================
from __future__ import annotations

import base64
import io
import logging
import os
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

# ── Optional matplotlib import ─────────────────────────────────────────────
try:
    import matplotlib
    matplotlib.use("Agg")  # non-interactive backend
    import matplotlib.pyplot as plt
    import matplotlib.gridspec as gridspec
    import matplotlib.patches as mpatches
    _MPL_AVAILABLE = True
except ImportError:
    _MPL_AVAILABLE = False
    logger.warning("matplotlib not available — charts will be skipped")


# ─────────────────────────────────────────────────────────────────────────────
# Colour palette
# ─────────────────────────────────────────────────────────────────────────────

_C_BULL    = "#26a65b"   # green
_C_BEAR    = "#e74c3c"   # red
_C_RANGING = "#3498db"   # blue
_C_VOL_EXP = "#e67e22"   # orange
_C_VOL_COM = "#9b59b6"   # purple
_C_NEUTRAL = "#95a5a6"   # grey
_C_ACCENT  = "#2c3e50"   # dark blue-grey

_REGIME_COLOURS = {
    "bull_trend":    _C_BULL,
    "bear_trend":    _C_BEAR,
    "ranging":       _C_RANGING,
    "vol_expansion": _C_VOL_EXP,
    "vol_compress":  _C_VOL_COM,
    "unknown":       _C_NEUTRAL,
    "":              _C_NEUTRAL,
}


def _regime_colour(regime: str) -> str:
    return _REGIME_COLOURS.get(regime.lower(), _C_NEUTRAL)


# ─────────────────────────────────────────────────────────────────────────────
# Chart generation helpers
# ─────────────────────────────────────────────────────────────────────────────

def _fig_to_b64(fig) -> str:
    """Encode a matplotlib figure as a base64 PNG string."""
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=120, bbox_inches="tight",
                facecolor=fig.get_facecolor())
    buf.seek(0)
    return base64.b64encode(buf.read()).decode("utf-8")


def _style_axes(ax, title: str = "", xlabel: str = "", ylabel: str = ""):
    ax.set_facecolor("#1a1a2e")
    ax.tick_params(colors="#cccccc", labelsize=9)
    ax.spines["bottom"].set_color("#444466")
    ax.spines["left"].set_color("#444466")
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.grid(True, color="#2a2a4a", linestyle="--", alpha=0.5, linewidth=0.6)
    if title:
        ax.set_title(title, color="#e0e0ff", fontsize=10, pad=6)
    if xlabel:
        ax.set_xlabel(xlabel, color="#aaaacc", fontsize=8)
    if ylabel:
        ax.set_ylabel(ylabel, color="#aaaacc", fontsize=8)


def chart_equity_curve(result) -> Optional[str]:
    """Equity curve across all walk-forward windows."""
    if not _MPL_AVAILABLE:
        return None
    eq = result.equity_curve
    if len(eq) < 2:
        return None

    fig, ax = plt.subplots(figsize=(10, 4), facecolor="#0d0d1a")
    _style_axes(ax, title="Equity Curve — Walk-Forward Out-of-Sample",
                xlabel="Trade index", ylabel="Portfolio value (USDT)")

    xs = list(range(len(eq)))
    ax.plot(xs, eq, color="#00ccff", linewidth=1.4, label="Equity")

    # Shade drawdown regions
    peak = eq[0]
    for i, v in enumerate(eq):
        if v > peak:
            peak = v
        if v < peak:
            ax.axvspan(i - 1, i, alpha=0.12, color=_C_BEAR, linewidth=0)

    ax.axhline(y=result.config.initial_capital, color="#888899",
               linewidth=0.8, linestyle="--", alpha=0.7, label="Starting capital")

    ax.legend(fontsize=8, facecolor="#1a1a2e", labelcolor="#cccccc")
    plt.tight_layout()
    b64 = _fig_to_b64(fig)
    plt.close(fig)
    return b64


def chart_rolling_expectancy(result) -> Optional[str]:
    """Rolling-20 expectancy over trade sequence."""
    if not _MPL_AVAILABLE:
        return None
    hist = result.rolling_20_exp_history
    if len(hist) < 2:
        return None

    fig, ax = plt.subplots(figsize=(10, 3.5), facecolor="#0d0d1a")
    _style_axes(ax, title="Rolling-20 Expectancy (R)",
                xlabel="Trade index (offset by window size)", ylabel="E[R]")

    xs = list(range(len(hist)))
    ax.plot(xs, hist, color="#f39c12", linewidth=1.2, label="Rolling-20 E[R]")
    ax.axhline(y=0.0, color="#e74c3c", linewidth=1.0, linestyle="--",
               alpha=0.9, label="Break-even (0R)")
    ax.axhline(y=0.25, color="#27ae60", linewidth=0.8, linestyle=":",
               alpha=0.7, label="Target threshold (0.25R)")

    # Fill areas
    ax.fill_between(xs, hist, 0,
                    where=[v > 0 for v in hist], alpha=0.18,
                    color=_C_BULL, label="Positive edge")
    ax.fill_between(xs, hist, 0,
                    where=[v <= 0 for v in hist], alpha=0.18,
                    color=_C_BEAR, label="Negative edge")

    ax.legend(fontsize=8, facecolor="#1a1a2e", labelcolor="#cccccc")
    plt.tight_layout()
    b64 = _fig_to_b64(fig)
    plt.close(fig)
    return b64


def chart_rolling_pf(result) -> Optional[str]:
    """Rolling-20 and rolling-40 profit factor."""
    if not _MPL_AVAILABLE:
        return None
    pf20 = result.rolling_20_pf_history
    if len(pf20) < 2:
        return None

    # Compute rolling-40 from all trade R values
    from core.validation.walk_forward_regime_validator import _rolling_pf
    r_seq = [t.get("realized_r_multiple", 0.0) for t in result.all_trades
             if t.get("realized_r_multiple") is not None]
    pf40 = _rolling_pf(r_seq, 40) if len(r_seq) >= 40 else []

    fig, ax = plt.subplots(figsize=(10, 3.5), facecolor="#0d0d1a")
    _style_axes(ax, title="Rolling Profit Factor",
                xlabel="Trade index (offset by window size)", ylabel="PF")

    xs20 = list(range(len(pf20)))
    pf20_clipped = [min(v, 5.0) for v in pf20]
    ax.plot(xs20, pf20_clipped, color="#3498db", linewidth=1.3, label="Rolling-20 PF")

    if pf40:
        xs40 = list(range(len(pf40)))
        pf40_clipped = [min(v, 5.0) for v in pf40]
        ax.plot(xs40, pf40_clipped, color="#9b59b6", linewidth=1.0,
                linestyle="-.", alpha=0.8, label="Rolling-40 PF")

    ax.axhline(y=1.0, color="#e74c3c", linewidth=1.0, linestyle="--",
               alpha=0.9, label="Break-even PF = 1.0")
    ax.axhline(y=1.4, color="#27ae60", linewidth=0.8, linestyle=":",
               alpha=0.7, label="Target PF = 1.40")

    ax.set_ylim(bottom=0)
    ax.legend(fontsize=8, facecolor="#1a1a2e", labelcolor="#cccccc")
    plt.tight_layout()
    b64 = _fig_to_b64(fig)
    plt.close(fig)
    return b64


def chart_drawdown_r(result) -> Optional[str]:
    """Rolling drawdown in R units."""
    if not _MPL_AVAILABLE:
        return None
    dd = result.rolling_dd_r_history
    if len(dd) < 2:
        return None

    fig, ax = plt.subplots(figsize=(10, 3.0), facecolor="#0d0d1a")
    _style_axes(ax, title="Rolling Drawdown (R units)",
                xlabel="Trade index", ylabel="Max drawdown (R)")

    xs = list(range(len(dd)))
    ax.fill_between(xs, 0, [-v for v in dd], alpha=0.4, color=_C_BEAR)
    ax.plot(xs, [-v for v in dd], color=_C_BEAR, linewidth=1.0)
    ax.axhline(y=-10.0, color="#ff6b35", linewidth=1.0, linestyle="--",
               alpha=0.8, label="Limit: −10R")
    ax.axhline(y=0, color="#666688", linewidth=0.5)

    ax.legend(fontsize=8, facecolor="#1a1a2e", labelcolor="#cccccc")
    plt.tight_layout()
    b64 = _fig_to_b64(fig)
    plt.close(fig)
    return b64


def chart_regime_bar(result) -> Optional[str]:
    """Expectancy by regime — horizontal bar chart."""
    if not _MPL_AVAILABLE:
        return None
    by_r = result.by_regime
    items = [(k, v.get("expectancy_r", 0.0), v.get("total_trades", 0))
             for k, v in by_r.items() if v.get("total_trades", 0) >= 1]
    if not items:
        return None

    items.sort(key=lambda x: x[1], reverse=True)
    labels = [f"{k}\n(n={n})" for k, _, n in items]
    values = [v for _, v, _ in items]
    colours = [_C_BULL if v > 0 else _C_BEAR for v in values]

    fig, ax = plt.subplots(figsize=(8, max(3, len(items) * 0.7 + 1)),
                           facecolor="#0d0d1a")
    _style_axes(ax, title="Expectancy by Regime (R)",
                xlabel="E[R]", ylabel="")

    bars = ax.barh(labels, values, color=colours, edgecolor="#2a2a4a",
                   linewidth=0.5, height=0.6)
    for bar, val in zip(bars, values):
        x_pos = val + 0.01 if val >= 0 else val - 0.01
        ha = "left" if val >= 0 else "right"
        ax.text(x_pos, bar.get_y() + bar.get_height() / 2,
                f"{val:+.3f}R", va="center", ha=ha,
                color="#e0e0ff", fontsize=8)

    ax.axvline(x=0, color="#888899", linewidth=0.8, linestyle="--")
    ax.tick_params(axis="y", labelcolor="#cccccc", labelsize=8)
    plt.tight_layout()
    b64 = _fig_to_b64(fig)
    plt.close(fig)
    return b64


def chart_asset_bar(result) -> Optional[str]:
    """Expectancy by asset — horizontal bar chart."""
    if not _MPL_AVAILABLE:
        return None
    by_a = result.by_asset
    items = [(k, v.get("expectancy_r", 0.0), v.get("total_trades", 0))
             for k, v in by_a.items() if v.get("total_trades", 0) >= 1]
    if not items:
        return None

    items.sort(key=lambda x: x[1], reverse=True)
    labels = [f"{k}\n(n={n})" for k, _, n in items]
    values = [v for _, v, _ in items]
    colours = [_C_BULL if v > 0 else _C_BEAR for v in values]

    fig, ax = plt.subplots(figsize=(8, max(3, len(items) * 0.7 + 1)),
                           facecolor="#0d0d1a")
    _style_axes(ax, title="Expectancy by Asset (R)",
                xlabel="E[R]", ylabel="")

    bars = ax.barh(labels, values, color=colours, edgecolor="#2a2a4a",
                   linewidth=0.5, height=0.6)
    for bar, val in zip(bars, values):
        x_pos = val + 0.005 if val >= 0 else val - 0.005
        ha = "left" if val >= 0 else "right"
        ax.text(x_pos, bar.get_y() + bar.get_height() / 2,
                f"{val:+.3f}R", va="center", ha=ha,
                color="#e0e0ff", fontsize=8)

    ax.axvline(x=0, color="#888899", linewidth=0.8, linestyle="--")
    ax.tick_params(axis="y", labelcolor="#cccccc", labelsize=8)
    plt.tight_layout()
    b64 = _fig_to_b64(fig)
    plt.close(fig)
    return b64


def chart_model_bar(result) -> Optional[str]:
    """Expectancy by model — horizontal bar chart."""
    if not _MPL_AVAILABLE:
        return None
    by_m = result.by_model
    items = [(k, v.get("expectancy_r", 0.0), v.get("total_trades", 0))
             for k, v in by_m.items() if v.get("total_trades", 0) >= 1]
    if not items:
        return None

    items.sort(key=lambda x: x[1], reverse=True)
    labels = [f"{k}\n(n={n})" for k, _, n in items]
    values = [v for _, v, _ in items]
    colours = ["#3498db" if v > 0 else _C_BEAR for v in values]

    fig, ax = plt.subplots(figsize=(9, max(3, len(items) * 0.75 + 1)),
                           facecolor="#0d0d1a")
    _style_axes(ax, title="Expectancy by Model (R)",
                xlabel="E[R]", ylabel="")

    bars = ax.barh(labels, values, color=colours, edgecolor="#2a2a4a",
                   linewidth=0.5, height=0.6)
    for bar, val in zip(bars, values):
        x_pos = val + 0.005 if val >= 0 else val - 0.005
        ha = "left" if val >= 0 else "right"
        ax.text(x_pos, bar.get_y() + bar.get_height() / 2,
                f"{val:+.3f}R", va="center", ha=ha,
                color="#e0e0ff", fontsize=8)

    ax.axvline(x=0, color="#888899", linewidth=0.8, linestyle="--")
    ax.tick_params(axis="y", labelcolor="#cccccc", labelsize=8)
    plt.tight_layout()
    b64 = _fig_to_b64(fig)
    plt.close(fig)
    return b64


def chart_cumulative_r(result) -> Optional[str]:
    """Cumulative R-multiple progression."""
    if not _MPL_AVAILABLE:
        return None
    cum_r = result.cumulative_r_history
    if len(cum_r) < 2:
        return None

    fig, ax = plt.subplots(figsize=(10, 3.5), facecolor="#0d0d1a")
    _style_axes(ax, title="Cumulative R — Out-of-Sample Walk-Forward",
                xlabel="Trade index", ylabel="Cumulative R")

    xs = list(range(1, len(cum_r) + 1))
    ax.plot(xs, cum_r, color="#00ccff", linewidth=1.4, label="Cumulative R")
    ax.axhline(y=0, color="#888899", linewidth=0.5, linestyle="--")
    ax.fill_between(xs, 0, cum_r,
                    where=[v > 0 for v in cum_r], alpha=0.12, color=_C_BULL)
    ax.fill_between(xs, 0, cum_r,
                    where=[v <= 0 for v in cum_r], alpha=0.12, color=_C_BEAR)

    ax.legend(fontsize=8, facecolor="#1a1a2e", labelcolor="#cccccc")
    plt.tight_layout()
    b64 = _fig_to_b64(fig)
    plt.close(fig)
    return b64


def generate_all_charts(result) -> dict[str, Optional[str]]:
    """
    Generate all charts for the walk-forward report.

    Returns dict of {chart_name: base64_png_string or None}.
    """
    return {
        "equity_curve":        chart_equity_curve(result),
        "cumulative_r":        chart_cumulative_r(result),
        "rolling_expectancy":  chart_rolling_expectancy(result),
        "rolling_pf":          chart_rolling_pf(result),
        "drawdown_r":          chart_drawdown_r(result),
        "regime_bar":          chart_regime_bar(result),
        "asset_bar":           chart_asset_bar(result),
        "model_bar":           chart_model_bar(result),
    }


# ─────────────────────────────────────────────────────────────────────────────
# HTML Report Generator
# ─────────────────────────────────────────────────────────────────────────────

_CSS = """
body{font-family:'Segoe UI',Arial,sans-serif;background:#0d0d1a;color:#d0d0f0;
     margin:0;padding:20px;font-size:13px}
h1{color:#00ccff;border-bottom:2px solid #223355;padding-bottom:10px;font-size:22px}
h2{color:#7ec8e3;border-left:4px solid #3355aa;padding-left:10px;font-size:16px;margin-top:30px}
h3{color:#aaccff;font-size:13px;margin-bottom:4px}
.verdict-green{background:#0a2a1a;border:2px solid #26a65b;border-radius:8px;padding:16px;
               margin:20px 0;text-align:center}
.verdict-red  {background:#2a0a0a;border:2px solid #e74c3c;border-radius:8px;padding:16px;
               margin:20px 0;text-align:center}
.verdict-grey {background:#1a1a2a;border:2px solid #555577;border-radius:8px;padding:16px;
               margin:20px 0;text-align:center}
.verdict-label{font-size:28px;font-weight:bold;letter-spacing:2px}
.verdict-green .verdict-label{color:#26a65b}
.verdict-red   .verdict-label{color:#e74c3c}
.verdict-grey  .verdict-label{color:#888899}
.summary-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin:16px 0}
.stat-card{background:#141428;border:1px solid #2a2a4a;border-radius:6px;
           padding:12px;text-align:center}
.stat-value{font-size:22px;font-weight:bold;color:#00ccff}
.stat-label{font-size:10px;color:#8888aa;margin-top:4px;text-transform:uppercase}
table{border-collapse:collapse;width:100%;margin:10px 0;font-size:12px}
th{background:#141428;color:#7ec8e3;padding:7px 10px;border:1px solid #2a2a4a;
   font-weight:600;text-align:left}
td{padding:6px 10px;border:1px solid #1e1e38;color:#c0c0e0}
tr:nth-child(even) td{background:#111122}
.pos{color:#26a65b} .neg{color:#e74c3c} .neu{color:#aaaacc}
.chart-container{margin:20px 0;background:#141428;border-radius:8px;
                 padding:12px;border:1px solid #2a2a4a}
.chart-container img{width:100%;max-width:1000px;display:block;border-radius:4px}
.explanation{background:#141428;border:1px solid #2a3355;border-radius:6px;
             padding:14px;margin:10px 0;line-height:1.6;font-size:12px;color:#c0c0e0}
.section-divider{height:1px;background:linear-gradient(90deg,#223355,transparent);margin:30px 0}
.badge{display:inline-block;border-radius:4px;padding:2px 8px;font-size:11px;font-weight:600}
.badge-green{background:#0a2a1a;color:#26a65b;border:1px solid #26a65b}
.badge-red  {background:#2a0a0a;color:#e74c3c;border:1px solid #e74c3c}
.badge-grey {background:#1a1a2a;color:#888899;border:1px solid #555577}
footer{margin-top:40px;padding-top:16px;border-top:1px solid #2a2a4a;
       font-size:11px;color:#555577;text-align:center}
"""


def _colour_class(value: float, zero_threshold: float = 0.0) -> str:
    if value > zero_threshold:
        return "pos"
    if value < zero_threshold:
        return "neg"
    return "neu"


def _pct(value: float, decimals: int = 1) -> str:
    return f"{value:.{decimals}f}%"


def _r(value: float) -> str:
    return f"{value:+.3f}R"


def _img_tag(b64: Optional[str]) -> str:
    if not b64:
        return '<p style="color:#555577;font-style:italic">(chart unavailable — matplotlib not installed)</p>'
    return f'<img src="data:image/png;base64,{b64}" alt="chart">'


def _metric_rows_html(metrics: dict, label_prefix: str = "") -> str:
    """Return <tr> rows for a metrics dict."""
    gm = metrics
    n = gm.get("total_trades", 0)
    wr = gm.get("win_rate", 0.0)
    exp_r = gm.get("expectancy_r", 0.0)
    pf = gm.get("profit_factor", 0.0)
    dd_r = gm.get("drawdown_r", 0.0)
    pnl = gm.get("total_pnl_usdt", 0.0)
    aw_r = gm.get("avg_win_r", 0.0)
    al_r = gm.get("avg_loss_r", 0.0)

    def td_clr(val, thr=0.0):
        cls = _colour_class(val, thr)
        return f'<td class="{cls}">'

    rows = ""
    rows += f"<tr><td>{label_prefix}Trades</td><td>{n}</td></tr>"
    rows += f"<tr><td>{label_prefix}Win rate</td>{td_clr(wr - 0.45)}{_pct(wr)}</td></tr>"
    rows += f"<tr><td>{label_prefix}Expectancy</td>{td_clr(exp_r)}{_r(exp_r)}</td></tr>"
    rows += f"<tr><td>{label_prefix}Profit Factor</td>{td_clr(pf - 1.0)}{pf:.2f}</td></tr>"
    rows += f"<tr><td>{label_prefix}Max DD (R)</td>{td_clr(-dd_r)}{dd_r:.2f}R</td></tr>"
    rows += f"<tr><td>{label_prefix}Total P&L</td>{td_clr(pnl)}${pnl:+.2f}</td></tr>"
    rows += f"<tr><td>{label_prefix}Avg Win R</td><td>{aw_r:.3f}R</td></tr>"
    rows += f"<tr><td>{label_prefix}Avg Loss R</td><td>−{al_r:.3f}R</td></tr>"
    return rows


def _regime_table_html(by_regime: dict) -> str:
    rows = ""
    for regime, m in sorted(by_regime.items()):
        n = m.get("total_trades", 0)
        if n == 0:
            continue
        exp_r = m.get("expectancy_r", 0.0)
        pf    = m.get("profit_factor", 0.0)
        wr    = m.get("win_rate", 0.0)
        dd_r  = m.get("drawdown_r", 0.0)
        exp_cls = "pos" if exp_r > 0 else "neg"
        pf_cls  = "pos" if pf >= 1.1 else "neg"
        badge_cls = "badge-green" if exp_r > 0 else "badge-red"
        rows += (
            f"<tr>"
            f"<td>{regime}</td>"
            f"<td>{n}</td>"
            f'<td class="{exp_cls}">{_r(exp_r)}</td>'
            f'<td class="{pf_cls}">{pf:.2f}</td>'
            f"<td>{dd_r:.2f}R</td>"
            f"<td>{_pct(wr)}</td>"
            f'<td><span class="badge {badge_cls}">{"EDGE" if exp_r > 0 else "NO EDGE"}</span></td>'
            f"</tr>"
        )
    if not rows:
        return "<tr><td colspan='7' class='neu'>No regime data</td></tr>"
    return rows


def _dimension_table_html(by_dim: dict, dim_label: str) -> str:
    rows = ""
    for label, m in sorted(by_dim.items(),
                             key=lambda kv: kv[1].get("total_trades", 0),
                             reverse=True):
        n = m.get("total_trades", 0)
        if n == 0:
            continue
        exp_r = m.get("expectancy_r", 0.0)
        pf    = m.get("profit_factor", 0.0)
        wr    = m.get("win_rate", 0.0)
        pnl   = m.get("total_pnl_usdt", 0.0)
        exp_cls = "pos" if exp_r > 0 else "neg"
        pf_cls  = "pos" if pf >= 1.0 else "neg"
        pnl_cls = "pos" if pnl > 0 else "neg"
        rows += (
            f"<tr>"
            f"<td>{label}</td>"
            f"<td>{n}</td>"
            f'<td class="{exp_cls}">{_r(exp_r)}</td>'
            f'<td class="{pf_cls}">{pf:.2f}</td>'
            f"<td>{_pct(wr)}</td>"
            f'<td class="{pnl_cls}">${pnl:+.2f}</td>'
            f"</tr>"
        )
    if not rows:
        return f"<tr><td colspan='6' class='neu'>No {dim_label} data</td></tr>"
    return rows


def _score_bucket_table_html(by_bucket: dict) -> str:
    rows = ""
    for bucket, m in sorted(by_bucket.items()):
        n = m.get("total_trades", 0)
        if n == 0:
            continue
        exp_r = m.get("expectancy_r", 0.0)
        wr    = m.get("win_rate", 0.0)
        pf    = m.get("profit_factor", 0.0)
        exp_cls = "pos" if exp_r > 0 else "neg"
        rows += (
            f"<tr>"
            f"<td>{bucket}</td>"
            f"<td>{n}</td>"
            f'<td class="{exp_cls}">{_r(exp_r)}</td>'
            f"<td>{_pct(wr)}</td>"
            f"<td>{pf:.2f}</td>"
            f"</tr>"
        )
    if not rows:
        return "<tr><td colspan='5' class='neu'>No score bucket data</td></tr>"
    return rows


def _window_table_html(result) -> str:
    rows = ""
    for w in result.windows:
        n  = w.get("n_trades", 0)
        sym = w.get("symbol", "")
        idx = w.get("window", "?")
        t_start = w.get("test_start", "")
        t_end   = w.get("test_end", "")
        eq_end  = w.get("end_equity", 0)
        # Compute window-level expectancy
        w_trades = [t for t in result.all_trades
                    if t.get("symbol") == sym and t.get("wf_window") == idx]
        if w_trades:
            from core.validation.walk_forward_regime_validator import compute_metrics
            wm = compute_metrics(w_trades)
            exp_r = wm.get("expectancy_r", 0.0)
            pf    = wm.get("profit_factor", 0.0)
        else:
            exp_r = 0.0
            pf    = 0.0
        exp_cls = "pos" if exp_r > 0 else ("neg" if n > 0 else "neu")
        rows += (
            f"<tr>"
            f"<td>{sym}</td>"
            f"<td>W{idx + 1}</td>"
            f"<td>{t_start} → {t_end}</td>"
            f"<td>{n}</td>"
            f'<td class="{exp_cls}">{_r(exp_r)}</td>'
            f"<td>{pf:.2f}</td>"
            f"<td>${eq_end:,.0f}</td>"
            f"</tr>"
        )
    if not rows:
        return "<tr><td colspan='7' class='neu'>No window data</td></tr>"
    return rows


class WalkForwardReportGenerator:
    """
    Generates a standalone HTML report from a WalkForwardResult.

    Usage:
        gen = WalkForwardReportGenerator()
        html_path = gen.generate(result, output_dir="reports/walk_forward")
    """

    def generate(
        self,
        result,
        output_dir: str = "reports/walk_forward",
        save_charts: bool = True,
    ) -> str:
        """
        Generate the HTML report and save to output_dir/walk_forward_report.html.

        Returns the absolute path of the generated file.
        """
        os.makedirs(output_dir, exist_ok=True)

        # Generate all charts
        charts = generate_all_charts(result)

        # Save individual chart PNGs alongside the HTML
        if save_charts and _MPL_AVAILABLE:
            for name, b64 in charts.items():
                if b64:
                    png_path = os.path.join(output_dir, f"{name}.png")
                    with open(png_path, "wb") as f:
                        f.write(base64.b64decode(b64))

        html = self._build_html(result, charts)
        out_path = os.path.join(output_dir, "walk_forward_report.html")
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(html)

        logger.info("Walk-forward report saved → %s", out_path)
        return out_path

    def _build_html(self, result, charts: dict) -> str:
        gm = result.global_metrics
        verdict = result.edge_verdict
        explanation = result.edge_explanation

        n_trades    = gm.get("total_trades", 0)
        win_rate    = gm.get("win_rate", 0.0)
        exp_r       = gm.get("expectancy_r", 0.0)
        pf          = gm.get("profit_factor", 0.0)
        dd_r        = gm.get("drawdown_r", 0.0)
        total_pnl   = gm.get("total_pnl_usdt", 0.0)
        avg_win_r   = gm.get("avg_win_r", 0.0)
        avg_loss_r  = gm.get("avg_loss_r", 0.0)

        # Verdict styling
        if verdict == "PERSISTENT_EDGE":
            v_cls, v_icon = "verdict-green", "✅"
        elif verdict == "REGIME_DEPENDENT":
            v_cls, v_icon = "verdict-red",   "⚠️"
        else:
            v_cls, v_icon = "verdict-grey",  "❓"

        # PFS
        pf_hist = result.rolling_20_pf_history
        if len(pf_hist) >= 5:
            import statistics
            snap = pf_hist[-10:] if len(pf_hist) >= 10 else pf_hist
            finite = [min(v, 5.0) for v in snap]
            mean_pf = statistics.mean(finite)
            std_pf  = statistics.stdev(finite) if len(finite) >= 2 else 0.0
            pfs_cv  = std_pf / mean_pf if mean_pf > 0 else 1.0
            pfs_score = max(0, min(100, round(100 * (1 - pfs_cv))))
        else:
            pfs_score = 0

        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        cfg = result.config

        # ── Strength / weakness extraction ────────────────────
        strengths_html = ""
        weaknesses_html = ""
        if "STRENGTHS:" in explanation:
            s_part = explanation.split("STRENGTHS:")[1]
            s_part = s_part.split("WEAKNESSES:")[0].strip().rstrip(".")
            for item in s_part.split(";"):
                item = item.strip()
                if item:
                    strengths_html += f'<li class="pos">✓ {item}</li>'
        if "WEAKNESSES:" in explanation:
            w_part = explanation.split("WEAKNESSES:")[1]
            w_part = w_part.split("CONCLUSION:")[0].strip().rstrip(".")
            for item in w_part.split(";"):
                item = item.strip()
                if item:
                    weaknesses_html += f'<li class="neg">✗ {item}</li>'

        conclusion_html = ""
        if "CONCLUSION:" in explanation:
            conclusion_html = explanation.split("CONCLUSION:")[1].strip()

        regimes_with_trades = sum(
            1 for v in result.by_regime.values() if v.get("total_trades", 0) >= 1
        )
        regimes_positive = sum(
            1 for v in result.by_regime.values()
            if v.get("total_trades", 0) >= 1 and v.get("expectancy_r", 0) > 0
        )

        html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>NexusTrader — Walk-Forward Regime-Segmented Validation</title>
<style>{_CSS}</style>
</head>
<body>

<h1>🔬 NexusTrader — Walk-Forward Regime-Segmented Validation</h1>
<p style="color:#7788aa;font-size:12px">
Generated: {now_str} &nbsp;|&nbsp;
Symbols: {', '.join(cfg.symbols)} &nbsp;|&nbsp;
Timeframe: {cfg.timeframe} &nbsp;|&nbsp;
Calibration: {cfg.calibration_bars} bars &nbsp;|&nbsp;
Test window: {cfg.test_bars} bars
</p>

<div class="section-divider"></div>

<!-- ══════════════════════════════════════════════════════ -->
<!-- VERDICT BANNER -->
<!-- ══════════════════════════════════════════════════════ -->
<h2>Section 10 — Final Evaluation</h2>

<div class="{v_cls}">
  <div class="verdict-label">{v_icon} {verdict.replace('_', ' ')}</div>
  <p style="margin:10px 0 0 0;font-size:13px;color:#ccccdd">
    {conclusion_html}
  </p>
</div>

<div style="display:grid;grid-template-columns:1fr 1fr;gap:16px;margin:16px 0">
  <div>
    <h3 style="color:#26a65b">Strengths</h3>
    <ul style="margin:6px 0;padding-left:20px;line-height:1.8">
    {strengths_html or '<li class="neu">None identified</li>'}
    </ul>
  </div>
  <div>
    <h3 style="color:#e74c3c">Weaknesses / Risks</h3>
    <ul style="margin:6px 0;padding-left:20px;line-height:1.8">
    {weaknesses_html or '<li class="neu">None identified</li>'}
    </ul>
  </div>
</div>

<div class="section-divider"></div>

<!-- ══════════════════════════════════════════════════════ -->
<!-- SECTION 1 — FRAMEWORK SUMMARY -->
<!-- ══════════════════════════════════════════════════════ -->
<h2>Section 1 — Walk-Forward Validation Framework</h2>
<p style="color:#9999bb;font-size:12px;line-height:1.7">
Each symbol's OHLCV data was split into sequential calibration/forward-test windows.
The calibration window supplies historical bars for indicator warm-up only — no trades
are generated during calibration.  Each forward-test window is evaluated on strictly
out-of-sample data.  The window advances by <strong style="color:#aaccff">{cfg.step_bars} bars</strong>
each cycle, creating non-overlapping test windows.
</p>

<table>
<thead><tr>
<th>Symbol</th><th>Window</th><th>Test Period</th><th>OOS Trades</th>
<th>Expectancy</th><th>Profit Factor</th><th>End Equity</th>
</tr></thead>
<tbody>
{_window_table_html(result)}
</tbody>
</table>

<div class="section-divider"></div>

<!-- ══════════════════════════════════════════════════════ -->
<!-- SECTIONS 3 + 4 — GLOBAL METRICS -->
<!-- ══════════════════════════════════════════════════════ -->
<h2>Sections 3–4 — Global Performance Metrics</h2>

<div class="summary-grid">
  <div class="stat-card">
    <div class="stat-value {'pos' if exp_r > 0 else 'neg' if exp_r < 0 else 'neu'}" style="color:{'#26a65b' if exp_r > 0 else '#e74c3c'}">{_r(exp_r)}</div>
    <div class="stat-label">Expectancy</div>
  </div>
  <div class="stat-card">
    <div class="stat-value {'pos' if pf >= 1.4 else 'neg'}" style="color:{'#26a65b' if pf >= 1.4 else '#e74c3c' if pf < 1.0 else '#f39c12'}">{pf:.2f}</div>
    <div class="stat-label">Profit Factor</div>
  </div>
  <div class="stat-card">
    <div class="stat-value">{_pct(win_rate)}</div>
    <div class="stat-label">Win Rate</div>
  </div>
  <div class="stat-card">
    <div class="stat-value" style="color:{'#26a65b' if total_pnl > 0 else '#e74c3c'}">${total_pnl:+,.2f}</div>
    <div class="stat-label">Total P&L (USDT)</div>
  </div>
  <div class="stat-card">
    <div class="stat-value">{n_trades}</div>
    <div class="stat-label">OOS Trades</div>
  </div>
  <div class="stat-card">
    <div class="stat-value" style="color:{'#26a65b' if dd_r < 5 else '#e74c3c'}">{dd_r:.2f}R</div>
    <div class="stat-label">Max Drawdown (R)</div>
  </div>
  <div class="stat-card">
    <div class="stat-value" style="color:#26a65b">{avg_win_r:.3f}R</div>
    <div class="stat-label">Avg Win R</div>
  </div>
  <div class="stat-card">
    <div class="stat-value" style="color:#e74c3c">{avg_loss_r:.3f}R</div>
    <div class="stat-label">Avg Loss R</div>
  </div>
  <div class="stat-card">
    <div class="stat-value">{pfs_score}/100</div>
    <div class="stat-label">PF Stability Score</div>
  </div>
  <div class="stat-card">
    <div class="stat-value">{regimes_positive}/{regimes_with_trades}</div>
    <div class="stat-label">Regimes w/ Positive Edge</div>
  </div>
  <div class="stat-card">
    <div class="stat-value">{len(result.by_asset)}</div>
    <div class="stat-label">Symbols Traded</div>
  </div>
  <div class="stat-card">
    <div class="stat-value">{result.window_count}</div>
    <div class="stat-label">Walk-Forward Windows</div>
  </div>
</div>

<div class="chart-container">
  <h3>Equity Curve</h3>
  {_img_tag(charts.get('equity_curve'))}
</div>

<div class="chart-container">
  <h3>Cumulative R — Out-of-Sample</h3>
  {_img_tag(charts.get('cumulative_r'))}
</div>

<div class="section-divider"></div>

<!-- ══════════════════════════════════════════════════════ -->
<!-- SECTION 5 — REGIME TABLE -->
<!-- ══════════════════════════════════════════════════════ -->
<h2>Section 5 — Regime Performance Table</h2>

<table>
<thead><tr>
<th>Regime</th><th>Trades</th><th>Expectancy</th><th>Profit Factor</th>
<th>Max DD (R)</th><th>Win Rate</th><th>Assessment</th>
</tr></thead>
<tbody>
{_regime_table_html(result.by_regime)}
</tbody>
</table>

<div class="chart-container">
  {_img_tag(charts.get('regime_bar'))}
</div>

<div class="section-divider"></div>

<!-- ══════════════════════════════════════════════════════ -->
<!-- SECTION 6 — ASSET & MODEL ATTRIBUTION -->
<!-- ══════════════════════════════════════════════════════ -->
<h2>Section 6 — Asset Attribution</h2>

<table>
<thead><tr>
<th>Asset</th><th>Trades</th><th>Expectancy</th><th>Profit Factor</th>
<th>Win Rate</th><th>Total P&L</th>
</tr></thead>
<tbody>
{_dimension_table_html(result.by_asset, 'asset')}
</tbody>
</table>

<div class="chart-container">
  {_img_tag(charts.get('asset_bar'))}
</div>

<h2>Section 6b — Model Attribution</h2>

<table>
<thead><tr>
<th>Model</th><th>Trades</th><th>Expectancy</th><th>Profit Factor</th>
<th>Win Rate</th><th>Total P&L</th>
</tr></thead>
<tbody>
{_dimension_table_html(result.by_model, 'model')}
</tbody>
</table>

<div class="chart-container">
  {_img_tag(charts.get('model_bar'))}
</div>

<div class="section-divider"></div>

<!-- ══════════════════════════════════════════════════════ -->
<!-- SECTION 7 — ROLLING STABILITY -->
<!-- ══════════════════════════════════════════════════════ -->
<h2>Section 7 — Rolling Stability Metrics</h2>

<div class="chart-container">
  <h3>Rolling-20 Expectancy</h3>
  {_img_tag(charts.get('rolling_expectancy'))}
</div>

<div class="chart-container">
  <h3>Rolling Profit Factor</h3>
  {_img_tag(charts.get('rolling_pf'))}
</div>

<div class="chart-container">
  <h3>Drawdown in R</h3>
  {_img_tag(charts.get('drawdown_r'))}
</div>

<div class="section-divider"></div>

<!-- ══════════════════════════════════════════════════════ -->
<!-- SECTION 8 — SCORE BUCKET CALIBRATION -->
<!-- ══════════════════════════════════════════════════════ -->
<h2>Section 8 — Score Calibration Diagnostic</h2>
<p style="color:#9999bb;font-size:12px">
Higher confluence scores should produce higher expectancy.
This table is diagnostic only — it does not influence live weights.
</p>

<table>
<thead><tr>
<th>Score bucket</th><th>Trades</th><th>Expectancy</th><th>Win Rate</th><th>Profit Factor</th>
</tr></thead>
<tbody>
{_score_bucket_table_html(result.by_score_bucket)}
</tbody>
</table>

<div class="section-divider"></div>

<!-- ══════════════════════════════════════════════════════ -->
<!-- SECTION 9 — VISUALIZATIONS REFERENCE -->
<!-- ══════════════════════════════════════════════════════ -->
<h2>Section 9 — Visualization Summary</h2>
<p style="color:#9999bb;font-size:12px;line-height:1.7">
All charts are embedded above and saved as PNG files alongside this report.
Individual chart files: equity_curve.png, cumulative_r.png, rolling_expectancy.png,
rolling_pf.png, drawdown_r.png, regime_bar.png, asset_bar.png, model_bar.png.
</p>

<div class="section-divider"></div>

<footer>
NexusTrader Walk-Forward Regime-Segmented Validation &nbsp;|&nbsp;
Generated: {now_str} &nbsp;|&nbsp;
This report is for evaluation purposes only.  It does not constitute financial advice.
</footer>

</body>
</html>"""

        return html
