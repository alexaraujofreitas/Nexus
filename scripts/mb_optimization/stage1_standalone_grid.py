"""Stage 1: MB standalone grid search - IS period"""
import sys, json, logging, warnings, time, itertools, numpy as np
warnings.filterwarnings("ignore")
logging.basicConfig(level=logging.WARNING)
sys.path.insert(0, "/sessions/wizardly-dreamy-pasteur/mnt/NexusTrader")

from research.engine.backtest_runner import BacktestRunner

IS_START = "2022-03-22"
IS_END   = "2025-09-22"
DM_BASE  = {"disabled_models": ["mean_reversion","liquidity_sweep","trend","donchian_breakout"]}

GRID = {
    "lookback":    [20, 30, 40, 60],
    "vol_mult_min":[1.5, 2.0, 2.5, 3.0],
}
RSI_BULLISH_FIXED = 60

print("Loading data…", flush=True)
r = BacktestRunner(date_start=IS_START, date_end=IS_END, mode="momentum")
r.load_data()
print(f"  {len(r._master_ts)} IS bars loaded", flush=True)

results = []
combos = list(itertools.product(GRID["lookback"], GRID["vol_mult_min"]))
print(f"\nRunning {len(combos)} combos…\n", flush=True)
print(f"{'#':>3} {'lb':>4} {'vm':>5} {'n':>5} {'WR%':>6} {'PF_0':>7} {'PF_fee':>7} {'CAGR%':>6} {'MaxDD%':>7}", flush=True)
print("-"*60, flush=True)

for i, (lb, vm) in enumerate(combos, 1):
    params = {
        **DM_BASE,
        "models.momentum_breakout.lookback":    lb,
        "models.momentum_breakout.vol_mult_min": vm,
        "models.momentum_breakout.rsi_bullish":  RSI_BULLISH_FIXED,
        "models.momentum_breakout.rsi_bearish":  100 - RSI_BULLISH_FIXED,
    }
    t0 = time.time()
    res_0 = r.run(params=params, cost_per_side=0.0, n_workers=1)
    res_f = r.run(params=params, cost_per_side=0.0004, n_workers=1)
    elapsed = time.time() - t0

    row = {
        "lookback": lb, "vol_mult_min": vm,
        "rsi_bullish": RSI_BULLISH_FIXED,
        "n": res_f["n_trades"],
        "wr": res_f["win_rate"],
        "pf_0": res_0["profit_factor"],
        "pf_fee": res_f["profit_factor"],
        "cagr": res_f["cagr"],
        "max_dd": res_f["max_drawdown"],
        "final_eq": res_f["final_equity"],
    }
    results.append(row)
    print(f"{i:3d} lb={lb:2d} vm={vm:.1f} n={row['n']:4d} WR={row['wr']*100:4.1f}% PF_0={row['pf_0']:.4f} PF_fee={row['pf_fee']:.4f} CAGR={row['cagr']*100:5.2f}% MaxDD={row['max_dd']*100:5.2f}% ({elapsed:.0f}s)", flush=True)

for r2 in results:
    r2["score"] = r2["pf_fee"] * (1 - abs(r2["max_dd"]))

import os
os.makedirs("/sessions/wizardly-dreamy-pasteur/mnt/NexusTrader/reports/mb_optimization", exist_ok=True)
with open("/sessions/wizardly-dreamy-pasteur/mnt/NexusTrader/reports/mb_optimization/stage1_grid_results.json", "w") as f:
    json.dump(results, f, indent=2)

results_sorted = sorted(results, key=lambda x: -x["score"])
print("\nTOP 8:", flush=True)
for r2 in results_sorted[:8]:
    print(f"  lb={r2['lookback']:2d} vm={r2['vol_mult_min']:.1f} n={r2['n']:4d} WR={r2['wr']*100:.1f}% PF_fee={r2['pf_fee']:.4f} CAGR={r2['cagr']*100:.2f}% MaxDD={r2['max_dd']*100:.2f}% score={r2['score']:.4f}", flush=True)
print("DONE", flush=True)
