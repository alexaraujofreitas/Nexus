#!/usr/bin/env python
"""
NexusTrader — Testnet Validation Report Generator
===================================================
Reads the trade lifecycle JSONL log and test harness results,
then generates a comprehensive validation report.

Usage:
    python scripts/testnet/generate_report.py
    python scripts/testnet/generate_report.py --min-trades 50
    python scripts/testnet/generate_report.py --output reports/testnet_report.json

Reads from:
  - data/trade_lifecycle.jsonl  (lifecycle events)
  - data/test_harness_results.json  (edge-case scenario results)
  - data/testnet_preflight.json  (pre-flight check results)

Outputs:
  - JSON summary to stdout (or --output file)
  - Console-formatted table
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)


def _load_lifecycle_events(path: Path) -> list[dict]:
    """Load all events from the JSONL log."""
    events = []
    if not path.exists():
        return events
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return events


def _load_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    with open(path, "r") as f:
        return json.load(f)


def _compute_report(events: list[dict], harness_results: dict | None,
                     preflight: dict | None) -> dict:
    """Compute the full validation report from raw data."""

    # ── Event classification ──────────────────────────────────
    fills = [e for e in events if e.get("event") == "order_filled"]
    closes = [e for e in events if e.get("event") == "position_closed"]
    partials = [e for e in events if e.get("event") == "partial_close"]
    errors = [e for e in events if e.get("event") in ("order_failed", "order_timeout", "anomaly")]
    reconnects = [e for e in events if e.get("event") in ("exchange_disconnected", "exchange_reconnected")]
    gate_blocks = [e for e in events if e.get("event") == "pre_trade_check" and not e.get("passed")]
    gate_passes = [e for e in events if e.get("event") == "pre_trade_check" and e.get("passed")]
    candidates = [e for e in events if e.get("event") == "candidate_created"]

    # ── Trade metrics ─────────────────────────────────────────
    wins = [c for c in closes if c.get("pnl_usdt", 0) > 0]
    losses = [c for c in closes if c.get("pnl_usdt", 0) <= 0]
    total_pnl = sum(c.get("pnl_usdt", 0) for c in closes)
    gross_profit = sum(c.get("pnl_usdt", 0) for c in wins)
    gross_loss = abs(sum(c.get("pnl_usdt", 0) for c in losses))

    # ── Latency stats ─────────────────────────────────────────
    latencies = [f.get("latency_ms", 0) for f in fills if f.get("latency_ms")]
    lat_sorted = sorted(latencies) if latencies else [0]

    # ── Per-symbol breakdown ──────────────────────────────────
    symbol_stats = {}
    for c in closes:
        sym = c.get("symbol", "?")
        if sym not in symbol_stats:
            symbol_stats[sym] = {"wins": 0, "losses": 0, "pnl": 0.0, "count": 0}
        symbol_stats[sym]["count"] += 1
        symbol_stats[sym]["pnl"] = round(symbol_stats[sym]["pnl"] + c.get("pnl_usdt", 0), 4)
        if c.get("pnl_usdt", 0) > 0:
            symbol_stats[sym]["wins"] += 1
        else:
            symbol_stats[sym]["losses"] += 1

    # ── Exit reason breakdown ─────────────────────────────────
    exit_reasons = {}
    for c in closes:
        reason = c.get("exit_reason", "unknown")
        exit_reasons[reason] = exit_reasons.get(reason, 0) + 1

    # ── Slippage analysis ─────────────────────────────────────
    slippages = []
    for f in fills:
        slippage = f.get("slippage_bps")
        if slippage is not None:
            slippages.append(slippage)

    # ── Duration analysis ─────────────────────────────────────
    durations = [c.get("duration_s", 0) for c in closes if c.get("duration_s")]

    # ── Build report ──────────────────────────────────────────
    report = {
        "report_generated_at": datetime.now(timezone.utc).isoformat(),
        "data_source": "data/trade_lifecycle.jsonl",
        "total_events": len(events),

        # Trade summary
        "trade_summary": {
            "candidates_submitted": len(candidates),
            "orders_filled": len(fills),
            "positions_closed": len(closes),
            "partial_closes": len(partials),
            "errors": len(errors),
            "gate_blocks": len(gate_blocks),
            "gate_passes": len(gate_passes),
            "reconnects": len(reconnects),
        },

        # P&L
        "pnl": {
            "wins": len(wins),
            "losses": len(losses),
            "win_rate_pct": round(len(wins) / len(closes) * 100, 1) if closes else 0,
            "total_pnl_usdt": round(total_pnl, 2),
            "gross_profit_usdt": round(gross_profit, 2),
            "gross_loss_usdt": round(gross_loss, 2),
            "profit_factor": round(gross_profit / gross_loss, 4) if gross_loss > 0 else 999.0,
            "avg_pnl_per_trade_usdt": round(total_pnl / len(closes), 4) if closes else 0,
        },

        # Latency
        "latency": {
            "avg_ms": round(sum(latencies) / len(latencies), 1) if latencies else 0,
            "p50_ms": round(lat_sorted[len(lat_sorted) // 2], 1) if latencies else 0,
            "p99_ms": round(lat_sorted[int(len(lat_sorted) * 0.99)], 1) if latencies else 0,
            "max_ms": round(max(latencies), 1) if latencies else 0,
            "min_ms": round(min(latencies), 1) if latencies else 0,
            "sample_count": len(latencies),
        },

        # Slippage
        "slippage": {
            "avg_bps": round(sum(slippages) / len(slippages), 2) if slippages else 0,
            "max_bps": round(max(slippages), 2) if slippages else 0,
            "min_bps": round(min(slippages), 2) if slippages else 0,
            "sample_count": len(slippages),
        },

        # Duration
        "trade_duration": {
            "avg_s": round(sum(durations) / len(durations), 1) if durations else 0,
            "max_s": round(max(durations), 1) if durations else 0,
            "min_s": round(min(durations), 1) if durations else 0,
        },

        # Breakdowns
        "symbol_breakdown": symbol_stats,
        "exit_reasons": exit_reasons,

        # Error details (capped)
        "error_details": errors[:20],
        "reconnect_details": reconnects[:10],

        # Test harness results
        "test_harness": harness_results,

        # Pre-flight results
        "preflight": preflight,
    }

    # ── Validation verdict ────────────────────────────────────
    issues = []
    if len(closes) < 50:
        issues.append(f"Insufficient trades: {len(closes)} < 50 minimum")
    if len(errors) > 0:
        err_rate = len(errors) / max(len(fills), 1) * 100
        if err_rate > 5:
            issues.append(f"Error rate {err_rate:.1f}% exceeds 5% threshold")
    if latencies and max(latencies) > 5000:
        issues.append(f"Max latency {max(latencies):.0f}ms exceeds 5s threshold")
    if harness_results:
        hr_failed = harness_results.get("failed", 0)
        if hr_failed > 0:
            issues.append(f"Test harness: {hr_failed} scenario(s) failed")

    report["validation"] = {
        "issues": issues,
        "verdict": "PASS" if len(issues) == 0 else "NEEDS_REVIEW",
        "ready_for_live": len(issues) == 0,
    }

    return report


def _print_report(report: dict):
    """Pretty-print the report to console."""
    print("\n" + "=" * 70)
    print("  NEXUSTRADER TESTNET VALIDATION REPORT")
    print("=" * 70)

    ts = report["trade_summary"]
    print(f"\n  Total events:           {report['total_events']}")
    print(f"  Candidates submitted:   {ts['candidates_submitted']}")
    print(f"  Orders filled:          {ts['orders_filled']}")
    print(f"  Positions closed:       {ts['positions_closed']}")
    print(f"  Partial closes:         {ts['partial_closes']}")
    print(f"  Errors:                 {ts['errors']}")
    print(f"  Gate blocks:            {ts['gate_blocks']}")
    print(f"  Reconnects:             {ts['reconnects']}")

    pnl = report["pnl"]
    print(f"\n  ── P&L Summary ────────────────────────")
    print(f"  Wins / Losses:          {pnl['wins']} / {pnl['losses']}")
    print(f"  Win Rate:               {pnl['win_rate_pct']}%")
    print(f"  Total P&L:              ${pnl['total_pnl_usdt']:.2f}")
    print(f"  Profit Factor:          {pnl['profit_factor']}")
    print(f"  Avg P&L / trade:        ${pnl['avg_pnl_per_trade_usdt']:.4f}")

    lat = report["latency"]
    print(f"\n  ── Latency (order fill) ───────────────")
    print(f"  Avg:  {lat['avg_ms']:.1f} ms  |  P50: {lat['p50_ms']:.1f} ms")
    print(f"  P99:  {lat['p99_ms']:.1f} ms  |  Max: {lat['max_ms']:.1f} ms")

    sl = report["slippage"]
    if sl["sample_count"] > 0:
        print(f"\n  ── Slippage ──────────────────────────")
        print(f"  Avg:  {sl['avg_bps']:.2f} bps  |  Max: {sl['max_bps']:.2f} bps")

    if report["symbol_breakdown"]:
        print(f"\n  ── Per-Symbol Breakdown ──────────────")
        for sym, stats in report["symbol_breakdown"].items():
            print(f"    {sym:12s}  n={stats['count']:3d}  W={stats['wins']}  L={stats['losses']}  "
                  f"P&L=${stats['pnl']:.2f}")

    if report["exit_reasons"]:
        print(f"\n  ── Exit Reasons ─────────────────────")
        for reason, count in report["exit_reasons"].items():
            print(f"    {reason:25s}  {count}")

    hr = report.get("test_harness")
    if hr:
        print(f"\n  ── Test Harness ─────────────────────")
        print(f"  Passed: {hr.get('passed', '?')}/{hr.get('total', '?')}, "
              f"Failed: {hr.get('failed', '?')}")
        for r in hr.get("results", []):
            icon = "PASS" if r.get("passed") else "FAIL"
            print(f"    [{icon}] Scenario {r.get('scenario')}: {r.get('name')}")

    v = report["validation"]
    print(f"\n  {'=' * 50}")
    print(f"  VERDICT: {v['verdict']}")
    if v["issues"]:
        for issue in v["issues"]:
            print(f"    - {issue}")
    else:
        print(f"    All checks passed. Ready for live deployment.")
    print(f"  {'=' * 50}")


def main():
    parser = argparse.ArgumentParser(description="NexusTrader Testnet Validation Report")
    parser.add_argument("--min-trades", type=int, default=50,
                        help="Minimum trades required for PASS verdict (default: 50)")
    parser.add_argument("--output", type=str, default=None,
                        help="Output JSON path (default: data/testnet_validation_report.json)")
    args = parser.parse_args()

    data_dir = Path(ROOT) / "data"
    lifecycle_path = data_dir / "trade_lifecycle.jsonl"
    harness_path = data_dir / "test_harness_results.json"
    preflight_path = data_dir / "testnet_preflight.json"

    events = _load_lifecycle_events(lifecycle_path)
    harness = _load_json(harness_path)
    preflight = _load_json(preflight_path)

    if not events:
        print("WARNING: No lifecycle events found. Run testnet trades first.")
        print(f"  Expected: {lifecycle_path}")

    report = _compute_report(events, harness, preflight)
    _print_report(report)

    # Save
    out_path = Path(args.output) if args.output else data_dir / "testnet_validation_report.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(report, f, indent=2, default=str)
    print(f"\nFull report saved to: {out_path}")


if __name__ == "__main__":
    main()
