#!/usr/bin/env python3
"""
research/harness/run_research.py
================================
Standalone PBL/SLC optimization runner. Run this directly on your local machine.

REQUIRES (from NexusTrader root):
    pip install numpy pandas pyarrow tqdm

GPU NOTE: The simulation loop is sequential by design (positions depend on prior
bars). GPU acceleration does NOT help here — all speedup comes from:
  (a) precomputing ALL indicators once per process (fast_backtest.FastBacktestEngine)
  (b) multiprocessing.Pool with one engine per worker process

Typical throughput: ~0.25-0.40s per trial (varies by CPU speed and data size).
  - 2500 coarse trials on 8 cores ≈ 80-130 seconds
  - Full 4-year IS period (2022-03-22 → 2025-03-21)

STAGES
------
  baseline     — Reproduce PBL PF=0.8995 / SLC PF=1.5455 / combined PF=1.2682
  coarse_pbl   — Stage B: sweep core PBL params (~2500 trials, pbl_only mode)
  slc_sweep    — Secondary SLC sweep (~500 trials, slc_only mode)
  confirmation — Stage D: 10 HTF confirmation variants, combined mode
                 (uses --top-params-file to load best PBL params from Stage B)
  walkforward  — Stage E: WF validation on top-N candidates
                 (uses --top-params-file)
  holdout      — Stage E final: run on 2025-03-22→2026-03-21 NEVER touch during search
                 (uses --top-params-file; only run after WF validation)
  stress       — Stage F: fee sensitivity at 0%, 0.04%, 0.08%, 0.15%
                 (uses --top-params-file)

USAGE
-----
  # From NexusTrader root directory:

  # Stage A — baseline reproduction (1 trial, fast)
  python research/harness/run_research.py --stage baseline

  # Stage B — coarse PBL sweep
  python research/harness/run_research.py --stage coarse_pbl --workers 10

  # Stage D — confirmation variants (after Stage B, best params auto-loaded from CSV)
  python research/harness/run_research.py --stage confirmation --workers 10

  # Stage E — walk-forward on top-5 from Stage B
  python research/harness/run_research.py --stage walkforward --top-n 5

  # Stage E holdout — ONLY after full WF; runs on held-out 2025-2026 year
  python research/harness/run_research.py --stage holdout --top-n 5

  # Stage F — fee stress
  python research/harness/run_research.py --stage stress

  # Custom worker count (default: os.cpu_count() - 1)
  python research/harness/run_research.py --stage coarse_pbl --workers 16

OUTPUT FILES
------------
  research/results/trials_pbl_coarse.csv        — all coarse PBL trials
  research/results/trials_slc_sweep.csv         — all SLC trials
  research/results/trials_confirmation.csv      — confirmation variant trials
  research/results/trials_walkforward.csv       — walk-forward window results
  research/results/trials_holdout.csv           — holdout results (final only)
  research/results/trials_stress.csv            — fee sensitivity results
  research/leaderboards/top10_coarse_pbl.json   — top-10 snapshot after coarse
  research/leaderboards/top10_confirmation.json — top-10 confirmation variants
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import multiprocessing as mp
import os
import signal
import sys
import time
from itertools import product
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

# ── Path setup ────────────────────────────────────────────────────────────────
ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

# Silence all library chatter — only our prints matter
logging.basicConfig(level=logging.CRITICAL)
for lib in ["core", "torch", "numba", "matplotlib", "PIL"]:
    logging.getLogger(lib).setLevel(logging.CRITICAL)

# ── Import fast engine ────────────────────────────────────────────────────────
try:
    from research.harness.fast_backtest import worker_run, FastBacktestEngine, SYMBOLS
except ImportError as e:
    print(f"[ERROR] Cannot import fast_backtest: {e}")
    print("        Make sure you run from the NexusTrader root directory.")
    sys.exit(1)

# ── Output directories ────────────────────────────────────────────────────────
RESULTS_DIR  = ROOT / "research" / "results"
LEADER_DIR   = ROOT / "research" / "leaderboards"
RESULTS_DIR.mkdir(parents=True, exist_ok=True)
LEADER_DIR.mkdir(parents=True, exist_ok=True)

# ── Date splits ───────────────────────────────────────────────────────────────
IS_START  = "2022-03-22"
IS_END    = "2025-03-21"
OOS_START = "2025-03-22"
OOS_END   = "2026-03-21"   # NEVER used during search — holdout only

WF_WINDOWS = [
    ("2022-03-22", "2023-03-21", "2023-03-22", "2023-06-21"),
    ("2022-06-22", "2023-06-21", "2023-06-22", "2023-09-21"),
    ("2022-09-22", "2023-09-21", "2023-09-22", "2023-12-21"),
    ("2022-12-22", "2023-12-21", "2023-12-22", "2024-03-21"),
]

# ── Baseline params ───────────────────────────────────────────────────────────
BASELINE_PBL = {
    "ema_prox_atr_mult":      0.50,
    "rsi_min":                40.0,
    "sl_atr_mult":            2.50,
    "tp_atr_mult":            3.00,
    "body_ratio_max":         None,
    "htf_ema_fast":           20,
    "htf_ema_slow":           50,
    "htf_adx_min":            None,
    "htf_price_above_ema200": False,
    "slc_adx_min":            28.0,
    "slc_swing_bars":         10,
    "slc_sl_atr_mult":        2.50,
    "slc_tp_atr_mult":        2.00,
}

# ── HTF confirmation variants ─────────────────────────────────────────────────
CONFIRMATION_VARIANTS = [
    {"id": "V0",  "label": "Baseline: EMA20>50",            "htf_ema_fast": 20, "htf_ema_slow": 50,  "htf_adx_min": None, "htf_price_above_ema200": False},
    {"id": "V1",  "label": "EMA9>21",                       "htf_ema_fast":  9, "htf_ema_slow": 21,  "htf_adx_min": None, "htf_price_above_ema200": False},
    {"id": "V2",  "label": "EMA20>100",                     "htf_ema_fast": 20, "htf_ema_slow": 100, "htf_adx_min": None, "htf_price_above_ema200": False},
    {"id": "V3",  "label": "EMA50>200 major trend",         "htf_ema_fast": 50, "htf_ema_slow": 200, "htf_adx_min": None, "htf_price_above_ema200": False},
    {"id": "V4",  "label": "V0 + ADX≥20",                  "htf_ema_fast": 20, "htf_ema_slow": 50,  "htf_adx_min": 20,   "htf_price_above_ema200": False},
    {"id": "V5",  "label": "V0 + ADX≥25",                  "htf_ema_fast": 20, "htf_ema_slow": 50,  "htf_adx_min": 25,   "htf_price_above_ema200": False},
    {"id": "V6",  "label": "V0 + price>EMA200",             "htf_ema_fast": 20, "htf_ema_slow": 50,  "htf_adx_min": None, "htf_price_above_ema200": True},
    {"id": "V7",  "label": "V0 + ADX≥20 + EMA200",         "htf_ema_fast": 20, "htf_ema_slow": 50,  "htf_adx_min": 20,   "htf_price_above_ema200": True},
    {"id": "V8",  "label": "V1 + ADX≥20",                  "htf_ema_fast":  9, "htf_ema_slow": 21,  "htf_adx_min": 20,   "htf_price_above_ema200": False},
    {"id": "V9",  "label": "V3 + ADX≥25 major trend",      "htf_ema_fast": 50, "htf_ema_slow": 200, "htf_adx_min": 25,   "htf_price_above_ema200": False},
]

FEE_SCENARIOS = [0.0, 0.0004, 0.0008, 0.0015]


# ═══════════════════════════════════════════════════════════════════════════════
#  Grid builders
# ═══════════════════════════════════════════════════════════════════════════════

def build_coarse_pbl_grid() -> list[dict]:
    """
    Stage B coarse PBL sweep.
    Baseline HTF settings (no EMA fast/slow variation — that's Stage D).
    Pruning: sl_mult < tp_mult enforced (valid R:R for longs).
    """
    prox_vals   = [0.20, 0.35, 0.50, 0.65, 0.80]
    rsi_vals    = [35.0, 40.0, 45.0, 50.0, 55.0]
    sl_vals     = [1.50, 2.00, 2.50, 3.00, 3.50]
    tp_vals     = [2.00, 2.50, 3.00, 3.50, 4.00, 4.50, 5.00]
    body_vals   = [None, 0.30, 0.40, 0.50]

    grid = []
    for prox, rsi, sl, tp, body in product(prox_vals, rsi_vals, sl_vals, tp_vals, body_vals):
        if sl >= tp:        # Prune: SL must be smaller than TP for valid long R:R
            continue
        grid.append({
            "ema_prox_atr_mult":      prox,
            "rsi_min":                rsi,
            "sl_atr_mult":            sl,
            "tp_atr_mult":            tp,
            "body_ratio_max":         body,
            "htf_ema_fast":           20,   # baseline HTF
            "htf_ema_slow":           50,
            "htf_adx_min":            None,
            "htf_price_above_ema200": False,
            "slc_adx_min":            28.0,  # baseline SLC (not swept in pbl_only mode)
            "slc_swing_bars":         10,
            "slc_sl_atr_mult":        2.50,
            "slc_tp_atr_mult":        2.00,
        })
    return grid


def build_slc_sweep_grid() -> list[dict]:
    """Secondary SLC parameter sweep. SLC PF=1.5455 already good; keep floor ≥1.40."""
    adx_vals    = [22.0, 25.0, 28.0, 31.0, 34.0]
    swing_vals  = [7, 10, 14, 20]
    sl_vals     = [1.50, 2.00, 2.50, 3.00, 3.50]
    tp_vals     = [1.50, 2.00, 2.50, 3.00, 3.50]

    grid = []
    for adx, swing, sl, tp in product(adx_vals, swing_vals, sl_vals, tp_vals):
        if sl >= tp:    # Prune: SL < TP for valid short R:R (tp is below entry for shorts)
            continue
        grid.append({
            **BASELINE_PBL,
            "slc_adx_min":     adx,
            "slc_swing_bars":  swing,
            "slc_sl_atr_mult": sl,
            "slc_tp_atr_mult": tp,
        })
    return grid


def build_confirmation_grid(best_pbl_params: dict) -> list[dict]:
    """
    Stage D: 10 HTF confirmation variants with the best PBL params from Stage B.
    Runs in combined (PBL + SLC) mode with baseline SLC params.
    """
    grid = []
    for v in CONFIRMATION_VARIANTS:
        p = {
            **best_pbl_params,
            "htf_ema_fast":           v["htf_ema_fast"],
            "htf_ema_slow":           v["htf_ema_slow"],
            "htf_adx_min":            v["htf_adx_min"],
            "htf_price_above_ema200": v["htf_price_above_ema200"],
            "_variant_id":            v["id"],
            "_variant_label":         v["label"],
        }
        grid.append(p)
    return grid


def _load_top_n_from_csv(csv_path: Path, n: int = 5, sort_col: str = "pf") -> list[dict]:
    """Load top-N param sets from a results CSV, sorted by sort_col descending."""
    if not csv_path.exists():
        raise FileNotFoundError(f"Results CSV not found: {csv_path}")
    df = pd.read_csv(csv_path)
    df = df[df["status"] == "ok"].sort_values(sort_col, ascending=False).head(n)
    results = []
    for _, row in df.iterrows():
        # Parse nested 'params' column if present, else reconstruct from flat columns
        if "params" in df.columns and isinstance(row.get("params"), str):
            try:
                params = json.loads(row["params"].replace("'", '"').replace("None", "null").replace("True", "true").replace("False", "false"))
                results.append(params)
                continue
            except Exception:
                pass
        # Fallback: reconstruct from flat columns
        param_keys = [
            "ema_prox_atr_mult", "rsi_min", "sl_atr_mult", "tp_atr_mult",
            "body_ratio_max", "htf_ema_fast", "htf_ema_slow", "htf_adx_min",
            "htf_price_above_ema200", "slc_adx_min", "slc_swing_bars",
            "slc_sl_atr_mult", "slc_tp_atr_mult",
        ]
        p = {}
        for k in param_keys:
            if k in row:
                val = row[k]
                if pd.isna(val):
                    p[k] = None
                elif k in ("htf_price_above_ema200",):
                    p[k] = bool(val)
                elif k in ("htf_ema_fast", "htf_ema_slow", "slc_swing_bars"):
                    p[k] = int(val)
                else:
                    try:
                        p[k] = float(val)
                    except (ValueError, TypeError):
                        p[k] = val
        # Fill missing with baseline
        for k, v in BASELINE_PBL.items():
            p.setdefault(k, v)
        results.append(p)
    return results


# ═══════════════════════════════════════════════════════════════════════════════
#  CSV writer
# ═══════════════════════════════════════════════════════════════════════════════

_CSV_FIELDNAMES = [
    "trial_id", "status", "error", "stage", "mode", "cost_per_side",
    "date_start", "date_end",
    "pf", "wr", "cagr", "maxdd", "n_trades", "n_wins", "n_losses",
    "gross_profit", "gross_loss", "final_capital",
    # PBL params
    "ema_prox_atr_mult", "rsi_min", "sl_atr_mult", "tp_atr_mult",
    "body_ratio_max", "htf_ema_fast", "htf_ema_slow", "htf_adx_min",
    "htf_price_above_ema200",
    # SLC params
    "slc_adx_min", "slc_swing_bars", "slc_sl_atr_mult", "slc_tp_atr_mult",
    # Extras
    "_variant_id", "_variant_label", "_wf_window",
]


def _open_csv(path: Path, append: bool = False) -> tuple:
    mode = "a" if (append and path.exists()) else "w"
    fh   = open(path, mode, newline="", encoding="utf-8")
    writer = csv.DictWriter(fh, fieldnames=_CSV_FIELDNAMES, extrasaction="ignore")
    if mode == "w":
        writer.writeheader()
    return fh, writer


def _flatten_result(r: dict, stage: str, extra: dict = None) -> dict:
    """Flatten params dict into top-level row for CSV."""
    row = {k: r.get(k) for k in _CSV_FIELDNAMES}
    row["stage"] = stage
    # Extract nested params if present
    p = r.get("params", {}) or {}
    for k in [
        "ema_prox_atr_mult", "rsi_min", "sl_atr_mult", "tp_atr_mult",
        "body_ratio_max", "htf_ema_fast", "htf_ema_slow", "htf_adx_min",
        "htf_price_above_ema200", "slc_adx_min", "slc_swing_bars",
        "slc_sl_atr_mult", "slc_tp_atr_mult",
        "_variant_id", "_variant_label",
    ]:
        row[k] = p.get(k, row.get(k))
    if extra:
        row.update(extra)
    return row


# ═══════════════════════════════════════════════════════════════════════════════
#  Progress display
# ═══════════════════════════════════════════════════════════════════════════════

def _fmt_params(p: dict) -> str:
    """Compact param string for leaderboard display."""
    body = f"{p.get('body_ratio_max'):.2f}" if p.get("body_ratio_max") else " off"
    adx  = f"{int(p.get('htf_adx_min'))}" if p.get("htf_adx_min") else "off"
    ema200 = "Y" if p.get("htf_price_above_ema200") else "N"
    return (
        f"prox={p.get('ema_prox_atr_mult',0):.2f} "
        f"rsi={p.get('rsi_min',0):.0f} "
        f"sl={p.get('sl_atr_mult',0):.2f} "
        f"tp={p.get('tp_atr_mult',0):.2f} "
        f"body={body} "
        f"htf={p.get('htf_ema_fast','?')}/{p.get('htf_ema_slow','?')} "
        f"adx={adx} ema200={ema200}"
    )


def print_leaderboard(top10: list[dict], stage: str, mode: str, cost: float,
                       done: int, total: int, t_start: float):
    elapsed = time.time() - t_start
    rate = done / elapsed if elapsed > 0 else 0
    eta = (total - done) / rate if rate > 0 else 0

    bar_len = 72
    print("\n" + "━" * bar_len)
    print(f" Trial {done:>5}/{total}  |  Elapsed: {elapsed:>6.0f}s  |  "
          f"ETA: {eta/60:>5.1f}min  |  Rate: {rate:.2f}/s")
    if not top10:
        print(" No results yet.")
        print("━" * bar_len)
        return

    hdr = f" TOP {min(len(top10),10)} — stage={stage}  mode={mode}  cost={cost:.4f}"
    print(hdr)
    print(f"  {'#':>3}  {'PF':>7}  {'WR':>6}  {'CAGR':>7}  {'MaxDD':>7}  {'n':>5}  Parameters")
    print("  " + "─" * (bar_len - 2))
    for i, r in enumerate(top10[:10], 1):
        p   = r.get("params", {}) or {}
        pf  = r.get("pf", 0)
        wr  = r.get("wr", 0) * 100
        cagr= r.get("cagr", 0)
        mdd = r.get("maxdd", 0)
        n   = r.get("n_trades", 0)
        vid = p.get("_variant_id", "")
        vlabel = p.get("_variant_label", "")
        if vid:
            param_str = f"{vid}: {vlabel}"
        else:
            param_str = _fmt_params(p)
        sign = "+" if cagr >= 0 else ""
        print(f"  {i:>3}  {pf:>7.4f}  {wr:>5.1f}%  {sign}{cagr:>6.1f}%  {mdd:>6.1f}%  {n:>5}  {param_str}")
    print("━" * bar_len + "\n")


# ═══════════════════════════════════════════════════════════════════════════════
#  Core sweep runner
# ═══════════════════════════════════════════════════════════════════════════════

def run_sweep(
    stage: str,
    trials: list[dict],        # list of param dicts
    output_csv: Path,
    mode: str,
    cost: float,
    date_start: str = IS_START,
    date_end:   str = IS_END,
    workers: int = None,
    report_every: int = 50,
    extra_per_trial: dict = None,   # extra fields to write per trial (e.g. wf window)
    sort_col: str = "pf",
    sort_dir: str = "desc",
) -> list[dict]:
    """
    Run a sweep of param trials using multiprocessing.Pool.
    Returns list of all results sorted by sort_col.
    """
    if workers is None:
        workers = max(1, (os.cpu_count() or 4) - 1)

    n_total = len(trials)
    print(f"\n[{stage.upper()}] {n_total} trials | mode={mode} | cost={cost:.4f} | "
          f"period={date_start}→{date_end} | workers={workers}")
    print(f"  Output: {output_csv.name}")

    # Build args list
    args_list = [
        (tid, p, mode, date_start, date_end, cost)
        for tid, p in enumerate(trials)
    ]

    top10: list[dict] = []
    all_results: list[dict] = []
    fh, writer = _open_csv(output_csv, append=output_csv.exists())
    t_start = time.time()
    done = 0
    errors = 0

    # Graceful interrupt handler
    interrupted = False
    original_sigint = signal.getsignal(signal.SIGINT)
    def _handler(sig, frame):
        nonlocal interrupted
        interrupted = True
        print("\n[!] Interrupted — saving progress and exiting cleanly...")
    signal.signal(signal.SIGINT, _handler)

    try:
        # Use 'spawn' context on Windows (required for CUDA / numpy fork safety)
        ctx = mp.get_context("spawn")
        with ctx.Pool(processes=workers, maxtasksperchild=50) as pool:
            for result in pool.imap_unordered(worker_run, args_list, chunksize=max(1, workers * 2)):
                if interrupted:
                    pool.terminate()
                    break

                done += 1
                row = _flatten_result(result, stage, extra_per_trial)
                writer.writerow(row)
                fh.flush()

                if result.get("status") == "error":
                    errors += 1
                else:
                    all_results.append(result)
                    # Maintain sorted top-10
                    all_results_ok = [r for r in all_results if r.get("status") == "ok"]
                    top10 = sorted(
                        all_results_ok,
                        key=lambda x: x.get(sort_col, 0),
                        reverse=(sort_dir == "desc")
                    )[:10]

                if done % report_every == 0 or done == n_total:
                    print_leaderboard(top10, stage, mode, cost, done, n_total, t_start)
                    if errors > 0:
                        print(f"  [WARN] {errors} errors so far — check CSV for details")
    finally:
        fh.close()
        signal.signal(signal.SIGINT, original_sigint)

    elapsed = time.time() - t_start
    ok = len([r for r in all_results if r.get("status") == "ok"])
    print(f"\n[{stage.upper()}] DONE — {ok}/{n_total} ok, {errors} errors, "
          f"{elapsed:.1f}s ({elapsed/max(n_total,1):.2f}s/trial)")
    print(f"  Results saved → {output_csv}")
    return all_results


# ═══════════════════════════════════════════════════════════════════════════════
#  Stage runners
# ═══════════════════════════════════════════════════════════════════════════════

def stage_baseline(args):
    """Stage A: reproduce the published baselines."""
    print("\n" + "═"*72)
    print(" STAGE A — BASELINE REPRODUCTION")
    print(" Expected: PBL PF≈0.8995 | SLC PF≈1.5455 | Combined PF≈1.2682")
    print("═"*72)

    out = RESULTS_DIR / "baseline_reproduction.csv"

    for label, mode, cost in [
        ("PBL standalone (zero fees)",       "pbl_only",  0.0),
        ("SLC standalone (zero fees)",       "slc_only",  0.0),
        ("Combined zero fees",               "combined",  0.0),
        ("Combined 0.04%/side",              "combined",  0.0004),
    ]:
        print(f"\n  Running: {label}")
        args_list = [(0, BASELINE_PBL, mode, IS_START, IS_END, cost)]
        ctx = mp.get_context("spawn")
        with ctx.Pool(1) as pool:
            results = list(pool.imap_unordered(worker_run, args_list))
        r = results[0]
        if r.get("status") == "error":
            print(f"  [ERROR] {r.get('error')}")
        else:
            print(f"  PF={r['pf']:.4f}  WR={r['wr']*100:.1f}%  "
                  f"CAGR={r['cagr']:.2f}%  MaxDD={r['maxdd']:.2f}%  n={r['n_trades']}")

    print("\n  [Stage A complete — compare above to expected values]")


def stage_coarse_pbl(args):
    """Stage B: coarse PBL parameter sweep."""
    print("\n" + "═"*72)
    print(" STAGE B — COARSE PBL SWEEP")
    print(" Searching for PBL configs with standalone PF > 1.10")
    print("═"*72)

    grid = build_coarse_pbl_grid()
    print(f"  Grid size: {len(grid)} trials after pruning")

    results = run_sweep(
        stage="coarse_pbl",
        trials=grid,
        output_csv=RESULTS_DIR / "trials_pbl_coarse.csv",
        mode="pbl_only",
        cost=0.0,                   # zero fees for IS search
        date_start=IS_START,
        date_end=IS_END,
        workers=args.workers,
        report_every=args.report_every,
    )

    # Save leaderboard snapshot
    ok = [r for r in results if r.get("status") == "ok"]
    top10 = sorted(ok, key=lambda x: x.get("pf", 0), reverse=True)[:10]
    lb_path = LEADER_DIR / "top10_coarse_pbl.json"
    lb_path.write_text(json.dumps(top10, indent=2, default=str))
    print(f"  Top-10 leaderboard → {lb_path}")

    # Print acceptance summary
    passing = [r for r in ok if r.get("pf", 0) >= 1.10]
    print(f"\n  Configs with PF ≥ 1.10: {len(passing)}/{len(ok)}")
    if passing:
        best = top10[0]
        p = best.get("params", {})
        print(f"  Best: PF={best['pf']:.4f} | {_fmt_params(p)}")
    else:
        print("  [!] No config cleared PF ≥ 1.10 — review hypotheses H1-H7")


def stage_slc_sweep(args):
    """Secondary SLC sweep."""
    print("\n" + "═"*72)
    print(" STAGE (SLC) — SLC PARAMETER SWEEP")
    print(" Floor: SLC PF must stay ≥ 1.40 (baseline = 1.5455)")
    print("═"*72)

    grid = build_slc_sweep_grid()
    print(f"  Grid size: {len(grid)} trials after pruning")

    results = run_sweep(
        stage="slc_sweep",
        trials=grid,
        output_csv=RESULTS_DIR / "trials_slc_sweep.csv",
        mode="slc_only",
        cost=0.0,
        date_start=IS_START,
        date_end=IS_END,
        workers=args.workers,
        report_every=args.report_every,
    )

    ok = [r for r in results if r.get("status") == "ok"]
    floor_pass = [r for r in ok if r.get("pf", 0) >= 1.40]
    print(f"\n  Configs clearing PF ≥ 1.40: {floor_pass}/{len(ok)}")


def stage_confirmation(args):
    """Stage D: 10 HTF confirmation variants, combined mode."""
    print("\n" + "═"*72)
    print(" STAGE D — HTF CONFIRMATION VARIANTS (combined mode)")
    print(" Tests 10 alternative 4h EMA/ADX/EMA200 gates")
    print("═"*72)

    # Load best PBL params from coarse sweep
    coarse_csv = RESULTS_DIR / "trials_pbl_coarse.csv"
    if coarse_csv.exists():
        try:
            top_list = _load_top_n_from_csv(coarse_csv, n=1, sort_col="pf")
            best_pbl = top_list[0] if top_list else BASELINE_PBL
            print(f"  Best PBL params loaded from {coarse_csv.name}")
        except Exception as e:
            print(f"  [WARN] Could not load coarse CSV ({e}) — using baseline PBL params")
            best_pbl = BASELINE_PBL
    else:
        print("  [WARN] No coarse_pbl CSV found — using baseline PBL params")
        best_pbl = BASELINE_PBL

    grid = build_confirmation_grid(best_pbl)
    print(f"  Grid: {len(grid)} variants")

    results = run_sweep(
        stage="confirmation",
        trials=grid,
        output_csv=RESULTS_DIR / "trials_confirmation.csv",
        mode="combined",
        cost=0.0004,                # combined uses realistic fees
        date_start=IS_START,
        date_end=IS_END,
        workers=min(args.workers, len(grid)),  # no need for more workers than variants
        report_every=1,             # only 10 trials — report every one
        sort_col="pf",
    )

    ok = [r for r in results if r.get("status") == "ok"]
    top = sorted(ok, key=lambda x: x.get("pf", 0), reverse=True)[:10]
    lb_path = LEADER_DIR / "top10_confirmation.json"
    lb_path.write_text(json.dumps(top, indent=2, default=str))
    print(f"  Leaderboard → {lb_path}")


def stage_walkforward(args):
    """Stage E: walk-forward validation on top-N candidates from coarse sweep."""
    print("\n" + "═"*72)
    print(" STAGE E — WALK-FORWARD VALIDATION")
    print(f" {len(WF_WINDOWS)} windows (12m IS / 3m OOS) | top-{args.top_n} candidates")
    print(" Acceptance: WFE = mean(OOS PF) / mean(IS PF) ≥ 0.75")
    print("═"*72)

    coarse_csv = RESULTS_DIR / "trials_pbl_coarse.csv"
    if not coarse_csv.exists():
        print(f"[ERROR] Need {coarse_csv} — run --stage coarse_pbl first")
        return

    candidates = _load_top_n_from_csv(coarse_csv, n=args.top_n, sort_col="pf")
    print(f"  Loaded {len(candidates)} candidate(s) from coarse sweep")

    out_csv = RESULTS_DIR / "trials_walkforward.csv"
    all_wf: list[dict] = []

    for cand_idx, params in enumerate(candidates, 1):
        print(f"\n  Candidate {cand_idx}/{len(candidates)}: {_fmt_params(params)}")
        wfe_is = []
        wfe_oos = []

        for w_idx, (is_s, is_e, oos_s, oos_e) in enumerate(WF_WINDOWS, 1):
            print(f"    Window {w_idx}: IS={is_s}→{is_e} | OOS={oos_s}→{oos_e}")
            extra = {"_wf_window": f"W{w_idx}_{is_s}_{oos_s}"}

            # IS trial
            is_args = [(0, params, "combined", is_s, is_e, 0.0004)]
            ctx = mp.get_context("spawn")
            with ctx.Pool(1) as pool:
                is_res = list(pool.imap_unordered(worker_run, is_args))[0]

            # OOS trial
            oos_args = [(0, params, "combined", oos_s, oos_e, 0.0004)]
            with ctx.Pool(1) as pool:
                oos_res = list(pool.imap_unordered(worker_run, oos_args))[0]

            is_pf  = is_res.get("pf", 0) if is_res.get("status") == "ok" else 0
            oos_pf = oos_res.get("pf", 0) if oos_res.get("status") == "ok" else 0

            print(f"      IS PF={is_pf:.4f}  OOS PF={oos_pf:.4f}  "
                  f"WFE={oos_pf/is_pf:.3f}" if is_pf > 0 else
                  f"      IS PF={is_pf:.4f}  OOS PF={oos_pf:.4f}")

            wfe_is.append(is_pf)
            wfe_oos.append(oos_pf)

            # Write both IS and OOS rows
            for pfx, res, period in [("IS", is_res, f"IS_W{w_idx}"), ("OOS", oos_res, f"OOS_W{w_idx}")]:
                row = _flatten_result(res, "walkforward", {"_wf_window": period, "_cand_idx": cand_idx})
                all_wf.append(row)

        if wfe_is and wfe_oos:
            mean_is  = np.mean([x for x in wfe_is if x > 0]) if any(x > 0 for x in wfe_is) else 0
            mean_oos = np.mean([x for x in wfe_oos]) if wfe_oos else 0
            wfe      = mean_oos / mean_is if mean_is > 0 else 0
            std_oos  = np.std(wfe_oos) if len(wfe_oos) > 1 else 0
            stab     = std_oos / mean_oos if mean_oos > 0 else 999
            verdict  = "✅ PASS" if wfe >= 0.75 and stab <= 0.30 else "⚠️  CAUTION" if wfe >= 0.50 else "❌ FAIL"
            print(f"\n  → WFE={wfe:.3f}  OOS stability (std/mean)={stab:.3f}  {verdict}")

    # Write all WF results
    fh, writer = _open_csv(out_csv, append=False)
    for row in all_wf:
        writer.writerow(row)
    fh.close()
    print(f"\n  Walk-forward results → {out_csv}")


def stage_holdout(args):
    """Stage E final holdout — ONLY run after full WF validation."""
    print("\n" + "═"*72)
    print(" STAGE E — FINAL HOLDOUT (2025-03-22 → 2026-03-21)")
    print(" ⚠️  HOLDOUT IS SACRED — run this ONLY ONCE per candidate set")
    print(" ⚠️  NEVER use holdout results to select parameters")
    print("═"*72)

    print("\n  This will use the holdout period: 2025-03-22 → 2026-03-21")
    confirm = input("  Type 'CONFIRM HOLDOUT' to proceed: ").strip()
    if confirm != "CONFIRM HOLDOUT":
        print("  Aborted.")
        return

    coarse_csv = RESULTS_DIR / "trials_pbl_coarse.csv"
    if not coarse_csv.exists():
        print(f"[ERROR] Need {coarse_csv}")
        return

    candidates = _load_top_n_from_csv(coarse_csv, n=args.top_n, sort_col="pf")
    print(f"\n  Running holdout on {len(candidates)} candidate(s)...")

    results = run_sweep(
        stage="holdout",
        trials=candidates,
        output_csv=RESULTS_DIR / "trials_holdout.csv",
        mode="combined",
        cost=0.0004,
        date_start=OOS_START,
        date_end=OOS_END,
        workers=min(args.workers, len(candidates)),
        report_every=1,
    )

    ok = [r for r in results if r.get("status") == "ok"]
    passing = [r for r in ok if r.get("pf", 0) >= 1.05]
    print(f"\n  Holdout PF ≥ 1.05: {len(passing)}/{len(ok)}")
    for r in sorted(ok, key=lambda x: x.get("pf", 0), reverse=True):
        p = r.get("params", {})
        verdict = "✅ PASS" if r.get("pf", 0) >= 1.05 else "❌ FAIL"
        print(f"  {verdict}  PF={r['pf']:.4f}  CAGR={r['cagr']:.1f}%  MaxDD={r['maxdd']:.1f}%  {_fmt_params(p)}")


def stage_stress(args):
    """Stage F: fee sensitivity stress test."""
    print("\n" + "═"*72)
    print(" STAGE F — FEE SENSITIVITY STRESS TEST")
    print(f" Fee scenarios: {[f'{c*10000:.0f}bps' for c in FEE_SCENARIOS]}")
    print("═"*72)

    coarse_csv = RESULTS_DIR / "trials_pbl_coarse.csv"
    if not coarse_csv.exists():
        print(f"[ERROR] Need {coarse_csv}")
        return

    candidates = _load_top_n_from_csv(coarse_csv, n=args.top_n, sort_col="pf")
    all_trials = []
    for cost in FEE_SCENARIOS:
        all_trials.extend(candidates)  # same params, different cost per pool call

    out_csv = RESULTS_DIR / "trials_stress.csv"
    fh, writer = _open_csv(out_csv, append=False)

    for cand_idx, params in enumerate(candidates, 1):
        print(f"\n  Candidate {cand_idx}: {_fmt_params(params)}")
        print(f"  {'Cost':>8}  {'PF':>7}  {'CAGR':>7}  {'MaxDD':>7}  {'n':>5}")
        for cost in FEE_SCENARIOS:
            args_list = [(0, params, "combined", IS_START, IS_END, cost)]
            ctx = mp.get_context("spawn")
            with ctx.Pool(1) as pool:
                r = list(pool.imap_unordered(worker_run, args_list))[0]
            if r.get("status") == "ok":
                sign = "+" if r["cagr"] >= 0 else ""
                lbl  = f"{cost*10000:.0f}bps"
                print(f"  {lbl:>8}  {r['pf']:>7.4f}  {sign}{r['cagr']:>5.1f}%  {r['maxdd']:>6.1f}%  {r['n_trades']:>5}")
                row = _flatten_result(r, "stress", {"_wf_window": f"cost_{cost:.4f}"})
                writer.writerow(row)
                fh.flush()

    fh.close()
    print(f"\n  Stress results → {out_csv}")


# ═══════════════════════════════════════════════════════════════════════════════
#  CLI entry point
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="NexusTrader PBL/SLC optimization runner",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--stage",
        choices=["baseline", "coarse_pbl", "slc_sweep", "confirmation",
                 "walkforward", "holdout", "stress"],
        required=True,
        help="Which research stage to run",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=max(1, (os.cpu_count() or 4) - 1),
        help=f"Worker processes (default: cpu_count-1 = {max(1,(os.cpu_count() or 4)-1)})",
    )
    parser.add_argument(
        "--top-n",
        type=int,
        default=5,
        help="Top-N candidates to use in WF / holdout / stress (default: 5)",
    )
    parser.add_argument(
        "--report-every",
        type=int,
        default=50,
        help="Print leaderboard every N completed trials (default: 50)",
    )
    args = parser.parse_args()

    print(f"\n{'='*72}")
    print(f"  NexusTrader — PBL/SLC Optimization Harness")
    print(f"  Stage: {args.stage.upper()}")
    print(f"  Workers: {args.workers} | CPU cores available: {os.cpu_count()}")
    print(f"  Results directory: {RESULTS_DIR}")
    print(f"{'='*72}")

    stage_fn = {
        "baseline":     stage_baseline,
        "coarse_pbl":   stage_coarse_pbl,
        "slc_sweep":    stage_slc_sweep,
        "confirmation": stage_confirmation,
        "walkforward":  stage_walkforward,
        "holdout":      stage_holdout,
        "stress":       stage_stress,
    }[args.stage]

    stage_fn(args)
    print("\n[DONE]\n")


if __name__ == "__main__":
    # IMPORTANT: Required on Windows for multiprocessing.
    # This guard MUST be present — without it, each worker spawns infinitely.
    mp.freeze_support()
    main()
