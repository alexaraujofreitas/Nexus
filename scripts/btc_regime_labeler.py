#!/usr/bin/env python3
"""
=============================================================================
BTC Regime Labeling Framework — NexusTrader v1.2
=============================================================================
Scientific regime classification for the 4-year BTC 30m dataset.

Six regimes:
  0  SIDEWAYS      — Low-trend, range-bound, ATR normal
  1  BULL_TREND    — Sustained uptrend, ADX strong, EMA stacked bullish
  2  BEAR_TREND    — Sustained downtrend, ADX strong, EMA stacked bearish
  3  BULL_EXPANSION— Volatility expansion with upward thrust (breakout)
  4  BEAR_EXPANSION— Volatility expansion with downward thrust (breakdown)
  5  CRASH_PANIC   — Extreme rapid drawdown, ATR spike, panic conditions

Usage:
  # Default (uses backtest_data/BTC_USDT_30m_ind.parquet automatically):
  python btc_regime_labeler.py

  # Custom data path:
  python btc_regime_labeler.py --data path/to/BTC_USDT_30m_ind.parquet

  # Custom output directory:
  python btc_regime_labeler.py --output-dir results/regime_v1/

  # Skip hysteresis smoothing:
  python btc_regime_labeler.py --no-smooth

  # Skip multiprocessing (debug mode):
  python btc_regime_labeler.py --no-mp

Output files (all in --output-dir, default: regime_output/):
  btc_regime_labeled.csv      — Full dataset with regime labels + indicators
  regime_distribution.csv/json — Bar counts, %, episode counts, durations
  transition_counts.csv        — Regime-to-regime transition counts
  transition_probs.csv         — Regime-to-regime transition probabilities
  stability_analysis.csv/json  — Whipsaw / short-run analysis per regime
  face_validity.csv/json       — ATR, ADX, forward return stats per regime
  validity_checks.json         — Automated pass/warn/fail audit results
  master_summary.json          — Everything in one file (return this to Claude)

Requirements:
  pip install pandas numpy pyarrow  (all typically already installed)
  Optional: pip install cupy-cuda12x  (for GPU acceleration, negligible speedup)

Hardware note:
  - Runs vectorized on all data in < 5 seconds on modern CPU
  - Multiprocessing used for stability analysis (scales with CPU cores)
  - GPU offers minimal speedup here since bottleneck is pandas EWM, not BLAS
=============================================================================
"""

import argparse
import json
import logging
import multiprocessing as mp
import os
import sys
import time
import warnings
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# SECTION 1 — FORMAL REGIME DEFINITIONS
# ─────────────────────────────────────────────────────────────────────────────
# Regime codes (int8 stored in the output CSV 'regime' column)

REGIMES: Dict[str, int] = {
    "SIDEWAYS":       0,
    "BULL_TREND":     1,
    "BEAR_TREND":     2,
    "BULL_EXPANSION": 3,
    "BEAR_EXPANSION": 4,
    "CRASH_PANIC":    5,
}
REGIME_NAMES: Dict[int, str] = {v: k for k, v in REGIMES.items()}
N_REGIMES = len(REGIMES)

# ─────────────────────────────────────────────────────────────────────────────
# SECTION 2 — LABELING PARAMETERS (all tunable via PARAMS dict)
# ─────────────────────────────────────────────────────────────────────────────

PARAMS: Dict = {
    # ── Trend detection ──────────────────────────────────────────────────────
    # ADX >= adx_trend_min  → trending regime candidate
    # ADX <= adx_range_max  → ranging/sideways candidate
    # Dead zone between the two → use EMA direction as tiebreaker
    "adx_trend_min":  22,
    "adx_range_max":  18,

    # ── Volatility expansion ─────────────────────────────────────────────────
    # ATR > atr_expansion_mult × rolling_baseline → expansion candidate
    # ATR > atr_crash_mult × rolling_baseline     → crash candidate
    "atr_expansion_mult": 1.80,
    "atr_crash_mult":     2.80,
    "atr_baseline_bars":  100,   # rolling mean window for ATR baseline

    # ── Directional return thresholds ────────────────────────────────────────
    # |5d return| > return_expansion_pct → directional expansion
    # 2d return   < -return_crash_2d_pct  → crash candidate (fast crash)
    "return_expansion_pct": 0.06,   # 6% over 5 days (240 bars at 30m)
    "return_crash_2d_pct":  0.08,   # 8% drop over 2 days (96 bars)

    # ── Crash: rolling peak drawdown ─────────────────────────────────────────
    # Drawdown from 24h rolling peak >= crash_peak_dd → crash candidate
    "crash_peak_dd":   0.15,   # 15% from 24h peak
    "crash_peak_bars": 48,     # 24h lookback for rolling peak (48 × 30m)

    # ── EMA settings (pre-computed in _ind parquet, no recomputation needed) ─
    "ema_fast":  20,
    "ema_mid":   50,
    "ema_slow":  200,

    # ── ATR / ADX periods (pre-computed in _ind parquet) ────────────────────
    "atr_col":  "atr_14",   # column name for ATR
    "adx_col":  "adx_14",   # column name for ADX

    # ── Post-labeling hysteresis (anti-whipsaw smoothing) ───────────────────
    # Regimes lasting fewer than hysteresis_bars bars are merged into the
    # surrounding regime.  Set to 1 to disable.
    "hysteresis_bars": 3,
}

# Derived: bars per day at 30m
BARS_PER_DAY = 48
BARS_5D      = 5 * BARS_PER_DAY   # 240 bars
BARS_2D      = 2 * BARS_PER_DAY   # 96 bars


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 3 — INDICATOR COMPUTATION
# ─────────────────────────────────────────────────────────────────────────────

def _ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()


def _atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low  - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(span=period, adjust=False).mean()


def _adx(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    """Vectorized ADX (Wilder smoothed)."""
    ph, pl, pc = high.shift(1), low.shift(1), close.shift(1)
    tr = pd.concat([high - low, (high - pc).abs(), (low - pc).abs()], axis=1).max(axis=1)
    up   = high - ph
    down = pl   - low
    pdm = pd.Series(np.where((up > down) & (up > 0), up, 0.0), index=close.index)
    ndm = pd.Series(np.where((down > up) & (down > 0), down, 0.0), index=close.index)
    tr_s  = tr.ewm(span=period, adjust=False).mean()
    pdi   = 100 * pdm.ewm(span=period, adjust=False).mean() / tr_s.replace(0, np.nan)
    ndi   = 100 * ndm.ewm(span=period, adjust=False).mean() / tr_s.replace(0, np.nan)
    dx    = 100 * (pdi - ndi).abs() / (pdi + ndi).replace(0, np.nan)
    return dx.ewm(span=period, adjust=False).mean().fillna(0)


def prepare_indicators(df: pd.DataFrame, params: Dict) -> pd.DataFrame:
    """
    Attach all regime-labeling indicators to df.
    Prefers pre-computed columns from the _ind parquet where available;
    falls back to computing from OHLCV if missing.
    """
    log.info("Preparing indicators …")
    t0 = time.time()

    close = df["close"]
    high  = df["high"]
    low   = df["low"]

    # ── ATR ──────────────────────────────────────────────────────────────────
    atr_col = params["atr_col"]
    if atr_col in df.columns:
        df["_atr"] = df[atr_col].fillna(0)
        log.info(f"  ATR: using pre-computed column '{atr_col}'")
    elif "atr" in df.columns:
        df["_atr"] = df["atr"].fillna(0)
    else:
        log.info("  ATR: computing from OHLCV …")
        df["_atr"] = _atr(high, low, close, 14).fillna(0)

    # ── ADX ──────────────────────────────────────────────────────────────────
    adx_col = params["adx_col"]
    if adx_col in df.columns:
        df["_adx"] = df[adx_col].fillna(0)
        log.info(f"  ADX: using pre-computed column '{adx_col}'")
    elif "adx" in df.columns:
        df["_adx"] = df["adx"].fillna(0)
    else:
        log.info("  ADX: computing from OHLCV …")
        df["_adx"] = _adx(high, low, close, 14).fillna(0)

    # ── EMAs ─────────────────────────────────────────────────────────────────
    for span, col in [(params["ema_fast"], "ema_20"),
                       (params["ema_mid"],  "ema_50"),
                       (params["ema_slow"], "ema_200")]:
        key = f"_ema{span}"
        if col in df.columns:
            df[key] = df[col].fillna(method="ffill")
        else:
            df[key] = _ema(close, span)

    # ── ATR ratio (current / rolling baseline) ───────────────────────────────
    baseline = df["_atr"].rolling(params["atr_baseline_bars"], min_periods=10).mean()
    df["_atr_ratio"] = (df["_atr"] / baseline.replace(0, np.nan)).fillna(1.0).clip(0, 20)

    # ── Rolling returns ───────────────────────────────────────────────────────
    df["_ret_5d"] = close.pct_change(BARS_5D)
    df["_ret_2d"] = close.pct_change(BARS_2D)

    # ── Rolling peak drawdown (for crash detection) ───────────────────────────
    rolling_peak   = close.rolling(params["crash_peak_bars"], min_periods=1).max()
    df["_peak_dd"] = ((rolling_peak - close) / rolling_peak.replace(0, np.nan)).fillna(0).clip(0, 1)

    # ── EMA direction flags ───────────────────────────────────────────────────
    df["_ema_bull"]         = (df["_ema20"]  > df["_ema50"]).astype(np.int8)
    df["_price_above_slow"] = (close          > df["_ema200"]).astype(np.int8)

    log.info(f"  Indicators ready in {time.time()-t0:.2f}s")
    return df


# ─────────────────────────────────────────────────────────────────────────────
# REGIME LABELING — PRIORITY-ORDERED VECTORISED LOGIC
# ─────────────────────────────────────────────────────────────────────────────
# Priority (highest overrides lowest):
#   5  CRASH_PANIC      — extreme drawdown AND ATR spike
#   4  BEAR_EXPANSION   — rapid down-thrust AND ATR expansion (not crash level)
#   3  BULL_EXPANSION   — rapid up-thrust AND ATR expansion
#   2  BULL_TREND       — ADX strong + EMA stacked bullish + low vol
#   1  BEAR_TREND       — ADX strong + EMA stacked bearish + low vol
#   0  SIDEWAYS         — everything else (default)

def label_regimes(df: pd.DataFrame, params: Dict) -> np.ndarray:
    """
    Return int8 array of regime labels (length = len(df)).
    Operates entirely on numpy arrays for maximum speed.
    """
    n = len(df)

    atr_r    = df["_atr_ratio"].values
    adx      = df["_adx"].values
    ret5d    = df["_ret_5d"].values
    ret2d    = df["_ret_2d"].values
    peak_dd  = df["_peak_dd"].values
    ema_bull = df["_ema_bull"].values
    abv_slow = df["_price_above_slow"].values

    adx_trend = params["adx_trend_min"]
    adx_range = params["adx_range_max"]
    atr_exp   = params["atr_expansion_mult"]
    atr_crsh  = params["atr_crash_mult"]
    ret_exp   = params["return_expansion_pct"]
    ret_crsh  = params["return_crash_2d_pct"]
    crash_dd  = params["crash_peak_dd"]

    # Default: SIDEWAYS = 0
    labels = np.zeros(n, dtype=np.int8)

    # 1. BULL_TREND
    bull_trend = (
        (adx >= adx_trend) &
        (ema_bull == 1) &
        (abv_slow == 1) &
        (atr_r < atr_exp)
    )
    labels[bull_trend] = REGIMES["BULL_TREND"]

    # 2. BEAR_TREND
    bear_trend = (
        (adx >= adx_trend) &
        (ema_bull == 0) &
        (abv_slow == 0) &
        (atr_r < atr_exp)
    )
    labels[bear_trend] = REGIMES["BEAR_TREND"]

    # 3. BULL_EXPANSION (overrides trend labels)
    valid5d = ~np.isnan(ret5d)
    bull_exp = valid5d & (atr_r >= atr_exp) & (ret5d >= ret_exp)
    labels[bull_exp] = REGIMES["BULL_EXPANSION"]

    # 4. BEAR_EXPANSION (overrides trend labels, but not crash)
    bear_exp = valid5d & (atr_r >= atr_exp) & (ret5d <= -ret_exp) & (peak_dd < crash_dd)
    labels[bear_exp] = REGIMES["BEAR_EXPANSION"]

    # 5. CRASH/PANIC — highest priority, overrides everything
    valid2d = ~np.isnan(ret2d)
    crash = valid2d & (
        ((peak_dd >= crash_dd) | (ret2d <= -ret_crsh)) &
        (atr_r >= atr_crsh)
    )
    labels[crash] = REGIMES["CRASH_PANIC"]

    return labels


def apply_hysteresis(labels: np.ndarray, min_bars: int) -> np.ndarray:
    """
    Merge short-lived regime spikes back into the surrounding regime.
    Any contiguous run of identical regime labels that is shorter than
    min_bars is replaced by the preceding label.  Two passes handle
    back-to-back short runs.
    """
    if min_bars <= 1:
        return labels.copy()

    out = labels.copy()
    n   = len(out)

    for _ in range(2):   # two passes to catch consecutive short bursts
        i = 0
        while i < n:
            # find end of current run
            j = i + 1
            while j < n and out[j] == out[i]:
                j += 1
            run_len = j - i
            if run_len < min_bars and i > 0:
                # replace this short run with the preceding regime
                out[i:j] = out[i - 1]
            i = j

    return out


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 4 — APPLY TO FULL DATASET
# ─────────────────────────────────────────────────────────────────────────────

def load_data(path: str) -> pd.DataFrame:
    """Load OHLCV (or OHLCV+indicators) file.  Supports .parquet and .csv."""
    p = Path(path)
    if not p.exists():
        log.error(f"Data file not found: {path}")
        sys.exit(1)

    log.info(f"Loading: {path}")
    t0 = time.time()

    if p.suffix == ".parquet":
        df = pd.read_parquet(p)
    else:
        df = pd.read_csv(p)
        # Normalise column names
        rename = {}
        for c in df.columns:
            cl = c.lower().strip()
            for target, aliases in [
                ("timestamp", ("timestamp", "time", "datetime", "date", "open_time", "dt")),
                ("open",  ("open", "o")),
                ("high",  ("high", "h")),
                ("low",   ("low",  "l")),
                ("close", ("close","c","price")),
                ("volume",("volume","vol","v")),
            ]:
                if cl in aliases:
                    rename[c] = target
        df.rename(columns=rename, inplace=True)
        if "timestamp" in df.columns:
            df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True, errors="coerce")
            df.set_index("timestamp", inplace=True)

    # Ensure DatetimeIndex
    if not isinstance(df.index, pd.DatetimeIndex):
        for candidate in ("timestamp", "dt", "date", "time"):
            if candidate in df.columns:
                df[candidate] = pd.to_datetime(df[candidate], utc=True, errors="coerce")
                df.set_index(candidate, inplace=True)
                break

    df.sort_index(inplace=True)
    df = df[~df.index.duplicated(keep="first")]

    for col in ("open", "high", "low", "close"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    df.dropna(subset=["close", "high", "low"], inplace=True)

    log.info(f"  {len(df):,} bars | {df.index.min()} → {df.index.max()} "
             f"({time.time()-t0:.2f}s)")
    return df


def run_labeling(data_path: str, params: Dict, smooth: bool = True) -> pd.DataFrame:
    """Full pipeline: load → indicators → label → smooth."""
    df = load_data(data_path)
    df = prepare_indicators(df, params)

    log.info("Labeling regimes …")
    raw    = label_regimes(df, params)
    smooth_labels = apply_hysteresis(raw, params["hysteresis_bars"]) if smooth else raw.copy()

    df["regime_raw"]  = raw
    df["regime"]      = smooth_labels
    df["regime_name"] = [REGIME_NAMES[r] for r in smooth_labels]

    log.info(f"Labeling complete.  Regime distribution (raw):")
    unique, counts = np.unique(raw, return_counts=True)
    for u, c in zip(unique, counts):
        log.info(f"  {REGIME_NAMES[u]:<20} {c:>6,} bars  ({c/len(raw)*100:.1f}%)")

    return df


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 5 — DISTRIBUTION & TRANSITION ANALYSIS
# ─────────────────────────────────────────────────────────────────────────────

def _get_runs(arr: np.ndarray, regime_id: int) -> np.ndarray:
    """Return array of run-lengths for a given regime in label array."""
    mask   = (arr == regime_id).astype(np.int8)
    # Find start/end of each run via diff
    edges  = np.diff(np.concatenate([[0], mask, [0]]))
    starts = np.where(edges ==  1)[0]
    ends   = np.where(edges == -1)[0]
    return ends - starts  # run lengths


def compute_distribution(df: pd.DataFrame) -> Dict:
    labels     = df["regime"].values
    total      = len(labels)
    HALF_HOUR  = 0.5   # 30m → hours

    result = {}
    for rid, rname in REGIME_NAMES.items():
        runs = _get_runs(labels, rid)
        n_bars = int((labels == rid).sum())
        result[rname] = {
            "bar_count":           n_bars,
            "pct":                 round(n_bars / total * 100, 2),
            "n_episodes":          int(len(runs)),
            "avg_duration_bars":   round(float(runs.mean()), 1)   if len(runs) else 0,
            "median_duration_bars":round(float(np.median(runs)),1)if len(runs) else 0,
            "max_duration_bars":   int(runs.max())                 if len(runs) else 0,
            "avg_duration_hours":  round(float(runs.mean())   * HALF_HOUR, 1) if len(runs) else 0,
            "median_duration_hours":round(float(np.median(runs))* HALF_HOUR,1)if len(runs) else 0,
            "max_duration_hours":  round(float(runs.max())    * HALF_HOUR, 1) if len(runs) else 0,
        }
    return result


def compute_transition_matrix(labels: np.ndarray) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """Regime-to-regime transition counts and probabilities."""
    counts = np.zeros((N_REGIMES, N_REGIMES), dtype=np.int64)
    # Vectorised transition detection
    from_idx = labels[:-1]
    to_idx   = labels[1:]
    mask     = from_idx != to_idx   # only actual transitions
    for f, t in zip(from_idx[mask], to_idx[mask]):
        counts[f, t] += 1

    row_sums = counts.sum(axis=1, keepdims=True)
    probs    = np.divide(counts, row_sums,
                         where=row_sums > 0,
                         out=np.zeros_like(counts, dtype=float))

    names = [REGIME_NAMES[i] for i in range(N_REGIMES)]
    return (
        pd.DataFrame(counts, index=names, columns=names),
        pd.DataFrame(np.round(probs, 4), index=names, columns=names),
    )


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 6 — STABILITY / WHIPSAW ANALYSIS  (multiprocessing)
# ─────────────────────────────────────────────────────────────────────────────

def _stability_worker(args: Tuple) -> Tuple[int, Dict]:
    regime_id, labels, thresholds = args
    runs = _get_runs(labels, regime_id)
    if len(runs) == 0:
        return regime_id, {"total_runs": 0, "total_bars": 0}

    result = {
        "total_runs":  int(len(runs)),
        "total_bars":  int(runs.sum()),
        "min_run_bars":int(runs.min()),
        "max_run_bars":int(runs.max()),
        "mean_run_bars":round(float(runs.mean()), 2),
    }
    for thr in thresholds:
        n_short = int((runs < thr).sum())
        result[f"runs_lt_{thr}bars"]     = n_short
        result[f"runs_lt_{thr}bars_pct"] = round(n_short / len(runs) * 100, 1)
    return regime_id, result


def compute_stability(labels: np.ndarray, use_mp: bool = True) -> Dict:
    thresholds = [2, 4, 8, 12, 24, 48]
    args       = [(rid, labels, thresholds) for rid in range(N_REGIMES)]

    if use_mp and N_REGIMES > 1:
        n_proc = min(N_REGIMES, max(1, mp.cpu_count() - 1))
        log.info(f"Stability analysis: {n_proc} workers …")
        with mp.Pool(processes=n_proc) as pool:
            raw = pool.map(_stability_worker, args)
    else:
        raw = [_stability_worker(a) for a in args]

    return {REGIME_NAMES[rid]: data for rid, data in raw}


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 7 — FACE-VALIDITY AUDIT
# ─────────────────────────────────────────────────────────────────────────────

def compute_face_validity(df: pd.DataFrame) -> Dict:
    """
    Per-regime:
    - Characteristic indicators (ATR ratio, ADX, peak_dd)
    - 1-bar and 48-bar forward return statistics
    """
    labels = df["regime"].values
    close  = df["close"].values
    atr_r  = df["_atr_ratio"].values
    adx_v  = df["_adx"].values
    pkdd   = df["_peak_dd"].values

    fwd_1  = np.empty(len(close)); fwd_1[:] = np.nan
    fwd_1[:-1] = (close[1:] - close[:-1]) / close[:-1]

    fwd_48 = np.empty(len(close)); fwd_48[:] = np.nan
    if len(close) > 48:
        fwd_48[:-48] = (close[48:] - close[:-48]) / close[:-48]

    result = {}
    for rid, rname in REGIME_NAMES.items():
        mask = labels == rid
        if not mask.any():
            result[rname] = {}
            continue

        f1   = fwd_1[mask]
        f1   = f1[~np.isnan(f1)]
        f48  = fwd_48[mask]
        f48  = f48[~np.isnan(f48)]

        result[rname] = {
            # Regime fingerprint
            "avg_atr_ratio":   round(float(atr_r[mask].mean()), 3),
            "p95_atr_ratio":   round(float(np.percentile(atr_r[mask], 95)), 3),
            "avg_adx":         round(float(adx_v[mask].mean()), 1),
            "avg_peak_dd":     round(float(pkdd[mask].mean()), 4),
            "max_peak_dd":     round(float(pkdd[mask].max()), 4),
            "volatility_tier": (
                "HIGH"   if atr_r[mask].mean() > 1.8 else
                "MEDIUM" if atr_r[mask].mean() > 0.9 else
                "LOW"
            ),
            # 1-bar forward returns (bps)
            "fwd_1bar_n":        int(len(f1)),
            "fwd_1bar_mean_bps": round(float(f1.mean()   * 10000), 2) if len(f1) else 0,
            "fwd_1bar_std_bps":  round(float(f1.std()    * 10000), 2) if len(f1) else 0,
            # 48-bar (24h) forward returns
            "fwd_48bar_n":          int(len(f48)),
            "fwd_48bar_mean_pct":   round(float(f48.mean()  * 100), 3) if len(f48) else 0,
            "fwd_48bar_std_pct":    round(float(f48.std()   * 100), 3) if len(f48) else 0,
            "fwd_48bar_pos_pct":    round(float((f48 > 0).mean() * 100), 1) if len(f48) else 0,
            "fwd_48bar_sharpe":     (
                round(float(f48.mean() / f48.std() * np.sqrt(BARS_PER_DAY * 365)), 2)
                if len(f48) > 1 and f48.std() > 0 else 0.0
            ),
        }
    return result


def run_validity_checks(
    dist:  Dict,
    fv:    Dict,
    stab:  Dict,
) -> List[Dict]:
    """
    10 automated face-validity checks.
    Each returns: {"check", "result": PASS/WARN/FAIL, "detail"}
    """
    checks = []

    def chk(name, result, detail):
        checks.append({"check": name, "result": result, "detail": detail})

    # 1. SIDEWAYS should be the most common regime (BTC is mostly ranging)
    sw_pct = dist.get("SIDEWAYS", {}).get("pct", 0)
    chk("SIDEWAYS is dominant regime (≥35%)",
        "PASS" if sw_pct >= 35 else "WARN" if sw_pct >= 20 else "FAIL",
        f"SIDEWAYS = {sw_pct:.1f}%  (expected ≥ 35%)")

    # 2. CRASH/PANIC should be rare
    cr_pct = dist.get("CRASH_PANIC", {}).get("pct", 0)
    chk("CRASH_PANIC is rare (≤ 5%)",
        "PASS" if cr_pct <= 5 else "WARN" if cr_pct <= 10 else "FAIL",
        f"CRASH_PANIC = {cr_pct:.1f}%  (expected ≤ 5%)")

    # 3. BULL_TREND has positive 48h forward return
    bt_fwd = fv.get("BULL_TREND",  {}).get("fwd_48bar_mean_pct", 0)
    chk("BULL_TREND positive 48h fwd return",
        "PASS" if bt_fwd > 0 else "FAIL",
        f"BULL_TREND 48h fwd = {bt_fwd:.2f}%")

    # 4. BEAR_TREND has negative 48h forward return
    btr_fwd = fv.get("BEAR_TREND", {}).get("fwd_48bar_mean_pct", 0)
    chk("BEAR_TREND negative 48h fwd return",
        "PASS" if btr_fwd < 0 else "FAIL",
        f"BEAR_TREND 48h fwd = {btr_fwd:.2f}%")

    # 5. CRASH/PANIC has highest ATR ratio
    cr_atr  = fv.get("CRASH_PANIC",    {}).get("avg_atr_ratio", 0)
    bt_atr  = fv.get("BULL_TREND",     {}).get("avg_atr_ratio", 1)
    btr_atr = fv.get("BEAR_TREND",     {}).get("avg_atr_ratio", 1)
    max_others = max(bt_atr, btr_atr, 0.01)
    chk("CRASH_PANIC has highest ATR ratio",
        "PASS" if cr_atr > max_others * 1.5 else "WARN",
        f"CRASH ATR = {cr_atr:.3f}  vs BULL_TREND = {bt_atr:.3f}")

    # 6. BULL_EXPANSION has higher 48h fwd than SIDEWAYS
    be_fwd  = fv.get("BULL_EXPANSION", {}).get("fwd_48bar_mean_pct", 0)
    sw_fwd  = fv.get("SIDEWAYS",       {}).get("fwd_48bar_mean_pct", 0)
    chk("BULL_EXPANSION 48h fwd > SIDEWAYS",
        "PASS" if be_fwd > sw_fwd else "WARN",
        f"BULL_EXP = {be_fwd:.2f}%  SIDEWAYS = {sw_fwd:.2f}%")

    # 7. BEAR_EXPANSION has lower 48h fwd than SIDEWAYS
    bae_fwd = fv.get("BEAR_EXPANSION", {}).get("fwd_48bar_mean_pct", 0)
    chk("BEAR_EXPANSION 48h fwd < SIDEWAYS",
        "PASS" if bae_fwd < sw_fwd else "WARN",
        f"BEAR_EXP = {bae_fwd:.2f}%  SIDEWAYS = {sw_fwd:.2f}%")

    # 8. All 6 regimes present
    n_present = sum(1 for v in dist.values() if v.get("bar_count", 0) > 100)
    chk("All 6 regimes well-represented (≥ 100 bars each)",
        "PASS" if n_present == N_REGIMES else "WARN",
        f"{n_present}/{N_REGIMES} regimes have ≥ 100 bars")

    # 9. SIDEWAYS whipsaw rate acceptable (< 40% of runs shorter than 4 bars)
    sw_stab = stab.get("SIDEWAYS", {})
    sw_ws   = sw_stab.get("runs_lt_4bars_pct", 100)
    chk("SIDEWAYS whipsaw rate < 40% (runs < 4 bars)",
        "PASS" if sw_ws < 30 else "WARN" if sw_ws < 50 else "FAIL",
        f"SIDEWAYS runs < 4 bars = {sw_ws:.1f}% of all SIDEWAYS runs")

    # 10. Regime coverage sums to 100%
    total_pct = sum(v.get("pct", 0) for v in dist.values())
    chk("All regimes sum to 100%",
        "PASS" if abs(total_pct - 100) < 0.5 else "FAIL",
        f"Sum = {total_pct:.1f}%")

    return checks


# ─────────────────────────────────────────────────────────────────────────────
# OUTPUT
# ─────────────────────────────────────────────────────────────────────────────

def save_outputs(
    df:       pd.DataFrame,
    dist:     Dict,
    cnt_mtx:  pd.DataFrame,
    prb_mtx:  pd.DataFrame,
    stab:     Dict,
    fv:       Dict,
    checks:   List[Dict],
    out_dir:  str,
) -> Dict:
    os.makedirs(out_dir, exist_ok=True)
    log.info(f"Writing outputs → {os.path.abspath(out_dir)}")

    # 1. Labelled dataset
    keep_cols = [c for c in [
        "open", "high", "low", "close", "volume",
        "regime", "regime_name", "regime_raw",
        "_adx", "_atr_ratio", "_ema20", "_ema50", "_ema200",
        "_ret_5d", "_ret_2d", "_peak_dd",
    ] if c in df.columns]
    df[keep_cols].to_csv(f"{out_dir}/btc_regime_labeled.csv")
    log.info("  ✓ btc_regime_labeled.csv")

    # 2. Distribution
    pd.DataFrame(dist).T.to_csv(f"{out_dir}/regime_distribution.csv")
    with open(f"{out_dir}/regime_distribution.json", "w") as f:
        json.dump(dist, f, indent=2)
    log.info("  ✓ regime_distribution.csv / .json")

    # 3. Transition matrices
    cnt_mtx.to_csv(f"{out_dir}/transition_counts.csv")
    prb_mtx.to_csv(f"{out_dir}/transition_probs.csv")
    log.info("  ✓ transition_counts.csv / transition_probs.csv")

    # 4. Stability
    pd.DataFrame(stab).T.to_csv(f"{out_dir}/stability_analysis.csv")
    with open(f"{out_dir}/stability_analysis.json", "w") as f:
        json.dump(stab, f, indent=2)
    log.info("  ✓ stability_analysis.csv / .json")

    # 5. Face validity
    pd.DataFrame(fv).T.to_csv(f"{out_dir}/face_validity.csv")
    with open(f"{out_dir}/face_validity.json", "w") as f:
        json.dump(fv, f, indent=2)
    log.info("  ✓ face_validity.csv / .json")

    # 6. Validity checks
    checks_summary = {
        "total": len(checks),
        "pass":  sum(1 for c in checks if c["result"] == "PASS"),
        "warn":  sum(1 for c in checks if c["result"] == "WARN"),
        "fail":  sum(1 for c in checks if c["result"] == "FAIL"),
        "checks": checks,
    }
    with open(f"{out_dir}/validity_checks.json", "w") as f:
        json.dump(checks_summary, f, indent=2)
    log.info("  ✓ validity_checks.json")

    # 7. Master summary (return this to Claude for Section 8)
    master = {
        "dataset": {
            "total_bars": len(df),
            "date_start": str(df.index.min()),
            "date_end":   str(df.index.max()),
            "timeframe":  "30m",
            "asset":      "BTC",
        },
        "params":       PARAMS,
        "distribution": dist,
        "transition_probs": prb_mtx.to_dict(),
        "stability": {
            k: {
                "total_runs":        v.get("total_runs", 0),
                "mean_run_bars":     v.get("mean_run_bars", 0),
                "whipsaw_pct_4bar":  v.get("runs_lt_4bars_pct", 0),
                "whipsaw_pct_8bar":  v.get("runs_lt_8bars_pct", 0),
                "whipsaw_pct_24bar": v.get("runs_lt_24bars_pct", 0),
            }
            for k, v in stab.items()
        },
        "face_validity":   fv,
        "validity_checks": checks_summary,
    }
    with open(f"{out_dir}/master_summary.json", "w") as f:
        json.dump(master, f, indent=2, default=str)
    log.info("  ✓ master_summary.json  ← RETURN THIS TO CLAUDE")

    return master


def print_report(dist: Dict, stab: Dict, fv: Dict, checks: List[Dict]):
    W = 72
    print("\n" + "=" * W)
    print(" BTC REGIME LABELING FRAMEWORK — RESULTS".center(W))
    print("=" * W)

    print("\n▌ SECTION 4 — DISTRIBUTION")
    hdr = f"{'Regime':<20} {'Bars':>8} {'%':>6}  {'Episodes':>9}  {'Avg (h)':>8}  {'Max (h)':>8}"
    print(hdr)
    print("-" * W)
    for rname, d in dist.items():
        print(f"{rname:<20} {d['bar_count']:>8,} {d['pct']:>5.1f}%  "
              f"{d['n_episodes']:>9,}  {d['avg_duration_hours']:>8.1f}  "
              f"{d['max_duration_hours']:>8.1f}")

    print("\n▌ SECTION 6 — WHIPSAW ANALYSIS")
    hdr2 = f"{'Regime':<20} {'Runs':>7}  {'<4bar%':>7}  {'<8bar%':>7}  {'<24bar%':>8}"
    print(hdr2)
    print("-" * W)
    for rname, s in stab.items():
        print(f"{rname:<20} {s.get('total_runs',0):>7,}  "
              f"{s.get('runs_lt_4bars_pct',0):>6.1f}%  "
              f"{s.get('runs_lt_8bars_pct',0):>6.1f}%  "
              f"{s.get('runs_lt_24bars_pct',0):>7.1f}%")

    print("\n▌ SECTION 7 — FACE-VALIDITY METRICS")
    hdr3 = (f"{'Regime':<20} {'ATR×':>7}  {'ADX':>5}  "
            f"{'48hFwd%':>8}  {'Pos%':>6}  {'Sharpe':>7}")
    print(hdr3)
    print("-" * W)
    for rname, fv_r in fv.items():
        if not fv_r:
            continue
        print(f"{rname:<20} {fv_r.get('avg_atr_ratio',0):>7.3f}  "
              f"{fv_r.get('avg_adx',0):>5.1f}  "
              f"{fv_r.get('fwd_48bar_mean_pct',0):>8.2f}%  "
              f"{fv_r.get('fwd_48bar_pos_pct',0):>5.1f}%  "
              f"{fv_r.get('fwd_48bar_sharpe',0):>7.2f}")

    print("\n▌ SECTION 7 — AUTOMATED CHECKS")
    icons = {"PASS": "✓", "WARN": "⚠", "FAIL": "✗"}
    for c in checks:
        icon = icons.get(c["result"], "?")
        print(f"  [{c['result']:4s}] {icon}  {c['check']}")
        print(f"          → {c['detail']}")

    n_p = sum(1 for c in checks if c["result"] == "PASS")
    n_w = sum(1 for c in checks if c["result"] == "WARN")
    n_f = sum(1 for c in checks if c["result"] == "FAIL")
    print(f"\n  Summary: {n_p} PASS  /  {n_w} WARN  /  {n_f} FAIL")
    print("=" * W)
    print("\n  → Return  regime_output/master_summary.json  to Claude")
    print("    for Section 8 Final Verdict + strategy recommendations.")
    print("=" * W + "\n")


# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────

def default_data_path() -> str:
    """Try to find the backtest_data parquet next to this script."""
    here = Path(__file__).parent
    candidates = [
        here.parent / "backtest_data" / "BTC_USDT_30m_ind.parquet",
        here.parent / "backtest_data" / "BTC_USDT_30m.parquet",
        here        / "backtest_data" / "BTC_USDT_30m_ind.parquet",
    ]
    for c in candidates:
        if c.exists():
            return str(c)
    return ""


def main():
    parser = argparse.ArgumentParser(
        description="BTC Regime Labeling Framework — NexusTrader v1.2",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    parser.add_argument(
        "--data", default="",
        help="Path to BTC OHLCV (or OHLCV+indicators) parquet/CSV.\n"
             "Default: auto-detected from scripts/../backtest_data/",
    )
    parser.add_argument(
        "--output-dir", default="regime_output",
        help="Directory for all output files (default: regime_output/)",
    )
    parser.add_argument(
        "--no-smooth", action="store_true",
        help="Disable hysteresis smoothing (keep raw labels)",
    )
    parser.add_argument(
        "--no-mp", action="store_true",
        help="Disable multiprocessing (serial mode, useful for debugging)",
    )
    parser.add_argument(
        "--adx-trend", type=float, default=None,
        help="Override adx_trend_min parameter",
    )
    parser.add_argument(
        "--atr-exp", type=float, default=None,
        help="Override atr_expansion_mult parameter",
    )
    args = parser.parse_args()

    # Parameter overrides
    if args.adx_trend is not None:
        PARAMS["adx_trend_min"] = args.adx_trend
        log.info(f"Override: adx_trend_min = {args.adx_trend}")
    if args.atr_exp is not None:
        PARAMS["atr_expansion_mult"] = args.atr_exp
        log.info(f"Override: atr_expansion_mult = {args.atr_exp}")

    # Resolve data path
    data_path = args.data or default_data_path()
    if not data_path:
        log.error(
            "No data file found.  Pass --data path/to/BTC_USDT_30m_ind.parquet\n"
            "Expected location: <project_root>/backtest_data/BTC_USDT_30m_ind.parquet"
        )
        sys.exit(1)

    t_start = time.time()
    log.info(f"CPU cores: {mp.cpu_count()}")
    log.info(f"Parameters: adx_trend={PARAMS['adx_trend_min']}  "
             f"atr_exp={PARAMS['atr_expansion_mult']}  "
             f"crash_dd={PARAMS['crash_peak_dd']}  "
             f"hysteresis={PARAMS['hysteresis_bars']}bars")

    # ── Pipeline ──────────────────────────────────────────────────────────────
    df       = run_labeling(data_path, PARAMS, smooth=not args.no_smooth)
    dist     = compute_distribution(df)
    cnt_mtx, prb_mtx = compute_transition_matrix(df["regime"].values)
    stab     = compute_stability(df["regime"].values, use_mp=not args.no_mp)
    fv       = compute_face_validity(df)
    checks   = run_validity_checks(dist, fv, stab)
    master   = save_outputs(df, dist, cnt_mtx, prb_mtx, stab, fv, checks, args.output_dir)

    print_report(dist, stab, fv, checks)
    log.info(f"Total runtime: {time.time()-t_start:.1f}s")
    log.info(f"Outputs in:    {os.path.abspath(args.output_dir)}/")


if __name__ == "__main__":
    mp.freeze_support()   # Windows safety guard
    main()
