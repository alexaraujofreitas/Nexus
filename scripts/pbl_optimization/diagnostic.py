"""
Phase 1 — PBL Diagnostic Script
Runs pbl_slc baseline, extracts PBL trades, produces full breakdown.
"""
import sys, json, warnings
warnings.filterwarnings("ignore")
sys.path.insert(0, "/sessions/wizardly-dreamy-pasteur/mnt/NexusTrader")

import numpy as np
from research.engine.backtest_runner import BacktestRunner

def pct(x): return f"{x*100:.2f}%"

print("=== PBL DIAGNOSTIC — Phase 1 ===\n")
print("Running pbl_slc baseline (full 4yr, 0.04% fees)…")

runner = BacktestRunner(mode="pbl_slc")
runner.load_data()
res = runner.run(cost_per_side=0.0004)

trades = res["all_trades"]
pbl = [t for t in trades if t["model"] == "pullback_long"]
slc = [t for t in trades if t["model"] == "swing_low_continuation"]

print(f"Total trades: {len(trades)}  PBL: {len(pbl)}  SLC: {len(slc)}")
print(f"Combined PF: {res['profit_factor']}  WR: {pct(res['win_rate'])}  CAGR: {pct(res['cagr'])}  MaxDD: {pct(res['max_drawdown'])}")
print(f"PBL PF: {res['pbl_pf']}  WR: {pct(res['pbl_wr'])}")
print(f"SLC PF: {res['slc_pf']}  WR: {pct(res['slc_wr'])}")
print()

# ── R:R analysis ─────────────────────────────────────────────────────────────
pbl_w = [t for t in pbl if t["pnl"] > 0]
pbl_l = [t for t in pbl if t["pnl"] <= 0]
avg_win_r  = np.mean([t["r_value"] for t in pbl_w]) if pbl_w else 0
avg_loss_r = np.mean([t["r_value"] for t in pbl_l]) if pbl_l else 0
print("── R:R analysis ─────────────────────────────────────────────────────────")
print(f"  Avg winning R:  {avg_win_r:.3f}")
print(f"  Avg losing R:   {avg_loss_r:.3f}")
print(f"  Expected value: {res['pbl_wr']*avg_win_r + (1-res['pbl_wr'])*avg_loss_r:.4f}R")
print(f"  Breakeven R:R needed at WR={pct(res['pbl_wr'])}: {(1-res['pbl_wr'])/res['pbl_wr']:.3f}")
print()

# ── Exit reason breakdown ─────────────────────────────────────────────────────
reasons = {}
for t in pbl:
    reasons[t["exit_reason"]] = reasons.get(t["exit_reason"], 0) + 1
print("── Exit reason breakdown ────────────────────────────────────────────────")
for r, cnt in sorted(reasons.items(), key=lambda x: -x[1]):
    print(f"  {r:15s}  n={cnt:4d}  ({cnt/len(pbl)*100:.1f}%)")
print()

# ── Asset breakdown ───────────────────────────────────────────────────────────
print("── Asset breakdown ──────────────────────────────────────────────────────")
for sym in ["BTC/USDT", "SOL/USDT", "ETH/USDT"]:
    st = [t for t in pbl if t["symbol"] == sym]
    if not st: continue
    sw = [t for t in st if t["pnl"] > 0]
    sl_t = [t for t in st if t["pnl"] <= 0]
    gp = sum(t["pnl"] for t in sw)
    gl = abs(sum(t["pnl"] for t in sl_t))
    pf = gp/gl if gl > 0 else 999
    wr = len(sw)/len(st)
    avg_r = np.mean([t["r_value"] for t in st])
    sl_hit = sum(1 for t in st if t["exit_reason"]=="sl")
    tp_hit = sum(1 for t in st if t["exit_reason"]=="tp")
    print(f"  {sym}: n={len(st):4d}  WR={pct(wr)}  PF={pf:.4f}  AvgR={avg_r:.3f}  SL={sl_hit}({sl_hit/len(st)*100:.0f}%)  TP={tp_hit}({tp_hit/len(st)*100:.0f}%)")
print()

# ── Bars held distribution ────────────────────────────────────────────────────
bars_w = [t["bars_held"] for t in pbl_w]
bars_l = [t["bars_held"] for t in pbl_l]
print("── Bars held (30m bars) ─────────────────────────────────────────────────")
print(f"  Winners:  mean={np.mean(bars_w):.1f}  median={np.median(bars_w):.1f}  p25={np.percentile(bars_w,25):.0f}  p75={np.percentile(bars_w,75):.0f}")
print(f"  Losers:   mean={np.mean(bars_l):.1f}  median={np.median(bars_l):.1f}  p25={np.percentile(bars_l,25):.0f}  p75={np.percentile(bars_l,75):.0f}")
print()

# ── R-value distribution ─────────────────────────────────────────────────────
print("── R-value distribution (PBL all trades) ───────────────────────────────")
r_vals = [t["r_value"] for t in pbl]
percentiles = [10, 25, 50, 75, 90, 95]
pvals = np.percentile(r_vals, percentiles)
for p, v in zip(percentiles, pvals):
    print(f"  p{p:2d}: {v:.3f}R")
print()

# ── Year-by-year breakdown ────────────────────────────────────────────────────
print("── Year-by-year PBL performance ─────────────────────────────────────────")
import pandas as pd
for yr in [2022, 2023, 2024, 2025]:
    yt = [t for t in pbl if str(yr) in str(t["entry_ts"])]
    if not yt: continue
    yw = [t for t in yt if t["pnl"] > 0]
    yl = [t for t in yt if t["pnl"] <= 0]
    gp = sum(t["pnl"] for t in yw)
    gl = abs(sum(t["pnl"] for t in yl))
    pf = gp/gl if gl > 0 else 999
    print(f"  {yr}: n={len(yt):4d}  WR={pct(len(yw)/len(yt))}  PF={pf:.4f}  PnL=${sum(t['pnl'] for t in yt):+,.0f}")
print()

# ── Breakeven analysis at different TP multipliers ───────────────────────────
print("── Breakeven math at different TP multipliers (assuming WR fixed at {:.1f}%) ─".format(res['pbl_wr']*100))
wr = res['pbl_wr']
for tp_r in [2.5, 3.0, 3.5, 4.0, 4.5]:
    sl_r = 2.5  # current default
    ev = wr * tp_r - (1-wr) * sl_r
    pf_est = (wr * tp_r) / ((1-wr) * sl_r) if (1-wr)*sl_r > 0 else 999
    print(f"  tp={tp_r}x sl=2.5x: EV={ev:.3f}R  PF≈{pf_est:.4f}")
print()
for sl_r in [2.0, 2.5, 3.0]:
    for tp_r in [3.0, 3.5, 4.0]:
        pf_est = (wr * tp_r) / ((1-wr) * sl_r) if (1-wr)*sl_r > 0 else 999
        print(f"  tp={tp_r}x sl={sl_r}x: PF≈{pf_est:.4f}  {'✓ >1.05' if pf_est>1.05 else '✗'}")
print()

# ── WR needed per TP/SL combination to hit PF=1.05 ───────────────────────────
print("── WR required to achieve PF=1.05 at each TP/SL combo ──────────────────")
for sl_r in [2.0, 2.5]:
    for tp_r in [3.0, 3.5, 4.0]:
        # PF = WR*tp / (1-WR)*sl = 1.05 → WR = 1.05*sl / (tp + 1.05*sl)
        wr_needed = 1.05*sl_r / (tp_r + 1.05*sl_r)
        print(f"  tp={tp_r}x sl={sl_r}x: WR needed={pct(wr_needed)}  (current={pct(wr)}  delta={pct(wr_needed-wr)})")
print()

# ── Save for report ───────────────────────────────────────────────────────────
diag = {
    "pbl_n": len(pbl),
    "pbl_wr": res["pbl_wr"],
    "pbl_pf": res["pbl_pf"],
    "combined_pf": res["profit_factor"],
    "combined_wr": res["win_rate"],
    "combined_n": res["n_trades"],
    "avg_win_r": round(float(avg_win_r), 4),
    "avg_loss_r": round(float(avg_loss_r), 4),
    "exit_reasons": reasons,
    "by_asset": {},
    "by_year": {},
    "r_percentiles": {f"p{p}": round(float(v), 4) for p, v in zip(percentiles, pvals)},
}
for sym in ["BTC/USDT", "SOL/USDT", "ETH/USDT"]:
    st = [t for t in pbl if t["symbol"] == sym]
    if not st: continue
    sw = [t for t in st if t["pnl"] > 0]
    sl_t = [t for t in st if t["pnl"] <= 0]
    gp = sum(t["pnl"] for t in sw)
    gl = abs(sum(t["pnl"] for t in sl_t))
    diag["by_asset"][sym] = {
        "n": len(st), "wr": round(len(sw)/len(st), 4),
        "pf": round(gp/gl, 4) if gl > 0 else 999,
        "avg_r": round(float(np.mean([t["r_value"] for t in st])), 4),
        "sl_pct": round(sum(1 for t in st if t["exit_reason"]=="sl")/len(st), 4),
        "tp_pct": round(sum(1 for t in st if t["exit_reason"]=="tp")/len(st), 4),
    }
for yr in [2022, 2023, 2024, 2025]:
    yt = [t for t in pbl if str(yr) in str(t["entry_ts"])]
    if not yt: continue
    yw = [t for t in yt if t["pnl"] > 0]
    yl = [t for t in yt if t["pnl"] <= 0]
    gp = sum(t["pnl"] for t in yw)
    gl = abs(sum(t["pnl"] for t in yl))
    diag["by_year"][str(yr)] = {
        "n": len(yt), "wr": round(len(yw)/len(yt), 4),
        "pf": round(gp/gl, 4) if gl > 0 else 999,
        "pnl": round(sum(t["pnl"] for t in yt), 2),
    }

with open("/sessions/wizardly-dreamy-pasteur/pbl_diag_out.json", "w") as f:
    json.dump(diag, f, indent=2)
print("Diagnostic saved → pbl_diag_out.json")
