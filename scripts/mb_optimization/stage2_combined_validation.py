"""
Stage 2: Combined PBL+SLC+MB IS/OOS validation on top Stage 1 candidates.
Also tests HMM confidence gate on best candidate.
"""
import sys, json, logging, warnings, time, numpy as np
warnings.filterwarnings("ignore")
logging.basicConfig(level=logging.WARNING)
sys.path.insert(0, "/sessions/wizardly-dreamy-pasteur/mnt/NexusTrader")

from research.engine.backtest_runner import BacktestRunner

IS_START  = "2022-03-22"
IS_END    = "2025-09-22"
OOS_START = "2025-09-22"
OOS_END   = "2026-03-21"
FULL_START= "2022-03-22"
FULL_END  = "2026-03-21"

DM_BASE = {"disabled_models": ["mean_reversion","liquidity_sweep","trend","donchian_breakout"]}

# ── Top 4 candidates from Stage 1 ─────────────────────────────────────────
CANDIDATES = [
    {"label": "lb=60 vm=1.5 (best CAGR/PF)",    "lookback": 60, "vol_mult_min": 1.5, "hmm_conf": 0.0},
    {"label": "lb=40 vm=2.5 (balanced)",          "lookback": 40, "vol_mult_min": 2.5, "hmm_conf": 0.0},
    {"label": "lb=40 vm=3.0 (lowest MaxDD)",       "lookback": 40, "vol_mult_min": 3.0, "hmm_conf": 0.0},
    {"label": "lb=60 vm=2.0 (2nd best PF)",        "lookback": 60, "vol_mult_min": 2.0, "hmm_conf": 0.0},
    {"label": "lb=60 vm=1.5 conf≥0.6 (gated)",    "lookback": 60, "vol_mult_min": 1.5, "hmm_conf": 0.6},
]
RSI_BULLISH = 60

def mb_params(cand):
    return {
        **DM_BASE,
        "models.momentum_breakout.lookback":    cand["lookback"],
        "models.momentum_breakout.vol_mult_min": cand["vol_mult_min"],
        "models.momentum_breakout.rsi_bullish":  RSI_BULLISH,
        "models.momentum_breakout.rsi_bearish":  100 - RSI_BULLISH,
    }

# ── Pre-load runners ───────────────────────────────────────────────────────
print("Loading runners…", flush=True)
SUBSET = ["pullback_long","swing_low_continuation","momentum_breakout"]

r_is  = BacktestRunner(date_start=IS_START,   date_end=IS_END,   mode="custom", strategy_subset=SUBSET)
r_oos = BacktestRunner(date_start=OOS_START,  date_end=OOS_END,  mode="custom", strategy_subset=SUBSET)
r_full= BacktestRunner(date_start=FULL_START, date_end=FULL_END, mode="custom", strategy_subset=SUBSET)

# IS baseline (PBL+SLC only)
r_base_is  = BacktestRunner(date_start=IS_START,   date_end=IS_END,   mode="pbl_slc")
r_base_oos = BacktestRunner(date_start=OOS_START,  date_end=OOS_END,  mode="pbl_slc")
r_base_full= BacktestRunner(date_start=FULL_START, date_end=FULL_END, mode="pbl_slc")

r_is.load_data()
r_oos.load_data()
r_full.load_data()
r_base_is.load_data()
r_base_oos.load_data()
r_base_full.load_data()
print("All runners ready.\n", flush=True)

# ── PBL+SLC reference baselines ────────────────────────────────────────────
print("=== PBL+SLC BASELINE REFERENCE ===", flush=True)
base_is_res  = r_base_is.run(params=DM_BASE,  cost_per_side=0.0004, n_workers=1)
base_oos_res = r_base_oos.run(params=DM_BASE, cost_per_side=0.0004, n_workers=1)
base_full_res= r_base_full.run(params=DM_BASE,cost_per_side=0.0004, n_workers=1)

def fmt(res):
    calmar = abs(res["cagr"]/res["max_drawdown"]) if res["max_drawdown"] != 0 else 0
    return (f"PF={res['profit_factor']:.4f}  CAGR={res['cagr']*100:.2f}%  "
            f"WR={res['win_rate']*100:.1f}%  MaxDD={res['max_drawdown']*100:.2f}%  "
            f"Calmar={calmar:.2f}  n={res['n_trades']}")

print(f"  IS  ({IS_START}→{IS_END}): {fmt(base_is_res)}", flush=True)
print(f"  OOS ({OOS_START}→{OOS_END}): {fmt(base_oos_res)}", flush=True)
print(f"  FULL({FULL_START}→{FULL_END}): {fmt(base_full_res)}", flush=True)

all_results = []

# ── Candidates ─────────────────────────────────────────────────────────────
for ci, cand in enumerate(CANDIDATES, 1):
    print(f"\n{'='*70}", flush=True)
    print(f"[{ci}/{len(CANDIDATES)}] {cand['label']}", flush=True)
    params = mb_params(cand)
    hmc = cand["hmm_conf"]

    t0 = time.time()
    res_is  = r_is.run(params=params,  cost_per_side=0.0004, n_workers=1, _precomp_sigs_override=None)
    if hmc > 0:
        # Re-create runner with hmm_confidence_min set
        r_is2 = BacktestRunner(date_start=IS_START, date_end=IS_END, mode="custom",
                               strategy_subset=SUBSET, hmm_confidence_min=hmc)
        r_is2._data_loaded = r_is._data_loaded
        r_is2._ind = r_is._ind
        r_is2._reg30 = r_is._reg30; r_is2._reg1h = r_is._reg1h
        r_is2._nx_regime = r_is._nx_regime; r_is2._nx_conf = r_is._nx_conf
        r_is2._hmm = r_is._hmm; r_is2._master_ts = r_is._master_ts
        r_is2._highs = r_is._highs; r_is2._lows = r_is._lows; r_is2._opens = r_is._opens
        res_is = r_is2.run(params=params, cost_per_side=0.0004, n_workers=1)

    res_oos = r_oos.run(params=params, cost_per_side=0.0004, n_workers=1)
    res_full= r_full.run(params=params, cost_per_side=0.0004, n_workers=1)
    elapsed = time.time() - t0

    calmar_is   = abs(res_is["cagr"]/res_is["max_drawdown"])   if res_is["max_drawdown"]   != 0 else 0
    calmar_oos  = abs(res_oos["cagr"]/res_oos["max_drawdown"])  if res_oos["max_drawdown"]  != 0 else 0
    calmar_full = abs(res_full["cagr"]/res_full["max_drawdown"]) if res_full["max_drawdown"] != 0 else 0

    print(f"  IS  : {fmt(res_is)}", flush=True)
    print(f"        MB: n={res_is.get('mb_n',0)} WR={res_is.get('mb_wr',0)*100:.1f}% PF={res_is.get('mb_pf',0):.4f}  PBL delta={res_is.get('pbl_n',0)-base_is_res.get('pbl_n',0):+d}  SLC delta={res_is.get('slc_n',0)-base_is_res.get('slc_n',0):+d}", flush=True)
    print(f"  OOS : {fmt(res_oos)}", flush=True)
    print(f"        MB: n={res_oos.get('mb_n',0)} WR={res_oos.get('mb_wr',0)*100:.1f}% PF={res_oos.get('mb_pf',0):.4f}", flush=True)
    print(f"  FULL: {fmt(res_full)}", flush=True)
    print(f"        MB: n={res_full.get('mb_n',0)} WR={res_full.get('mb_wr',0)*100:.1f}% PF={res_full.get('mb_pf',0):.4f}  ({elapsed:.0f}s total)", flush=True)

    # Compare IS vs baseline
    pf_delta_is   = res_is["profit_factor"]   - base_is_res["profit_factor"]
    cagr_delta_is = (res_is["cagr"] - base_is_res["cagr"]) * 100
    mdd_delta_is  = (res_is["max_drawdown"] - base_is_res["max_drawdown"]) * 100
    print(f"  IS vs baseline: PF{pf_delta_is:+.4f}  CAGR{cagr_delta_is:+.2f}%  MaxDD{mdd_delta_is:+.2f}%", flush=True)

    all_results.append({
        "label": cand["label"],
        "lookback": cand["lookback"],
        "vol_mult_min": cand["vol_mult_min"],
        "hmm_conf": hmc,
        "IS":   {k: round(v*100,2) if k in ("cagr","win_rate","max_drawdown") else round(v,4) if isinstance(v,float) else v
                 for k,v in res_is.items() if k in ("profit_factor","win_rate","cagr","max_drawdown","n_trades","mb_n","mb_pf","mb_wr","pbl_n","slc_n")},
        "OOS":  {k: round(v*100,2) if k in ("cagr","win_rate","max_drawdown") else round(v,4) if isinstance(v,float) else v
                 for k,v in res_oos.items() if k in ("profit_factor","win_rate","cagr","max_drawdown","n_trades","mb_n","mb_pf","mb_wr","pbl_n","slc_n")},
        "FULL": {k: round(v*100,2) if k in ("cagr","win_rate","max_drawdown") else round(v,4) if isinstance(v,float) else v
                 for k,v in res_full.items() if k in ("profit_factor","win_rate","cagr","max_drawdown","n_trades","mb_n","mb_pf","mb_wr","pbl_n","slc_n")},
        "calmar_is": round(calmar_is,3),
        "calmar_oos": round(calmar_oos,3),
        "calmar_full": round(calmar_full,3),
    })

# Save
import os
os.makedirs("/sessions/wizardly-dreamy-pasteur/mnt/NexusTrader/reports/mb_optimization", exist_ok=True)
baseline_row = {
    "IS":  {k: round(v,4) for k,v in base_is_res.items()  if k in ("profit_factor","win_rate","cagr","max_drawdown","n_trades","pbl_n","slc_n")},
    "OOS": {k: round(v,4) for k,v in base_oos_res.items() if k in ("profit_factor","win_rate","cagr","max_drawdown","n_trades","pbl_n","slc_n")},
    "FULL":{k: round(v,4) for k,v in base_full_res.items()if k in ("profit_factor","win_rate","cagr","max_drawdown","n_trades","pbl_n","slc_n")},
}
out = {"baseline_pbl_slc": baseline_row, "candidates": all_results}
with open("/sessions/wizardly-dreamy-pasteur/mnt/NexusTrader/reports/mb_optimization/stage2_combined_results.json","w") as f:
    json.dump(out, f, indent=2)
print("\nSaved → reports/mb_optimization/stage2_combined_results.json", flush=True)
print("DONE", flush=True)
