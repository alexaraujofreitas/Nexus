"""
Phase 2c Step 1: FeatureTransitionDetector Validation
=====================================================
Density test + Quality test (IS / OOS) for all 3 transition types.

Tests:
  Phase A — Density: count signals per type/symbol/year
  Phase B — Quality: simulate trades with execution realism
  Phase C — IS/OOS split analysis
  Phase D — Confidence bucket performance
  Phase E — Breakout confirmation ON vs OFF comparison

Pass/fail gates per design doc Section 7.
"""

import json
import logging
import os
import sys
import time
from collections import defaultdict
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

logging.basicConfig(level=logging.WARNING, format="%(message)s")
logger = logging.getLogger("phase2c_validation")
logger.setLevel(logging.INFO)

# ════════════════════════════════════════════════════════════
# Constants
# ════════════════════════════════════════════════════════════
DATE_START = "2022-03-22"
DATE_END   = "2026-03-21"
SYMBOLS    = ["BTC/USDT", "SOL/USDT", "ETH/USDT"]

IS_CUTOFF  = "2025-09-22"   # 3.5yr IS | 6mo OOS

FEE        = 0.0004
SLIPPAGE   = 0.0003
COST       = FEE + SLIPPAGE  # 0.0007 per side

# SL/TP per event type (30m ATR multiples)
SL_TP = {
    "compression_expansion":   (2.0, 4.0),
    "range_breakout":          (1.5, 3.0),
    "pullback_continuation":   (2.5, 3.5),
}

MAX_HOLD_BARS = 200  # 30m bars ≈ 100 hours

# ════════════════════════════════════════════════════════════
# Data loading
# ════════════════════════════════════════════════════════════

def load_data():
    """Load all timeframes via BacktestRunner."""
    from research.engine.backtest_runner import BacktestRunner

    logger.info("Loading data (mode=custom for indicator cache)...")
    runner = BacktestRunner(
        date_start=DATE_START, date_end=DATE_END,
        mode="custom",
        strategy_subset=["pullback_long"],  # need indicators computed
    )
    runner.load_data()

    data = {}
    for sym in SYMBOLS:
        data[sym] = {
            "30m": runner._ind[sym].get("30m"),
            "1h":  runner._ind[sym].get("1h"),
            "4h":  runner._ind[sym].get("4h"),
        }
        for tf, df in data[sym].items():
            if df is not None and len(df) > 0:
                logger.info("  %s %s: %d bars", sym, tf, len(df))
            else:
                logger.warning("  %s %s: NO DATA", sym, tf)

    return data, runner


# ════════════════════════════════════════════════════════════
# Signal generation — run detector over full dataset
# ════════════════════════════════════════════════════════════

def run_detector(data: dict, params: dict = None) -> list[dict]:
    """Run FeatureTransitionDetector over all symbols, collect signals."""
    from core.regime.feature_transition_detector import FeatureTransitionDetector

    all_signals = []

    for sym in SYMBOLS:
        df_1h = data[sym]["1h"]
        df_4h = data[sym]["4h"]
        df_30m = data[sym]["30m"]

        if df_1h is None or len(df_1h) < 100:
            logger.warning("  %s: insufficient 1h data, skipping", sym)
            continue

        det = FeatureTransitionDetector(params=params)

        # Build 4h index lookup: for each 1h bar, find the concurrent 4h bar
        idx_4h_map = None
        if df_4h is not None and len(df_4h) > 0:
            ts_4h = df_4h.index.asi8
            ts_1h = df_1h.index.asi8
            idx_4h_arr = np.searchsorted(ts_4h, ts_1h, side="right") - 1
            idx_4h_arr = np.clip(idx_4h_arr, 0, len(df_4h) - 1)
            idx_4h_map = idx_4h_arr

        # Build 30m index lookup: for each 1h bar, find the concurrent 30m bar
        idx_30m_map = None
        if df_30m is not None and len(df_30m) > 0:
            ts_30m = df_30m.index.asi8
            ts_1h = df_1h.index.asi8
            idx_30m_arr = np.searchsorted(ts_30m, ts_1h, side="right") - 1
            idx_30m_arr = np.clip(idx_30m_arr, 0, len(df_30m) - 1)
            idx_30m_map = idx_30m_arr

        n_1h = len(df_1h)
        sym_count = 0

        for i in range(60, n_1h):
            i4h = int(idx_4h_map[i]) if idx_4h_map is not None else None
            events = det.detect(df_1h, i, df_4h=df_4h, idx_4h=i4h)

            for ev in events:
                # Find corresponding 30m bar for entry simulation
                i30m = int(idx_30m_map[i]) if idx_30m_map is not None else None

                sig = {
                    "symbol": sym,
                    "event_type": ev.event_type,
                    "direction": ev.direction,
                    "confidence": ev.confidence,
                    "bar_idx_1h": i,
                    "bar_idx_30m": i30m,
                    "timestamp": str(ev.bar_timestamp),
                    "features": ev.features_snapshot,
                }
                all_signals.append(sig)
                sym_count += 1

        logger.info("  %s: %d signals", sym, sym_count)

    return all_signals


# ════════════════════════════════════════════════════════════
# Quality simulation
# ════════════════════════════════════════════════════════════

def simulate_quality(signals: list, data: dict) -> list[dict]:
    """Simulate trades for each signal: next-bar-open entry on 30m with fees+slippage."""
    outcomes = []

    for sig in signals:
        sym = sig["symbol"]
        df_30m = data[sym]["30m"]
        if df_30m is None:
            continue

        i30m = sig["bar_idx_30m"]
        if i30m is None or i30m + 1 >= len(df_30m):
            continue

        direction = sig["direction"]
        event_type = sig["event_type"]
        sl_mult, tp_mult = SL_TP.get(event_type, (2.0, 3.0))

        # Entry at next 30m bar open
        entry_raw = float(df_30m.iloc[i30m + 1]["open"])
        atr_30m = float(df_30m.iloc[i30m + 1].get("atr_14", df_30m.iloc[i30m + 1].get("atr", 0)))
        if np.isnan(atr_30m) or atr_30m <= 0:
            # Try current bar
            atr_30m = float(df_30m.iloc[i30m].get("atr_14", df_30m.iloc[i30m].get("atr", 0)))
            if np.isnan(atr_30m) or atr_30m <= 0:
                continue

        if direction == "long":
            entry = entry_raw * (1 + COST)
            sl = entry_raw - sl_mult * atr_30m
            tp = entry_raw + tp_mult * atr_30m
            valid = sl < entry_raw < tp
        else:
            entry = entry_raw * (1 - COST)
            sl = entry_raw + sl_mult * atr_30m
            tp = entry_raw - tp_mult * atr_30m
            valid = tp < entry_raw < sl

        if not valid:
            outcomes.append({**sig, "r": -1.0, "max_r": 0, "hit_sl": True, "hit_tp": False, "reason": "gap"})
            continue

        risk = abs(entry - sl)
        if risk <= 0:
            continue

        # Walk forward
        hit_sl = hit_tp = False
        max_r = 0.0

        for j in range(i30m + 2, min(i30m + MAX_HOLD_BARS, len(df_30m))):
            hi = float(df_30m.iloc[j]["high"])
            lo = float(df_30m.iloc[j]["low"])

            if direction == "long":
                if lo <= sl:
                    hit_sl = True; break
                if hi >= tp:
                    hit_tp = True; break
                max_r = max(max_r, (hi - entry) / risk)
            else:
                if hi >= sl:
                    hit_sl = True; break
                if lo <= tp:
                    hit_tp = True; break
                max_r = max(max_r, (entry - lo) / risk)

        if hit_tp:
            exit_p = tp * (1 - COST) if direction == "long" else tp * (1 + COST)
        elif hit_sl:
            exit_p = sl * (1 - COST) if direction == "long" else sl * (1 + COST)
        else:
            last = float(df_30m.iloc[min(i30m + MAX_HOLD_BARS, len(df_30m) - 1)]["close"])
            exit_p = last * (1 - COST) if direction == "long" else last * (1 + COST)

        pnl = (exit_p - entry) if direction == "long" else (entry - exit_p)
        r_val = pnl / risk

        outcomes.append({
            **sig,
            "r": round(r_val, 4),
            "max_r": round(max_r, 4),
            "hit_sl": hit_sl,
            "hit_tp": hit_tp,
            "reason": "tp" if hit_tp else ("sl" if hit_sl else "timeout"),
        })

    return outcomes


# ════════════════════════════════════════════════════════════
# Analysis helpers
# ════════════════════════════════════════════════════════════

def compute_metrics(outcomes: list) -> dict:
    if not outcomes:
        return {"n": 0, "wr": 0, "avg_r": 0, "median_r": 0, "pct_1r": 0, "pct_2r": 0, "false_bk_rate": 0}

    n = len(outcomes)
    rs = [o["r"] for o in outcomes]
    max_rs = [o.get("max_r", 0) for o in outcomes]

    wins = sum(1 for r in rs if r > 0)
    reached_1r = sum(1 for i, r in enumerate(rs) if r >= 1.0 or max_rs[i] >= 1.0)
    reached_2r = sum(1 for i, r in enumerate(rs) if r >= 2.0 or max_rs[i] >= 2.0)
    false_bk = sum(1 for o in outcomes if o.get("hit_sl") and o.get("max_r", 0) < 0.5)

    return {
        "n": n,
        "wr": round(wins / n, 4),
        "avg_r": round(float(np.mean(rs)), 4),
        "median_r": round(float(np.median(rs)), 4),
        "pct_1r": round(reached_1r / n, 4),
        "pct_2r": round(reached_2r / n, 4),
        "false_bk_rate": round(false_bk / n, 4),
        "tp_hits": sum(1 for o in outcomes if o.get("hit_tp")),
        "sl_hits": sum(1 for o in outcomes if o.get("hit_sl")),
        "timeouts": sum(1 for o in outcomes if o.get("reason") == "timeout"),
    }


def split_is_oos(outcomes: list):
    is_out = [o for o in outcomes if o["timestamp"] < IS_CUTOFF]
    oos_out = [o for o in outcomes if o["timestamp"] >= IS_CUTOFF]
    return is_out, oos_out


def confidence_buckets(outcomes: list) -> dict:
    buckets = {
        "0.35-0.45": [], "0.45-0.55": [], "0.55-0.65": [],
        "0.65-0.75": [], "0.75-0.85": [],
    }
    for o in outcomes:
        c = o.get("confidence", 0)
        if c < 0.45:
            buckets["0.35-0.45"].append(o)
        elif c < 0.55:
            buckets["0.45-0.55"].append(o)
        elif c < 0.65:
            buckets["0.55-0.65"].append(o)
        elif c < 0.75:
            buckets["0.65-0.75"].append(o)
        else:
            buckets["0.75-0.85"].append(o)

    return {k: compute_metrics(v) for k, v in buckets.items()}


def density_breakdown(signals: list) -> dict:
    by_type = defaultdict(int)
    by_symbol = defaultdict(int)
    by_year = defaultdict(int)
    by_dir = defaultdict(int)
    by_type_sym = defaultdict(lambda: defaultdict(int))

    for s in signals:
        by_type[s["event_type"]] += 1
        by_symbol[s["symbol"]] += 1
        by_dir[s["direction"]] += 1
        by_type_sym[s["event_type"]][s["symbol"]] += 1
        try:
            by_year[s["timestamp"][:4]] += 1
        except Exception:
            pass

    # Per-type per-year for CV calculation
    by_type_year = defaultdict(lambda: defaultdict(int))
    for s in signals:
        try:
            by_type_year[s["event_type"]][s["timestamp"][:4]] += 1
        except Exception:
            pass

    type_cv = {}
    for et, years in by_type_year.items():
        vals = list(years.values())
        if len(vals) > 1 and np.mean(vals) > 0:
            type_cv[et] = round(float(np.std(vals) / np.mean(vals)), 3)
        else:
            type_cv[et] = 0

    return {
        "total": len(signals),
        "by_type": dict(by_type),
        "by_symbol": dict(by_symbol),
        "by_direction": dict(by_dir),
        "by_year": dict(by_year),
        "by_type_symbol": {k: dict(v) for k, v in by_type_sym.items()},
        "by_type_year": {k: dict(v) for k, v in by_type_year.items()},
        "type_cv": type_cv,
    }


def judge_type(et: str, density: dict, quality_is: dict, quality_oos: dict) -> dict:
    """Apply pass/fail gates per design doc Section 7."""
    n = density["by_type"].get(et, 0)

    # Density verdict
    if n >= 400:
        d_verdict = "PASS (strong)"
    elif n >= 200:
        d_verdict = "PASS (adequate)"
    elif n >= 100:
        d_verdict = "MARGINAL"
    else:
        d_verdict = "FAIL"

    # Quality verdict (IS)
    q_pass = (
        quality_is.get("pct_1r", 0) >= 0.30 and
        quality_is.get("pct_2r", 0) >= 0.15 and
        quality_is.get("false_bk_rate", 1) <= 0.55 and
        quality_is.get("avg_r", -1) > 0.0 and
        quality_is.get("wr", 0) >= 0.35
    )
    q_verdict = "PASS" if q_pass else "FAIL"

    # OOS degradation check
    oos_ok = True
    oos_notes = []
    if quality_oos.get("n", 0) > 0:
        if quality_oos.get("pct_1r", 0) < 0.30:
            oos_ok = False; oos_notes.append("pct_1r below 30%")
        if quality_oos.get("false_bk_rate", 1) > 0.55:
            oos_ok = False; oos_notes.append("false_bk_rate above 55%")
        if quality_oos.get("avg_r", -1) <= 0:
            oos_ok = False; oos_notes.append("avg_r non-positive")
        if quality_oos.get("wr", 0) < 0.35:
            oos_ok = False; oos_notes.append("WR below 35%")

    # Overall
    if d_verdict == "FAIL":
        overall = "FAIL (density)"
    elif q_verdict == "FAIL":
        overall = "FAIL (quality)"
    elif not oos_ok:
        overall = "REVIEW (OOS degradation)"
    else:
        overall = "PASS"

    return {
        "density_n": n,
        "density_verdict": d_verdict,
        "quality_is": quality_is,
        "quality_oos": quality_oos,
        "quality_verdict": q_verdict,
        "oos_ok": oos_ok,
        "oos_notes": oos_notes,
        "overall": overall,
    }


# ════════════════════════════════════════════════════════════
# Main
# ════════════════════════════════════════════════════════════

def main():
    logger.info("=" * 70)
    logger.info("Phase 2c Step 1: FeatureTransitionDetector Validation")
    logger.info("=" * 70)
    t0 = time.time()

    # 1. Load data
    data, runner = load_data()

    # 2. Run detector (default params — with required adjustments baked in)
    logger.info("\n--- Phase A: Signal generation (default params) ---")
    signals = run_detector(data)
    density = density_breakdown(signals)

    logger.info("  Total signals: %d", density["total"])
    for et, n in density["by_type"].items():
        cv = density["type_cv"].get(et, 0)
        logger.info("    %s: %d (CV=%.2f)", et, n, cv)
    for sym, n in density["by_symbol"].items():
        logger.info("    %s: %d", sym, n)
    for yr, n in sorted(density["by_year"].items()):
        logger.info("    %s: %d", yr, n)

    # 3. Quality simulation
    logger.info("\n--- Phase B: Quality simulation ---")
    outcomes = simulate_quality(signals, data)
    logger.info("  Simulated outcomes: %d", len(outcomes))

    # Overall metrics
    overall_m = compute_metrics(outcomes)
    logger.info("  Overall: WR=%.1f%% AvgR=%.3f Pct1R=%.1f%% Pct2R=%.1f%% FalseBK=%.1f%%",
                overall_m["wr"]*100, overall_m["avg_r"],
                overall_m["pct_1r"]*100, overall_m["pct_2r"]*100,
                overall_m["false_bk_rate"]*100)

    # Per-type metrics
    type_metrics = {}
    for et in ["compression_expansion", "range_breakout", "pullback_continuation"]:
        et_outcomes = [o for o in outcomes if o["event_type"] == et]
        type_metrics[et] = compute_metrics(et_outcomes)
        m = type_metrics[et]
        logger.info("  %s (n=%d): WR=%.1f%% AvgR=%.3f Pct1R=%.1f%% FalseBK=%.1f%%",
                     et, m["n"], m["wr"]*100, m["avg_r"], m["pct_1r"]*100, m["false_bk_rate"]*100)

    # 4. IS/OOS split
    logger.info("\n--- Phase C: IS/OOS split ---")
    is_outcomes, oos_outcomes = split_is_oos(outcomes)
    logger.info("  IS outcomes: %d | OOS outcomes: %d", len(is_outcomes), len(oos_outcomes))

    type_is_metrics = {}
    type_oos_metrics = {}
    for et in ["compression_expansion", "range_breakout", "pullback_continuation"]:
        et_is = [o for o in is_outcomes if o["event_type"] == et]
        et_oos = [o for o in oos_outcomes if o["event_type"] == et]
        type_is_metrics[et] = compute_metrics(et_is)
        type_oos_metrics[et] = compute_metrics(et_oos)

        m_is = type_is_metrics[et]
        m_oos = type_oos_metrics[et]
        logger.info("  %s IS  (n=%d): WR=%.1f%% AvgR=%.3f Pct1R=%.1f%% FalseBK=%.1f%%",
                     et, m_is["n"], m_is["wr"]*100, m_is["avg_r"], m_is["pct_1r"]*100, m_is["false_bk_rate"]*100)
        logger.info("  %s OOS (n=%d): WR=%.1f%% AvgR=%.3f Pct1R=%.1f%% FalseBK=%.1f%%",
                     et, m_oos["n"], m_oos["wr"]*100, m_oos["avg_r"], m_oos["pct_1r"]*100, m_oos["false_bk_rate"]*100)

    # 5. Confidence buckets
    logger.info("\n--- Phase D: Confidence buckets ---")
    conf_all = confidence_buckets(outcomes)
    conf_by_type = {}
    for et in ["compression_expansion", "range_breakout", "pullback_continuation"]:
        et_out = [o for o in outcomes if o["event_type"] == et]
        conf_by_type[et] = confidence_buckets(et_out)

    for bucket, m in conf_all.items():
        logger.info("  %s (n=%d): WR=%.1f%% AvgR=%.3f", bucket, m["n"], m["wr"]*100, m["avg_r"])

    # 6. Breakout confirmation ON vs OFF
    logger.info("\n--- Phase E: Breakout confirmation ON vs OFF ---")
    confirm_comparison = {}

    # Already have ON results
    bo_on = [o for o in outcomes if o["event_type"] == "range_breakout"]
    confirm_comparison["confirmation_ON"] = compute_metrics(bo_on)
    logger.info("  Confirmation ON:  n=%d WR=%.1f%% AvgR=%.3f FalseBK=%.1f%%",
                confirm_comparison["confirmation_ON"]["n"],
                confirm_comparison["confirmation_ON"]["wr"]*100,
                confirm_comparison["confirmation_ON"]["avg_r"],
                confirm_comparison["confirmation_ON"]["false_bk_rate"]*100)

    # Run with confirmation OFF
    params_no_confirm = {"range_breakout.require_confirmation_bar": False}
    signals_nc = run_detector(data, params=params_no_confirm)
    bo_nc_signals = [s for s in signals_nc if s["event_type"] == "range_breakout"]
    bo_nc_outcomes = simulate_quality(bo_nc_signals, data)
    confirm_comparison["confirmation_OFF"] = compute_metrics(bo_nc_outcomes)
    logger.info("  Confirmation OFF: n=%d WR=%.1f%% AvgR=%.3f FalseBK=%.1f%%",
                confirm_comparison["confirmation_OFF"]["n"],
                confirm_comparison["confirmation_OFF"]["wr"]*100,
                confirm_comparison["confirmation_OFF"]["avg_r"],
                confirm_comparison["confirmation_OFF"]["false_bk_rate"]*100)

    # 7. Verdicts
    logger.info("\n" + "=" * 70)
    logger.info("VERDICTS")
    logger.info("=" * 70)

    verdicts = {}
    for et in ["compression_expansion", "range_breakout", "pullback_continuation"]:
        v = judge_type(et, density, type_is_metrics[et], type_oos_metrics[et])
        verdicts[et] = v
        logger.info("  %s: %s (density=%d, IS_WR=%.1f%%, IS_AvgR=%.3f)",
                     et, v["overall"], v["density_n"],
                     v["quality_is"]["wr"]*100 if v["quality_is"]["n"] > 0 else 0,
                     v["quality_is"]["avg_r"])

    passing = [et for et, v in verdicts.items() if v["overall"].startswith("PASS")]
    logger.info("\n  PASSING types: %d / 3 → %s", len(passing), passing if passing else "NONE")

    if len(passing) == 0:
        logger.info("  PHASE 2c OUTCOME: TERMINATED (no types pass)")
    else:
        logger.info("  PHASE 2c OUTCOME: PROCEED with %s", passing)

    # 8. Save report
    report = {
        "phase": "Phase 2c Step 1: FeatureTransitionDetector Validation",
        "dataset": {"start": DATE_START, "end": DATE_END, "symbols": SYMBOLS, "is_cutoff": IS_CUTOFF},
        "execution_realism": {"fee": FEE, "slippage": SLIPPAGE, "cost_per_side": COST, "sl_tp": SL_TP},
        "detector_params": {
            "compression_expansion": {k.split(".")[-1]: _DEFAULTS[k] for k in _DEFAULTS if k.startswith("compression")},
            "range_breakout": {k.split(".")[-1]: _DEFAULTS[k] for k in _DEFAULTS if k.startswith("range")},
            "pullback_continuation": {k.split(".")[-1]: _DEFAULTS[k] for k in _DEFAULTS if k.startswith("pullback")},
        },
        "density": density,
        "quality_overall": overall_m,
        "quality_per_type": type_metrics,
        "is_oos": {
            "is_overall": compute_metrics(is_outcomes),
            "oos_overall": compute_metrics(oos_outcomes),
            "is_per_type": type_is_metrics,
            "oos_per_type": type_oos_metrics,
        },
        "confidence_buckets": {"overall": conf_all, "per_type": conf_by_type},
        "breakout_confirmation_comparison": confirm_comparison,
        "verdicts": verdicts,
        "passing_types": passing,
        "elapsed_s": round(time.time() - t0, 1),
        "signal_samples": signals[:30],
    }

    out_dir = ROOT / "reports" / "phase2c"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "step1_validation_results.json"
    with open(out_path, "w") as f:
        json.dump(report, f, indent=2, default=str)

    logger.info("\nReport: %s", out_path)
    logger.info("Elapsed: %.1fs", time.time() - t0)


# Import defaults for report
from core.regime.feature_transition_detector import _DEFAULTS

if __name__ == "__main__":
    main()
