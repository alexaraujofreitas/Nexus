#!/usr/bin/env python3
"""
Phase 3b — Live Shadow Mode Report

Reads the Phase 2c shadow log (data/phase2c_shadow_log.jsonl) and produces
a structured report answering the 4 key questions:

  1. Is RB behaving in real time as backtest predicts?
  2. Is PBL continuing to drag in live shadow?
  3. Are portfolio controls working correctly?
  4. Is recent behavior closer to OOS or full-period?

Usage:
    python scripts/phase2c/phase3b_shadow_report.py              # full log
    python scripts/phase2c/phase3b_shadow_report.py --hours 24   # last 24h
    python scripts/phase2c/phase3b_shadow_report.py --json       # JSON output
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT_DIR))
os.chdir(ROOT_DIR)


# Phase 3a baseline reference (from shadow backtest)
BASELINE = {
    "full_period": {"pf": 1.2758, "wr": 0.564, "cagr": 48.5},
    "oos": {"pf": 1.6308, "wr": 0.634},
    "phase3a_full": {"pf": 1.2419, "wr": 0.594},
}


def _fmt_pf(v):
    if v is None:
        return "N/A"
    return f"{v:.4f}"


def _fmt_pct(v):
    if v is None:
        return "N/A"
    return f"{v * 100:.1f}%"


def _verdict(label, value, threshold, higher_is_better=True):
    """Return a colored verdict string."""
    if value is None:
        return f"  {label}: N/A (insufficient data)"
    if higher_is_better:
        ok = value >= threshold
    else:
        ok = value <= threshold
    mark = "PASS" if ok else "WATCH"
    return f"  {label}: {value:.4f} (threshold {threshold:.4f}) → {mark}"


def main():
    parser = argparse.ArgumentParser(description="Phase 3b Shadow Report")
    parser.add_argument("--hours", type=float, default=None,
                        help="Limit report to last N hours")
    parser.add_argument("--json", action="store_true",
                        help="Output raw JSON instead of formatted text")
    args = parser.parse_args()

    from core.scanning.shadow_tracker import shadow_tracker

    summary = shadow_tracker.get_summary(last_n_hours=args.hours)

    if args.json:
        print(json.dumps(summary, indent=2, default=str))
        return

    # ── Header ───────────────────────────────────────────────────────────
    period_str = f"Last {args.hours}h" if args.hours else "All time"
    print("=" * 72)
    print(f"  PHASE 3b — LIVE SHADOW MODE REPORT  ({period_str})")
    print("=" * 72)
    print(f"  Generated: {summary['period']['generated_at']}")
    print(f"  Signals:   {summary['period']['n_signals']}")
    print(f"  Outcomes:  {summary['period']['n_outcomes']}")
    print()

    # ── Q1: Is RB behaving as backtest predicts? ─────────────────────────
    print("─" * 72)
    print("  Q1: Is RB behaving in real time as backtest predicts?")
    print("─" * 72)
    rb = summary["model_contribution"].get("range_breakout", {})
    rb_sig = summary["signal_distribution"].get("range_breakout", {})
    ctl = summary["controls"]
    print(f"  RB signals generated:   {rb_sig.get('total', 0)}")
    print(f"  RB signals executed:    {rb_sig.get('executed', 0)}")
    print(f"  RB signals blocked:     {rb_sig.get('blocked', 0)}")
    print(f"  RB avg projected R:R:   {ctl.get('rb_avg_projected_rr', 0):.2f}")
    print(f"  RB outcomes:            n={rb.get('n', 0)}")
    if rb.get("n", 0) > 0:
        print(f"  RB PF:                  {_fmt_pf(rb['pf'])}  (backtest OOS: 1.3773)")
        print(f"  RB WR:                  {_fmt_pct(rb['wr'])}  (backtest OOS: 59.1%)")
        print(f"  RB Avg R:               {rb['avg_r']:+.4f}  (backtest OOS: +0.1411)")
        print(f"  RB Total PnL:           ${rb['total_pnl']:.2f}")
    else:
        print("  (No RB trade outcomes yet — need more live data)")
    print()

    # ── Q2: Is PBL continuing to drag? ───────────────────────────────────
    print("─" * 72)
    print("  Q2: Is PBL continuing to drag in live shadow?")
    print("─" * 72)
    pbl = summary["model_contribution"].get("pullback_long", {})
    slc = summary["model_contribution"].get("swing_low_continuation", {})
    pbl_sig = summary["signal_distribution"].get("pullback_long", {})
    slc_sig = summary["signal_distribution"].get("swing_low_continuation", {})
    print(f"  PBL signals:  {pbl_sig.get('total', 0)}  |  SLC signals:  {slc_sig.get('total', 0)}")
    print(f"  PBL outcomes: n={pbl.get('n', 0)}  |  SLC outcomes: n={slc.get('n', 0)}")
    if pbl.get("n", 0) > 0:
        print(f"  PBL PF:       {_fmt_pf(pbl['pf'])}  (backtest full: 0.857, OOS: 0.639)")
        print(f"  PBL WR:       {_fmt_pct(pbl['wr'])}  (backtest full: 47.3%)")
        print(f"  PBL Avg R:    {pbl['avg_r']:+.4f}")
        print(f"  PBL PnL:      ${pbl['total_pnl']:.2f}")
    if slc.get("n", 0) > 0:
        print(f"  SLC PF:       {_fmt_pf(slc['pf'])}  (backtest full: 1.367, OOS: 1.832)")
        print(f"  SLC WR:       {_fmt_pct(slc['wr'])}  (backtest full: 61.2%)")
        print(f"  SLC Avg R:    {slc['avg_r']:+.4f}")
        print(f"  SLC PnL:      ${slc['total_pnl']:.2f}")
    if pbl.get("n", 0) == 0 and slc.get("n", 0) == 0:
        print("  (No PBL/SLC outcomes yet — need more live data)")
    print()

    # ── Q3: Are portfolio controls working? ──────────────────────────────
    print("─" * 72)
    print("  Q3: Are portfolio controls behaving correctly?")
    print("─" * 72)
    print(f"  RB capital cap applied:    {ctl.get('rb_cap_applied', 0)} times")
    print(f"  RB position blocked:       {ctl.get('rb_pos_blocked', 0)} times")
    enh = summary["enhancement"]
    print(f"  Enhancement boosts:        {enh.get('n_boosted', 0)}")
    print(f"  Enhancement relaxations:   {enh.get('n_relaxed', 0)}")
    print(f"  Anti-amplification blocks: {enh.get('anti_amplification', 0)}")
    conf = summary["conflicts"]
    print(f"  PBL+RB same-bar conflicts: {conf.get('pbl_rb_same_bar', 0)}")
    print(f"  SLC+RB same-bar conflicts: {conf.get('slc_rb_same_bar', 0)}")
    print()

    # ── Q4: Recent behavior vs OOS vs full-period? ───────────────────────
    print("─" * 72)
    print("  Q4: Is recent behavior closer to OOS or full-period?")
    print("─" * 72)
    combined = summary["combined"]
    rpf = summary["rolling_pf"]
    print(f"  Combined PF:     {_fmt_pf(combined.get('pf'))}  "
          f"(baseline: {BASELINE['full_period']['pf']:.4f}, OOS: {BASELINE['oos']['pf']:.4f})")
    print(f"  Combined WR:     {_fmt_pct(combined.get('wr'))}  "
          f"(baseline: {BASELINE['full_period']['wr'] * 100:.1f}%, OOS: {BASELINE['oos']['wr'] * 100:.1f}%)")
    print(f"  Combined Avg R:  {combined.get('avg_r', 0):+.4f}")
    print(f"  Total PnL:       ${combined.get('total_pnl', 0):.2f}")
    print(f"  Rolling PF-20:   {_fmt_pf(rpf.get('rpf_20'))}")
    print(f"  Rolling PF-50:   {_fmt_pf(rpf.get('rpf_50'))}")
    print()

    # ── Per-Asset Breakdown ──────────────────────────────────────────────
    pa = summary.get("per_asset", {})
    if pa:
        print("─" * 72)
        print("  PER-ASSET BREAKDOWN")
        print("─" * 72)
        for sym, data in sorted(pa.items()):
            print(f"  {sym}: n={data['n']} PF={_fmt_pf(data['pf'])} WR={_fmt_pct(data['wr'])} "
                  f"PnL=${data['pnl']:.0f} "
                  f"(PBL={data['pbl_n']} SLC={data['slc_n']} RB={data['rb_n']})")
        print()

    # ── Verdicts ─────────────────────────────────────────────────────────
    print("─" * 72)
    print("  VERDICTS (Phase 3b success criteria)")
    print("─" * 72)
    n_out = combined.get("n", 0)
    if n_out < 10:
        print(f"  INSUFFICIENT DATA — only {n_out} outcomes. Need ≥10 for assessment.")
    else:
        print(_verdict("Combined PF ≥ baseline", combined.get("pf"), BASELINE["full_period"]["pf"]))
        print(_verdict("RB PF > 1.0 (positive)", rb.get("pf"), 1.0))
        print(_verdict("SLC PF > 1.2 (strong)", slc.get("pf"), 1.2))
        # PBL isolation check
        pbl_pf = pbl.get("pf")
        if pbl_pf is not None and pbl_pf < 1.0 and rb.get("pf") and rb["pf"] >= 1.0:
            print("  PBL drag confirmed, RB NOT the problem → EXPECTED")
        elif pbl_pf is not None and pbl_pf >= 1.0:
            print("  PBL recovering — monitor closely → ENCOURAGING")
    print()

    # Save JSON report
    report_dir = ROOT_DIR / "reports" / "phase2c"
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / "phase3b_shadow_report_latest.json"
    with open(report_path, "w") as f:
        json.dump(summary, f, indent=2, default=str)
    print(f"  JSON report saved: {report_path}")
    print("=" * 72)


if __name__ == "__main__":
    main()
