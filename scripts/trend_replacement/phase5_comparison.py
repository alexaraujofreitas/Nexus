"""
Phase 5 — Trend Replacement Study: Backtest Comparison
=======================================================
Compares three strategy configurations on the same 4-year dataset
(BTC+SOL+ETH, 30m, 2022-03-22 → 2026-03-21):

  A. PBL+SLC only        (mode="pbl_slc"   — reference baseline)
  B. PBL+SLC+MB          (mode="custom",   strategy_subset=[pbl, slc, mb])
  C. PBL+SLC+Donchian    (mode="custom",   strategy_subset=[pbl, slc, db])

Metrics reported for zero-fee and 0.04%/side fee scenarios.

Notes
-----
- disabled_models is overridden per-scenario via params so that models under
  research are not blocked by the production gate in config.yaml.
- cagr and max_drawdown are returned as fractions (0.48 = 48%) — multiply ×100
  for display.
- win_rate is also a fraction (0.564 = 56.4%).
"""
from __future__ import annotations

import json
import logging
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
# Surface INFO from backtest runner only
logging.getLogger("research.engine.backtest_runner").setLevel(logging.INFO)

from research.engine.backtest_runner import BacktestRunner

DATE_START = "2022-03-22"
DATE_END   = "2026-03-21"

# Base disabled_models (excludes production gates that are always off)
BASE_DISABLED = ["mean_reversion", "liquidity_sweep", "trend"]

SCENARIOS = [
    {
        "label":    "A: PBL+SLC only (baseline)",
        "mode":     "pbl_slc",
        "subset":   None,
        # disabled_models override: trend already disabled; other archived models excluded
        "params_extra": {"disabled_models": BASE_DISABLED},
    },
    {
        "label":    "B: PBL+SLC+MomentumBreakout",
        "mode":     "custom",
        "subset":   ["pullback_long", "swing_low_continuation", "momentum_breakout"],
        "params_extra": {"disabled_models": BASE_DISABLED},
    },
    {
        "label":    "C: PBL+SLC+DonchianBreakout",
        "mode":     "custom",
        "subset":   ["pullback_long", "swing_low_continuation", "donchian_breakout"],
        # Remove donchian_breakout from disabled_models so the research gate is lifted
        "params_extra": {"disabled_models": BASE_DISABLED},  # donchian_breakout intentionally NOT in this list
    },
]

COST_PAIRS = [
    ("zero_fee",      0.0),
    ("0.04pct_side",  0.0004),
]


def main():
    print("\n" + "=" * 70)
    print("PHASE 5 — TREND REPLACEMENT COMPARISON")
    print(f"Dataset: BTC+SOL+ETH  30m  {DATE_START} → {DATE_END}")
    print("=" * 70)

    results_all = {}

    for sc in SCENARIOS:
        label  = sc["label"]
        mode   = sc["mode"]
        subset = sc["subset"]
        params_extra = sc.get("params_extra", {})

        print(f"\n{'─'*70}")
        print(f"Scenario {label}")
        if subset:
            print(f"  Models: {subset}")

        runner = BacktestRunner(
            date_start=DATE_START,
            date_end=DATE_END,
            mode=mode,
            strategy_subset=subset,
        )

        print("  Loading data (cache warm expected)…")
        t0 = time.time()
        runner.load_data()
        print(f"  load_data: {time.time()-t0:.1f}s")

        sc_results = {}

        for cost_label, cost in COST_PAIRS:
            print(f"\n  [{cost_label}]  running…")
            t1 = time.time()
            res = runner.run(params=params_extra, cost_per_side=cost, n_workers=1)
            elapsed = time.time() - t1

            # Metrics — cagr/max_drawdown are fractions; multiply ×100 for %
            pf   = res.get("profit_factor", 0.0)
            cagr = res.get("cagr",          0.0) * 100
            wr   = res.get("win_rate",       0.0) * 100
            mdd  = res.get("max_drawdown",   0.0) * 100
            n    = res.get("n_trades",       0)
            final_eq = res.get("final_equity", 100_000)

            sc_results[cost_label] = {
                "profit_factor":    round(pf,   4),
                "cagr_pct":         round(cagr, 2),
                "win_rate_pct":     round(wr,   2),
                "max_drawdown_pct": round(abs(mdd), 2),
                "n_trades":         n,
                "final_equity":     round(final_eq, 2),
            }

            # Per-model breakdown if available
            breakdown = res.get("model_breakdown", {})
            if breakdown:
                sc_results[cost_label]["model_breakdown"] = breakdown

            print(
                f"  PF={pf:.4f}  CAGR={cagr:.2f}%  WR={wr:.1f}%  "
                f"MaxDD={abs(mdd):.2f}%  n={n}  ({elapsed:.1f}s)"
            )

        results_all[label] = sc_results

    # ── Summary table ─────────────────────────────────────────────────────────
    print("\n" + "=" * 70)
    print("SUMMARY (0.04%/side fees)")
    print("=" * 70)
    print(f"{'Scenario':<35} {'PF':>7} {'CAGR':>8} {'WR':>7} {'MaxDD':>8} {'n':>6}")
    print("-" * 70)
    for label, sc_res in results_all.items():
        r = sc_res.get("0.04pct_side", {})
        print(
            f"{label:<35} "
            f"{r.get('profit_factor',0):.4f}  "
            f"{r.get('cagr_pct',0):>6.2f}%  "
            f"{r.get('win_rate_pct',0):>5.1f}%  "
            f"{r.get('max_drawdown_pct',0):>6.2f}%  "
            f"{r.get('n_trades',0):>5}"
        )

    print("\nSUMMARY (zero fees)")
    print("-" * 70)
    for label, sc_res in results_all.items():
        r = sc_res.get("zero_fee", {})
        print(
            f"{label:<35} "
            f"{r.get('profit_factor',0):.4f}  "
            f"{r.get('cagr_pct',0):>6.2f}%  "
            f"{r.get('win_rate_pct',0):>5.1f}%  "
            f"{r.get('max_drawdown_pct',0):>6.2f}%  "
            f"{r.get('n_trades',0):>5}"
        )

    # Per-model breakdown
    print("\nPER-MODEL BREAKDOWN (0.04%/side fees, where available)")
    print("-" * 70)
    for label, sc_res in results_all.items():
        r = sc_res.get("0.04pct_side", {})
        bd = r.get("model_breakdown")
        if bd:
            print(f"\n{label}")
            for mname, stats in bd.items():
                mn = stats.get("n", 0)
                mwr = stats.get("win_rate", 0) * 100
                mpf = stats.get("profit_factor", 0)
                mar = stats.get("avg_r", 0)
                print(f"  {mname:<30} n={mn:>4}  WR={mwr:>5.1f}%  PF={mpf:.4f}  AvgR={mar:+.3f}")

    # Save JSON
    out_path = ROOT / "reports" / "phase5_trend_replacement_comparison.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w") as f:
        json.dump({"scenarios": results_all, "date_start": DATE_START, "date_end": DATE_END}, f, indent=2)
    print(f"\nFull results saved → {out_path}")


if __name__ == "__main__":
    main()
