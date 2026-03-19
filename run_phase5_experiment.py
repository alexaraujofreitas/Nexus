#!/usr/bin/env python
"""
Phase 5 — Baseline vs Staged Backtester A/B Comparison
=======================================================
Runs both modes on same synthetic data, prints KPI comparison.
"""
import json
import sys
import time

sys.path.insert(0, ".")

from core.validation.staged_backtester import run_ab_comparison


def main():
    print("=" * 70)
    print("  PHASE 5 — BASELINE vs STAGED BACKTEST COMPARISON")
    print("=" * 70)
    print()

    t0 = time.time()

    result = run_ab_comparison(
        symbols=["BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT", "XRP/USDT"],
        timeframe="1h",
        initial_capital=10_000.0,
        seed=42,
        progress_cb=lambda m: print(f"  {m}"),
    )

    elapsed = time.time() - t0

    # ── Print results ──────────────────────────────────────────────
    bm = result["baseline"]["aggregate_metrics"]
    sm = result["staged"]["aggregate_metrics"]
    comp = result["comparison"]

    print()
    print("=" * 70)
    print("  KPI COMPARISON")
    print("=" * 70)
    print()
    fmt = "  {:<30s}  {:>12s}  {:>12s}  {:>12s}"
    print(fmt.format("Metric", "Baseline", "Staged", "Change"))
    print("  " + "-" * 68)
    print(fmt.format("Total Trades",
                      str(bm.get("total_trades", 0)),
                      str(sm.get("total_trades", 0)), ""))
    print(fmt.format("Win Rate (%)",
                      f"{bm.get('win_rate', 0):.1f}",
                      f"{sm.get('win_rate', 0):.1f}",
                      f"{sm.get('win_rate', 0) - bm.get('win_rate', 0):+.1f}pp"))
    print(fmt.format("Profit Factor",
                      f"{comp['profit_factor_baseline']:.2f}",
                      f"{comp['profit_factor_staged']:.2f}",
                      f"{comp['profit_factor_change_pct']:+.1f}%"))
    print(fmt.format("Max Drawdown (%)",
                      f"{comp['max_drawdown_baseline']:.2f}",
                      f"{comp['max_drawdown_staged']:.2f}",
                      f"{comp['max_drawdown_change_pct']:+.1f}%"))
    print(fmt.format("Trade Expectancy (R)",
                      f"{comp['expectancy_baseline']:.4f}",
                      f"{comp['expectancy_staged']:.4f}",
                      f"{comp['expectancy_change_pct']:+.1f}%"))
    print(fmt.format("Total Return (%)",
                      f"{comp['total_return_baseline']:.2f}",
                      f"{comp['total_return_staged']:.2f}", ""))
    print(fmt.format("Sharpe Ratio",
                      f"{bm.get('sharpe_ratio', 0):.2f}",
                      f"{sm.get('sharpe_ratio', 0):.2f}", ""))

    # ── Candidate lifecycle (staged only) ──────────────────────────
    lc = result["staged"]["candidate_lifecycle"]
    if lc:
        print()
        print("=" * 70)
        print("  CANDIDATE LIFECYCLE METRICS (Staged)")
        print("=" * 70)
        print()
        print(f"  Total Created:       {lc['total_created']}")
        print(f"  Total Confirmed:     {lc['total_confirmed']}")
        print(f"  Total Executed:      {lc['total_executed']}")
        print(f"  Total Voided:        {lc['total_voided']}")
        print(f"  Total Expired:       {lc['total_expired']}")
        print(f"  Conversion Rate:     {lc['conversion_rate']:.1%}")
        print(f"  Expiry Rate:         {lc['expiry_rate']:.1%}")
        print(f"  Void Rate:           {lc['void_rate']:.1%}")
        print(f"  Avg Confirm Delay:   {lc['avg_confirmation_delay_15m']:.1f} × 15m")
        print(f"  Avg Candidate Age:   {lc['avg_candidate_age_15m']:.1f} × 15m")

        cd = lc.get("confirmation_delay_distribution", {})
        print(f"  Confirm Delay Min:   {cd.get('min', 'N/A')} × 15m")
        print(f"  Confirm Delay Max:   {cd.get('max', 'N/A')} × 15m")
        print(f"  Confirm Delay Med:   {cd.get('median', 'N/A')} × 15m")

        ec = lc.get("execution_clustering", {})
        print(f"  Burst Cycles (>1):   {ec.get('burst_cycles', 0)}")
        print(f"  Max Exec/Cycle:      {ec.get('max_executions_per_cycle', 0)}")

    # ── Per-symbol results ─────────────────────────────────────────
    print()
    print("=" * 70)
    print("  PER-SYMBOL COMPARISON")
    print("=" * 70)
    print()
    sfmt = "  {:<12s}  {:>6s} / {:>6s}  {:>8s} / {:>8s}  {:>8s} / {:>8s}"
    print(sfmt.format("Symbol", "B.Trd", "S.Trd", "B.WR%", "S.WR%", "B.PF", "S.PF"))
    print("  " + "-" * 68)
    for sym in result["symbols"]:
        bp = result["baseline"]["per_symbol"].get(sym, {})
        sp = result["staged"]["per_symbol"].get(sym, {})
        print(sfmt.format(
            sym,
            str(bp.get("total_trades", 0)), str(sp.get("total_trades", 0)),
            f"{bp.get('win_rate', 0):.1f}", f"{sp.get('win_rate', 0):.1f}",
            f"{bp.get('profit_factor', 0):.2f}", f"{sp.get('profit_factor', 0):.2f}",
        ))

    # ── Per-symbol lifecycle ───────────────────────────────────────
    psl = result["staged"].get("per_symbol_lifecycle", {})
    if psl:
        print()
        print("  Per-Symbol Candidate Lifecycle:")
        for sym, slc in psl.items():
            if slc:
                print(f"    {sym}: created={slc['total_created']} conf={slc['total_confirmed']} "
                      f"exec={slc['total_executed']} void={slc['total_voided']} "
                      f"exp={slc['total_expired']} conv={slc['conversion_rate']:.1%}")

    # ── Validation ─────────────────────────────────────────────────
    val = result["validation"]
    print()
    print("=" * 70)
    print("  STRUCTURAL VALIDATION")
    print("=" * 70)
    print()
    print(f"  All Passed:                           {'YES' if val['all_passed'] else 'NO !!!'}")
    print(f"  Violations:                           {len(val['violations'])}")
    if val["violations"]:
        for v in val["violations"][:10]:
            print(f"    - {v}")

    # ── Success criteria evaluation ────────────────────────────────
    print()
    print("=" * 70)
    print("  SUCCESS CRITERIA EVALUATION")
    print("=" * 70)
    print()

    pf_improved = comp["profit_factor_change_pct"] >= 10.0
    dd_ok = comp["max_drawdown_staged"] <= comp["max_drawdown_baseline"] * 1.01  # 1% tolerance
    exp_improved = comp["expectancy_change_pct"] >= 10.0
    no_overtrade = (sm.get("total_trades", 0) <= bm.get("total_trades", 0) * 1.5)

    print(f"  Profit Factor ≥ 10% improvement:      {'PASS' if pf_improved else 'FAIL'} ({comp['profit_factor_change_pct']:+.1f}%)")
    print(f"  Max Drawdown not increased:           {'PASS' if dd_ok else 'FAIL'} ({comp['max_drawdown_baseline']:.2f}% → {comp['max_drawdown_staged']:.2f}%)")
    print(f"  Expectancy ≥ 10% improvement:         {'PASS' if exp_improved else 'FAIL'} ({comp['expectancy_change_pct']:+.1f}%)")
    print(f"  No overtrading:                       {'PASS' if no_overtrade else 'FAIL'} ({bm.get('total_trades', 0)} → {sm.get('total_trades', 0)} trades)")

    all_criteria_met = pf_improved and dd_ok and exp_improved and no_overtrade
    print()
    print(f"  OVERALL: {'ALL CRITERIA MET — PROCEED' if all_criteria_met else 'CRITERIA NOT MET — TUNE OR INVESTIGATE'}")

    print()
    print(f"  Elapsed: {elapsed:.1f}s")
    print("=" * 70)

    # Save raw results
    # (strip non-serialisable items for JSON)
    import copy
    save = copy.deepcopy(result)
    save["baseline"].pop("all_trades", None)
    save["staged"].pop("all_trades", None)
    save["staged"].pop("per_symbol_candidates", None)

    with open("reports/phase5_comparison.json", "w") as f:
        json.dump(save, f, indent=2, default=str)
    print(f"  Results saved to reports/phase5_comparison.json")


if __name__ == "__main__":
    main()
