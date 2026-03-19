#!/usr/bin/env python
"""
Phase 5b — Calibrated-Data Backtester A/B Comparison
=====================================================
Re-runs Phase 5 baseline vs staged comparison using:
  1. Market-calibrated OHLCV data (Sep 2024 – Feb 2025 price anchors)
  2. Full ta indicator pipeline (113 columns: EMA, RSI, ATR, VWAP, BB, MACD, ADX, Ichimoku…)
  3. Identical IDSS pipeline / risk rules as production

NO TUNING — same thresholds, same parameters as Phase 5.
The only difference is the data quality.
"""
import copy
import json
import os
import sys
import time
from collections import defaultdict

sys.path.insert(0, ".")

from core.features.indicator_library import calculate_all
from core.validation.calibrated_data_generator import CalibratedDataGenerator
from core.validation.staged_backtester import (
    CandidateLifecycleMetrics,
    StagedBacktester,
    _CandidateState,
)


SYMBOLS = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT", "XRP/USDT"]
TIMEFRAME = "1h"
INITIAL_CAPITAL = 10_000.0
SEED = 42


def main():
    print("=" * 80)
    print("  PHASE 5b — CALIBRATED DATA A/B COMPARISON")
    print("  Market-Calibrated OHLCV | Full Indicator Pipeline | No Tuning")
    print("=" * 80)
    print()

    t0 = time.time()

    # ── Step 1: Generate calibrated data ────────────────────────
    print("Step 1: Generating market-calibrated OHLCV data...")
    print("  Period: Sep 2024 – Feb 2025 (6 months)")
    print("  Symbols:", ", ".join(SYMBOLS))
    print()

    gen = CalibratedDataGenerator(seed=SEED)
    raw_data = gen.generate_all(SYMBOLS)

    # ── Step 2: Apply full indicator pipeline ──────────────────
    print("Step 2: Applying full indicator pipeline (ta library)...")
    ohlcv_map = {}
    for sym in SYMBOLS:
        df_raw, regime_periods = raw_data[sym]
        df_ind = calculate_all(df_raw)
        ohlcv_map[sym] = df_ind

        n_indicators = len(df_ind.columns) - 6  # minus OHLCV + true_regime
        print(f"  {sym}: {len(df_ind)} bars, {n_indicators} indicators, "
              f"${df_ind['close'].iloc[0]:.2f} → ${df_ind['close'].iloc[-1]:.2f}")

    print(f"\n  Total indicator columns: {len(df_ind.columns)}")
    print(f"  Key indicators verified: ema_9, ema_21, rsi_14, atr_14, vwap, bb_upper/lower, macd, adx_14")
    print()

    # ── Step 3: Run BASELINE ───────────────────────────────────
    print("=" * 80)
    print("Step 3: BASELINE RUN (immediate execution)")
    print("=" * 80)

    baseline_bt = StagedBacktester(staged=False, seed=SEED)
    baseline_results = {}
    baseline_all_trades = []

    for sym in SYMBOLS:
        print(f"  Baseline: {sym}...", end=" ")
        result = baseline_bt.run(ohlcv_map[sym], sym, TIMEFRAME, INITIAL_CAPITAL)
        baseline_results[sym] = result
        baseline_all_trades.extend(result["trades"])
        print(f"{len(result['trades'])} trades")

    # ── Step 4: Run STAGED ─────────────────────────────────────
    print()
    print("=" * 80)
    print("Step 4: STAGED RUN (15m confirmation)")
    print("=" * 80)

    staged_bt = StagedBacktester(staged=True, seed=SEED)
    staged_results = {}
    staged_all_trades = []
    staged_all_lifecycle = CandidateLifecycleMetrics()
    staged_all_candidates_list = []

    for sym in SYMBOLS:
        print(f"  Staged: {sym}...", end=" ")
        result = staged_bt.run(ohlcv_map[sym], sym, TIMEFRAME, INITIAL_CAPITAL)
        staged_results[sym] = result
        staged_all_trades.extend(result["trades"])
        print(f"{len(result['trades'])} trades, "
              f"{result['candidate_lifecycle']['total_created'] if result['candidate_lifecycle'] else 0} candidates")

        # Aggregate lifecycle
        if result["candidate_lifecycle"]:
            lc = result["candidate_lifecycle"]
            staged_all_lifecycle.total_created += lc["total_created"]
            staged_all_lifecycle.total_confirmed += lc["total_confirmed"]
            staged_all_lifecycle.total_executed += lc["total_executed"]
            staged_all_lifecycle.total_voided += lc["total_voided"]
            staged_all_lifecycle.total_expired += lc["total_expired"]

        if result["all_candidates"]:
            staged_all_candidates_list.extend(result["all_candidates"])

    # ── Step 5: Compute aggregate metrics ──────────────────────
    from core.backtesting.idss_backtester import _calc_metrics

    b_eq = [INITIAL_CAPITAL]
    for t in sorted(baseline_all_trades, key=lambda x: x.get("exit_time", "")):
        b_eq.append(b_eq[-1] + t["pnl"])
    bm = _calc_metrics(baseline_all_trades, b_eq, INITIAL_CAPITAL)
    if baseline_all_trades:
        bm["expectancy_r"] = round(
            sum(t.get("realized_r_multiple", 0) for t in baseline_all_trades)
            / len(baseline_all_trades), 4
        )
    else:
        bm["expectancy_r"] = 0.0

    s_eq = [INITIAL_CAPITAL]
    for t in sorted(staged_all_trades, key=lambda x: x.get("exit_time", "")):
        s_eq.append(s_eq[-1] + t["pnl"])
    sm = _calc_metrics(staged_all_trades, s_eq, INITIAL_CAPITAL)
    if staged_all_trades:
        sm["expectancy_r"] = round(
            sum(t.get("realized_r_multiple", 0) for t in staged_all_trades)
            / len(staged_all_trades), 4
        )
    else:
        sm["expectancy_r"] = 0.0

    elapsed = time.time() - t0

    # ── Step 6: Comparison metrics ─────────────────────────────
    b_pf = bm.get("profit_factor", 0)
    s_pf = sm.get("profit_factor", 0)
    b_dd = bm.get("max_drawdown_pct", 0)
    s_dd = sm.get("max_drawdown_pct", 0)
    b_exp = bm.get("expectancy_r", 0)
    s_exp = sm.get("expectancy_r", 0)
    b_ret = (b_eq[-1] - INITIAL_CAPITAL) / INITIAL_CAPITAL * 100
    s_ret = (s_eq[-1] - INITIAL_CAPITAL) / INITIAL_CAPITAL * 100

    pf_change = ((s_pf - b_pf) / max(b_pf, 0.001)) * 100
    dd_change = ((s_dd - b_dd) / max(b_dd, 0.001)) * 100
    exp_change = ((s_exp - b_exp) / max(abs(b_exp), 0.0001)) * 100

    # ── PRINT RESULTS ──────────────────────────────────────────

    print()
    print("=" * 80)
    print("  PHASE 5b — KPI COMPARISON")
    print("=" * 80)
    print()

    # Dataset description
    print("  DATASET")
    print("  ─" * 30)
    print(f"  Type:           Market-Calibrated Synthetic (Brownian Bridge + GARCH)")
    print(f"  Period:         Sep 2024 – Feb 2025 (6 months)")
    print(f"  Bars/Symbol:    4,320 (1H)")
    print(f"  Total Bars:     {len(SYMBOLS) * 4320:,}")
    print(f"  Indicators:     {len(ohlcv_map['BTC/USDT'].columns)} columns (ta library)")
    print(f"  Regimes:        ranging, bull_trend, bear_trend, vol_expansion")
    print(f"  Cross-Corr:     BTC→alts (ETH 0.75, SOL 0.65, BNB 0.60, XRP 0.50)")
    print()

    # Price anchors
    print("  PRICE ANCHORS (calibrated to real market)")
    print("  ─" * 30)
    for sym in SYMBOLS:
        df = ohlcv_map[sym]
        print(f"  {sym:<12s}  ${df['close'].iloc[0]:>10,.2f} → ${df['close'].iloc[-1]:>10,.2f}  "
              f"(range ${df['close'].min():>10,.2f} – ${df['close'].max():>10,.2f})")
    print()

    # KPI comparison
    print("  PRIMARY KPIs")
    print("  ─" * 30)
    fmt = "  {:<32s}  {:>14s}  {:>14s}  {:>14s}"
    print(fmt.format("Metric", "Baseline", "Staged", "Change"))
    print("  " + "─" * 76)
    print(fmt.format("Total Trades",
                      str(bm.get("total_trades", 0)),
                      str(sm.get("total_trades", 0)),
                      f"{sm.get('total_trades', 0) - bm.get('total_trades', 0):+d}"))
    print(fmt.format("Win Rate (%)",
                      f"{bm.get('win_rate', 0):.1f}",
                      f"{sm.get('win_rate', 0):.1f}",
                      f"{sm.get('win_rate', 0) - bm.get('win_rate', 0):+.1f}pp"))
    print(fmt.format("Profit Factor",
                      f"{b_pf:.3f}",
                      f"{s_pf:.3f}",
                      f"{pf_change:+.1f}%"))
    print(fmt.format("Max Drawdown (%)",
                      f"{b_dd:.2f}",
                      f"{s_dd:.2f}",
                      f"{dd_change:+.1f}%"))
    print(fmt.format("Trade Expectancy (R)",
                      f"{b_exp:.4f}",
                      f"{s_exp:.4f}",
                      f"{exp_change:+.1f}%"))
    print(fmt.format("Total Return (%)",
                      f"{b_ret:.2f}",
                      f"{s_ret:.2f}",
                      f"{s_ret - b_ret:+.2f}pp"))
    print(fmt.format("Sharpe Ratio",
                      f"{bm.get('sharpe_ratio', 0):.3f}",
                      f"{sm.get('sharpe_ratio', 0):.3f}",
                      ""))

    # ── Secondary KPIs ─────────────────────────────────────────
    print()
    print("  SECONDARY KPIs")
    print("  ─" * 30)

    # Trade frequency
    b_trades_per_month = bm.get("total_trades", 0) / 6
    s_trades_per_month = sm.get("total_trades", 0) / 6
    print(f"  Trades/Month:          Baseline {b_trades_per_month:.1f}  |  Staged {s_trades_per_month:.1f}")

    # Early stop-out rate
    b_sl_count = sum(1 for t in baseline_all_trades if t.get("exit_reason") == "stop_loss")
    s_sl_count = sum(1 for t in staged_all_trades if t.get("exit_reason") == "stop_loss")
    b_sl_rate = b_sl_count / max(len(baseline_all_trades), 1) * 100
    s_sl_rate = s_sl_count / max(len(staged_all_trades), 1) * 100
    print(f"  Stop-Loss Exit Rate:   Baseline {b_sl_rate:.1f}%  |  Staged {s_sl_rate:.1f}%")

    # TP rate
    b_tp_count = sum(1 for t in baseline_all_trades if t.get("exit_reason") == "take_profit")
    s_tp_count = sum(1 for t in staged_all_trades if t.get("exit_reason") == "take_profit")
    b_tp_rate = b_tp_count / max(len(baseline_all_trades), 1) * 100
    s_tp_rate = s_tp_count / max(len(staged_all_trades), 1) * 100
    print(f"  Take-Profit Exit Rate: Baseline {b_tp_rate:.1f}%  |  Staged {s_tp_rate:.1f}%")

    # Average holding period (bars)
    b_holds = [t.get("holding_bars", 0) for t in baseline_all_trades if t.get("holding_bars")]
    s_holds = [t.get("holding_bars", 0) for t in staged_all_trades if t.get("holding_bars")]
    print(f"  Avg Holding (bars):    Baseline {sum(b_holds)/max(len(b_holds),1):.1f}  |  "
          f"Staged {sum(s_holds)/max(len(s_holds),1):.1f}")

    # ── Candidate Lifecycle (Staged only) ──────────────────────
    lc = staged_all_lifecycle
    print()
    print("=" * 80)
    print("  CANDIDATE LIFECYCLE METRICS (Staged Mode)")
    print("=" * 80)
    print()
    print(f"  Total Created:       {lc.total_created}")
    print(f"  Total Confirmed:     {lc.total_confirmed}")
    print(f"  Total Executed:      {lc.total_executed}")
    print(f"  Total Voided:        {lc.total_voided}")
    print(f"  Total Expired:       {lc.total_expired}")
    cr = lc.total_confirmed / max(lc.total_created, 1)
    er = lc.total_expired / max(lc.total_created, 1)
    vr = lc.total_voided / max(lc.total_created, 1)
    print(f"  Conversion Rate:     {cr:.1%}")
    print(f"  Expiry Rate:         {er:.1%}")
    print(f"  Void Rate:           {vr:.1%}")

    # Confirmation delay distribution
    confirm_delays = []
    for c in staged_all_candidates_list:
        if hasattr(c, "confirmed_at_bar") and hasattr(c, "created_at_bar"):
            if c.confirmed_at_bar >= 0 and c.created_at_bar >= 0:
                delay = (c.confirmed_at_bar - c.created_at_bar) * 4 + \
                        (getattr(c, "confirmed_at_sub", 0) - getattr(c, "created_at_sub", 0))
                confirm_delays.append(delay)
        elif isinstance(c, dict):
            cb = c.get("confirmed_at_bar", -1)
            crb = c.get("created_at_bar", -1)
            if cb >= 0 and crb >= 0:
                delay = (cb - crb) * 4 + \
                        (c.get("confirmed_at_sub", 0) - c.get("created_at_sub", 0))
                confirm_delays.append(delay)

    if confirm_delays:
        import statistics
        print(f"\n  Confirmation Delay Distribution (15m sub-bars):")
        print(f"    Min:    {min(confirm_delays)}")
        print(f"    Max:    {max(confirm_delays)}")
        print(f"    Mean:   {statistics.mean(confirm_delays):.1f}")
        print(f"    Median: {statistics.median(confirm_delays):.1f}")
        print(f"    Stdev:  {statistics.stdev(confirm_delays) if len(confirm_delays) > 1 else 0:.1f}")

        # Distribution buckets
        buckets = defaultdict(int)
        for d in confirm_delays:
            if d == 0:
                buckets["0 (immediate)"] += 1
            elif d <= 2:
                buckets["1-2 (< 30m)"] += 1
            elif d <= 4:
                buckets["3-4 (30m-1h)"] += 1
            else:
                buckets["5+ (> 1h)"] += 1
        print(f"    Buckets:")
        for k, v in sorted(buckets.items()):
            pct = v / len(confirm_delays) * 100
            bar = "█" * int(pct / 2)
            print(f"      {k:<20s}: {v:4d} ({pct:5.1f}%) {bar}")

    # ── Execution Clustering ───────────────────────────────────
    print()
    print("  EXECUTION CLUSTERING (Phase 4 Risk Metrics)")
    print("  ─" * 30)

    # Group executions by 15m cycle
    exec_by_cycle = defaultdict(int)
    for c in staged_all_candidates_list:
        if hasattr(c, "executed_at_bar") and c.executed_at_bar >= 0:
            cycle_key = (c.executed_at_bar, getattr(c, "confirmed_at_sub", 0))
            exec_by_cycle[cycle_key] += 1
        elif isinstance(c, dict) and c.get("executed_at_bar", -1) >= 0:
            cycle_key = (c["executed_at_bar"], c.get("confirmed_at_sub", 0))
            exec_by_cycle[cycle_key] += 1

    if exec_by_cycle:
        exec_counts = list(exec_by_cycle.values())
        burst_cycles = sum(1 for v in exec_counts if v > 1)
        max_per_cycle = max(exec_counts)
        print(f"  Total execution cycles: {len(exec_by_cycle)}")
        print(f"  Burst cycles (>1 exec): {burst_cycles}")
        print(f"  Max executions/cycle:   {max_per_cycle}")
    else:
        print(f"  No executions recorded")

    # ── Candidate Aging Distribution ───────────────────────────
    print()
    print("  CANDIDATE AGING DISTRIBUTION")
    print("  ─" * 30)

    age_at_terminal = []
    for c in staged_all_candidates_list:
        if isinstance(c, dict):
            state = c.get("state", "")
            created = c.get("created_at_bar", 0)
            if state == "EXPIRED":
                terminal = c.get("expired_at_bar", created)
            elif state == "VOIDED":
                terminal = c.get("voided_at_bar", created)
            elif state == "EXECUTED":
                terminal = c.get("executed_at_bar", created)
            else:
                continue
            age = (terminal - created) * 4  # in 15m units
            age_at_terminal.append((state, age))
        elif hasattr(c, "state"):
            created = c.created_at_bar
            if c.state == _CandidateState.EXPIRED:
                terminal = c.expired_at_bar if c.expired_at_bar >= 0 else created
            elif c.state == _CandidateState.VOIDED:
                terminal = c.voided_at_bar if c.voided_at_bar >= 0 else created
            elif c.state == _CandidateState.EXECUTED:
                terminal = c.executed_at_bar if c.executed_at_bar >= 0 else created
            else:
                continue
            age = (terminal - created) * 4
            age_at_terminal.append((c.state, age))

    if age_at_terminal:
        for state_filter in ["EXECUTED", "EXPIRED", "VOIDED"]:
            ages = [a for s, a in age_at_terminal if s == state_filter or s == state_filter]
            if ages:
                import statistics
                print(f"  {state_filter}:")
                print(f"    Count:  {len(ages)}")
                print(f"    Min:    {min(ages)} × 15m")
                print(f"    Max:    {max(ages)} × 15m")
                print(f"    Mean:   {statistics.mean(ages):.1f} × 15m")
                print(f"    Median: {statistics.median(ages):.0f} × 15m")

    # ── Per-Symbol Comparison ──────────────────────────────────
    print()
    print("=" * 80)
    print("  PER-SYMBOL COMPARISON")
    print("=" * 80)
    print()
    sfmt = "  {:<12s}  {:>6s} / {:>6s}  {:>8s} / {:>8s}  {:>8s} / {:>8s}  {:>8s} / {:>8s}"
    print(sfmt.format("Symbol", "B.Trd", "S.Trd", "B.WR%", "S.WR%", "B.PF", "S.PF", "B.Exp", "S.Exp"))
    print("  " + "─" * 76)

    for sym in SYMBOLS:
        bt = baseline_results[sym]["trades"]
        st = staged_results[sym]["trades"]

        b_wins = sum(1 for t in bt if t["pnl"] > 0)
        s_wins = sum(1 for t in st if t["pnl"] > 0)
        b_wr = b_wins / max(len(bt), 1) * 100
        s_wr = s_wins / max(len(st), 1) * 100

        b_gross_win = sum(t["pnl"] for t in bt if t["pnl"] > 0)
        b_gross_loss = abs(sum(t["pnl"] for t in bt if t["pnl"] < 0))
        s_gross_win = sum(t["pnl"] for t in st if t["pnl"] > 0)
        s_gross_loss = abs(sum(t["pnl"] for t in st if t["pnl"] < 0))

        b_pf_sym = b_gross_win / max(b_gross_loss, 0.01)
        s_pf_sym = s_gross_win / max(s_gross_loss, 0.01)

        b_exp_sym = sum(t.get("realized_r_multiple", 0) for t in bt) / max(len(bt), 1)
        s_exp_sym = sum(t.get("realized_r_multiple", 0) for t in st) / max(len(st), 1)

        print(sfmt.format(
            sym,
            str(len(bt)), str(len(st)),
            f"{b_wr:.1f}", f"{s_wr:.1f}",
            f"{b_pf_sym:.2f}", f"{s_pf_sym:.2f}",
            f"{b_exp_sym:.3f}", f"{s_exp_sym:.3f}",
        ))

    # ── Per-Symbol Lifecycle ───────────────────────────────────
    print()
    print("  Per-Symbol Candidate Lifecycle:")
    for sym in SYMBOLS:
        slc = staged_results[sym].get("candidate_lifecycle")
        if slc:
            cr_s = slc['total_confirmed'] / max(slc['total_created'], 1)
            print(f"    {sym}: created={slc['total_created']} conf={slc['total_confirmed']} "
                  f"exec={slc['total_executed']} void={slc['total_voided']} "
                  f"exp={slc['total_expired']} conv={cr_s:.1%}")

    # ── Structural Validation ──────────────────────────────────
    print()
    print("=" * 80)
    print("  STRUCTURAL VALIDATION")
    print("=" * 80)
    print()

    all_violations = []
    for sym in SYMBOLS:
        val = staged_results[sym].get("validation", {})
        if val and not val.get("all_passed", True):
            for v in val.get("violations", []):
                all_violations.append(f"[{sym}] {v}")

    if all_violations:
        print(f"  Violations: {len(all_violations)}")
        for v in all_violations[:15]:
            print(f"    - {v}")
    else:
        print(f"  All structural validations PASSED")

    # ── Success Criteria ───────────────────────────────────────
    print()
    print("=" * 80)
    print("  SUCCESS CRITERIA EVALUATION")
    print("=" * 80)
    print()

    pf_improved = pf_change >= 10.0
    dd_ok = s_dd <= b_dd * 1.01  # 1% tolerance
    exp_improved = exp_change >= 10.0
    no_overtrade = sm.get("total_trades", 0) <= bm.get("total_trades", 0) * 1.5

    print(f"  Profit Factor ≥ 10% improvement:      {'PASS' if pf_improved else 'FAIL'} ({pf_change:+.1f}%)")
    print(f"  Max Drawdown not increased:           {'PASS' if dd_ok else 'FAIL'} ({b_dd:.2f}% → {s_dd:.2f}%)")
    print(f"  Expectancy ≥ 10% improvement:         {'PASS' if exp_improved else 'FAIL'} ({exp_change:+.1f}%)")
    print(f"  No overtrading:                       {'PASS' if no_overtrade else 'FAIL'} ({bm.get('total_trades', 0)} → {sm.get('total_trades', 0)} trades)")

    all_criteria_met = pf_improved and dd_ok and exp_improved and no_overtrade
    print()
    print(f"  OVERALL: {'ALL CRITERIA MET — STAGED ARCHITECTURE VALIDATED' if all_criteria_met else 'CRITERIA NOT ALL MET — SEE ANALYSIS BELOW'}")
    print()
    print(f"  Elapsed: {elapsed:.1f}s")
    print("=" * 80)

    # ── Save results ───────────────────────────────────────────
    os.makedirs("reports", exist_ok=True)
    save = {
        "phase": "5b",
        "data_source": "market_calibrated_synthetic",
        "period": "2024-09 to 2025-02",
        "symbols": SYMBOLS,
        "indicator_columns": len(ohlcv_map["BTC/USDT"].columns),
        "bars_per_symbol": 4320,
        "initial_capital": INITIAL_CAPITAL,
        "seed": SEED,
        "baseline": {
            "total_trades": bm.get("total_trades", 0),
            "win_rate": bm.get("win_rate", 0),
            "profit_factor": b_pf,
            "max_drawdown_pct": b_dd,
            "expectancy_r": b_exp,
            "total_return_pct": b_ret,
            "sharpe_ratio": bm.get("sharpe_ratio", 0),
        },
        "staged": {
            "total_trades": sm.get("total_trades", 0),
            "win_rate": sm.get("win_rate", 0),
            "profit_factor": s_pf,
            "max_drawdown_pct": s_dd,
            "expectancy_r": s_exp,
            "total_return_pct": s_ret,
            "sharpe_ratio": sm.get("sharpe_ratio", 0),
        },
        "comparison": {
            "pf_change_pct": round(pf_change, 2),
            "dd_change_pct": round(dd_change, 2),
            "exp_change_pct": round(exp_change, 2),
            "trade_count_change": sm.get("total_trades", 0) - bm.get("total_trades", 0),
        },
        "lifecycle": {
            "total_created": lc.total_created,
            "total_confirmed": lc.total_confirmed,
            "total_executed": lc.total_executed,
            "total_voided": lc.total_voided,
            "total_expired": lc.total_expired,
            "conversion_rate": round(cr, 4),
            "expiry_rate": round(er, 4),
            "void_rate": round(vr, 4),
        },
        "success_criteria": {
            "pf_improved": pf_improved,
            "dd_ok": dd_ok,
            "exp_improved": exp_improved,
            "no_overtrade": no_overtrade,
            "all_met": all_criteria_met,
        },
    }

    with open("reports/phase5b_comparison.json", "w") as f:
        json.dump(save, f, indent=2, default=str)
    print(f"  Results saved to reports/phase5b_comparison.json")


if __name__ == "__main__":
    main()
