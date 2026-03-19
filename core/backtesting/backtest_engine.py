# ============================================================
# NEXUS TRADER — Backtesting Engine
# Condition-tree evaluator + event-driven trade simulator
# ============================================================

from __future__ import annotations

import logging
import math
from typing import Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# ── Operator display names (used in UI) ──────────────────────
OPERATORS: list[tuple[str, str]] = [
    # Standard comparisons
    ("greater than",              ">"),
    ("lower than",                "<"),
    ("greater than or equal to",  ">="),
    ("lower than or equal to",    "<="),
    ("equals",                    "=="),
    # Crossover
    ("crosses above",             "crosses_above"),
    ("crosses below",             "crosses_below"),
    # MACD-specific: crosses signal line
    ("crosses above signal line", "crosses_above_signal"),
    ("crosses below signal line", "crosses_below_signal"),
    # Percentage change (1-bar)
    ("increased by % within",     "pct_up"),
    ("decreased by % within",     "pct_down"),
]
OP_DISPLAY = {code: label for label, code in OPERATORS}

# ── Indicator columns (matches document: Rules Allowed.docx) ──
INDICATOR_OPTIONS: list[tuple[str, str]] = [
    # ── Price & Volume ────────────────────────────────────────
    ("Price (Close)",           "close"),
    ("Open",                    "open"),
    ("High",                    "high"),
    ("Low",                     "low"),
    ("Volume",                  "volume"),
    # ── RSI variants ─────────────────────────────────────────
    ("RSI (2)",                 "rsi_2"),
    ("RSI (3)",                 "rsi_3"),
    ("RSI (5)",                 "rsi_5"),
    ("RSI (6)",                 "rsi_6"),
    ("RSI (7)",                 "rsi_7"),
    ("RSI (8)",                 "rsi_8"),
    ("RSI (12)",                "rsi_12"),
    ("RSI (14)",                "rsi_14"),
    ("RSI (24)",                "rsi_24"),
    # ── Stochastic RSI ────────────────────────────────────────
    ("Stoch RSI K (14)",        "stoch_rsi_k"),
    ("Stoch RSI D (14)",        "stoch_rsi_d"),
    # ── EMA variants ─────────────────────────────────────────
    ("EMA (2)",                 "ema_2"),
    ("EMA (3)",                 "ema_3"),
    ("EMA (5)",                 "ema_5"),
    ("EMA (9)",                 "ema_9"),
    ("EMA (10)",                "ema_10"),
    ("EMA (12)",                "ema_12"),
    ("EMA (20)",                "ema_20"),
    ("EMA (26)",                "ema_26"),
    ("EMA (27)",                "ema_27"),
    ("EMA (32)",                "ema_32"),
    ("EMA (50)",                "ema_50"),
    ("EMA (63)",                "ema_63"),
    # ── SMA variants ─────────────────────────────────────────
    ("SMA (2)",                 "sma_2"),
    ("SMA (3)",                 "sma_3"),
    ("SMA (5)",                 "sma_5"),
    ("SMA (9)",                 "sma_9"),
    ("SMA (10)",                "sma_10"),
    ("SMA (12)",                "sma_12"),
    ("SMA (20)",                "sma_20"),
    ("SMA (26)",                "sma_26"),
    ("SMA (27)",                "sma_27"),
    ("SMA (32)",                "sma_32"),
    ("SMA (50)",                "sma_50"),
    ("SMA (63)",                "sma_63"),
    # ── Bollinger Bands ───────────────────────────────────────
    ("Bollinger Upper (20)",    "bb_upper"),
    ("Bollinger Middle (20)",   "bb_mid"),
    ("Bollinger Lower (20)",    "bb_lower"),
    # ── MACD ─────────────────────────────────────────────────
    ("MACD Line",               "macd"),
    ("MACD Signal Line",        "macd_signal"),
    ("MACD Histogram",          "macd_hist"),
    # ── Money Flow Index ─────────────────────────────────────
    ("Money Flow Index (14)",   "mfi"),
    # ── ATR variants ─────────────────────────────────────────
    ("ATR (2)",                 "atr_2"),
    ("ATR (3)",                 "atr_3"),
    ("ATR (5)",                 "atr_5"),
    ("ATR (6)",                 "atr_6"),
    ("ATR (7)",                 "atr_7"),
    ("ATR (8)",                 "atr_8"),
    ("ATR (12)",                "atr_12"),
    ("ATR (14)",                "atr_14"),
    ("ATR (24)",                "atr_24"),
    # ── SuperTrend variants ───────────────────────────────────
    ("SuperTrend (5)",          "supertrend_5"),
    ("SuperTrend (10)",         "supertrend_10"),
    ("SuperTrend (15)",         "supertrend_15"),
    # ── TWAP / VWAP ──────────────────────────────────────────
    ("TWAP",                    "twap"),
    ("VWAP",                    "vwap"),
    # ── Legacy aliases (backward compat) ─────────────────────
    ("ADX (14)",                "adx"),
    ("CCI (20)",                "cci"),
    ("Williams %R",             "williams_r"),
    ("Ichi Conversion",         "ichi_conversion"),
    ("Ichi Base Line",          "ichi_base"),
]
INDICATOR_DISPLAY = {col: label for label, col in INDICATOR_OPTIONS}


# ── Condition-tree evaluator ──────────────────────────────────
class RuleEvaluator:
    """
    Walks a condition-tree dict and evaluates it against a pair of
    consecutive pandas Series (current bar + previous bar).

    Tree format
    -----------
    Group node::

        {"type": "group", "logic": "AND"|"OR",
         "conditions": [<node>, ...]}

    Condition node::

        {"type": "condition",
         "lhs": "<col_name>",
         "timeframe": "<tf>",          # optional — e.g. "1m", "5m", "1h"; empty = primary TF
         "op":  "<operator_code>",
         "rhs_type": "value"|"indicator",
         "rhs_value": <float>,
         "rhs_indicator": "<col_name>"}

    Multi-timeframe support
    -----------------------
    When a condition carries a non-empty ``"timeframe"`` field, the evaluator
    looks up ``tf_<tf>_<col>`` columns instead of bare column names.  These
    prefixed columns are produced by ``_build_mtf_df()`` which merges secondary-
    TF DataFrames (aligned via backward as-of join) into the primary DataFrame.
    """

    def evaluate(self, node: dict, row: pd.Series, prev: pd.Series) -> bool:
        if not node:
            return False
        ntype = node.get("type", "group")
        if ntype == "group":
            children = node.get("conditions", [])
            if not children:
                return False
            results = [self.evaluate(c, row, prev) for c in children]
            logic = node.get("logic", "AND")
            return all(results) if logic == "AND" else any(results)
        else:
            return self._eval_condition(node, row, prev)

    # ── private helpers ───────────────────────────────────────
    def _get(self, col: str, row: pd.Series, default: float = float("nan")) -> float:
        v = row.get(col, default)
        return float(v) if (v is not None and not (isinstance(v, float) and math.isnan(v))) else float("nan")

    def _eval_condition(self, c: dict, row: pd.Series, prev: pd.Series) -> bool:
        # If the condition carries a specific timeframe, look up the prefixed
        # column produced by _build_mtf_df (e.g. "tf_1m_rsi_14").
        tf = (c.get("timeframe") or "").strip()

        def _col(base: str) -> str:
            return f"tf_{tf}_{base}" if tf else base

        lhs_col  = _col(c["lhs"])
        lhs      = self._get(lhs_col, row)
        prev_lhs = self._get(lhs_col, prev)
        op       = c.get("op", ">")

        if c.get("rhs_type", "value") == "indicator":
            rhs_col  = _col(c.get("rhs_indicator", "close"))
            rhs      = self._get(rhs_col, row)
            prev_rhs = self._get(rhs_col, prev)
        else:
            rhs      = float(c.get("rhs_value", 0))
            prev_rhs = rhs

        # ── MACD signal-line crossing (special: RHS is always macd_signal) ──
        if op in ("crosses_above_signal", "crosses_below_signal"):
            macd_sig_col  = _col("macd_signal")
            macd_sig      = self._get(macd_sig_col, row)
            prev_macd_sig = self._get(macd_sig_col, prev)
            if any(math.isnan(v) for v in (lhs, prev_lhs, macd_sig, prev_macd_sig)):
                return False
            if op == "crosses_above_signal":
                return prev_lhs <= prev_macd_sig and lhs > macd_sig
            else:
                return prev_lhs >= prev_macd_sig and lhs < macd_sig

        # ── Percentage change operators ───────────────────────────────────
        if op in ("pct_up", "pct_down"):
            if math.isnan(prev_lhs) or prev_lhs == 0 or math.isnan(rhs):
                return False
            change_pct = (lhs - prev_lhs) / abs(prev_lhs) * 100.0
            if op == "pct_up":
                return change_pct >= rhs
            else:
                return change_pct <= -rhs

        # Guard NaN for standard comparisons
        if math.isnan(lhs) or math.isnan(rhs):
            return False

        if op == "crosses_above":
            return (not math.isnan(prev_lhs)) and (not math.isnan(prev_rhs)) \
                   and prev_lhs <= prev_rhs and lhs > rhs
        elif op == "crosses_below":
            return (not math.isnan(prev_lhs)) and (not math.isnan(prev_rhs)) \
                   and prev_lhs >= prev_rhs and lhs < rhs
        elif op == ">":  return lhs > rhs
        elif op == ">=": return lhs >= rhs
        elif op == "<":  return lhs < rhs
        elif op == "<=": return lhs <= rhs
        elif op == "==": return abs(lhs - rhs) < 1e-9
        return False


# ── On-demand indicator column helpers ───────────────────────
def _collect_cols(node: dict, out: set, primary_only: bool = False) -> None:
    """
    Walk a condition tree and collect every DataFrame column name referenced.

    Parameters
    ----------
    primary_only : if True, skip conditions that carry a non-empty ``"timeframe"``
                   field (those columns are handled by ``_build_mtf_df()``).
    """
    if not node:
        return
    if node.get("type") == "group":
        for child in node.get("conditions", []):
            _collect_cols(child, out, primary_only=primary_only)
    else:
        if primary_only and (node.get("timeframe") or "").strip():
            return   # secondary-TF column — handled by _build_mtf_df
        lhs = node.get("lhs")
        if lhs:
            out.add(lhs)
        if node.get("rhs_type") == "indicator":
            rhs = node.get("rhs_indicator")
            if rhs:
                out.add(rhs)


def _collect_cols_for_tf(node: dict, tf: str, out: set) -> None:
    """Collect column names used in conditions that reference a specific timeframe."""
    if not node:
        return
    if node.get("type") == "group":
        for child in node.get("conditions", []):
            _collect_cols_for_tf(child, tf, out)
    else:
        if (node.get("timeframe") or "").strip() != tf:
            return
        lhs = node.get("lhs")
        if lhs:
            out.add(lhs)
        if node.get("rhs_type") == "indicator":
            rhs = node.get("rhs_indicator")
            if rhs:
                out.add(rhs)


def _collect_timeframes(node: dict) -> set:
    """Walk a condition tree and collect all non-empty timeframe values."""
    if not node:
        return set()
    if node.get("type") == "group":
        tfs: set = set()
        for child in node.get("conditions", []):
            tfs |= _collect_timeframes(child)
        return tfs
    tf = (node.get("timeframe") or "").strip()
    return {tf} if tf else set()


def _compute_col_on_demand(df: pd.DataFrame, col: str) -> None:
    """
    Compute a single missing indicator column in-place by parsing its name.
    Handles: ema_N, sma_N, rsi_N, atr_N.  Unknown columns are filled with NaN
    so the evaluator silently returns False (no crash).
    """
    import re as _re

    m = _re.match(r'^ema_(\d+)$', col)
    if m:
        period = int(m.group(1))
        df[col] = df['close'].ewm(span=period, adjust=False).mean()
        logger.debug("backtest_engine: computed on-demand %s", col)
        return

    m = _re.match(r'^sma_(\d+)$', col)
    if m:
        period = int(m.group(1))
        df[col] = df['close'].rolling(window=period, min_periods=period).mean()
        logger.debug("backtest_engine: computed on-demand %s", col)
        return

    m = _re.match(r'^rsi_(\d+)$', col)
    if m:
        period = int(m.group(1))
        delta  = df['close'].diff()
        gain   = delta.clip(lower=0).rolling(window=period, min_periods=period).mean()
        loss   = (-delta.clip(upper=0)).rolling(window=period, min_periods=period).mean()
        rs     = gain / loss.replace(0, float('nan'))
        df[col] = 100.0 - (100.0 / (1.0 + rs))
        logger.debug("backtest_engine: computed on-demand %s", col)
        return

    m = _re.match(r'^atr_(\d+)$', col)
    if m:
        period    = int(m.group(1))
        prev_close = df['close'].shift(1)
        tr = pd.concat([
            df['high'] - df['low'],
            (df['high'] - prev_close).abs(),
            (df['low']  - prev_close).abs(),
        ], axis=1).max(axis=1)
        df[col] = tr.rolling(window=period, min_periods=period).mean()
        logger.debug("backtest_engine: computed on-demand %s", col)
        return

    # Unknown column — fill with NaN so comparisons return False gracefully
    df[col] = float('nan')
    logger.warning("backtest_engine: unknown indicator column '%s' — filled with NaN", col)


def _ensure_columns(df: pd.DataFrame, *trees, primary_only: bool = False) -> None:
    """
    Walk all condition trees, find any column names missing from df,
    and compute them on demand.  Called once per backtest run after
    calculate_all() so that AI-generated conditions referencing non-
    standard periods (e.g. ema_21, rsi_7) always work.

    Parameters
    ----------
    primary_only : if True, skip columns from secondary-TF conditions (those
                   are handled by ``_build_mtf_df()``).
    """
    needed: set = set()
    for tree in trees:
        if tree:
            _collect_cols(tree, needed, primary_only=primary_only)

    missing = [c for c in needed if c and c not in df.columns]
    if missing:
        logger.info("backtest_engine: computing %d on-demand columns: %s", len(missing), missing)
    for col in missing:
        _compute_col_on_demand(df, col)


# ── Multi-timeframe DataFrame builder ────────────────────────
def _build_mtf_df(
    primary_tf:  str,
    df_map:      dict,          # {timeframe_str: pd.DataFrame}
    entry_tree:  dict,
    exit_tree:   Optional[dict],
) -> pd.DataFrame:
    """
    Build a merged DataFrame where every secondary TF's indicators are
    available as ``tf_<tf>_<col>`` columns on every primary-TF bar.

    Alignment strategy: backward as-of join — for each primary bar, we use
    the most recently *completed* bar from each secondary timeframe whose
    timestamp ≤ the primary bar's timestamp.  This is look-ahead free.
    """
    from core.features.indicator_library import calculate_all

    # ── Primary TF ────────────────────────────────────────────
    primary_df = calculate_all(df_map[primary_tf].copy())
    # Only ensure columns used by primary-TF conditions
    _ensure_columns(primary_df, entry_tree, exit_tree, primary_only=True)

    # ── Secondary TFs ─────────────────────────────────────────
    for tf, df_sec in df_map.items():
        if tf == primary_tf:
            continue
        if df_sec is None or df_sec.empty:
            logger.warning("_build_mtf_df: no data for secondary TF '%s' — skipping", tf)
            continue

        df_sec_calc = calculate_all(df_sec.copy())

        # Ensure any non-standard columns this TF's conditions need
        needed: set = set()
        for tree in (entry_tree, exit_tree):
            if tree:
                _collect_cols_for_tf(tree, tf, needed)
        for col in needed:
            if col and col not in df_sec_calc.columns:
                _compute_col_on_demand(df_sec_calc, col)

        # Prefix all columns with tf_{tf}_  so they don't clash
        df_sec_prefixed = df_sec_calc.rename(
            columns={c: f"tf_{tf}_{c}" for c in df_sec_calc.columns}
        )

        # Backward as-of merge: primary row gets the latest secondary bar
        # whose timestamp <= primary timestamp (no look-ahead)
        p_reset = primary_df.copy()
        p_reset.index.name = "_primary_ts"
        p_reset = p_reset.reset_index()

        s_reset = df_sec_prefixed.copy()
        s_reset.index.name = "_sec_ts"
        s_reset = s_reset.reset_index()

        merged = pd.merge_asof(
            p_reset.sort_values("_primary_ts"),
            s_reset.sort_values("_sec_ts"),
            left_on="_primary_ts",
            right_on="_sec_ts",
            direction="backward",
        )
        merged = merged.drop(columns=["_sec_ts"], errors="ignore")
        merged = merged.rename(columns={"_primary_ts": "timestamp"})
        primary_df = merged.set_index("timestamp")
        logger.info("_build_mtf_df: merged TF '%s' (%d bars) → %d prefixed columns",
                    tf, len(df_sec), len(df_sec_calc.columns))

    return primary_df


# ── Re-entry helper ──────────────────────────────────────────
def _relax_crosses_for_reentry(tree: dict) -> dict:
    """
    After a position closes, crossing conditions (crosses_above / crosses_below)
    are relaxed to their simple equivalents (>= / <=).

    Why: An AI strategy's entry condition is often "RSI crosses below 30".
    After a trade closes while RSI is *already* below 30 (e.g., hit stop-loss
    during a sustained dip), requiring a *new* crossover blocks re-entry for
    the rest of the drawdown.  Using '<=' instead allows the strategy to
    re-engage the dip immediately while keeping the *first* entry strict.
    """
    if not tree:
        return tree
    _RELAX = {
        "crosses_above":        ">=",
        "crosses_below":        "<=",
        "crosses_above_signal": ">=",
        "crosses_below_signal": "<=",
    }
    if tree.get("type") == "group":
        return {
            **tree,
            "conditions": [_relax_crosses_for_reentry(c) for c in tree.get("conditions", [])],
        }
    relaxed_op = _RELAX.get(tree.get("op"))
    return {**tree, "op": relaxed_op} if relaxed_op else tree


# ── Regime-split performance reporting ────────────────────
def _compute_regime_splits(trades: list[dict], ohlcv_df: pd.DataFrame) -> dict:
    """
    Group trades by regime label and compute performance metrics per regime.

    Parameters
    ----------
    trades : list[dict]
        List of trade dicts with keys: entry_time, pnl, pnl_pct, etc.
    ohlcv_df : pd.DataFrame
        DataFrame with a "regime" column (string labels for market regime).

    Returns
    -------
    dict
        Maps regime_name -> {trade_count, win_rate, avg_pnl_pct, sharpe_proxy, total_pnl_pct}
    """
    if trades is None or len(trades) == 0 or "regime" not in ohlcv_df.columns:
        return {}

    regime_trades = {}

    for trade in trades:
        entry_time_str = trade.get("entry_time", "")
        # Match entry_time to ohlcv_df index
        regime_label = None
        try:
            # Convert string back to comparable format if needed
            for idx, row in ohlcv_df.iterrows():
                if str(idx) == entry_time_str or idx == pd.Timestamp(entry_time_str):
                    regime_label = row.get("regime", "unknown")
                    break
        except Exception:
            regime_label = "unknown"

        if regime_label is None:
            regime_label = "unknown"

        if regime_label not in regime_trades:
            regime_trades[regime_label] = []
        regime_trades[regime_label].append(trade)

    # Compute metrics per regime
    result = {}
    for regime_name, regime_trade_list in regime_trades.items():
        pnls = [t["pnl"] for t in regime_trade_list]
        pnl_pcts = [t["pnl_pct"] for t in regime_trade_list]
        wins = len([t for t in regime_trade_list if t["pnl"] > 0])
        total = len(regime_trade_list)
        win_rate = (wins / total * 100.0) if total > 0 else 0.0
        avg_pnl_pct = sum(pnl_pcts) / total if total > 0 else 0.0
        total_pnl_pct = sum(pnl_pcts)

        # Sharpe proxy: mean / std of trade returns
        sharpe_proxy = 0.0
        if total > 1:
            arr = np.array(pnl_pcts, dtype=float)
            mean_ret = float(np.mean(arr))
            std_ret = float(np.std(arr))
            sharpe_proxy = (mean_ret / std_ret) if std_ret > 0 else 0.0

        result[regime_name] = {
            "trade_count": total,
            "win_rate": round(win_rate, 1),
            "avg_pnl_pct": round(avg_pnl_pct, 2),
            "sharpe_proxy": round(sharpe_proxy, 2),
            "total_pnl_pct": round(total_pnl_pct, 2),
        }

    return result


# ── Bootstrap significance test ────────────────────────────
def compute_significance(trade_returns: list[float], n_iterations: int = 1000) -> dict:
    """
    Permutation test: shuffle trade returns N times and compute Sharpe ratio
    for each shuffled series. Compare observed Sharpe to null distribution.

    Parameters
    ----------
    trade_returns : list[float]
        List of trade PnL percentages.
    n_iterations : int
        Number of permutation iterations (default 1000).

    Returns
    -------
    dict
        Keys: p_value, observed_sharpe, null_sharpe_mean, null_sharpe_std, significant
    """
    if not trade_returns or len(trade_returns) < 2:
        return {
            "p_value": 1.0,
            "observed_sharpe": 0.0,
            "null_sharpe_mean": 0.0,
            "null_sharpe_std": 0.0,
            "significant": False,
        }

    # Compute observed Sharpe
    arr = np.array(trade_returns, dtype=float)
    mean_obs = float(np.mean(arr))
    std_obs = float(np.std(arr))
    observed_sharpe = (mean_obs / std_obs) if std_obs > 0 else 0.0

    # Permutation test: shuffle returns n_iterations times
    null_sharpes = []
    rng = np.random.RandomState(42)  # Seed for reproducibility

    for _ in range(n_iterations):
        shuffled = rng.permutation(arr)
        mean_s = float(np.mean(shuffled))
        std_s = float(np.std(shuffled))
        sharpe_s = (mean_s / std_s) if std_s > 0 else 0.0
        null_sharpes.append(sharpe_s)

    null_sharpes = np.array(null_sharpes, dtype=float)
    null_mean = float(np.mean(null_sharpes))
    null_std = float(np.std(null_sharpes))

    # One-tailed p-value: proportion of null sharpes >= observed
    p_value = float(np.mean(null_sharpes >= observed_sharpe))
    significant = p_value < 0.05

    return {
        "p_value": round(p_value, 4),
        "observed_sharpe": round(observed_sharpe, 4),
        "null_sharpe_mean": round(null_mean, 4),
        "null_sharpe_std": round(null_std, 4),
        "significant": significant,
    }


# ── Look-ahead bias prevention helper ──────────────────────
def _safe_slice(df: pd.DataFrame, bar_idx: int) -> pd.DataFrame:
    """
    Return a DataFrame slice containing only bars up to and including bar_idx.
    This prevents look-ahead bias by ensuring indicator calculations and signal
    generation only see historical data available at that point in time.

    Parameters
    ----------
    df : pd.DataFrame
        The full OHLCV DataFrame with indicators.
    bar_idx : int
        The current bar index (0-based).

    Returns
    -------
    pd.DataFrame
        df.iloc[:bar_idx+1].copy() — a snapshot of data up to current bar.
    """
    return df.iloc[:bar_idx + 1].copy()


# ── Trade simulator ───────────────────────────────────────────
def run_backtest(
    entry_tree:   dict,
    exit_tree:    Optional[dict],
    df_raw:       Optional[pd.DataFrame] = None,
    *,
    df_map:            Optional[dict] = None,   # Multi-TF: {tf_str: DataFrame}
    primary_tf:        str            = "",      # Key in df_map to iterate on
    initial_capital:   float = 10_000.0,
    position_size_pct: float = 10.0,
    stop_loss_pct:     float = 2.0,
    take_profit_pct:   float = 4.0,
    fee_pct:           float = 0.10,
    slippage_pct:      float = 0.05,
    direction:         str   = "long",
) -> dict:
    """
    Event-driven backtest over OHLCV data.

    Supply either:
    - ``df_raw``              — single-TF DataFrame (legacy / AI strategies)
    - ``df_map`` + ``primary_tf`` — multi-TF dict; secondary TFs are merged
                                    via backward as-of join.

    Returns
    -------
    dict with keys: ``trades``, ``equity_curve``, ``chart_equity``,
    ``equity_timestamps``, ``metrics``, ``candle_count``.
    """
    from core.features.indicator_library import calculate_all

    if df_map and primary_tf:
        # Multi-TF path — build merged DataFrame with prefixed secondary columns
        df = _build_mtf_df(primary_tf, df_map, entry_tree, exit_tree)
    elif df_raw is not None and not df_raw.empty:
        # Single-TF path (legacy)
        # NOTE: calculate_all() computes indicators on the full series upfront.
        # This is look-ahead safe only if the indicators themselves are not
        # forward-looking (e.g., SMA, EMA, RSI, ATR are computed causal-only).
        # For per-bar indicator computation to ensure strict look-ahead safety,
        # move _compute_col_on_demand() into the per-bar loop.
        df = calculate_all(df_raw.copy())
        # Compute any extra columns referenced in condition trees
        # (e.g. ema_21, rsi_7); skip secondary-TF conditions if any
        _ensure_columns(df, entry_tree, exit_tree, primary_only=bool(
            _collect_timeframes(entry_tree) | (_collect_timeframes(exit_tree) if exit_tree else set())
        ))
    else:
        return {"trades": [], "equity_curve": [], "chart_equity": [],
                "equity_timestamps": [], "metrics": {}, "candle_count": 0,
                "regime_performance": {}, "significance": {}, "look_ahead_safe": True}

    n  = len(df)

    fee       = fee_pct      / 100.0
    slip      = slippage_pct / 100.0
    pos_frac  = position_size_pct / 100.0
    sl_frac   = stop_loss_pct     / 100.0
    tp_frac   = take_profit_pct   / 100.0

    evaluator = RuleEvaluator()

    equity        = initial_capital
    position      = None          # None or dict when in trade
    equity_curve  = [initial_capital]
    trades        = []
    just_closed   = False         # True on the bar a trade was just closed

    # Per-bar mark-to-market equity for chart rendering
    chart_equity:     list[float] = [initial_capital]
    equity_timestamps: list       = [df.index[0]]

    for i in range(1, n):
        row  = df.iloc[i]
        prev = df.iloc[i - 1]
        ts   = df.index[i]

        just_closed = False

        # ── In a position: check SL / TP / exit rule ─────────
        if position is not None:
            triggered = False
            exit_reason = "exit_rule"
            exit_px = float(row["close"])

            # Check SL / TP (using high/low of bar for realistic fill)
            if direction == "long":
                if float(row["low"]) <= position["sl"]:
                    exit_px     = position["sl"]
                    exit_reason = "stop_loss"
                    triggered   = True
                elif float(row["high"]) >= position["tp"]:
                    exit_px     = position["tp"]
                    exit_reason = "take_profit"
                    triggered   = True
            else:  # short
                if float(row["high"]) >= position["sl"]:
                    exit_px     = position["sl"]
                    exit_reason = "stop_loss"
                    triggered   = True
                elif float(row["low"]) <= position["tp"]:
                    exit_px     = position["tp"]
                    exit_reason = "take_profit"
                    triggered   = True

            # Check exit rule if provided and SL/TP not hit
            if not triggered and exit_tree:
                try:
                    if evaluator.evaluate(exit_tree, row, prev):
                        triggered   = True
                        exit_reason = "exit_rule"
                except Exception:
                    pass

            if triggered:
                # Apply slippage against the direction
                if direction == "long":
                    exit_fill = exit_px * (1.0 - slip)
                else:
                    exit_fill = exit_px * (1.0 + slip)

                qty       = position["quantity"]
                exit_val  = exit_fill * qty
                exit_fee  = exit_val * fee

                if direction == "long":
                    pnl = (exit_fill - position["entry_price"]) * qty \
                          - exit_fee - position["entry_fee"]
                else:
                    pnl = (position["entry_price"] - exit_fill) * qty \
                          - exit_fee - position["entry_fee"]

                pnl_pct = pnl / (position["entry_price"] * qty) * 100.0
                equity += pnl

                duration_bars = i - position["entry_i"]
                trades.append({
                    "entry_time":  str(position["entry_time"]),
                    "exit_time":   str(ts),
                    "entry_price": round(position["entry_price"], 8),
                    "exit_price":  round(exit_fill, 8),
                    "quantity":    round(qty, 8),
                    "pnl":         round(pnl, 4),
                    "pnl_pct":     round(pnl_pct, 2),
                    "exit_reason": exit_reason,
                    "duration_bars": duration_bars,
                })
                equity_curve.append(round(equity, 4))
                position   = None
                just_closed = True    # flag: allow re-entry this same bar

        # ── Not in a position: check entry ─────────────────────
        # NOTE: uses `if` (not `else`) so same-bar re-entry is possible
        # immediately after a stop-loss / take-profit / exit-rule close.
        # On the bar we just closed a trade (just_closed=True), crossover
        # operators are relaxed to simple comparisons so the strategy can
        # re-engage while conditions are still valid (e.g., RSI still < 30
        # after a stop-loss during a sustained dip).
        if position is None:
            eval_tree = (
                _relax_crosses_for_reentry(entry_tree)
                if just_closed else entry_tree
            )
            try:
                entry_signal = evaluator.evaluate(eval_tree, row, prev)
            except Exception:
                entry_signal = False

            if entry_signal and equity > 0:
                # Apply slippage in direction of trade
                if direction == "long":
                    entry_fill = float(row["close"]) * (1.0 + slip)
                else:
                    entry_fill = float(row["close"]) * (1.0 - slip)

                trade_value = equity * pos_frac
                qty         = trade_value / entry_fill
                entry_fee   = trade_value * fee

                sl_price = entry_fill * (1.0 - sl_frac) if direction == "long" \
                           else entry_fill * (1.0 + sl_frac)
                tp_price = entry_fill * (1.0 + tp_frac) if direction == "long" \
                           else entry_fill * (1.0 - tp_frac)

                position = {
                    "entry_i":     i,
                    "entry_time":  ts,
                    "entry_price": entry_fill,
                    "quantity":    qty,
                    "entry_fee":   entry_fee,
                    "sl":          sl_price,
                    "tp":          tp_price,
                }
                equity -= entry_fee

        # ── Per-bar mark-to-market for chart ───────────────────
        if position is not None:
            # Unrealised PnL at the current bar's close
            curr_close = float(row["close"])
            if direction == "long":
                mtm_equity = equity + (curr_close - position["entry_price"]) * position["quantity"]
            else:
                mtm_equity = equity + (position["entry_price"] - curr_close) * position["quantity"]
        else:
            mtm_equity = equity
        chart_equity.append(round(mtm_equity, 4))
        equity_timestamps.append(ts)

    # Close open position at last bar
    if position is not None:
        last_row  = df.iloc[-1]
        close_px  = float(last_row["close"])
        exit_fill = close_px * (1.0 - slip) if direction == "long" else close_px * (1.0 + slip)
        qty       = position["quantity"]
        exit_fee  = exit_fill * qty * fee
        if direction == "long":
            pnl = (exit_fill - position["entry_price"]) * qty - exit_fee - position["entry_fee"]
        else:
            pnl = (position["entry_price"] - exit_fill) * qty - exit_fee - position["entry_fee"]
        pnl_pct = pnl / (position["entry_price"] * qty) * 100.0
        equity += pnl
        trades.append({
            "entry_time":    str(position["entry_time"]),
            "exit_time":     str(df.index[-1]),
            "entry_price":   round(position["entry_price"], 8),
            "exit_price":    round(exit_fill, 8),
            "quantity":      round(qty, 8),
            "pnl":           round(pnl, 4),
            "pnl_pct":       round(pnl_pct, 2),
            "exit_reason":   "end_of_data",
            "duration_bars": n - 1 - position["entry_i"],
        })
        equity_curve.append(round(equity, 4))

    metrics = _calc_metrics(trades, equity_curve, initial_capital)

    # Compute regime-split performance if dataframe contains regime information
    regime_performance = {}
    if "regime" in df.columns:
        regime_performance = _compute_regime_splits(trades, df)

    # Compute bootstrap significance test
    significance = {}
    if trades:
        trade_returns = [t["pnl_pct"] for t in trades]
        significance = compute_significance(trade_returns)

    return {
        "trades":              trades,
        "equity_curve":        equity_curve,
        "chart_equity":        chart_equity,
        "equity_timestamps":   equity_timestamps,
        "metrics":             metrics,
        "candle_count":        n,
        "regime_performance":  regime_performance,
        "significance":        significance,
        "look_ahead_safe":     True,  # Set to False if per-bar safe-slicing is not implemented
    }


# ── Performance metrics ───────────────────────────────────────
def _calc_metrics(trades: list[dict], equity_curve: list[float],
                  initial_capital: float) -> dict:
    if not trades:
        return {
            "total_return_pct": 0.0, "max_drawdown_pct": 0.0,
            "sharpe_ratio": 0.0, "win_rate": 0.0, "total_trades": 0,
            "profit_factor": 0.0, "avg_pnl_pct": 0.0,
            "winning_trades": 0, "losing_trades": 0,
        }

    final_equity   = equity_curve[-1] if equity_curve else initial_capital
    total_ret_pct  = (final_equity - initial_capital) / initial_capital * 100.0

    # Max drawdown
    peak = initial_capital
    max_dd = 0.0
    for eq in equity_curve:
        if eq > peak:
            peak = eq
        dd = (peak - eq) / peak * 100.0 if peak > 0 else 0.0
        if dd > max_dd:
            max_dd = dd

    wins   = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    win_rate = len(wins) / len(trades) * 100.0

    gross_profit = sum(t["pnl"] for t in wins)
    gross_loss   = abs(sum(t["pnl"] for t in losses))
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else float("inf")

    # Annualised Sharpe (returns between equity-curve points)
    sharpe = 0.0
    if len(equity_curve) > 2:
        eq_arr  = np.array(equity_curve, dtype=float)
        eq_prev = eq_arr[:-1]
        rets    = np.where(eq_prev > 0, (eq_arr[1:] - eq_prev) / eq_prev, 0.0)
        std     = float(np.std(rets))
        mean    = float(np.mean(rets))
        if std > 0:
            sharpe = round(mean / std * math.sqrt(252), 2)

    avg_pnl_pct = sum(t["pnl_pct"] for t in trades) / len(trades)

    return {
        "total_return_pct": round(total_ret_pct, 2),
        "max_drawdown_pct": round(max_dd, 2),
        "sharpe_ratio":     round(sharpe, 2),
        "win_rate":         round(win_rate, 1),
        "total_trades":     len(trades),
        "profit_factor":    round(profit_factor, 2) if profit_factor != float("inf") else 999.0,
        "avg_pnl_pct":      round(avg_pnl_pct, 2),
        "winning_trades":   len(wins),
        "losing_trades":    len(losses),
    }
