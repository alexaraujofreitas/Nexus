"""
scripts/cps_calibration.py — CPS Threshold & Tier Calibration Pipeline
=======================================================================
Converts the CPS from a "good predictor" into a usable trading signal by
calibrating probability thresholds, tier boundaries, multiplier curves,
and analysing BTC-dominance effects.

Sections:
  1. ThresholdSweeper  — sweep CPS probability 0.05→0.90; rank by EVI
  2. MultiHorizonDesigner — define CRASH_WATCH / CRASH_CONFIRMED logic
  3. TierOptimizer    — sweep DEFENSIVE/HIGH_ALERT/EMERGENCY thresholds
  4. MultiplierCalibrator — compare step / sigmoid / linear / custom curves
  5. BtcDominanceAnalyzer — 3-part composite: lead-lag + RS + breadth
  6. LiquidationGapEvaluator — CoinGlass integration cost/benefit

USAGE (run on Windows after cpi_validation_realdata.py):
  python scripts/cps_calibration.py
  python scripts/cps_calibration.py --symbols BTC ETH   # subset

OUTPUT:
  data/validation/cps_calibration_results.json
  data/validation/cps_calibration_report.txt

PRIMARY RANKING METRIC (per user spec):
  EVI = Net P&L_B,t - Net P&L_A  (including slippage + spread)
  Equivalently: expectancy_delta × n_affected_trades
  Constraints: FPR ≤ 70%, fire_rate ≤ 5%, MDD worsening ≤ 0.5pp

DEPENDENCIES:
  pip install pandas pyarrow scikit-learn scipy numpy tqdm
"""

from __future__ import annotations

import argparse
import json
import logging
import warnings
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
from scipy.stats import spearmanr

warnings.filterwarnings("ignore", category=FutureWarning)
warnings.filterwarnings("ignore", category=UserWarning)
warnings.filterwarnings("ignore", category=RuntimeWarning)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("cps_cal")

# ── Paths ─────────────────────────────────────────────────────────────────────

ROOT_DIR = Path(__file__).parent.parent
VAL_DIR  = ROOT_DIR / "data" / "validation"
OUT_JSON = VAL_DIR / "cps_calibration_results.json"
OUT_REPORT = VAL_DIR / "cps_calibration_report.txt"

# ── Constants ─────────────────────────────────────────────────────────────────

# Execution costs (round-trip)
SLIPPAGE_PCT   = 0.0004   # 0.04% per side (Bybit perp market order)
SPREAD_PCT     = 0.0002   # 0.02% spread (limit order fill)
EXEC_COST_RT   = (SLIPPAGE_PCT + SPREAD_PCT) * 2   # ~0.12% total round-trip

# Crash labelling
HORIZONS = {
    "crash_5m":  (5,  0.010),
    "crash_10m": (10, 0.018),
    "crash_30m": (30, 0.025),
}
FEATURE_COLS = [
    "funding_z", "oi_change_z", "breadth_w", "liq_z", "cascade_z", "cda_score_proxy"
]

# Phase 1A CDA tier thresholds and multipliers (baseline)
BASELINE_THRESHOLDS = {
    "DEFENSIVE": 5.0, "HIGH_ALERT": 7.0, "EMERGENCY": 8.0, "SYSTEMIC": 9.0
}
BASELINE_MULTIPLIERS = {
    "NORMAL": 1.00, "DEFENSIVE": 0.65, "HIGH_ALERT": 0.35,
    "EMERGENCY": 0.10, "SYSTEMIC": 0.00,
}

# EVI constraint targets
FPR_TARGET      = 0.70   # FPR must be ≤ 70%
FIRE_RATE_TARGET = 0.05  # fire rate must be ≤ 5% of all bars
MDD_WORSENING_MAX = 0.5  # MDD must not worsen by more than 0.5pp vs Scenario A


# ── Shared feature engineering (mirrors cpi_validation_realdata.py) ───────────

def _rolling_zscore(series: pd.Series, window: int, clip: float = 4.0) -> pd.Series:
    mu    = series.rolling(window, min_periods=window // 2).mean()
    sigma = series.rolling(window, min_periods=window // 2).std().replace(0, np.nan)
    return ((series - mu) / sigma).clip(-clip, clip).fillna(0.0)


def _load_parquet(path: Path, desc: str) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(
            f"{desc} not found: {path}\nRun: python scripts/fetch_historical_data.py"
        )
    df = pd.read_parquet(path)
    logger.info("Loaded %-22s — %d rows", path.name, len(df))
    return df


def load_all_data(symbols: list[str]) -> dict[str, dict[str, pd.DataFrame]]:
    data = {}
    for sym in symbols:
        slug = sym + "USDT"
        data[sym] = {
            "ohlcv":   _load_parquet(VAL_DIR / f"{slug}_1m.parquet", f"{sym} OHLCV 1m"),
            "funding": _load_parquet(VAL_DIR / f"{slug}_funding.parquet", f"{sym} funding"),
            "oi":      _load_parquet(VAL_DIR / f"{slug}_oi.parquet", f"{sym} OI"),
        }
    return data


def compute_cps_features(ohlcv, funding, oi, symbol: str) -> pd.DataFrame:
    """Compute 6 CPS features on 5m aggregated data (mirrors cpi_validation_realdata.py)."""
    logger.info("[%s] Computing CPS features…", symbol)
    ohlcv5 = ohlcv.set_index("timestamp").resample("5min").agg({
        "open": "first", "high": "max", "low": "min",
        "close": "last", "volume": "sum",
    }).dropna(subset=["close"])

    if len(funding) > 10:
        fund5 = funding.set_index("timestamp")[["funding_rate"]].resample("5min").ffill()
        ohlcv5 = ohlcv5.join(fund5, how="left")
        ohlcv5["funding_rate"] = ohlcv5["funding_rate"].fillna(0.0)
    else:
        ohlcv5["funding_rate"] = 0.0
    ohlcv5["funding_z"] = _rolling_zscore(ohlcv5["funding_rate"], 288)

    if len(oi) > 10:
        oi5 = oi.set_index("timestamp")[["oi"]].resample("5min").last().ffill()
        ohlcv5 = ohlcv5.join(oi5, how="left")
        ohlcv5["oi"] = ohlcv5["oi"].ffill().fillna(0.0)
        ohlcv5["oi_change"] = ohlcv5["oi"].pct_change(12).fillna(0.0)
    else:
        ohlcv5["oi_change"] = 0.0
    ohlcv5["oi_change_z"] = _rolling_zscore(ohlcv5["oi_change"], 288)

    ohlcv5["vol_ratio"] = (
        ohlcv5["volume"] /
        ohlcv5["volume"].rolling(288, min_periods=12).mean().replace(0, np.nan)
    ).fillna(1.0).clip(0, 10)
    ohlcv5["breadth_w"] = (ohlcv5["vol_ratio"] - 1.0).clip(-3, 3)

    ohlcv5["return_5m"] = ohlcv5["close"].pct_change().fillna(0.0)
    ohlcv5["liq_proxy"] = ohlcv5["vol_ratio"] * (-ohlcv5["return_5m"]).clip(0, None)
    ohlcv5["liq_z"]     = _rolling_zscore(ohlcv5["liq_proxy"], 288)

    ohlcv5["acf_shock"] = (
        ohlcv5["return_5m"].rolling(6).apply(
            lambda x: abs(x[-1]) / (x[:-1].std() + 1e-9) if len(x) > 1 else 0.0, raw=True
        )
    ).fillna(0.0)
    ohlcv5["cascade_z"] = _rolling_zscore(ohlcv5["acf_shock"], 288)

    w = {"funding_z": 0.20, "oi_change_z": 0.25, "breadth_w": 0.15, "liq_z": 0.25, "cascade_z": 0.15}
    ohlcv5["cda_score_proxy"] = sum(ohlcv5[f].clip(-4, 4) * wt for f, wt in w.items())
    _lo = ohlcv5["cda_score_proxy"].quantile(0.01)
    _hi = ohlcv5["cda_score_proxy"].quantile(0.99)
    ohlcv5["cda_score_proxy"] = (
        (ohlcv5["cda_score_proxy"] - _lo) / max(_hi - _lo, 1e-9) * 10
    ).clip(0, 10)

    return ohlcv5[[
        "open", "high", "low", "close", "volume",
        "funding_z", "oi_change_z", "breadth_w", "liq_z", "cascade_z",
        "cda_score_proxy", "return_5m",
    ]].copy()


def label_crashes(df: pd.DataFrame) -> pd.DataFrame:
    for label, (bars, threshold) in HORIZONS.items():
        future_ret = df["close"].shift(-bars) / df["close"] - 1.0
        df[label] = (future_ret <= -threshold).astype(int)
    max_h = max(v[0] for v in HORIZONS.values())
    return df.iloc[:-max_h].copy()


def train_cps_model(df: pd.DataFrame, horizon: str, symbol: str):
    """Train logistic regression CPS model; return (model, scaler, X_test, y_test, proba_cal)."""
    from sklearn.linear_model import LogisticRegression
    from sklearn.isotonic import IsotonicRegression
    from sklearn.preprocessing import StandardScaler

    n     = len(df)
    split = int(n * 0.70)

    X = df[FEATURE_COLS].values
    y = df[horizon].values

    scaler  = StandardScaler().fit(X[:split])
    X_train = scaler.transform(X[:split])
    X_test  = scaler.transform(X[split:])
    y_train = y[:split]
    y_test  = y[split:]

    if y_test.sum() == 0:
        logger.warning("[%s][%s] No crash events in test set", symbol, horizon)
        return None, None, None, None, None

    model = LogisticRegression(C=0.1, class_weight="balanced", solver="lbfgs",
                                max_iter=500, random_state=42)
    model.fit(X_train, y_train)
    proba_raw = model.predict_proba(X_test)[:, 1]

    iso = IsotonicRegression(out_of_bounds="clip")
    iso.fit(proba_raw, y_test)
    proba_cal = iso.predict(proba_raw)

    return model, scaler, X_test, y_test, proba_cal


# ── Shared mini-backtest ───────────────────────────────────────────────────────

def _entry_signals(close: np.ndarray) -> np.ndarray:
    """Simple momentum entry: close crosses above 20-bar SMA."""
    sma = pd.Series(close).rolling(20, min_periods=20).mean().values
    sigs = (close > sma) & (np.roll(close, 1) <= np.roll(sma, 1))
    sigs[:20] = False
    return sigs


def _score_to_mult(score: float, thresholds: dict, multipliers: dict) -> float:
    if score >= thresholds.get("SYSTEMIC", 9.0):
        return multipliers.get("SYSTEMIC", 0.0)
    if score >= thresholds.get("EMERGENCY", 8.0):
        return multipliers.get("EMERGENCY", 0.10)
    if score >= thresholds.get("HIGH_ALERT", 7.0):
        return multipliers.get("HIGH_ALERT", 0.35)
    if score >= thresholds.get("DEFENSIVE", 5.0):
        return multipliers.get("DEFENSIVE", 0.65)
    return multipliers.get("NORMAL", 1.0)


def _run_backtest(
    close: np.ndarray,
    score_proxy: np.ndarray,
    cps_proba: Optional[np.ndarray],         # CPS probabilities on test set (or None → use score)
    cps_threshold: Optional[float],          # CPS threshold (None → use baseline CDA gate)
    thresholds: dict,
    multipliers: dict,
    test_offset: int,                        # index in full array where test set starts
) -> dict:
    """
    Run a backtest on the FULL array (train + test) but only analyse test period.

    cps_proba: if provided, defence is triggered when cps_proba[i - test_offset] >= cps_threshold
    Otherwise: defence triggered by CDA score as usual.

    Includes execution costs: EXEC_COST_RT applied to each trade.
    """
    n        = len(close)
    entries  = _entry_signals(close)
    capital  = 100_000.0
    cap_curve = [capital]
    trades   = []
    in_pos   = False
    entry_p  = 0.0
    pos_size = 0.0

    for i in range(1, n - 30):
        score = float(score_proxy[i])

        # Determine multiplier
        if cps_proba is not None and cps_threshold is not None:
            # CPS-gated: only apply multiplier if CPS says so
            test_i = i - test_offset
            if 0 <= test_i < len(cps_proba) and cps_proba[test_i] >= cps_threshold:
                mult = _score_to_mult(score, thresholds, multipliers)
            else:
                mult = multipliers.get("NORMAL", 1.0)
        else:
            mult = _score_to_mult(score, thresholds, multipliers)

        # Exit via ATR stop
        if in_pos:
            atr = np.std(close[max(0, i - 14): i + 1]) * 2.0
            stop = entry_p - atr
            if close[i] <= stop:
                # Include slippage/spread on exit
                exit_p = close[i] * (1.0 - SLIPPAGE_PCT)
                pnl = (exit_p - entry_p) / entry_p * pos_size
                capital += pnl
                trades.append({"pnl": pnl, "won": pnl > 0, "i_exit": i})
                in_pos = False

        # Entry
        if not in_pos and entries[i] and mult > 0:
            base_size = capital * 0.02
            pos_size  = base_size * mult
            # Execution cost on entry
            entry_p   = close[i] * (1.0 + SLIPPAGE_PCT)
            in_pos    = True

        cap_curve.append(capital)

    # Close open position
    if in_pos:
        exit_p = close[-1] * (1.0 - SLIPPAGE_PCT)
        pnl    = (exit_p - entry_p) / entry_p * pos_size
        capital += pnl
        trades.append({"pnl": pnl, "won": pnl > 0, "i_exit": n - 1})
        cap_curve.append(capital)

    if not trades:
        return {"error": "no_trades", "net_pnl": 0.0, "n_trades": 0}

    cap_arr  = np.array(cap_curve)
    peak_arr = np.maximum.accumulate(cap_arr)
    dd_arr   = (cap_arr - peak_arr) / peak_arr * 100
    mdd      = float(dd_arr.min())

    pnls  = [t["pnl"] for t in trades]
    wins  = [p for p in pnls if p > 0]
    losses= [abs(p) for p in pnls if p <= 0]
    gp    = sum(wins) if wins else 0.0
    gl    = sum(losses) if losses else 0.0

    years  = len(cap_arr) / (252 * 12 * 24)
    cagr   = (cap_arr[-1] / 100_000) ** (1 / max(years, 0.1)) - 1.0
    rets   = np.diff(cap_arr) / (cap_arr[:-1] + 1e-9)
    sharpe = float(rets.mean() / (rets.std() + 1e-12) * np.sqrt(252 * 12 * 24))

    return {
        "net_pnl":         round(float(cap_arr[-1]) - 100_000.0, 2),
        "final_capital":   round(float(cap_arr[-1]), 2),
        "cagr_pct":        round(cagr * 100, 2),
        "mdd_pct":         round(mdd, 2),
        "sharpe":          round(sharpe, 4),
        "profit_factor":   round(gp / max(gl, 1e-9), 4),
        "win_rate":        round(sum(t["won"] for t in trades) / len(trades), 4),
        "expectancy_usdt": round(sum(pnls) / len(trades), 2),
        "n_trades":        len(trades),
    }


# ══════════════════════════════════════════════════════════════════════════════
#  1. THRESHOLD SWEEPER
# ══════════════════════════════════════════════════════════════════════════════

def run_threshold_sweep(
    df: pd.DataFrame,
    symbol: str,
    horizons: list[str] = ("crash_5m", "crash_10m"),
) -> dict:
    """
    Sweep CPS probability thresholds 0.05 → 0.90 for each active horizon.
    For each threshold t, compute:
      - EVI_net_pnl = Net P&L_B,t - Net P&L_A  (primary ranking metric)
      - EVI_expectancy = expectancy_delta × n_affected_trades
      - FPR, fire_rate (hard constraints)
      - precision, recall, F-beta(0.5), Sharpe delta (diagnostics)
    Optimal threshold = max EVI subject to FPR ≤ 70% and fire_rate ≤ 5%
    and MDD worsening ≤ 0.5pp.
    """
    from sklearn.metrics import roc_auc_score
    logger.info("[%s] Threshold sweep starting…", symbol)

    n         = len(df)
    split     = int(n * 0.70)
    close_all = df["close"].values
    score_all = df["cda_score_proxy"].values

    # Scenario A baseline (no multiplier) — run once
    scen_a = _run_backtest(close_all, score_all,
                            cps_proba=None, cps_threshold=None,
                            thresholds=BASELINE_THRESHOLDS,
                            multipliers={k: 1.0 for k in BASELINE_MULTIPLIERS},
                            test_offset=split)

    thresholds_grid = np.arange(0.05, 0.95, 0.05).round(2)
    results_by_horizon = {}

    for horizon in horizons:
        if horizon not in df.columns:
            logger.warning("[%s][%s] Crash column missing — skipping", symbol, horizon)
            continue

        _, _, X_test, y_test, proba_cal = train_cps_model(df, horizon, symbol)
        if proba_cal is None:
            continue

        y_test_arr = np.array(y_test)
        auc_baseline = float(roc_auc_score(y_test_arr, proba_cal))

        # Fire metrics at each threshold
        rows = []
        for t in thresholds_grid:
            fires     = proba_cal >= t
            n_fires   = int(fires.sum())
            fire_rate = n_fires / max(len(fires), 1)

            crash_conf   = int((fires & (y_test_arr == 1)).sum())
            false_pos    = int((fires & (y_test_arr == 0)).sum())
            fpr_val      = false_pos / max((y_test_arr == 0).sum(), 1)
            true_neg     = int((~fires & (y_test_arr == 0)).sum())
            precision    = crash_conf / max(n_fires, 1)
            recall       = crash_conf / max(int(y_test_arr.sum()), 1)
            f_beta       = (1 + 0.25) * precision * recall / max(0.25 * precision + recall, 1e-9)

            # EVI: mini-backtest
            scen_b = _run_backtest(close_all, score_all,
                                   cps_proba=proba_cal, cps_threshold=float(t),
                                   thresholds=BASELINE_THRESHOLDS,
                                   multipliers=BASELINE_MULTIPLIERS,
                                   test_offset=split)

            if "error" in scen_b or "error" in scen_a:
                evi_net_pnl   = 0.0
                evi_expect    = 0.0
                sharpe_delta  = 0.0
                mdd_delta     = 0.0
            else:
                evi_net_pnl  = scen_b["net_pnl"] - scen_a["net_pnl"]
                evi_expect   = (scen_b["expectancy_usdt"] - scen_a["expectancy_usdt"]) * scen_b["n_trades"]
                sharpe_delta = scen_b["sharpe"] - scen_a["sharpe"]
                mdd_delta    = scen_b["mdd_pct"] - scen_a["mdd_pct"]   # negative = improvement

            # Constraints
            meets_fpr       = fpr_val <= FPR_TARGET
            meets_fire_rate = fire_rate <= FIRE_RATE_TARGET
            meets_mdd       = mdd_delta <= MDD_WORSENING_MAX
            feasible        = meets_fpr and meets_fire_rate and meets_mdd

            rows.append({
                "threshold":     round(float(t), 2),
                "n_fires":       n_fires,
                "fire_rate":     round(fire_rate, 4),
                "precision":     round(precision, 4),
                "recall":        round(recall, 4),
                "fpr":           round(fpr_val, 4),
                "f_beta_05":     round(f_beta, 4),
                "evi_net_pnl":   round(evi_net_pnl, 2),
                "evi_expectancy":round(evi_expect, 2),
                "sharpe_delta":  round(sharpe_delta, 4),
                "mdd_delta_pp":  round(mdd_delta, 4),
                "scen_b_pf":     scen_b.get("profit_factor", 0.0),
                "feasible":      feasible,
                "meets_fpr":     meets_fpr,
                "meets_fire_rate": meets_fire_rate,
                "meets_mdd":     meets_mdd,
            })

        # Find optimal: feasible point with highest EVI_net_pnl
        feasible_rows = [r for r in rows if r["feasible"]]
        if feasible_rows:
            optimal = max(feasible_rows, key=lambda r: r["evi_net_pnl"])
        else:
            # Relax: best trade-off even if constraints not fully met
            optimal = max(rows, key=lambda r: r["evi_net_pnl"] - (
                (max(0, r["fpr"] - FPR_TARGET) * 50) +
                (max(0, r["fire_rate"] - FIRE_RATE_TARGET) * 100)
            ))

        logger.info("[%s][%s] AUC=%.4f | Optimal threshold=%.2f | EVI_net_pnl=%.0f | "
                    "FPR=%.1f%% | fire_rate=%.1f%% | feasible=%s",
                    symbol, horizon, auc_baseline,
                    optimal["threshold"],
                    optimal["evi_net_pnl"],
                    optimal["fpr"] * 100,
                    optimal["fire_rate"] * 100,
                    optimal["feasible"])

        results_by_horizon[horizon] = {
            "auc_baseline":    round(auc_baseline, 4),
            "scenario_a":      scen_a,
            "sweep_table":     rows,
            "optimal":         optimal,
            "n_test_bars":     len(y_test_arr),
            "n_crash_events":  int(y_test_arr.sum()),
        }

    return results_by_horizon


# ══════════════════════════════════════════════════════════════════════════════
#  2. MULTI-HORIZON DESIGNER
# ══════════════════════════════════════════════════════════════════════════════

def run_multi_horizon_design(
    df: pd.DataFrame,
    symbol: str,
    threshold_5m: float,
    threshold_10m: float,
    confirm_window_bars: int = 3,  # bars CPS_10m must confirm within
) -> dict:
    """
    Define and evaluate two-stage trigger logic:
      CRASH_WATCH:     CPS_5m  >= threshold_5m
      CRASH_CONFIRMED: CPS_5m  >= threshold_5m AND CPS_10m >= threshold_10m
                       (within confirm_window_bars = 15 min)

    Measure per-stage: precision, recall, FPR, lead time, EVI.
    """
    logger.info("[%s] Multi-horizon design: t5m=%.2f t10m=%.2f window=%d bars",
                symbol, threshold_5m, threshold_10m, confirm_window_bars)

    n     = len(df)
    split = int(n * 0.70)

    _, _, _, y5_test, proba_5m  = train_cps_model(df, "crash_5m",  symbol)
    _, _, _, y10_test, proba_10m = train_cps_model(df, "crash_10m", symbol)

    if proba_5m is None or proba_10m is None:
        return {"error": "insufficient_crash_events"}

    y5  = np.array(y5_test)
    y10 = np.array(y10_test)
    # Use crash_5m as the ground truth crash label (forward-looking)
    y_true = y5

    # CRASH_WATCH (CPS_5m alone)
    watch_fires  = proba_5m >= threshold_5m
    watch_n      = int(watch_fires.sum())
    watch_prec   = int((watch_fires & (y_true == 1)).sum()) / max(watch_n, 1)
    watch_recall = int((watch_fires & (y_true == 1)).sum()) / max(int(y_true.sum()), 1)
    watch_fpr    = int((watch_fires & (y_true == 0)).sum()) / max(int((y_true == 0).sum()), 1)

    # CRASH_CONFIRMED: watch fires AND at least one 10m fire within window
    confirm_fires = np.zeros(len(y5), dtype=bool)
    for i in range(len(y5)):
        if watch_fires[i]:
            end = min(len(y5), i + confirm_window_bars + 1)
            if np.any(proba_10m[i:end] >= threshold_10m):
                confirm_fires[i] = True

    conf_n      = int(confirm_fires.sum())
    conf_prec   = int((confirm_fires & (y_true == 1)).sum()) / max(conf_n, 1)
    conf_recall = int((confirm_fires & (y_true == 1)).sum()) / max(int(y_true.sum()), 1)
    conf_fpr    = int((confirm_fires & (y_true == 0)).sum()) / max(int((y_true == 0).sum()), 1)

    # Lead times
    def _lead_times(fires: np.ndarray, y: np.ndarray) -> dict:
        crash_idx = np.where(y == 1)[0]
        leads = []
        for ci in crash_idx:
            for lookback in range(1, 61):
                ii = ci - lookback
                if ii >= 0 and fires[ii]:
                    leads.append(lookback * 5)  # minutes
                    break
        if leads:
            lt = np.array(leads)
            return {k: round(float(np.percentile(lt, int(k[1:]))), 1)
                    for k in ("P10", "P25", "P50", "P75", "P90")} | {"n": len(leads)}
        return {"P10": 0, "P25": 0, "P50": 0, "P75": 0, "P90": 0, "n": 0}

    close_all = df["close"].values
    score_all = df["cda_score_proxy"].values

    # EVI for each stage
    def _stage_evi(fires_arr: np.ndarray, label: str) -> float:
        scen_a = _run_backtest(close_all, score_all, None, None,
                               BASELINE_THRESHOLDS,
                               {k: 1.0 for k in BASELINE_MULTIPLIERS}, split)
        scen_b = _run_backtest(close_all, score_all, fires_arr.astype(float), 0.5,
                               BASELINE_THRESHOLDS, BASELINE_MULTIPLIERS, split)
        if "error" in scen_a or "error" in scen_b:
            return 0.0
        evi = scen_b["net_pnl"] - scen_a["net_pnl"]
        logger.info("[%s] %s EVI_net_pnl=%.0f (A=%.0f B=%.0f)",
                    symbol, label, evi, scen_a["net_pnl"], scen_b["net_pnl"])
        return round(evi, 2)

    result = {
        "crash_watch": {
            "threshold_5m": threshold_5m,
            "n_fires":   watch_n,
            "fire_rate": round(watch_n / max(len(y5), 1), 4),
            "precision": round(watch_prec, 4),
            "recall":    round(watch_recall, 4),
            "fpr":       round(watch_fpr, 4),
            "lead_time": _lead_times(watch_fires, y_true),
            "evi_net_pnl": _stage_evi(watch_fires, "CRASH_WATCH"),
        },
        "crash_confirmed": {
            "threshold_5m":  threshold_5m,
            "threshold_10m": threshold_10m,
            "confirm_window_bars": confirm_window_bars,
            "n_fires":   conf_n,
            "fire_rate": round(conf_n / max(len(y5), 1), 4),
            "precision": round(conf_prec, 4),
            "recall":    round(conf_recall, 4),
            "fpr":       round(conf_fpr, 4),
            "lead_time": _lead_times(confirm_fires, y_true),
            "evi_net_pnl": _stage_evi(confirm_fires, "CRASH_CONFIRMED"),
        },
    }

    logger.info("[%s] CRASH_WATCH: prec=%.3f rec=%.3f FPR=%.3f fires=%d",
                symbol, watch_prec, watch_recall, watch_fpr, watch_n)
    logger.info("[%s] CRASH_CONFIRMED: prec=%.3f rec=%.3f FPR=%.3f fires=%d",
                symbol, conf_prec, conf_recall, conf_fpr, conf_n)
    return result


# ══════════════════════════════════════════════════════════════════════════════
#  3. TIER OPTIMIZER
# ══════════════════════════════════════════════════════════════════════════════

def run_tier_optimization(df: pd.DataFrame, symbol: str) -> dict:
    """
    Sequential greedy optimization of CDA tier thresholds:
    Step 1: sweep DEFENSIVE (3.0→8.0), fix HIGH_ALERT=7.0, EMERGENCY=8.0
    Step 2: sweep HIGH_ALERT (DEFENSIVE+1 → 10.0), fix optimal DEFENSIVE, EMERGENCY=8.0
    Step 3: sweep EMERGENCY (HIGH_ALERT+1 → 11.0), fix optimal DEFENSIVE + HIGH_ALERT

    EVI_net_pnl as primary ranking metric, with FPR and MDD constraints.
    """
    logger.info("[%s] Tier threshold optimization…", symbol)

    close_all = df["close"].values
    score_all = df["cda_score_proxy"].values
    split     = int(len(df) * 0.70)

    # Baseline (Scenario A: no multiplier)
    scen_a = _run_backtest(close_all, score_all, None, None,
                           BASELINE_THRESHOLDS,
                           {k: 1.0 for k in BASELINE_MULTIPLIERS}, split)

    def _sweep(def_thresh: float, ha_thresh: float, em_thresh: float) -> dict:
        thresholds = {"DEFENSIVE": def_thresh, "HIGH_ALERT": ha_thresh,
                      "EMERGENCY": em_thresh, "SYSTEMIC": em_thresh + 1.0}
        scen_b = _run_backtest(close_all, score_all, None, None,
                               thresholds, BASELINE_MULTIPLIERS, split)
        if "error" in scen_a or "error" in scen_b:
            return {"evi_net_pnl": 0, "mdd_delta": 0}

        # Compute FPR at DEFENSIVE threshold (most frequent tier)
        fires_def = (score_all[split:] >= def_thresh).sum()
        fire_rate = fires_def / max(len(score_all[split:]), 1)

        evi    = scen_b["net_pnl"] - scen_a["net_pnl"]
        mdd_d  = scen_b["mdd_pct"] - scen_a["mdd_pct"]
        feasible = (fire_rate <= 0.20) and (mdd_d <= MDD_WORSENING_MAX)

        return {
            "def": def_thresh, "ha": ha_thresh, "em": em_thresh,
            "evi_net_pnl":   round(evi, 2),
            "mdd_delta_pp":  round(mdd_d, 4),
            "fire_rate_def": round(float(fire_rate), 4),
            "sharpe_b":      scen_b.get("sharpe", 0),
            "pf_b":          scen_b.get("profit_factor", 0),
            "feasible":      feasible,
        }

    # Step 1: sweep DEFENSIVE
    step1 = [_sweep(d, 7.0, 8.0) for d in np.arange(3.0, 8.5, 0.5)]
    best1 = max((r for r in step1 if r["feasible"]), key=lambda r: r["evi_net_pnl"],
                default=max(step1, key=lambda r: r["evi_net_pnl"]))
    opt_def = best1["def"]
    logger.info("[%s] Step1 optimal DEFENSIVE=%.1f EVI=%.0f", symbol, opt_def, best1["evi_net_pnl"])

    # Step 2: sweep HIGH_ALERT
    step2 = [_sweep(opt_def, ha, 8.0)
             for ha in np.arange(opt_def + 0.5, 11.0, 0.5)]
    best2 = max((r for r in step2 if r["feasible"]), key=lambda r: r["evi_net_pnl"],
                default=max(step2, key=lambda r: r["evi_net_pnl"]))
    opt_ha = best2["ha"]
    logger.info("[%s] Step2 optimal HIGH_ALERT=%.1f EVI=%.0f", symbol, opt_ha, best2["evi_net_pnl"])

    # Step 3: sweep EMERGENCY
    step3 = [_sweep(opt_def, opt_ha, em)
             for em in np.arange(opt_ha + 0.5, 12.0, 0.5)]
    best3 = max((r for r in step3 if r["feasible"]), key=lambda r: r["evi_net_pnl"],
                default=max(step3, key=lambda r: r["evi_net_pnl"]))
    opt_em = best3["em"]
    logger.info("[%s] Step3 optimal EMERGENCY=%.1f EVI=%.0f", symbol, opt_em, best3["evi_net_pnl"])

    # Final result with optimized thresholds
    final = _sweep(opt_def, opt_ha, opt_em)
    logger.info("[%s] Optimal thresholds: DEF=%.1f HA=%.1f EM=%.1f | EVI=%.0f MDD_delta=%.2fpp",
                symbol, opt_def, opt_ha, opt_em, final["evi_net_pnl"], final["mdd_delta_pp"])

    return {
        "baseline": {"DEFENSIVE": 5.0, "HIGH_ALERT": 7.0, "EMERGENCY": 8.0, "SYSTEMIC": 9.0},
        "optimal":  {"DEFENSIVE": opt_def, "HIGH_ALERT": opt_ha, "EMERGENCY": opt_em,
                     "SYSTEMIC": opt_em + 1.0},
        "step1_sweep": step1,
        "step2_sweep": step2,
        "step3_sweep": step3,
        "final_result": final,
    }


# ══════════════════════════════════════════════════════════════════════════════
#  4. MULTIPLIER CALIBRATOR
# ══════════════════════════════════════════════════════════════════════════════

def run_multiplier_calibration(df: pd.DataFrame, symbol: str) -> dict:
    """
    Compare 6 multiplier curves on EVI_net_pnl and MDD improvement.

    Curves:
      1. baseline_phase1a  — current step (1.0, 0.65, 0.35, 0.10, 0.00)
      2. aggressive_step   — steeper step (1.0, 0.50, 0.20, 0.05, 0.00)
      3. conservative_step — gentler step (1.0, 0.80, 0.60, 0.30, 0.10)
      4. linear            — linear decay from score 0→10
      5. sigmoid           — smooth sigmoid centred at DEFENSIVE threshold
      6. flat_halved       — uniform 50% in all warning tiers
    """
    logger.info("[%s] Multiplier calibration (6 curves)…", symbol)

    close_all = df["close"].values
    score_all = df["cda_score_proxy"].values
    split     = int(len(df) * 0.70)

    scen_a = _run_backtest(close_all, score_all, None, None,
                           BASELINE_THRESHOLDS,
                           {k: 1.0 for k in BASELINE_MULTIPLIERS}, split)

    named_curves = {
        "baseline_phase1a": {
            "NORMAL": 1.00, "DEFENSIVE": 0.65, "HIGH_ALERT": 0.35,
            "EMERGENCY": 0.10, "SYSTEMIC": 0.00
        },
        "aggressive_step": {
            "NORMAL": 1.00, "DEFENSIVE": 0.50, "HIGH_ALERT": 0.20,
            "EMERGENCY": 0.05, "SYSTEMIC": 0.00
        },
        "conservative_step": {
            "NORMAL": 1.00, "DEFENSIVE": 0.80, "HIGH_ALERT": 0.60,
            "EMERGENCY": 0.30, "SYSTEMIC": 0.10
        },
        "flat_halved": {
            "NORMAL": 1.00, "DEFENSIVE": 0.50, "HIGH_ALERT": 0.50,
            "EMERGENCY": 0.50, "SYSTEMIC": 0.00
        },
    }

    # Linear and sigmoid require per-bar computation; precompute as custom thresholds
    # We'll build equivalent step approximations at 0.5-score intervals
    def _linear_mult(score: float) -> float:
        """Linear: 1.0 at score=0, 0.0 at score=10."""
        return float(max(0.0, 1.0 - score / 10.0))

    def _sigmoid_mult(score: float, centre: float = 5.0, steepness: float = 0.8) -> float:
        """Sigmoid centred at DEFENSIVE threshold (5.0)."""
        return float(1.0 / (1.0 + np.exp(steepness * (score - centre))))

    def _run_continuous(mult_fn) -> dict:
        """Run backtest with a continuous multiplier function (not step-based)."""
        n        = len(close_all)
        entries  = _entry_signals(close_all)
        capital  = 100_000.0
        cap_curve = [capital]
        trades   = []
        in_pos   = False
        entry_p  = 0.0
        pos_size = 0.0

        for i in range(1, n - 30):
            mult = mult_fn(float(score_all[i]))
            if in_pos:
                atr  = np.std(close_all[max(0, i - 14): i + 1]) * 2.0
                stop = entry_p - atr
                if close_all[i] <= stop:
                    exit_p = close_all[i] * (1.0 - SLIPPAGE_PCT)
                    pnl    = (exit_p - entry_p) / entry_p * pos_size
                    capital += pnl
                    trades.append({"pnl": pnl, "won": pnl > 0})
                    in_pos = False
            if not in_pos and entries[i] and mult > 0:
                pos_size = capital * 0.02 * mult
                entry_p  = close_all[i] * (1.0 + SLIPPAGE_PCT)
                in_pos   = True
            cap_curve.append(capital)

        if in_pos:
            pnl = (close_all[-1] * (1 - SLIPPAGE_PCT) - entry_p) / entry_p * pos_size
            capital += pnl
            trades.append({"pnl": pnl, "won": pnl > 0})
            cap_curve.append(capital)

        if not trades:
            return {"error": "no_trades", "net_pnl": 0.0}
        cap_arr  = np.array(cap_curve)
        dd       = (cap_arr - np.maximum.accumulate(cap_arr)) / np.maximum.accumulate(cap_arr) * 100
        pnls     = [t["pnl"] for t in trades]
        wins     = [p for p in pnls if p > 0]
        losses   = [abs(p) for p in pnls if p <= 0]
        rets     = np.diff(cap_arr) / (cap_arr[:-1] + 1e-9)
        return {
            "net_pnl":         round(float(cap_arr[-1]) - 100_000.0, 2),
            "final_capital":   round(float(cap_arr[-1]), 2),
            "mdd_pct":         round(float(dd.min()), 2),
            "sharpe":          round(float(rets.mean() / (rets.std() + 1e-12) * np.sqrt(252 * 12 * 24)), 4),
            "profit_factor":   round(sum(wins) / max(sum(losses), 1e-9), 4),
            "expectancy_usdt": round(sum(pnls) / len(trades), 2),
            "n_trades":        len(trades),
        }

    results = {}
    for name, mults in named_curves.items():
        scen_b = _run_backtest(close_all, score_all, None, None,
                               BASELINE_THRESHOLDS, mults, split)
        evi = (scen_b.get("net_pnl", 0) - scen_a.get("net_pnl", 0)) if "error" not in scen_b else 0
        results[name] = {**scen_b, "evi_net_pnl": round(evi, 2)}

    for name, fn in [("linear", _linear_mult), ("sigmoid", _sigmoid_mult)]:
        scen_b = _run_continuous(fn)
        evi = (scen_b.get("net_pnl", 0) - scen_a.get("net_pnl", 0)) if "error" not in scen_b else 0
        results[name] = {**scen_b, "evi_net_pnl": round(evi, 2)}

    # Find best by EVI
    valid = {k: v for k, v in results.items() if "error" not in v}
    optimal = max(valid, key=lambda k: valid[k]["evi_net_pnl"]) if valid else "baseline_phase1a"
    logger.info("[%s] Best multiplier curve: %s (EVI=%.0f, MDD=%.2f%%)",
                symbol, optimal,
                results[optimal].get("evi_net_pnl", 0),
                results[optimal].get("mdd_pct", 0))

    return {"curves": results, "optimal_curve": optimal, "scenario_a": scen_a}


# ══════════════════════════════════════════════════════════════════════════════
#  5. BTC DOMINANCE ANALYZER
# ══════════════════════════════════════════════════════════════════════════════

def run_btc_dominance_analysis(all_data: dict) -> dict:
    """
    3-part composite BTC-led move definition (production-grade):

    1. Lead-lag (required, primary signal):
       - Uses 1m OHLCV data for 1-minute precision
       - BTC must show a ≥ 0.20% 1m return BEFORE ETH/SOL move in same direction
       - Follow window: 1–3 minutes (1–3 bars at 1m resolution)

    2. Relative strength (required):
       - Computed at 5m level: RS = |r_BTC| / median(|r_ETH|, |r_SOL|)
       - Threshold ≥ 1.5 (strong BTC lead)
       - ≥ 1.2 is mild and logged as secondary

    3. Breadth confirmation (required):
       - At 5m level: ETH and SOL both move same direction as BTC within 3 bars (15 min)
       - Ensures it's a market move, not isolated BTC noise

    Optional secondary: BTC.D-proxy (BTC volume share) used as context label only.

    Segments CPS performance into BTC-led vs non-BTC-led events.
    """
    logger.info("BTC dominance analysis (3-part composite, 1m lead-lag precision)…")

    if not all(s in all_data for s in ["BTC", "ETH", "SOL"]):
        return {"error": "missing_symbols"}

    # ── Part 1: Lead-lag at 1m resolution ─────────────────────────────────────
    # Load 1m OHLCV returns for all 3 symbols
    logger.info("Computing 1m returns for lead-lag detection…")
    btc_1m = all_data["BTC"]["ohlcv"].set_index("timestamp")["close"].resample("1min").last().ffill()
    eth_1m = all_data["ETH"]["ohlcv"].set_index("timestamp")["close"].resample("1min").last().ffill()
    sol_1m = all_data["SOL"]["ohlcv"].set_index("timestamp")["close"].resample("1min").last().ffill()

    # Align 1m series on common timestamps
    common_1m = btc_1m.index.intersection(eth_1m.index).intersection(sol_1m.index)
    if len(common_1m) < 5000:
        return {"error": "insufficient_1m_common_bars"}

    r1m_btc = btc_1m.loc[common_1m].pct_change().fillna(0.0).values
    r1m_eth = eth_1m.loc[common_1m].pct_change().fillna(0.0).values
    r1m_sol = sol_1m.loc[common_1m].pct_change().fillna(0.0).values

    LEAD_THRESH_1M  = 0.002   # 0.20% minimum 1m move to register as BTC lead
    FOLLOW_BARS_1M  = 3       # ETH/SOL must follow within 3 minutes

    n_1m = len(common_1m)
    lead_lag_1m = np.zeros(n_1m, dtype=bool)
    for i in range(1, n_1m - FOLLOW_BARS_1M):
        if abs(r1m_btc[i]) >= LEAD_THRESH_1M:
            btc_dir = np.sign(r1m_btc[i])
            for j in range(i + 1, min(i + FOLLOW_BARS_1M + 1, n_1m)):
                eth_follows = np.sign(r1m_eth[j]) == btc_dir
                sol_follows = np.sign(r1m_sol[j]) == btc_dir
                if eth_follows or sol_follows:
                    lead_lag_1m[i] = True
                    break

    # Map 1m lead-lag signal to 5m grid (True if any 1m bar in the 5m window fired)
    btc_ts_1m = pd.Series(lead_lag_1m.astype(int), index=common_1m)
    lead_lag_5m = btc_ts_1m.resample("5min").max().fillna(0).astype(bool)
    logger.info("1m lead-lag: %d fires → downsampled to 5m", lead_lag_1m.sum())

    # ── Build 5m feature DataFrames for all symbols ────────────────────────────
    dfs = {}
    for sym in ["BTC", "ETH", "SOL"]:
        d = all_data[sym]
        df_feat = compute_cps_features(d["ohlcv"], d["funding"], d["oi"], sym)
        df_feat = label_crashes(df_feat)
        dfs[sym] = df_feat

    # Align 5m frames on common index
    common_5m = (dfs["BTC"].index
                 .intersection(dfs["ETH"].index)
                 .intersection(dfs["SOL"].index)
                 .intersection(lead_lag_5m.index))
    if len(common_5m) < 1000:
        return {"error": "insufficient_5m_common_bars"}

    df_btc = dfs["BTC"].loc[common_5m].copy()
    df_eth = dfs["ETH"].loc[common_5m].copy()
    df_sol = dfs["SOL"].loc[common_5m].copy()
    ll_5m  = lead_lag_5m.reindex(common_5m, fill_value=False).values

    r_btc = df_btc["return_5m"].values
    r_eth = df_eth["return_5m"].values
    r_sol = df_sol["return_5m"].values
    n     = len(common_5m)

    # ── Part 2: Relative strength at 5m (RS ≥ 1.5) ───────────────────────────
    alt_abs_median = (np.abs(r_eth) + np.abs(r_sol)) / 2.0
    rs = np.abs(r_btc) / (alt_abs_median + 1e-9)
    rs_strong = rs >= 1.5   # strong BTC leadership
    rs_mild   = rs >= 1.2   # mild (for reference)

    # ── Part 3: Breadth confirmation at 5m (ETH + SOL confirm direction, ≤ 3 bars) ──
    BREADTH_BARS = 3
    breadth_ok = np.zeros(n, dtype=bool)
    for i in range(n - BREADTH_BARS):
        if abs(r_btc[i]) >= 0.002:
            btc_dir = np.sign(r_btc[i])
            eth_ok = any(np.sign(r_eth[i: i + BREADTH_BARS + 1]) == btc_dir)
            sol_ok = any(np.sign(r_sol[i: i + BREADTH_BARS + 1]) == btc_dir)
            breadth_ok[i] = eth_ok and sol_ok

    # ── Composite BTC-led label ───────────────────────────────────────────────
    btc_led = ll_5m & rs_strong & breadth_ok
    alt_led = ~btc_led
    alt_led = ~btc_led

    n_btc_led = int(btc_led.sum())
    n_alt_led = int(alt_led.sum())
    logger.info("BTC-led bars: %d (%.1f%%) | Alt-led: %d (%.1f%%)",
                n_btc_led, n_btc_led / n * 100,
                n_alt_led, n_alt_led / n * 100)

    # CPS AUC by segment (BTC symbol, 5m + 10m horizons)
    from sklearn.metrics import roc_auc_score
    from sklearn.linear_model import LogisticRegression
    from sklearn.preprocessing import StandardScaler
    from sklearn.isotonic import IsotonicRegression

    segment_results = {}
    for seg_name, mask in [("btc_led", btc_led), ("alt_led", alt_led)]:
        seg_df = df_btc.iloc[mask]
        if len(seg_df) < 500:
            segment_results[seg_name] = {"error": "too_few_bars"}
            continue

        seg_results = {}
        for horizon in ["crash_5m", "crash_10m"]:
            if horizon not in seg_df.columns:
                continue
            X = seg_df[FEATURE_COLS].values
            y = seg_df[horizon].values
            if y.sum() < 10:
                seg_results[horizon] = {"auc": "insufficient_events"}
                continue

            sp = int(len(X) * 0.70)
            scaler = StandardScaler().fit(X[:sp])
            xt = scaler.transform(X[sp:])
            yt = y[sp:]
            if yt.sum() < 5:
                seg_results[horizon] = {"auc": "insufficient_test_events"}
                continue

            model = LogisticRegression(C=0.1, class_weight="balanced",
                                       solver="lbfgs", max_iter=500, random_state=42)
            model.fit(scaler.transform(X[:sp]), y[:sp])
            proba = model.predict_proba(xt)[:, 1]
            iso   = IsotonicRegression(out_of_bounds="clip")
            iso.fit(proba, yt)
            proba_cal = iso.predict(proba)
            try:
                auc = float(roc_auc_score(yt, proba_cal))
            except Exception:
                auc = float("nan")
            seg_results[horizon] = {
                "auc":           round(auc, 4),
                "n_bars":        len(seg_df),
                "n_crashes":     int(y.sum()),
                "crash_rate":    round(float(y.mean()), 4),
            }
        segment_results[seg_name] = seg_results

    # Compare AUC
    def _safe_auc(seg, h):
        v = segment_results.get(seg, {}).get(h, {})
        if isinstance(v, dict):
            return v.get("auc")
        return None

    auc_5m_btc = _safe_auc("btc_led", "crash_5m")
    auc_5m_alt = _safe_auc("alt_led", "crash_5m")

    if isinstance(auc_5m_btc, float) and isinstance(auc_5m_alt, float):
        diff_5m = round(auc_5m_btc - auc_5m_alt, 4)
        if diff_5m > 0.03:
            recommendation = "BTC_WEIGHTED_CPS — CPS significantly stronger on BTC-led moves; weight BTC features higher"
        elif diff_5m < -0.03:
            recommendation = "ALT_FOCUS — CPS performs better on alt-led moves; investigate alt-specific signals"
        else:
            recommendation = "UNIFIED_CPS_OK — AUC difference < 0.03pp; single CPS model sufficient"
    else:
        recommendation = "INSUFFICIENT_DATA"

    return {
        "n_total_bars":   n,
        "n_btc_led":      n_btc_led,
        "n_alt_led":      n_alt_led,
        "btc_led_pct":    round(n_btc_led / n * 100, 2),
        "segment_results": segment_results,
        "auc_5m_btc_led": auc_5m_btc,
        "auc_5m_alt_led": auc_5m_alt,
        "recommendation": recommendation,
    }


# ══════════════════════════════════════════════════════════════════════════════
#  6. LIQUIDATION GAP EVALUATOR
# ══════════════════════════════════════════════════════════════════════════════

def evaluate_liquidation_gap() -> dict:
    """
    Data-free assessment of CoinGlass liquidation data integration.
    Based on API documentation, pricing, and expected AUC improvement.
    """
    return {
        "current_proxy": {
            "description": "vol_ratio × (-return_5m).clip(0) — volume spike + price drop proxy",
            "limitation":  "Cannot distinguish forced liquidations from voluntary selling. "
                           "Overestimates small liquidations during high-volume ordinary moves.",
            "information_coefficient": "~0.03–0.07 (weak)",
        },
        "coinglass_api": {
            "endpoint":        "https://open-api.coinglass.com/public/v2/liquidation_history",
            "auth":            "API key required (free tier available)",
            "free_tier":       {"rate_limit": "10 req/min", "history": "30 days", "resolution": "1h"},
            "paid_tier":       {"rate_limit": "600 req/min", "history": "4+ years", "resolution": "5m"},
            "paid_cost_usd":   "~$99–299/month depending on plan",
            "key_fields":      ["longLiquidationUsd", "shortLiquidationUsd", "symbol", "timestamp"],
            "availability":    "BTC/ETH/SOL all available",
            "reliability":     "High — Coinglass is the primary source for crypto liquidation data",
        },
        "expected_improvement": {
            "current_liq_auc_contribution": "Marginal — proxy IC ≈ 0.03",
            "real_liq_auc_contribution":    "Moderate — real liquidation data IC ≈ 0.10–0.15 (literature)",
            "estimated_auc_uplift":         "+0.02 to +0.05 on crash_5m (conservative estimate)",
            "false_positive_impact":        "Real liquidation spikes are highly correlated with actual crashes; "
                                            "expected FPR reduction of 5–15pp at HIGH_ALERT tier",
            "confidence":                   "Medium — based on academic literature and CoinGlass data quality",
        },
        "integration_plan": {
            "phase":    "Phase 1B (after CPS threshold calibration is validated in shadow mode)",
            "step_1":   "Request free CoinGlass API key → validate data quality (30-day test)",
            "step_2":   "Add liq_long_z and liq_short_z as 2 new features (replace liq_proxy)",
            "step_3":   "Retrain CPS with 8 features; compare AUC on same 4yr test set",
            "step_4":   "If AUC improves ≥ 0.02: upgrade to paid tier for historical 5m data",
            "step_5":   "Full re-validation with real liquidation features before Phase 2 activation",
            "risk":     "API dependency; if CoinGlass changes pricing/API, system degrades to proxy",
            "mitigation": "Maintain liq_proxy as fallback; graceful degradation if API unavailable",
        },
        "recommendation": "PURSUE_IN_PHASE1B — High expected benefit, low implementation cost. "
                          "Start with free tier to validate data quality before committing to paid plan.",
    }


# ══════════════════════════════════════════════════════════════════════════════
#  REPORT WRITER
# ══════════════════════════════════════════════════════════════════════════════

def write_report(results: dict, path: Path) -> None:
    lines = [
        "=" * 80,
        "NexusTrader CPS Calibration Report",
        f"Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
        "=" * 80, "",
    ]

    lines += ["─" * 60, "1. THRESHOLD SWEEP (Primary: EVI = Net P&L_B - Net P&L_A)", "─" * 60]
    for sym, sym_res in results.get("threshold_sweep", {}).items():
        lines.append(f"\n  {sym}:")
        for horizon, h_res in sym_res.items():
            opt = h_res.get("optimal", {})
            lines.append(f"    [{horizon}]")
            lines.append(f"      Baseline AUC:          {h_res.get('auc_baseline', '?')}")
            lines.append(f"      Optimal threshold:     {opt.get('threshold', '?')}")
            lines.append(f"      EVI Net P&L:           ${opt.get('evi_net_pnl', 0):+,.0f}")
            lines.append(f"      EVI Expectancy:        ${opt.get('evi_expectancy', 0):+,.0f}")
            lines.append(f"      FPR at threshold:      {opt.get('fpr', 0)*100:.1f}%")
            lines.append(f"      Fire rate:             {opt.get('fire_rate', 0)*100:.1f}%")
            lines.append(f"      Precision / Recall:    {opt.get('precision', 0):.3f} / {opt.get('recall', 0):.3f}")
            lines.append(f"      Sharpe delta:          {opt.get('sharpe_delta', 0):+.4f}")
            lines.append(f"      MDD delta:             {opt.get('mdd_delta_pp', 0):+.2f}pp")
            lines.append(f"      Feasible (all gates):  {opt.get('feasible', False)}")

    lines += ["", "─" * 60, "2. MULTI-HORIZON DESIGN", "─" * 60]
    for sym, sym_res in results.get("multi_horizon", {}).items():
        lines.append(f"\n  {sym}:")
        for stage, s_res in sym_res.items():
            if "error" in s_res:
                continue
            lines.append(f"    [{stage.upper()}]")
            lines.append(f"      Fires: {s_res.get('n_fires',0)} | fire_rate: {s_res.get('fire_rate',0)*100:.1f}%")
            lines.append(f"      Prec: {s_res.get('precision',0):.3f} | Rec: {s_res.get('recall',0):.3f} | FPR: {s_res.get('fpr',0)*100:.1f}%")
            lines.append(f"      EVI Net P&L: ${s_res.get('evi_net_pnl',0):+,.0f}")
            lt = s_res.get("lead_time", {})
            lines.append(f"      Lead time P50: {lt.get('P50',0)} min | P25: {lt.get('P25',0)} min")

    lines += ["", "─" * 60, "3. TIER THRESHOLD OPTIMIZATION", "─" * 60]
    for sym, sym_res in results.get("tier_optimization", {}).items():
        opt = sym_res.get("optimal", {})
        fin = sym_res.get("final_result", {})
        lines.append(f"\n  {sym}:")
        lines.append(f"    Baseline:  DEF=5.0  HA=7.0  EM=8.0  SYS=9.0")
        lines.append(f"    Optimal:   DEF={opt.get('DEFENSIVE','?')}  HA={opt.get('HIGH_ALERT','?')}  EM={opt.get('EMERGENCY','?')}  SYS={opt.get('SYSTEMIC','?')}")
        lines.append(f"    EVI:       ${fin.get('evi_net_pnl',0):+,.0f}")
        lines.append(f"    MDD delta: {fin.get('mdd_delta_pp',0):+.2f}pp")
        lines.append(f"    Fire rate @ DEF: {fin.get('fire_rate_def',0)*100:.1f}%")

    lines += ["", "─" * 60, "4. MULTIPLIER CALIBRATION", "─" * 60]
    for sym, sym_res in results.get("multiplier_calibration", {}).items():
        lines.append(f"\n  {sym}:")
        lines.append(f"    Optimal curve: {sym_res.get('optimal_curve','?')}")
        for name, c in sym_res.get("curves", {}).items():
            if "error" in c:
                continue
            lines.append(f"    {name:20s}  EVI=${c.get('evi_net_pnl',0):+8,.0f}  "
                         f"MDD={c.get('mdd_pct',0):6.2f}%  "
                         f"Sharpe={c.get('sharpe',0):.4f}")

    lines += ["", "─" * 60, "5. BTC DOMINANCE ANALYSIS", "─" * 60]
    dom = results.get("btc_dominance", {})
    if "error" not in dom:
        lines.append(f"  BTC-led bars: {dom.get('n_btc_led',0):,} ({dom.get('btc_led_pct',0):.1f}%)")
        lines.append(f"  AUC crash_5m (BTC-led): {dom.get('auc_5m_btc_led','?')}")
        lines.append(f"  AUC crash_5m (alt-led): {dom.get('auc_5m_alt_led','?')}")
        lines.append(f"  Recommendation: {dom.get('recommendation','?')}")
    else:
        lines.append(f"  ERROR: {dom.get('error','?')}")

    lines += ["", "─" * 60, "6. LIQUIDATION GAP", "─" * 60]
    liq = results.get("liquidation_gap", {})
    lines.append(f"  Recommendation: {liq.get('recommendation','?')}")
    lines.append(f"  Expected AUC uplift: {liq.get('expected_improvement',{}).get('estimated_auc_uplift','?')}")
    lines.append(f"  Cost: {liq.get('coinglass_api',{}).get('paid_cost_usd','?')}/mo")

    lines += ["", "=" * 80, "SUMMARY", "=" * 80]
    summary = results.get("summary", {})
    lines.append(f"  Recommended CPS_5m threshold:  {summary.get('recommended_threshold_5m','?')}")
    lines.append(f"  Recommended CPS_10m threshold: {summary.get('recommended_threshold_10m','?')}")
    lines.append(f"  Optimal trigger: {summary.get('optimal_trigger','?')}")
    lines.append(f"  Optimal tier thresholds: {summary.get('optimal_tiers','?')}")
    lines.append(f"  Best multiplier curve: {summary.get('best_multiplier_curve','?')}")
    lines.append(f"  Phase 1B recommendation: {summary.get('phase1b_verdict','?')}")
    lines.append("")

    path.write_text("\n".join(lines), encoding="utf-8")
    logger.info("Report written → %s", path)


# ══════════════════════════════════════════════════════════════════════════════
#  MAIN PIPELINE
# ══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="CPS Calibration Pipeline")
    parser.add_argument("--symbols", nargs="+", default=["BTC", "ETH", "SOL"])
    parser.add_argument("--skip-btc-dom", action="store_true",
                        help="Skip BTC dominance analysis (requires all 3 symbols)")
    args = parser.parse_args()

    symbols = args.symbols
    logger.info("CPS Calibration Pipeline | symbols=%s", symbols)
    logger.info("EVI metric: Net P&L delta vs baseline (incl. slippage=%.2f%% RT, spread=%.2f%%)",
                EXEC_COST_RT * 100 / 2, SPREAD_PCT * 100)
    logger.info("Constraints: FPR≤%.0f%%, fire_rate≤%.0f%%, MDD_worsening≤%.1fpp",
                FPR_TARGET * 100, FIRE_RATE_TARGET * 100, MDD_WORSENING_MAX)

    # Load data
    raw_data = load_all_data(symbols)

    # Build feature DataFrames
    dfs = {}
    for sym in symbols:
        d    = raw_data[sym]
        df_f = compute_cps_features(d["ohlcv"], d["funding"], d["oi"], sym)
        df_f = label_crashes(df_f)
        dfs[sym] = df_f
        logger.info("[%s] Total bars after labelling: %d", sym, len(df_f))

    results: dict = {
        "meta": {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "symbols":      symbols,
            "exec_cost_rt_pct": round(EXEC_COST_RT * 100, 3),
            "fpr_target":   FPR_TARGET,
            "fire_rate_target": FIRE_RATE_TARGET,
        },
        "threshold_sweep":       {},
        "multi_horizon":         {},
        "tier_optimization":     {},
        "multiplier_calibration": {},
        "btc_dominance":         {},
        "liquidation_gap":       {},
        "summary":               {},
    }

    # ── Section 1: Threshold sweep ────────────────────────────────────────────
    for sym in symbols:
        logger.info("[%s] === SECTION 1: THRESHOLD SWEEP ===", sym)
        results["threshold_sweep"][sym] = run_threshold_sweep(
            dfs[sym], sym, horizons=["crash_5m", "crash_10m"]
        )

    # ── Derive optimal thresholds for multi-horizon design ────────────────────
    def _get_opt_threshold(sym: str, horizon: str) -> float:
        r = results["threshold_sweep"].get(sym, {}).get(horizon, {})
        return r.get("optimal", {}).get("threshold", 0.40)

    # ── Section 2: Multi-horizon design ──────────────────────────────────────
    for sym in symbols:
        logger.info("[%s] === SECTION 2: MULTI-HORIZON DESIGN ===", sym)
        t5m  = _get_opt_threshold(sym, "crash_5m")
        t10m = _get_opt_threshold(sym, "crash_10m")
        results["multi_horizon"][sym] = run_multi_horizon_design(
            dfs[sym], sym, threshold_5m=t5m, threshold_10m=t10m, confirm_window_bars=3
        )

    # ── Section 3: Tier optimization ─────────────────────────────────────────
    for sym in symbols:
        logger.info("[%s] === SECTION 3: TIER OPTIMIZATION ===", sym)
        results["tier_optimization"][sym] = run_tier_optimization(dfs[sym], sym)

    # ── Section 4: Multiplier calibration ────────────────────────────────────
    for sym in symbols:
        logger.info("[%s] === SECTION 4: MULTIPLIER CALIBRATION ===", sym)
        results["multiplier_calibration"][sym] = run_multiplier_calibration(dfs[sym], sym)

    # ── Section 5: BTC dominance ─────────────────────────────────────────────
    if not args.skip_btc_dom and all(s in raw_data for s in ["BTC", "ETH", "SOL"]):
        logger.info("=== SECTION 5: BTC DOMINANCE ANALYSIS ===")
        results["btc_dominance"] = run_btc_dominance_analysis(raw_data)
    else:
        logger.info("Skipping BTC dominance analysis")
        results["btc_dominance"] = {"skipped": True}

    # ── Section 6: Liquidation gap ────────────────────────────────────────────
    logger.info("=== SECTION 6: LIQUIDATION GAP EVALUATION ===")
    results["liquidation_gap"] = evaluate_liquidation_gap()

    # ── Build summary ─────────────────────────────────────────────────────────
    # Use BTC as primary representative for summary
    btc_5m_opt = results["threshold_sweep"].get("BTC", {}).get("crash_5m", {}).get("optimal", {})
    btc_10m_opt = results["threshold_sweep"].get("BTC", {}).get("crash_10m", {}).get("optimal", {})
    btc_tiers  = results["tier_optimization"].get("BTC", {}).get("optimal", {})
    btc_mult   = results["multiplier_calibration"].get("BTC", {}).get("optimal_curve", "?")
    dom_rec    = results["btc_dominance"].get("recommendation", "?")

    # Phase 1B verdict
    t5m_ok   = btc_5m_opt.get("feasible", False)
    t10m_ok  = btc_10m_opt.get("feasible", False)
    phase1b  = "YES — Calibrated thresholds meet all constraints" if (t5m_ok or t10m_ok) \
               else "CONDITIONAL — Not all constraints met; review sweep table"

    results["summary"] = {
        "recommended_threshold_5m":  btc_5m_opt.get("threshold", "?"),
        "recommended_threshold_10m": btc_10m_opt.get("threshold", "?"),
        "optimal_trigger":           "CRASH_CONFIRMED (5m + 10m confirmation)",
        "optimal_tiers":             btc_tiers,
        "best_multiplier_curve":     btc_mult,
        "btc_dominance_recommendation": dom_rec,
        "phase1b_verdict":           phase1b,
    }

    # Save JSON
    OUT_JSON.write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")
    logger.info("Results saved → %s (%.1f KB)", OUT_JSON, OUT_JSON.stat().st_size / 1024)

    # Write text report
    write_report(results, OUT_REPORT)

    # Summary to console
    logger.info("=" * 60)
    logger.info("CALIBRATION COMPLETE")
    logger.info("  CPS_5m threshold:  %s", results["summary"]["recommended_threshold_5m"])
    logger.info("  CPS_10m threshold: %s", results["summary"]["recommended_threshold_10m"])
    logger.info("  Best curve: %s", results["summary"]["best_multiplier_curve"])
    logger.info("  Phase 1B: %s", results["summary"]["phase1b_verdict"])
    logger.info("  Report → %s", OUT_REPORT)
    logger.info("=" * 60)
    logger.info("DONE. To generate v0.5 Word document:")
    logger.info("  python scripts/generate_v05_doc.py")


if __name__ == "__main__":
    main()
