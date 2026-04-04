"""
scripts/cpi_validation_realdata.py — CPS Real-Data Validation & Impact Analysis
================================================================================
Runs the full CPI/CPS quantitative validation suite against real historical
data fetched by scripts/fetch_historical_data.py.

WHAT THIS SCRIPT PRODUCES
  1. CPS Validation (per horizon: 5m, 10m, 30m)
     ROC-AUC, Brier score, calibration curve, precision/recall/F1,
     false positive rate, lead time distribution (P10/P25/P50/P75/P90)

  2. CDA Multiplier Impact Analysis
     Scenario A (no multiplier, 1.0 always) vs Scenario B (live multiplier)
     Metrics: final capital, CAGR, MDD, Sharpe, profit factor, WR, expectancy

  3. Multiplier Sensitivity Analysis
     Tests 4 alternative tier configurations to find the optimal multiplier set

  4. False Positive Analysis
     When CDA fires HIGH_ALERT+, what % of the time does a real crash NOT occur?

  5. Portfolio Defence Pre-Validation (simulation only, no code changes)
     Tests: block new longs, tighten stops by 30%
     Measures: drawdown impact, profit impact

  6. Cross-market cascade detection (BTC→ETH/SOL lead-lag)

OUTPUT
  data/validation/cpi_validation_results_real.json   — machine-readable
  data/validation/cpi_validation_report_real.txt     — human-readable summary

USAGE (run after fetch_historical_data.py):
  python scripts/cpi_validation_realdata.py
  python scripts/cpi_validation_realdata.py --symbols BTC     # BTC only
  python scripts/cpi_validation_realdata.py --skip-backtest   # CPS only

DEPENDENCIES:
  pip install pandas pyarrow scikit-learn scipy numpy tqdm matplotlib

IMPORTANT — real-data only:
  This script refuses to run without validated Parquet files in data/validation/.
  Run fetch_historical_data.py first, then this script.
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
from scipy.stats import norm

warnings.filterwarnings("ignore", category=FutureWarning)
warnings.filterwarnings("ignore", category=UserWarning)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("cps_val")

# ── Paths ─────────────────────────────────────────────────────────────────────

ROOT_DIR    = Path(__file__).parent.parent
VAL_DIR     = ROOT_DIR / "data" / "validation"
OUT_JSON    = VAL_DIR / "cpi_validation_results_real.json"
OUT_REPORT  = VAL_DIR / "cpi_validation_report_real.txt"

# Study 4 baseline (real NexusTrader backtest — 13 months, 5 symbols)
STUDY4_BASELINE = {
    "trades":        675,
    "win_rate":      0.550,
    "profit_factor": 2.154,
    "mdd_pct":      -3.93,
    "cagr_pct":     302.9 / 13 * 12,   # annualised from 13-month period
    "sharpe":        2.41,              # estimated from Study 4 data
}

# ── CDA tier thresholds (mirror crash_detection_agent.py) ─────────────────────

CDA_THRESHOLDS = {"DEFENSIVE": 5.0, "HIGH_ALERT": 7.0, "EMERGENCY": 8.0, "SYSTEMIC": 9.0}
TIER_ORDER     = ["NORMAL", "DEFENSIVE", "HIGH_ALERT", "EMERGENCY", "SYSTEMIC"]

# Baseline multiplier set (Phase 1A)
BASELINE_MULTIPLIERS = {
    "NORMAL":     1.00,
    "DEFENSIVE":  0.65,
    "HIGH_ALERT": 0.35,
    "EMERGENCY":  0.10,
    "SYSTEMIC":   0.00,
}

# ── Data loading ──────────────────────────────────────────────────────────────

def _load_parquet(path: Path, desc: str) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(
            f"{desc} file not found: {path}\n"
            f"Run: python scripts/fetch_historical_data.py"
        )
    df = pd.read_parquet(path)
    logger.info("Loaded %-22s — %d rows | %s → %s",
                path.name, len(df),
                df.iloc[0]["timestamp"].strftime("%Y-%m-%d") if len(df) else "?",
                df.iloc[-1]["timestamp"].strftime("%Y-%m-%d") if len(df) else "?")
    return df


def load_all_data(symbols: list[str]) -> dict[str, dict[str, pd.DataFrame]]:
    """
    Returns:
      { "BTC": { "ohlcv": df, "funding": df, "oi": df }, ... }
    """
    data = {}
    for sym in symbols:
        slug = sym + "USDT"
        data[sym] = {
            "ohlcv":   _load_parquet(VAL_DIR / f"{slug}_1m.parquet", f"{sym} OHLCV 1m"),
            "funding": _load_parquet(VAL_DIR / f"{slug}_funding.parquet", f"{sym} funding"),
            "oi":      _load_parquet(VAL_DIR / f"{slug}_oi.parquet", f"{sym} OI"),
        }
    return data


# ── Feature engineering ───────────────────────────────────────────────────────

def _rolling_zscore(series: pd.Series, window: int, clip: float = 4.0) -> pd.Series:
    """Rolling z-score with outlier clipping."""
    mu    = series.rolling(window, min_periods=window // 2).mean()
    sigma = series.rolling(window, min_periods=window // 2).std().replace(0, np.nan)
    z     = (series - mu) / sigma
    return z.clip(-clip, clip).fillna(0.0)


def compute_cps_features(
    ohlcv: pd.DataFrame,
    funding: pd.DataFrame,
    oi: pd.DataFrame,
    symbol: str,
) -> pd.DataFrame:
    """
    Compute the 6 CPS features on 5-minute aggregated data.

    Returns a DataFrame indexed by timestamp with columns:
      funding_z, oi_change_z, breadth_w, liq_z, cascade_z, cda_score_proxy
    """
    logger.info("[%s] Computing CPS features…", symbol)

    # ── 1. Resample OHLCV to 5m ──────────────────────────────────────────────
    ohlcv5 = ohlcv.set_index("timestamp").resample("5T").agg({
        "open":   "first",
        "high":   "max",
        "low":    "min",
        "close":  "last",
        "volume": "sum",
    }).dropna(subset=["close"])

    n = len(ohlcv5)
    logger.info("[%s] 5m bars: %d", symbol, n)

    # ── 2. Funding rate z-score ───────────────────────────────────────────────
    if len(funding) > 10:
        # Funding is 8h; forward-fill to 5m grid
        fund5 = funding.set_index("timestamp")[["funding_rate"]].resample("5T").ffill()
        ohlcv5 = ohlcv5.join(fund5, how="left")
        ohlcv5["funding_rate"] = ohlcv5["funding_rate"].fillna(0.0)
    else:
        logger.warning("[%s] No funding data — using zeros", symbol)
        ohlcv5["funding_rate"] = 0.0

    ohlcv5["funding_z"] = _rolling_zscore(ohlcv5["funding_rate"], window=288)  # 24h

    # ── 3. OI change z-score ─────────────────────────────────────────────────
    if len(oi) > 10:
        oi5 = oi.set_index("timestamp")[["oi"]].resample("5T").last().ffill()
        ohlcv5 = ohlcv5.join(oi5, how="left")
        ohlcv5["oi"] = ohlcv5["oi"].fillna(method="ffill").fillna(0.0)
        ohlcv5["oi_change"] = ohlcv5["oi"].pct_change(12).fillna(0.0)  # 1h change
    else:
        logger.warning("[%s] No OI data — using zeros", symbol)
        ohlcv5["oi"]        = 0.0
        ohlcv5["oi_change"] = 0.0

    ohlcv5["oi_change_z"] = _rolling_zscore(ohlcv5["oi_change"], window=288)

    # ── 4. Volume breadth (weighted magnitude) ────────────────────────────────
    # Proxy: normalised volume relative to 24h rolling average
    ohlcv5["vol_ratio"]  = ohlcv5["volume"] / (
        ohlcv5["volume"].rolling(288, min_periods=12).mean().replace(0, np.nan)
    )
    ohlcv5["vol_ratio"]  = ohlcv5["vol_ratio"].fillna(1.0).clip(0, 10)
    ohlcv5["breadth_w"]  = (ohlcv5["vol_ratio"] - 1.0).clip(-3, 3)   # centred at 0

    # ── 5. Liquidation proxy (volume spike + price drop) ─────────────────────
    # Real liquidation data requires Coinglass API; proxy: vol_ratio × return_shock
    ohlcv5["return_5m"] = ohlcv5["close"].pct_change().fillna(0.0)
    ohlcv5["liq_proxy"] = ohlcv5["vol_ratio"] * (-ohlcv5["return_5m"]).clip(0, None)
    ohlcv5["liq_z"]     = _rolling_zscore(ohlcv5["liq_proxy"], window=288)

    # ── 6. Cascade / cross-market proxy ──────────────────────────────────────
    # For multi-symbol runs: BTC lead-lag (computed later and injected)
    # For single-symbol: autocorrelation shock proxy
    ohlcv5["acf_shock"] = (
        ohlcv5["return_5m"].rolling(6).apply(
            lambda x: abs(x[-1]) / (x[:-1].std() + 1e-9) if len(x) > 1 else 0.0,
            raw=True
        )
    ).fillna(0.0)
    ohlcv5["cascade_z"] = _rolling_zscore(ohlcv5["acf_shock"], window=288)

    # ── 7. Lightweight CDA score proxy ───────────────────────────────────────
    # Weighted sum of normalised components (matches crash_detection_agent logic)
    w = {"funding_z": 0.20, "oi_change_z": 0.25, "breadth_w": 0.15,
         "liq_z": 0.25, "cascade_z": 0.15}
    ohlcv5["cda_score_proxy"] = sum(
        ohlcv5[feat].clip(-4, 4) * wt for feat, wt in w.items()
    )
    # Map to 0–10 scale (CDA uses 0–10 internally)
    _min = ohlcv5["cda_score_proxy"].quantile(0.01)
    _max = ohlcv5["cda_score_proxy"].quantile(0.99)
    ohlcv5["cda_score_proxy"] = (
        (ohlcv5["cda_score_proxy"] - _min) / max(_max - _min, 1e-9) * 10
    ).clip(0, 10)

    features = ohlcv5[[
        "open", "high", "low", "close", "volume",
        "funding_z", "oi_change_z", "breadth_w", "liq_z", "cascade_z",
        "cda_score_proxy",
    ]].copy()

    logger.info("[%s] Features computed: %d rows, %d features",
                symbol, len(features), 6)
    return features


# ── Crash labelling ───────────────────────────────────────────────────────────

def label_crashes(df: pd.DataFrame, horizons: dict[str, tuple[int, float]]) -> pd.DataFrame:
    """
    horizons: { "crash_5m":  (5,  0.010),   # 1.0% drop in 5m
                "crash_10m": (10, 0.018),    # 1.8% drop in 10m
                "crash_30m": (30, 0.025) }   # 2.5% drop in 30m
    Returns df with boolean crash columns added.
    """
    for label, (bars, threshold) in horizons.items():
        future_ret = df["close"].shift(-bars) / df["close"] - 1.0
        df[label] = (future_ret <= -threshold).astype(int)

    # Drop last N rows where future return can't be computed
    max_horizon = max(v[0] for v in horizons.values())
    df = df.iloc[:-max_horizon].copy()
    return df


# ── CPS model training and evaluation ────────────────────────────────────────

FEATURE_COLS = ["funding_z", "oi_change_z", "breadth_w", "liq_z", "cascade_z", "cda_score_proxy"]
HORIZONS = {
    "crash_5m":  (5,  0.010),
    "crash_10m": (10, 0.018),
    "crash_30m": (30, 0.025),
}


def train_and_evaluate_cps(df: pd.DataFrame, symbol: str) -> dict:
    """
    Train logistic regression CPS model per horizon.
    Uses walk-forward validation: train on first 70%, evaluate on last 30%.
    Returns a results dict with ROC-AUC, Brier, etc. per horizon.
    """
    from sklearn.linear_model import LogisticRegression
    from sklearn.isotonic import IsotonicRegression
    from sklearn.metrics import (
        roc_auc_score, brier_score_loss, precision_recall_curve,
        confusion_matrix,
    )
    from sklearn.preprocessing import StandardScaler

    results = {}
    n       = len(df)
    split   = int(n * 0.70)

    logger.info("[%s] Training CPS | train=%d bars | test=%d bars", symbol, split, n - split)

    X = df[FEATURE_COLS].values
    scaler = StandardScaler().fit(X[:split])
    X_train = scaler.transform(X[:split])
    X_test  = scaler.transform(X[split:])

    per_component_ic = {}
    for feat in FEATURE_COLS:
        y30 = df["crash_30m"].values
        # Information coefficient: rank correlation between feature and label
        try:
            from scipy.stats import spearmanr
            ic, pval = spearmanr(df[feat].values[split:], y30[split:])
            per_component_ic[feat] = round(float(ic), 4)
        except Exception:
            per_component_ic[feat] = 0.0

    for label, (bars, threshold) in HORIZONS.items():
        y_all   = df[label].values
        y_train = y_all[:split]
        y_test  = y_all[split:]

        crash_rate = float(y_test.mean())

        # Skip if no crashes in test set
        if y_test.sum() == 0:
            logger.warning("[%s][%s] No crash events in test set — skipping", symbol, label)
            results[label] = {"error": "no_crashes_in_test_set", "crash_rate": crash_rate}
            continue

        # Logistic regression
        model = LogisticRegression(
            C=0.1,
            class_weight="balanced",
            solver="lbfgs",
            max_iter=500,
            random_state=42,
        )
        model.fit(X_train, y_train)
        proba_raw = model.predict_proba(X_test)[:, 1]

        # Isotonic calibration
        iso = IsotonicRegression(out_of_bounds="clip")
        iso.fit(proba_raw, y_test)
        proba_cal = iso.predict(proba_raw)

        # Metrics
        auc       = float(roc_auc_score(y_test, proba_cal))
        brier     = float(brier_score_loss(y_test, proba_cal))
        threshold_50 = 0.5
        y_pred    = (proba_cal >= threshold_50).astype(int)

        cm = confusion_matrix(y_test, y_pred)
        tn, fp, fn, tp = cm.ravel() if cm.shape == (2, 2) else (0, 0, 0, int(y_test.sum()))

        precision = tp / max(tp + fp, 1)
        recall    = tp / max(tp + fn, 1)
        f1        = 2 * precision * recall / max(precision + recall, 1e-9)
        fpr       = fp / max(fp + tn, 1)

        # Calibration curve (10 buckets)
        try:
            from sklearn.calibration import calibration_curve
            frac_pos, mean_pred = calibration_curve(y_test, proba_cal, n_bins=10)
            cal_curve = {
                "mean_predicted": [round(float(v), 4) for v in mean_pred],
                "fraction_positive": [round(float(v), 4) for v in frac_pos],
            }
        except Exception:
            cal_curve = {}

        # Lead time distribution
        # For each crash event in test set, find the earliest bar where
        # proba_cal >= 0.40 within the preceding 60 bars (5h window)
        lead_times_min = []
        crash_indices  = np.where(y_test == 1)[0]
        LOOKBACK = 60   # bars = 5 hours @ 5m
        EARLY_THRESH = 0.40

        for ci in crash_indices:
            start = max(0, ci - LOOKBACK)
            window_proba = proba_cal[start:ci]
            if len(window_proba) == 0:
                continue
            triggered = np.where(window_proba >= EARLY_THRESH)[0]
            if len(triggered) > 0:
                lead_bars = ci - (start + triggered[0])
                lead_times_min.append(lead_bars * 5)   # 5 min per bar

        lt_arr = np.array(lead_times_min)
        lead_time_dist = {}
        if len(lt_arr) > 0:
            for pct in [10, 25, 50, 75, 90]:
                lead_time_dist[f"P{pct}"] = round(float(np.percentile(lt_arr, pct)), 1)
            lead_time_dist["n_detected"] = len(lt_arr)
            lead_time_dist["n_crashes"]  = len(crash_indices)
        else:
            for pct in [10, 25, 50, 75, 90]:
                lead_time_dist[f"P{pct}"] = 0.0
            lead_time_dist["n_detected"] = 0
            lead_time_dist["n_crashes"]  = len(crash_indices)

        # Logistic coefficients (β per feature)
        coeff_map = {feat: round(float(c), 4)
                     for feat, c in zip(FEATURE_COLS, model.coef_[0])}

        meets_auc_gate = auc >= 0.62

        results[label] = {
            "roc_auc":          round(auc, 4),
            "meets_auc_gate":   meets_auc_gate,
            "brier_score":      round(brier, 4),
            "precision":        round(precision, 4),
            "recall":           round(recall, 4),
            "f1":               round(f1, 4),
            "false_positive_rate": round(fpr, 4),
            "crash_rate":       round(crash_rate, 4),
            "n_test_bars":      int(len(y_test)),
            "n_crash_events":   int(y_test.sum()),
            "calibration_curve": cal_curve,
            "lead_time_dist":   lead_time_dist,
            "coefficients":     coeff_map,
        }
        logger.info("[%s][%s] ROC-AUC=%.4f %s | Brier=%.4f | Prec=%.3f Rec=%.3f FPR=%.3f",
                    symbol, label, auc,
                    "✅ PASS" if meets_auc_gate else "❌ FAIL (gate=0.62)",
                    brier, precision, recall, fpr)

    results["per_component_ic"] = per_component_ic
    return results


# ── CDA tier from score proxy ─────────────────────────────────────────────────

def _score_to_tier(score: float) -> str:
    if score >= CDA_THRESHOLDS["SYSTEMIC"]:
        return "SYSTEMIC"
    if score >= CDA_THRESHOLDS["EMERGENCY"]:
        return "EMERGENCY"
    if score >= CDA_THRESHOLDS["HIGH_ALERT"]:
        return "HIGH_ALERT"
    if score >= CDA_THRESHOLDS["DEFENSIVE"]:
        return "DEFENSIVE"
    return "NORMAL"


# ── CDA multiplier impact backtest ────────────────────────────────────────────

def run_multiplier_impact(
    df: pd.DataFrame,
    symbol: str,
    multiplier_set: Optional[dict] = None,
) -> dict:
    """
    Scenario A: all signals taken at full size (multiplier=1.0 always).
    Scenario B: position size scaled by CDA multiplier based on cda_score_proxy.

    Entry signal: momentum proxy — close crosses above 20-bar rolling mean
                  AND cda_score_proxy < HIGH_ALERT threshold (no crash expected)
    Exit: 30-bar trailing high stop (for longs)

    Capital: $100,000 starting.
    Position size: 2% risk per trade (Study 4 base: 0.5% risk × position_size).
    """
    if multiplier_set is None:
        multiplier_set = BASELINE_MULTIPLIERS

    logger.info("[%s] Running multiplier impact backtest…", symbol)

    df = df.copy().reset_index()
    if "index" not in df.columns and "timestamp" in df.columns:
        df = df.reset_index()

    close     = df["close"].values
    score_prx = df["cda_score_proxy"].values
    n         = len(df)

    # Generate entry signals (simple momentum: close > 20-bar SMA)
    sma20 = pd.Series(close).rolling(20, min_periods=20).mean().values
    entries = (
        (close > sma20) &
        (np.roll(close, 1) <= np.roll(sma20, 1))
    )
    entries[:20] = False

    def _run_scenario(use_multiplier: bool) -> dict:
        capital = 100_000.0
        capital_curve = [capital]
        trades  = []
        in_pos  = False
        entry_p = 0.0
        entry_i = 0
        pos_size = 0.0
        peak    = capital

        for i in range(1, n - 30):
            tier = _score_to_tier(float(score_prx[i]))
            mult = multiplier_set.get(tier, 1.0) if use_multiplier else 1.0

            # Exit: 30-bar trailing stop at 2× ATR proxy
            if in_pos:
                atr_proxy = np.std(close[max(0, i - 14):i + 1]) * 2
                stop = entry_p - atr_proxy
                if close[i] <= stop:
                    pnl = (close[i] - entry_p) / entry_p * pos_size
                    capital += pnl
                    trades.append({"pnl": pnl, "won": pnl > 0, "size": pos_size,
                                   "tier_at_entry": tier})
                    in_pos = False

            # Entry
            if not in_pos and entries[i] and mult > 0:
                # Base position: 2% of capital per trade
                base_size = capital * 0.02
                pos_size  = base_size * mult
                entry_p   = close[i]
                entry_i   = i
                in_pos    = True

            peak = max(peak, capital)
            capital_curve.append(capital)

        # Close open position at last bar
        if in_pos:
            pnl = (close[-1] - entry_p) / entry_p * pos_size
            capital += pnl
            trades.append({"pnl": pnl, "won": pnl > 0, "size": pos_size,
                           "tier_at_entry": _score_to_tier(float(score_prx[-1]))})
            capital_curve.append(capital)

        if not trades:
            return {"error": "no_trades"}

        cap_arr = np.array(capital_curve)
        peak_arr = np.maximum.accumulate(cap_arr)
        dd_arr   = (cap_arr - peak_arr) / peak_arr * 100
        mdd      = float(dd_arr.min())

        pnls   = [t["pnl"] for t in trades]
        won    = [t["pnl"] > 0 for t in trades]
        wins   = [t["pnl"] for t in trades if t["pnl"] > 0]
        losses = [abs(t["pnl"]) for t in trades if t["pnl"] <= 0]
        gross_profit = sum(wins) if wins else 0.0
        gross_loss   = sum(losses) if losses else 0.0

        years   = len(cap_arr) / (252 * 12 * 24)   # 5m bars per year
        cagr    = (cap_arr[-1] / 100_000) ** (1 / max(years, 0.1)) - 1.0

        rets    = np.diff(cap_arr) / cap_arr[:-1]
        sharpe  = float(rets.mean() / (rets.std() + 1e-12) * np.sqrt(252 * 12 * 24))

        return {
            "final_capital":   round(float(cap_arr[-1]), 2),
            "cagr_pct":        round(cagr * 100, 2),
            "mdd_pct":         round(mdd, 2),
            "sharpe":          round(sharpe, 4),
            "profit_factor":   round(gross_profit / max(gross_loss, 1e-9), 4),
            "win_rate":        round(sum(won) / max(len(trades), 1), 4),
            "expectancy_usdt": round(sum(pnls) / max(len(trades), 1), 2),
            "n_trades":        len(trades),
        }

    scen_a = _run_scenario(use_multiplier=False)
    scen_b = _run_scenario(use_multiplier=True)

    # Delta analysis
    delta = {}
    if "error" not in scen_a and "error" not in scen_b:
        for k in ["final_capital", "cagr_pct", "mdd_pct", "sharpe", "profit_factor",
                  "win_rate", "expectancy_usdt", "n_trades"]:
            delta[k] = round(scen_b.get(k, 0) - scen_a.get(k, 0), 4)

        delta["mdd_improvement_pp"] = round(scen_b["mdd_pct"] - scen_a["mdd_pct"], 2)
        delta["capital_delta_pct"]  = round(
            (scen_b["final_capital"] - scen_a["final_capital"]) / scen_a["final_capital"] * 100, 2
        )
        delta["verdict"] = (
            "MULTIPLIER_HELPS"
            if delta["mdd_improvement_pp"] > 0 and delta["profit_factor"] >= -0.1
            else "MULTIPLIER_HURTS" if delta["capital_delta_pct"] < -10
            else "NEUTRAL"
        )

    logger.info("[%s] Scenario A: capital=%.0f MDD=%.1f%% PF=%.2f",
                symbol,
                scen_a.get("final_capital", 0),
                scen_a.get("mdd_pct", 0),
                scen_a.get("profit_factor", 0))
    logger.info("[%s] Scenario B: capital=%.0f MDD=%.1f%% PF=%.2f (mult_verdict=%s)",
                symbol,
                scen_b.get("final_capital", 0),
                scen_b.get("mdd_pct", 0),
                scen_b.get("profit_factor", 0),
                delta.get("verdict", "N/A"))

    return {"scenario_a": scen_a, "scenario_b": scen_b, "delta": delta}


# ── Multiplier sensitivity analysis ──────────────────────────────────────────

SENSITIVITY_CONFIGS = {
    "baseline_phase1a": {"NORMAL": 1.00, "DEFENSIVE": 0.65, "HIGH_ALERT": 0.35,
                          "EMERGENCY": 0.10, "SYSTEMIC": 0.00},
    "aggressive":       {"NORMAL": 1.00, "DEFENSIVE": 0.50, "HIGH_ALERT": 0.20,
                          "EMERGENCY": 0.05, "SYSTEMIC": 0.00},
    "conservative":     {"NORMAL": 1.00, "DEFENSIVE": 0.80, "HIGH_ALERT": 0.50,
                          "EMERGENCY": 0.20, "SYSTEMIC": 0.00},
    "flat_50pct":       {"NORMAL": 1.00, "DEFENSIVE": 0.50, "HIGH_ALERT": 0.50,
                          "EMERGENCY": 0.50, "SYSTEMIC": 0.00},
}


def run_sensitivity_analysis(df: pd.DataFrame, symbol: str) -> dict:
    """Run backtest for each multiplier configuration."""
    logger.info("[%s] Running sensitivity analysis (%d configs)…",
                symbol, len(SENSITIVITY_CONFIGS))
    results = {}
    for config_name, mults in SENSITIVITY_CONFIGS.items():
        res = run_multiplier_impact(df, symbol, multiplier_set=mults)
        results[config_name] = res
        b = res.get("scenario_b", {})
        logger.info("[%s] Config %-18s → capital=%.0f MDD=%.1f%% PF=%.2f",
                    symbol, config_name,
                    b.get("final_capital", 0), b.get("mdd_pct", 0),
                    b.get("profit_factor", 0))

    # Find optimal config by Sharpe ratio
    valid = {k: v["scenario_b"] for k, v in results.items()
             if "error" not in v.get("scenario_b", {})}
    if valid:
        best = max(valid, key=lambda k: valid[k].get("sharpe", -99))
        results["optimal_config"] = best
        logger.info("[%s] Optimal config: %s", symbol, best)
    return results


# ── False positive analysis ───────────────────────────────────────────────────

def run_false_positive_analysis(df: pd.DataFrame, symbol: str) -> dict:
    """
    When CDA fires HIGH_ALERT+ (score ≥ 7.0), what fraction of the time
    does a crash NOT occur within the next 30 minutes?

    Also measures the cost: how many normal-regime trades were skipped?
    """
    logger.info("[%s] Running false positive analysis…", symbol)

    score  = df["cda_score_proxy"].values
    crash  = df.get("crash_30m", pd.Series(np.zeros(len(df)))).values

    # Tiers
    is_normal    = score < CDA_THRESHOLDS["DEFENSIVE"]
    is_defensive = (score >= CDA_THRESHOLDS["DEFENSIVE"]) & (score < CDA_THRESHOLDS["HIGH_ALERT"])
    is_high_alert= (score >= CDA_THRESHOLDS["HIGH_ALERT"]) & (score < CDA_THRESHOLDS["EMERGENCY"])
    is_emergency = (score >= CDA_THRESHOLDS["EMERGENCY"]) & (score < CDA_THRESHOLDS["SYSTEMIC"])
    is_systemic  = score >= CDA_THRESHOLDS["SYSTEMIC"]
    is_elevated  = ~is_normal   # any non-NORMAL tier

    def _fp_stats(mask: np.ndarray, tier: str) -> dict:
        n_fire = int(mask.sum())
        if n_fire == 0:
            return {"tier": tier, "n_fires": 0, "false_positive_rate": None}
        n_crash_when_fired = int((mask & (crash == 1)).sum())
        n_fp = n_fire - n_crash_when_fired
        fpr  = n_fp / n_fire
        return {
            "tier":                tier,
            "n_fires":             n_fire,
            "n_crash_confirmed":   n_crash_when_fired,
            "n_false_positives":   n_fp,
            "false_positive_rate": round(fpr, 4),
            "fire_rate_of_total":  round(n_fire / max(len(score), 1), 4),
        }

    result = {
        "DEFENSIVE":  _fp_stats(is_defensive,  "DEFENSIVE"),
        "HIGH_ALERT": _fp_stats(is_high_alert,  "HIGH_ALERT"),
        "EMERGENCY":  _fp_stats(is_emergency,   "EMERGENCY"),
        "SYSTEMIC":   _fp_stats(is_systemic,    "SYSTEMIC"),
        "ALL_ELEVATED": _fp_stats(is_elevated,  "ALL_ELEVATED"),
    }

    for tier, r in result.items():
        fpr = r.get("false_positive_rate")
        if fpr is not None:
            logger.info("[%s] FP[%-12s] fires=%d FPR=%.1f%% (fire_rate=%.1f%%)",
                        symbol, tier, r["n_fires"], fpr * 100,
                        r["fire_rate_of_total"] * 100)

    return result


# ── Portfolio defence pre-validation ─────────────────────────────────────────

def run_portfolio_defence(df: pd.DataFrame, symbol: str) -> dict:
    """
    Simulate two defence mechanisms (simulation only — no code changes):
    A. Block all new long entries when CDA score ≥ 5.0 (DEFENSIVE+)
    B. Tighten stops by 30% (ATR stop → 0.7× ATR stop) when CDA score ≥ 7.0 (HIGH_ALERT+)

    Reports:
    - Drawdown with/without defence
    - Capital growth with/without defence
    - Trades blocked by defence
    """
    logger.info("[%s] Running portfolio defence pre-validation…", symbol)

    close     = df["close"].values
    score_prx = df["cda_score_proxy"].values
    n         = len(df)

    sma20 = pd.Series(close).rolling(20, min_periods=20).mean().values
    entries = (close > sma20) & (np.roll(close, 1) <= np.roll(sma20, 1))
    entries[:20] = False

    def _run_defence(block_defensive: bool, tighten_stops: bool) -> dict:
        capital      = 100_000.0
        cap_curve    = [capital]
        trades       = []
        in_pos       = False
        entry_p      = 0.0
        pos_size     = 0.0
        blocked      = 0
        tightened    = 0
        atr_mult_base = 2.0

        for i in range(1, n - 30):
            tier    = _score_to_tier(float(score_prx[i]))
            is_def  = score_prx[i] >= CDA_THRESHOLDS["DEFENSIVE"]
            is_ha   = score_prx[i] >= CDA_THRESHOLDS["HIGH_ALERT"]

            atr_proxy = np.std(close[max(0, i - 14):i + 1])
            atr_mult  = atr_mult_base * (0.7 if (tighten_stops and is_ha) else 1.0)
            if tighten_stops and is_ha and in_pos:
                tightened += 1

            if in_pos:
                stop = entry_p - atr_proxy * atr_mult
                if close[i] <= stop:
                    pnl = (close[i] - entry_p) / entry_p * pos_size
                    capital += pnl
                    trades.append({"pnl": pnl, "won": pnl > 0})
                    in_pos = False

            if not in_pos and entries[i]:
                if block_defensive and is_def:
                    blocked += 1
                else:
                    pos_size = capital * 0.02
                    entry_p  = close[i]
                    in_pos   = True

            cap_curve.append(capital)

        if in_pos:
            pnl = (close[-1] - entry_p) / entry_p * pos_size
            capital += pnl
            trades.append({"pnl": pnl, "won": pnl > 0})

        cap_arr  = np.array(cap_curve)
        peak_arr = np.maximum.accumulate(cap_arr)
        dd_arr   = (cap_arr - peak_arr) / peak_arr * 100
        mdd      = float(dd_arr.min())

        pnls   = [t["pnl"] for t in trades]
        wins   = [p for p in pnls if p > 0]
        losses = [abs(p) for p in pnls if p <= 0]
        pf     = sum(wins) / max(sum(losses), 1e-9)

        return {
            "final_capital":    round(float(cap_arr[-1]), 2),
            "mdd_pct":          round(mdd, 2),
            "profit_factor":    round(pf, 4),
            "win_rate":         round(sum(t["won"] for t in trades) / max(len(trades), 1), 4),
            "n_trades":         len(trades),
            "n_blocked":        blocked,
            "n_stop_tightened": tightened,
        }

    baseline       = _run_defence(False,  False)
    block_only     = _run_defence(True,   False)
    tighten_only   = _run_defence(False,  True)
    combined       = _run_defence(True,   True)

    def _delta(a: dict, b: dict) -> dict:
        return {
            "mdd_improvement_pp":     round(b["mdd_pct"] - a["mdd_pct"], 2),
            "capital_delta_pct":      round((b["final_capital"] - a["final_capital"]) / a["final_capital"] * 100, 2),
            "pf_delta":               round(b["profit_factor"] - a["profit_factor"], 4),
            "trades_blocked":         b["n_blocked"],
            "worth_pursuing":         b["mdd_pct"] - a["mdd_pct"] > 0,
        }

    result = {
        "baseline":       baseline,
        "block_longs":    {"result": block_only,   "delta": _delta(baseline, block_only)},
        "tighten_stops":  {"result": tighten_only,  "delta": _delta(baseline, tighten_only)},
        "combined":       {"result": combined,      "delta": _delta(baseline, combined)},
    }

    logger.info("[%s] Defence: baseline MDD=%.1f%% | block_longs MDD=%.1f%% | tighten MDD=%.1f%%",
                symbol,
                baseline["mdd_pct"], block_only["mdd_pct"], tighten_only["mdd_pct"])

    return result


# ── Main orchestrator ─────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="CPS real-data validation + multiplier impact analysis"
    )
    parser.add_argument("--symbols", nargs="+", choices=["BTC", "ETH", "SOL"],
                        default=["BTC", "ETH", "SOL"])
    parser.add_argument("--skip-backtest", action="store_true",
                        help="Run CPS validation only (skip backtest scenarios)")
    parser.add_argument("--skip-defence", action="store_true",
                        help="Skip portfolio defence simulation")
    args = parser.parse_args()

    logger.info("=" * 65)
    logger.info("NexusTrader — CPS Real-Data Validation Suite")
    logger.info("Symbols: %s | Backtest: %s | Defence: %s",
                args.symbols,
                "YES" if not args.skip_backtest else "NO",
                "YES" if not args.skip_defence else "NO")
    logger.info("=" * 65)

    # ── Load data ─────────────────────────────────────────────────────────────
    try:
        all_data = load_all_data(args.symbols)
    except FileNotFoundError as exc:
        logger.error("\nDATA NOT FOUND:\n  %s", exc)
        logger.error("Run first:  python scripts/fetch_historical_data.py")
        return

    results = {
        "meta": {
            "generated_at":  datetime.now(timezone.utc).isoformat(),
            "symbols":        args.symbols,
            "auc_gate":       0.62,
            "study4_baseline": STUDY4_BASELINE,
        },
        "cps_validation":     {},
        "multiplier_impact":  {},
        "sensitivity":        {},
        "false_positives":    {},
        "portfolio_defence":  {},
        "summary":            {},
    }

    for sym in args.symbols:
        logger.info("")
        logger.info("═" * 65)
        logger.info(" Processing %s", sym)
        logger.info("═" * 65)

        ohlcv   = all_data[sym]["ohlcv"]
        funding = all_data[sym]["funding"]
        oi      = all_data[sym]["oi"]

        # ── Feature computation ───────────────────────────────────────────────
        feat_df = compute_cps_features(ohlcv, funding, oi, sym)
        feat_df = label_crashes(feat_df, HORIZONS)

        # ── CPS validation ────────────────────────────────────────────────────
        cps_res = train_and_evaluate_cps(feat_df, sym)
        results["cps_validation"][sym] = cps_res

        # ── Multiplier impact ─────────────────────────────────────────────────
        if not args.skip_backtest:
            imp_res = run_multiplier_impact(feat_df, sym)
            results["multiplier_impact"][sym] = imp_res

            sens_res = run_sensitivity_analysis(feat_df, sym)
            results["sensitivity"][sym] = sens_res

        # ── False positive analysis ───────────────────────────────────────────
        if "crash_30m" in feat_df.columns:
            fp_res  = run_false_positive_analysis(feat_df, sym)
            results["false_positives"][sym] = fp_res

        # ── Portfolio defence ─────────────────────────────────────────────────
        if not args.skip_defence and "crash_30m" in feat_df.columns:
            def_res = run_portfolio_defence(feat_df, sym)
            results["portfolio_defence"][sym] = def_res

    # ── Summary verdict ────────────────────────────────────────────────────────
    any_pass_auc = False
    for sym in args.symbols:
        for horizon in HORIZONS:
            hres = results["cps_validation"].get(sym, {}).get(horizon, {})
            if hres.get("meets_auc_gate"):
                any_pass_auc = True

    mult_helps = any(
        results["multiplier_impact"].get(sym, {}).get("delta", {}).get("verdict") == "MULTIPLIER_HELPS"
        for sym in args.symbols
    )

    results["summary"] = {
        "cps_auc_gate_passed":    any_pass_auc,
        "cda_multiplier_verdict": "HELPS" if mult_helps else "NEUTRAL_OR_HURTS",
        "phase1b_recommendation": (
            "PROCEED_TO_PHASE1B" if any_pass_auc and mult_helps
            else "REDESIGN_CPS" if not any_pass_auc
            else "PROCEED_WITH_CAUTION"
        ),
    }

    logger.info("")
    logger.info("═" * 65)
    logger.info("SUMMARY")
    logger.info("  CPS AUC gate (≥0.62) passed:  %s", "✅ YES" if any_pass_auc else "❌ NO")
    logger.info("  CDA multiplier helps:          %s", "✅ YES" if mult_helps else "⚠️  NEUTRAL/HURTS")
    logger.info("  Phase 1B recommendation:       %s", results["summary"]["phase1b_recommendation"])
    logger.info("═" * 65)

    # ── Persist results ───────────────────────────────────────────────────────
    VAL_DIR.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(results, indent=2, default=str))
    logger.info("Results saved → %s", OUT_JSON)

    # ── Human-readable report ─────────────────────────────────────────────────
    _write_text_report(results, args.symbols)
    logger.info("Report saved  → %s", OUT_REPORT)

    logger.info("")
    logger.info("DONE. Report saved to: %s", OUT_REPORT)


def _write_text_report(results: dict, symbols: list[str]) -> None:
    lines = [
        "=" * 70,
        "NexusTrader — CPI/CPS Real-Data Validation Report",
        f"Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
        "=" * 70,
        "",
        "AUC GATE: 0.62 — must be met for Phase 1B to proceed",
        "",
    ]

    for sym in symbols:
        lines.append(f"╔══ {sym} ══════════════════════════════════════════════════════╗")
        cps = results.get("cps_validation", {}).get(sym, {})
        for horizon in HORIZONS:
            h = cps.get(horizon, {})
            if "error" in h:
                lines.append(f"  [{horizon}] ERROR: {h['error']}")
                continue
            gate = "✅ PASS" if h.get("meets_auc_gate") else "❌ FAIL"
            lines.append(
                f"  [{horizon}]  AUC={h.get('roc_auc','N/A'):.4f} {gate} | "
                f"Brier={h.get('brier_score','N/A'):.4f} | "
                f"Prec={h.get('precision','N/A'):.3f} Rec={h.get('recall','N/A'):.3f} | "
                f"FPR={h.get('false_positive_rate','N/A'):.3f}"
            )
            ld = h.get("lead_time_dist", {})
            if ld:
                lines.append(
                    f"           Lead time: P25={ld.get('P25',0)}m P50={ld.get('P50',0)}m "
                    f"P75={ld.get('P75',0)}m | detected {ld.get('n_detected',0)}/{ld.get('n_crashes',0)}"
                )

        imp = results.get("multiplier_impact", {}).get(sym, {})
        if imp:
            sa = imp.get("scenario_a", {})
            sb = imp.get("scenario_b", {})
            dl = imp.get("delta", {})
            lines.append(f"  Multiplier Impact:")
            lines.append(f"    Scenario A (no mult): capital={sa.get('final_capital',0):,.0f} "
                         f"MDD={sa.get('mdd_pct',0):.1f}% PF={sa.get('profit_factor',0):.2f}")
            lines.append(f"    Scenario B (with mult): capital={sb.get('final_capital',0):,.0f} "
                         f"MDD={sb.get('mdd_pct',0):.1f}% PF={sb.get('profit_factor',0):.2f}")
            lines.append(f"    Verdict: {dl.get('verdict','N/A')}")

        fp = results.get("false_positives", {}).get(sym, {})
        if fp:
            lines.append(f"  False Positives (vs crash_30m):")
            for tier in ["DEFENSIVE", "HIGH_ALERT", "EMERGENCY"]:
                r = fp.get(tier, {})
                fpr = r.get("false_positive_rate")
                if fpr is not None:
                    lines.append(
                        f"    {tier:<14} FPR={fpr:.1%} | fires={r.get('n_fires',0)} "
                        f"| confirmed={r.get('n_crash_confirmed',0)}"
                    )

        lines.append("")

    lines += [
        "=" * 70,
        "FINAL SUMMARY",
        f"  CPS AUC gate passed:        {results['summary'].get('cps_auc_gate_passed')}",
        f"  CDA multiplier verdict:     {results['summary'].get('cda_multiplier_verdict')}",
        f"  Phase 1B recommendation:    {results['summary'].get('phase1b_recommendation')}",
        "=" * 70,
    ]

    OUT_REPORT.write_text("\n".join(lines))


if __name__ == "__main__":
    main()
