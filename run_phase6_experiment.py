#!/usr/bin/env python
"""
Phase 6 — LTF Confirmation Optimization
=========================================
Tests multiple LTF confirmation configurations against the same
calibrated dataset and full indicator pipeline.

Architecture, execution logic, and candidate lifecycle are UNCHANGED.
Only LTF confirmation thresholds vary between configs.
"""
import json
import os
import statistics
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field

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


# ─────────────────────────────────────────────────────────────────────────────
# Configuration Definitions
# ─────────────────────────────────────────────────────────────────────────────

CONFIGS = {
    # Reference: Phase 5b defaults (no LTF tuning)
    "Phase5b_Default": {
        # All defaults: ema_slope > 0, rsi ≤ 72 (long) / ≥ 28 (short), vol ≥ 0.8
    },

    # ── Mandatory Configs A, B, C ────────────────────────────────

    "A_Balanced": {
        "ema_slope_min": 0.001,    # > 0.1% over 3 bars
        "ema_slope_bars": 3,
        "rsi_max_long": 65.0,
        "rsi_min_short": 35.0,
        "volume_ratio_min": 1.0,
    },

    "B_Strict": {
        "ema_slope_min": 0.002,    # > 0.2% over 5 bars
        "ema_slope_bars": 5,
        "rsi_max_long": 60.0,
        "rsi_min_short": 40.0,
        "volume_ratio_min": 1.2,
    },

    "C_Momentum": {
        "ema_slope_min": 0.0,      # > 0 over 3 bars (same as default)
        "ema_slope_bars": 3,
        "rsi_min_long": 45.0,      # RSI 45-65 for longs
        "rsi_max_long": 65.0,
        "rsi_min_short": 35.0,     # RSI 35-55 for shorts
        "rsi_max_short": 55.0,
        "volume_ratio_min": 1.1,
    },

    # ── Additional Variations ────────────────────────────────────

    "D_Balanced_Plus": {
        # Config A with slightly tighter void thresholds
        "ema_slope_min": 0.001,
        "ema_slope_bars": 3,
        "rsi_max_long": 65.0,
        "rsi_min_short": 35.0,
        "rsi_void_long": 72.0,     # Tighter void (default 78)
        "rsi_void_short": 28.0,    # Tighter void (default 22)
        "volume_ratio_min": 1.0,
    },

    "E_Strict_Relaxed_Vol": {
        # Config B but with volume ratio relaxed back to 1.0
        "ema_slope_min": 0.002,
        "ema_slope_bars": 5,
        "rsi_max_long": 60.0,
        "rsi_min_short": 40.0,
        "volume_ratio_min": 1.0,   # Relaxed from 1.2
    },

    "F_Momentum_Tight_Band": {
        # Config C with tighter RSI band
        "ema_slope_min": 0.0005,   # Slight EMA slope required
        "ema_slope_bars": 3,
        "rsi_min_long": 48.0,      # RSI 48-62 for longs
        "rsi_max_long": 62.0,
        "rsi_min_short": 38.0,     # RSI 38-52 for shorts
        "rsi_max_short": 52.0,
        "volume_ratio_min": 1.1,
    },

    "G_Hybrid_AB": {
        # Blend of A's slope with B's RSI
        "ema_slope_min": 0.001,    # A's slope threshold
        "ema_slope_bars": 4,       # Between A and B
        "rsi_max_long": 60.0,      # B's RSI
        "rsi_min_short": 40.0,     # B's RSI
        "volume_ratio_min": 1.1,   # Between A and B
    },

    "H_Slope_Focus": {
        # Strong EMA slope requirement, relaxed RSI
        "ema_slope_min": 0.003,    # 0.3% slope — strongest
        "ema_slope_bars": 3,
        "rsi_max_long": 68.0,      # Relaxed RSI
        "rsi_min_short": 32.0,
        "volume_ratio_min": 0.9,   # Relaxed volume
    },

    "I_Volume_Focus": {
        # High volume requirement, moderate others
        "ema_slope_min": 0.0005,
        "ema_slope_bars": 3,
        "rsi_max_long": 65.0,
        "rsi_min_short": 35.0,
        "volume_ratio_min": 1.3,   # Highest volume threshold
    },

    "J_Ultra_Strict": {
        # Tightest version of everything
        "ema_slope_min": 0.002,
        "ema_slope_bars": 5,
        "rsi_min_long": 45.0,
        "rsi_max_long": 58.0,
        "rsi_min_short": 42.0,
        "rsi_max_short": 55.0,
        "rsi_void_long": 70.0,
        "rsi_void_short": 30.0,
        "volume_ratio_min": 1.2,
    },
}


# ─────────────────────────────────────────────────────────────────────────────
# Experiment Runner
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class ExperimentResult:
    name: str
    # Primary KPIs
    total_trades: int = 0
    win_rate: float = 0.0
    profit_factor: float = 0.0
    max_drawdown_pct: float = 0.0
    expectancy_r: float = 0.0
    total_return_pct: float = 0.0
    sharpe_ratio: float = 0.0
    # Secondary
    sl_rate: float = 0.0
    tp_rate: float = 0.0
    trades_per_month: float = 0.0
    # Lifecycle
    total_created: int = 0
    total_confirmed: int = 0
    total_voided: int = 0
    total_expired: int = 0
    conversion_rate: float = 0.0
    expiry_rate: float = 0.0
    void_rate: float = 0.0
    # Confirmation behavior
    pct_immediate: float = 0.0
    avg_confirm_delay: float = 0.0
    median_confirm_delay: float = 0.0
    max_confirm_delay: int = 0
    # Execution clustering
    burst_cycles: int = 0
    max_per_cycle: int = 0
    # Raw data for analysis
    all_trades: list = field(default_factory=list, repr=False)
    confirm_delays: list = field(default_factory=list, repr=False)


def run_single_experiment(
    name: str,
    ltf_config: dict,
    ohlcv_map: dict,
    baseline_metrics: dict | None = None,
) -> ExperimentResult:
    """Run a single staged backtester experiment."""
    from core.backtesting.idss_backtester import _calc_metrics

    bt = StagedBacktester(staged=True, seed=SEED, ltf_config=ltf_config)
    all_trades = []
    lifecycle = CandidateLifecycleMetrics()
    all_candidates = []

    for sym in SYMBOLS:
        result = bt.run(ohlcv_map[sym], sym, TIMEFRAME, INITIAL_CAPITAL)
        all_trades.extend(result["trades"])

        if result["candidate_lifecycle"]:
            lc = result["candidate_lifecycle"]
            lifecycle.total_created += lc["total_created"]
            lifecycle.total_confirmed += lc["total_confirmed"]
            lifecycle.total_executed += lc["total_executed"]
            lifecycle.total_voided += lc["total_voided"]
            lifecycle.total_expired += lc["total_expired"]

        if result["all_candidates"]:
            all_candidates.extend(result["all_candidates"])

    # Compute metrics
    eq = [INITIAL_CAPITAL]
    for t in sorted(all_trades, key=lambda x: x.get("exit_time", "")):
        eq.append(eq[-1] + t["pnl"])

    m = _calc_metrics(all_trades, eq, INITIAL_CAPITAL)
    if all_trades:
        exp_r = sum(t.get("realized_r_multiple", 0) for t in all_trades) / len(all_trades)
    else:
        exp_r = 0.0

    # Confirmation delays
    confirm_delays = []
    for c in all_candidates:
        if isinstance(c, dict):
            cb = c.get("confirmed_at_bar", -1)
            crb = c.get("created_at_bar", -1)
            if cb >= 0 and crb >= 0:
                d = (cb - crb) * 4 + (c.get("confirmed_at_sub", 0) - c.get("created_at_sub", 0))
                confirm_delays.append(d)
        elif hasattr(c, "confirmed_at_bar"):
            if c.confirmed_at_bar >= 0 and c.created_at_bar >= 0:
                d = (c.confirmed_at_bar - c.created_at_bar) * 4 + \
                    (getattr(c, "confirmed_at_sub", 0) - getattr(c, "created_at_sub", 0))
                confirm_delays.append(d)

    pct_immediate = (sum(1 for d in confirm_delays if d == 0) / max(len(confirm_delays), 1)) * 100

    # Execution clustering
    exec_by_cycle = defaultdict(int)
    for c in all_candidates:
        eb = c.get("executed_at_bar", -1) if isinstance(c, dict) else getattr(c, "executed_at_bar", -1)
        if eb >= 0:
            sub = c.get("confirmed_at_sub", 0) if isinstance(c, dict) else getattr(c, "confirmed_at_sub", 0)
            exec_by_cycle[(eb, sub)] += 1

    exec_counts = list(exec_by_cycle.values()) if exec_by_cycle else [0]
    burst_cycles = sum(1 for v in exec_counts if v > 1)
    max_per_cycle = max(exec_counts) if exec_counts else 0

    sl_count = sum(1 for t in all_trades if t.get("exit_reason") == "stop_loss")
    tp_count = sum(1 for t in all_trades if t.get("exit_reason") == "take_profit")

    return ExperimentResult(
        name=name,
        total_trades=m.get("total_trades", 0),
        win_rate=m.get("win_rate", 0),
        profit_factor=m.get("profit_factor", 0),
        max_drawdown_pct=m.get("max_drawdown_pct", 0),
        expectancy_r=round(exp_r, 4),
        total_return_pct=round((eq[-1] - INITIAL_CAPITAL) / INITIAL_CAPITAL * 100, 2),
        sharpe_ratio=m.get("sharpe_ratio", 0),
        sl_rate=round(sl_count / max(len(all_trades), 1) * 100, 1),
        tp_rate=round(tp_count / max(len(all_trades), 1) * 100, 1),
        trades_per_month=round(len(all_trades) / 6, 1),
        total_created=lifecycle.total_created,
        total_confirmed=lifecycle.total_confirmed,
        total_voided=lifecycle.total_voided,
        total_expired=lifecycle.total_expired,
        conversion_rate=round(lifecycle.total_confirmed / max(lifecycle.total_created, 1) * 100, 1),
        expiry_rate=round(lifecycle.total_expired / max(lifecycle.total_created, 1) * 100, 1),
        void_rate=round(lifecycle.total_voided / max(lifecycle.total_created, 1) * 100, 1),
        pct_immediate=round(pct_immediate, 1),
        avg_confirm_delay=round(statistics.mean(confirm_delays), 1) if confirm_delays else 0.0,
        median_confirm_delay=round(statistics.median(confirm_delays), 1) if confirm_delays else 0.0,
        max_confirm_delay=max(confirm_delays) if confirm_delays else 0,
        burst_cycles=burst_cycles,
        max_per_cycle=max_per_cycle,
        all_trades=all_trades,
        confirm_delays=confirm_delays,
    )


def run_baseline(ohlcv_map: dict) -> ExperimentResult:
    """Run baseline (immediate execution, no LTF confirmation)."""
    from core.backtesting.idss_backtester import _calc_metrics

    bt = StagedBacktester(staged=False, seed=SEED)
    all_trades = []

    for sym in SYMBOLS:
        result = bt.run(ohlcv_map[sym], sym, TIMEFRAME, INITIAL_CAPITAL)
        all_trades.extend(result["trades"])

    eq = [INITIAL_CAPITAL]
    for t in sorted(all_trades, key=lambda x: x.get("exit_time", "")):
        eq.append(eq[-1] + t["pnl"])

    m = _calc_metrics(all_trades, eq, INITIAL_CAPITAL)
    exp_r = sum(t.get("realized_r_multiple", 0) for t in all_trades) / max(len(all_trades), 1)

    sl_count = sum(1 for t in all_trades if t.get("exit_reason") == "stop_loss")
    tp_count = sum(1 for t in all_trades if t.get("exit_reason") == "take_profit")

    return ExperimentResult(
        name="BASELINE",
        total_trades=m.get("total_trades", 0),
        win_rate=m.get("win_rate", 0),
        profit_factor=m.get("profit_factor", 0),
        max_drawdown_pct=m.get("max_drawdown_pct", 0),
        expectancy_r=round(exp_r, 4),
        total_return_pct=round((eq[-1] - INITIAL_CAPITAL) / INITIAL_CAPITAL * 100, 2),
        sharpe_ratio=m.get("sharpe_ratio", 0),
        sl_rate=round(sl_count / max(len(all_trades), 1) * 100, 1),
        tp_rate=round(tp_count / max(len(all_trades), 1) * 100, 1),
        trades_per_month=round(len(all_trades) / 6, 1),
        all_trades=all_trades,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main():
    print("=" * 100)
    print("  PHASE 6 — LTF CONFIRMATION OPTIMIZATION")
    print("  Testing", len(CONFIGS), "configurations + baseline")
    print("=" * 100)
    print()

    t0 = time.time()

    # Step 1: Generate data
    print("Generating market-calibrated OHLCV data...")
    gen = CalibratedDataGenerator(seed=SEED)
    raw_data = gen.generate_all(SYMBOLS)

    ohlcv_map = {}
    for sym in SYMBOLS:
        df_raw, _ = raw_data[sym]
        ohlcv_map[sym] = calculate_all(df_raw)
    print(f"  {len(SYMBOLS)} symbols × 4,320 bars × {len(ohlcv_map['BTC/USDT'].columns)} columns\n")

    # Step 2: Run baseline
    print("Running BASELINE (immediate execution)...")
    baseline = run_baseline(ohlcv_map)
    print(f"  → {baseline.total_trades} trades, PF={baseline.profit_factor:.3f}, "
          f"WR={baseline.win_rate:.1f}%, E[R]={baseline.expectancy_r:.4f}\n")

    # Step 3: Run all configs
    results = {"BASELINE": baseline}
    for name, ltf_config in CONFIGS.items():
        print(f"Running {name}...", end=" ", flush=True)
        r = run_single_experiment(name, ltf_config, ohlcv_map)
        results[name] = r
        print(f"→ {r.total_trades} trades, PF={r.profit_factor:.3f}, "
              f"WR={r.win_rate:.1f}%, E[R]={r.expectancy_r:.4f}, "
              f"Imm={r.pct_immediate:.0f}%, Conv={r.conversion_rate:.0f}%")

    elapsed = time.time() - t0
    print(f"\nAll experiments complete in {elapsed:.0f}s\n")

    # ═════════════════════════════════════════════════════════════════════════
    # RESULTS
    # ═════════════════════════════════════════════════════════════════════════

    # Baseline reference values
    b = results["BASELINE"]
    p5b = results["Phase5b_Default"]

    # ── Table 1: PRIMARY KPI COMPARISON ──────────────────────────────────────
    print("=" * 100)
    print("  TABLE 1: PRIMARY KPI COMPARISON")
    print("=" * 100)
    print()

    hdr = f"  {'Config':<22s} {'Trades':>6s} {'WR%':>7s} {'PF':>8s} {'MaxDD%':>8s} {'E[R]':>10s} {'Return%':>9s} {'Sharpe':>8s}"
    print(hdr)
    print("  " + "─" * 96)

    for name in ["BASELINE", "Phase5b_Default", "A_Balanced", "B_Strict", "C_Momentum",
                  "D_Balanced_Plus", "E_Strict_Relaxed_Vol", "F_Momentum_Tight_Band",
                  "G_Hybrid_AB", "H_Slope_Focus", "I_Volume_Focus", "J_Ultra_Strict"]:
        r = results[name]
        mark = ""
        if name == "BASELINE":
            mark = " ◆"
        elif name == "Phase5b_Default":
            mark = " ○"
        print(f"  {name:<22s} {r.total_trades:>6d} {r.win_rate:>6.1f}% {r.profit_factor:>8.3f} "
              f"{r.max_drawdown_pct:>7.2f}% {r.expectancy_r:>+10.4f} {r.total_return_pct:>+8.2f}% "
              f"{r.sharpe_ratio:>8.3f}{mark}")

    # ── PF improvement vs baseline ───────────────────────────────────────────
    print()
    print("  PF Improvement vs Baseline:")
    for name, r in results.items():
        if name == "BASELINE":
            continue
        pf_delta = ((r.profit_factor - b.profit_factor) / max(b.profit_factor, 0.001)) * 100
        tag = "✓" if pf_delta >= 10 else "✗"
        print(f"    {tag} {name:<22s}: {pf_delta:+.1f}% (target ≥ +10%)")

    # ── Table 2: SECONDARY KPIs ──────────────────────────────────────────────
    print()
    print("=" * 100)
    print("  TABLE 2: SECONDARY KPIs")
    print("=" * 100)
    print()

    hdr2 = f"  {'Config':<22s} {'Trd/Mo':>7s} {'SL%':>7s} {'TP%':>7s}"
    print(hdr2)
    print("  " + "─" * 46)

    for name in ["BASELINE", "Phase5b_Default", "A_Balanced", "B_Strict", "C_Momentum",
                  "D_Balanced_Plus", "E_Strict_Relaxed_Vol", "F_Momentum_Tight_Band",
                  "G_Hybrid_AB", "H_Slope_Focus", "I_Volume_Focus", "J_Ultra_Strict"]:
        r = results[name]
        print(f"  {name:<22s} {r.trades_per_month:>7.1f} {r.sl_rate:>6.1f}% {r.tp_rate:>6.1f}%")

    # ── Table 3: CANDIDATE LIFECYCLE ─────────────────────────────────────────
    print()
    print("=" * 100)
    print("  TABLE 3: CANDIDATE LIFECYCLE")
    print("=" * 100)
    print()

    hdr3 = f"  {'Config':<22s} {'Created':>8s} {'Conf':>6s} {'Exec':>6s} {'Void':>6s} {'Exp':>6s} {'Conv%':>7s} {'Exp%':>6s} {'Void%':>6s}"
    print(hdr3)
    print("  " + "─" * 82)

    for name in ["Phase5b_Default", "A_Balanced", "B_Strict", "C_Momentum",
                  "D_Balanced_Plus", "E_Strict_Relaxed_Vol", "F_Momentum_Tight_Band",
                  "G_Hybrid_AB", "H_Slope_Focus", "I_Volume_Focus", "J_Ultra_Strict"]:
        r = results[name]
        print(f"  {name:<22s} {r.total_created:>8d} {r.total_confirmed:>6d} "
              f"{r.total_confirmed:>6d} {r.total_voided:>6d} {r.total_expired:>6d} "
              f"{r.conversion_rate:>6.1f}% {r.expiry_rate:>5.1f}% {r.void_rate:>5.1f}%")

    # ── Table 4: CONFIRMATION BEHAVIOR ───────────────────────────────────────
    print()
    print("=" * 100)
    print("  TABLE 4: CONFIRMATION BEHAVIOR (Critical: % Immediate)")
    print("=" * 100)
    print()

    hdr4 = f"  {'Config':<22s} {'%Immed':>8s} {'AvgDly':>8s} {'MedDly':>8s} {'MaxDly':>8s} {'Burst':>7s} {'Max/Cyc':>8s}"
    print(hdr4)
    print("  " + "─" * 72)

    for name in ["Phase5b_Default", "A_Balanced", "B_Strict", "C_Momentum",
                  "D_Balanced_Plus", "E_Strict_Relaxed_Vol", "F_Momentum_Tight_Band",
                  "G_Hybrid_AB", "H_Slope_Focus", "I_Volume_Focus", "J_Ultra_Strict"]:
        r = results[name]
        print(f"  {name:<22s} {r.pct_immediate:>7.1f}% {r.avg_confirm_delay:>8.1f} "
              f"{r.median_confirm_delay:>8.1f} {r.max_confirm_delay:>8d} "
              f"{r.burst_cycles:>7d} {r.max_per_cycle:>8d}")

    # ── Table 5: CONFIRMATION DELAY DISTRIBUTION ─────────────────────────────
    print()
    print("=" * 100)
    print("  TABLE 5: CONFIRMATION DELAY DISTRIBUTION (15m sub-bars)")
    print("=" * 100)
    print()

    hdr5 = f"  {'Config':<22s} {'0(imm)':>8s} {'1-2':>8s} {'3-4':>8s} {'5+':>8s}"
    print(hdr5)
    print("  " + "─" * 56)

    for name in ["Phase5b_Default", "A_Balanced", "B_Strict", "C_Momentum",
                  "D_Balanced_Plus", "E_Strict_Relaxed_Vol", "F_Momentum_Tight_Band",
                  "G_Hybrid_AB", "H_Slope_Focus", "I_Volume_Focus", "J_Ultra_Strict"]:
        r = results[name]
        if r.confirm_delays:
            n = len(r.confirm_delays)
            b0 = sum(1 for d in r.confirm_delays if d == 0)
            b12 = sum(1 for d in r.confirm_delays if 1 <= d <= 2)
            b34 = sum(1 for d in r.confirm_delays if 3 <= d <= 4)
            b5p = sum(1 for d in r.confirm_delays if d >= 5)
            print(f"  {name:<22s} {b0/n*100:>7.1f}% {b12/n*100:>7.1f}% {b34/n*100:>7.1f}% {b5p/n*100:>7.1f}%")
        else:
            print(f"  {name:<22s}  N/A")

    # ── SUCCESS CRITERIA EVALUATION ──────────────────────────────────────────
    print()
    print("=" * 100)
    print("  SUCCESS CRITERIA EVALUATION")
    print("=" * 100)
    print()
    print(f"  Reference: Baseline PF={b.profit_factor:.3f}, Phase5b PF={p5b.profit_factor:.3f}, "
          f"Phase5b DD={p5b.max_drawdown_pct:.2f}%")
    print()

    best_name = None
    best_score = -999

    for name, r in results.items():
        if name == "BASELINE":
            continue

        pf_vs_base = ((r.profit_factor - b.profit_factor) / max(b.profit_factor, 0.001)) * 100
        pf_pass = pf_vs_base >= 10
        dd_pass = r.max_drawdown_pct <= p5b.max_drawdown_pct * 1.01
        exp_pass = r.expectancy_r > 0
        no_overtrade = r.total_trades <= b.total_trades * 1.5
        immed_decreased = r.pct_immediate < p5b.pct_immediate - 5  # At least 5pp decrease

        all_pass = pf_pass and dd_pass and exp_pass and no_overtrade

        # Composite score for ranking (weighted)
        score = (
            r.profit_factor * 30
            + r.expectancy_r * 20
            + r.win_rate * 0.3
            - r.max_drawdown_pct * 5
            + r.sharpe_ratio * 10
            - (1 if r.pct_immediate > 60 else 0) * 5  # Penalize high immediate %
        )

        if score > best_score and exp_pass:
            best_score = score
            best_name = name

        status = "ACCEPT" if all_pass else "REJECT"
        flags = []
        if pf_pass: flags.append("PF✓")
        else: flags.append(f"PF✗({pf_vs_base:+.0f}%)")
        if dd_pass: flags.append("DD✓")
        else: flags.append(f"DD✗({r.max_drawdown_pct:.1f}%)")
        if exp_pass: flags.append("E✓")
        else: flags.append("E✗")
        if no_overtrade: flags.append("OT✓")
        else: flags.append("OT✗")
        if immed_decreased: flags.append("IM✓")
        else: flags.append(f"IM○({r.pct_immediate:.0f}%)")

        print(f"  [{status}] {name:<22s}: {' | '.join(flags)}")

    # ── RECOMMENDATION ───────────────────────────────────────────────────────
    print()
    print("=" * 100)
    print("  RECOMMENDATION")
    print("=" * 100)
    print()

    if best_name:
        r = results[best_name]
        pf_vs_base = ((r.profit_factor - b.profit_factor) / max(b.profit_factor, 0.001)) * 100
        pf_vs_p5b = ((r.profit_factor - p5b.profit_factor) / max(p5b.profit_factor, 0.001)) * 100

        print(f"  Best configuration: {best_name}")
        print(f"  LTF params: {CONFIGS[best_name]}")
        print()
        print(f"  vs Baseline:        PF {pf_vs_base:+.1f}%, Trades {r.total_trades - b.total_trades:+d}")
        print(f"  vs Phase5b Default: PF {pf_vs_p5b:+.1f}%, Trades {r.total_trades - p5b.total_trades:+d}")
        print()
        print(f"  Key metrics:")
        print(f"    Profit Factor:     {r.profit_factor:.3f}")
        print(f"    Win Rate:          {r.win_rate:.1f}%")
        print(f"    Expectancy (R):    {r.expectancy_r:+.4f}")
        print(f"    Max Drawdown:      {r.max_drawdown_pct:.2f}%")
        print(f"    Sharpe Ratio:      {r.sharpe_ratio:.3f}")
        print(f"    Conversion Rate:   {r.conversion_rate:.1f}%")
        print(f"    % Immediate:       {r.pct_immediate:.1f}%")
        print(f"    Total Return:      {r.total_return_pct:+.2f}%")
    else:
        print("  No configuration found with positive expectancy.")

    print()
    print(f"  Elapsed: {elapsed:.0f}s")
    print("=" * 100)

    # ── Save JSON ────────────────────────────────────────────────────────────
    os.makedirs("reports", exist_ok=True)
    save = {
        "phase": "6",
        "configs_tested": len(CONFIGS) + 1,  # +1 for baseline
        "best_config": best_name,
        "best_params": CONFIGS.get(best_name, {}),
        "results": {},
    }
    for name, r in results.items():
        save["results"][name] = {
            "total_trades": r.total_trades,
            "win_rate": r.win_rate,
            "profit_factor": r.profit_factor,
            "max_drawdown_pct": r.max_drawdown_pct,
            "expectancy_r": r.expectancy_r,
            "total_return_pct": r.total_return_pct,
            "sharpe_ratio": r.sharpe_ratio,
            "sl_rate": r.sl_rate,
            "tp_rate": r.tp_rate,
            "conversion_rate": r.conversion_rate,
            "expiry_rate": r.expiry_rate,
            "void_rate": r.void_rate,
            "pct_immediate": r.pct_immediate,
            "avg_confirm_delay": r.avg_confirm_delay,
        }

    with open("reports/phase6_comparison.json", "w") as f:
        json.dump(save, f, indent=2, default=str)
    print(f"  Results saved to reports/phase6_comparison.json")


if __name__ == "__main__":
    main()
