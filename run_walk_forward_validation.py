#!/usr/bin/env python3
"""
NexusTrader — Walk-Forward Regime-Segmented Validation Runner
=============================================================

PURPOSE
-------
Run a rigorous out-of-sample walk-forward validation to determine
whether NexusTrader demonstrates PERSISTENT EDGE across different
market regimes, or whether profitability depends on a favorable
historical period.

This script:
  1.  Instantiates the RegimeSegmentedWalkForwardValidator
  2.  Generates synthetic regime-labeled OHLCV data for 5 symbols
  3.  Runs the walk-forward validation (no future data leakage)
  4.  Computes regime/asset/model/score-bucket metrics
  5.  Generates an HTML report with embedded matplotlib charts
  6.  Saves all trade records to CSV
  7.  Prints a structured 10-section summary to console

OUTPUT
------
  reports/walk_forward/walk_forward_report.html   — full HTML report
  reports/walk_forward/trades.csv                 — all OOS trade records
  reports/walk_forward/*.png                      — individual charts

USAGE
-----
  python run_walk_forward_validation.py
  python run_walk_forward_validation.py --output-dir custom/path

This script does NOT modify any trading architecture, model logic,
or configuration.  It is strictly an evaluation framework.
"""
from __future__ import annotations

import argparse
import csv
import logging
import os
import sys
import time
from datetime import datetime, timezone

# ── Add project root to path ───────────────────────────────────────────────
_ROOT = os.path.dirname(os.path.abspath(__file__))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# ── Configure logging ──────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("walk_forward_runner")


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _print_banner(text: str):
    bar = "═" * 62
    print(f"\n{bar}")
    print(f"  {text}")
    print(bar)


def _print_section(n: int, title: str):
    print(f"\n{'─'*62}")
    print(f"  SECTION {n}  —  {title}")
    print(f"{'─'*62}")


def _fmt_r(v: float) -> str:
    return f"{v:+.3f}R"


def _fmt_pct(v: float) -> str:
    return f"{v:.1f}%"


def _verdict_label(verdict: str) -> str:
    return {
        "PERSISTENT_EDGE":    "✅  PERSISTENT EDGE",
        "REGIME_DEPENDENT":   "⚠️   REGIME DEPENDENT",
        "INSUFFICIENT_DATA":  "❓  INSUFFICIENT DATA",
    }.get(verdict, verdict)


def _save_trades_csv(trades: list[dict], path: str):
    if not trades:
        logger.warning("No trades to save")
        return
    fieldnames = [
        "symbol", "wf_window", "entry_time", "exit_time",
        "direction", "regime_at_entry",
        "entry_price", "exit_price", "stop_price", "tp_price",
        "quantity", "size_usdt",
        "pnl", "pnl_pct",
        "realized_r_multiple", "expected_rr",
        "exit_reason", "duration_hours",
        "confluence_score",
        "models_fired", "slippage_pct",
    ]
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for t in trades:
            row = dict(t)
            # Normalise list fields
            models = row.get("models_fired") or row.get("models_triggered") or []
            row["models_fired"] = "|".join(str(m) for m in models)
            writer.writerow(row)
    logger.info("Trades CSV saved → %s  (%d rows)", path, len(trades))


# ─────────────────────────────────────────────────────────────────────────────
# Console report — 10 sections
# ─────────────────────────────────────────────────────────────────────────────

def print_console_report(result):  # noqa: C901
    from core.validation.walk_forward_regime_validator import (
        compute_metrics,
        assess_edge_persistence,
    )

    gm = result.global_metrics
    verdict = result.edge_verdict
    explanation = result.edge_explanation

    # ── Section 1 — Framework ─────────────────────────────────────────────
    _print_section(1, "Walk-Forward Validation Framework")
    cfg = result.config
    print(f"  Symbols        : {', '.join(cfg.symbols)}")
    print(f"  Timeframe      : {cfg.timeframe}")
    print(f"  Calibration    : {cfg.calibration_bars} bars (indicator warm-up)")
    print(f"  Test window    : {cfg.test_bars} bars (out-of-sample)")
    print(f"  Step           : {cfg.step_bars} bars (non-overlapping)")
    print(f"  Initial equity : ${cfg.initial_capital:,.0f} USDT")
    print(f"  Fee            : {cfg.fee_pct}% per side")
    print(f"  Slippage       : {cfg.slippage_pct}% + {cfg.spread_pct}% spread")
    print(f"  Min score      : {cfg.min_confluence_score}")
    print(f"\n  Walk-forward windows generated: {result.window_count}")
    print(f"\n  {'Symbol':<12} {'Win':<4} {'OOS Trades':<12} {'Expectancy':<12} {'PF'}")
    print(f"  {'-'*50}")
    for sym, sr in result.symbol_results.items():
        n = sr.get("n_trades", 0)
        if n > 0:
            m = compute_metrics(sr.get("trades", []))
            print(f"  {sym:<12} {sr.get('n_wins', '?'):<4} {n:<12} {_fmt_r(m['expectancy_r']):<12} {m['profit_factor']:.2f}")
        else:
            print(f"  {sym:<12} {'—':<4} {0:<12} {'—':<12} {'—'}")

    # ── Section 2 — Regime Labeling ───────────────────────────────────────
    _print_section(2, "Regime Labeling")
    regime_counts: dict[str, int] = {}
    for t in result.all_trades:
        r = t.get("regime_at_entry") or t.get("regime") or "unknown"
        regime_counts[r] = regime_counts.get(r, 0) + 1
    print(f"  Regime distribution across {len(result.all_trades)} OOS trades:")
    for regime, cnt in sorted(regime_counts.items(), key=lambda x: -x[1]):
        pct = cnt / max(len(result.all_trades), 1) * 100
        bar = "█" * int(pct / 4)
        print(f"  {regime:<18}  {cnt:4d} trades  ({pct:5.1f}%)  {bar}")

    # ── Section 3 — Trade Dataset overview ───────────────────────────────
    _print_section(3, "Trade Outcome Dataset")
    print(f"  Total OOS trades    : {len(result.all_trades)}")
    directions = {"long": 0, "short": 0}
    exits = {"take_profit": 0, "stop_loss": 0, "end_of_data": 0}
    for t in result.all_trades:
        d = t.get("direction", "long")
        directions[d] = directions.get(d, 0) + 1
        e = t.get("exit_reason", "end_of_data")
        exits[e]  = exits.get(e,  0) + 1
    print(f"  Long / Short        : {directions.get('long', 0)} / {directions.get('short', 0)}")
    print(f"  TP / SL / EOD       : {exits.get('take_profit', 0)} / "
          f"{exits.get('stop_loss', 0)} / {exits.get('end_of_data', 0)}")
    if result.all_trades:
        durations = [t.get("duration_hours", 0) for t in result.all_trades]
        import statistics
        print(f"  Avg hold time       : {statistics.mean(durations):.1f}h")
        scores = [t.get("confluence_score", 0) for t in result.all_trades]
        print(f"  Avg confluence score: {statistics.mean(scores):.3f}")

    # ── Section 4 — Global metrics ────────────────────────────────────────
    _print_section(4, "Global Performance Metrics")
    print(f"  Trades             : {gm['total_trades']}")
    print(f"  Win rate           : {_fmt_pct(gm['win_rate'])}")
    print(f"  Expectancy         : {_fmt_r(gm['expectancy_r'])}")
    print(f"  Profit Factor      : {gm['profit_factor']:.2f}")
    print(f"  Avg Win R          : {gm['avg_win_r']:.3f}R")
    print(f"  Avg Loss R         : {gm['avg_loss_r']:.3f}R")
    print(f"  Max Drawdown (R)   : {gm['drawdown_r']:.2f}R")
    print(f"  Total P&L          : ${gm['total_pnl_usdt']:+,.2f} USDT")
    print(f"  Gross win          : ${gm['gross_win_usdt']:+,.2f} USDT")
    print(f"  Gross loss         : $−{gm['gross_loss_usdt']:,.2f} USDT")

    # PFS
    pf_hist = result.rolling_20_pf_history
    if len(pf_hist) >= 5:
        snap = pf_hist[-10:] if len(pf_hist) >= 10 else pf_hist
        finite = [min(v, 5.0) for v in snap]
        mean_pf = statistics.mean(finite)
        std_pf  = statistics.stdev(finite) if len(finite) >= 2 else 0.0
        pfs_cv  = std_pf / mean_pf if mean_pf > 0 else 1.0
        pfs_score = max(0, min(100, round(100 * (1 - pfs_cv))))
        pfs_label = "Stable" if pfs_score >= 85 else ("Moderate" if pfs_score >= 60 else "Unstable")
        print(f"  PF Stability Score : {pfs_score}/100 ({pfs_label})")

    # ── Section 5 — Regime table ──────────────────────────────────────────
    _print_section(5, "Regime Performance Table")
    hdr = f"  {'Regime':<18}  {'Trades':>6}  {'Expect':>8}  {'PF':>5}  {'Max DD':>7}  {'WR':>6}  Edge?"
    print(hdr)
    print("  " + "─" * (len(hdr) - 2))
    for regime, m in sorted(result.by_regime.items()):
        n = m.get("total_trades", 0)
        if n == 0:
            continue
        edge = "✅ YES" if m["expectancy_r"] > 0 else "❌ NO"
        print(f"  {regime:<18}  {n:>6}  {_fmt_r(m['expectancy_r']):>8}  "
              f"{m['profit_factor']:>5.2f}  {m['drawdown_r']:>6.2f}R  "
              f"{_fmt_pct(m['win_rate']):>6}  {edge}")

    # ── Section 6 — Asset & model attribution ─────────────────────────────
    _print_section(6, "Asset Attribution")
    hdr = f"  {'Asset':<14}  {'Trades':>6}  {'Expect':>8}  {'PF':>5}  {'WR':>6}  {'P&L':>10}"
    print(hdr)
    print("  " + "─" * (len(hdr) - 2))
    for asset, m in sorted(result.by_asset.items(),
                            key=lambda kv: kv[1].get("total_trades", 0), reverse=True):
        n = m.get("total_trades", 0)
        if n == 0:
            continue
        print(f"  {asset:<14}  {n:>6}  {_fmt_r(m['expectancy_r']):>8}  "
              f"{m['profit_factor']:>5.2f}  {_fmt_pct(m['win_rate']):>6}  "
              f"${m['total_pnl_usdt']:>+9.2f}")

    _print_section(6, "Model Attribution")
    hdr = f"  {'Model':<22}  {'Trades':>6}  {'Expect':>8}  {'PF':>5}  {'WR':>6}  {'P&L':>10}"
    print(hdr)
    print("  " + "─" * (len(hdr) - 2))
    for model, m in sorted(result.by_model.items(),
                            key=lambda kv: kv[1].get("expectancy_r", 0), reverse=True):
        n = m.get("total_trades", 0)
        if n == 0:
            continue
        print(f"  {model:<22}  {n:>6}  {_fmt_r(m['expectancy_r']):>8}  "
              f"{m['profit_factor']:>5.2f}  {_fmt_pct(m['win_rate']):>6}  "
              f"${m['total_pnl_usdt']:>+9.2f}")

    # ── Section 7 — Rolling stability ─────────────────────────────────────
    _print_section(7, "Rolling Stability Metrics")
    roll20 = result.rolling_20_exp_history
    roll_pf = result.rolling_20_pf_history
    roll_dd = result.rolling_dd_r_history
    if roll20:
        pct_pos = sum(1 for v in roll20 if v > 0) / len(roll20) * 100
        print(f"  Rolling-20 Expectancy :")
        print(f"    Observations        : {len(roll20)}")
        print(f"    % positive windows  : {pct_pos:.1f}%")
        if len(roll20) >= 2:
            print(f"    Min / Max           : {min(roll20):.3f}R / {max(roll20):.3f}R")
            print(f"    Final value         : {roll20[-1]:.3f}R")
    if roll_pf:
        pf20_clipped = [min(v, 5.0) for v in roll_pf]
        print(f"\n  Rolling-20 Profit Factor:")
        print(f"    Observations        : {len(roll_pf)}")
        print(f"    % above 1.0         : {sum(1 for v in roll_pf if v > 1.0) / len(roll_pf) * 100:.1f}%")
        print(f"    Final value         : {min(roll_pf[-1], 99.0):.2f}")
    if roll_dd:
        print(f"\n  Rolling Max Drawdown (R):")
        print(f"    Max observed DD     : {max(roll_dd):.2f}R")
        print(f"    Final value         : {roll_dd[-1]:.2f}R")

    # ── Section 8 — Edge persistence ──────────────────────────────────────
    _print_section(8, "Edge Persistence Analysis")
    print(f"  {'─' * 50}")
    for line in explanation.split("  "):
        if line.strip():
            print(f"  {line.strip()}")
    print(f"\n  Score bucket calibration:")
    print(f"  {'Bucket':<12}  {'Trades':>6}  {'Expect':>8}  {'WR':>6}")
    print(f"  {'─'*38}")
    for bucket, m in sorted(result.by_score_bucket.items()):
        n = m.get("total_trades", 0)
        if n == 0:
            continue
        print(f"  {bucket:<12}  {n:>6}  {_fmt_r(m['expectancy_r']):>8}  {_fmt_pct(m['win_rate']):>6}")

    # ── Section 9 — Visualizations ────────────────────────────────────────
    _print_section(9, "Visualizations")
    print("  Charts saved to: reports/walk_forward/")
    print("    equity_curve.png         — equity curve across walk-forward windows")
    print("    cumulative_r.png         — cumulative R progression")
    print("    rolling_expectancy.png   — rolling-20 expectancy")
    print("    rolling_pf.png           — rolling-20/40 profit factor")
    print("    drawdown_r.png           — rolling drawdown in R")
    print("    regime_bar.png           — expectancy by regime")
    print("    asset_bar.png            — expectancy by asset")
    print("    model_bar.png            — expectancy by model")

    # ── Section 10 — Final verdict ────────────────────────────────────────
    _print_section(10, "Final Evaluation")
    print()
    print(f"  {_verdict_label(verdict)}")
    print()
    if "CONCLUSION:" in explanation:
        print(f"  {explanation.split('CONCLUSION:')[1].strip()}")
    print()


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="NexusTrader Walk-Forward Regime-Segmented Validation"
    )
    parser.add_argument(
        "--output-dir", default="reports/walk_forward",
        help="Directory for HTML report, charts, and CSV (default: reports/walk_forward)"
    )
    parser.add_argument(
        "--no-report", action="store_true",
        help="Skip HTML report generation (useful for quick console-only runs)"
    )
    args = parser.parse_args()

    output_dir = os.path.join(_ROOT, args.output_dir)

    _print_banner("NexusTrader — Walk-Forward Regime-Segmented Validation")
    print(f"  Output directory : {output_dir}")
    print(f"  Started          : {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")

    # ── Import validator ───────────────────────────────────────────────────
    try:
        from core.validation.walk_forward_regime_validator import (
            RegimeSegmentedWalkForwardValidator,
            WalkForwardConfig,
        )
    except ImportError as exc:
        logger.error("Cannot import validator: %s", exc)
        sys.exit(1)

    # ── Configure ─────────────────────────────────────────────────────────
    cfg = WalkForwardConfig(
        symbols            = ["BTC/USDT", "ETH/USDT", "BNB/USDT", "SOL/USDT", "XRP/USDT"],
        timeframe          = "4h",
        calibration_bars   = 400,
        test_bars          = 200,
        step_bars          = 200,
        initial_capital    = 10_000.0,
        fee_pct            = 0.10,
        slippage_pct       = 0.05,
        spread_pct         = 0.05,
        min_confluence_score = 0.45,
        warmup_bars        = 100,
    )

    validator = RegimeSegmentedWalkForwardValidator(config=cfg)

    # ── Run ───────────────────────────────────────────────────────────────
    t0 = time.time()
    print("\nRunning validation… (this may take 1–3 minutes)\n")

    def progress(msg: str):
        print(f"  {msg}")

    result = validator.run(ohlcv_data=None, progress_cb=progress)

    elapsed = time.time() - t0
    print(f"\n  Validation completed in {elapsed:.1f}s")

    # ── Console report ────────────────────────────────────────────────────
    print_console_report(result)

    # ── Save trades CSV ───────────────────────────────────────────────────
    csv_path = os.path.join(output_dir, "trades.csv")
    _save_trades_csv(result.all_trades, csv_path)

    # ── HTML report + charts ──────────────────────────────────────────────
    if not args.no_report:
        try:
            from core.validation.report_generator import WalkForwardReportGenerator
            gen = WalkForwardReportGenerator()
            html_path = gen.generate(result, output_dir=output_dir)
            print(f"\n  HTML report saved  → {html_path}")
        except Exception as exc:
            logger.warning("HTML report generation failed: %s", exc)
            print(f"  (HTML report skipped — {exc})")

    # ── Final summary ─────────────────────────────────────────────────────
    _print_banner(f"FINAL VERDICT: {result.edge_verdict}")
    print(f"  Trades: {len(result.all_trades)}  |  "
          f"E[R]: {result.global_metrics.get('expectancy_r', 0):+.3f}R  |  "
          f"PF: {result.global_metrics.get('profit_factor', 0):.2f}  |  "
          f"DD: {result.global_metrics.get('drawdown_r', 0):.2f}R")
    print()

    return result


if __name__ == "__main__":
    main()
