"""
NexusTrader — Full Exhaustive Optimization Runner
==================================================
Run this DIRECTLY on your Windows machine from the NexusTrader root directory:

    cd C:\\path\\to\\NexusTrader
    python scripts\\run_optimization.py

Hardware targets:
  - CPU: All available cores via ProcessPoolExecutor (default: cpu_count - 1)
  - GPU: RTX 4070 via PyTorch CUDA for vectorized regime/signal pre-computation
  - RAM: Indicator DataFrames held in memory across all workers

Architecture:
  1. Load + indicator-compute all TF parquets (once per TF)
  2. GPU pre-computation: regime array + signal direction/score for every bar
     across ALL parameter combinations simultaneously via batched CUDA tensor ops
  3. CPU parallel simulation: ProcessPoolExecutor runs trade simulations
     across (config, TF) pairs concurrently — workers receive pre-computed arrays
  4. Walk-forward validation on top-N configs per TF
  5. Multi-TF confirmation matrix (all combinations)
  6. PF >= 1.60 assessment + root-cause if unachievable

Output:
  - reports/optimization/optimization_results_{date}.json
  - Live leaderboard printed to stdout

GPU speedup comes from steps 2 (batched matrix ops across all configs × all bars
simultaneously). CPU parallelism handles the sequential simulation loop across
many independent (config, TF) pairs. Both axes are exploited.
"""

from __future__ import annotations

import os
import sys
import time
import json
import math
import logging
import warnings
import itertools
import statistics
import datetime
import multiprocessing as mp
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")
logging.basicConfig(level=logging.WARNING)

# ─── Paths ─────────────────────────────────────────────────────────────────
ROOT        = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
BACKTEST_DIR = ROOT / "backtest_data"
REPORT_DIR   = ROOT / "reports" / "optimization"
REPORT_DIR.mkdir(parents=True, exist_ok=True)

# ─── Hardware detection ────────────────────────────────────────────────────
N_CPU_WORKERS = max(1, (os.cpu_count() or 2) - 1)

GPU_AVAILABLE = False
GPU_NAME = "None"
try:
    import torch
    if torch.cuda.is_available():
        GPU_AVAILABLE = True
        GPU_NAME = torch.cuda.get_device_name(0)
        torch.backends.cuda.matmul.allow_tf32 = True
except ImportError:
    pass

# Banner printed inside main() only — keeps it out of spawned child processes.

# ─── Regime integer encoding ────────────────────────────────────────────────
# 0=uncertain 1=bull_trend 2=bear_trend 3=volatility_expansion
# 4=volatility_compression 5=ranging
REGIME_LABELS = ["uncertain", "bull_trend", "bear_trend",
                 "volatility_expansion", "volatility_compression", "ranging"]
R_UNC, R_BULL, R_BEAR, R_VEXP, R_VCOM, R_RANG = 0, 1, 2, 3, 4, 5

# ATR SL multipliers by regime (matches BaseSubModel.REGIME_ATR_MULTIPLIERS)
REGIME_ATR_SL = np.array([2.5, 1.875, 1.875, 3.75, 2.25, 3.125], dtype=np.float32)

# ─── Default parameters (mirrors live system) ─────────────────────────────
DEFAULTS: dict[str, Any] = dict(
    confluence_threshold  = 0.45,
    adx_trend_thresh      = 25.0,
    adx_ranging_thresh    = 20.0,
    trend_adx_min         = 25.0,
    trend_rsi_long_min    = 45.0,
    trend_rsi_long_max    = 70.0,
    trend_rsi_short_min   = 30.0,
    trend_rsi_short_max   = 55.0,
    trend_strength_base   = 0.15,
    trend_adx_bonus_max   = 0.40,
    mb_lookback           = 20,
    mb_vol_mult_min       = 1.5,
    mb_rsi_bullish        = 55.0,
    mb_rsi_bearish        = 45.0,
    mb_strength_base      = 0.35,
    bb_expansion_ratio    = 2.5,
    bb_compression_ratio  = 0.5,
    atr_sl_mult_override  = None,   # None = REGIME_ATR_SL table
    trend_bull_only       = False,  # True = TrendModel only in bull_trend
    fee_pct               = 0.04,
    slippage_pct          = 0.05,
    initial_capital       = 10_000.0,
    warmup_bars           = 100,
)

# ─── Full parameter grid ────────────────────────────────────────────────────
PARAM_GRID: dict[str, list] = dict(
    confluence_threshold  = [0.30, 0.35, 0.40, 0.45, 0.50, 0.55, 0.60, 0.65, 0.70],
    adx_trend_thresh      = [20.0, 22.5, 25.0, 27.5, 30.0, 33.0],
    trend_adx_min         = [20.0, 22.0, 25.0, 28.0, 32.0],
    atr_sl_mult_override  = [1.25, 1.50, 1.875, 2.50, 3.00, 3.75, 4.50],
    bb_expansion_ratio    = [1.8,  2.0,  2.5,   3.0,  3.5],
    mb_vol_mult_min       = [1.0,  1.5,  2.0,   2.5],
    trend_bull_only       = [False, True],
)

def build_grid() -> list[dict]:
    keys   = list(PARAM_GRID.keys())
    values = list(PARAM_GRID.values())
    out = []
    for combo in itertools.product(*values):
        p = dict(DEFAULTS)
        p.update(dict(zip(keys, combo)))
        p["adx_ranging_thresh"] = p["adx_trend_thresh"] - 5.0
        out.append(p)
    return out


# ─────────────────────────────────────────────────────────────────────────────
# GPU batch pre-computation
# Computes regime int-code and signal direction/score for every bar,
# for ALL configs simultaneously using broadcast CUDA tensor operations.
# Memory: n_cfg × n_bars × (int8 + float32 + int8) ≈ manageable for RTX 4070
# ─────────────────────────────────────────────────────────────────────────────

def gpu_precompute(df: pd.DataFrame, configs: list[dict]) -> tuple:
    import torch
    device  = torch.device("cuda")
    n_bars  = len(df)
    n_cfg   = len(configs)
    CHUNK   = 2000   # process configs in chunks to stay within VRAM

    # Shared indicator tensors [n_bars]
    def T(col, dtype=torch.float32):
        v = df[col].values.astype(np.float32)
        return torch.tensor(v, dtype=dtype, device=device)

    adx_t    = T("adx");  ema9_t = T("ema_9");  ema21_t = T("ema_21")
    rsi_t    = T("rsi");  cl_t   = T("close");  vol_t   = T("volume")
    open_t   = T("open"); atr_t  = T("atr")

    # BB ratio
    bbw = df["bb_width"].values.astype(np.float32)
    bbr_np = bbw / np.where(
        pd.Series(bbw).rolling(20, min_periods=5).mean().values > 0,
        pd.Series(bbw).rolling(20, min_periods=5).mean().values, 1.0)
    bbr_t = torch.tensor(bbr_np.astype(np.float32), device=device)

    # Rolling range for MomentumBreakout
    lookback = 20
    cl_np  = df["close"].values.astype(np.float32)
    vol_np = df["volume"].values.astype(np.float32)
    rh_t   = torch.tensor(pd.Series(cl_np).shift(1).rolling(lookback, min_periods=lookback).max().values.astype(np.float32), device=device)
    rl_t   = torch.tensor(pd.Series(cl_np).shift(1).rolling(lookback, min_periods=lookback).min().values.astype(np.float32), device=device)
    va_t   = torch.tensor(pd.Series(vol_np).rolling(lookback, min_periods=lookback).mean().values.astype(np.float32), device=device)

    adx_nan = torch.isnan(adx_t)   # [n_bars]
    ema_up  = ema9_t > ema21_t      # [n_bars]

    # Accumulate outputs
    out_regime = np.zeros((n_cfg, n_bars), dtype=np.int8)
    out_score  = np.zeros((n_cfg, n_bars), dtype=np.float32)
    out_dir    = np.zeros((n_cfg, n_bars), dtype=np.int8)

    for chunk_start in range(0, n_cfg, CHUNK):
        chunk_cfgs = configs[chunk_start : chunk_start + CHUNK]
        nc = len(chunk_cfgs)

        def P(key, dtype=torch.float32):
            return torch.tensor([c[key] for c in chunk_cfgs], dtype=dtype, device=device).unsqueeze(1)  # [nc,1]

        adx_thresh  = P("adx_trend_thresh")
        adx_range   = adx_thresh - 5.0
        bb_exp      = P("bb_expansion_ratio")
        adx_sig     = P("trend_adx_min")
        rsi_lmin    = P("trend_rsi_long_min");   rsi_lmax = P("trend_rsi_long_max")
        rsi_smin    = P("trend_rsi_short_min");  rsi_smax = P("trend_rsi_short_max")
        mb_vol      = P("mb_vol_mult_min")
        mb_rbull    = P("mb_rsi_bullish");        mb_rbear = P("mb_rsi_bearish")
        str_base    = P("trend_strength_base");  adx_bmax = P("trend_adx_bonus_max")
        mb_str      = P("mb_strength_base")
        bull_only   = torch.tensor([c["trend_bull_only"] for c in chunk_cfgs], dtype=torch.bool, device=device).unsqueeze(1)

        # Broadcast: [nc, n_bars]
        adx_B = adx_t.unsqueeze(0);  ema_up_B = ema_up.unsqueeze(0)
        rsi_B = rsi_t.unsqueeze(0);  bbr_B    = bbr_t.unsqueeze(0)
        cl_B  = cl_t.unsqueeze(0);   vol_B    = vol_t.unsqueeze(0)
        rh_B  = rh_t.unsqueeze(0);   rl_B     = rl_t.unsqueeze(0)
        va_B  = va_t.unsqueeze(0);   nan_B    = adx_nan.unsqueeze(0)

        # ── Regime ──────────────────────────────────────────────────────
        regime = torch.full((nc, n_bars), R_UNC, dtype=torch.int8, device=device)
        trend_m = ~nan_B & (adx_B >= adx_thresh)
        range_m = ~nan_B & (adx_B < adx_range)
        dead_m  = ~nan_B & (adx_B >= adx_range) & (adx_B < adx_thresh)
        # Boolean-indexed assignment requires exact shape match in PyTorch (no broadcast).
        # range_m/dead_m/trend_m/bb_exp involve a per-config [nc,1] param → produce [nc,n] ✅
        # bbr_B < 0.5 and nan_B are [1,n] (no per-config param) → use torch.where ✅
        regime[range_m | dead_m]    = R_RANG
        regime[trend_m &  ema_up_B] = R_BULL
        regime[trend_m & ~ema_up_B] = R_BEAR
        regime[bbr_B > bb_exp]      = R_VEXP
        regime = torch.where(bbr_B < 0.5,
                             torch.full_like(regime, R_VCOM), regime)
        regime = torch.where(nan_B,
                             torch.full_like(regime, R_UNC),  regime)

        # ── TrendModel signals ──────────────────────────────────────────
        in_bull = (regime == R_BULL)
        in_bear = (regime == R_BEAR)
        in_vol  = (regime == R_VEXP)
        allowed = torch.where(bull_only, in_bull, in_bull | in_bear)

        has_adx   = ~nan_B & (adx_B >= adx_sig)
        adx_bonus = torch.minimum(adx_bmax,
                                  torch.clamp(adx_B - adx_sig, min=0.0) / (adx_sig + 1e-9) * adx_bmax)
        t_score   = str_base + adx_bonus

        tl = allowed & has_adx &  ema_up_B & (rsi_B >= rsi_lmin) & (rsi_B <= rsi_lmax)
        ts = in_bear  & has_adx & ~ema_up_B & (rsi_B >= rsi_smin) & (rsi_B <= rsi_smax) & ~bull_only

        score = torch.zeros(nc, n_bars, device=device)
        direc = torch.zeros(nc, n_bars, dtype=torch.int8, device=device)
        score = torch.where(tl | ts, torch.maximum(score, t_score), score)
        direc = torch.where(tl,  torch.ones_like(direc), direc)
        direc = torch.where(ts, -torch.ones_like(direc), direc)

        # ── MomentumBreakout signals ─────────────────────────────────────
        vol_pass = (va_B > 0) & (vol_B > va_B * mb_vol)
        with torch.no_grad():
            bp = torch.clamp((cl_B - rh_B) / (rh_B + 1e-9) * 100.0, min=0.0)
            vr = torch.clamp((vol_B / (va_B + 1e-9) - mb_vol) / (mb_vol + 1e-9), 0.0, 1.0)
            mb_s = mb_str + vr * 0.35 + torch.clamp(bp / 2.0, 0.0, 1.0) * 0.30
        mbl = in_vol & vol_pass & (cl_B > rh_B) & (rsi_B > mb_rbull)
        mbs = in_vol & vol_pass & (cl_B < rl_B) & (rsi_B < mb_rbear)
        score = torch.where(mbl | mbs, torch.maximum(score, mb_s), score)
        direc = torch.where(mbl,  torch.ones_like(direc), direc)
        direc = torch.where(mbs, -torch.ones_like(direc), direc)

        out_regime[chunk_start:chunk_start+nc] = regime.cpu().numpy()
        out_score [chunk_start:chunk_start+nc] = score.cpu().numpy()
        out_dir   [chunk_start:chunk_start+nc] = direc.cpu().numpy()

    return out_regime, out_score, out_dir


def cpu_precompute(df: pd.DataFrame, configs: list[dict]) -> tuple:
    """CPU numpy fallback — same logic as GPU version."""
    n_bars = len(df);  n_cfg = len(configs)
    adx  = df["adx"].values.astype(np.float32)
    ema9 = df["ema_9"].values.astype(np.float32)
    ema21= df["ema_21"].values.astype(np.float32)
    rsi  = df["rsi"].values.astype(np.float32)
    cl   = df["close"].values.astype(np.float32)
    vol  = df["volume"].values.astype(np.float32)
    bbw  = df["bb_width"].values.astype(np.float32)

    bbr  = bbw / np.where(pd.Series(bbw).rolling(20, min_periods=5).mean().values > 0,
                           pd.Series(bbw).rolling(20, min_periods=5).mean().values, 1.0)
    lookback = 20
    rh = pd.Series(cl).shift(1).rolling(lookback, min_periods=lookback).max().values.astype(np.float32)
    rl = pd.Series(cl).shift(1).rolling(lookback, min_periods=lookback).min().values.astype(np.float32)
    va = pd.Series(vol).rolling(lookback, min_periods=lookback).mean().values.astype(np.float32)

    nan_m = np.isnan(adx); ema_up = ema9 > ema21
    out_r = np.zeros((n_cfg, n_bars), dtype=np.int8)
    out_s = np.zeros((n_cfg, n_bars), dtype=np.float32)
    out_d = np.zeros((n_cfg, n_bars), dtype=np.int8)

    for ci, c in enumerate(configs):
        at = c["adx_trend_thresh"]; ar = at - 5.0
        r  = np.full(n_bars, R_UNC, dtype=np.int8)
        r[(~nan_m) & (adx < ar)] = R_RANG
        r[(~nan_m) & (adx >= ar) & (adx < at)] = R_RANG
        r[(~nan_m) & (adx >= at) &  ema_up] = R_BULL
        r[(~nan_m) & (adx >= at) & ~ema_up] = R_BEAR
        r[bbr > c["bb_expansion_ratio"]]  = R_VEXP
        r[bbr < c["bb_compression_ratio"]] = R_VCOM
        r[nan_m] = R_UNC

        in_bull = r == R_BULL; in_bear = r == R_BEAR; in_vol = r == R_VEXP
        allowed = in_bull if c["trend_bull_only"] else (in_bull | in_bear)
        has_adx = (~nan_m) & (adx >= c["trend_adx_min"])
        adx_bon = np.minimum(c["trend_adx_bonus_max"],
                             np.where(has_adx, (adx - c["trend_adx_min"]) / (c["trend_adx_min"]+1e-9) * c["trend_adx_bonus_max"], 0.0))
        ts_score = c["trend_strength_base"] + adx_bon

        tl = allowed & has_adx & ema_up  & (rsi >= c["trend_rsi_long_min"])  & (rsi <= c["trend_rsi_long_max"])
        ts = in_bear  & has_adx & ~ema_up & (rsi >= c["trend_rsi_short_min"]) & (rsi <= c["trend_rsi_short_max"])
        if c["trend_bull_only"]: ts[:] = False

        sc = np.zeros(n_bars, np.float32); di = np.zeros(n_bars, np.int8)
        sc = np.where(tl | ts, np.maximum(sc, ts_score), sc)
        di = np.where(tl, np.int8(1), np.where(ts, np.int8(-1), di))

        with np.errstate(divide='ignore', invalid='ignore'):
            bp = np.clip((cl - rh) / (rh + 1e-9) * 100.0, 0, None)
        vr = np.clip((vol / (va + 1e-9) - c["mb_vol_mult_min"]) / (c["mb_vol_mult_min"]+1e-9), 0, 1.0)
        mb_s = c["mb_strength_base"] + vr * 0.35 + np.clip(bp / 2.0, 0, 1.0) * 0.30
        vp   = (va > 0) & (vol > va * c["mb_vol_mult_min"])
        mbl  = in_vol & vp & (cl > rh) & (rsi > c["mb_rsi_bullish"])
        mbs  = in_vol & vp & (cl < rl) & (rsi < c["mb_rsi_bearish"])
        sc   = np.where(mbl | mbs, np.maximum(sc, mb_s), sc)
        di   = np.where(mbl, np.int8(1), np.where(mbs, np.int8(-1), di))

        out_r[ci] = r; out_s[ci] = sc; out_d[ci] = di

    return out_r, out_s, out_d


# ─────────────────────────────────────────────────────────────────────────────
# Sequential simulation kernel (one config, one TF)
# Must be sequential — each bar depends on current equity/position state.
# This runs in parallel ACROSS configs via ProcessPoolExecutor.
# ─────────────────────────────────────────────────────────────────────────────

def simulate_one(open_, high_, low_, atr_, regime_row, score_row, dir_row, params):
    n       = len(open_)
    thresh  = float(params["confluence_threshold"])
    fee     = float(params["fee_pct"]) / 100.0
    slip    = float(params["slippage_pct"]) / 100.0
    capital = float(params["initial_capital"])
    warmup  = int(params["warmup_bars"])
    sl_ovr  = params.get("atr_sl_mult_override")

    trades = []
    equity = capital
    position = None    # (direction, entry_i, entry_px, qty, sl, tp, regime, score)
    pending  = None    # (direction, sl, tp, regime, score)

    for i in range(1, n):
        # Fill pending order at next bar's open
        if pending is not None and position is None:
            d, psl, ptp, pr, ps = pending
            px   = float(open_[i])
            fill = px * (1 + slip) if d == 1 else px * (1 - slip)
            tv   = equity * 0.10
            if tv > 0 and fill > 0:
                qty     = tv / fill
                ef      = tv * fee
                equity -= ef
                position = (d, i, fill, qty, psl, ptp, int(pr), float(ps))
            pending = None

        # Check SL / TP
        if position is not None:
            d, ei, epx, qty, sl, tp, pr, ps = position
            exit_px = 0.0; reason = ""; triggered = False
            if d == 1:
                if float(low_[i]) <= sl:  exit_px, reason, triggered = sl, "SL", True
                elif float(high_[i]) >= tp: exit_px, reason, triggered = tp, "TP", True
            else:
                if float(high_[i]) >= sl:  exit_px, reason, triggered = sl, "SL", True
                elif float(low_[i]) <= tp:  exit_px, reason, triggered = tp, "TP", True
            if triggered:
                fe  = exit_px * (1-slip) if d==1 else exit_px * (1+slip)
                xf  = fe * qty * fee
                pnl = ((fe - epx) * qty - xf) if d==1 else ((epx - fe) * qty - xf)
                equity += pnl
                trades.append((d, pr, ps, pnl, i - ei, reason))
                position = None

        # New entry signal
        if position is None and pending is None and i >= warmup:
            sc_i = float(score_row[i])
            di_i = int(dir_row[i])
            if di_i != 0 and sc_i >= thresh:
                ri   = int(regime_row[i])
                atr_i= float(atr_[i]) if not math.isnan(float(atr_[i])) else float(open_[i]) * 0.01
                sl_m = float(sl_ovr) if sl_ovr is not None else float(REGIME_ATR_SL[min(ri, 5)])
                px_e = float(open_[i])
                if di_i == 1:
                    psl = px_e - atr_i * sl_m
                    ptp = px_e + atr_i * (sl_m + 1.0)
                else:
                    psl = px_e + atr_i * sl_m
                    ptp = px_e - atr_i * (sl_m + 1.0)
                pending = (di_i, psl, ptp, ri, sc_i)

    # Force-close at final bar
    if position is not None:
        d, ei, epx, qty, sl, tp, pr, ps = position
        fe  = float(open_[-1]) * (1-slip) if d==1 else float(open_[-1]) * (1+slip)
        xf  = fe * qty * fee
        pnl = (fe - epx) * qty - xf if d==1 else (epx - fe) * qty - xf
        equity += pnl
        trades.append((d, pr, ps, pnl, n-1-ei, "EOD"))

    return trades


def calc_metrics(trades, capital=10_000.0):
    if not trades:
        return dict(n=0, wr=0.0, pf=0.0, ret=0.0, dd=0.0, sharpe=0.0)
    pnls  = [t[3] for t in trades]
    wins  = [p for p in pnls if p > 0]
    losses= [p for p in pnls if p <= 0]
    gw    = sum(wins); gl = abs(sum(losses))
    net   = sum(pnls)
    eq    = capital; pk = capital; mdd = 0.0
    for p in pnls:
        eq += p
        if eq > pk: pk = eq
        dd = pk - eq
        if dd > mdd: mdd = dd
    avg = statistics.mean(pnls)
    std = statistics.stdev(pnls) if len(pnls) > 1 else 1e-9
    return dict(n=len(trades), wr=len(wins)/len(trades)*100,
                pf=gw/gl if gl>0 else 999.0, ret=net/capital*100.0,
                dd=mdd/capital*100.0, sharpe=avg/std)


def _worker(args):
    """Top-level picklable worker."""
    (ci, tf, open_, high_, low_, atr_,
     r_row, s_row, d_row, params) = args
    trades = simulate_one(open_, high_, low_, atr_, r_row, s_row, d_row, params)
    m = calc_metrics(trades, params["initial_capital"])
    return ci, tf, m


# ─────────────────────────────────────────────────────────────────────────────
# Walk-forward (5 rolling folds × 18-mo train / 6-mo test)
# ─────────────────────────────────────────────────────────────────────────────

FOLDS = [
    ("F1", "2022-03-22", "2023-09-22", "2023-09-22", "2024-03-22"),
    ("F2", "2022-09-22", "2024-03-22", "2024-03-22", "2024-09-22"),
    ("F3", "2023-03-22", "2024-09-22", "2024-09-22", "2025-03-22"),
    ("F4", "2023-09-22", "2025-03-22", "2025-03-22", "2025-09-22"),
    ("F5", "2024-03-22", "2025-09-22", "2025-09-22", "2026-03-21"),
]

def walk_forward(df: pd.DataFrame, params: dict) -> dict:
    results = []
    for fname, _, _, te_s, te_e in FOLDS:
        df_te = df[(df.index >= te_s) & (df.index < te_e)]
        if len(df_te) < 50: continue
        r_m, s_m, d_m = cpu_precompute(df_te, [params])
        trades = simulate_one(
            df_te["open"].values.astype(np.float32),
            df_te["high"].values.astype(np.float32),
            df_te["low"].values.astype(np.float32),
            df_te["atr"].values.astype(np.float32),
            r_m[0], s_m[0], d_m[0], params)
        m = calc_metrics(trades, params["initial_capital"])
        results.append({"fold": fname, **m})
    if not results: return {}
    pfs = [r["pf"] for r in results]
    return dict(folds=results, n=len(results),
                profitable=sum(1 for p in pfs if p > 1.0),
                median_pf=statistics.median(pfs),
                min_pf=min(pfs), max_pf=max(pfs),
                std_pf=statistics.stdev(pfs) if len(pfs)>1 else 0)


# ─────────────────────────────────────────────────────────────────────────────
# Multi-TF confirmation matrix
# ─────────────────────────────────────────────────────────────────────────────

MTF_COMBOS = [
    ("5m", "15m"), ("5m", "1h"),
    ("15m", "1h"), ("15m", "4h"),
    ("30m", "1h"), ("30m", "4h"),
    ("1h",  "4h"),
]
HTF_ALLOWED_DEFAULT = [R_BULL, R_BEAR, R_VEXP]

def run_mtf(df_ltf, df_htf, params, htf_allowed=None):
    if htf_allowed is None: htf_allowed = HTF_ALLOWED_DEFAULT
    r_l, s_l, d_l = cpu_precompute(df_ltf, [params])
    r_h, _, _     = cpu_precompute(df_htf, [params])

    # Forward-fill HTF regime onto LTF index
    htf_ser = pd.Series(r_h[0], index=df_htf.index, dtype=np.int8)
    htf_aln = htf_ser.reindex(df_ltf.index, method='ffill').fillna(R_UNC).values.astype(np.int8)
    htf_pass= np.isin(htf_aln, htf_allowed)

    d_filt = d_l[0].copy()
    d_filt[~htf_pass] = 0

    trades = simulate_one(
        df_ltf["open"].values.astype(np.float32),
        df_ltf["high"].values.astype(np.float32),
        df_ltf["low"].values.astype(np.float32),
        df_ltf["atr"].values.astype(np.float32),
        r_l[0], s_l[0], d_filt, params)
    return calc_metrics(trades, params["initial_capital"])


# ─────────────────────────────────────────────────────────────────────────────
# TF loader — top-level so ProcessPoolExecutor (spawn) can pickle it.
# Caches computed indicator DataFrame to BTC_USDT_{tf}_ind.parquet.
# First run: compute + save (slow once). All subsequent runs: pure parquet I/O.
# ─────────────────────────────────────────────────────────────────────────────

def _load_and_compute_tf(args: tuple):
    tf, backtest_dir_str, root_str = args
    import sys, time
    from pathlib import Path
    import pandas as pd
    _bd   = Path(backtest_dir_str)
    _root = Path(root_str)
    sys.path.insert(0, str(_root))
    from core.features.indicator_library import calculate_all

    cache = _bd / f"BTC_USDT_{tf}_ind.parquet"
    if cache.exists():
        t0 = time.time()
        df = pd.read_parquet(cache)
        return tf, df, f"{len(df):,} bars  [cached {time.time()-t0:.1f}s]"

    fpath = _bd / f"BTC_USDT_{tf}.parquet"
    if not fpath.exists():
        if tf == "30m":
            base = _bd / "BTC_USDT_5m.parquet"
            if base.exists():
                t0 = time.time()
                raw = pd.read_parquet(base).resample("30min").agg(
                    {"open":"first","high":"max","low":"min",
                     "close":"last","volume":"sum"}).dropna()
                dfi = calculate_all(raw)
                dfi.to_parquet(cache)
                return tf, dfi, (f"resampled 5m→30m → {len(dfi):,} bars  "
                                 f"[{time.time()-t0:.1f}s, saved cache]")
        return tf, None, "NOT FOUND — skipped"

    t0  = time.time()
    raw = pd.read_parquet(fpath)
    dfi = calculate_all(raw)
    dfi.to_parquet(cache)
    return tf, dfi, (f"{len(dfi):,} bars  "
                     f"[{time.time()-t0:.1f}s, saved cache]")


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main():
    t_start  = time.time()
    date_str = datetime.datetime.now().strftime("%Y%m%d_%H%M")

    print(f"\n{'='*72}")
    print(f"NEXUSTRADER EXHAUSTIVE OPTIMIZATION RUNNER")
    print(f"{'='*72}")
    print(f"  CPU workers : {N_CPU_WORKERS} of {os.cpu_count()} cores")
    print(f"  GPU         : {GPU_NAME} {'[ENABLED]' if GPU_AVAILABLE else '[not available]'}")
    print(f"  Root        : {ROOT}")
    print(f"{'='*72}\n")

    # ── 1. Load + indicator-compute all TF data ────────────────────────────
    # ThreadPoolExecutor doesn't escape the GIL for pandas Python-level overhead.
    # ProcessPoolExecutor (spawn) gives each TF its own interpreter → true parallelism.
    # Results are cached as BTC_USDT_{tf}_ind.parquet: first run slow, all reruns instant.
    print("[1] Loading + computing indicators (5 parallel processes, cached)...")
    t0_load = time.time()
    dfs: dict[str, pd.DataFrame] = {}
    load_args = [(tf, str(BACKTEST_DIR), str(ROOT))
                 for tf in ["5m", "15m", "30m", "1h", "4h"]]
    with ProcessPoolExecutor(max_workers=5) as lpe:
        load_futs = {lpe.submit(_load_and_compute_tf, a): a[0] for a in load_args}
        for fut in as_completed(load_futs):
            tf_k, df_result, msg = fut.result()
            if df_result is not None:
                dfs[tf_k] = df_result
            print(f"  {tf_k}: {msg}")
    print(f"  → all TFs ready in {time.time()-t0_load:.1f}s")

    tfs = list(dfs.keys())

    # 1m: cannot upsample OHLCV from 5m (fabricates phantom bars, invalidates vol/wick)
    print("\n  NOTE: 1m excluded — upsampling OHLCV from 5m fabricates phantom bars;")
    print("  no tick-data source exists. Data integrity takes precedence over coverage.")

    # ── 2. Build grid ──────────────────────────────────────────────────────
    print("\n[2] Building parameter grid...")
    configs = build_grid()
    total_runs = len(configs) * len(tfs)
    print(f"  Configurations : {len(configs):,}")
    print(f"  TF × config    : {total_runs:,} total simulation runs")
    print(f"  CPU workers    : {N_CPU_WORKERS}")
    print(f"  GPU precompute : {'YES — ' + GPU_NAME if GPU_AVAILABLE else 'NO — CPU fallback'}")

    # ── 3 + 4. Streaming precompute → simulate (chunk by chunk) ──────────
    # Root cause of OOM: pre-allocating np.zeros((75600, 420465)) = 190 GB upfront.
    # Fix: process STREAM_CHUNK configs end-to-end, extract row copies, free immediately.
    # Peak RAM per chunk at 5m: 500 × 420,465 × 6 bytes (int8+float32+int8) = 1.26 GB.
    # GPU VRAM per chunk at 5m: ~3.4 GB of broadcast tensors → safe for RTX 4070 12 GB.
    STREAM_CHUNK = 500

    print(f"\n[3+4] Streaming precompute + simulate "
          f"({STREAM_CHUNK} configs/chunk, {N_CPU_WORKERS} parallel workers)...")

    results_raw: dict[str, list] = {tf: [None] * len(configs) for tf in tfs}
    done = 0; t_sim = time.time(); last_log = time.time()

    with ProcessPoolExecutor(max_workers=N_CPU_WORKERS) as pool:
        for tf, df_i in dfs.items():
            open_ = df_i["open"].values.astype(np.float32)
            high_ = df_i["high"].values.astype(np.float32)
            low_  = df_i["low"].values.astype(np.float32)
            atr_  = df_i["atr"].values.astype(np.float32)
            n_chunks = math.ceil(len(configs) / STREAM_CHUNK)
            print(f"\n  {tf.upper()} — {len(df_i):,} bars, {n_chunks} chunks × {STREAM_CHUNK} configs")

            for chunk_start in range(0, len(configs), STREAM_CHUNK):
                chunk_cfgs = configs[chunk_start : chunk_start + STREAM_CHUNK]
                chunk_idxs = list(range(chunk_start, chunk_start + len(chunk_cfgs)))

                # Precompute regime/score/dir for this chunk only [STREAM_CHUNK, n_bars]
                if GPU_AVAILABLE:
                    try:
                        r_m, s_m, d_m = gpu_precompute(df_i, chunk_cfgs)
                    except Exception as ex:
                        print(f"    GPU error chunk {chunk_start}: {ex} — CPU fallback")
                        r_m, s_m, d_m = cpu_precompute(df_i, chunk_cfgs)
                else:
                    r_m, s_m, d_m = cpu_precompute(df_i, chunk_cfgs)

                # Copy individual rows out, then immediately free the [CHUNK, n_bars] block
                row_data = [(r_m[li].copy(), s_m[li].copy(), d_m[li].copy())
                            for li in range(len(chunk_cfgs))]
                del r_m, s_m, d_m

                # Submit simulations for this chunk
                chunk_futures: dict = {}
                for li, ci in enumerate(chunk_idxs):
                    rr, ss, dd = row_data[li]
                    args = (ci, tf, open_, high_, low_, atr_, rr, ss, dd, configs[ci])
                    chunk_futures[pool.submit(_worker, args)] = (ci, tf)
                del row_data   # args already pickled into worker queue

                # Collect this chunk before moving to the next (keeps memory bounded)
                for future in as_completed(chunk_futures):
                    try:
                        ci, tf_k, m = future.result()
                        results_raw[tf_k][ci] = {"ci": ci, "params": configs[ci], **m}
                    except Exception as ex:
                        print(f"    Worker error: {ex}")
                    done += 1
                    now = time.time()
                    if now - last_log >= 15 or done == total_runs:
                        el   = now - t_sim
                        rate = done / el if el > 0 else 0
                        eta  = (total_runs - done) / rate if rate > 0 else 0
                        pct  = done / total_runs * 100
                        print(f"  [{pct:5.1f}%] {done:,}/{total_runs:,}  "
                              f"{rate:6.0f} runs/s  ETA {eta:.0f}s")
                        last_log = now

    sim_elapsed = time.time() - t_sim
    print(f"\n  Simulation done: {total_runs:,} runs in {sim_elapsed:.1f}s  "
          f"({total_runs/sim_elapsed:.0f} runs/s)")

    # ── 5. Rank ────────────────────────────────────────────────────────────
    print("\n[5] Ranking results (min 20 trades)...")
    MIN_TRADES = 20
    leaderboard: dict[str, list] = {}
    for tf in tfs:
        rows = [r for r in results_raw[tf] if r and r.get("n",0) >= MIN_TRADES]
        rows.sort(key=lambda x: -x["pf"])
        leaderboard[tf] = rows

    print("\n" + "="*72)
    print("SINGLE-TF LEADERBOARD — TOP 10 PER TIMEFRAME (full 4yr, min 20 trades)")
    print("="*72)

    baseline_pf = {}
    for tf in ["4h","1h","30m","15m","5m"]:
        if tf not in leaderboard: continue
        top  = leaderboard[tf][:10]
        # Baseline = config at default params
        def_rows = [r for r in results_raw[tf] if r and
                    r["params"].get("confluence_threshold")==0.45 and
                    r["params"].get("adx_trend_thresh")==25.0 and
                    r["params"].get("trend_adx_min")==25.0 and
                    r["params"].get("atr_sl_mult_override")==None and
                    r["params"].get("trend_bull_only")==False]
        bpf = def_rows[0]["pf"] if def_rows else 0.0
        baseline_pf[tf] = bpf
        best_pf = top[0]["pf"] if top else 0.0
        print(f"\n  {tf.upper()} — baseline PF={bpf:.3f}  best PF={best_pf:.3f}  "
              f"improvement={best_pf-bpf:+.3f}")
        print(f"  {'#':>3} {'thr':>5} {'adxT':>5} {'adxS':>5} {'SL':>6} {'bbE':>5} {'bull':>5} {'mbV':>5} | "
              f"{'n':>5} {'WR':>6} {'PF':>6} {'ret':>8} {'DD':>6}")
        print("  " + "-"*80)
        for rank, r in enumerate(top, 1):
            p = r["params"]
            sl_s = f"{p.get('atr_sl_mult_override','tab')}"
            print(f"  {rank:>3} "
                  f"{p['confluence_threshold']:>5.2f} "
                  f"{p['adx_trend_thresh']:>5.1f} "
                  f"{p['trend_adx_min']:>5.1f} "
                  f"{sl_s:>6} "
                  f"{p['bb_expansion_ratio']:>5.1f} "
                  f"{'Y' if p['trend_bull_only'] else 'N':>5} "
                  f"{p['mb_vol_mult_min']:>5.1f} | "
                  f"{r['n']:>5} {r['wr']:>5.1f}% {r['pf']:>6.3f} "
                  f"{r['ret']:>+7.2f}% {r['dd']:>5.2f}%")

    # ── 6. Walk-forward on top 5 per TF ────────────────────────────────────
    print("\n[6] Walk-forward validation (top 5 per TF)...")
    wf_store = {}
    for tf in ["4h","1h","30m","15m","5m"]:
        if tf not in leaderboard or not leaderboard[tf]: continue
        df_i = dfs[tf]
        print(f"\n  {tf.upper()}:")
        wf_store[tf] = []
        for rank, row in enumerate(leaderboard[tf][:5], 1):
            wf = walk_forward(df_i, row["params"])
            wf_store[tf].append({"rank": rank, "full_pf": row["pf"], "wf": wf})
            med = wf.get("median_pf", 0)
            prof= wf.get("profitable", 0)
            nf  = wf.get("n", 0)
            print(f"    #{rank:1d} full={row['pf']:.3f} → WF med={med:.3f}  "
                  f"profitable={prof}/{nf}  "
                  f"[{wf.get('min_pf',0):.2f}–{wf.get('max_pf',0):.2f}]")

    # ── 7. Multi-TF architecture matrix ────────────────────────────────────
    print("\n[7] Multi-TF confirmation matrix (best 1h params)...")
    best_1h = (leaderboard.get("1h") or [{}])[0]
    mtf_params = best_1h.get("params", DEFAULTS)

    mtf_results = []
    for ltf, htf in MTF_COMBOS:
        if ltf not in dfs or htf not in dfs: continue
        m = run_mtf(dfs[ltf], dfs[htf], mtf_params)
        mtf_results.append({"arch": f"{ltf}+{htf}", **m})
        print(f"  {ltf}+{htf}: n={m['n']:5d}  WR={m['wr']:5.1f}%  "
              f"PF={m['pf']:6.3f}  ret={m['ret']:>+7.2f}%  DD={m['dd']:5.2f}%")
    mtf_results.sort(key=lambda x: -x["pf"])

    # ── 8. PF ≥ 1.60 analysis ──────────────────────────────────────────────
    print("\n[8] PF ≥ 1.60 achievement analysis...")
    all_rows = []
    for tf in tfs:
        for r in leaderboard.get(tf, []):
            all_rows.append({**r, "tf": tf})
    all_rows.sort(key=lambda x: -x.get("pf", 0))

    above_160 = [r for r in all_rows if r.get("pf",0) >= 1.60]
    above_130 = [r for r in all_rows if r.get("pf",0) >= 1.30]
    print(f"  Configs PF ≥ 1.60: {len(above_160):,}")
    print(f"  Configs PF ≥ 1.30: {len(above_130):,}")
    print(f"  Best overall PF  : {all_rows[0]['pf']:.3f} ({all_rows[0]['tf']}) "
          f"n={all_rows[0]['n']}")

    if above_160:
        print("\n  CONFIGS ACHIEVING PF ≥ 1.60 (top 10):")
        for r in above_160[:10]:
            p = r["params"]
            print(f"    {r['tf']:>4}  PF={r['pf']:.3f}  n={r['n']:5d}  "
                  f"thresh={p['confluence_threshold']}  adxT={p['adx_trend_thresh']}  "
                  f"adxS={p['trend_adx_min']}  SL={p['atr_sl_mult_override']}  "
                  f"bull_only={p['trend_bull_only']}")

    # ── 9. Save ────────────────────────────────────────────────────────────
    report = dict(
        date=date_str, n_configs=len(configs), tfs=tfs,
        sim_elapsed_s=round(sim_elapsed,1),
        total_runs=total_runs, throughput_runs_per_s=round(total_runs/sim_elapsed,1),
        gpu_used=GPU_AVAILABLE, gpu_name=GPU_NAME,
        cpu_workers=N_CPU_WORKERS,
        baseline_pf=baseline_pf,
        above_160_count=len(above_160),
        best_pf_overall=all_rows[0].get("pf",0) if all_rows else 0,
        best_overall_top10=[{"tf":r["tf"],"pf":r["pf"],"n":r["n"],"params":{
            k:v for k,v in r["params"].items() if k not in ("fee_pct","slippage_pct","initial_capital","warmup_bars","trend_strength_base","trend_adx_bonus_max","mb_strength_base","mb_rsi_bullish","mb_rsi_bearish","bb_compression_ratio","trend_rsi_short_min","trend_rsi_short_max","trend_rsi_long_min","trend_rsi_long_max","adx_ranging_thresh")
        }} for r in all_rows[:10]],
        leaderboard_top5={tf: leaderboard.get(tf, [])[:5] for tf in tfs},
        wf_results=wf_store,
        mtf_results=mtf_results,
    )
    out_path = REPORT_DIR / f"optimization_results_{date_str}.json"
    with open(out_path, "w") as f:
        json.dump(report, f, indent=2, default=str)

    total_elapsed = time.time() - t_start
    print(f"\n[9] Saved → {out_path}")
    print(f"\n{'='*72}")
    print(f"CAMPAIGN COMPLETE  {total_elapsed:.1f}s total  "
          f"{total_runs/total_elapsed:.0f} runs/s end-to-end")
    print(f"{'='*72}")


if __name__ == "__main__":
    mp.set_start_method("spawn", force=True)
    main()
