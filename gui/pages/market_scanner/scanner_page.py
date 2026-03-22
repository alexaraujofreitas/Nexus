# ============================================================
# NEXUS TRADER — Market Scanner Page  (v3 — IDSS edition)
#
# Two-tab layout:
#   Tab 1 "Market Scanner":  original CoinGecko + tech-indicator scan
#   Tab 2 "IDSS AI Scanner": IDSS pipeline with regime labels,
#                             confluence scores, watchlists, order candidates
# ============================================================
from __future__ import annotations

import logging
import time
import urllib.request
import urllib.parse
import json
from datetime import datetime, date, timezone
from typing import Optional

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QFrame,
    QTableWidget, QTableWidgetItem, QPushButton, QComboBox,
    QHeaderView, QProgressBar, QLineEdit, QListWidget, QListWidgetItem,
    QAbstractItemView, QSizePolicy, QTabWidget, QTextEdit,
    QSplitter, QInputDialog, QScrollArea, QSpinBox, QCheckBox,
)
from PySide6.QtCore import Qt, QThread, Signal, Slot, QTimer
from PySide6.QtGui import QColor, QFont

from gui.main_window import PageHeader

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────
# Shared Styles
# ─────────────────────────────────────────────────────────────
_COMBO_STYLE = (
    "QComboBox { background:#0F1623; color:#E8EBF0; border:1px solid #2A3A52; "
    "border-radius:4px; padding:3px 8px; font-size:13px; min-height:28px; }"
    "QComboBox:focus { border-color:#1E90FF; }"
    "QComboBox QAbstractItemView { background:#0F1623; color:#E8EBF0; "
    "selection-background-color:#1A2D4A; border:1px solid #2A3A52; }"
)
_EDIT_STYLE = (
    "QLineEdit { background:#0F1623; color:#E8EBF0; border:1px solid #2A3A52; "
    "border-radius:4px; padding:3px 8px; font-size:13px; min-height:28px; }"
    "QLineEdit:focus { border-color:#1E90FF; }"
)
_LBL_STYLE   = "color:#8899AA; font-size:13px; font-weight:600;"
_SECT_STYLE  = "color:#6A7E99; font-size:13px; font-weight:700;"

_TAB_STYLE = (
    "QTabWidget::pane { border:none; background:#080C16; }"
    "QTabBar::tab { background:#0D1320; color:#6A7E99; padding:8px 18px; "
    "font-size:13px; font-weight:600; border:none; border-bottom:2px solid transparent; }"
    "QTabBar::tab:selected { color:#E8EBF0; border-bottom:2px solid #1E90FF; }"
    "QTabBar::tab:hover { color:#B0C0D0; }"
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

_CARD_STYLE = (
    "QFrame#card { background:#0D1320; border:1px solid #1A2332; border-radius:6px; }"
)

_BTN_PRIMARY = (
    "QPushButton { background:#1E90FF; color:#FFF; border:none; "
    "border-radius:5px; font-size:13px; font-weight:700; padding:0 16px; }"
    "QPushButton:hover { background:#3AA0FF; }"
    "QPushButton:pressed { background:#1070DD; }"
    "QPushButton:disabled { background:#1A2D4A; color:#4A6A8A; }"
)
_BTN_STOP = (
    "QPushButton { background:#1A0A0A; color:#FF3355; "
    "border:1px solid #440011; border-radius:5px; "
    "font-size:13px; font-weight:700; padding:0 16px; }"
    "QPushButton:hover { background:#2A1010; }"
    "QPushButton:disabled { color:#3A1A1A; border-color:#1A0A0A; }"
)
_BTN_SUCCESS = (
    "QPushButton { background:#0D2A1A; color:#00CC77; "
    "border:1px solid #005522; border-radius:5px; "
    "font-size:13px; font-weight:700; padding:0 16px; }"
    "QPushButton:hover { background:#103320; }"
    "QPushButton:disabled { color:#1A4A2A; border-color:#0A1A0A; }"
)
_BTN_AUTO_OFF = (
    "QPushButton { background:#111828; color:#4A6A8A; "
    "border:1px solid #1E2D44; border-radius:5px; "
    "font-size:13px; font-weight:700; padding:0 16px; }"
    "QPushButton:hover { background:#1A2540; color:#6A8AAA; }"
)
_BTN_AUTO_ON = (
    "QPushButton { background:#2A1800; color:#FFB300; "
    "border:1px solid #664400; border-radius:5px; "
    "font-size:13px; font-weight:700; padding:0 16px; }"
    "QPushButton:hover { background:#3A2200; }"
)


# ─────────────────────────────────────────────────────────────
# Existing market-scanner utilities (unchanged)
# ─────────────────────────────────────────────────────────────
def _fmt_mcap(v: float) -> str:
    if not v or v <= 0:
        return "—"
    if v >= 1e12:
        return f"${v / 1e12:.2f}T"
    if v >= 1e9:
        return f"${v / 1e9:.2f}B"
    if v >= 1e6:
        return f"${v / 1e6:.0f}M"
    return f"${v:,.0f}"


def _fmt_price(v: float) -> str:
    if v == 0:
        return "—"
    if v >= 1_000:
        return f"{v:,.2f}"
    if v >= 1:
        return f"{v:.4f}"
    return f"{v:.6f}"


def _fmt_vol(v) -> str:
    if v is None or v <= 0:
        return "—"
    if v >= 1e9:
        return f"${v / 1e9:.2f}B"
    if v >= 1e6:
        return f"${v / 1e6:.1f}M"
    if v >= 1e3:
        return f"${v / 1e3:.0f}K"
    return f"${v:.0f}"


def _pct_color(p: float) -> str:
    if p > 0:
        return "#00CC77"
    if p < 0:
        return "#FF3355"
    return "#8899AA"


def _colored_item(text: str, color: str = "#E8EBF0",
                  align=Qt.AlignCenter) -> QTableWidgetItem:
    item = QTableWidgetItem(text)
    item.setForeground(QColor(color))
    item.setTextAlignment(align)
    item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
    return item


class _NumericItem(QTableWidgetItem):
    """QTableWidgetItem that sorts numerically via UserRole data."""
    def __lt__(self, other: QTableWidgetItem) -> bool:
        try:
            my_val    = self.data(Qt.UserRole)
            other_val = other.data(Qt.UserRole)
            # None values sort to the bottom (ascending) — they have no data
            if my_val is None:
                return False
            if other_val is None:
                return True
            return float(my_val) < float(other_val)
        except (TypeError, ValueError):
            return super().__lt__(other)


def _numeric_item(value, text: str, color: str = "#E8EBF0") -> _NumericItem:
    """Create a table item that displays *text* but sorts by numeric *value*."""
    item = _NumericItem(text)
    item.setData(Qt.UserRole, float(value) if value is not None else None)
    item.setForeground(QColor(color))
    item.setTextAlignment(Qt.AlignCenter)
    item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
    return item


def _pct_cell(val) -> tuple:
    """Return (text, color) for a percentage value.
    *val* == None means the data is unavailable → show "—".
    *val* == 0.0 is a genuine flat reading → show "+0.00%".
    """
    if val is None:
        return ("—", "#3A4A5A")
    return (f"{val:+.2f}%", _pct_color(val))


SIGNAL_COLORS = {
    "bullish": "#00CC77",
    "bearish": "#FF3355",
    "neutral": "#8899AA",
}


# ─────────────────────────────────────────────────────────────────────────────
# Rationale Panel HTML builder
# Converts the per-symbol candidate dict (including diagnostics) into a rich
# HTML document displayed in the QTextEdit rationale panel.
# ─────────────────────────────────────────────────────────────────────────────

# Model human-readable names
_MODEL_DISPLAY_NAMES: dict[str, str] = {
    "trend":              "Trend Model",
    "mean_reversion":     "Mean Reversion",
    "momentum_breakout":  "Momentum Breakout",
    "vwap_reversion":     "VWAP Reversion",
    "liquidity_sweep":    "Liquidity Sweep",
    "funding_rate":       "Funding Rate",
    "order_book":         "Order Book",
    "sentiment":          "Sentiment",
    "rl_ensemble":        "RL Ensemble",
    "orchestrator":       "Orchestrator",
}

# Regime context descriptions (brief, conversational)
_REGIME_CONTEXT: dict[str, str] = {
    "bull_trend":            "Sustained upward price movement — trend-following models have highest edge.",
    "bear_trend":            "Sustained downward price movement — short-bias models have highest edge.",
    "ranging":               "Price oscillating between levels — mean-reversion models preferred.",
    "volatility_expansion":  "Volatility spiking outward — breakout/momentum models active.",
    "volatility_compression":"Volatility contracting — expect breakout soon; reversion models active.",
    "uncertain":             "Directional signal is weak — all model activation weights are reduced.",
}

# Model 'no signal' reason explanations per regime
_MODEL_REGIME_HINTS: dict[str, dict[str, str]] = {
    "trend": {
        "ranging":              "ADX insufficient for ranging regime — needs trending conditions.",
        "uncertain":            "Evaluated but conditions not met (ADX threshold or EMA alignment).",
        "volatility_expansion": "Evaluated in volatility expansion — EMA/ADX check not met.",
        "default":              "EMA crossover or ADX threshold not met this candle.",
    },
    "momentum_breakout": {
        "ranging":              "Low-volatility ranging markets suppress breakout signals.",
        "uncertain":            "Evaluated — Bollinger/ATR squeeze condition not met.",
        "default":              "Bollinger Band or ATR breakout threshold not met.",
    },
    "vwap_reversion": {
        "bull_trend":           "Strong trend reduces VWAP reversion signal probability.",
        "bear_trend":           "Strong trend reduces VWAP reversion signal probability.",
        "default":              "Price not sufficiently deviated from VWAP this candle.",
    },
    "funding_rate": {
        "default":              "Funding rate not extreme enough to trigger contrarian signal.",
    },
    "order_book": {
        "default":              "Order book imbalance below threshold.",
    },
    "sentiment": {
        "default":              "Insufficient sentiment signal from news analysis.",
    },
    "rl_ensemble": {
        "default":              "RL model confidence below activation threshold.",
    },
}


def _regime_model_hint(model_name: str, regime: str) -> str:
    """Return a short reason why a model didn't fire in this regime."""
    hints = _MODEL_REGIME_HINTS.get(model_name, {})
    return hints.get(regime, hints.get("default", "Conditions not met this candle."))


def _fmt_age(seconds) -> str:
    """Format candle age as human-readable string."""
    if seconds is None:
        return "Unknown"
    s = int(seconds)
    if s < 60:
        return f"{s}s ago"
    if s < 3600:
        return f"{s // 60}m {s % 60}s ago"
    return f"{s // 3600}h {(s % 3600) // 60}m ago"


def _build_rationale_html(c: dict) -> str:
    """
    Build full-transparency HTML rationale for the IDSS analysis panel.
    Uses actual pipeline state — no hardcoded messages.
    """
    # ── Core candidate fields ──────────────────────────────────────────
    sym          = c.get("symbol", "?")
    side_raw     = c.get("side", "")
    tf           = c.get("timeframe", "1h")
    is_approved  = c.get("is_approved", False)
    status       = c.get("status", "")
    score_val    = float(c.get("score") or 0.0)
    entry        = c.get("entry_price")
    stop_p       = float(c.get("stop_loss_price") or 0.0)
    tp_p         = float(c.get("take_profit_price") or 0.0)
    rr           = float(c.get("risk_reward_ratio") or 0.0)
    size         = float(c.get("position_size_usdt") or 0.0)
    models_fired = c.get("models_fired") or []
    regime_raw   = c.get("regime", "")
    htf_regime   = c.get("higher_tf_regime", "")
    no_signal    = c.get("_no_signal", False)

    # ── Diagnostics dict ──────────────────────────────────────────────
    diag          = c.get("diagnostics") or {}
    regime_conf   = float(diag.get("regime_confidence") or 0.0)
    regime_probs  = diag.get("regime_probs") or {}
    candle_age_s  = diag.get("candle_age_s")
    candle_count  = int(diag.get("candle_count") or 0)
    candle_ts     = diag.get("candle_ts_str", "")
    all_models    = diag.get("all_model_names") or []
    m_disabled    = diag.get("models_disabled") or []
    m_no_signal   = diag.get("models_no_signal") or []
    m_fired       = diag.get("models_fired") or models_fired
    raw_score     = float(diag.get("raw_score") or score_val)
    eff_thresh    = float(diag.get("effective_threshold") or 0.45)
    per_model     = diag.get("per_model") or {}
    dir_split     = diag.get("direction_split") or {}

    # ── Derived display values ─────────────────────────────────────────
    side_label  = "LONG" if side_raw == "buy" else ("SHORT" if side_raw == "sell" else "—")
    side_color  = "#00CC77" if side_raw == "buy" else ("#FF3355" if side_raw == "sell" else "#8899AA")
    regime_lbl  = REGIME_LABELS.get(regime_raw, regime_raw.replace("_", " ").title() if regime_raw else "Unknown")
    regime_clr  = REGIME_COLORS.get(regime_raw, "#8899AA")
    score_pct   = f"{raw_score:.1%}" if raw_score else "—"
    thresh_pct  = f"{eff_thresh:.1%}"

    # ── CSS shared across all sections ────────────────────────────────
    S = {
        "bg":       "#080C16",
        "hdr_txt":  "#E8EBF0",
        "dim":      "#6A7E99",
        "bright":   "#C0D0E0",
        "green":    "#00CC77",
        "red":      "#FF3355",
        "amber":    "#FFB300",
        "blue":     "#1E90FF",
        "label":    "color:#6A7E99; font-size:11px; font-weight:700; letter-spacing:0.08em;",
        "value":    "color:#C0D0E0; font-size:13px;",
        "mono":     "font-family:'Courier New',monospace; font-size:12px; color:#C0D0E0;",
    }

    def _sec(title: str) -> str:
        return (f'<div style="color:#4A6A8A; font-size:10px; font-weight:700; '
                f'letter-spacing:0.12em; text-transform:uppercase; '
                f'margin-top:12px; margin-bottom:4px; '
                f'border-bottom:1px solid #1A2332; padding-bottom:2px;">'
                f'{title}</div>')

    def _row(label: str, value_html: str) -> str:
        return (f'<div style="display:flex; margin-bottom:3px;">'
                f'<span style="color:#6A7E99; font-size:12px; min-width:110px; flex-shrink:0;">{label}</span>'
                f'<span style="color:#C0D0E0; font-size:12px;">{value_html}</span>'
                f'</div>')

    parts: list[str] = []

    # ════════════════════════════════════════════════════════════════
    # HEADER — symbol + side + timeframe
    # ════════════════════════════════════════════════════════════════
    parts.append(
        f'<div style="margin-bottom:10px; padding-bottom:8px; border-bottom:1px solid #1A2332;">'
        f'<span style="font-size:16px; font-weight:700; color:#FFFFFF;">{sym}</span>'
        + (f'&nbsp;&nbsp;<span style="font-size:13px; font-weight:700; color:{side_color}; '
           f'background:{side_color}22; padding:2px 8px; border-radius:3px;">{side_label}</span>' if side_raw else '')
        + (f'&nbsp;<span style="font-size:12px; color:#4A6A8A;">· {tf}</span>' if tf else '')
        + f'</div>'
    )

    # ════════════════════════════════════════════════════════════════
    # STATUS
    # ════════════════════════════════════════════════════════════════
    parts.append(_sec("STATUS"))
    if is_approved:
        status_badge  = f'<b style="color:{S["green"]}; font-size:13px;">✓ APPROVED</b>'
        status_detail = (f'Score <b style="color:{S["green"]}">{score_pct}</b> '
                         f'&ge; threshold <b style="color:{S["amber"]}">{thresh_pct}</b>')
    elif status in ("No signal", "No data", "Stale data", "Filtered"):
        _badge_clr    = S["dim"]
        status_badge  = f'<b style="color:{_badge_clr}; font-size:13px;">✗ {status.upper()}</b>'
        status_detail = {
            "No signal":  "No sub-model triggered signal conditions this candle.",
            "No data":    "Insufficient OHLCV data returned from exchange.",
            "Stale data": "Latest candle is older than 3× the timeframe — data may be stale.",
            "Filtered":   "Symbol did not pass universe filter (liquidity / spread / ATR).",
        }.get(status, status)
    elif status == "Below threshold":
        score_gap  = eff_thresh - raw_score
        status_badge  = f'<b style="color:{S["amber"]}; font-size:13px;">✗ BELOW THRESHOLD</b>'
        status_detail = (f'Score <b style="color:{S["amber"]}">{score_pct}</b> '
                         f'vs threshold <b style="color:{S["amber"]}">{thresh_pct}</b> '
                         f'&mdash; gap: <b style="color:{S["red"]}">{score_gap:.1%}</b>')
    else:
        # Risk gate rejection
        status_badge  = f'<b style="color:{S["red"]}; font-size:13px;">✗ REJECTED</b>'
        status_detail = f'<span style="color:#FF6680;">{status}</span>'

    parts.append(f'<div style="margin-bottom:6px;">{status_badge}'
                 f'&nbsp;&nbsp;<span style="color:#8899AA; font-size:12px;">{status_detail}</span></div>')

    # ════════════════════════════════════════════════════════════════
    # REGIME
    # ════════════════════════════════════════════════════════════════
    parts.append(_sec("REGIME"))
    regime_conf_pct = f"{regime_conf:.0%}" if regime_conf else "?"
    regime_ctx  = _REGIME_CONTEXT.get(regime_raw, "")
    parts.append(
        f'<div style="margin-bottom:4px;">'
        f'<span style="color:{regime_clr}; font-weight:700; font-size:13px;">{regime_lbl}</span>'
        f'&nbsp;&nbsp;<span style="color:#6A7E99; font-size:12px;">confidence {regime_conf_pct}</span>'
        f'</div>'
    )
    if regime_ctx:
        parts.append(f'<div style="color:#7A8FA8; font-size:11px; margin-bottom:4px; font-style:italic;">{regime_ctx}</div>')

    # Top regime probabilities (show top-3 if available)
    if regime_probs:
        top3 = sorted(regime_probs.items(), key=lambda x: x[1], reverse=True)[:3]
        prob_parts = []
        for rk, rv in top3:
            rc = REGIME_COLORS.get(rk, "#8899AA")
            rl = REGIME_LABELS.get(rk, rk.replace("_", " ").title())
            prob_parts.append(f'<span style="color:{rc};">{rl} {rv:.0%}</span>')
        parts.append(
            f'<div style="font-size:11px; color:#6A7E99; margin-bottom:2px;">'
            f'Regime distribution: {" &nbsp;|&nbsp; ".join(prob_parts)}</div>'
        )

    # HTF regime alignment
    if htf_regime:
        htf_lbl = REGIME_LABELS.get(htf_regime, htf_regime.replace("_", " ").title())
        htf_clr = REGIME_COLORS.get(htf_regime, "#8899AA")
        # Alignment check
        _htf_aligned = (side_raw == "buy" and "bear" not in htf_regime) or \
                       (side_raw == "sell" and "bull" not in htf_regime) or \
                       (not side_raw)
        _align_str = f'<span style="color:{S["green"]};">✓ Aligned</span>' if _htf_aligned else \
                     f'<span style="color:{S["red"]};">✗ Conflict</span>'
        parts.append(
            f'<div style="font-size:11px; color:#6A7E99; margin-bottom:2px;">'
            f'4h HTF regime: <span style="color:{htf_clr};">{htf_lbl}</span>'
            f'&nbsp;&nbsp;{_align_str}</div>'
        )

    # ════════════════════════════════════════════════════════════════
    # MODEL BREAKDOWN
    # ════════════════════════════════════════════════════════════════
    parts.append(_sec("MODEL BREAKDOWN"))

    # Build ordered list: fired first, then no-signal, then disabled
    # Use all_models if available, otherwise fall back to fired+disabled+no_signal
    _ordered_models = (
        list(all_models) if all_models
        else list(m_fired) + [m for m in m_no_signal if m not in m_fired]
                          + [m for m in m_disabled if m not in m_fired and m not in m_no_signal]
    )
    # Deduplicate preserving order
    _seen: set = set()
    _model_order: list = []
    for _mn in _ordered_models:
        if _mn not in _seen:
            _model_order.append(_mn)
            _seen.add(_mn)
    # If nothing available, show a generic placeholder
    if not _model_order:
        parts.append('<div style="color:#4A6A8A; font-size:12px; font-style:italic;">No model data available.</div>')
    else:
        for mname in _model_order:
            mdisplay = _MODEL_DISPLAY_NAMES.get(mname, mname.replace("_", " ").title())
            if mname in m_disabled:
                # Disabled by config
                parts.append(
                    f'<div style="margin-bottom:3px; font-size:12px;">'
                    f'<span style="color:#2A3A52;">—&nbsp;</span>'
                    f'<span style="color:#2A4A3A;">{mdisplay}</span>'
                    f'<span style="color:#1A3A2A; font-size:11px;">&nbsp;[DISABLED by config]</span>'
                    f'</div>'
                )
            elif mname in m_fired or mname in per_model:
                # Model fired — show direction, strength, contribution
                pm    = per_model.get(mname, {})
                dirs  = pm.get("direction", "")
                st    = pm.get("strength", 0.0)
                cont  = pm.get("contribution", 0.0)
                wt    = pm.get("weight", 0.0)
                d_clr = "#00CC77" if dirs == "long" else ("#FF3355" if dirs == "short" else "#8899AA")
                d_lbl = dirs.upper() if dirs else "—"
                parts.append(
                    f'<div style="margin-bottom:3px; font-size:12px;">'
                    f'<span style="color:{S["green"]};">✓&nbsp;</span>'
                    f'<span style="color:#D0E0F0; font-weight:600;">{mdisplay}</span>'
                    f'&nbsp;<span style="color:{d_clr}; font-size:11px;">[{d_lbl}]</span>'
                    f'&nbsp;<span style="color:#8899AA; font-size:11px;">'
                    f'str {st:.2f} &middot; wt {wt:.2f} &middot; contrib {cont:.1%}'
                    f'</span>'
                    f'</div>'
                )
            else:
                # Model ran but no signal
                hint = _regime_model_hint(mname, regime_raw)
                parts.append(
                    f'<div style="margin-bottom:3px; font-size:12px;">'
                    f'<span style="color:#3A5A7A;">○&nbsp;</span>'
                    f'<span style="color:#4A6A8A;">{mdisplay}</span>'
                    f'<span style="color:#3A4A5A; font-size:11px;">&nbsp;— {hint}</span>'
                    f'</div>'
                )

    # Direction split (only if meaningful)
    if dir_split and (dir_split.get("long", 0) > 0 or dir_split.get("short", 0) > 0):
        _dlong  = dir_split.get("long", 0)
        _dshort = dir_split.get("short", 0)
        _ddom   = dir_split.get("dominance", 0)
        parts.append(
            f'<div style="font-size:11px; color:#5A7A9A; margin-top:4px;">'
            f'Direction weight: <span style="color:#00CC77;">Long {_dlong:.3f}</span>'
            f' &nbsp;|&nbsp; <span style="color:#FF3355;">Short {_dshort:.3f}</span>'
            f' &nbsp;|&nbsp; Dominance {_ddom:.0%}'
            f'</div>'
        )

    # ════════════════════════════════════════════════════════════════
    # TRADE SETUP (approved) or WHY NO TRADE (rejected/no signal)
    # ════════════════════════════════════════════════════════════════
    if is_approved and entry and entry > 0:
        parts.append(_sec("TRADE SETUP"))
        parts.append(
            f'<div style="font-size:12px; margin-bottom:3px;">'
            f'<span style="color:#6A7E99;">Entry&nbsp;</span>'
            f'<span style="color:#E8EBF0; font-weight:600;">{_fmt_price(entry)}</span>'
            f'&nbsp;&nbsp;'
            f'<span style="color:#6A7E99;">Stop&nbsp;</span>'
            f'<span style="color:#FF6680;">{_fmt_price(stop_p)}</span>'
            f'&nbsp;&nbsp;'
            f'<span style="color:#6A7E99;">Target&nbsp;</span>'
            f'<span style="color:#00CC77;">{_fmt_price(tp_p)}</span>'
            f'</div>'
        )
        parts.append(
            f'<div style="font-size:12px;">'
            f'<span style="color:#6A7E99;">R:R&nbsp;</span>'
            f'<span style="color:#FFB300; font-weight:600;">{rr:.2f}×</span>'
            f'&nbsp;&nbsp;'
            f'<span style="color:#6A7E99;">Est. Size&nbsp;</span>'
            f'<span style="color:#C0D0E0;">${size:.0f} USDT</span>'
            f'</div>'
        )
    elif status == "Below threshold":
        parts.append(_sec("WHY NO TRADE"))
        score_gap  = eff_thresh - raw_score
        fired_count = len(m_fired)
        _gap_clr = S["amber"] if score_gap < 0.1 else S["red"]
        parts.append(
            f'<div style="font-size:12px; margin-bottom:4px; color:#8899AA;">'
            f'Score <b style="color:{S["amber"]}">{raw_score:.1%}</b> needs to reach '
            f'<b style="color:{S["amber"]}">{eff_thresh:.1%}</b> '
            f'(gap: <b style="color:{_gap_clr}">{score_gap:.1%}</b>).'
            f'</div>'
        )
        parts.append(_sec("WHAT NEEDS TO CHANGE"))
        _need_parts = []
        if fired_count == 1:
            _need_parts.append("A second model firing in the same direction would increase confluence significantly.")
        if regime_conf < 0.5:
            _need_parts.append(f"Higher regime confidence (currently {regime_conf:.0%}) would lower the dynamic threshold.")
        if score_gap < 0.08:
            _need_parts.append("Score is very close — a slightly stronger signal from an active model may be sufficient.")
        if regime_raw == "uncertain":
            _need_parts.append("Clearer regime classification (e.g. ranging or bull_trend) would activate more models at full weight.")
        if not _need_parts:
            _need_parts.append("Wait for a stronger market impulse or regime shift to push signal strength above threshold.")
        for _np in _need_parts:
            parts.append(f'<div style="font-size:12px; color:#7A9ABB; margin-bottom:2px;">▸ {_np}</div>')

    elif status in ("No signal", "Filtered", "No data", "Stale data"):
        parts.append(_sec("WHAT NEEDS TO CHANGE"))
        _status_advice = {
            "No signal": (
                f"Sub-models need clearer market conditions for regime <b style='color:{regime_clr}'>{regime_lbl}</b>. "
                "A directional impulse (ADX rise, Bollinger squeeze break, VWAP deviation) "
                "would trigger at least one model."
            ),
            "Filtered":   "Symbol did not pass the universe liquidity/spread filter. Volume or spread conditions need to improve.",
            "No data":    "Exchange returned insufficient bars. Check connection and symbol availability.",
            "Stale data": "Candle data is older than 3× the timeframe. The exchange or feed may be throttled.",
        }
        _adv = _status_advice.get(status, "Investigate the pipeline stage indicated in the STATUS field.")
        parts.append(f'<div style="font-size:12px; color:#7A9ABB;">{_adv}</div>')

    elif not is_approved and status:
        # Risk gate rejection with specific reason
        parts.append(_sec("WHY REJECTED"))
        _rg_details = {
            "EV gate":            "Expected value (win_prob × reward − loss_prob × risk) is below the minimum threshold of 0.05. Signal may not justify risk.",
            "R:R floor":          f"Risk:Reward ratio {rr:.2f}× is below the minimum floor of 1.0. Stop may be too tight relative to target.",
            "Portfolio heat":     "Portfolio is at maximum heat (6% capital at risk). No new trades until existing positions reduce.",
            "MTF conflict":       f"Higher timeframe regime ({htf_regime or '4h'}) contradicts signal direction. Disable MTF confirmation to allow.",
            "Correlation":        "This asset is too correlated with an existing open position.",
        }
        _found = False
        for _key, _detail in _rg_details.items():
            if _key.lower() in status.lower():
                parts.append(f'<div style="font-size:12px; color:#8899AA; margin-bottom:2px;">{_detail}</div>')
                _found = True
                break
        if not _found:
            parts.append(f'<div style="font-size:12px; color:#8899AA;">{status}</div>')

    # ════════════════════════════════════════════════════════════════
    # DATA STATUS
    # ════════════════════════════════════════════════════════════════
    parts.append(_sec("DATA STATUS"))
    _age_str = _fmt_age(candle_age_s)
    _age_clr = S["green"] if candle_age_s is not None and candle_age_s < 120 else S["amber"]
    _data_html = (
        f'<span style="color:#6A7E99;">Last candle:</span> '
        f'<span style="color:#B0C0D0;">{candle_ts or "—"}</span>'
        f'&nbsp;&nbsp;'
        f'<span style="color:#6A7E99;">Age:</span> '
        f'<span style="color:{_age_clr};">{_age_str}</span>'
        f'&nbsp;&nbsp;'
        f'<span style="color:#6A7E99;">Bars:</span> '
        f'<span style="color:#8899AA;">{candle_count or "—"}</span>'
        f'&nbsp;&nbsp;'
        f'<span style="color:#6A7E99;">TF:</span> '
        f'<span style="color:#8899AA;">{tf}</span>'
    )
    parts.append(f'<div style="font-size:12px; margin-bottom:2px;">{_data_html}</div>')

    # ════════════════════════════════════════════════════════════════
    # Wrap in root div
    # ════════════════════════════════════════════════════════════════
    body = "\n".join(parts)
    return (
        f'<div style="background:#080C16; color:#C0D0E0; font-family:\'Segoe UI\',Arial,sans-serif; '
        f'font-size:13px; line-height:1.55; padding:10px 12px;">'
        f'{body}'
        f'</div>'
    )

MIN_MCAP_OPTIONS: list[tuple[str, float]] = [
    ("Any market cap",  0),
    ("> $500M",         500e6),
    ("> $1B",           1e9),
    ("> $2B",           2e9),
    ("> $3B",           3e9),
    ("> $4B",           4e9),
    ("> $5B",           5e9),
    ("> $10B",          10e9),
    ("> $15B",          15e9),
    ("> $20B",          20e9),
    ("> $25B",          25e9),
    ("> $50B",          50e9),
    ("> $75B",          75e9),
    ("> $100B",         100e9),
]


# ─────────────────────────────────────────────────────────────
# IDSS colour / format helpers
# ─────────────────────────────────────────────────────────────
REGIME_COLORS: dict[str, str] = {
    "bull_trend":            "#00CC77",
    "bear_trend":            "#FF3355",
    "ranging":               "#FFB300",
    "volatility_expansion":  "#1E90FF",
    "volatility_compression":"#8899AA",
    "uncertain":             "#4A6A8A",
}

REGIME_LABELS: dict[str, str] = {
    "bull_trend":            "Bull Trend",
    "bear_trend":            "Bear Trend",
    "ranging":               "Ranging",
    "volatility_expansion":  "Vol Expansion",
    "volatility_compression":"Vol Compress",
    "uncertain":             "Uncertain",
}

MODEL_ABBREVS: dict[str, str] = {
    "trend":              "TRD",
    "mean_reversion":     "MRV",
    "momentum_breakout":  "MOM",
    "liquidity_sweep":    "LIQ",
}


def _score_color(score: float) -> str:
    if score >= 0.75:
        return "#00CC77"
    if score >= 0.60:
        return "#FFB300"
    return "#FF9800"


def _fmt_age(ts_iso: str) -> str:
    """Return '2m ago', '1h ago', etc. from an ISO timestamp string."""
    try:
        dt = datetime.fromisoformat(ts_iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        delta = (datetime.now(timezone.utc) - dt).total_seconds()
        if delta < 60:
            return "just now"
        if delta < 3600:
            return f"{int(delta / 60)}m ago"
        if delta < 86400:
            return f"{int(delta / 3600)}h ago"
        return f"{int(delta / 86400)}d ago"
    except Exception:
        return "—"


# Timeframe → duration in seconds (used for candidate age check in auto-execute)
_TF_SECONDS: dict[str, int] = {
    "1m":  60,    "3m":  180,   "5m":  300,   "15m": 900,
    "30m": 1800,  "1h":  3600,  "2h":  7200,  "4h":  14400,
    "6h":  21600, "8h":  28800, "12h": 43200, "1d":  86400,
}


def _model_tag_text(models: list[str]) -> str:
    return "  ".join(MODEL_ABBREVS.get(m, m[:3].upper()) for m in models)


# ─────────────────────────────────────────────────────────────
# CoinGecko fetcher (unchanged)
# ─────────────────────────────────────────────────────────────
class CoinGeckoFetcher:
    BASE = "https://api.coingecko.com/api/v3"

    @staticmethod
    def fetch_markets(vs_currency: str = "usd",
                      min_market_cap: float = 0,
                      max_pages: int = 8) -> dict[str, dict]:
        result: dict[str, dict] = {}
        for page in range(1, max_pages + 1):
            params = urllib.parse.urlencode({
                "vs_currency":             vs_currency,
                "order":                   "market_cap_desc",
                "per_page":                250,
                "page":                    page,
                "price_change_percentage": "1h,7d,30d",
                "sparkline":               "false",
            })
            url = f"{CoinGeckoFetcher.BASE}/coins/markets?{params}"
            try:
                req = urllib.request.Request(
                    url,
                    headers={"User-Agent": "NexusTrader/3.0 (market scanner)"},
                )
                with urllib.request.urlopen(req, timeout=20) as resp:
                    coins = json.loads(resp.read().decode())
                if not coins:
                    break
                page_exhausted = False
                for coin in coins:
                    mcap = coin.get("market_cap") or 0
                    if min_market_cap > 0 and mcap < min_market_cap:
                        page_exhausted = True
                        break
                    sym = (coin.get("symbol") or "").upper()
                    if sym and sym not in result:
                        result[sym] = {
                            "market_cap":  mcap,
                            "change_1h":   coin.get("price_change_percentage_1h_in_currency")  or 0.0,
                            "change_24h":  coin.get("price_change_percentage_24h")             or 0.0,
                            "change_7d":   coin.get("price_change_percentage_7d_in_currency")  or 0.0,
                            "change_30d":  coin.get("price_change_percentage_30d_in_currency") or 0.0,
                            "volume_24h":  coin.get("total_volume")                            or 0,
                        }
                if page_exhausted:
                    break
            except Exception as exc:
                logger.warning("CoinGecko page %d: %s", page, exc)
                break
        return result


class CoinGeckoWorker(QThread):
    data_ready = Signal(dict)
    error      = Signal(str)

    def __init__(self, vs_currency: str = "usd",
                 min_market_cap: float = 0, parent=None):
        super().__init__(parent)
        self._vs       = vs_currency
        self._min_mcap = min_market_cap

    def run(self):
        try:
            data = CoinGeckoFetcher.fetch_markets(
                vs_currency=self._vs,
                min_market_cap=self._min_mcap,
            )
            self.data_ready.emit(data)
        except Exception as exc:
            self.error.emit(str(exc))


# ─────────────────────────────────────────────────────────────
# Market scanner worker (unchanged)
# ─────────────────────────────────────────────────────────────
class ScannerWorker(QThread):
    row_ready = Signal(dict)
    progress  = Signal(int, int, str)
    finished  = Signal(int)
    error     = Signal(str)

    def __init__(self, symbols: list[str], timeframe: str = "1h",
                 max_symbols: int = 150, cg_data: dict | None = None):
        super().__init__()
        self._symbols   = symbols[:max_symbols]
        self._timeframe = timeframe
        self._cg        = cg_data or {}
        self._stop      = False

    def stop(self):
        self._stop = True

    def run(self):
        try:
            from core.market_data.exchange_manager import exchange_manager
            from core.features.indicator_library import calculate_all, get_signals
            import pandas as pd

            ex = exchange_manager.get_exchange()
            if not ex:
                self.error.emit("Not connected to exchange")
                return

            total = len(self._symbols)
            done  = 0

            for sym in self._symbols:
                if self._stop:
                    break
                try:
                    self.progress.emit(done, total, f"Scanning {sym}…")
                    candles = ex.fetch_ohlcv(sym, self._timeframe, limit=100)
                    if not candles or len(candles) < 10:
                        done += 1
                        continue

                    import pandas as pd
                    df = pd.DataFrame(
                        candles,
                        columns=["timestamp", "open", "high", "low", "close", "volume"]
                    )
                    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
                    df.set_index("timestamp", inplace=True)
                    df = df.astype(float)
                    df = calculate_all(df)
                    sig = get_signals(df)

                    last  = df.iloc[-1]
                    close = float(last["close"])
                    vol   = float(last.get("volume", 0))
                    rsi   = sig.get("rsi")

                    base = sym.split("/")[0].upper()
                    cg   = self._cg.get(base, {})

                    # 24H % and 24H Volume — single ticker call covers both
                    change_24h: float | None = None
                    vol_24h_ticker: float | None = None
                    try:
                        ticker = exchange_manager.fetch_ticker(sym)
                        if ticker:
                            # 24H price change
                            ex_ch = ticker.get("change") or ticker.get("percentage")
                            if ex_ch is not None:
                                change_24h = float(ex_ch)
                            # 24H volume in USDT (quoteVolume) — the correct field
                            qv = ticker.get("quoteVolume")
                            if qv is not None and float(qv) > 0:
                                vol_24h_ticker = float(qv)
                    except Exception:
                        pass
                    if change_24h is None:
                        raw = cg.get("change_24h")
                        change_24h = float(raw) if raw is not None else None

                    # 15m % — last two 15m bars
                    change_15m: float | None = None
                    try:
                        c15 = ex.fetch_ohlcv(sym, "15m", limit=2)
                        if c15 and len(c15) >= 2:
                            p0, p1 = float(c15[-2][4]), float(c15[-1][4])
                            if p0 > 0:
                                change_15m = (p1 - p0) / p0 * 100.0
                    except Exception:
                        pass

                    # 1H % — reuse main scan bars when TF=1h; else fetch 2 bars
                    change_1h: float | None = None
                    try:
                        if self._timeframe == "1h" and len(df) >= 2:
                            p0 = float(df.iloc[-2]["close"])
                            p1 = float(df.iloc[-1]["close"])
                            if p0 > 0:
                                change_1h = (p1 - p0) / p0 * 100.0
                        else:
                            c1h = ex.fetch_ohlcv(sym, "1h", limit=2)
                            if c1h and len(c1h) >= 2:
                                p0, p1 = float(c1h[-2][4]), float(c1h[-1][4])
                                if p0 > 0:
                                    change_1h = (p1 - p0) / p0 * 100.0
                    except Exception:
                        pass

                    # 4H % — last two 4h bars
                    change_4h: float | None = None
                    try:
                        c4h = ex.fetch_ohlcv(sym, "4h", limit=2)
                        if c4h and len(c4h) >= 2:
                            p0, p1 = float(c4h[-2][4]), float(c4h[-1][4])
                            if p0 > 0:
                                change_4h = (p1 - p0) / p0 * 100.0
                    except Exception:
                        pass

                    # 7D % and 30D % — single daily fetch (32 bars covers both)
                    change_7d: float | None  = None
                    change_30d: float | None = None
                    try:
                        c1d = ex.fetch_ohlcv(sym, "1d", limit=32)
                        if c1d:
                            last_close = float(c1d[-1][4])
                            if len(c1d) >= 8:
                                p7 = float(c1d[-8][4])
                                if p7 > 0:
                                    change_7d = (last_close - p7) / p7 * 100.0
                            if len(c1d) >= 31:
                                p30 = float(c1d[-31][4])
                                if p30 > 0:
                                    change_30d = (last_close - p30) / p30 * 100.0
                    except Exception:
                        pass

                    # 24H Volume resolution (priority order):
                    #   1. quoteVolume from live ticker (rolling 24h in USDT)
                    #   2. CoinGecko total_volume (USDT)
                    #   3. Sum of last 24 × 1h OHLCV bars converted to USDT
                    vol_24h: float | None = vol_24h_ticker
                    if vol_24h is None:
                        cg_vol = cg.get("volume_24h")
                        vol_24h = float(cg_vol) if cg_vol else None
                    if vol_24h is None:
                        # Fallback: sum base-currency volume for last 24 1h bars × close
                        try:
                            bars_24 = candles[-24:] if len(candles) >= 24 else candles
                            vol_24h = sum(float(b[5]) for b in bars_24) * close
                        except Exception:
                            vol_24h = None

                    self.row_ready.emit({
                        "symbol":      sym,
                        "base":        base,
                        "market_cap":  cg.get("market_cap", 0),
                        "price":       close,
                        "change_15m":  change_15m,
                        "change_1h":   change_1h,
                        "change_4h":   change_4h,
                        "change_24h":  change_24h,
                        "change_7d":   change_7d,
                        "change_30d":  change_30d,
                        "volume_24h":  vol_24h,
                        "rsi":         rsi,
                        "signal":      sig["signal"],
                        "strength":    sig["strength"],
                        "bullish":     sig.get("bullish", 0),
                        "bearish":     sig.get("bearish", 0),
                    })

                except Exception as exc:
                    logger.debug("Scanner skip %s: %s", sym, exc)

                done += 1
                self.msleep(ex.rateLimit // 4 if ex.rateLimit else 100)

            self.finished.emit(done)

        except Exception as exc:
            logger.error("ScannerWorker: %s", exc, exc_info=True)
            self.error.emit(str(exc))


# ─────────────────────────────────────────────────────────────
# IDSS: Watchlist editor widget
# ─────────────────────────────────────────────────────────────
class WatchlistEditorWidget(QWidget):
    """
    Compact watchlist editor — create/delete lists, add/remove symbols,
    enable/disable lists via checkboxes.
    """
    changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._mgr: Optional[object]  = None
        self._current_wl: str        = ""
        self._build()
        self._refresh()

    def _get_mgr(self):
        if self._mgr is None:
            try:
                from core.scanning.watchlist import WatchlistManager
                self._mgr = WatchlistManager()
            except Exception as exc:
                logger.warning("WatchlistManager unavailable: %s", exc)
        return self._mgr

    # ── build ──────────────────────────────────────────────
    def _build(self):
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)

        # Header row
        hdr = QHBoxLayout()
        lbl = QLabel("WATCHLISTS")
        lbl.setStyleSheet(_SECT_STYLE)
        hdr.addWidget(lbl)
        hdr.addStretch()

        self._new_btn = QPushButton("+ New")
        self._new_btn.setFixedSize(62, 28)
        self._new_btn.setStyleSheet(
            "QPushButton{background:#1A2D4A;color:#4488CC;border:1px solid #2A3A52;"
            "border-radius:4px;font-size:13px;font-weight:600;}"
            "QPushButton:hover{background:#1E3A5A;}"
        )
        self._new_btn.clicked.connect(self._new_watchlist)
        hdr.addWidget(self._new_btn)

        self._del_btn = QPushButton("✕ Delete")
        self._del_btn.setFixedSize(70, 28)
        self._del_btn.setStyleSheet(
            "QPushButton{background:#1A0A0A;color:#FF3355;border:1px solid #440011;"
            "border-radius:4px;font-size:13px;font-weight:600;}"
            "QPushButton:hover{background:#2A1010;}"
        )
        self._del_btn.clicked.connect(self._delete_watchlist)
        hdr.addWidget(self._del_btn)

        lay.addLayout(hdr)

        # Watchlist list
        self._wl_list = QListWidget()
        self._wl_list.setFixedHeight(110)
        self._wl_list.setStyleSheet(
            "QListWidget{background:#080C16;color:#E8EBF0;border:1px solid #1A2332;"
            "font-size:13px;}"
            "QListWidget::item{padding:5px 8px;}"
            "QListWidget::item:selected{background:#1A2D4A;color:#88CCFF;}"
        )
        self._wl_list.itemChanged.connect(self._on_wl_item_changed)
        self._wl_list.currentRowChanged.connect(self._on_wl_row_changed)
        lay.addWidget(self._wl_list)

        # Symbol section header
        hdr2 = QHBoxLayout()
        self._sym_hdr_lbl = QLabel("SYMBOLS")
        self._sym_hdr_lbl.setStyleSheet(_SECT_STYLE)
        hdr2.addWidget(self._sym_hdr_lbl)
        hdr2.addStretch()
        lay.addLayout(hdr2)

        # Fixed-pair checkboxes — same 4 pairs as Market Scanner
        _cb_style = (
            "QCheckBox { color:#C8D0E0; font-size:13px; spacing:6px; }"
            "QCheckBox::indicator { width:14px; height:14px; border:1px solid #2A3A52;"
            " border-radius:3px; background:#0F1623; }"
            "QCheckBox::indicator:checked { background:#1E90FF; border-color:#1E90FF; }"
        )
        _FIXED_PAIRS = ["BTC/USDT", "ETH/USDT", "BNB/USDT", "SOL/USDT", "XRP/USDT"]
        self._pair_checks: dict[str, QCheckBox] = {}

        pairs_frame = QWidget()
        pairs_frame.setStyleSheet(
            "QWidget{background:#080C16;border:1px solid #1A2332;border-radius:4px;}"
        )
        pairs_lay = QVBoxLayout(pairs_frame)
        pairs_lay.setContentsMargins(8, 8, 8, 8)
        pairs_lay.setSpacing(8)

        for sym in _FIXED_PAIRS:
            cb = QCheckBox(sym)
            cb.setChecked(True)
            cb.setStyleSheet(_cb_style)
            cb.stateChanged.connect(
                lambda state, s=sym: self._on_pair_toggled(s, state)
            )
            self._pair_checks[sym] = cb
            pairs_lay.addWidget(cb)

        lay.addWidget(pairs_frame)
        lay.addStretch()

    # ── internal ────────────────────────────────────────────
    def _refresh(self):
        mgr = self._get_mgr()
        if not mgr:
            return
        try:
            all_wl = mgr.get_all()
        except Exception:
            return

        self._wl_list.blockSignals(True)
        prev_name = self._current_wl
        self._wl_list.clear()

        first_item = None
        for name, data in all_wl.items():
            item = QListWidgetItem(name)
            item.setCheckState(
                Qt.Checked if data.get("enabled", True) else Qt.Unchecked
            )
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable | Qt.ItemIsEnabled)
            self._wl_list.addItem(item)
            if first_item is None:
                first_item = item

        self._wl_list.blockSignals(False)

        # Restore selection
        if prev_name:
            matches = self._wl_list.findItems(prev_name, Qt.MatchExactly)
            if matches:
                self._wl_list.setCurrentItem(matches[0])
                return
        if first_item:
            self._wl_list.setCurrentItem(first_item)

    def _refresh_symbols(self):
        """Sync checkbox states with the symbols in the active watchlist."""
        mgr = self._get_mgr()
        if not mgr or not self._current_wl:
            return
        try:
            wl = mgr.get_watchlist(self._current_wl)
            active = set(wl.get("symbols", [])) if wl else set()
        except Exception:
            active = set()

        for sym, cb in self._pair_checks.items():
            cb.blockSignals(True)
            cb.setChecked(sym in active)
            cb.blockSignals(False)

        n = len(active)
        self._sym_hdr_lbl.setText(f"SYMBOLS  ({n})" if n else "SYMBOLS")

    def _on_wl_row_changed(self, row: int):
        item = self._wl_list.item(row)
        self._current_wl = item.text() if item else ""
        self._refresh_symbols()

    def _on_wl_item_changed(self, item: QListWidgetItem):
        """Sync enabled/disabled state when checkbox is toggled."""
        mgr = self._get_mgr()
        if not mgr:
            return
        name    = item.text()
        enabled = item.checkState() == Qt.Checked
        try:
            mgr.set_enabled(name, enabled)
        except Exception as exc:
            logger.warning("WatchlistEditor: set_enabled failed: %s", exc)
        self.changed.emit()

    def _new_watchlist(self):
        name, ok = QInputDialog.getText(
            self, "New Watchlist", "Watchlist name:"
        )
        if not ok or not name.strip():
            return
        mgr = self._get_mgr()
        if mgr:
            try:
                mgr.create_watchlist(name.strip(), [])
            except Exception as exc:
                logger.warning("create_watchlist: %s", exc)
        self._refresh()
        self.changed.emit()

    def _delete_watchlist(self):
        if not self._current_wl:
            return
        mgr = self._get_mgr()
        if mgr:
            try:
                mgr.delete_watchlist(self._current_wl)
            except Exception as exc:
                logger.warning("delete_watchlist: %s", exc)
        self._current_wl = ""
        self._refresh()
        self._sym_list.clear()
        self.changed.emit()

    def _on_pair_toggled(self, sym: str, state: int):
        """Add or remove a fixed pair from the active watchlist when checkbox toggled."""
        if not self._current_wl:
            return
        mgr = self._get_mgr()
        if not mgr:
            return
        checked = (state == Qt.Checked)
        try:
            if checked:
                mgr.add_symbol(self._current_wl, sym)
            else:
                mgr.remove_symbol(self._current_wl, sym)
        except Exception as exc:
            logger.warning("pair_toggled (%s): %s", sym, exc)
        # Refresh the header count
        try:
            wl = mgr.get_watchlist(self._current_wl)
            n = len(wl.get("symbols", [])) if wl else 0
            self._sym_hdr_lbl.setText(f"SYMBOLS  ({n})" if n else "SYMBOLS")
        except Exception:
            pass
        self.changed.emit()


# ─────────────────────────────────────────────────────────────
# IDSS: Candidate table
# ─────────────────────────────────────────────────────────────
_IDSS_COLS = [
    "Symbol", "Side", "Regime", "Score",
    "Models", "Entry", "Stop", "Target", "R:R",
    "Est. Size", "Status", "Age",
]


class IDSSCandidateTable(QTableWidget):
    """
    Displays OrderCandidate dicts from the IDSS scanner.
    Row selection emits row_selected(dict) for the detail panel.
    """
    row_selected = Signal(dict)

    # Column indices (must stay in sync with _IDSS_COLS)
    _STATUS_COL = 10
    _AGE_COL    = 11

    def __init__(self, parent=None):
        super().__init__(0, len(_IDSS_COLS), parent)
        self._rows: list[dict] = []
        self._setup()

        # Tick every 60 s to keep the Age column current (just now → 1m ago → …)
        self._age_timer = QTimer(self)
        self._age_timer.setInterval(60_000)
        self._age_timer.timeout.connect(self._refresh_ages)
        self._age_timer.start()

    def _setup(self):
        self.setHorizontalHeaderLabels(_IDSS_COLS)
        hdr = self.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.Stretch)          # Symbol
        hdr.setSectionResizeMode(1, QHeaderView.ResizeToContents) # Side
        hdr.setSectionResizeMode(2, QHeaderView.ResizeToContents) # Regime
        hdr.setSectionResizeMode(3, QHeaderView.ResizeToContents) # Score
        hdr.setSectionResizeMode(4, QHeaderView.ResizeToContents) # Models
        hdr.setSectionResizeMode(self._STATUS_COL, QHeaderView.ResizeToContents) # Status
        for c in range(5, len(_IDSS_COLS)):
            hdr.setSectionResizeMode(c, QHeaderView.ResizeToContents)
        hdr.setMinimumSectionSize(60)

        self.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.setAlternatingRowColors(True)
        self.setSortingEnabled(True)
        self.verticalHeader().setVisible(False)
        self.setShowGrid(True)
        self.setStyleSheet(_TABLE_STYLE)
        self.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.currentCellChanged.connect(lambda row, _col, _prow, _pcol: self._on_row_changed(row))
        self.doubleClicked.connect(self._on_double_click)

    def load_candidates(self, candidates: list[dict]) -> None:
        self._rows = list(candidates)
        self.setSortingEnabled(False)
        self.setRowCount(0)
        self.setRowCount(len(candidates))

        for ri, c in enumerate(candidates):
            status     = c.get("status", "")
            is_approved= c.get("is_approved", False)
            no_data    = status in ("No data", "Stale data", "Filtered", "No signal",
                                    "Below threshold", "Scan error")
            has_prices = bool(c.get("entry_price"))  # risk-gate rejected still have prices
            dim        = "#4A6A8A"   # dimmed colour for no-signal rows

            side       = (c.get("side") or "").upper()
            if not side or side == "—":
                side_color = dim
            else:
                side_color = "#00CC77" if side == "BUY" else "#FF3355"

            regime     = c.get("regime", "")
            reg_label  = REGIME_LABELS.get(regime, regime.replace("_", " ").title()) if regime else "—"
            reg_color  = REGIME_COLORS.get(regime, "#8899AA") if regime else dim

            score      = c.get("score") or 0.0
            sc_color   = dim if no_data else _score_color(score)

            models     = c.get("models_fired", [])
            model_text = _model_tag_text(models) if models else "—"

            entry      = c.get("entry_price") or 0.0
            stop_p     = c.get("stop_loss_price") or 0.0
            tp         = c.get("take_profit_price") or 0.0
            rr         = c.get("risk_reward_ratio") or 0.0
            size       = c.get("position_size_usdt") or 0.0
            # Age: always shown for every row — uses generated_at (scan cycle timestamp)
            age        = _fmt_age(c.get("generated_at", ""))

            # ── Status cell ────────────────────────────────────
            if is_approved:
                status_text  = "✓ Approved"
                status_color = "#00CC77"
            elif status in ("No signal", "Below threshold", "No data",
                            "Stale data", "Filtered", "Scan error"):
                status_text  = f"✗ {status}"
                status_color = dim
            elif status and status not in ("pending", ""):
                # Risk-gate rejection — shorten if verbose (e.g. "EV gate: EV=0.03 < 0.05" → "EV gate")
                short = status.split(":")[0].strip() if ":" in status else status
                status_text  = f"✗ {short}"
                status_color = "#FFB300"  # amber — had a signal but gated
            else:
                status_text  = "—"
                status_color = dim

            sym_color  = "#8899AA" if no_data else "#E8EBF0"
            self.setItem(ri, 0, _colored_item(
                c.get("symbol", ""), sym_color, Qt.AlignLeft | Qt.AlignVCenter
            ))
            self.setItem(ri, 1, _colored_item(side or "—", side_color))
            self.setItem(ri, 2, _colored_item(reg_label, reg_color))
            self.setItem(ri, 3, _numeric_item(score, f"{score:.3f}" if (score and not no_data) else "—", sc_color))
            self.setItem(ri, 4, _colored_item(model_text, dim if no_data else "#8899AA"))
            self.setItem(ri, 5, _colored_item(
                "—" if not has_prices else (_fmt_price(entry) if entry else "market"),
                dim if not has_prices else "#E8EBF0"
            ))
            self.setItem(ri, 6, _colored_item(
                "—" if not has_prices else _fmt_price(stop_p),
                dim if not has_prices else "#FF3355"
            ))
            self.setItem(ri, 7, _colored_item(
                "—" if not has_prices else _fmt_price(tp),
                dim if not has_prices else "#00CC77"
            ))
            self.setItem(ri, 8, _colored_item(
                "—" if not has_prices else (f"{rr:.2f}×" if rr else "—"),
                dim if not has_prices else "#FFB300"
            ))
            self.setItem(ri, 9, _colored_item(
                "—" if not has_prices else (f"${size:.0f}" if size else "—"),
                dim
            ))
            self.setItem(ri, self._STATUS_COL, _colored_item(status_text, status_color))
            self.setItem(ri, self._AGE_COL,    _colored_item(age, dim))

        self.setSortingEnabled(True)

    def _refresh_ages(self) -> None:
        """
        Tick handler — updates the Age column in-place every 60 seconds so
        ALL rows show '1m ago', '2m ago', … instead of frozen 'just now'.
        This includes no-signal and filtered rows, confirming to the user
        that the scan data is recent.

        Works correctly even when the table is sorted (visual order ≠ _rows
        order): we read the symbol from column 0 of each visible row and do
        a dict lookup to find the corresponding generated_at timestamp.
        """
        if not self._rows:
            return
        # Build symbol → generated_at map for ALL rows (including no-signal)
        sym_to_ts: dict[str, str] = {
            r.get("symbol", ""): r.get("generated_at", "")
            for r in self._rows
            if r.get("generated_at")
        }
        if not sym_to_ts:
            return
        # Temporarily disable sorting so row indices are stable during update
        sorting_was_on = self.isSortingEnabled()
        self.setSortingEnabled(False)
        for vi in range(self.rowCount()):
            sym_item = self.item(vi, 0)
            if sym_item is None:
                continue
            ts = sym_to_ts.get(sym_item.text())
            if ts:
                age_item = self.item(vi, self._AGE_COL)
                if age_item:
                    age_item.setText(_fmt_age(ts))
        if sorting_was_on:
            self.setSortingEnabled(True)

    def load_scan_results(
        self,
        candidates: list[dict],
        symbol_progress: dict[str, tuple[str, float]],
    ) -> None:
        """
        Fallback loader used when scan_all_results is not available.
        Approved candidates first, then no-signal symbols from symbol_progress.
        """
        candidate_syms = {c.get("symbol") for c in candidates}
        all_rows = list(candidates)

        # Append non-signal symbols from symbol_progress
        for sym, (regime, score) in symbol_progress.items():
            if sym not in candidate_syms:
                all_rows.append({
                    "symbol":              sym,
                    "side":                "",
                    "regime":              regime,
                    "score":               score,
                    "models_fired":        [],
                    "entry_price":         None,
                    "stop_loss_price":     0.0,
                    "take_profit_price":   0.0,
                    "risk_reward_ratio":   0.0,
                    "position_size_usdt":  0.0,
                    "generated_at":        "",
                    "status":              "No signal",
                    "is_approved":         False,
                })

        self.load_candidates(all_rows)

    def _on_row_changed(self, row: int):
        if row < 0:
            return
        # When sorting is enabled, visual row index ≠ self._rows index.
        # Read the symbol from the actual table cell and match to _rows.
        sym_item = self.item(row, 0)
        if sym_item is None:
            return
        symbol = sym_item.text()
        for candidate in self._rows:
            if candidate.get("symbol") == symbol:
                self.row_selected.emit(candidate)
                return

    def _on_double_click(self, index):
        sym_item = self.item(index.row(), 0)
        if not sym_item:
            return
        symbol = sym_item.text()
        try:
            main = self.window()
            if hasattr(main, "_pages") and "chart_workspace" in main._pages:
                chart_page = main._pages["chart_workspace"]
                chart_page._symbol_combo.setCurrentText(symbol)
                main._navigate_to("chart_workspace")
        except Exception as exc:
            logger.debug("IDSSCandidateTable: chart nav failed: %s", exc)


# ─────────────────────────────────────────────────────────────
# IDSS Scanner Tab
# ─────────────────────────────────────────────────────────────
class IDSSScannerTab(QWidget):
    """
    Full IDSS AI Scanner panel:
      Left  — Scan controls + Watchlist editor
      Right — Stats bar + Candidate table + Rationale panel
    """
    # Internal signal to marshal exchange-ready callback to main thread.
    # Qt Signal.emit() is always thread-safe — the connected slot runs on
    # the receiver's thread (main thread for QWidget subclasses).
    _sig_exchange_ready = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._sig_exchange_ready.connect(self._start_scanner_now)
        self._idss              = None
        self._auto_running      = False
        self._last_scan_ts      = "—"
        self._candidate_history: list[dict] = []
        self._selected_candidate: dict | None = None   # currently highlighted row
        # Tracks (regime, best_score) for every symbol seen in the current scan
        self._sym_progress: dict[str, tuple[str, float]] = {}
        # Set to True once scan_all_results fires so the n==0 fallback in
        # _on_scan_finished doesn't also reload the table.
        self._scan_all_results_received: bool = False

        # ── Auto-execute state ──────────────────────────────
        # MANDATORY: Auto-execute is ALWAYS enabled on startup.
        # The user requires trades to execute automatically without manual toggling.
        # If config.yaml has auto_execute: false from a prior toggle-off, we override
        # it here and persist true so the config stays correct.
        try:
            from config.settings import settings as _s
            _cooldown = int(_s.get("scanner.auto_execute_cooldown_seconds", 30))
        except Exception:
            _cooldown = 30
        self._auto_execute_enabled: bool = True
        # Ensure config.yaml reflects the forced-on state so it persists correctly
        try:
            if not bool(_s.get("scanner.auto_execute", True)):
                _s.set("scanner.auto_execute", True)
                logger.info("IDSSScannerTab: forced auto_execute=true in config (was false)")
        except Exception:
            pass
        # Delegate all guard state to the pure-Python module (testable without Qt)
        from core.scanning.auto_execute_guard import AutoExecuteState
        self._ae_state = AutoExecuteState(cooldown_seconds=_cooldown)

        self._build()
        self._connect_scanner()

        # ── Auto-start scanner + auto-execute on launch ──────────
        # The user expects NexusTrader to begin scanning and executing
        # immediately on startup without manual toggling.
        #
        # The exchange may not be connected yet when __init__ runs.
        # Subscribe to EXCHANGE_CONNECTED so the scanner starts the
        # moment the exchange is ready — not before, not 15 seconds
        # later, but exactly when connectivity is established.
        # Set the UI state immediately so the user sees "Auto Running".
        self._auto_start_pending = False
        if self._idss:
            self._auto_running = True
            self._auto_start_pending = True
            self._auto_btn.setText("\u23f9  Stop Auto Scan")
            self._auto_btn.setStyleSheet(_BTN_STOP)
            self._status_lbl.setText(
                "Status: <b style='color:#1E90FF'>Waiting for exchange\u2026</b>"
            )

            # Subscribe to the exchange connected event
            try:
                from core.event_bus import bus, Topics
                bus.subscribe(Topics.EXCHANGE_CONNECTED, self._on_exchange_ready)
                logger.info("IDSSScannerTab: waiting for exchange connection to start auto-scan")
            except Exception as exc:
                logger.warning("IDSSScannerTab: could not subscribe to exchange event: %s — using 15s fallback", exc)
                from PySide6.QtCore import QTimer
                QTimer.singleShot(15_000, self._start_scanner_now)

        if self._auto_execute_enabled:
            self._exec_status_lbl.setText(
                "\u26a1 Auto-execute enabled \u2014 trades will be submitted automatically"
            )
            self._exec_status_lbl.setStyleSheet("color:#FFB300; font-size:13px;")
            logger.info("IDSSScannerTab: auto-execute ON at launch")

    # ── layout ─────────────────────────────────────────────
    def _build(self):
        root = QHBoxLayout(self)
        root.setContentsMargins(0, 8, 0, 0)
        root.setSpacing(0)

        # ── Left control panel (scrollable) ───────────────────
        left_scroll = QScrollArea()
        left_scroll.setFixedWidth(280)
        left_scroll.setWidgetResizable(True)
        left_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        left_scroll.setStyleSheet(
            "QScrollArea { background: transparent; border: none; }"
            "QScrollBar:vertical { background:#0A0E1A; width:6px; border:none; }"
            "QScrollBar::handle:vertical { background:#2A3A52; border-radius:3px; min-height:30px; }"
            "QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height:0; }"
        )
        left = QFrame()
        left.setObjectName("card")
        left.setStyleSheet(_CARD_STYLE)
        lv = QVBoxLayout(left)
        lv.setContentsMargins(12, 12, 12, 12)
        lv.setSpacing(10)
        left_scroll.setWidget(left)

        # Section: Scanner controls
        ctrl_lbl = QLabel("SCANNER CONTROLS")
        ctrl_lbl.setStyleSheet(_SECT_STYLE)
        lv.addWidget(ctrl_lbl)

        # Timeframe row
        tf_row = QHBoxLayout()
        tf_row.setSpacing(6)
        tf_lbl = QLabel("Timeframe:")
        tf_lbl.setStyleSheet(_LBL_STYLE)
        tf_row.addWidget(tf_lbl)
        self._tf_combo = QComboBox()
        self._tf_combo.addItems(["1h", "4h", "1d", "30m", "15m", "5m"])
        self._tf_combo.setFixedWidth(80)
        self._tf_combo.setStyleSheet(_COMBO_STYLE)
        self._tf_combo.currentTextChanged.connect(self._on_tf_changed)
        tf_row.addWidget(self._tf_combo)
        tf_row.addStretch()
        lv.addLayout(tf_row)

        # Capital row
        cap_row = QHBoxLayout()
        cap_row.setSpacing(6)
        cap_lbl = QLabel("Capital ($):")
        cap_lbl.setStyleSheet(_LBL_STYLE)
        cap_row.addWidget(cap_lbl)
        self._capital_input = QLineEdit(self._get_available_capital_str())
        self._capital_input.setFixedWidth(80)
        self._capital_input.setStyleSheet(
            "QLineEdit{background:#0F1623;color:#E8EBF0;border:1px solid #2A3A52;"
            "border-radius:4px;padding:2px 6px;font-size:13px;}"
        )
        self._capital_input.editingFinished.connect(self._on_capital_changed)
        cap_row.addWidget(self._capital_input)
        cap_row.addStretch()
        lv.addLayout(cap_row)

        # Scan Now button
        self._scan_now_btn = QPushButton("⚡  Scan Now")
        self._scan_now_btn.setFixedHeight(34)
        self._scan_now_btn.setStyleSheet(_BTN_PRIMARY)
        self._scan_now_btn.clicked.connect(self._scan_now)
        lv.addWidget(self._scan_now_btn)

        # Auto Scan toggle
        self._auto_btn = QPushButton("▶  Start Auto Scan")
        self._auto_btn.setFixedHeight(34)
        self._auto_btn.setStyleSheet(_BTN_SUCCESS)
        self._auto_btn.clicked.connect(self._toggle_auto)
        lv.addWidget(self._auto_btn)

        # Auto-Execute toggle
        ae_lbl = QLabel("AUTO-EXECUTE")
        ae_lbl.setStyleSheet(_SECT_STYLE)
        lv.addWidget(ae_lbl)

        _ae_on  = self._auto_execute_enabled
        self._auto_exec_btn = QPushButton(
            "⚡  Auto-Execute is ON" if _ae_on else "⚡  Auto-Execute is OFF"
        )
        self._auto_exec_btn.setFixedHeight(34)
        self._auto_exec_btn.setStyleSheet(_BTN_AUTO_ON if _ae_on else _BTN_AUTO_OFF)
        self._auto_exec_btn.setToolTip(
            "When ON, every approved IDSS candidate is automatically sent to\n"
            "Paper Trading after each scan cycle — no manual click needed.\n"
            "Safeguards: age ≤ 1×TF, position limit, duplicate, drawdown, cooldown."
        )
        self._auto_exec_btn.clicked.connect(self._toggle_auto_execute)
        lv.addWidget(self._auto_exec_btn)

        # Divider
        div = QFrame()
        div.setFrameShape(QFrame.HLine)
        div.setStyleSheet("QFrame{color:#1A2332;}")
        lv.addWidget(div)

        # Watchlist editor
        self._wl_editor = WatchlistEditorWidget()
        self._wl_editor.changed.connect(self._on_watchlist_changed)
        lv.addWidget(self._wl_editor, 1)

        root.addWidget(left_scroll)

        # Spacer
        root.addSpacing(8)

        # ── Right main panel ─────────────────────────────────
        right = QWidget()
        rv = QVBoxLayout(right)
        rv.setContentsMargins(0, 0, 0, 0)
        rv.setSpacing(6)

        # Stats bar
        stats = QFrame()
        stats.setObjectName("card")
        stats.setStyleSheet(_CARD_STYLE)
        stats.setMinimumHeight(42)
        sh = QHBoxLayout(stats)
        sh.setContentsMargins(14, 6, 14, 6)
        sh.setSpacing(20)

        self._status_lbl    = self._stat("Status",     "Idle",    "#8899AA")
        self._last_scan_lbl = self._stat("Last Scan",  "—",       "#8899AA")
        self._cands_lbl     = self._stat("Candidates", "0",       "#E8EBF0")
        self._scanning_lbl  = self._stat("Scanning",   "—",       "#4A6A8A")
        self._auto_exec_counter_lbl = self._stat(
            "Auto-Executed Today", "0", "#4A6A8A"
        )

        for w in [self._status_lbl, self._last_scan_lbl,
                  self._cands_lbl, self._scanning_lbl,
                  self._auto_exec_counter_lbl]:
            sh.addWidget(w)
        sh.addStretch()

        rv.addWidget(stats)

        # Progress bar
        self._progress = QProgressBar()
        self._progress.setFixedHeight(4)
        self._progress.setVisible(False)
        self._progress.setRange(0, 0)
        self._progress.setStyleSheet(
            "QProgressBar{background:#0F1623;border:none;border-radius:2px;}"
            "QProgressBar::chunk{background:#1E90FF;border-radius:2px;}"
        )
        rv.addWidget(self._progress)

        # Candidate table + rationale splitter
        splitter = QSplitter(Qt.Vertical)
        splitter.setHandleWidth(4)
        splitter.setStyleSheet(
            "QSplitter::handle{background:#1A2332;}"
            "QSplitter::handle:hover{background:#2A3A52;}"
        )

        self._table = IDSSCandidateTable()
        self._table.row_selected.connect(self._on_candidate_selected)
        splitter.addWidget(self._table)

        # Rationale panel
        rationale_frame = QFrame()
        rationale_frame.setObjectName("card")
        rationale_frame.setStyleSheet(_CARD_STYLE)
        rationale_frame.setMinimumHeight(90)
        rl = QVBoxLayout(rationale_frame)
        rl.setContentsMargins(10, 8, 10, 8)
        rl.setSpacing(4)

        rat_hdr = QLabel("RATIONALE")
        rat_hdr.setStyleSheet(_SECT_STYLE)
        rl.addWidget(rat_hdr)

        self._rationale_txt = QTextEdit()
        self._rationale_txt.setReadOnly(True)
        self._rationale_txt.setAcceptRichText(True)
        self._rationale_txt.setPlaceholderText(
            "Select a symbol row above to view full pipeline analysis…"
        )
        self._rationale_txt.setStyleSheet(
            "QTextEdit{background:#080C16;color:#C0D0E0;border:none;"
            "font-size:12px;}"
            "QScrollBar:vertical{width:6px;background:#0A0E1A;}"
            "QScrollBar::handle:vertical{background:#2A3A52;border-radius:3px;}"
        )
        rl.addWidget(self._rationale_txt, 1)

        # ── Execute to Paper button row ───────────────────────
        exec_row = QHBoxLayout(); exec_row.setSpacing(8)
        self._exec_paper_btn = QPushButton("▶  Execute to Paper Trading")
        self._exec_paper_btn.setFixedHeight(30)
        self._exec_paper_btn.setStyleSheet(
            "QPushButton { background:#0A2A1A; color:#00CC77; border:1px solid #004422; "
            "border-radius:5px; font-size:13px; font-weight:700; padding:0 16px; }"
            "QPushButton:hover { background:#0D3A22; }"
            "QPushButton:disabled { color:#1A3A28; border-color:#0A1A10; }"
        )
        self._exec_paper_btn.setEnabled(False)
        self._exec_paper_btn.clicked.connect(self._execute_to_paper)
        self._exec_status_lbl = QLabel("")
        self._exec_status_lbl.setStyleSheet("color:#8899AA; font-size:13px;")
        exec_row.addWidget(self._exec_paper_btn)
        exec_row.addWidget(self._exec_status_lbl, 1)
        rl.addLayout(exec_row)

        splitter.addWidget(rationale_frame)

        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 1)

        rv.addWidget(splitter, 1)
        root.addWidget(right, 1)

        # Placeholder when IDSS unavailable
        self._placeholder = QLabel(
            "⚠  IDSS scanner modules could not be loaded.\n"
            "Check that core/scanning/, core/regime/, core/signals/ and\n"
            "core/meta_decision/ are present and importable."
        )
        self._placeholder.setAlignment(Qt.AlignCenter)
        self._placeholder.setStyleSheet("color:#FF9800; font-size:13px;")
        self._placeholder.setVisible(False)

    @staticmethod
    def _stat(key: str, val: str, color: str) -> QLabel:
        lbl = QLabel(f"{key}: <b style='color:{color}'>{val}</b>")
        lbl.setStyleSheet("font-size:13px; color:#8899AA;")
        return lbl

    # ── auto-start on exchange connect ─────────────────────
    def _on_exchange_ready(self, event=None):
        """Called when EXCHANGE_CONNECTED fires. Starts auto-scan if pending.
        The event bus passes the Event object as the first positional arg.

        CRITICAL: This callback is invoked from a background thread
        (_StartupConnectThread). Qt timers inside AssetScanner.start() MUST
        be started from the main thread. We emit a Qt Signal (always
        thread-safe) which Qt automatically delivers to the main thread
        via its queued connection mechanism.
        """
        if not self._auto_start_pending:
            return
        self._auto_start_pending = False
        # Emit Qt Signal — guaranteed to deliver to main thread
        self._sig_exchange_ready.emit()

    def _start_scanner_now(self):
        """Schedule scanner start with an 8s GPU/RL init delay.

        Delays the first concurrent OHLCV prefetch by 8 seconds to allow
        FinBERT model loading and RL CUDA context initialisation to complete.
        Without this window, a race between ThreadPoolExecutor threads and
        GPU initialisation can cause a native-level process crash with no
        Python traceback (observed 2026-03-22, crashed 6s after startup).
        """
        if self._idss and self._auto_running:
            from PySide6.QtCore import QTimer as _QT
            _QT.singleShot(8_000, self._do_start_scanner)
            logger.info("IDSSScannerTab: auto-scan deferred 8s for GPU/RL init window")
            self._status_lbl.setText(
                "Status: <b style='color:#1E90FF'>Starting in 8s…</b>"
            )

    def _do_start_scanner(self):
        """Inner: called after 8s delay — actually starts the IDSS scanner."""
        if self._idss and self._auto_running:
            try:
                if not self._idss._running:
                    self._idss.start()
                logger.info("IDSSScannerTab: auto-scan started (exchange connected)")
                self._status_lbl.setText(
                    "Status: <b style='color:#1E90FF'>Auto Running</b>"
                )
            except Exception as exc:
                logger.warning("IDSSScannerTab: auto-scan start failed: %s", exc)

    # ── scanner wiring ─────────────────────────────────────
    def _connect_scanner(self):
        try:
            from core.scanning.scanner import scanner as _idss
            self._idss = _idss
            _idss.candidates_ready.connect(self._on_candidates_ready)
            _idss.confirmed_ready.connect(self._on_confirmed_ready)
            _idss.scan_all_results.connect(self._on_scan_all_results)
            _idss.scan_started.connect(self._on_scan_started)
            _idss.scan_finished.connect(self._on_scan_finished)
            _idss.scan_error.connect(self._on_scan_error)
            _idss.symbol_progress.connect(self._on_symbol_progress)
            logger.debug("IDSSScannerTab: connected to IDSS scanner singleton (dual-scan mode)")
        except Exception as exc:
            logger.warning("IDSS scanner unavailable: %s", exc)
            self._idss = None
            self._scan_now_btn.setEnabled(False)
            self._auto_btn.setEnabled(False)
            self._status_lbl.setText(
                "Status: <b style='color:#FF9800'>Unavailable</b>"
            )

    # ── scanner signals ─────────────────────────────────────
    @Slot()
    def _on_scan_started(self):
        self._sym_progress.clear()
        self._scan_all_results_received = False
        self._progress.setVisible(True)
        self._status_lbl.setText(
            "Status: <b style='color:#1E90FF'>Scanning…</b>"
        )

    @Slot(int)
    def _on_scan_finished(self, n: int):
        self._progress.setVisible(False)
        now = datetime.now().strftime("%H:%M:%S")
        self._last_scan_ts = now
        self._last_scan_lbl.setText(
            f"Last Scan: <b style='color:#8899AA'>{now}</b>"
        )
        self._status_lbl.setText(
            "Status: <b style='color:#00CC77'>Idle</b>"
        )
        self._scanning_lbl.setText(
            f"Scanning: <b style='color:#4A6A8A'>—</b>"
        )
        self._cands_lbl.setText(
            f"Candidates: <b style='color:#E8EBF0'>{n}</b>"
        )
        # scan_all_results fires after scan_complete so the table is populated
        # there.  The fallback below covers the case where the scanner is too
        # old to emit scan_all_results (e.g. tests that stub AssetScanner).
        if n == 0 and not getattr(self, "_scan_all_results_received", False):
            self._table.load_scan_results([], self._sym_progress)

    @Slot(list)
    def _on_scan_all_results(self, results: list):
        """Handle full per-symbol scan results (all symbols, all statuses).

        This supersedes the old load_scan_results(candidates, sym_progress)
        approach.  We receive one dict per symbol including the rejection
        reason, so the table always shows all 5 symbols with meaningful data.
        """
        self._scan_all_results_received = True
        # Sort: approved first, then by symbol name
        sorted_results = sorted(
            results,
            key=lambda r: (0 if r.get("is_approved") else 1, r.get("symbol", ""))
        )
        self._table.load_candidates(sorted_results)

    @Slot(list)
    def _on_candidates_ready(self, candidates: list):
        """Handle HTF approved candidates — store for auto-execute check only.

        UI display is now handled exclusively by _on_scan_all_results.
        This handler keeps _candidate_history up to date for the
        toggle-auto-execute immediate-fire logic.
        """
        self._candidate_history = list(candidates)
        # NOTE: Auto-execute is deliberately NOT called here.
        # candidates_ready = HTF (1H) approved candidates → display only.
        # confirmed_ready = LTF (15m) confirmed candidates → execution.

    @Slot(list)
    def _on_confirmed_ready(self, confirmed_candidates: list):
        """Handle LTF-confirmed candidates — execution pathway.

        This is the ONLY path through which auto-execution can occur.
        Candidates arriving here have been:
          1. Generated by the 1H signal pipeline
          2. Approved by RiskGate
          3. Staged in CandidateStore as CREATED
          4. Confirmed by the 15m LTF scan (EMA + RSI + Volume)

        After execution, the candidate is marked EXECUTED in the store.
        """
        if not confirmed_candidates:
            return

        logger.info(
            "IDSSScannerTab: %d LTF-confirmed candidate(s) received for execution",
            len(confirmed_candidates),
        )

        if self._auto_execute_enabled:
            self._try_auto_execute(confirmed_candidates)
        else:
            logger.info(
                "IDSSScannerTab: auto-execute OFF — %d confirmed candidate(s) waiting",
                len(confirmed_candidates),
            )

    @Slot(str)
    def _on_scan_error(self, err: str):
        self._progress.setVisible(False)
        self._status_lbl.setText(
            f"Status: <b style='color:#FF3355'>Error</b>"
        )
        logger.error("IDSS scanner error: %s", err)

    @Slot(str, str, float)
    def _on_symbol_progress(self, symbol: str, regime: str, score: float):
        # Accumulate: keep the regime and the highest score seen for this symbol
        prev_score = self._sym_progress.get(symbol, ("", 0.0))[1]
        self._sym_progress[symbol] = (regime, max(score, prev_score))

        regime_label = REGIME_LABELS.get(regime, regime)
        regime_color = REGIME_COLORS.get(regime, "#8899AA")
        scan_text = (
            f"<b style='color:#E8EBF0'>{symbol}</b> "
            f"<span style='color:{regime_color}'>({regime_label})</span>"
        )
        self._scanning_lbl.setText(f"Scanning: {scan_text}")

    # ── candidate selection ─────────────────────────────────
    @Slot(dict)
    def _on_candidate_selected(self, candidate: dict):
        # Build and display rich HTML rationale
        try:
            html = _build_rationale_html(candidate)
            self._rationale_txt.setHtml(html)
        except Exception as _exc:
            logger.warning("IDSSScannerTab: rationale HTML build failed: %s", _exc)
            # Fall back to plain text
            sym   = candidate.get("symbol", "?")
            score = candidate.get("score", 0.0)
            self._rationale_txt.setPlainText(
                f"{sym}  score={score:.3f}  status={candidate.get('status', '?')}"
            )

        # Track selected candidate and enable/disable paper execute button
        no_signal = candidate.get("_no_signal", False)
        models    = candidate.get("models_fired", [])
        self._selected_candidate = None if no_signal else candidate
        has_signal = (not no_signal) and bool(models)
        self._exec_paper_btn.setEnabled(has_signal)
        self._exec_status_lbl.setText(
            "" if has_signal else
            "Select a row with an active signal to enable paper execution"
        )

    def _execute_to_paper(self):
        """Send the currently-selected candidate to the paper executor."""
        c = self._selected_candidate
        if not c:
            return
        try:
            from core.meta_decision.order_candidate import OrderCandidate
            from core.execution.order_router import order_router
            from core.market_data.exchange_manager import exchange_manager
            from datetime import datetime, timedelta

            sym = c.get("symbol", "BTC/USDT")
            model_entry = c.get("entry_price") or 0.0
            stop  = c.get("stop_loss_price", 0.0)
            tp    = c.get("take_profit_price", 0.0)
            size  = c.get("position_size_usdt", 40.0)

            # Fetch current market price for market orders (same logic as
            # _do_auto_execute_one) — model entry_price includes ATR buffer
            # intended for limit orders, not market fills.
            market_price = 0.0
            try:
                ticker = exchange_manager.fetch_ticker(sym)
                if ticker:
                    market_price = float(ticker.get("last") or 0.0)
            except Exception as exc:
                logger.debug("Execute to paper: ticker fetch failed for %s: %s", sym, exc)

            entry = market_price if market_price > 0 else model_entry

            candidate = OrderCandidate(
                symbol             = sym,
                side               = c.get("side", "buy"),
                entry_type         = "market",
                entry_price        = entry if entry else None,
                stop_loss_price    = stop,
                take_profit_price  = tp,
                position_size_usdt = size,
                score              = c.get("score", 0.6),
                models_fired       = c.get("models_fired", []),
                regime             = c.get("regime", "unknown"),
                rationale          = c.get("rationale", "Manual paper execution from scanner"),
                timeframe          = c.get("timeframe", "1h"),
                atr_value          = c.get("atr_value", 0.0),
                approved           = True,   # manually approved by user
                expiry             = datetime.utcnow() + timedelta(hours=4),
            )
            # Forward SymbolAllocator fields so paper_executor can log them
            candidate.symbol_weight  = float(c.get("symbol_weight",  1.0) or 1.0)
            candidate.adjusted_score = float(c.get("adjusted_score", c.get("score", 0.6)) or 0.6)
            ok = order_router.submit(candidate)
            if ok:
                self._exec_status_lbl.setText(
                    f"✓ {c.get('symbol')} {c.get('side','').upper()} sent to paper trading"
                )
                self._exec_status_lbl.setStyleSheet("color:#00CC77; font-size:13px;")
                self._exec_paper_btn.setEnabled(False)
            else:
                self._exec_status_lbl.setText("⚠ Submission rejected — check position limits")
                self._exec_status_lbl.setStyleSheet("color:#FF9800; font-size:13px;")
        except Exception as exc:
            logger.error("Execute to paper: %s", exc, exc_info=True)
            self._exec_status_lbl.setText(f"Error: {exc}")
            self._exec_status_lbl.setStyleSheet("color:#FF3355; font-size:13px;")

    # ── auto-execute ─────────────────────────────────────────

    def _toggle_auto_execute(self):
        """Toggle the auto-execute flag, update the button UI, and persist to settings."""
        self._auto_execute_enabled = not self._auto_execute_enabled
        # Persist immediately so the state survives app restart
        try:
            from config.settings import settings as _s
            _s.set("scanner.auto_execute", self._auto_execute_enabled)
        except Exception as exc:
            logger.warning("Auto-execute: could not persist setting: %s", exc)

        if self._auto_execute_enabled:
            self._auto_exec_btn.setText("⚡  Auto-Execute is ON")
            self._auto_exec_btn.setStyleSheet(_BTN_AUTO_ON)
            self._exec_status_lbl.setText("⚡ Auto-execute enabled — trades will be submitted automatically")
            self._exec_status_lbl.setStyleSheet("color:#FFB300; font-size:13px;")
            logger.info("IDSSScannerTab: auto-execute ENABLED")
            # Auto-execute is useless without the scan timer running.
            # If the user enables auto-execute but hasn't started auto-scan,
            # start it automatically.  This prevents the common scenario where
            # the user toggles auto-execute ON, clicks "Scan Now" once, and
            # then the scanner never fires again because the recurring timer
            # was never started.
            if not self._auto_running and self._idss:
                try:
                    self._idss.start()
                    self._auto_running = True
                    self._auto_btn.setText("⏹  Stop Auto Scan")
                    self._auto_btn.setStyleSheet(_BTN_STOP)
                    self._status_lbl.setText(
                        "Status: <b style='color:#1E90FF'>Auto Running</b>"
                    )
                    logger.info("IDSSScannerTab: auto-scan started implicitly (auto-execute requires it)")
                except Exception as exc:
                    logger.warning("IDSSScannerTab: could not auto-start scanner: %s", exc)
            # When toggling ON mid-session, check for any CONFIRMED candidates
            # in the CandidateStore that haven't been executed yet.
            try:
                from core.scanning.candidate_store import get_candidate_store
                store = get_candidate_store()
                confirmed = store.get_confirmed()
                if confirmed:
                    # Build candidate dicts for the execution pipeline
                    ready = []
                    for sc in confirmed:
                        enriched = dict(sc.raw_candidate_dict)
                        enriched["ltf_confirmed"] = True
                        enriched["ltf_confirmation_price"] = sc.ltf_confirmation_price
                        enriched["staged_candidate_id"] = sc.candidate_id
                        ready.append(enriched)
                    logger.info(
                        "IDSSScannerTab: auto-execute ON — %d CONFIRMED candidate(s) ready",
                        len(ready),
                    )
                    self._try_auto_execute(ready)
                else:
                    logger.info("IDSSScannerTab: auto-execute ON — no CONFIRMED candidates waiting")
            except Exception as exc:
                logger.warning("IDSSScannerTab: could not check CandidateStore: %s", exc)
        else:
            self._auto_exec_btn.setText("⚡  Auto-Execute is OFF")
            self._auto_exec_btn.setStyleSheet(_BTN_AUTO_OFF)
            self._exec_status_lbl.setText("Auto-execute disabled — use ▶ Execute to Paper Trading to execute manually")
            self._exec_status_lbl.setStyleSheet("color:#8899AA; font-size:13px;")
            logger.info("IDSSScannerTab: auto-execute DISABLED")

    def _try_auto_execute(self, candidates: list):
        """
        Evaluate each approved candidate and auto-submit those that pass all
        safeguards.  Called from _on_candidates_ready — always on main thread.
        All guard logic lives in core/scanning/auto_execute_guard.py (Qt-free).
        """
        from core.scanning.auto_execute_guard import run_batch as _run_batch
        from core.execution.order_router import order_router as _router
        from config.settings import settings as _s

        # Roll over daily counter if needed
        self._ae_state.reset_if_new_day()

        # Read live portfolio state once for the whole batch
        try:
            _executor      = _router.active_executor
            open_positions = _executor.get_open_positions()
            drawdown_pct   = _executor.drawdown_pct
            max_dd         = float(_s.get("risk.max_portfolio_drawdown_pct", 15.0))
            max_pos        = int(_s.get("risk.max_concurrent_positions", 3))
        except Exception as exc:
            logger.error("Auto-execute: could not read portfolio state: %s", exc)
            return

        tf = self._tf_combo.currentText() if hasattr(self, "_tf_combo") else "1h"

        # run_batch does all guard checks and records executions in _ae_state
        to_execute = _run_batch(
            candidates     = candidates,
            timeframe      = tf,
            open_positions = open_positions,
            drawdown_pct   = drawdown_pct,
            max_dd_pct     = max_dd,
            max_pos        = max_pos,
            state          = self._ae_state,
        )

        for c in to_execute:
            self._do_auto_execute_one(c)
            self._update_auto_exec_counter()

    def _do_auto_execute_one(self, c: dict) -> bool:
        """
        Build an OrderCandidate from candidate dict *c* and submit it via
        order_router.  Returns True if the submission succeeded.
        Reuses the same OrderCandidate construction as _execute_to_paper so
        both paths stay in sync.

        For market orders the entry price is fetched from the exchange ticker
        (last traded price) rather than using the model's ATR-buffered entry.
        The ATR buffer is designed for limit orders — using it for market
        orders inflates the entry by ~0.2–0.4 ATR, creating an immediate
        unrealised loss against the actual market price.
        """
        sym = c.get("symbol", "?")
        try:
            from core.meta_decision.order_candidate import OrderCandidate
            from core.execution.order_router import order_router
            from core.market_data.exchange_manager import exchange_manager
            from datetime import timedelta

            model_entry = c.get("entry_price") or 0.0   # ATR-buffered (for reference)
            stop  = c.get("stop_loss_price", 0.0)
            tp    = c.get("take_profit_price", 0.0)
            size  = c.get("position_size_usdt", 40.0)

            # ── Fetch current market price for market-order fill ───────
            # The model's entry_price includes an ATR buffer intended for
            # limit orders.  For market orders we use the exchange's last
            # traded price so the paper position starts near the real fill.
            market_price = 0.0
            try:
                ticker = exchange_manager.fetch_ticker(sym)
                if ticker:
                    market_price = float(ticker.get("last") or 0.0)
            except Exception as exc:
                logger.debug("Auto-execute: ticker fetch failed for %s: %s", sym, exc)

            # Use market price when available; fall back to model entry
            entry = market_price if market_price > 0 else model_entry
            if market_price > 0 and model_entry > 0:
                diff_pct = abs(entry - model_entry) / model_entry * 100
                logger.info(
                    "Auto-execute: %s market price %.4f vs model entry %.4f (Δ%.2f%%)",
                    sym, market_price, model_entry, diff_pct,
                )

            candidate = OrderCandidate(
                symbol             = sym,
                side               = c.get("side", "buy"),
                entry_type         = "market",
                entry_price        = entry if entry else None,
                stop_loss_price    = stop,
                take_profit_price  = tp,
                position_size_usdt = size,
                score              = c.get("score", 0.6),
                models_fired       = c.get("models_fired", []),
                regime             = c.get("regime", "unknown"),
                rationale          = c.get("rationale", "Auto-executed by IDSS scanner"),
                timeframe          = c.get("timeframe", "1h"),
                atr_value          = c.get("atr_value", 0.0),
                approved           = True,
                expiry             = datetime.utcnow() + timedelta(hours=4),
            )
            # Forward SymbolAllocator fields so paper_executor can log them
            candidate.symbol_weight  = float(c.get("symbol_weight",  1.0) or 1.0)
            candidate.adjusted_score = float(c.get("adjusted_score", c.get("score", 0.6)) or 0.6)
            ok = order_router.submit(candidate)
            if ok:
                price_str = f"{entry:,.4f}" if entry else "market"
                msg = (
                    f"✅ Auto-executed: {sym} {c.get('side','').upper()} @ {price_str}"
                )
                self._exec_status_lbl.setText(msg)
                self._exec_status_lbl.setStyleSheet("color:#00CC77; font-size:13px;")
                logger.info("Auto-execute: submitted %s %s @ %s", sym, c.get("side"), price_str)
                self._flash_exec_row(sym)

                # Mark the staged candidate as EXECUTED in the CandidateStore
                staged_id = c.get("staged_candidate_id")
                if staged_id:
                    try:
                        from core.scanning.candidate_store import get_candidate_store
                        get_candidate_store().mark_executed(staged_id)
                        logger.info("Auto-execute: marked staged candidate %s as EXECUTED", staged_id)
                    except Exception as exc:
                        logger.warning("Auto-execute: could not mark %s as EXECUTED: %s", staged_id, exc)
            else:
                logger.warning("Auto-execute: order_router rejected %s", sym)
            return ok
        except Exception as exc:
            logger.error("Auto-execute: exception for %s: %s", sym, exc, exc_info=True)
            return False

    def _flash_exec_row(self, symbol: str):
        """Briefly highlight the row for *symbol* in bright green to give visual feedback."""
        try:
            table = self._table
            for row in range(table.rowCount()):
                item = table.item(row, 0)   # Symbol column
                if item and item.text().strip() == symbol:
                    for col in range(table.columnCount()):
                        cell = table.item(row, col)
                        if cell:
                            cell.setBackground(QColor("#003322"))
                    # Restore default background after 1.5 seconds
                    from PySide6.QtCore import QTimer
                    QTimer.singleShot(1500, lambda r=row: self._restore_row_bg(r))
                    break
        except Exception as exc:
            logger.debug("_flash_exec_row: %s", exc)

    def _restore_row_bg(self, row: int):
        """Restore table row to default background after flash."""
        try:
            default_bg = QColor("#0A0E1A")
            for col in range(self._table.columnCount()):
                cell = self._table.item(row, col)
                if cell:
                    cell.setBackground(default_bg)
        except Exception:
            pass

    def _update_auto_exec_counter(self):
        """Refresh the Auto-Executed Today label in the stats bar."""
        count = self._ae_state.today_count
        color = "#FFB300" if count > 0 else "#4A6A8A"
        self._auto_exec_counter_lbl.setText(
            f"Auto-Executed Today: <b style='color:{color}'>{count}</b>"
        )

    # ── controls ─────────────────────────────────────────────
    def _on_tf_changed(self, tf: str):
        if self._idss:
            try:
                self._idss.set_timeframe(tf)
            except Exception as exc:
                logger.debug("set_timeframe: %s", exc)

    # ── Capital helpers ─────────────────────────────────────

    @staticmethod
    def _get_available_capital_str() -> str:
        """Return the paper executor's available capital as an integer string.
        Falls back to '500' if the executor is not yet initialised."""
        try:
            from core.execution.paper_executor import paper_executor as _pe
            val = _pe.available_capital
            if val and val > 0:
                return str(int(val))
        except Exception:
            pass
        return "500"

    def showEvent(self, event):
        """Refresh the Capital field with current available capital each time the page is shown."""
        super().showEvent(event)
        current = self._capital_input.text().strip()
        try:
            # Only auto-update if the user hasn't manually typed a custom value
            # that differs from what live capital would show. We refresh whenever
            # the current value looks like it was previously auto-set (i.e. it
            # matches what the executor would give today or is still the 500 default).
            live = self._get_available_capital_str()
            if current in ("500", "", live):
                self._capital_input.setText(live)
                self._on_capital_changed()
        except Exception:
            pass

    def _on_capital_changed(self):
        val = self._capital_input.text().strip()
        try:
            from config.settings import settings
            settings.set("scanner.capital_usdt", float(val))
        except Exception:
            pass

    def _scan_now(self):
        if self._idss:
            try:
                self._idss.scan_now()
            except Exception as exc:
                logger.error("scan_now: %s", exc)
        else:
            self._status_lbl.setText(
                "Status: <b style='color:#FF9800'>Not available</b>"
            )

    def _toggle_auto(self):
        if not self._idss:
            return
        if self._auto_running:
            try:
                self._idss.stop()
            except Exception as exc:
                logger.error("scanner.stop: %s", exc)
            self._auto_running = False
            self._auto_btn.setText("▶  Start Auto Scan")
            self._auto_btn.setStyleSheet(_BTN_SUCCESS)
            self._status_lbl.setText(
                "Status: <b style='color:#8899AA'>Idle</b>"
            )
        else:
            try:
                self._idss.start()
            except Exception as exc:
                logger.error("scanner.start: %s", exc)
                return
            self._auto_running = True
            self._auto_btn.setText("⏹  Stop Auto Scan")
            self._auto_btn.setStyleSheet(_BTN_STOP)
            self._status_lbl.setText(
                "Status: <b style='color:#1E90FF'>Auto Running</b>"
            )

    def _on_watchlist_changed(self):
        logger.debug("Watchlist changed — triggering immediate scan")
        if self._idss and self._auto_running:
            try:
                self._idss.scan_now()
            except Exception:
                pass



# ─────────────────────────────────────────────────────────────
# Market Scanner Page  (v3 — hosts both tabs)
# ─────────────────────────────────────────────────────────────
class MarketScannerPage(QWidget):
    """
    Market Scanner page with two tabs:
      1. Market Scanner — technical-indicator scan enriched with CoinGecko data
      2. IDSS AI Scanner — IDSS pipeline: regime classification, confluence scoring,
                           order candidates with watchlist management
    """

    # Tab-1 columns (unchanged from v2)
    COLUMNS = [
        "Symbol", "Mkt Cap", "Price",
        "15M %", "1H %", "4H %", "24H %", "7D %", "30D %",
        "24H Volume", "RSI",
        "Signal", "Strength", "Bull", "Bear",
    ]

    # Thread-safe signal — carries (exchange_name, connected) from background thread
    _sig_exchange_state = Signal(str, bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._worker:    Optional[ScannerWorker]   = None
        self._cg_worker: Optional[CoinGeckoWorker] = None
        self._cg_data:   dict                      = {}
        self._all_rows:  list[dict]                = []
        self._build()
        self._subscribe_exchange()

    # ── outer layout ────────────────────────────────────────
    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        root.addWidget(PageHeader(
            "Market Scanner",
            "Technical indicator scan  ·  IDSS AI signals with regime labels and confluence scores"
        ))

        body = QWidget()
        bv = QVBoxLayout(body)
        bv.setContentsMargins(16, 8, 16, 12)
        bv.setSpacing(0)

        self._tabs = QTabWidget()
        self._tabs.setStyleSheet(_TAB_STYLE)

        # Tab 1: classic market scanner
        tab1 = self._build_market_scanner_tab()
        self._tabs.addTab(tab1, "  ⊡  Market Scanner  ")

        # Tab 2: IDSS scanner
        self._idss_tab = IDSSScannerTab()
        self._tabs.addTab(self._idss_tab, "  ◈  IDSS AI Scanner  ")

        # (Intelligence Agents moved to DASHBOARDS → Intelligence Agents page)

        bv.addWidget(self._tabs, 1)
        root.addWidget(body, 1)

    # ── Tab 1: market scanner ────────────────────────────────
    def _build_market_scanner_tab(self) -> QWidget:
        tab = QWidget()
        tv = QVBoxLayout(tab)
        tv.setContentsMargins(0, 8, 0, 0)
        tv.setSpacing(8)

        tv.addWidget(self._build_toolbar())

        self._progress = QProgressBar()
        self._progress.setFixedHeight(6)
        self._progress.setVisible(False)
        self._progress.setStyleSheet(
            "QProgressBar { background:#0F1623; border:none; border-radius:3px; }"
            "QProgressBar::chunk { background:#1E90FF; border-radius:3px; }"
        )
        tv.addWidget(self._progress)

        tv.addWidget(self._build_summary_bar())

        self._table = QTableWidget(0, len(self.COLUMNS))
        self._table.setHorizontalHeaderLabels(self.COLUMNS)
        hdr = self._table.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.Stretch)
        for c in range(1, len(self.COLUMNS)):
            hdr.setSectionResizeMode(c, QHeaderView.ResizeToContents)
        hdr.setMinimumSectionSize(70)
        self._table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._table.setAlternatingRowColors(True)
        self._table.setSortingEnabled(True)
        self._table.verticalHeader().setVisible(False)
        self._table.setShowGrid(True)
        self._table.setStyleSheet(_TABLE_STYLE)
        self._table.doubleClicked.connect(self._on_row_double_clicked)
        tv.addWidget(self._table, 1)

        return tab

    # Fixed pairs available in the scanner (always USDT quote)
    _SCAN_PAIRS = ["BTC/USDT", "ETH/USDT", "BNB/USDT", "SOL/USDT", "XRP/USDT"]

    def _build_toolbar(self) -> QFrame:
        bar = QFrame()
        bar.setObjectName("card")
        outer = QVBoxLayout(bar)
        outer.setContentsMargins(14, 10, 14, 10)
        outer.setSpacing(8)

        row1 = QHBoxLayout()
        row1.setSpacing(10)

        def _lbl(text: str) -> QLabel:
            l = QLabel(text)
            l.setStyleSheet(_LBL_STYLE)
            return l

        # ── Pair selection checkboxes (USDT pairs only) ───────
        row1.addWidget(_lbl("Pairs:"))
        _cb_style = (
            "QCheckBox { color:#C8D0E0; font-size:13px; spacing:5px; }"
            "QCheckBox::indicator { width:14px; height:14px; border:1px solid #2A3A52;"
            " border-radius:3px; background:#0F1623; }"
            "QCheckBox::indicator:checked { background:#1E90FF; border-color:#1E90FF; }"
        )
        self._pair_checks: dict[str, QCheckBox] = {}
        for sym in self._SCAN_PAIRS:
            cb = QCheckBox(sym.split("/")[0])   # show "BTC" not "BTC/USDT"
            cb.setChecked(True)
            cb.setStyleSheet(_cb_style)
            cb.stateChanged.connect(self._apply_filter)
            self._pair_checks[sym] = cb
            row1.addWidget(cb)

        row1.addSpacing(16)
        row1.addWidget(_lbl("Timeframe:"))
        self._tf_combo = QComboBox()
        self._tf_combo.addItems(["1h", "4h", "1d", "15m", "5m"])
        self._tf_combo.setFixedWidth(80)
        self._tf_combo.setStyleSheet(_COMBO_STYLE)
        row1.addWidget(self._tf_combo)

        row1.addSpacing(4)
        row1.addWidget(_lbl("Signal:"))
        self._signal_filter = QComboBox()
        self._signal_filter.addItems(["All", "Bullish", "Bearish", "Neutral"])
        self._signal_filter.setFixedWidth(100)
        self._signal_filter.setStyleSheet(_COMBO_STYLE)
        self._signal_filter.currentTextChanged.connect(self._apply_filter)
        row1.addWidget(self._signal_filter)

        row1.addStretch(1)
        outer.addLayout(row1)

        row2 = QHBoxLayout()
        row2.setSpacing(10)

        self._scan_btn = QPushButton("🔍  Scan Market")
        self._scan_btn.setFixedHeight(34)
        self._scan_btn.setMinimumWidth(140)
        self._scan_btn.setStyleSheet(_BTN_PRIMARY)
        self._scan_btn.clicked.connect(self._start_scan)
        row2.addWidget(self._scan_btn)

        self._stop_btn = QPushButton("■  Stop")
        self._stop_btn.setFixedHeight(34)
        self._stop_btn.setMinimumWidth(80)
        self._stop_btn.setEnabled(False)
        self._stop_btn.setStyleSheet(_BTN_STOP)
        self._stop_btn.clicked.connect(self._stop_scan)
        row2.addWidget(self._stop_btn)

        self._connect_btn = QPushButton("⚡  Connect Exchange")
        self._connect_btn.setFixedHeight(34)
        self._connect_btn.setMinimumWidth(150)
        self._connect_btn.setStyleSheet(
            "QPushButton { background:#2D1B00; color:#F6AD55; border:1px solid #7B4A00; "
            "border-radius:4px; font-size:13px; font-weight:600; padding:0 12px; }"
            "QPushButton:hover { background:#3D2800; border-color:#F6AD55; }"
            "QPushButton:disabled { color:#5A4020; border-color:#3D2800; }"
        )
        self._connect_btn.clicked.connect(self._connect_exchange)
        self._connect_btn.setVisible(True)   # hidden once exchange connects
        row2.addWidget(self._connect_btn)

        row2.addStretch(1)

        self._status_lbl = QLabel("Checking exchange connection…")
        self._status_lbl.setStyleSheet("color:#5A7A9A; font-size:13px;")
        row2.addWidget(self._status_lbl)

        outer.addLayout(row2)
        return bar

    def _build_summary_bar(self) -> QFrame:
        bar = QFrame()
        bar.setObjectName("card")
        bar.setFixedHeight(46)
        h = QHBoxLayout(bar)
        h.setContentsMargins(16, 8, 16, 8)
        h.setSpacing(24)

        self._total_lbl   = self._sum_lbl("Total",        "0")
        self._bull_lbl    = self._sum_lbl("Bullish",      "0", "#00CC77")
        self._bear_lbl    = self._sum_lbl("Bearish",      "0", "#FF3355")
        self._neutral_lbl = self._sum_lbl("Neutral",      "0", "#8899AA")
        self._top_lbl     = self._sum_lbl("Top Signal",   "—")
        self._cg_lbl      = self._sum_lbl("Market Data",  "—", "#4488CC")

        for w in [self._total_lbl, self._bull_lbl, self._bear_lbl,
                  self._neutral_lbl, self._top_lbl, self._cg_lbl]:
            h.addWidget(w)
        h.addStretch()
        return bar

    @staticmethod
    def _sum_lbl(key: str, val: str, color: str = "#E8EBF0") -> QLabel:
        l = QLabel(f"{key}: <b style='color:{color}'>{val}</b>")
        l.setStyleSheet("font-size:13px; color:#8899AA;")
        return l

    # ── Exchange connectivity ─────────────────────────────────

    def _subscribe_exchange(self):
        """Subscribe to EXCHANGE_CONNECTED so the scanner auto-refreshes on connect."""
        from core.event_bus import bus, Topics
        # Wire the Qt signal so UI update is always on the main thread
        self._sig_exchange_state.connect(self._on_exchange_state)
        bus.subscribe(Topics.EXCHANGE_CONNECTED, self._on_exchange_event)
        bus.subscribe(Topics.EXCHANGE_ERROR,     self._on_exchange_event_error)
        # Check current connection state immediately (exchange may already be up)
        self._check_exchange_status()

    def _on_exchange_event(self, event):
        """Called from EventBus (may be background thread) — emit Qt signal."""
        data = event.data or {}
        self._sig_exchange_state.emit(
            data.get("name", "Exchange"),
            data.get("connected", False),
        )

    def _on_exchange_event_error(self, event):
        self._sig_exchange_state.emit("Exchange", False)

    @Slot(str, bool)
    def _on_exchange_state(self, name: str, connected: bool):
        """Qt slot — always runs on main thread."""
        if connected:
            self._connect_btn.setVisible(False)
            self._refresh_quote_combo()
            self._status_lbl.setText(f"✔  {name} connected — ready to scan")
            self._status_lbl.setStyleSheet("color:#00CC77; font-size:13px;")
        else:
            self._connect_btn.setVisible(True)
            self._status_lbl.setText(f"⚠  {name} disconnected — connect in Exchange Management")
            self._status_lbl.setStyleSheet("color:#FF6644; font-size:13px;")

    def _check_exchange_status(self):
        """Poll current state once on page load (exchange may already be connected)."""
        try:
            from core.market_data.exchange_manager import exchange_manager
            if exchange_manager.is_connected():
                self._connect_btn.setVisible(False)
                self._refresh_quote_combo()
                self._status_lbl.setText("✔  Exchange connected — ready to scan")
                self._status_lbl.setStyleSheet("color:#00CC77; font-size:13px;")
            else:
                self._connect_btn.setVisible(True)
        except Exception:
            pass

    def _refresh_quote_combo(self):
        """No-op — quote is now always USDT (hardcoded pair list)."""
        pass

    def _connect_exchange(self):
        """Trigger background exchange connect (same as startup thread)."""
        import threading
        self._connect_btn.setEnabled(False)
        self._status_lbl.setText("🔄  Connecting to exchange…")
        self._status_lbl.setStyleSheet("color:#F6AD55; font-size:13px;")

        def _do_connect():
            try:
                from core.market_data.exchange_manager import exchange_manager
                exchange_manager.load_active_exchange()
            except Exception as exc:
                logger.warning("Scanner reconnect failed: %s", exc)
            finally:
                # Re-enable the button regardless of outcome
                try:
                    self._connect_btn.setEnabled(True)
                except Exception:
                    pass

        threading.Thread(target=_do_connect, daemon=True).start()

    # ── Tab-1 scan control ───────────────────────────────────
    def _get_selected_pairs(self) -> list[str]:
        """Return the list of pairs whose checkboxes are checked."""
        return [sym for sym, cb in self._pair_checks.items() if cb.isChecked()]

    def _start_scan(self):
        # Use only the checked pairs — no exchange symbol lookup needed
        symbols = self._get_selected_pairs()
        if not symbols:
            self._status_lbl.setText("⚠  Select at least one pair to scan")
            return

        try:
            from core.market_data.exchange_manager import exchange_manager
            if not exchange_manager.is_connected():
                self._status_lbl.setText("⚠  Not connected to exchange")
                self._connect_btn.setVisible(True)
                return
        except Exception as exc:
            self._status_lbl.setText(f"⚠  {exc}")
            return

        for w in (self._worker, self._cg_worker):
            if w and w.isRunning():
                try:
                    w.stop() if hasattr(w, "stop") else None
                    w.wait(1000)
                except Exception:
                    pass

        self._all_rows.clear()
        self._table.setRowCount(0)
        self._table.setSortingEnabled(False)
        self._scan_btn.setEnabled(False)
        self._stop_btn.setEnabled(True)

        self._progress.setRange(0, 0)
        self._progress.setVisible(True)

        self._pending_symbols = symbols
        self._status_lbl.setText("📡  Fetching market data from CoinGecko…")
        self._cg_data = {}

        # Fetch CoinGecko data for price/volume enrichment (no cap filtering)
        self._cg_worker = CoinGeckoWorker(vs_currency="usd", min_market_cap=0)
        self._cg_worker.data_ready.connect(self._on_cg_ready)
        self._cg_worker.error.connect(self._on_cg_error)
        self._cg_worker.start()

    @Slot(dict)
    def _on_cg_ready(self, data: dict):
        self._cg_data = data
        n_cg          = len(data)
        self._cg_lbl.setText(
            f"Market Data: <b style='color:#4488CC'>{n_cg} coins</b>"
        )
        self._status_lbl.setText(
            f"✓ CoinGecko: {n_cg} coins loaded — "
            f"scanning {len(self._pending_symbols)} pairs…"
        )
        self._progress.setRange(0, max(len(self._pending_symbols), 1))
        self._progress.setValue(0)
        self._launch_scanner()

    @Slot(str)
    def _on_cg_error(self, err: str):
        logger.warning("CoinGecko error (continuing): %s", err)
        self._cg_lbl.setText("Market Data: <b style='color:#FF9800'>unavailable</b>")
        self._status_lbl.setText("⚠  CoinGecko unavailable — scanning without market data")
        self._progress.setRange(0, max(len(self._pending_symbols), 1))
        self._progress.setValue(0)
        self._launch_scanner()

    def _launch_scanner(self):
        symbols = getattr(self, "_pending_symbols", [])
        if not symbols:
            self._status_lbl.setText("⚠  No matching pairs for the selected filter")
            self._progress.setVisible(False)
            self._scan_btn.setEnabled(True)
            self._stop_btn.setEnabled(False)
            return
        self._worker = ScannerWorker(
            symbols   = symbols,
            timeframe = self._tf_combo.currentText(),
            max_symbols = 500,
            cg_data   = self._cg_data,
        )
        self._worker.row_ready.connect(self._on_row_ready)
        self._worker.progress.connect(self._on_progress)
        self._worker.finished.connect(self._on_scan_finished)
        self._worker.error.connect(self._on_scan_error)
        self._worker.start()

    def _stop_scan(self):
        for w in (self._worker, self._cg_worker):
            if w and w.isRunning():
                if hasattr(w, "stop"):
                    w.stop()
        self._scan_btn.setEnabled(True)
        self._stop_btn.setEnabled(False)
        self._progress.setVisible(False)
        self._status_lbl.setText("Scan stopped")

    # ── Tab-1 worker signals ─────────────────────────────────
    @Slot(dict)
    def _on_row_ready(self, data: dict):
        self._all_rows.append(data)
        self._apply_filter()
        self._update_summary()

    @Slot(int, int, str)
    def _on_progress(self, done: int, total: int, msg: str):
        self._progress.setValue(done)
        self._status_lbl.setText(msg)

    @Slot(int)
    def _on_scan_finished(self, total: int):
        self._progress.setVisible(False)
        self._scan_btn.setEnabled(True)
        self._stop_btn.setEnabled(False)
        self._table.setSortingEnabled(True)
        self._status_lbl.setText(
            f"✓ Scan complete — {total} symbols scanned, {len(self._all_rows)} results"
        )

    @Slot(str)
    def _on_scan_error(self, err: str):
        self._progress.setVisible(False)
        self._scan_btn.setEnabled(True)
        self._stop_btn.setEnabled(False)
        self._status_lbl.setText(f"⚠  Error: {err}")

    # ── Tab-1 table rendering ────────────────────────────────
    def _apply_filter(self):
        sig_filter   = self._signal_filter.currentText().lower()
        active_pairs = self._get_selected_pairs()

        rows = self._all_rows

        # Filter to checked pairs only
        if active_pairs:
            rows = [r for r in rows if r["symbol"] in active_pairs]
        if sig_filter != "all":
            rows = [r for r in rows if r["signal"] == sig_filter]

        rows = sorted(rows, key=lambda r: (r["strength"], r["bullish"]), reverse=True)

        self._table.setSortingEnabled(False)
        self._table.setRowCount(len(rows))

        for ri, d in enumerate(rows):
            sig    = d["signal"]
            sig_c  = SIGNAL_COLORS.get(sig, "#8899AA")
            mcap   = d.get("market_cap", 0)
            price  = d.get("price", 0.0)
            ch_15m = d.get("change_15m")   # None = unavailable
            ch_1h  = d.get("change_1h")
            ch_4h  = d.get("change_4h")
            ch_24h = d.get("change_24h")
            ch_7d  = d.get("change_7d")
            ch_30d = d.get("change_30d")
            vol    = d.get("volume_24h", 0)
            rsi    = d.get("rsi")
            rsi_s  = f"{rsi:.1f}" if rsi is not None else "—"
            rsi_c  = (
                "#00CC77" if rsi is not None and rsi < 35 else
                "#FF3355" if rsi is not None and rsi > 65 else
                "#E8EBF0"
            )

            self._table.setItem(ri, 0, _colored_item(
                d["symbol"], "#E8EBF0", Qt.AlignLeft | Qt.AlignVCenter
            ))
            self._table.setItem(ri, 1, _numeric_item(
                mcap, _fmt_mcap(mcap), "#4499DD" if mcap > 0 else "#3A4A5A"
            ))
            # Price — numeric item so it sorts as a number, not a string
            self._table.setItem(ri, 2, _numeric_item(price, _fmt_price(price), "#E8EBF0"))

            t, c = _pct_cell(ch_15m)
            self._table.setItem(ri, 3, _numeric_item(ch_15m, t, c))
            t, c = _pct_cell(ch_1h)
            self._table.setItem(ri, 4, _numeric_item(ch_1h, t, c))
            t, c = _pct_cell(ch_4h)
            self._table.setItem(ri, 5, _numeric_item(ch_4h, t, c))
            t, c = _pct_cell(ch_24h)
            self._table.setItem(ri, 6, _numeric_item(ch_24h, t, c))
            t, c = _pct_cell(ch_7d)
            self._table.setItem(ri, 7, _numeric_item(ch_7d, t, c))
            t, c = _pct_cell(ch_30d)
            self._table.setItem(ri, 8, _numeric_item(ch_30d, t, c))

            self._table.setItem(ri, 9,  _numeric_item(vol, _fmt_vol(vol), "#8899AA" if vol else "#3A4A5A"))
            self._table.setItem(ri, 10, _numeric_item(rsi, rsi_s, rsi_c))
            self._table.setItem(ri, 11, _colored_item(sig.upper(), sig_c))
            self._table.setItem(ri, 12, _numeric_item(
                d["strength"], f"{d['strength']}%", sig_c
            ))
            self._table.setItem(ri, 13, _numeric_item(
                d["bullish"], str(d["bullish"]), "#00CC77"
            ))
            self._table.setItem(ri, 14, _numeric_item(
                d["bearish"], str(d["bearish"]), "#FF3355"
            ))

        self._table.setSortingEnabled(True)

    def _update_summary(self):
        bull = sum(1 for r in self._all_rows if r["signal"] == "bullish")
        bear = sum(1 for r in self._all_rows if r["signal"] == "bearish")
        neut = sum(1 for r in self._all_rows if r["signal"] == "neutral")
        tot  = len(self._all_rows)

        top = "—"
        if self._all_rows:
            best = max(self._all_rows, key=lambda r: r["strength"])
            top  = f"{best['symbol']} ({best['strength']}%)"

        self._total_lbl.setText(  f"Total: <b style='color:#E8EBF0'>{tot}</b>")
        self._bull_lbl.setText(   f"Bullish: <b style='color:#00CC77'>{bull}</b>")
        self._bear_lbl.setText(   f"Bearish: <b style='color:#FF3355'>{bear}</b>")
        self._neutral_lbl.setText(f"Neutral: <b style='color:#8899AA'>{neut}</b>")
        self._top_lbl.setText(    f"Top: <b style='color:#FFB300'>{top}</b>")

    def _on_row_double_clicked(self, index):
        sym_item = self._table.item(index.row(), 0)
        if not sym_item:
            return
        symbol = sym_item.text()
        try:
            main = self.window()
            if hasattr(main, "_pages") and "chart_workspace" in main._pages:
                chart_page = main._pages["chart_workspace"]
                chart_page._symbol_combo.setCurrentText(symbol)
                main._navigate_to("chart_workspace")
        except Exception as exc:
            logger.debug("Could not switch to chart: %s", exc)
