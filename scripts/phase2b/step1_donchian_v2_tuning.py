"""
Phase 2b Step 1: DonchianBreakout v2 — Experimental Single-Pass Tuning
========================================================================
EXPERIMENTAL MODEL — STRICT DROP POLICY:
  If no combo achieves PF >= 1.18 at fees+slippage, DB v2 is permanently dropped.

Execution Realism (v2.1 requirements):
  - Entry at next candle open (pending_entries buffer — already in BacktestRunner)
  - No same-bar fills (enforced by pending_entries)
  - 0.04% fees per side (DEFAULT_COST = 0.0004)
  - Slippage simulation: 0.03% per side (midpoint of 0.02%–0.05% range)
    Applied as additional cost: effective_cost = fee + slippage = 0.0004 + 0.0003 = 0.0007

Grid (single pass — plan says DO NOT attempt extended tuning):
  lookback:     [40, 60]
  vol_mult_min: [1.8, 2.0, 2.5]
  sl_atr_mult:  [2.0]  (fixed — wider stop per plan)
  tp_atr_mult:  [3.0, 4.0]  (test wider TP)
  ACTIVE_REGIMES restriction via regime_affinity override:
    - ranging=0.0, vol_compression=0.0, uncertain=0.0, accumulation=0.0
    - Keeps: bull_trend, bear_trend, vol_expansion, squeeze, recovery, distribution

Total combos: 2 × 3 × 1 × 2 = 12  (feasible single pass)

Acceptance: PF >= 1.18 (fees+slippage), MaxDD <= 25%, n >= 200
"""

import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

# ── Project root ──
ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

logging.basicConfig(level=logging.WARNING, format="%(message)s")
logger = logging.getLogger("step1_db_v2")
logger.setLevel(logging.INFO)

# ════════════════════════════════════════════════════════════
# Constants
# ════════════════════════════════════════════════════════════
DATE_START   = "2022-03-22"
DATE_END     = "2026-03-21"
IS_END       = "2025-09-22"   # 3.5yr IS / 6mo OOS
FEE          = 0.0004          # 0.04% per side
SLIPPAGE     = 0.0003          # 0.03% per side (midpoint of 0.02%–0.05%)
COST_TOTAL   = FEE + SLIPPAGE  # 0.07% per side total

# Grid search params
LOOKBACKS    = [40, 60]
VOL_MULTS    = [1.8, 2.0, 2.5]
SL_ATR_MULTS = [2.0]
TP_ATR_MULTS = [3.0, 4.0]

# Models to disable (keep only PBL+SLC+Donchian)
BASE_DISABLED = ["mean_reversion", "liquidity_sweep", "trend", "momentum_breakout"]

# Restricted regime affinity — zero out weak regimes
RESTRICTED_AFFINITY = {
    "bull_trend":             0.85,
    "bear_trend":             0.85,
    "ranging":                0.0,   # zeroed
    "volatility_expansion":   0.90,
    "volatility_compression": 0.0,   # zeroed
    "uncertain":              0.0,   # zeroed
    "crisis":                 0.0,
    "liquidation_cascade":    0.0,
    "squeeze":                0.75,
    "recovery":               0.65,
    "accumulation":           0.0,   # zeroed
    "distribution":           0.50,
}

# ════════════════════════════════════════════════════════════
# Helpers
# ════════════════════════════════════════════════════════════

def build_params(lb, vm, sl, tp) -> dict[str, Any]:
    """Build params dict for a single grid point."""
    return {
        "disabled_models":                         BASE_DISABLED,
        "models.donchian_breakout.lookback":        lb,
        "models.donchian_breakout.vol_mult_min":    vm,
        "models.donchian_breakout.sl_atr_mult":     sl,
        "models.donchian_breakout.tp_atr_mult":     tp,
        "models.donchian_breakout.entry_buffer_atr": 0.15,   # v2: wider buffer per plan
        "models.donchian_breakout.rsi_long_min":    50.0,
        "models.donchian_breakout.rsi_short_max":   50.0,
    }


def apply_restricted_affinity():
    """Monkey-patch DonchianBreakoutModel.REGIME_AFFINITY to restrict weak regimes.

    REGIME_AFFINITY is a class attribute, not read from settings.
    The only way to restrict it for backtest is to modify the class directly.
    """
    from core.signals.sub_models.donchian_breakout_model import DonchianBreakoutModel
    DonchianBreakoutModel.REGIME_AFFINITY = RESTRICTED_AFFINITY
    logger.info("  Applied restricted REGIME_AFFINITY to DonchianBreakoutModel")
    logger.info("  Zeroed regimes: ranging, vol_compression, uncertain, accumulation")


def restore_default_affinity():
    """Restore original REGIME_AFFINITY after test."""
    from core.signals.sub_models.donchian_breakout_model import DonchianBreakoutModel
    DonchianBreakoutModel.REGIME_AFFINITY = {
        "bull_trend": 0.85, "bear_trend": 0.85, "ranging": 0.15,
        "volatility_expansion": 0.90, "volatility_compression": 0.10,
        "uncertain": 0.25, "crisis": 0.0, "liquidation_cascade": 0.0,
        "squeeze": 0.75, "recovery": 0.65, "accumulation": 0.35, "distribution": 0.50,
    }


def extract_metrics(res: dict, label: str = "") -> dict:
    """Extract standard metrics from runner result.

    Works with both _run_scenario (pbl_slc mode) and _run_unified_scenario (custom mode).
    The runner already computes slc_n, slc_pf, pbl_n, pbl_pf etc. in the result dict,
    so prefer those. Fall back to trades list parsing if not present.
    """
    n = res.get("n_trades", 0)
    if n == 0:
        trades = res.get("trades", [])
        n = len(trades)
    if n == 0:
        return {"label": label, "n": 0, "wr": 0, "pf": 0, "cagr": 0, "max_dd": 0,
                "db_n": 0, "db_pf": 0, "db_wr": 0, "slc_n": 0, "slc_pf": 0}

    # Use runner's computed metrics (preferred)
    pf = res.get("profit_factor", 0)
    wr = res.get("win_rate", 0)
    cagr = res.get("cagr", 0)
    max_dd = res.get("max_drawdown", 0)
    final_eq = res.get("final_equity", 0)

    # Per-model stats (runner computes these in both modes)
    slc_n = res.get("slc_n", 0)
    slc_pf = res.get("slc_pf", 0)
    pbl_n = res.get("pbl_n", 0)
    pbl_pf = res.get("pbl_pf", 0)

    # Donchian stats (unified engine uses abbreviated keys: db_n, db_pf, db_wr)
    db_n = res.get("db_n", 0)
    db_pf = res.get("db_pf", 0)
    db_wr = res.get("db_wr", 0)

    # Fallback: parse trades list if runner didn't provide per-model stats
    if db_n == 0 and "trades" in res:
        trades = res["trades"]
        db_trades = [t for t in trades if t.get("model", "") == "donchian_breakout"]
        db_n = len(db_trades)
        if db_n > 0:
            db_wins = [t for t in db_trades if t.get("pnl", 0) > 0]
            db_losses = [t for t in db_trades if t.get("pnl", 0) <= 0]
            db_gp = sum(t["pnl"] for t in db_wins) if db_wins else 0
            db_gl = abs(sum(t["pnl"] for t in db_losses)) if db_losses else 0.001
            db_pf = db_gp / db_gl
            db_wr = len(db_wins) / db_n

    if slc_n == 0 and "trades" in res:
        trades = res["trades"]
        slc_trades = [t for t in trades if t.get("model", "") == "swing_low_continuation"]
        slc_n = len(slc_trades)
        if slc_n > 0:
            slc_wins = [t for t in slc_trades if t.get("pnl", 0) > 0]
            slc_losses = [t for t in slc_trades if t.get("pnl", 0) <= 0]
            slc_gp = sum(t["pnl"] for t in slc_wins) if slc_wins else 0
            slc_gl = abs(sum(t["pnl"] for t in slc_losses)) if slc_losses else 0.001
            slc_pf = slc_gp / slc_gl

    return {
        "label":     label,
        "n":         n,
        "wr":        wr,
        "pf":        pf,
        "cagr":      cagr,
        "max_dd":    max_dd,
        "final_eq":  final_eq,
        "pbl_n":     pbl_n,
        "pbl_pf":    pbl_pf,
        "db_n":      db_n,
        "db_pf":     db_pf,
        "db_wr":     db_wr,
        "slc_n":     slc_n,
        "slc_pf":    slc_pf,
    }


def run_combo(runner, params: dict, cost: float, label: str) -> dict:
    """Run a single grid combo and return metrics."""
    try:
        res = runner.run(params=params, cost_per_side=cost, n_workers=1)
        return extract_metrics(res, label)
    except Exception as e:
        logger.error("FAILED %s: %s", label, e)
        return {"label": label, "n": 0, "wr": 0, "pf": 0, "cagr": 0, "max_dd": 0, "error": str(e)}


# ════════════════════════════════════════════════════════════
# Main
# ════════════════════════════════════════════════════════════

def main():
    from research.engine.backtest_runner import BacktestRunner

    logger.info("=" * 70)
    logger.info("Phase 2b Step 1: DonchianBreakout v2 — Experimental Tuning")
    logger.info("=" * 70)
    logger.info("Dataset:  %s → %s  (BTC+SOL+ETH 30m)", DATE_START, DATE_END)
    logger.info("Cost:     fee=%.4f + slippage=%.4f = %.4f per side", FEE, SLIPPAGE, COST_TOTAL)
    logger.info("Grid:     %d combos", len(LOOKBACKS) * len(VOL_MULTS) * len(SL_ATR_MULTS) * len(TP_ATR_MULTS))
    logger.info("")

    # ── 1. Establish baseline: PBL+SLC only ──────────────────────────────
    logger.info("Phase A: Establishing PBL+SLC baseline (with slippage)...")
    t0 = time.time()

    runner_base = BacktestRunner(
        date_start=DATE_START, date_end=DATE_END,
        mode="pbl_slc",
    )
    runner_base.load_data()

    base_res = runner_base.run(params={}, cost_per_side=COST_TOTAL, n_workers=1)
    base_m = extract_metrics(base_res, "BASELINE (PBL+SLC, fee+slip)")
    logger.info("  Baseline: n=%d  PF=%.4f  WR=%.1f%%  CAGR=%.2f%%  MaxDD=%.2f%%  SLC_n=%d  SLC_PF=%.4f",
                base_m["n"], base_m["pf"], base_m["wr"]*100,
                base_m["cagr"]*100, base_m["max_dd"]*100,
                base_m["slc_n"], base_m["slc_pf"])
    logger.info("  Baseline elapsed: %.1fs", time.time() - t0)

    # Also run baseline with fees only (no slippage) for reference
    base_fee_res = runner_base.run(params={}, cost_per_side=FEE, n_workers=1)
    base_fee_m = extract_metrics(base_fee_res, "BASELINE (PBL+SLC, fee only)")
    logger.info("  Baseline (fee only): PF=%.4f  n=%d  SLC_PF=%.4f", base_fee_m["pf"], base_fee_m["n"], base_fee_m["slc_pf"])

    # ── 2. Grid search: PBL+SLC+Donchian v2 ──────────────────────────────
    logger.info("")
    logger.info("Phase B: Grid search (12 combos, PBL+SLC+Donchian v2)...")
    apply_restricted_affinity()

    runner_grid = BacktestRunner(
        date_start=DATE_START, date_end=DATE_END,
        mode="custom",
        strategy_subset=["pullback_long", "swing_low_continuation", "donchian_breakout"],
    )
    # Must load_data() here because mode="custom" with donchian_breakout triggers
    # _needs_hmm()=True which computes NX regime arrays and fits HMM classifiers.
    # The pbl_slc baseline runner does NOT compute these (PBL/SLC are not HMM models).
    runner_grid.load_data()

    results = []
    combo_idx = 0
    total_combos = len(LOOKBACKS) * len(VOL_MULTS) * len(SL_ATR_MULTS) * len(TP_ATR_MULTS)

    for lb in LOOKBACKS:
        for vm in VOL_MULTS:
            for sl in SL_ATR_MULTS:
                for tp in TP_ATR_MULTS:
                    combo_idx += 1
                    label = f"lb={lb} vm={vm} sl={sl} tp={tp}"
                    logger.info("  [%d/%d] %s ...", combo_idx, total_combos, label)
                    t1 = time.time()

                    params = build_params(lb, vm, sl, tp)

                    # Primary metric: fees + slippage
                    m_slip = run_combo(runner_grid, params, COST_TOTAL, f"{label} (fee+slip)")

                    row = {
                        "lookback": lb,
                        "vol_mult_min": vm,
                        "sl_atr_mult": sl,
                        "tp_atr_mult": tp,
                        "n": m_slip["n"],
                        "pf_slip": m_slip["pf"],
                        "wr_slip": m_slip["wr"],
                        "cagr_slip": m_slip["cagr"],
                        "mdd_slip": m_slip["max_dd"],
                        "db_n": m_slip.get("db_n", 0),
                        "db_pf_slip": m_slip.get("db_pf", 0),
                        "db_wr_slip": m_slip.get("db_wr", 0),
                        "slc_n": m_slip.get("slc_n", 0),
                        "slc_pf_slip": m_slip.get("slc_pf", 0),
                        "pbl_n": m_slip.get("pbl_n", 0),
                        "score": m_slip["pf"] * (1 - abs(m_slip["max_dd"])) if m_slip["n"] > 0 else 0,
                    }
                    results.append(row)

                    dt = time.time() - t1
                    logger.info("    → n=%d  PF(slip)=%.4f  DB_n=%d  DB_PF=%.4f  SLC_n=%d  (%.1fs)",
                                row["n"], row["pf_slip"],
                                row["db_n"], row["db_pf_slip"], row["slc_n"], dt)

    # ── 3. IS/OOS split for top candidates ────────────────────────────────
    logger.info("")
    logger.info("Phase C: IS/OOS validation for top candidates...")

    # Rank by primary metric: PF at fee+slippage
    ranked = sorted(results, key=lambda r: r["pf_slip"], reverse=True)

    # Top 3 that pass the minimum gate
    candidates = [r for r in ranked if r["pf_slip"] >= 1.18 and r["n"] >= 200 and abs(r["mdd_slip"]) <= 0.25]

    is_oos_results = []
    if candidates:
        logger.info("  %d candidate(s) passed initial gate (PF>=1.18, n>=200, MaxDD<=25%%):", len(candidates))
        for c in candidates[:3]:
            logger.info("    %s: PF=%.4f  n=%d  MaxDD=%.2f%%",
                        f"lb={c['lookback']} vm={c['vol_mult_min']} tp={c['tp_atr_mult']}",
                        c["pf_slip"], c["n"], c["mdd_slip"]*100)

        # Run IS/OOS for each candidate
        runner_is = BacktestRunner(
            date_start=DATE_START, date_end=IS_END,
            mode="custom",
            strategy_subset=["pullback_long", "swing_low_continuation", "donchian_breakout"],
        )
        runner_is.load_data()

        runner_oos = BacktestRunner(
            date_start=IS_END, date_end=DATE_END,
            mode="custom",
            strategy_subset=["pullback_long", "swing_low_continuation", "donchian_breakout"],
        )
        runner_oos.load_data()

        for c in candidates[:3]:
            lb, vm, sl, tp = c["lookback"], c["vol_mult_min"], c["sl_atr_mult"], c["tp_atr_mult"]
            params = build_params(lb, vm, sl, tp)
            label_c = f"lb={lb} vm={vm} sl={sl} tp={tp}"

            # IS
            m_is = run_combo(runner_is, params, COST_TOTAL, f"{label_c} IS")
            # OOS
            m_oos = run_combo(runner_oos, params, COST_TOTAL, f"{label_c} OOS")

            is_oos_row = {
                "combo": label_c,
                "is_pf": m_is["pf"], "is_wr": m_is["wr"], "is_n": m_is["n"],
                "is_mdd": m_is["max_dd"], "is_cagr": m_is["cagr"],
                "is_db_n": m_is.get("db_n", 0), "is_db_pf": m_is.get("db_pf", 0),
                "oos_pf": m_oos["pf"], "oos_wr": m_oos["wr"], "oos_n": m_oos["n"],
                "oos_mdd": m_oos["max_dd"], "oos_cagr": m_oos["cagr"],
                "oos_db_n": m_oos.get("db_n", 0), "oos_db_pf": m_oos.get("db_pf", 0),
            }
            is_oos_results.append(is_oos_row)

            logger.info("    %s  IS: PF=%.4f n=%d DB_PF=%.4f  |  OOS: PF=%.4f n=%d DB_PF=%.4f",
                        label_c, m_is["pf"], m_is["n"], m_is.get("db_pf", 0),
                        m_oos["pf"], m_oos["n"], m_oos.get("db_pf", 0))
    else:
        logger.info("  NO candidates passed the gate. DB v2 FAILS Step 1.")

    # ── 4. Combined portfolio validation gate (v2.1) ──────────────────────
    logger.info("")
    logger.info("Phase D: Combined portfolio validation gate...")

    combined_pass = False
    best_candidate = None

    for c in candidates[:3]:
        pf_delta = c["pf_slip"] - base_m["pf"]
        mdd_ok = abs(c["mdd_slip"]) <= abs(base_m["max_dd"]) + 0.005  # small tolerance
        slc_pf_ok = c.get("slc_pf_slip", 0) >= base_m["slc_pf"] * 0.95  # 5% tolerance

        option_a = c["pf_slip"] >= base_m["pf"] + 0.03
        option_b = mdd_ok and slc_pf_ok

        logger.info("  %s: PF_delta=%.4f  Option_A(PF>=base+0.03)=%s  Option_B(MDD+SLC)=%s",
                    f"lb={c['lookback']} vm={c['vol_mult_min']} tp={c['tp_atr_mult']}",
                    pf_delta, option_a, option_b)

        if option_a or option_b:
            combined_pass = True
            best_candidate = c
            break

    # ── 5. Verdict ────────────────────────────────────────────────────────
    logger.info("")
    logger.info("=" * 70)
    if combined_pass and best_candidate:
        logger.info("VERDICT: PASS — DonchianBreakout v2 has a viable candidate")
        logger.info("  Best: lb=%d vm=%.1f sl=%.1f tp=%.1f",
                    best_candidate["lookback"], best_candidate["vol_mult_min"],
                    best_candidate["sl_atr_mult"], best_candidate["tp_atr_mult"])
        logger.info("  PF(fee+slip)=%.4f  n=%d  MaxDD=%.2f%%",
                    best_candidate["pf_slip"], best_candidate["n"], best_candidate["mdd_slip"]*100)
    elif candidates:
        logger.info("VERDICT: FAIL — Candidates passed individual gate but FAILED combined portfolio gate")
        logger.info("  DB v2 is DROPPED from Phase 2b scope.")
    else:
        logger.info("VERDICT: FAIL — No parameter combo achieved PF >= 1.18 at fees+slippage")
        logger.info("  DB v2 is DROPPED from Phase 2b scope.")
    logger.info("=" * 70)

    # ── 6. Save results ───────────────────────────────────────────────────
    report = {
        "step": "Phase 2b Step 1: DonchianBreakout v2 Experimental Tuning",
        "execution_realism": {
            "entry": "next-bar open (pending_entries buffer)",
            "same_bar_fills": False,
            "fee_per_side": FEE,
            "slippage_per_side": SLIPPAGE,
            "total_cost_per_side": COST_TOTAL,
        },
        "baseline": {
            "pf_fee_slip": base_m["pf"],
            "pf_fee_only": base_fee_m["pf"],
            "n": base_m["n"],
            "wr": base_m["wr"],
            "cagr": base_m["cagr"],
            "max_dd": base_m["max_dd"],
            "slc_n": base_m["slc_n"],
            "slc_pf": base_m["slc_pf"],
        },
        "grid_results": results,
        "grid_ranked": [r for r in ranked],
        "candidates_passing_individual_gate": [
            {**c, "label": f"lb={c['lookback']} vm={c['vol_mult_min']} sl={c['sl_atr_mult']} tp={c['tp_atr_mult']}"}
            for c in candidates
        ],
        "is_oos_results": is_oos_results,
        "combined_portfolio_gate_passed": combined_pass,
        "best_candidate": best_candidate,
        "verdict": "PASS" if combined_pass else "FAIL — DROPPED",
    }

    out_dir = ROOT / "reports" / "phase2b"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "step1_donchian_v2_results.json"
    with open(out_path, "w") as f:
        json.dump(report, f, indent=2, default=str)

    logger.info("")
    logger.info("Report saved: %s", out_path)
    logger.info("Total elapsed: %.1fs", time.time() - t0)


if __name__ == "__main__":
    main()
