"""
NexusTrader — Phase 5: Edge Expansion & Robustness
===================================================
Builds on Phase 4 best config: 30m + 4h MTF, bull_only, adxS=32, PF ~1.71-1.83
Tests:
  1. Exit logic — RR TP, ATR trailing, partial exits, time limits
  2. Trade frequency — ADX sweep 28-34
  3. Short side — bear-regime gate only
  4. Agent impact simulation — ATR volatility filter, time-of-day
  5. BTC cycle distribution — per-phase PF breakdown
  6. MTF 30m+4h with best params from above

Run on Windows:
  cd C:\\Users\\alexa\\NexusTrader
  python scripts\\run_phase5_edge_expansion.py
"""

from __future__ import annotations
import os, sys, time, json, math, logging, warnings, statistics, datetime
import multiprocessing as mp
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")
logging.basicConfig(level=logging.WARNING)

ROOT         = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
BACKTEST_DIR = ROOT / "backtest_data"
REPORT_DIR   = ROOT / "reports" / "optimization"
REPORT_DIR.mkdir(parents=True, exist_ok=True)

N_CPU_WORKERS = max(1, (os.cpu_count() or 2) - 1)
GPU_AVAILABLE = False; GPU_NAME = "None"
try:
    import torch
    if torch.cuda.is_available():
        GPU_AVAILABLE = True
        GPU_NAME = torch.cuda.get_device_name(0)
        torch.backends.cuda.matmul.allow_tf32 = True
except ImportError:
    pass

# ── Regime codes ─────────────────────────────────────────────────────────────
R_UNC, R_BULL, R_BEAR, R_VEXP, R_VCOM, R_RANG = 0, 1, 2, 3, 4, 5
REGIME_LABELS = ["uncertain","bull_trend","bear_trend",
                 "volatility_expansion","volatility_compression","ranging"]

# ── Phase 4 best 30m config (baseline) ───────────────────────────────────────
P4_BASE = dict(
    confluence_threshold = 0.50,
    adx_trend_thresh     = 25.0,
    adx_ranging_thresh   = 20.0,
    trend_adx_min        = 32.0,
    trend_rsi_long_min   = 45.0,
    trend_rsi_long_max   = 70.0,
    trend_rsi_short_min  = 30.0,
    trend_rsi_short_max  = 55.0,
    trend_strength_base  = 0.15,
    trend_adx_bonus_max  = 0.40,
    mb_lookback          = 20,
    mb_vol_mult_min      = 1.0,
    mb_rsi_bullish       = 55.0,
    mb_rsi_bearish       = 45.0,
    mb_strength_base     = 0.35,
    bb_expansion_ratio   = 3.5,
    bb_compression_ratio = 0.5,
    atr_sl_mult_override = 4.5,
    trend_bull_only      = True,
    fee_pct              = 0.04,
    slippage_pct         = 0.05,
    initial_capital      = 10_000.0,
    warmup_bars          = 100,
    # Phase 5 fields
    exit_mode        = 'fixed',
    tp_rr            = 1.22,    # baseline: TP = SL_dist * 1.22 (= SL+1 ATR)
    trail_atr_mult   = 1.5,
    trail_act_r      = 1.0,
    partial_r        = 1.0,
    partial_pct      = 0.5,
    max_bars         = None,
    bear_short_gate  = False,
    agent_filter     = None,    # None | 'atr_vol' | 'time_of_day' | 'combined'
    label            = 'p4_baseline',
)

# ── BTC cycle periods ─────────────────────────────────────────────────────────
CYCLES = {
    'bear_2022':      ('2022-03-22', '2022-12-31'),
    'recovery_2023':  ('2023-01-01', '2023-12-31'),
    'bull_2024':      ('2024-01-01', '2024-10-01'),
    'mixed_late2024': ('2024-10-01', '2025-06-30'),
    'recent_2025_26': ('2025-07-01', '2026-03-21'),
}


# ─────────────────────────────────────────────────────────────────────────────
# Loader (top-level for ProcessPoolExecutor spawn)
# ─────────────────────────────────────────────────────────────────────────────

def _load_tf(args):
    tf, bd_str, root_str = args
    import sys, time as _t
    from pathlib import Path
    import pandas as pd
    _bd = Path(bd_str); _root = Path(root_str)
    sys.path.insert(0, str(_root))

    cache = _bd / f"BTC_USDT_{tf}_ind.parquet"
    if cache.exists():
        t0 = _t.time(); df = pd.read_parquet(cache)
        return tf, df, f"{len(df):,} bars [cached {_t.time()-t0:.1f}s]"

    fpath = _bd / f"BTC_USDT_{tf}.parquet"
    if not fpath.exists():
        if tf == "30m":
            base = _bd / "BTC_USDT_5m.parquet"
            if base.exists():
                from core.features.indicator_library import calculate_all
                t0 = _t.time()
                raw = pd.read_parquet(base).resample("30min").agg(
                    {"open":"first","high":"max","low":"min","close":"last","volume":"sum"}
                ).dropna()
                dfi = calculate_all(raw); dfi.to_parquet(cache)
                return tf, dfi, f"{len(dfi):,} bars [computed+cached {_t.time()-t0:.1f}s]"
        return tf, None, "NOT FOUND"

    from core.features.indicator_library import calculate_all
    t0 = _t.time(); raw = pd.read_parquet(fpath); dfi = calculate_all(raw)
    dfi.to_parquet(cache)
    return tf, dfi, f"{len(dfi):,} bars [computed+cached {_t.time()-t0:.1f}s]"


# ─────────────────────────────────────────────────────────────────────────────
# CPU vectorised precompute (regime + signal arrays)
# Identical logic to Phase 4. GPU not needed for ~160 configs.
# ─────────────────────────────────────────────────────────────────────────────

def cpu_precompute(df: pd.DataFrame, configs: list[dict]):
    n_bars = len(df); n_cfg = len(configs)
    adx  = df["adx"].values.astype(np.float32)
    ema9 = df["ema_9"].values.astype(np.float32)
    ema21= df["ema_21"].values.astype(np.float32)
    rsi  = df["rsi"].values.astype(np.float32)
    cl   = df["close"].values.astype(np.float32)
    vol  = df["volume"].values.astype(np.float32)
    bbw  = df["bb_width"].values.astype(np.float32)
    bbr  = bbw / np.where(
        pd.Series(bbw).rolling(20, min_periods=5).mean().values > 0,
        pd.Series(bbw).rolling(20, min_periods=5).mean().values, 1.0)
    rh = pd.Series(cl).shift(1).rolling(20, min_periods=20).max().values.astype(np.float32)
    rl = pd.Series(cl).shift(1).rolling(20, min_periods=20).min().values.astype(np.float32)
    va = pd.Series(vol).rolling(20, min_periods=20).mean().values.astype(np.float32)
    nan_m  = np.isnan(adx); ema_up = ema9 > ema21

    out_r = np.zeros((n_cfg, n_bars), dtype=np.int8)
    out_s = np.zeros((n_cfg, n_bars), dtype=np.float32)
    out_d = np.zeros((n_cfg, n_bars), dtype=np.int8)

    for ci, c in enumerate(configs):
        at = c["adx_trend_thresh"]; ar = at - 5.0
        bb_exp = c["bb_expansion_ratio"]; bb_com = c.get("bb_compression_ratio", 0.5)
        r = np.full(n_bars, R_UNC, dtype=np.int8)
        r[(~nan_m) & (adx < ar)]                   = R_RANG
        r[(~nan_m) & (adx >= ar) & (adx < at)]     = R_RANG
        r[(~nan_m) & (adx >= at) &  ema_up]         = R_BULL
        r[(~nan_m) & (adx >= at) & ~ema_up]         = R_BEAR
        r[bbr > bb_exp]  = R_VEXP
        r[bbr < bb_com]  = R_VCOM
        r[nan_m]         = R_UNC

        in_bull = r == R_BULL; in_bear = r == R_BEAR; in_vol = r == R_VEXP
        allowed  = in_bull if c["trend_bull_only"] else (in_bull | in_bear)
        has_adx  = (~nan_m) & (adx >= c["trend_adx_min"])
        adx_bon  = np.minimum(c["trend_adx_bonus_max"],
                              np.where(has_adx,
                                       (adx - c["trend_adx_min"]) / (c["trend_adx_min"]+1e-9)
                                       * c["trend_adx_bonus_max"], 0.0))
        ts_score = c["trend_strength_base"] + adx_bon
        tl = allowed & has_adx & ema_up  & (rsi >= c["trend_rsi_long_min"])  & (rsi <= c["trend_rsi_long_max"])
        ts = in_bear  & has_adx & ~ema_up & (rsi >= c["trend_rsi_short_min"]) & (rsi <= c["trend_rsi_short_max"])
        if c["trend_bull_only"]: ts[:] = False

        sc = np.zeros(n_bars, np.float32); di = np.zeros(n_bars, np.int8)
        sc = np.where(tl | ts, np.maximum(sc, ts_score), sc)
        di = np.where(tl, np.int8(1), np.where(ts, np.int8(-1), di))

        with np.errstate(divide='ignore', invalid='ignore'):
            bp = np.clip((cl - rh) / (rh + 1e-9) * 100.0, 0, None)
        vr   = np.clip((vol/(va+1e-9) - c["mb_vol_mult_min"])/(c["mb_vol_mult_min"]+1e-9), 0, 1.0)
        mb_s = c["mb_strength_base"] + vr*0.35 + np.clip(bp/2.0, 0, 1.0)*0.30
        vp   = (va > 0) & (vol > va * c["mb_vol_mult_min"])
        mbl  = in_vol & vp & (cl > rh) & (rsi > c["mb_rsi_bullish"])
        mbs  = in_vol & vp & (cl < rl) & (rsi < c["mb_rsi_bearish"])
        sc   = np.where(mbl | mbs, np.maximum(sc, mb_s), sc)
        di   = np.where(mbl, np.int8(1), np.where(mbs, np.int8(-1), di))

        out_r[ci] = r; out_s[ci] = sc; out_d[ci] = di
    return out_r, out_s, out_d


# ─────────────────────────────────────────────────────────────────────────────
# Enhanced simulation kernel
# Supports: fixed | atr_trail | partial | time_limit exit modes
# Supports: bear_short_gate, agent_filter
# Returns list of (d, regime, score, pnl, duration_bars, exit_reason, entry_bar_idx)
# ─────────────────────────────────────────────────────────────────────────────

def simulate_enhanced(open_, high_, low_, atr_, regime_row, score_row, dir_row,
                      hour_row, atr_ma_row, params) -> list:
    n          = len(open_)
    thresh     = float(params['confluence_threshold'])
    fee        = float(params.get('fee_pct', 0.04)) / 100.0
    slip       = float(params.get('slippage_pct', 0.05)) / 100.0
    capital    = float(params.get('initial_capital', 10000.0))
    warmup     = int(params.get('warmup_bars', 100))
    sl_mult    = float(params.get('atr_sl_mult_override', 4.5))

    exit_mode      = params.get('exit_mode', 'fixed')
    tp_rr          = float(params.get('tp_rr', 1.22))
    trail_atr_mult = float(params.get('trail_atr_mult', 1.5))
    trail_act_r    = float(params.get('trail_act_r', 1.0))
    partial_r      = float(params.get('partial_r', 1.0))
    partial_pct    = float(params.get('partial_pct', 0.5))
    max_bars_h     = params.get('max_bars', None)
    bear_sg        = bool(params.get('bear_short_gate', False))
    agent_filter   = params.get('agent_filter', None)

    trades = []; equity = capital
    pos = None; pending = None

    for i in range(1, n):
        # ── Fill pending ──────────────────────────────────────────────────
        if pending is not None and pos is None:
            d   = pending['d']
            px  = float(open_[i])
            fill = px*(1+slip) if d==1 else px*(1-slip)
            tv  = equity * 0.10
            if tv > 0 and fill > 0:
                eq_risk = pending['initial_risk']
                qty  = tv / fill
                equity -= tv * fee
                pos = dict(
                    d=d, entry_i=i, entry_px=fill, qty=qty,
                    sl=pending['sl'], tp=pending['tp'],
                    regime=pending['regime'], score=pending['score'],
                    initial_risk=eq_risk, entry_atr=pending['entry_atr'],
                    trail_stop=pending['sl'], high_water=fill,
                    partial_done=False,
                )
            pending = None

        # ── Manage open position ─────────────────────────────────────────
        if pos is not None:
            d   = pos['d']
            i_lo = float(low_[i]); i_hi = float(high_[i])

            # ATR trailing: ratchet stop behind high-water mark
            if exit_mode == 'atr_trail':
                hw = pos['high_water']
                ts = pos['trail_stop']
                er = pos['initial_risk']
                ea = pos['entry_atr']
                if d == 1:
                    hw = max(hw, i_hi)
                    if hw >= pos['entry_px'] + trail_act_r * er:
                        ts = max(ts, hw - trail_atr_mult * ea)
                else:
                    hw = min(hw, i_lo)
                    if hw <= pos['entry_px'] - trail_act_r * er:
                        ts = min(ts, hw + trail_atr_mult * ea)
                pos['trail_stop'] = ts; pos['high_water'] = hw
                effective_sl = ts
            else:
                effective_sl = pos['sl']

            # Partial exit at partial_r * initial_risk
            if exit_mode == 'partial' and not pos['partial_done']:
                ep = pos['entry_px']; er = pos['initial_risk']
                tgt = ep + partial_r*er if d==1 else ep - partial_r*er
                if (d==1 and i_hi >= tgt) or (d==-1 and i_lo <= tgt):
                    pfe   = tgt*(1-slip) if d==1 else tgt*(1+slip)
                    pqty  = pos['qty'] * partial_pct
                    ppnl  = (pfe-ep)*pqty if d==1 else (ep-pfe)*pqty
                    equity += ppnl
                    trades.append((d, pos['regime'], pos['score'], ppnl,
                                   i-pos['entry_i'], 'PARTIAL', pos['entry_i']))
                    pos['qty']       *= (1 - partial_pct)
                    pos['sl']         = ep        # move SL to breakeven
                    effective_sl      = ep
                    pos['partial_done'] = True

            # Time-limit exit
            if max_bars_h is not None and (i - pos['entry_i']) >= max_bars_h:
                fe  = float(open_[i])*(1-slip) if d==1 else float(open_[i])*(1+slip)
                xf  = fe * pos['qty'] * fee
                pnl = (fe-pos['entry_px'])*pos['qty']-xf if d==1 \
                     else (pos['entry_px']-fe)*pos['qty']-xf
                equity += pnl
                trades.append((d, pos['regime'], pos['score'], pnl,
                               i-pos['entry_i'], 'TIME', pos['entry_i']))
                pos = None; continue

            # SL / TP
            exit_px = 0.0; reason = ""; hit = False
            if d == 1:
                if i_lo <= effective_sl:           exit_px,reason,hit = effective_sl,'SL',True
                elif pos['tp']>0 and i_hi>=pos['tp']: exit_px,reason,hit = pos['tp'],'TP',True
            else:
                if i_hi >= effective_sl:           exit_px,reason,hit = effective_sl,'SL',True
                elif pos['tp']>0 and i_lo<=pos['tp']: exit_px,reason,hit = pos['tp'],'TP',True

            if hit:
                fe  = exit_px*(1-slip) if d==1 else exit_px*(1+slip)
                xf  = fe * pos['qty'] * fee
                pnl = (fe-pos['entry_px'])*pos['qty']-xf if d==1 \
                     else (pos['entry_px']-fe)*pos['qty']-xf
                equity += pnl
                trades.append((d, pos['regime'], pos['score'], pnl,
                               i-pos['entry_i'], reason, pos['entry_i']))
                pos = None

        # ── New entry ────────────────────────────────────────────────────
        if pos is None and pending is None and i >= warmup:
            sc_i = float(score_row[i])
            di_i = int(dir_row[i])
            ri   = int(regime_row[i])

            # Bear-short gate: allow shorts only in bear_trend regime
            if bear_sg and di_i == -1 and ri != R_BEAR:
                continue

            # Agent filters
            if agent_filter == 'atr_vol':
                # Skip entries when ATR is elevated (>1.8× 20-bar mean) — high noise
                atr_ma_i = float(atr_ma_row[i])
                if not math.isnan(atr_ma_i) and float(atr_[i]) > 1.8 * atr_ma_i:
                    continue
            elif agent_filter == 'time_of_day':
                # Only trade UTC 08:00–20:00 (hours 8-19)
                hr = int(hour_row[i])
                if hr < 8 or hr >= 20:
                    continue
            elif agent_filter == 'combined':
                atr_ma_i = float(atr_ma_row[i])
                hr = int(hour_row[i])
                if not math.isnan(atr_ma_i) and float(atr_[i]) > 1.8 * atr_ma_i:
                    continue
                if hr < 8 or hr >= 20:
                    continue

            if di_i != 0 and sc_i >= thresh:
                atr_i = float(atr_[i])
                if math.isnan(atr_i): atr_i = float(open_[i]) * 0.01
                px_e = float(open_[i])
                init_risk = atr_i * sl_mult

                if di_i == 1:
                    psl = px_e - init_risk
                    ptp = px_e + init_risk * tp_rr
                else:
                    psl = px_e + init_risk
                    ptp = px_e - init_risk * tp_rr

                pending = dict(d=di_i, sl=psl, tp=ptp, regime=ri,
                               score=sc_i, initial_risk=init_risk, entry_atr=atr_i)

    # Force-close at end
    if pos is not None:
        d = pos['d']
        fe  = float(open_[-1])*(1-slip) if d==1 else float(open_[-1])*(1+slip)
        xf  = fe * pos['qty'] * fee
        pnl = (fe-pos['entry_px'])*pos['qty']-xf if d==1 \
             else (pos['entry_px']-fe)*pos['qty']-xf
        equity += pnl
        trades.append((d, pos['regime'], pos['score'], pnl,
                       (n-1)-pos['entry_i'], 'EOD', pos['entry_i']))
    return trades


# ─────────────────────────────────────────────────────────────────────────────
# Metrics
# ─────────────────────────────────────────────────────────────────────────────

def calc_metrics(trades, capital=10_000.0):
    if not trades:
        return dict(n=0, wr=0.0, pf=0.0, ret=0.0, dd=0.0, sharpe=0.0,
                    avg_dur=0.0, sl_pct=0.0, tp_pct=0.0, partial_pct_m=0.0)
    pnls  = [t[3] for t in trades]
    wins  = [p for p in pnls if p > 0]
    losses= [p for p in pnls if p <= 0]
    gw = sum(wins); gl = abs(sum(losses))
    eq = capital; pk = capital; mdd = 0.0
    for p in pnls:
        eq += p
        if eq > pk: pk = eq
        mdd = max(mdd, pk - eq)
    avg  = statistics.mean(pnls)
    std  = statistics.stdev(pnls) if len(pnls) > 1 else 1e-9
    exits = [t[5] for t in trades]
    n = len(trades)
    return dict(
        n=n,
        wr=len(wins)/n*100,
        pf=gw/gl if gl > 0 else 999.0,
        ret=sum(pnls)/capital*100,
        dd=mdd/capital*100,
        sharpe=avg/std,
        avg_dur=statistics.mean([t[4] for t in trades]),
        sl_pct=exits.count('SL')/n*100,
        tp_pct=exits.count('TP')/n*100,
        partial_pct_m=exits.count('PARTIAL')/n*100,
        time_pct=exits.count('TIME')/n*100,
    )


def cycle_breakdown(trades, timestamps, capital=10_000.0):
    """Break down metrics by BTC market cycle."""
    # Normalise tz: if index is tz-aware, make boundaries tz-aware (UTC) too
    tz = getattr(timestamps, 'tz', None)
    result = {}
    for name, (t_start, t_end) in CYCLES.items():
        ts0 = pd.Timestamp(t_start, tz=tz); ts1 = pd.Timestamp(t_end, tz=tz)
        cycle_trades = [t for t in trades
                        if ts0 <= timestamps[t[6]] < ts1]
        m = calc_metrics(cycle_trades, capital)
        result[name] = m
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Top-level worker (picklable)
# ─────────────────────────────────────────────────────────────────────────────

def _p5_worker(args):
    (label, tf, open_, high_, low_, atr_, hour_, atr_ma_,
     r_row, s_row, d_row, params) = args
    trades = simulate_enhanced(open_, high_, low_, atr_,
                               r_row, s_row, d_row, hour_, atr_ma_, params)
    m = calc_metrics(trades, params['initial_capital'])
    return label, tf, m, trades


# ─────────────────────────────────────────────────────────────────────────────
# Parameter grids
# ─────────────────────────────────────────────────────────────────────────────

def make_variant(label, **overrides):
    p = dict(P4_BASE)
    p.update(overrides)
    p['label'] = label
    return p


def build_exit_grid():
    """Lever 1: exit logic — fix Phase 4 best params, vary exit strategy."""
    g = []
    # 1a. RR-based fixed TP
    for rr in [1.22, 1.5, 2.0, 3.0, 4.0, 6.0]:
        g.append(make_variant(f'fixed_rr{rr}', exit_mode='fixed', tp_rr=rr))
    # 1b. ATR trailing stop (no fixed TP ceiling — set tp_rr very high)
    for act in [0.5, 1.0, 1.5]:
        for dist in [1.0, 1.5, 2.0, 2.5]:
            g.append(make_variant(
                f'trail_act{act}_dist{dist}',
                exit_mode='atr_trail', tp_rr=99.0,
                trail_act_r=act, trail_atr_mult=dist))
    # 1c. ATR trail WITH fixed TP ceiling at 4R (best of both worlds)
    for act in [0.5, 1.0]:
        for dist in [1.5, 2.0]:
            g.append(make_variant(
                f'trail_cap4R_act{act}_dist{dist}',
                exit_mode='atr_trail', tp_rr=4.0,
                trail_act_r=act, trail_atr_mult=dist))
    # 1d. Partial exit at 1R then trail remainder
    for pct in [0.33, 0.50]:
        g.append(make_variant(
            f'partial{int(pct*100)}pct_at1R',
            exit_mode='partial', tp_rr=4.0,
            partial_r=1.0, partial_pct=pct))
    # 1e. Time-limit exit (stale position forced close)
    for mb in [50, 100, 200]:
        g.append(make_variant(f'timelimit_{mb}bars',
                              exit_mode='fixed', tp_rr=6.0, max_bars=mb))
    return g


def build_frequency_grid(best_exit_params: dict):
    """Lever 2: ADX sweep 28-34, apply best exit config."""
    g = []
    for adx_min in [28, 29, 30, 31, 32, 33, 34]:
        for thresh in [0.45, 0.50, 0.55]:
            p = dict(best_exit_params)
            p['trend_adx_min'] = float(adx_min)
            p['confluence_threshold'] = thresh
            p['label'] = f'freq_adx{adx_min}_thr{thresh}'
            g.append(p)
    return g


def build_short_grid(best_exit_params: dict):
    """Lever 3: Short-side evaluation."""
    g = []
    # Full shorts (no bear gate) — both directions from TrendModel
    for adx_min in [30, 32, 34]:
        for thresh in [0.50, 0.55]:
            p = dict(best_exit_params)
            p.update(trend_bull_only=False, bear_short_gate=False,
                     trend_adx_min=float(adx_min),
                     confluence_threshold=thresh,
                     label=f'short_full_adx{adx_min}_thr{thresh}')
            g.append(p)
    # Bear-gated shorts (shorts ONLY in bear_trend regime)
    for adx_min in [30, 32, 34]:
        for thresh in [0.50, 0.55]:
            p = dict(best_exit_params)
            p.update(trend_bull_only=False, bear_short_gate=True,
                     trend_adx_min=float(adx_min),
                     confluence_threshold=thresh,
                     label=f'short_beargate_adx{adx_min}_thr{thresh}')
            g.append(p)
    return g


def build_agent_grid(best_params: dict):
    """Lever 4: Agent impact simulation."""
    g = []
    for filt in [None, 'atr_vol', 'time_of_day', 'combined']:
        p = dict(best_params)
        p['agent_filter'] = filt
        p['label'] = f'agent_{filt or "none"}'
        g.append(p)
    return g


# ─────────────────────────────────────────────────────────────────────────────
# MTF 30m + 4h helper
# ─────────────────────────────────────────────────────────────────────────────

HTF_ALLOWED = [R_BULL, R_BEAR, R_VEXP]

def run_mtf_sim(df_30m, df_4h, params,
                open_, high_, low_, atr_, hour_, atr_ma_):
    """Gate 30m signals by 4h regime, then simulate."""
    r30, s30, d30 = cpu_precompute(df_30m, [params])
    r4h, _,   _   = cpu_precompute(df_4h,  [params])

    htf_ser = pd.Series(r4h[0], index=df_4h.index, dtype=np.int8)
    htf_aln = htf_ser.reindex(df_30m.index, method='ffill').fillna(R_UNC).values.astype(np.int8)
    htf_pass= np.isin(htf_aln, HTF_ALLOWED)

    d_filt = d30[0].copy()
    d_filt[~htf_pass] = 0

    trades = simulate_enhanced(open_, high_, low_, atr_,
                               r30[0], s30[0], d_filt, hour_, atr_ma_, params)
    return calc_metrics(trades, params['initial_capital']), trades


# ─────────────────────────────────────────────────────────────────────────────
# Walk-forward (5 folds)
# ─────────────────────────────────────────────────────────────────────────────

FOLDS = [
    ("F1", "2022-03-22", "2023-09-22", "2023-09-22", "2024-03-22"),
    ("F2", "2022-09-22", "2024-03-22", "2024-03-22", "2024-09-22"),
    ("F3", "2023-03-22", "2024-09-22", "2024-09-22", "2025-03-22"),
    ("F4", "2023-09-22", "2025-03-22", "2025-03-22", "2025-09-22"),
    ("F5", "2024-03-22", "2025-09-22", "2025-09-22", "2026-03-21"),
]

def walk_forward(df, params, open_, high_, low_, atr_, hour_, atr_ma_):
    results = []
    for fname, _, _, te_s, te_e in FOLDS:
        mask = (df.index >= te_s) & (df.index < te_e)
        if mask.sum() < 50: continue
        idx  = np.where(mask)[0]
        r_m, s_m, d_m = cpu_precompute(df.iloc[idx], [params])
        trades = simulate_enhanced(
            open_[idx], high_[idx], low_[idx], atr_[idx],
            r_m[0], s_m[0], d_m[0], hour_[idx], atr_ma_[idx], params)
        m = calc_metrics(trades, params['initial_capital'])
        results.append({'fold': fname, **m})
    if not results: return {}
    pfs = [r['pf'] for r in results]
    return dict(folds=results, n=len(results),
                profitable=sum(1 for p in pfs if p > 1.0),
                median_pf=statistics.median(pfs),
                min_pf=min(pfs), max_pf=max(pfs),
                std_pf=statistics.stdev(pfs) if len(pfs) > 1 else 0)


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main():
    t_start  = time.time()
    date_str = datetime.datetime.now().strftime("%Y%m%d_%H%M")

    print(f"\n{'='*72}")
    print("NEXUSTRADER — PHASE 5: EDGE EXPANSION & ROBUSTNESS")
    print(f"{'='*72}")
    print(f"  CPU workers : {N_CPU_WORKERS}")
    print(f"  GPU         : {GPU_NAME} {'[ENABLED]' if GPU_AVAILABLE else '[not available]'}")
    print(f"{'='*72}\n")

    # ── 1. Load cached indicator parquets ────────────────────────────────
    print("[1] Loading indicator data (cached)...")
    t0_load = time.time()
    dfs: dict[str, pd.DataFrame] = {}
    with ProcessPoolExecutor(max_workers=5) as lpe:
        futs = {lpe.submit(_load_tf, (tf, str(BACKTEST_DIR), str(ROOT))): tf
                for tf in ["30m", "4h"]}
        for fut in as_completed(futs):
            tf_k, df_result, msg = fut.result()
            if df_result is not None:
                dfs[tf_k] = df_result
            print(f"  {tf_k}: {msg}")
    print(f"  → loaded in {time.time()-t0_load:.1f}s")

    if "30m" not in dfs:
        print("ERROR: 30m data not found. Run run_optimization.py first to build cache.")
        return

    df30 = dfs["30m"]
    df4h = dfs.get("4h")

    # ── Prepare arrays ───────────────────────────────────────────────────
    def arr(df, col): return df[col].values.astype(np.float32)
    open_30  = arr(df30, "open"); high_30 = arr(df30, "high")
    low_30   = arr(df30, "low");  atr_30  = arr(df30, "atr")
    # Hour-of-day for time-of-day filter
    hour_30  = np.array([ts.hour for ts in df30.index], dtype=np.int8)
    # ATR 20-bar mean for volatility filter
    atr_ma_30 = pd.Series(atr_30).rolling(20, min_periods=5).mean().values.astype(np.float32)
    ts_30    = df30.index  # DatetimeIndex for cycle tagging

    # ── 2. Phase 4 baseline ──────────────────────────────────────────────
    print("\n[2] Computing Phase 4 baseline...")
    r_base, s_base, d_base = cpu_precompute(df30, [P4_BASE])
    baseline_trades = simulate_enhanced(
        open_30, high_30, low_30, atr_30,
        r_base[0], s_base[0], d_base[0], hour_30, atr_ma_30, P4_BASE)
    bm = calc_metrics(baseline_trades, P4_BASE['initial_capital'])
    print(f"  Baseline: n={bm['n']}  WR={bm['wr']:.1f}%  PF={bm['pf']:.3f}  "
          f"ret={bm['ret']:+.2f}%  DD={bm['dd']:.2f}%")

    # ── 3. Exit logic grid ───────────────────────────────────────────────
    print("\n[3] Exit logic sweep...")
    exit_grid = build_exit_grid()
    print(f"  {len(exit_grid)} exit variants")

    r_exit, s_exit, d_exit = cpu_precompute(df30, exit_grid)
    exit_results = []
    exit_args = []
    for ci, params in enumerate(exit_grid):
        exit_args.append((params['label'], '30m', open_30, high_30, low_30, atr_30,
                          hour_30, atr_ma_30, r_exit[ci], s_exit[ci], d_exit[ci], params))

    with ProcessPoolExecutor(max_workers=N_CPU_WORKERS) as pool:
        futs = [pool.submit(_p5_worker, a) for a in exit_args]
        for fut in as_completed(futs):
            label, tf, m, _ = fut.result()
            exit_results.append({'label': label, **m})

    exit_results.sort(key=lambda x: -x['pf'])
    print(f"\n  EXIT LOGIC — TOP 10 (full 4yr, 30m):")
    print(f"  {'label':<35} {'n':>5} {'WR':>6} {'PF':>6} {'ret':>8} {'DD':>6} "
          f"{'SL%':>5} {'TP%':>5} {'avgDur':>7}")
    print("  " + "-"*92)
    for r in exit_results[:10]:
        print(f"  {r['label']:<35} {r['n']:>5} {r['wr']:>5.1f}% {r['pf']:>6.3f} "
              f"{r['ret']:>+7.2f}% {r['dd']:>5.2f}% "
              f"{r.get('sl_pct',0):>4.0f}% {r.get('tp_pct',0):>4.0f}% "
              f"{r.get('avg_dur',0):>6.1f}")

    # Best exit config
    best_exit = next(p for p in exit_grid if p['label'] == exit_results[0]['label'])
    print(f"\n  → Best exit: {exit_results[0]['label']}  PF={exit_results[0]['pf']:.3f}")

    # ── 4. Frequency grid ────────────────────────────────────────────────
    print("\n[4] Trade frequency sweep (ADX 28-34)...")
    freq_grid = build_frequency_grid(best_exit)
    print(f"  {len(freq_grid)} frequency variants")

    r_freq, s_freq, d_freq = cpu_precompute(df30, freq_grid)
    freq_results = []
    freq_args = [(p['label'], '30m', open_30, high_30, low_30, atr_30,
                  hour_30, atr_ma_30, r_freq[ci], s_freq[ci], d_freq[ci], p)
                 for ci, p in enumerate(freq_grid)]

    with ProcessPoolExecutor(max_workers=N_CPU_WORKERS) as pool:
        futs = [pool.submit(_p5_worker, a) for a in freq_args]
        for fut in as_completed(futs):
            label, tf, m, _ = fut.result()
            freq_results.append({'label': label, **m})

    freq_results.sort(key=lambda x: -x['pf'])
    print(f"\n  FREQUENCY — TOP 10 (PF ranked):")
    print(f"  {'label':<40} {'n':>5} {'WR':>6} {'PF':>6} {'ret':>8} {'DD':>6}")
    print("  " + "-"*72)
    for r in freq_results[:10]:
        print(f"  {r['label']:<40} {r['n']:>5} {r['wr']:>5.1f}% {r['pf']:>6.3f} "
              f"{r['ret']:>+7.2f}% {r['dd']:>5.2f}%")

    best_freq = next(p for p in freq_grid if p['label'] == freq_results[0]['label'])
    print(f"\n  → Best frequency: {freq_results[0]['label']}  "
          f"PF={freq_results[0]['pf']:.3f}  n={freq_results[0]['n']}")

    # Combined best (exit + frequency)
    best_combined = dict(best_freq)
    best_combined.update({k: best_exit[k] for k in
                          ['exit_mode','tp_rr','trail_atr_mult','trail_act_r',
                           'partial_r','partial_pct','max_bars']})
    best_combined['label'] = 'combined_best'

    # ── 5. Short-side evaluation ─────────────────────────────────────────
    print("\n[5] Short-side evaluation...")
    short_grid = build_short_grid(best_exit)
    print(f"  {len(short_grid)} short variants")

    r_sh, s_sh, d_sh = cpu_precompute(df30, short_grid)
    short_results = []
    short_args = [(p['label'], '30m', open_30, high_30, low_30, atr_30,
                   hour_30, atr_ma_30, r_sh[ci], s_sh[ci], d_sh[ci], p)
                  for ci, p in enumerate(short_grid)]

    with ProcessPoolExecutor(max_workers=N_CPU_WORKERS) as pool:
        futs = [pool.submit(_p5_worker, a) for a in short_args]
        for fut in as_completed(futs):
            label, tf, m, _ = fut.result()
            short_results.append({'label': label, **m})

    short_results.sort(key=lambda x: -x['pf'])
    # Compare to longs-only baseline
    long_only_m = bm
    print(f"\n  SHORTS EVALUATION — comparison to long-only (PF={long_only_m['pf']:.3f}, "
          f"n={long_only_m['n']}):")
    print(f"  {'label':<45} {'n':>5} {'WR':>6} {'PF':>6} {'ret':>8} {'DD':>6} {'vs baseline':>12}")
    print("  " + "-"*82)
    for r in short_results:
        delta = r['pf'] - long_only_m['pf']
        marker = "✓ BETTER" if delta > 0.05 else ("✗ WORSE" if delta < -0.05 else "~ NEUTRAL")
        print(f"  {r['label']:<45} {r['n']:>5} {r['wr']:>5.1f}% {r['pf']:>6.3f} "
              f"{r['ret']:>+7.2f}% {r['dd']:>5.2f}% {delta:>+8.3f} {marker}")

    # ── 6. Agent impact ──────────────────────────────────────────────────
    print("\n[6] Agent impact simulation...")
    agent_grid = build_agent_grid(best_exit)
    r_ag, s_ag, d_ag = cpu_precompute(df30, agent_grid)
    agent_results = []
    agent_args = [(p['label'], '30m', open_30, high_30, low_30, atr_30,
                   hour_30, atr_ma_30, r_ag[ci], s_ag[ci], d_ag[ci], p)
                  for ci, p in enumerate(agent_grid)]

    with ProcessPoolExecutor(max_workers=N_CPU_WORKERS) as pool:
        futs = [pool.submit(_p5_worker, a) for a in agent_args]
        for fut in as_completed(futs):
            label, tf, m, _ = fut.result()
            agent_results.append({'label': label, **m})

    agent_results.sort(key=lambda x: -x['pf'])
    print(f"\n  AGENT FILTER IMPACT:")
    print(f"  {'filter':<20} {'n':>5} {'WR':>6} {'PF':>6} {'ret':>8} {'DD':>6} {'pf_delta':>9}")
    print("  " + "-"*60)
    base_pf = next(r['pf'] for r in agent_results if 'none' in r['label'])
    for r in agent_results:
        print(f"  {r['label']:<20} {r['n']:>5} {r['wr']:>5.1f}% {r['pf']:>6.3f} "
              f"{r['ret']:>+7.2f}% {r['dd']:>5.2f}% {r['pf']-base_pf:>+8.3f}")

    # ── 7. BTC cycle distribution ────────────────────────────────────────
    print("\n[7] BTC cycle distribution (baseline config)...")
    cycle_data = cycle_breakdown(baseline_trades, ts_30, P4_BASE['initial_capital'])
    print(f"  {'cycle':<20} {'n':>5} {'WR':>6} {'PF':>6} {'ret':>8}")
    print("  " + "-"*50)
    for cname, cm in cycle_data.items():
        print(f"  {cname:<20} {cm['n']:>5} {cm['wr']:>5.1f}% {cm['pf']:>6.3f} "
              f"{cm['ret']:>+7.2f}%")
    # Concentration check
    total_n = sum(cm['n'] for cm in cycle_data.values())
    if total_n > 0:
        dominant = max(cycle_data.items(), key=lambda x: x[1]['n'])
        conc = dominant[1]['n'] / total_n * 100
        flag = " ⚠ CONCENTRATED" if conc > 50 else ""
        print(f"\n  Dominant phase: {dominant[0]} ({conc:.0f}% of trades){flag}")

    # ── 8. MTF 30m + 4h with best params ────────────────────────────────
    mtf_results_store = []
    if df4h is not None:
        print("\n[8] MTF 30m+4h with best Phase 5 configs...")
        open_4h = arr(df4h,"open"); high_4h = arr(df4h,"high")
        low_4h  = arr(df4h,"low");  atr_4h  = arr(df4h,"atr")

        mtf_candidates = [P4_BASE, best_exit, best_combined]
        for p in mtf_candidates:
            m_mtf, trades_mtf = run_mtf_sim(
                df30, df4h, p, open_30, high_30, low_30, atr_30, hour_30, atr_ma_30)
            mtf_results_store.append({'label': p['label']+'_MTF', **m_mtf})
            print(f"  {p['label']+'_MTF':<40} n={m_mtf['n']:5d}  WR={m_mtf['wr']:5.1f}%  "
                  f"PF={m_mtf['pf']:6.3f}  ret={m_mtf['ret']:+7.2f}%  DD={m_mtf['dd']:5.2f}%")

    # ── 9. Walk-forward on top 3 overall ────────────────────────────────
    print("\n[9] Walk-forward on top 3 candidates...")
    all_single = exit_results + freq_results
    all_single.sort(key=lambda x: (-x['pf'], -x['n']))
    top3_labels = [r['label'] for r in all_single[:3] if r['n'] >= 30]

    top3_params = []
    for lbl in top3_labels:
        for pg in [exit_grid, freq_grid]:
            hit = next((p for p in pg if p['label'] == lbl), None)
            if hit: top3_params.append(hit); break

    wf_store = {}
    for p in top3_params:
        wf = walk_forward(df30, p, open_30, high_30, low_30, atr_30, hour_30, atr_ma_30)
        wf_store[p['label']] = wf
        med = wf.get('median_pf', 0); prof = wf.get('profitable', 0)
        nf  = wf.get('n', 0); mn = wf.get('min_pf', 0); mx = wf.get('max_pf', 0)
        print(f"  {p['label']:<35} WF med={med:.3f}  {prof}/{nf} profitable  "
              f"[{mn:.2f}–{mx:.2f}]")

    # ── 10. Summary + save ───────────────────────────────────────────────
    print(f"\n{'='*72}")
    print("PHASE 5 SUMMARY")
    print(f"{'='*72}")
    print(f"  Phase 4 baseline     : PF={bm['pf']:.3f}  n={bm['n']}")
    best_overall = max(exit_results + freq_results, key=lambda x: x['pf'])
    print(f"  Best single-TF found : PF={best_overall['pf']:.3f}  n={best_overall['n']}  "
          f"({best_overall['label']})")
    if mtf_results_store:
        best_mtf = max(mtf_results_store, key=lambda x: x['pf'])
        print(f"  Best MTF 30m+4h      : PF={best_mtf['pf']:.3f}  n={best_mtf['n']}  "
              f"({best_mtf['label']})")
    short_verdict = "ADDS VALUE" if short_results and short_results[0]['pf'] > bm['pf'] + 0.05 \
                    else "NO NET BENEFIT"
    print(f"  Short-side verdict   : {short_verdict}")
    agent_best = max(agent_results, key=lambda x: x['pf'])
    agent_verdict = "IMPROVES PF" if agent_best['pf'] > base_pf + 0.05 else "MINIMAL IMPACT"
    print(f"  Agent filter verdict : {agent_verdict} (best: {agent_best['label']})")

    target_met = best_overall['pf'] >= 1.80
    print(f"\n  PF ≥ 1.80 target: {'✓ ACHIEVED' if target_met else '✗ NOT ACHIEVED'}")
    print(f"{'='*72}")

    # Save
    report = dict(
        date=date_str,
        p4_baseline=bm,
        exit_results=exit_results,
        freq_results=freq_results,
        short_results=short_results,
        agent_results=agent_results,
        cycle_distribution=cycle_data,
        mtf_results=mtf_results_store,
        wf_results=wf_store,
        best_overall=best_overall,
        best_exit_label=exit_results[0]['label'] if exit_results else None,
        best_freq_label=freq_results[0]['label'] if freq_results else None,
        short_verdict=short_verdict,
        agent_verdict=agent_verdict,
        target_met=target_met,
    )
    out_path = REPORT_DIR / f"phase5_results_{date_str}.json"
    with open(out_path, "w") as f:
        json.dump(report, f, indent=2, default=str)
    print(f"\n[Saved] → {out_path}")
    print(f"Total elapsed: {time.time()-t_start:.1f}s")


if __name__ == "__main__":
    mp.set_start_method("spawn", force=True)
    main()
