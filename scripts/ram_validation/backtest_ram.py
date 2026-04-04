#!/usr/bin/env python3
"""
Phase 2 — RangeAccumulationModel Backtest Validation (Session 51)

Runs 4 scenarios:
  A. RAM standalone (zero fee) — trade-level stats
  B. RAM standalone (0.04%/side) — production-realistic
  C. PBL+SLC baseline (0.04%/side) — for comparison
  D. PBL+SLC+RAM combined (0.04%/side) — does RAM add value?

Acceptance criteria:
  - RAM standalone PF ≥ 1.18 (with fees)
  - MaxDD ≤ 25%
  - Trade count ≥ 30
  - No single-period dependency (year-by-year stable)
"""
import json
import sys
import time
from pathlib import Path

# Ensure project root on path
ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from research.engine.backtest_runner import BacktestRunner


def _run_scenario(label, mode, subset, cost, params=None):
    print(f"\n{'='*60}")
    print(f"  {label}")
    print(f"{'='*60}")
    t0 = time.time()
    runner = BacktestRunner(
        symbols=["BTC/USDT", "ETH/USDT", "SOL/USDT"],
        mode=mode,
        strategy_subset=subset,
    )
    result = runner.run(params=params or {}, cost_per_side=cost, n_workers=1)
    elapsed = time.time() - t0
    print(f"  Elapsed: {elapsed:.1f}s")
    _print_summary(label, result)
    return result


def _print_summary(label, r):
    trades = r.get("all_trades", r.get("trades", []))
    n = len(trades)
    if n == 0:
        print(f"  {label}: 0 trades — no signals fired")
        return
    winners = [t for t in trades if t["pnl"] > 0]
    gross_profit = sum(t["pnl"] for t in winners)
    gross_loss = abs(sum(t["pnl"] for t in trades if t["pnl"] <= 0))
    pf = gross_profit / gross_loss if gross_loss > 0 else 999.0
    wr = len(winners) / n if n > 0 else 0
    avg_r = sum(t.get("r_multiple", 0) for t in trades) / n if n > 0 else 0

    # Drawdown
    equity_curve = []
    eq = 100_000.0
    peak = eq
    max_dd = 0.0
    for t in sorted(trades, key=lambda x: x.get("exit_ts", 0)):
        eq += t["pnl"]
        equity_curve.append(eq)
        peak = max(peak, eq)
        dd = (peak - eq) / peak
        max_dd = max(max_dd, dd)

    # CAGR
    if equity_curve:
        final_eq = equity_curve[-1]
        years = 4.0  # approximate
        cagr = ((final_eq / 100_000) ** (1.0 / years) - 1.0) * 100.0 if final_eq > 0 else -999
    else:
        cagr = 0.0

    print(f"\n  {label}:")
    print(f"    Trades:  {n}")
    print(f"    WR:      {wr:.1%}")
    print(f"    PF:      {pf:.4f}")
    print(f"    CAGR:    {cagr:.2f}%")
    print(f"    MaxDD:   {max_dd:.2%}")
    print(f"    Avg R:   {avg_r:.4f}")

    # Per-model breakdown
    models_seen = set(t.get("model", "unknown") for t in trades)
    for m in sorted(models_seen):
        mt = [t for t in trades if t.get("model") == m]
        mw = [t for t in mt if t["pnl"] > 0]
        mgp = sum(t["pnl"] for t in mw)
        mgl = abs(sum(t["pnl"] for t in mt if t["pnl"] <= 0))
        mpf = mgp / mgl if mgl > 0 else 999.0
        mwr = len(mw) / len(mt) if mt else 0
        print(f"    {m}: n={len(mt)} WR={mwr:.1%} PF={mpf:.4f}")

    # Year-by-year breakdown
    print(f"\n    Year-by-Year:")
    def _year(ts):
        if hasattr(ts, 'year'):
            return str(ts.year)
        return str(ts)[:4] if ts else "????"
    for year in sorted(set(_year(t.get("entry_ts")) for t in trades)):
        yt = [t for t in trades if _year(t.get("entry_ts")) == year]
        yw = [t for t in yt if t["pnl"] > 0]
        ygp = sum(t["pnl"] for t in yw)
        ygl = abs(sum(t["pnl"] for t in yt if t["pnl"] <= 0))
        ypf = ygp / ygl if ygl > 0 else 999.0
        ywr = len(yw) / len(yt) if yt else 0
        print(f"      {year}: n={len(yt)} WR={ywr:.1%} PF={ypf:.4f}")

    # Per-asset breakdown
    print(f"\n    Per-Asset:")
    for sym in sorted(set(t.get("symbol", "") for t in trades)):
        st = [t for t in trades if t.get("symbol") == sym]
        sw = [t for t in st if t["pnl"] > 0]
        sgp = sum(t["pnl"] for t in sw)
        sgl = abs(sum(t["pnl"] for t in st if t["pnl"] <= 0))
        spf = sgp / sgl if sgl > 0 else 999.0
        swr = len(sw) / len(st) if st else 0
        print(f"      {sym}: n={len(st)} WR={swr:.1%} PF={spf:.4f}")

    return {
        "label": label, "n": n, "wr": round(wr, 4), "pf": round(pf, 4),
        "cagr": round(cagr, 2), "max_dd": round(max_dd, 4), "avg_r": round(avg_r, 4),
    }


def main():
    results = {}

    # Scenario A: RAM standalone, zero fees
    rA = _run_scenario(
        "A: RAM standalone (zero fee)", "custom", ["range_accumulation"], 0.0
    )
    results["A_ram_zero_fee"] = rA

    # Scenario B: RAM standalone, 0.04%/side fees
    rB = _run_scenario(
        "B: RAM standalone (0.04%/side)", "custom", ["range_accumulation"], 0.0004
    )
    results["B_ram_with_fee"] = rB

    # Scenario C: PBL+SLC baseline, 0.04%/side fees
    rC = _run_scenario(
        "C: PBL+SLC baseline (0.04%/side)", "pbl_slc", None, 0.0004
    )
    results["C_baseline"] = rC

    # Scenario D: PBL+SLC+RAM combined, 0.04%/side fees
    rD = _run_scenario(
        "D: PBL+SLC+RAM combined (0.04%/side)", "custom",
        ["pullback_long", "swing_low_continuation", "range_accumulation"],
        0.0004
    )
    results["D_combined"] = rD

    # Save results
    out_path = ROOT / "reports" / "ram_validation" / "backtest_results.json"
    with open(out_path, "w") as f:
        json.dump(results, f, indent=2, default=str)
    print(f"\nResults saved to: {out_path}")

    # Summary
    print("\n" + "=" * 60)
    print("  FINAL SUMMARY")
    print("=" * 60)
    for key, r in results.items():
        trades = r.get("all_trades", r.get("trades", []))
        n = len(trades)
        if n > 0:
            winners = [t for t in trades if t["pnl"] > 0]
            gross_profit = sum(t["pnl"] for t in winners)
            gross_loss = abs(sum(t["pnl"] for t in trades if t["pnl"] <= 0))
            pf = gross_profit / gross_loss if gross_loss > 0 else 999.0
            wr = len(winners) / n
            print(f"  {key}: n={n} WR={wr:.1%} PF={pf:.4f}")
        else:
            print(f"  {key}: 0 trades")


if __name__ == "__main__":
    main()
