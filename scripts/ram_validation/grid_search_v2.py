#!/usr/bin/env python3
"""
RAM Parameter Grid Search v2 — Clean settings reset between runs.
Each config starts from a clean DEFAULT_CONFIG state.
"""
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

# Default RAM parameters to reset between runs
RAM_DEFAULTS = {
    "phase_2.range_accumulation.enabled": True,
    "phase_2.range_accumulation.lookback": 30,
    "phase_2.range_accumulation.touch_tolerance_atr": 0.3,
    "phase_2.range_accumulation.min_touches": 3,
    "phase_2.range_accumulation.max_drift_pct": 0.15,
    "phase_2.range_accumulation.rsi_oversold": 35.0,
    "phase_2.range_accumulation.rsi_overbought": 65.0,
    "phase_2.range_accumulation.adx_max": 25.0,
    "phase_2.range_accumulation.sl_atr_mult": 2.5,
    "phase_2.range_accumulation.volume_contraction_mult": 2.0,
    "phase_2.range_accumulation.atr_spike_mult": 1.5,
    "phase_2.range_accumulation.wick_strength": 1.0,
    "phase_2.range_accumulation.max_hold_bars": 40,
    "phase_2.range_accumulation.strength_base": 0.30,
    "phase_2.range_accumulation.entry_proximity_atr": 0.5,
}


def _eval_trades(trades):
    n = len(trades)
    if n == 0:
        return {"n": 0, "wr": 0, "pf": 0, "cagr": 0, "max_dd": 0, "avg_r": 0, "yearly": {}, "assets": {}}
    winners = [t for t in trades if t["pnl"] > 0]
    gross_profit = sum(t["pnl"] for t in winners)
    gross_loss = abs(sum(t["pnl"] for t in trades if t["pnl"] <= 0))
    pf = gross_profit / gross_loss if gross_loss > 0 else 999.0
    wr = len(winners) / n
    avg_r = sum(t.get("r_value", t.get("r_multiple", 0)) for t in trades) / n

    eq = 100_000.0; peak = eq; max_dd = 0.0
    for t in sorted(trades, key=lambda x: x.get("exit_ts", 0)):
        eq += t["pnl"]; peak = max(peak, eq)
        dd = (peak - eq) / peak; max_dd = max(max_dd, dd)
    cagr = ((eq / 100_000) ** (1.0 / 4.0) - 1.0) * 100.0 if eq > 0 else -999

    def _year(ts):
        return str(ts.year) if hasattr(ts, 'year') else str(ts)[:4] if ts else "????"
    yearly = {}
    for year in sorted(set(_year(t.get("entry_ts")) for t in trades)):
        yt = [t for t in trades if _year(t.get("entry_ts")) == year]
        yw = [t for t in yt if t["pnl"] > 0]
        ygp = sum(t["pnl"] for t in yw); ygl = abs(sum(t["pnl"] for t in yt if t["pnl"] <= 0))
        ypf = ygp / ygl if ygl > 0 else 999.0
        yearly[year] = {"n": len(yt), "wr": round(len(yw)/len(yt), 4), "pf": round(ypf, 4)}
    assets = {}
    for sym in sorted(set(t.get("symbol", "") for t in trades)):
        st = [t for t in trades if t.get("symbol") == sym]
        sw = [t for t in st if t["pnl"] > 0]
        sgp = sum(t["pnl"] for t in sw); sgl = abs(sum(t["pnl"] for t in st if t["pnl"] <= 0))
        spf = sgp / sgl if sgl > 0 else 999.0
        assets[sym] = {"n": len(st), "wr": round(len(sw)/len(st), 4), "pf": round(spf, 4)}

    return {"n": n, "wr": round(wr, 4), "pf": round(pf, 4), "cagr": round(cagr, 2),
            "max_dd": round(max_dd, 4), "avg_r": round(avg_r, 4), "yearly": yearly, "assets": assets}


def _reset_settings():
    """Reset ALL RAM params to defaults before each run."""
    from config.settings import settings as _s
    for k, v in RAM_DEFAULTS.items():
        _s.set(k, v)


def run_config(label, overrides, runner_cache=None):
    """Run a single RAM standalone config with fees. Reuses loaded data if provided."""
    from research.engine.backtest_runner import BacktestRunner
    _reset_settings()  # Clean slate

    print(f"\n{'='*60}")
    print(f"  Config: {label}")
    print(f"  Overrides: {overrides}")
    print(f"{'='*60}")

    t0 = time.time()
    if runner_cache is None:
        runner = BacktestRunner(
            symbols=["BTC/USDT", "ETH/USDT", "SOL/USDT"],
            mode="custom",
            strategy_subset=["range_accumulation"],
        )
        runner.load_data()
    else:
        runner = runner_cache

    # Apply the specific overrides for this config
    result = runner.run(params=overrides or {}, cost_per_side=0.0004, n_workers=1)
    elapsed = time.time() - t0

    trades = result.get("all_trades", result.get("trades", []))
    stats = _eval_trades(trades)
    print(f"  Elapsed: {elapsed:.1f}s")
    print(f"  n={stats['n']} WR={stats['wr']:.1%} PF={stats['pf']:.4f} "
          f"CAGR={stats['cagr']:.2f}% MaxDD={stats['max_dd']:.2%}")
    if stats.get("yearly"):
        for yr, ys in stats["yearly"].items():
            print(f"    {yr}: n={ys['n']} WR={ys['wr']:.1%} PF={ys['pf']:.4f}")
    if stats.get("assets"):
        for sym, sa in stats["assets"].items():
            print(f"    {sym}: n={sa['n']} WR={sa['wr']:.1%} PF={sa['pf']:.4f}")
    return {"label": label, "overrides": {k: v for k, v in overrides.items()} if overrides else {},
            "stats": stats, "elapsed": round(elapsed, 1)}


CONFIGS = [
    # Baseline (defaults)
    ("default", {}),

    # Single-param variations
    ("prox0.8", {"phase_2.range_accumulation.entry_proximity_atr": 0.8}),
    ("prox1.0", {"phase_2.range_accumulation.entry_proximity_atr": 1.0}),
    ("rsi40_60", {"phase_2.range_accumulation.rsi_oversold": 40.0, "phase_2.range_accumulation.rsi_overbought": 60.0}),
    ("wick0.5", {"phase_2.range_accumulation.wick_strength": 0.5}),
    ("wick0.7", {"phase_2.range_accumulation.wick_strength": 0.7}),
    ("touches2", {"phase_2.range_accumulation.min_touches": 2}),
    ("adx20", {"phase_2.range_accumulation.adx_max": 20.0}),
    ("sl3.0", {"phase_2.range_accumulation.sl_atr_mult": 3.0}),
    ("drift0.25", {"phase_2.range_accumulation.max_drift_pct": 0.25}),
    ("lb20", {"phase_2.range_accumulation.lookback": 20}),

    # Combined: moderate relaxation
    ("combo_moderate", {
        "phase_2.range_accumulation.entry_proximity_atr": 0.8,
        "phase_2.range_accumulation.rsi_oversold": 38.0,
        "phase_2.range_accumulation.rsi_overbought": 62.0,
        "phase_2.range_accumulation.wick_strength": 0.7,
    }),

    # Combined: aggressive relaxation
    ("combo_aggressive", {
        "phase_2.range_accumulation.entry_proximity_atr": 1.0,
        "phase_2.range_accumulation.rsi_oversold": 42.0,
        "phase_2.range_accumulation.rsi_overbought": 58.0,
        "phase_2.range_accumulation.wick_strength": 0.5,
        "phase_2.range_accumulation.min_touches": 2,
    }),

    # Combined: quality tightening
    ("combo_tight", {
        "phase_2.range_accumulation.rsi_oversold": 30.0,
        "phase_2.range_accumulation.rsi_overbought": 70.0,
        "phase_2.range_accumulation.adx_max": 20.0,
        "phase_2.range_accumulation.sl_atr_mult": 3.0,
    }),

    # Wider SL + wider proximity
    ("sl3_prox0.8", {
        "phase_2.range_accumulation.sl_atr_mult": 3.0,
        "phase_2.range_accumulation.entry_proximity_atr": 0.8,
    }),

    # Shorter lookback + relaxed entry
    ("lb20_relax", {
        "phase_2.range_accumulation.lookback": 20,
        "phase_2.range_accumulation.entry_proximity_atr": 0.8,
        "phase_2.range_accumulation.wick_strength": 0.7,
    }),
]


def main():
    from research.engine.backtest_runner import BacktestRunner

    batch_num = int(sys.argv[1]) if len(sys.argv) > 1 else 0

    # Load data once, reuse for all configs
    print("Loading data (shared across all configs)...")
    _reset_settings()
    runner = BacktestRunner(
        symbols=["BTC/USDT", "ETH/USDT", "SOL/USDT"],
        mode="custom",
        strategy_subset=["range_accumulation"],
    )
    runner.load_data()
    print(f"Data loaded. Master timeline: {len(runner._master_ts)} bars\n")

    if batch_num == 0:
        configs = CONFIGS
    else:
        # Split into batches of 4
        batch_size = 4
        start = (batch_num - 1) * batch_size
        configs = CONFIGS[start:start + batch_size]

    all_results = []
    for label, overrides in configs:
        r = run_config(label, overrides, runner_cache=runner)
        all_results.append(r)

    # Save
    out_dir = ROOT / "reports" / "ram_validation"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"grid_v2_batch{batch_num}_results.json"
    with open(out_path, "w") as f:
        json.dump(all_results, f, indent=2, default=str)
    print(f"\nResults saved to: {out_path}")

    # Summary
    print(f"\n{'='*85}")
    print(f"  GRID SUMMARY")
    print(f"{'='*85}")
    print(f"{'Label':<25} {'n':>5} {'WR':>7} {'PF':>8} {'CAGR':>8} {'MaxDD':>8}")
    print("-" * 85)
    for r in sorted(all_results, key=lambda x: x["stats"]["pf"], reverse=True):
        s = r["stats"]
        print(f"{r['label']:<25} {s['n']:>5} {s['wr']:>7.1%} {s['pf']:>8.4f} "
              f"{s['cagr']:>7.2f}% {s['max_dd']:>7.2%}")
    print(f"\nAcceptance: PF ≥ 1.18, MaxDD ≤ 25%, n ≥ 30")
    for r in all_results:
        s = r["stats"]
        passed = s["pf"] >= 1.18 and s["max_dd"] <= 0.25 and s["n"] >= 30
        mark = "✅ PASS" if passed else "❌ FAIL"
        reasons = []
        if s["pf"] < 1.18: reasons.append(f"PF={s['pf']:.4f}<1.18")
        if s["max_dd"] > 0.25: reasons.append(f"MaxDD={s['max_dd']:.2%}>25%")
        if s["n"] < 30: reasons.append(f"n={s['n']}<30")
        print(f"  {r['label']}: {mark} — {', '.join(reasons) if reasons else 'ALL CRITERIA MET'}")


if __name__ == "__main__":
    main()
