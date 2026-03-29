"""
PBL Fast Optimization Grid — Session 50 Phase 3
================================================
Bypasses the BacktestRunner per-bar ThreadPoolExecutor entirely.

Strategy:
  1. Load BacktestRunner once (uses cached indicators / regimes — ~3s)
  2. Pre-extract ALL PBL candidate bars directly from runner's internal arrays
     (fixed conditions: regime=bull_trend, bullish candle, lw>uw, HTF gate)
  3. For each param combo: vectorised numpy filter + sequential simulation
  4. Rank by PBL PF, run runner.run() for top-5 COMBINED (PBL+SLC) validation

Grid space:
  Stage 1:  sl × tp  (9 combos) — ema=0.4, rsi=45, wick=1.5 fixed
  Stage 2:  ema × rsi × wick  (27 combos) — best sl/tp from Stage 1
  Stage 3:  top-5 by PBL PF → combined PBL+SLC via runner.run()
"""

import sys, json, time, warnings, logging
import numpy as np
import pandas as pd
from collections import defaultdict

warnings.filterwarnings("ignore")
logging.basicConfig(level=logging.WARNING)   # silence INFO spam
sys.path.insert(0, "/sessions/wizardly-dreamy-pasteur/mnt/NexusTrader")

# ── Constants matching BacktestRunner ─────────────────────────────────────────
PRIMARY_TF      = "30m"
HTF_4H_TF       = "4h"
WARMUP_BARS     = 120
MODEL_LOOKBACK  = 350
HTF_LOOKBACK    = 60
INITIAL_CAPITAL = 100_000.0
POS_FRAC        = 0.35
MAX_POSITIONS   = 10
COST            = 0.0004       # 0.04% / side
IS_END          = "2025-09-22"  # IS/OOS split date

from research.engine.backtest_runner import BacktestRunner
from core.regime.research_regime_classifier import BULL_TREND as RES_BULL_TREND

# ── Load runner (warm cache) ───────────────────────────────────────────────────
print("Loading BacktestRunner (warm cache expected)…", flush=True)
t0 = time.time()
runner = BacktestRunner(mode="pbl_slc")
runner.load_data()
print(f"  load_data() done in {time.time()-t0:.1f}s\n", flush=True)

# ── Pre-extract ATR arrays ─────────────────────────────────────────────────────
print("Pre-extracting candidate bars…", flush=True)
t1 = time.time()

all_candidates = []   # list[dict] — one entry per valid pre-filtered bar

for sym in runner.symbols:
    df30 = runner._ind[sym].get(PRIMARY_TF, pd.DataFrame())
    df4h = runner._ind[sym].get(HTF_4H_TF, pd.DataFrame())

    if df30.empty:
        print(f"  {sym}: df30 empty — skip", flush=True)
        continue

    reg30   = runner._reg30.get(sym, np.array([]))
    idx30   = df30.index
    idx4h   = df4h.index if (df4h is not None and not df4h.empty) else pd.DatetimeIndex([])

    close_a = df30["close"].to_numpy(dtype=np.float64)
    open_a  = df30["open"].to_numpy(dtype=np.float64)
    high_a  = df30["high"].to_numpy(dtype=np.float64)
    low_a   = df30["low"].to_numpy(dtype=np.float64)

    ema50_a = df30["ema_50"].to_numpy(dtype=np.float64) \
              if "ema_50" in df30.columns else np.full(len(df30), np.nan)
    rsi_a   = df30["rsi_14"].to_numpy(dtype=np.float64) \
              if "rsi_14" in df30.columns else np.full(len(df30), np.nan)
    atr_a   = df30["atr_14"].to_numpy(dtype=np.float64) \
              if "atr_14" in df30.columns else np.full(len(df30), np.nan)

    # 4h HTF EMAs
    htf_ema20 = None
    htf_ema50 = None
    if df4h is not None and not df4h.empty:
        if "ema_20" in df4h.columns:
            htf_ema20 = df4h["ema_20"].to_numpy(dtype=np.float64)
        if "ema_50" in df4h.columns:
            htf_ema50 = df4h["ema_50"].to_numpy(dtype=np.float64)

    n    = len(df30)
    n4h  = len(idx4h)
    bull_cnt = htf_fail = missing_ind = nonbullish = lw_uw_fail = 0

    for i in range(WARMUP_BARS, n - 1):
        # ── Regime gate ───────────────────────────────────────────────────────
        if i >= len(reg30) or int(reg30[i]) != RES_BULL_TREND:
            continue
        bull_cnt += 1

        if i < MODEL_LOOKBACK - 1:
            continue

        close = close_a[i];  open_  = open_a[i]
        high  = high_a[i];   low    = low_a[i]
        ema50 = ema50_a[i];  rsi    = rsi_a[i];  atr = atr_a[i]

        # ── Missing indicators ────────────────────────────────────────────────
        if np.isnan(ema50) or np.isnan(rsi) or np.isnan(atr) or atr <= 0:
            missing_ind += 1
            continue

        # ── Fixed: bullish candle ─────────────────────────────────────────────
        if close <= open_:
            nonbullish += 1
            continue

        body = abs(close - open_)
        lw   = min(close, open_) - low
        uw   = high - max(close, open_)

        # ── Fixed: lower wick > upper wick ────────────────────────────────────
        if lw <= uw:
            lw_uw_fail += 1
            continue

        prox_distance = abs(close - ema50)

        # ── 4h HTF gate (FIXED — not param-dependent) ─────────────────────────
        # Matches PullbackLongModel.evaluate() exactly.
        # htf_ok = None → bypass (no 4h data); True → confirmed; False → rejected
        htf_ok           = None
        htf_strength_bonus = 0.0

        if htf_ema20 is not None and htf_ema50 is not None:
            l4h = int(idx4h.searchsorted(idx30[i], side="right")) - 1
            HTF_MIN = max(20, 50) + 5   # = 55  (matches _HTF_EMA_FAST, _HTF_EMA_SLOW + 5)
            if l4h >= HTF_MIN:
                ef = htf_ema20[l4h]
                es = htf_ema50[l4h]
                if not (np.isnan(ef) or np.isnan(es)):
                    if ef > es:
                        htf_ok = True
                        spread_pct = (ef - es) / es
                        htf_strength_bonus = 0.15 if spread_pct > 0.01 else 0.08
                    else:
                        htf_fail += 1
                        continue           # HTF gate required — reject

        all_candidates.append({
            "sym":          sym,
            "bar_idx":      i,
            "ts":           idx30[i],
            "close":        close,
            "next_open":    open_a[i + 1],
            "body":         body,
            "lw":           lw,
            "uw":           uw,
            "rsi":          float(rsi),
            "prox_distance":float(prox_distance),
            "atr":          float(atr),
            "htf_confirmed":bool(htf_ok),
            "htf_strength_bonus": htf_strength_bonus,
        })

    print(
        f"  {sym}: bull_bars={bull_cnt}  missing_ind={missing_ind}"
        f"  nonbullish={nonbullish}  lw_uw_fail={lw_uw_fail}"
        f"  htf_fail={htf_fail}  candidates={sum(1 for c in all_candidates if c['sym']==sym)}",
        flush=True,
    )

# Sort chronologically (BTC idx is master; but sort across all syms by ts)
all_candidates.sort(key=lambda x: x["ts"])
print(f"\nTotal pre-filtered candidates: {len(all_candidates)}  (in {time.time()-t1:.1f}s)\n", flush=True)


# ── Fast simulation ────────────────────────────────────────────────────────────
def simulate_pbl(candidates, params):
    """
    Sequential PBL standalone simulation with given params.
    Matches _run_scenario() logic:
      - One position per symbol at a time
      - Next-bar open entry (already stored as candidate.next_open)
      - SL = close − sl_mult × ATR  (signal bar values)
      - TP = close + tp_mult × ATR
      - sl < ep_raw < tp validation
      - SL takes priority when both triggered same bar
      - COST applied to entry and exit
    """
    ema_prox = params.get("ema_prox_atr_mult",  0.5)
    rsi_min  = params.get("rsi_min",            40.0)
    wick_str = params.get("wick_strength",       1.0)
    sl_mult  = params.get("sl_atr_mult",         2.5)
    tp_mult  = params.get("tp_atr_mult",         3.0)

    # Per-symbol open position: {sym: exit_bar_idx}
    open_pos: dict[str, int] = {}
    trades = []

    hi_arr = runner._highs
    lo_arr = runner._lows

    for cand in candidates:
        sym     = cand["sym"]
        bar_idx = cand["bar_idx"]

        # ── Position gate ─────────────────────────────────────────────────────
        if sym in open_pos:
            if bar_idx <= open_pos[sym]:
                continue          # still in position
            else:
                del open_pos[sym]  # position closed

        # ── Param filters ─────────────────────────────────────────────────────
        if cand["prox_distance"] > ema_prox * cand["atr"]:
            continue
        if cand["lw"] <= wick_str * cand["body"]:
            continue
        if cand["rsi"] <= rsi_min:
            continue

        close    = cand["close"]
        atr      = cand["atr"]
        ep_raw   = cand["next_open"]          # entry bar open
        sl       = close - sl_mult * atr
        tp       = close + tp_mult * atr

        # Validate (sl < ep_raw < tp) — long direction
        if not (sl < ep_raw < tp):
            continue

        ep_fill = ep_raw * (1.0 + COST)       # cost at entry

        # ── Vectorised exit search ────────────────────────────────────────────
        entry_bar = bar_idx + 1
        hi  = hi_arr[sym]
        lo  = lo_arr[sym]
        n   = len(hi)
        if entry_bar >= n:
            continue

        fut_lo = lo[entry_bar:]
        fut_hi = hi[entry_bar:]

        sl_hits = np.nonzero(fut_lo <= sl)[0]
        tp_hits = np.nonzero(fut_hi >= tp)[0]

        first_sl = int(sl_hits[0]) if len(sl_hits) > 0 else len(fut_lo)
        first_tp = int(tp_hits[0]) if len(tp_hits) > 0 else len(fut_hi)

        if first_sl <= first_tp:
            if first_sl >= len(fut_lo):
                # Never exits — mark as open at last bar (treat as open_end loss)
                exit_bar = n - 1
                exit_px  = lo[exit_bar]
                reason   = "open_end"
            else:
                exit_bar = entry_bar + first_sl
                exit_px  = sl
                reason   = "sl"
        else:
            if first_tp >= len(fut_hi):
                exit_bar = n - 1
                exit_px  = hi[exit_bar]
                reason   = "open_end"
            else:
                exit_bar = entry_bar + first_tp
                exit_px  = tp
                reason   = "tp"

        # ── PNL / R calculation ───────────────────────────────────────────────
        exit_adj = exit_px * (1.0 - COST)
        qty      = (INITIAL_CAPITAL * POS_FRAC) / ep_fill
        pnl      = (exit_adj - ep_fill) * qty
        r_val    = pnl / (abs(ep_fill - sl) * qty) if abs(ep_fill - sl) > 0 else 0.0

        trades.append({"sym": sym, "reason": reason, "pnl": pnl, "r_val": r_val,
                        "entry_bar": entry_bar, "exit_bar": exit_bar})
        open_pos[sym] = exit_bar

    if not trades:
        return {"pbl_pf": 0.0, "pbl_wr": 0.0, "pbl_n": 0,
                "pbl_n_win": 0, "pbl_n_loss": 0, "avg_win_r": 0, "avg_loss_r": 0}

    wins   = [t for t in trades if t["r_val"] > 0]
    losses = [t for t in trades if t["r_val"] <= 0]
    gross_win  = sum(t["pnl"] for t in wins)
    gross_loss = abs(sum(t["pnl"] for t in losses))

    pf  = gross_win / gross_loss if gross_loss > 0 else 999.0
    wr  = len(wins) / len(trades)
    avg_win_r  = float(np.mean([t["r_val"] for t in wins]))   if wins   else 0.0
    avg_loss_r = float(np.mean([t["r_val"] for t in losses])) if losses else 0.0

    return {
        "pbl_pf":     round(pf, 4),
        "pbl_wr":     round(wr, 4),
        "pbl_n":      len(trades),
        "pbl_n_win":  len(wins),
        "pbl_n_loss": len(losses),
        "avg_win_r":  round(avg_win_r,  4),
        "avg_loss_r": round(avg_loss_r, 4),
    }


# ── IS/OOS split for validation ───────────────────────────────────────────────
is_ts = pd.Timestamp(IS_END, tz="UTC")
candidates_is  = [c for c in all_candidates if c["ts"] <  is_ts]
candidates_oos = [c for c in all_candidates if c["ts"] >= is_ts]
print(f"IS candidates: {len(candidates_is)}  OOS: {len(candidates_oos)}\n", flush=True)


# ── Baseline ───────────────────────────────────────────────────────────────────
print("=== BASELINE (default params) ===", flush=True)
baseline_params = {}
baseline = simulate_pbl(all_candidates, baseline_params)
print(
    f"  BASELINE  PBL_PF={baseline['pbl_pf']:.4f}  "
    f"WR={baseline['pbl_wr']*100:.1f}%  n={baseline['pbl_n']}  "
    f"avgW={baseline['avg_win_r']:.3f}R  avgL={baseline['avg_loss_r']:.3f}R",
    flush=True,
)
print()


# ── STAGE 1: TP × SL sweep ─────────────────────────────────────────────────────
print("=== STAGE 1: TP × SL SWEEP (ema=0.4, rsi=45, wick=1.5) ===", flush=True)
stage1 = []
for sl in [2.0, 2.5, 3.0]:
    for tp in [3.0, 3.5, 4.0]:
        p = {"sl_atr_mult": sl, "tp_atr_mult": tp,
             "ema_prox_atr_mult": 0.4, "rsi_min": 45.0, "wick_strength": 1.5}
        r = simulate_pbl(all_candidates, p)
        r.update({"params": p,
                  "label": f"sl={sl} tp={tp} ema=0.4 rsi=45 wick=1.5"})
        stage1.append(r)
        print(
            f"  {r['label']:45s}  "
            f"PBL_PF={r['pbl_pf']:.4f}  WR={r['pbl_wr']*100:.1f}%  n={r['pbl_n']}",
            flush=True,
        )

best_s1 = max(stage1, key=lambda x: x["pbl_pf"])
print(f"\nStage 1 best: {best_s1['label']}  PF={best_s1['pbl_pf']:.4f}", flush=True)
best_sl = best_s1["params"]["sl_atr_mult"]
best_tp = best_s1["params"]["tp_atr_mult"]
print()


# ── STAGE 2: Entry quality sweep ────────────────────────────────────────────────
print(f"=== STAGE 2: ENTRY QUALITY SWEEP (sl={best_sl} tp={best_tp}) ===", flush=True)
stage2 = []
for ema_d in [0.3, 0.4, 0.5]:
    for rsi_m in [40.0, 45.0, 50.0]:
        for wick in [1.0, 1.5, 2.0]:
            p = {"sl_atr_mult": best_sl, "tp_atr_mult": best_tp,
                 "ema_prox_atr_mult": ema_d, "rsi_min": rsi_m, "wick_strength": wick}
            r = simulate_pbl(all_candidates, p)
            r.update({"params": p,
                      "label": f"ema={ema_d} rsi={rsi_m:.0f} wick={wick} sl={best_sl} tp={best_tp}"})
            stage2.append(r)
            print(
                f"  {r['label']:55s}  "
                f"PBL_PF={r['pbl_pf']:.4f}  WR={r['pbl_wr']*100:.1f}%  n={r['pbl_n']}",
                flush=True,
            )

best_s2 = max(stage2, key=lambda x: x["pbl_pf"])
print(f"\nStage 2 best: {best_s2['label']}  PF={best_s2['pbl_pf']:.4f}", flush=True)
print()


# ── IS / OOS standalone validation for top candidates ─────────────────────────
print("=== IS/OOS STANDALONE VALIDATION (top 5 by FULL-period PBL PF) ===", flush=True)
all_runs = stage1 + stage2
seen = set()
top5 = []
for r in sorted(all_runs, key=lambda x: -x["pbl_pf"]):
    key = (r["params"].get("sl_atr_mult"),
           r["params"].get("tp_atr_mult"),
           r["params"].get("ema_prox_atr_mult"),
           r["params"].get("rsi_min"),
           r["params"].get("wick_strength"))
    if key not in seen:
        seen.add(key)
        top5.append(r)
    if len(top5) == 5:
        break

is_oos_results = []
for r in top5:
    p = r["params"]
    is_r   = simulate_pbl(candidates_is,  p)
    oos_r  = simulate_pbl(candidates_oos, p)
    row = {
        "label":   r["label"],
        "params":  p,
        "full":    r,
        "is":      is_r,
        "oos":     oos_r,
    }
    is_oos_results.append(row)
    print(
        f"  {r['label']:55s}\n"
        f"    FULL n={r['pbl_n']:4d} PF={r['pbl_pf']:.4f} WR={r['pbl_wr']*100:.1f}%\n"
        f"    IS   n={is_r['pbl_n']:4d} PF={is_r['pbl_pf']:.4f} WR={is_r['pbl_wr']*100:.1f}%\n"
        f"    OOS  n={oos_r['pbl_n']:4d} PF={oos_r['pbl_pf']:.4f} WR={oos_r['pbl_wr']*100:.1f}%\n",
        flush=True,
    )
print()


# ── STAGE 3: Combined PBL+SLC via runner.run() ──────────────────────────────────
# Run full BacktestRunner for top 5 candidates to get combined PF
# This is the "exact" reference that matches the original pbl_slc mode.
print("=== STAGE 3: COMBINED PBL+SLC (runner.run, n_workers=2) ===", flush=True)
print("  (each run uses parallel precompute with 2 workers — may take a few minutes)", flush=True)

def _runner_params(p):
    """Convert fast-grid param dict to BacktestRunner settings keys."""
    return {
        "mr_pbl_slc.pullback_long.sl_atr_mult":       p.get("sl_atr_mult",        2.5),
        "mr_pbl_slc.pullback_long.tp_atr_mult":       p.get("tp_atr_mult",        3.0),
        "mr_pbl_slc.pullback_long.ema_prox_atr_mult": p.get("ema_prox_atr_mult",  0.5),
        "mr_pbl_slc.pullback_long.rsi_min":           p.get("rsi_min",           40.0),
        "mr_pbl_slc.pullback_long.wick_strength":     p.get("wick_strength",      1.0),
    }

# Baseline combined first (for delta reference)
print("\n  Running BASELINE combined…", flush=True)
t_base = time.time()
res_base = runner.run(cost_per_side=COST, params={}, n_workers=2)
print(f"  BASELINE combined: PF={res_base['profit_factor']:.4f}  "
      f"PBL_PF={res_base.get('pbl_pf',0):.4f}  n={res_base['n_trades']}  "
      f"({time.time()-t_base:.1f}s)", flush=True)

stage3 = []
for row in is_oos_results:
    rp = _runner_params(row["params"])
    print(f"\n  Running combined: {row['label']}…", flush=True)
    t_run = time.time()
    try:
        res = runner.run(cost_per_side=COST, params=rp, n_workers=2)
        delta = round(res["profit_factor"] - res_base["profit_factor"], 4)
        print(
            f"    FULL_PF={res['profit_factor']:.4f}  CAGR={res['cagr']:.2f}%  "
            f"WR={res['win_rate']*100:.1f}%  MDD={res['max_drawdown']:.2f}%  "
            f"n={res['n_trades']}  delta_vs_baseline={delta:+.4f}  "
            f"({time.time()-t_run:.1f}s)",
            flush=True,
        )
        entry = {
            "label":    row["label"],
            "params":   row["params"],
            "fast_pbl": row["full"],
            "fast_is":  row["is"],
            "fast_oos": row["oos"],
            "combined": {
                "full_pf": res["profit_factor"],
                "cagr":    res["cagr"],
                "wr":      res["win_rate"],
                "mdd":     res["max_drawdown"],
                "n":       res["n_trades"],
                "pbl_pf":  res.get("pbl_pf", None),
                "pbl_n":   res.get("pbl_n",  None),
                "slc_n":   res.get("slc_n",  None),
                "delta":   delta,
            },
        }
        stage3.append(entry)
    except Exception as exc:
        print(f"    ERROR: {exc}", flush=True)
        stage3.append({"label": row["label"], "params": row["params"],
                       "error": str(exc)})

# ── Summary ────────────────────────────────────────────────────────────────────
print("\n=== FINAL SUMMARY ===", flush=True)
print(f"  Baseline:  PBL_PF={baseline['pbl_pf']:.4f}  WR={baseline['pbl_wr']*100:.1f}%  n={baseline['pbl_n']}", flush=True)
print(f"  Best s1:   {best_s1['label']}  PF={best_s1['pbl_pf']:.4f}", flush=True)
print(f"  Best s2:   {best_s2['label']}  PF={best_s2['pbl_pf']:.4f}", flush=True)

if stage3:
    best_comb = max(stage3, key=lambda x: x.get("combined", {}).get("full_pf", 0))
    print(f"  Best combined: {best_comb['label']}  "
          f"FULL_PF={best_comb.get('combined',{}).get('full_pf', 'N/A')}", flush=True)

# ── Save results ───────────────────────────────────────────────────────────────
out = {
    "baseline":        baseline,
    "n_candidates":    len(all_candidates),
    "n_candidates_is": len(candidates_is),
    "n_candidates_oos":len(candidates_oos),
    "stage1":          stage1,
    "stage2":          stage2,
    "top5_is_oos":     is_oos_results,
    "stage3_combined": stage3,
    "best_s1":         best_s1,
    "best_s2":         best_s2,
    "baseline_combined": {
        "full_pf": res_base.get("profit_factor"),
        "pbl_pf":  res_base.get("pbl_pf"),
        "slc_n":   res_base.get("slc_n"),
        "n":       res_base.get("n_trades"),
        "cagr":    res_base.get("cagr"),
        "wr":      res_base.get("win_rate"),
        "mdd":     res_base.get("max_drawdown"),
    },
}
with open("/sessions/wizardly-dreamy-pasteur/pbl_fast_grid_out.json", "w") as f:
    json.dump(out, f, indent=2, default=str)

print("\nResults saved → pbl_fast_grid_out.json", flush=True)
print(f"Total elapsed: {time.time()-t0:.1f}s", flush=True)
