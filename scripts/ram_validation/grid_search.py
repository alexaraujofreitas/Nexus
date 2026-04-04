#!/usr/bin/env python3
"""
RAM Parameter Grid Search — Efficient batched execution.
Tests parameter combinations for RangeAccumulationModel to find configs
that achieve PF ≥ 1.18 with 0.04%/side fees.

Runs each config sequentially, printing results as they complete.
"""
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from research.engine.backtest_runner import BacktestRunner


def _eval_trades(trades):
    """Compute summary stats from trade list."""
    n = len(trades)
    if n == 0:
        return {"n": 0, "wr": 0, "pf": 0, "cagr": 0, "max_dd": 0, "avg_r": 0}
    winners = [t for t in trades if t["pnl"] > 0]
    gross_profit = sum(t["pnl"] for t in winners)
    gross_loss = abs(sum(t["pnl"] for t in trades if t["pnl"] <= 0))
    pf = gross_profit / gross_loss if gross_loss > 0 else 999.0
    wr = len(winners) / n
    avg_r = sum(t.get("r_value", t.get("r_multiple", 0)) for t in trades) / n

    eq = 100_000.0
    peak = eq
    max_dd = 0.0
    for t in sorted(trades, key=lambda x: x.get("exit_ts", 0)):
        eq += t["pnl"]
        peak = max(peak, eq)
        dd = (peak - eq) / peak
        max_dd = max(max_dd, dd)

    final_eq = eq
    years = 4.0
    cagr = ((final_eq / 100_000) ** (1.0 / years) - 1.0) * 100.0 if final_eq > 0 else -999

    # Year breakdown
    def _year(ts):
        if hasattr(ts, 'year'):
            return str(ts.year)
        return str(ts)[:4] if ts else "????"
    yearly = {}
    for year in sorted(set(_year(t.get("entry_ts")) for t in trades)):
        yt = [t for t in trades if _year(t.get("entry_ts")) == year]
        yw = [t for t in yt if t["pnl"] > 0]
        ygp = sum(t["pnl"] for t in yw)
        ygl = abs(sum(t["pnl"] for t in yt if t["pnl"] <= 0))
        ypf = ygp / ygl if ygl > 0 else 999.0
        yearly[year] = {"n": len(yt), "wr": round(len(yw)/len(yt), 4), "pf": round(ypf, 4)}

    # Per-asset breakdown
    assets = {}
    for sym in sorted(set(t.get("symbol", "") for t in trades)):
        st = [t for t in trades if t.get("symbol") == sym]
        sw = [t for t in st if t["pnl"] > 0]
        sgp = sum(t["pnl"] for t in sw)
        sgl = abs(sum(t["pnl"] for t in st if t["pnl"] <= 0))
        spf = sgp / sgl if sgl > 0 else 999.0
        assets[sym] = {"n": len(st), "wr": round(len(sw)/len(st), 4), "pf": round(spf, 4)}

    return {
        "n": n, "wr": round(wr, 4), "pf": round(pf, 4),
        "cagr": round(cagr, 2), "max_dd": round(max_dd, 4),
        "avg_r": round(avg_r, 4), "yearly": yearly, "assets": assets,
    }


def run_config(label, params):
    """Run a single RAM standalone config with fees."""
    print(f"\n{'='*60}")
    print(f"  Config: {label}")
    print(f"  Params: {params}")
    print(f"{'='*60}")
    t0 = time.time()
    runner = BacktestRunner(
        symbols=["BTC/USDT", "ETH/USDT", "SOL/USDT"],
        mode="custom",
        strategy_subset=["range_accumulation"],
    )
    result = runner.run(params=params or {}, cost_per_side=0.0004, n_workers=1)
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
    return {"label": label, "params": params, "stats": stats, "elapsed": round(elapsed, 1)}


# ── Grid configs — focused on relaxing the most binding constraints ──
# Baseline had 44 trades with PF=1.0095. Need to either:
# (a) get more trades by relaxing entry filters, or
# (b) improve win rate by tightening quality filters
BATCH_1 = [
    # Relax proximity: wider entry zone near boundary
    ("prox0.8", {"phase_2.range_accumulation.entry_proximity_atr": 0.8}),
    ("prox1.0", {"phase_2.range_accumulation.entry_proximity_atr": 1.0}),
    # Relax RSI: less extreme required
    ("rsi40_60", {"phase_2.range_accumulation.rsi_oversold": 40.0,
                  "phase_2.range_accumulation.rsi_overbought": 60.0}),
]

BATCH_2 = [
    # Relax wick strength: easier rejection candle
    ("wick0.5", {"phase_2.range_accumulation.wick_strength": 0.5}),
    ("wick0.7", {"phase_2.range_accumulation.wick_strength": 0.7}),
    # Fewer touches required
    ("touches2", {"phase_2.range_accumulation.min_touches": 2}),
]

BATCH_3 = [
    # Wider drift tolerance
    ("drift0.25", {"phase_2.range_accumulation.max_drift_pct": 0.25}),
    # Wider SL
    ("sl3.0", {"phase_2.range_accumulation.sl_atr_mult": 3.0}),
    # Combined relaxation: most promising
    ("combo_relax1", {
        "phase_2.range_accumulation.entry_proximity_atr": 0.8,
        "phase_2.range_accumulation.rsi_oversold": 40.0,
        "phase_2.range_accumulation.rsi_overbought": 60.0,
        "phase_2.range_accumulation.wick_strength": 0.7,
    }),
]

BATCH_4 = [
    # Aggressive relaxation — max trade count
    ("combo_relax2", {
        "phase_2.range_accumulation.entry_proximity_atr": 1.0,
        "phase_2.range_accumulation.rsi_oversold": 42.0,
        "phase_2.range_accumulation.rsi_overbought": 58.0,
        "phase_2.range_accumulation.wick_strength": 0.5,
        "phase_2.range_accumulation.min_touches": 2,
    }),
    # Tighter quality: fewer but better trades
    ("combo_tight", {
        "phase_2.range_accumulation.rsi_oversold": 30.0,
        "phase_2.range_accumulation.rsi_overbought": 70.0,
        "phase_2.range_accumulation.adx_max": 20.0,
        "phase_2.range_accumulation.sl_atr_mult": 3.0,
    }),
    # Lookback 20 (shorter ranges)
    ("lb20_prox0.8", {
        "phase_2.range_accumulation.lookback": 20,
        "phase_2.range_accumulation.entry_proximity_atr": 0.8,
    }),
]


def main():
    batch_num = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    batches = {1: BATCH_1, 2: BATCH_2, 3: BATCH_3, 4: BATCH_4}

    if batch_num == 0:
        # Run all batches
        all_results = []
        for bn, configs in batches.items():
            print(f"\n{'#'*60}")
            print(f"  BATCH {bn}")
            print(f"{'#'*60}")
            for label, params in configs:
                r = run_config(label, params)
                all_results.append(r)
    else:
        configs = batches.get(batch_num, [])
        all_results = []
        for label, params in configs:
            r = run_config(label, params)
            all_results.append(r)

    # Save results
    out_dir = ROOT / "reports" / "ram_validation"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"grid_batch{batch_num}_results.json"
    with open(out_path, "w") as f:
        json.dump(all_results, f, indent=2, default=str)
    print(f"\nResults saved to: {out_path}")

    # Summary table
    print(f"\n{'='*80}")
    print(f"  GRID SUMMARY (batch {batch_num})")
    print(f"{'='*80}")
    print(f"{'Label':<25} {'n':>4} {'WR':>7} {'PF':>8} {'CAGR':>8} {'MaxDD':>8}")
    print("-" * 80)
    for r in sorted(all_results, key=lambda x: x["stats"]["pf"], reverse=True):
        s = r["stats"]
        print(f"{r['label']:<25} {s['n']:>4} {s['wr']:>7.1%} {s['pf']:>8.4f} "
              f"{s['cagr']:>7.2f}% {s['max_dd']:>7.2%}")
    # Mark pass/fail vs acceptance criteria
    print(f"\nAcceptance: PF ≥ 1.18, MaxDD ≤ 25%, n ≥ 30")
    for r in all_results:
        s = r["stats"]
        passed = s["pf"] >= 1.18 and s["max_dd"] <= 0.25 and s["n"] >= 30
        mark = "✅ PASS" if passed else "❌ FAIL"
        print(f"  {r['label']}: {mark} (PF={s['pf']:.4f}, MaxDD={s['max_dd']:.2%}, n={s['n']})")


if __name__ == "__main__":
    main()
