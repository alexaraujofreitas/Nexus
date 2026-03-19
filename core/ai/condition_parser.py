# ============================================================
# NEXUS TRADER — AI Condition Parser
#
# Translates natural-language conditions produced by the AI
# (e.g. "RSI crosses above 30", "price above EMA20") into the
# structured condition-tree format consumed by RuleEvaluator
# and run_backtest() in core/backtesting/backtest_engine.py.
# ============================================================

from __future__ import annotations

import logging
import re
from typing import Optional

logger = logging.getLogger(__name__)


# ── Timeframe extraction helpers ───────────────────────────────
_VALID_TFS = r"(?:1m|3m|5m|15m|30m|1h|2h|4h|6h|12h|1d|1w)"

# Matches "_1h", "_15m" suffix   e.g. "EMA20_1h", "RSI14_15m", "ADX14_15m"
_TF_SUFFIX_RE  = re.compile(r"_(" + _VALID_TFS + r")$", re.IGNORECASE)
# Matches " on 1h", " on 5m" inline  e.g. "MACD on 5m", "EMA20 on 1h"
_TF_INLINE_RE  = re.compile(r"\s+on\s+(" + _VALID_TFS + r")\b", re.IGNORECASE)


def _strip_tf(text: str) -> tuple[str, Optional[str]]:
    """
    Strip a timeframe annotation from an indicator label and return
    (clean_indicator_text, timeframe_or_None).

    Handles two styles:
      • suffix :  "EMA20_1h"       → ("EMA20",  "1h")
      • inline :  "MACD on 5m"     → ("MACD",   "5m")
      • none   :  "RSI14"          → ("RSI14",  None)
    """
    t = text.strip()
    # Inline " on TF" takes priority (e.g. "RSI14 on 5m")
    m = _TF_INLINE_RE.search(t)
    if m:
        return t[: m.start()].strip(), m.group(1).lower()
    # Suffix "_TF" (e.g. "EMA20_15m")
    m = _TF_SUFFIX_RE.search(t)
    if m:
        return t[: m.start()].strip(), m.group(1).lower()
    return t, None


# ── Indicator name → backtest column name ─────────────────────
def _resolve_indicator(text: str) -> Optional[str]:
    """
    Map a human-readable indicator name to a DataFrame column name.
    Returns None if the indicator cannot be recognised.
    """
    t = text.strip().lower()

    # ── Price / OHLCV primitives ──────────────────────────────
    if re.match(r"^(price|close|closing\s*price|current\s*price)$", t):
        return "close"
    if t in ("open", "high", "low", "volume"):
        return t

    # ── RSI ───────────────────────────────────────────────────
    m = re.match(r"^rsi\s*[\(\s]?\s*(\d+)\s*\)?$", t)
    if m:
        return f"rsi_{m.group(1)}"
    if re.match(r"^rsi$", t):
        return "rsi_14"

    # ── EMA ───────────────────────────────────────────────────
    m = re.match(r"^ema\s*[\(\s]?\s*(\d+)\s*\)?$", t)
    if m:
        return f"ema_{m.group(1)}"
    if re.match(r"^ema$", t):
        return "ema_20"

    # ── SMA ───────────────────────────────────────────────────
    m = re.match(r"^sma\s*[\(\s]?\s*(\d+)\s*\)?$", t)
    if m:
        return f"sma_{m.group(1)}"
    if re.match(r"^sma$", t):
        return "sma_20"

    # ── MACD (order matters: signal/hist before generic macd) ─
    if re.search(r"macd.*(signal|sig)|signal.*macd|signal\s*line", t):
        return "macd_signal"
    if re.search(r"macd.*(hist|bar)|histogram.*macd", t):
        return "macd_hist"
    if "macd" in t:
        return "macd"

    # ── Bollinger Bands ───────────────────────────────────────
    if re.search(r"(bb|boll|band)", t):
        if re.search(r"(upper|top|high)", t):
            return "bb_upper"
        if re.search(r"(lower|bottom|low)", t):
            return "bb_lower"
        return "bb_mid"

    # ── ATR ───────────────────────────────────────────────────
    m = re.match(r"^atr\s*[\(\s]?\s*(\d+)\s*\)?$", t)
    if m:
        return f"atr_{m.group(1)}"
    if re.match(r"^atr$", t):
        return "atr_14"

    # ── Stochastic ────────────────────────────────────────────
    if re.search(r"stoch", t):
        if re.search(r"\bd\b", t):
            return "stoch_rsi_d"
        return "stoch_rsi_k"

    # ── VWAP / TWAP ──────────────────────────────────────────
    if "vwap" in t:
        return "vwap"
    if "twap" in t:
        return "twap"

    # ── ADX ───────────────────────────────────────────────────
    if re.match(r"^adx", t):
        return "adx"

    # ── MFI ───────────────────────────────────────────────────
    if re.search(r"mfi|money.flow", t):
        return "mfi"

    # ── Supertrend ────────────────────────────────────────────
    m = re.match(r"^supertrend\s*[\(\s]?\s*(\d+)\s*\)?$", t)
    if m:
        return f"supertrend_{m.group(1)}"
    if "supertrend" in t:
        return "supertrend_10"

    return None


# ── Single condition text parser ──────────────────────────────
def parse_condition_text(text: str) -> Optional[dict]:
    """
    Parse one condition string into a condition-tree leaf node.

    Supported patterns (case-insensitive):
      "RSI crosses above 30"
      "RSI crosses above EMA20"
      "MACD crosses above signal line"
      "price above EMA20"
      "RSI below 70"
      "RSI > 50"
      "close >= SMA50"

    Returns a condition dict compatible with RuleEvaluator, or None
    if the condition cannot be parsed.
    """
    t = text.strip()
    if not t:
        return None

    # ── Special: "X crosses above/below signal line" ──────────
    m = re.match(
        r"^(.+?)\s+crosses?\s+(above|below)\s+(?:the\s+)?(?:macd\s+)?signal\s+(?:line)?$",
        t, re.IGNORECASE,
    )
    if m:
        clean_lhs, tf = _strip_tf(m.group(1))
        lhs_col = _resolve_indicator(clean_lhs)
        if lhs_col:
            op = "crosses_above_signal" if m.group(2).lower() == "above" \
                 else "crosses_below_signal"
            node: dict = {"type": "condition", "lhs": lhs_col, "op": op}
            if tf:
                node["timeframe"] = tf
            return node

    # ── "X crosses above/below Y" ─────────────────────────────
    m = re.match(
        r"^(.+?)\s+crosses?\s+(above|below)\s+(.+)$",
        t, re.IGNORECASE,
    )
    if m:
        clean_lhs, tf = _strip_tf(m.group(1))
        lhs_col = _resolve_indicator(clean_lhs)
        op      = "crosses_above" if m.group(2).lower() == "above" else "crosses_below"
        rhs_txt = m.group(3).strip()
        if lhs_col:
            node = _build_rhs(lhs_col, op, rhs_txt)
            if node and tf:
                node["timeframe"] = tf
            return node

    # ── Comparison operators (text and symbolic) ──────────────
    _OPS = [
        # longer patterns first to avoid partial matches
        (r"\s+is\s+greater\s+than\s+or\s+equal\s+to\s+",   ">="),
        (r"\s+is\s+less\s+than\s+or\s+equal\s+to\s+",      "<="),
        (r"\s+(?:is\s+)?greater\s+than\s+or\s+equal\s+to\s+", ">="),
        (r"\s+(?:is\s+)?less\s+than\s+or\s+equal\s+to\s+",   "<="),
        (r"\s+(?:is\s+)?greater\s+than\s+",                ">"),
        (r"\s+(?:is\s+)?less\s+than\s+",                   "<"),
        (r"\s+(?:is\s+)?above\s+",                         ">"),
        (r"\s+(?:is\s+)?below\s+",                         "<"),
        (r"\s+(?:is\s+)?under\s+",                         "<"),
        (r"\s+(?:is\s+)?over\s+",                          ">"),
        (r"\s*>=\s*",                                       ">="),
        (r"\s*<=\s*",                                       "<="),
        (r"\s*>\s*",                                        ">"),
        (r"\s*<\s*",                                        "<"),
        (r"\s*==\s*",                                       "=="),
        (r"\s+(?:is\s+)?equal\s+to\s+",                    "=="),
    ]

    for pattern, op in _OPS:
        m = re.match(r"^(.+?)" + pattern + r"(.+)$", t, re.IGNORECASE)
        if m:
            clean_lhs, tf = _strip_tf(m.group(1))
            lhs_col = _resolve_indicator(clean_lhs)
            if lhs_col:
                result = _build_rhs(lhs_col, op, m.group(2).strip())
                if result:
                    if tf:
                        result["timeframe"] = tf
                    return result

    logger.debug("condition_parser: could not parse %r", text)
    return None


def _build_rhs(lhs_col: str, op: str, rhs_text: str) -> Optional[dict]:
    """Try to parse rhs_text as a numeric value or an indicator column."""
    # Numeric value?
    try:
        rhs_val = float(rhs_text.replace(",", ""))
        return {
            "type":      "condition",
            "lhs":       lhs_col,
            "op":        op,
            "rhs_type":  "value",
            "rhs_value": rhs_val,
        }
    except ValueError:
        pass

    # Indicator column? Strip any timeframe annotation before resolving.
    clean_rhs, _ = _strip_tf(rhs_text)
    rhs_col = _resolve_indicator(clean_rhs)
    if rhs_col:
        return {
            "type":          "condition",
            "lhs":           lhs_col,
            "op":            op,
            "rhs_type":      "indicator",
            "rhs_indicator": rhs_col,
        }

    return None


# ── Condition list → tree ─────────────────────────────────────
def build_condition_tree(
    conditions: list[str],
    logic: str = "AND",
) -> Optional[dict]:
    """
    Convert a list of condition strings into a RuleEvaluator-compatible tree.
    Returns None if nothing could be parsed.
    """
    # Patterns that are valid risk-management directives (not indicator conditions).
    # These appear when the LLM puts stop_loss/take_profit into the conditions list
    # instead of the risk section — they're silently skipped here; risk params are
    # extracted separately in ai_definition_to_backtest_params().
    _RISK_PARAM_RE = re.compile(
        r"^\s*(stop.?loss|take.?profit|trailing.?stop|sl|tp)\b", re.IGNORECASE
    )

    parsed: list[dict] = []
    for raw in conditions:
        if not raw.strip():
            continue
        if _RISK_PARAM_RE.match(raw):
            logger.debug("condition_parser: skipping risk-param directive (not an indicator condition): %r", raw)
            continue
        node = parse_condition_text(raw)
        if node:
            parsed.append(node)
        else:
            logger.warning("condition_parser: skipping unparseable condition: %r", raw)

    if not parsed:
        return None
    if len(parsed) == 1:
        return parsed[0]
    return {"type": "group", "logic": logic.upper(), "conditions": parsed}


# ── Full AI definition → backtest params ─────────────────────
def ai_definition_to_backtest_params(definition: dict) -> dict:
    """
    Convert an AI strategy ``definition`` dict (as stored in Strategy.definition)
    into keyword arguments suitable for ``run_backtest()``.

    The AI strategy JSON format is::

        {
          "symbols":    ["BTC/USDT"],
          "timeframe":  "1h",
          "indicators": [...],
          "entry_long":  {"conditions": [...], "logic": "AND"},
          "exit_long":   {"conditions": [...], "logic": "OR"},
          "entry_short": {"conditions": [...], "logic": "AND"},
          "exit_short":  {"conditions": [...], "logic": "OR"},
          "risk": {
              "stop_loss_pct":       2.0,
              "take_profit_pct":     4.0,
              "position_size_pct":   2.0,
          }
        }

    Returns
    -------
    dict with keys:
        entry_tree        – condition tree dict (None if parsing failed)
        exit_tree         – condition tree dict or None
        direction         – "long" | "short"
        stop_loss_pct     – float
        take_profit_pct   – float
        position_size_pct – float
        parse_errors      – list[str] of any warnings / skipped conditions
    """
    errors: list[str] = []
    risk = definition.get("risk") or {}

    entry_long  = definition.get("entry_long",  {}) or {}
    entry_short = definition.get("entry_short", {}) or {}
    long_conds  = [c for c in (entry_long.get("conditions")  or []) if c.strip()]
    short_conds = [c for c in (entry_short.get("conditions") or []) if c.strip()]

    # Prefer long; fall back to short if no long conditions given
    if short_conds and not long_conds:
        direction       = "short"
        entry_conds     = short_conds
        entry_logic     = entry_short.get("logic", "AND")
        exit_section    = definition.get("exit_short", {}) or {}
    else:
        direction       = "long"
        entry_conds     = long_conds
        entry_logic     = entry_long.get("logic", "AND")
        exit_section    = definition.get("exit_long", {}) or {}

    # Build trees
    entry_tree = build_condition_tree(entry_conds, entry_logic)
    if not entry_tree:
        errors.append(
            f"Could not parse any entry conditions. Raw: {entry_conds}"
        )

    exit_conds = [c for c in (exit_section.get("conditions") or []) if c.strip()]
    exit_tree  = build_condition_tree(exit_conds, exit_section.get("logic", "OR")) \
                 if exit_conds else None

    return {
        "entry_tree":        entry_tree,
        "exit_tree":         exit_tree,
        "direction":         direction,
        "stop_loss_pct":     float(risk.get("stop_loss_pct",     2.0)),
        "take_profit_pct":   float(risk.get("take_profit_pct",   4.0)),
        "position_size_pct": float(risk.get("position_size_pct", 2.0)),
        "parse_errors":      errors,
    }
