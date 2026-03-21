"""
gui/widgets/demo_monitor_helpers.py
Pure-Python helper functions for DemoMonitorWidget.
No Qt dependency — importable in headless test environments.
"""
from __future__ import annotations

_MODEL_NAME_MAP = {
    "trend":             "Trend",
    "momentum_breakout": "MomBreak",
    "sentiment":         "Sentiment",
    "funding_rate":      "FundRate",
    "order_book":        "OrdBook",
    "rl_ensemble":       "RL",
    "vwap_reversion":    "VWAP",
}


def fmt_pct(val) -> str:
    """Format a 0–1 float as a percentage string, e.g. 0.503 → '50.3%'."""
    if val is None:
        return "—"
    return f"{float(val) * 100:.1f}%"


def fmt_delta_pct(val) -> str:
    """Format a delta (0–1 float) with a +/- sign, e.g. 0.05 → '+5.0%'."""
    if val is None:
        return "—"
    v = float(val) * 100
    sign = "+" if v > 0 else ""
    return f"{sign}{v:.1f}%"


def fmt_model_name(key: str) -> str:
    """Return a display-friendly model name for sidebar/table use."""
    return _MODEL_NAME_MAP.get(key, key.replace("_", " ").title())
