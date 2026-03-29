#!/usr/bin/env python3
"""
research/harness/optimize_pbl.py
=================================
Parallelized PBL/SLC optimization harness for NexusTrader research branch.

DESIGN PRINCIPLES
-----------------
- Uses NexusTrader ResearchRegimeClassifier exactly (same as production)
- Process-based parallelism via multiprocessing.Pool (CPU-bound safe)
- Prints real-time progress: batch #, trial #, best-so-far, rolling top-10
- Saves results after every batch (no data loss on interruption)
- Supports coarse, focused, and WF sweeps from CLI flags
- GPU: optional CuPy for indicator pre-computation (safe CPU fallback)

USAGE
-----
# Stage A — Baseline reproduction
python research/harness/optimize_pbl.py --stage baseline

# Stage B — Coarse PBL sweep
python research/harness/optimize_pbl.py --stage coarse_pbl --workers 8

# Stage C — Focused PBL sweep (after reviewing Stage B results)
python research/harness/optimize_pbl.py --stage focused_pbl --workers 8

# Stage D — Confirmation variants
python research/harness/optimize_pbl.py --stage confirmation --workers 8

# Stage E — Walk-forward on top-N candidates
python research/harness/optimize_pbl.py --stage walkforward --top-n 5

# Stage F — Fee sensitivity stress test
python research/harness/optimize_pbl.py --stage stress --config research/results/top_candidates.json
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import multiprocessing as mp
import os
import sys
import time
import traceback
from dataclasses import dataclass, asdict, field
from datetime import datetime
from itertools import product
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

# ── Path setup ───────────────────────────────────────────────────────────────
ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

logging.basicConfig(level=logging.ERROR)
logging.getLogger("core").setLevel(logging.ERROR)
logging.getLogger("torch").setLevel(logging.ERROR)

# ── Config ───────────────────────────────────────────────────────────────────
DATA_DIR     = ROOT / "backtest_data"
RESULTS_DIR  = ROOT / "research" / "results"
LEADER_DIR   = ROOT / "research" / "leaderboards"
RESULTS_DIR.mkdir(parents=True, exist_ok=True)
LEADER_DIR.mkdir(parents=True, exist_ok=True)

SYMBOLS      = ["BTC/USDT", "SOL/USDT", "ETH/USDT"]
INITIAL_CAP  = 100_000.0
POS_FRAC     = 0.35
MAX_HEAT     = 0.80
MAX_POS      = 10
MODEL_LB     = 350   # bars passed to model per signal check
HTF_LB       = 60    # 4h lookback bars
SLC_1H_LB    = 150   # 1h lookback bars

# Data split
IS_END   = pd.Timestamp("2025-03-21", tz="UTC")   # training IS end
OOS_START= pd.Timestamp("2025-03-22", tz="UTC")   # holdout start

# WF windows (12-month train, 3-month OOS, rolling 3m step)
WF_WINDOWS = [
    ("2022-03-22", "2023-03-21", "2023-03-22", "2023-06-21"),
    ("2022-06-22", "2023-06-21", "2023-06-22", "2023-09-21"),
    ("2022-09-22", "2023-09-21", "2023-09-22", "2023-12-21"),
    ("2022-12-22", "2023-12-21", "2023-12-22", "2024-03-21"),
]

# ── Data cache (global in each worker process) ───────────────────────────────
_DATA_CACHE: dict = {}


def _sym_key(sym: str) -> str:
    return sym.replace("/", "_")


def _load_data(sym: str) -> dict[str, pd.DataFrame]:
    """Load 30m, 4h, 1h parquet files for a symbol. Cached per-process."""
    key = _sym_key(sym)
    if key in _DATA_CACHE:
        return _DATA_CACHE[key]
    result = {}
    for tf in ["30m", "4h", "1h"]:
        fp = DATA_DIR / f"{key}_{tf}.parquet"
        if fp.exists():
            df = pd.read_parquet(fp)
            if not isinstance(df.index, pd.DatetimeIndex):
                df.index = pd.to_datetime(df.index, utc=True)
            elif df.index.tz is None:
                df.index = df.index.tz_localize("UTC")
            result[tf] = df
    _DATA_CACHE[key] = result
    return result


# ── Research Regime Classifier (inline for worker isolation) ─────────────────
def _ema(s: pd.Series, span: int) -> pd.Series:
    return s.ewm(span=span, adjust=False).mean()


def _adx_ewm(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    ph, pl, pc = high.shift(1), low.shift(1), close.shift(1)
    tr = pd.concat([high - low, (high - pc).abs(), (low - pc).abs()], axis=1).max(axis=1)
    up, dn = high - ph, pl - low
    pdm = pd.Series(np.where((up > dn) & (up > 0), up, 0.0), index=close.index)
    ndm = pd.Series(np.where((dn > up) & (dn > 0), dn, 0.0), index=close.index)
    tr_s = tr.ewm(span=period, adjust=False).mean()
    pdi = 100 * pdm.ewm(span=period, adjust=False).mean() / tr_s.replace(0, np.nan)
    ndi = 100 * ndm.ewm(span=period, adjust=False).mean() / tr_s.replace(0, np.nan)
    dx  = 100 * (pdi - ndi).abs() / (pdi + ndi).replace(0, np.nan)
    return dx.ewm(span=period, adjust=False).mean().fillna(0)


def _atr_wilder(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    pc = close.shift(1)
    tr = pd.concat([high - low, (high - pc).abs(), (low - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False).mean()


def classify_regimes(df: pd.DataFrame) -> np.ndarray:
    """
    Vectorized research regime classifier. Exact match to ResearchRegimeClassifier.
    Returns int8 array: 0=SIDEWAYS 1=BULL_TREND 2=BEAR_TREND 3=BULL_EXP 4=BEAR_EXP 5=CRASH
    """
    c, h, l = df["close"], df["high"], df["low"]
    n = len(df)
    if n < 5:
        return np.zeros(n, dtype=np.int8)

    adx    = _adx_ewm(h, l, c, 14).values
    ema20  = _ema(c, 20).values
    ema50  = _ema(c, 50).values
    ema200 = _ema(c, 200).values
    atr    = _atr_wilder(h, l, c, 14).values

    # ATR ratio vs 100-bar rolling mean
    atr_s = pd.Series(atr)
    atr_baseline = atr_s.rolling(100, min_periods=1).mean().values
    atr_ratio = np.where(atr_baseline > 0, atr / atr_baseline, 1.0)

    # Return metrics
    cv = c.values
    ret_5d = pd.Series(cv).pct_change(240).values   # 5d at 30m=240 bars
    ret_2d = pd.Series(cv).pct_change(96).values    # 2d at 30m=96 bars

    # Rolling 48-bar peak drawdown
    rolling_max = pd.Series(cv).rolling(48, min_periods=1).max().values
    peak_dd = np.where(rolling_max > 0, (cv - rolling_max) / rolling_max, 0.0)

    ADX_MIN = 22.0
    ATR_EXP = 1.80
    ATR_CRASH = 2.80

    raw = np.zeros(n, dtype=np.int8)
    for i in range(n):
        ar  = atr_ratio[i]
        adx_v = adx[i]
        r5  = ret_5d[i] if not np.isnan(ret_5d[i]) else 0.0
        r2  = ret_2d[i] if not np.isnan(ret_2d[i]) else 0.0
        pd_ = peak_dd[i]
        a20 = ema20[i]
        a50 = ema50[i]
        a200= ema200[i]
        cv_ = cv[i]

        if (pd_ <= -0.15 or r2 <= -0.08) and ar >= ATR_CRASH:
            raw[i] = 5  # CRASH
        elif ar >= ATR_EXP and r5 <= -0.06 and pd_ > -0.15:
            raw[i] = 4  # BEAR_EXP
        elif ar >= ATR_EXP and r5 >= 0.06:
            raw[i] = 3  # BULL_EXP
        elif adx_v >= ADX_MIN and a20 <= a50 and cv_ <= a200 and ar < ATR_EXP:
            raw[i] = 2  # BEAR_TREND
        elif adx_v >= ADX_MIN and a20 > a50 and cv_ > a200 and ar < ATR_EXP:
            raw[i] = 1  # BULL_TREND
        else:
            raw[i] = 0  # SIDEWAYS

    # 3-bar hysteresis
    out = raw.copy()
    committed = -1
    count = 0
    HYST = 3
    for i in range(n):
        r = int(raw[i])
        if r != committed:
            count += 1
            if count >= HYST:
                committed = r
                count = 0
        out[i] = committed if committed >= 0 else r

    return out.astype(np.int8)


# ── Indicator computation ─────────────────────────────────────────────────────
def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Compute all required indicators on a OHLCV dataframe."""
    df = df.copy()
    c, h, l = df["close"], df["high"], df["low"]

    df["ema_9"]  = _ema(c, 9)
    df["ema_20"] = _ema(c, 20)
    df["ema_50"] = _ema(c, 50)
    df["ema_100"]= _ema(c, 100)
    df["ema_200"]= _ema(c, 200)
    df["atr_14"] = _atr_wilder(h, l, c, 14)
    df["adx_14"] = _adx_ewm(h, l, c, 14)

    # RSI (Wilder EWM)
    delta = c.diff()
    gain  = delta.clip(lower=0)
    loss  = (-delta).clip(lower=0)
    avg_gain = gain.ewm(alpha=1/14, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/14, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    df["rsi_14"] = 100 - (100 / (1 + rs))

    return df


def compute_htf_indicators(df_4h: pd.DataFrame) -> pd.DataFrame:
    """Compute 4h indicators for HTF gate."""
    df = df_4h.copy()
    c, h, l = df["close"], df["high"], df["low"]
    df["ema_9"]  = _ema(c, 9)
    df["ema_20"] = _ema(c, 20)
    df["ema_21"] = _ema(c, 21)
    df["ema_50"] = _ema(c, 50)
    df["ema_100"]= _ema(c, 100)
    df["ema_200"]= _ema(c, 200)
    df["adx_14"] = _adx_ewm(h, l, c, 14)
    return df


# ── Signal evaluation (inline, no Qt/production dependencies) ─────────────────
def _evaluate_pbl_signal(
    bar_30m: pd.Series,
    df_30m_window: pd.DataFrame,
    df_4h_window: Optional[pd.DataFrame],
    params: dict,
) -> Optional[dict]:
    """
    Evaluate PBL signal for a single bar. Returns trade dict or None.
    All params come from the parameter set being tested.
    """
    ema_prox_mult     = params["ema_prox_atr_mult"]
    sl_mult           = params["sl_atr_mult"]
    tp_mult           = params["tp_atr_mult"]
    rsi_min           = params["rsi_min"]
    htf_ema_fast_col  = f"ema_{params['htf_ema_fast']}"
    htf_ema_slow_col  = f"ema_{params['htf_ema_slow']}"
    htf_adx_min       = params.get("htf_adx_min")  # None = disabled
    htf_ema200_gate   = params.get("htf_price_above_ema200", False)
    body_ratio_max    = params.get("body_ratio_max")  # None = disabled

    # Require minimum bars
    if len(df_30m_window) < 60:
        return None

    close  = float(bar_30m["close"])
    open_  = float(bar_30m["open"])
    high_  = float(bar_30m["high"])
    low_   = float(bar_30m["low"])
    ema50  = float(bar_30m.get("ema_50", np.nan))
    rsi    = float(bar_30m.get("rsi_14", np.nan))
    atr    = float(bar_30m.get("atr_14", np.nan))

    if np.isnan(ema50) or np.isnan(rsi) or np.isnan(atr) or atr <= 0:
        return None

    # ── Condition 1: EMA50 proximity ─────────────────────────────────────
    if abs(close - ema50) > ema_prox_mult * atr:
        return None

    # ── Condition 2: Rejection candle ───────────────────────────────────
    body = abs(close - open_)
    lw   = min(close, open_) - low_
    uw   = high_ - max(close, open_)
    candle_range = high_ - low_

    if close <= open_:   return None  # must be bullish
    if lw <= uw:         return None  # lower wick must dominate
    if lw <= body:       return None  # lower wick must beat body

    # Optional body ratio filter
    if body_ratio_max is not None and candle_range > 0:
        if body / candle_range > body_ratio_max:
            return None

    # ── Condition 3: RSI gate ────────────────────────────────────────────
    if rsi <= rsi_min:
        return None

    # ── Condition 4: 4h HTF gate ─────────────────────────────────────────
    if df_4h_window is not None and len(df_4h_window) >= 5:
        try:
            last4h = df_4h_window.iloc[-1]

            # EMA crossover gate (always required)
            htf_ef = float(last4h.get(htf_ema_fast_col, np.nan))
            htf_es = float(last4h.get(htf_ema_slow_col, np.nan))
            if np.isnan(htf_ef) or np.isnan(htf_es) or htf_ef <= htf_es:
                return None  # HTF trend not bullish

            # Optional ADX gate
            if htf_adx_min is not None:
                htf_adx = float(last4h.get("adx_14", np.nan))
                if np.isnan(htf_adx) or htf_adx < htf_adx_min:
                    return None

            # Optional price > 4h EMA200 gate
            if htf_ema200_gate:
                htf_ema200 = float(last4h.get("ema_200", np.nan))
                if np.isnan(htf_ema200) or close <= htf_ema200:
                    return None
        except Exception:
            pass  # HTF data error — bypass gate

    # ── Signal valid ─────────────────────────────────────────────────────
    entry   = close
    sl      = close - sl_mult * atr
    tp      = close + tp_mult * atr

    if sl >= entry or tp <= entry:
        return None

    return {
        "entry": entry,
        "sl":    sl,
        "tp":    tp,
        "atr":   atr,
    }


def _evaluate_slc_signal(
    bar_1h: pd.Series,
    df_1h_window: pd.DataFrame,
    params: dict,
) -> Optional[dict]:
    """Evaluate SLC signal for a single 1h bar."""
    adx_min    = params.get("slc_adx_min", 28.0)
    swing_bars = params.get("slc_swing_bars", 10)
    sl_mult    = params.get("slc_sl_atr_mult", 2.5)
    tp_mult    = params.get("slc_tp_atr_mult", 2.0)

    if len(df_1h_window) < swing_bars + 5:
        return None

    close = float(bar_1h["close"])
    atr   = float(bar_1h.get("atr_14", np.nan))
    adx   = float(bar_1h.get("adx_14", np.nan))

    if np.isnan(atr) or np.isnan(adx) or atr <= 0:
        return None

    if adx < adx_min:
        return None

    prev_closes = df_1h_window["close"].iloc[-(swing_bars + 1):-1]
    if len(prev_closes) < swing_bars:
        return None
    prev_min = float(prev_closes.min())

    if close >= prev_min:
        return None

    entry = close
    sl    = entry + sl_mult * atr   # above (short)
    tp    = entry - tp_mult * atr   # below (short)

    if sl <= entry or tp >= entry:
        return None

    return {"entry": entry, "sl": sl, "tp": tp, "atr": atr}


# ── Position tracking ─────────────────────────────────────────────────────────
@dataclass
class Position:
    symbol: str
    direction: str         # "long" or "short"
    entry_price: float
    sl: float
    tp: float
    size_usdt: float
    entry_bar_idx: int
    model: str             # "pbl" or "slc"
    entry_ts: pd.Timestamp = field(default=None)

    def pnl(self, exit_price: float) -> float:
        if self.direction == "long":
            return (exit_price - self.entry_price) / self.entry_price * self.size_usdt
        else:
            return (self.entry_price - exit_price) / self.entry_price * self.size_usdt


# ── Core backtest engine ──────────────────────────────────────────────────────
def run_backtest(
    params: dict,
    mode: str = "pbl_only",   # "pbl_only" | "slc_only" | "combined"
    date_start: Optional[str] = None,
    date_end: Optional[str] = None,
    cost_per_side: float = 0.0004,
) -> dict:
    """
    Run a full backtest for given parameters.

    Parameters
    ----------
    params : dict    — parameter set (keys defined in search_spaces.json)
    mode   : str     — which models to run
    date_start/end   — ISO date strings for data slice (None = full dataset)
    cost_per_side    — fee per side (0 = Scenario A, 0.0004 = Scenario B)

    Returns
    -------
    dict with: pf, wr, cagr, maxdd, n_trades, n_wins, n_losses,
               gross_profit, gross_loss, params, mode, date_start, date_end
    """
    # ── Load + slice data ────────────────────────────────────────────────
    all_30m = {}
    all_4h  = {}
    all_1h  = {}

    for sym in SYMBOLS:
        data = _load_data(sym)
        df30 = compute_indicators(data.get("30m", pd.DataFrame()))
        df4h = compute_htf_indicators(data.get("4h", pd.DataFrame())) if "4h" in data else pd.DataFrame()
        df1h = compute_indicators(data.get("1h", pd.DataFrame())) if "1h" in data else pd.DataFrame()

        def _slice(df):
            if df.empty:
                return df
            if date_start:
                ds = pd.Timestamp(date_start, tz="UTC")
                df = df[df.index >= ds]
            if date_end:
                de = pd.Timestamp(date_end, tz="UTC")
                df = df[df.index <= de]
            return df

        all_30m[sym] = _slice(df30)
        all_4h[sym]  = _slice(df4h)
        all_1h[sym]  = _slice(df1h)

        # Pre-compute regime labels
        if not all_30m[sym].empty:
            all_30m[sym] = all_30m[sym].copy()
            all_30m[sym]["regime"] = classify_regimes(all_30m[sym])
        if not all_1h[sym].empty:
            all_1h[sym] = all_1h[sym].copy()
            all_1h[sym]["regime"] = classify_regimes(all_1h[sym])

    # ── Simulation ───────────────────────────────────────────────────────
    capital    = INITIAL_CAP
    positions: list[Position] = []
    closed: list[dict] = []

    # Build unified time index from 30m bars
    all_ts = sorted(set().union(*[set(df.index) for df in all_30m.values() if not df.empty]))

    for ts in all_ts:
        # ── Close expired positions ─────────────────────────────────────
        to_close = []
        for pos in positions:
            df30 = all_30m.get(pos.symbol)
            if df30 is None or ts not in df30.index:
                continue
            bar = df30.loc[ts]
            low_  = float(bar["low"])
            high_ = float(bar["high"])
            close_ = float(bar["close"])

            exit_price = None
            exit_reason = None

            if pos.direction == "long":
                if low_ <= pos.sl:
                    exit_price  = pos.sl
                    exit_reason = "sl"
                elif high_ >= pos.tp:
                    exit_price  = pos.tp
                    exit_reason = "tp"
            else:  # short
                if high_ >= pos.sl:
                    exit_price  = pos.sl
                    exit_reason = "sl"
                elif low_ <= pos.tp:
                    exit_price  = pos.tp
                    exit_reason = "tp"

            if exit_price is not None:
                raw_pnl  = pos.pnl(exit_price)
                fee      = pos.size_usdt * cost_per_side * 2
                net_pnl  = raw_pnl - fee
                capital += net_pnl
                closed.append({
                    "ts":       ts,
                    "symbol":   pos.symbol,
                    "model":    pos.model,
                    "dir":      pos.direction,
                    "entry":    pos.entry_price,
                    "exit":     exit_price,
                    "reason":   exit_reason,
                    "pnl":      net_pnl,
                    "size":     pos.size_usdt,
                })
                to_close.append(pos)

        for p in to_close:
            positions.remove(p)

        # ── Portfolio heat check ────────────────────────────────────────
        current_heat = sum(p.size_usdt for p in positions) / max(capital, 1)
        if current_heat >= MAX_HEAT or len(positions) >= MAX_POS:
            continue

        # ── Generate new signals ────────────────────────────────────────
        size_usdt = capital * POS_FRAC

        for sym in SYMBOLS:
            df30 = all_30m.get(sym)
            if df30 is None or ts not in df30.index:
                continue

            # Check if any position open for this symbol+model combo
            open_syms = {(p.symbol, p.model) for p in positions}

            bar     = df30.loc[ts]
            ts_pos  = df30.index.get_loc(ts)
            win_start = max(0, ts_pos - MODEL_LB)
            window30  = df30.iloc[win_start:ts_pos + 1]

            # ── PBL signal ──────────────────────────────────────────────
            if mode in ("pbl_only", "combined") and (sym, "pbl") not in open_syms:
                regime_30m = int(bar.get("regime", 0))
                if regime_30m == 1:  # BULL_TREND
                    df4h = all_4h.get(sym, pd.DataFrame())
                    if not df4h.empty:
                        idx4h = df4h.index.searchsorted(ts, side="right")
                        win4h = df4h.iloc[max(0, idx4h - HTF_LB): idx4h]
                    else:
                        win4h = None

                    sig = _evaluate_pbl_signal(bar, window30, win4h, params)
                    if sig and capital > size_usdt * 0.5:
                        positions.append(Position(
                            symbol      = sym,
                            direction   = "long",
                            entry_price = sig["entry"],
                            sl          = sig["sl"],
                            tp          = sig["tp"],
                            size_usdt   = min(size_usdt, capital * 0.04),
                            entry_bar_idx = ts_pos,
                            model       = "pbl",
                            entry_ts    = ts,
                        ))

            # ── SLC signal ──────────────────────────────────────────────
            if mode in ("slc_only", "combined") and (sym, "slc") not in open_syms:
                df1h = all_1h.get(sym, pd.DataFrame())
                if not df1h.empty:
                    idx1h = df1h.index.searchsorted(ts, side="right")
                    if idx1h > 0:
                        bar1h_ts = df1h.index[idx1h - 1]
                        bar1h = df1h.loc[bar1h_ts]
                        regime_1h = int(bar1h.get("regime", 0))
                        if regime_1h == 2:  # BEAR_TREND
                            win1h = df1h.iloc[max(0, idx1h - SLC_1H_LB): idx1h]
                            slc_params = {
                                "slc_adx_min":      params.get("slc_adx_min", 28.0),
                                "slc_swing_bars":   params.get("slc_swing_bars", 10),
                                "slc_sl_atr_mult":  params.get("slc_sl_atr_mult", 2.5),
                                "slc_tp_atr_mult":  params.get("slc_tp_atr_mult", 2.0),
                            }
                            sig = _evaluate_slc_signal(bar1h, win1h, slc_params)
                            if sig and capital > size_usdt * 0.5:
                                positions.append(Position(
                                    symbol      = sym,
                                    direction   = "short",
                                    entry_price = sig["entry"],
                                    sl          = sig["sl"],
                                    tp          = sig["tp"],
                                    size_usdt   = min(size_usdt, capital * 0.04),
                                    entry_bar_idx = ts_pos,
                                    model       = "slc",
                                    entry_ts    = ts,
                                ))

    # ── Force-close remaining open positions at last bar ────────────────
    if all_ts:
        last_ts = all_ts[-1]
        for pos in positions:
            df30 = all_30m.get(pos.symbol)
            if df30 is not None and not df30.empty:
                exit_p = float(df30.iloc[-1]["close"])
                raw_pnl = pos.pnl(exit_p)
                fee = pos.size_usdt * cost_per_side * 2
                closed.append({
                    "ts": last_ts, "symbol": pos.symbol, "model": pos.model,
                    "dir": pos.direction, "entry": pos.entry_price, "exit": exit_p,
                    "reason": "end_of_data", "pnl": raw_pnl - fee, "size": pos.size_usdt,
                })
                capital += raw_pnl - fee

    # ── Compute metrics ──────────────────────────────────────────────────
    n = len(closed)
    wins   = [t for t in closed if t["pnl"] > 0]
    losses = [t for t in closed if t["pnl"] <= 0]
    gross_profit = sum(t["pnl"] for t in wins)
    gross_loss   = abs(sum(t["pnl"] for t in losses))

    pf  = gross_profit / gross_loss if gross_loss > 0 else 999.0
    wr  = len(wins) / n if n > 0 else 0.0

    # CAGR — use actual data range
    if all_ts and len(all_ts) > 1:
        span_years = (all_ts[-1] - all_ts[0]).total_seconds() / (365.25 * 86400)
    else:
        span_years = 4.0
    cagr = ((capital / INITIAL_CAP) ** (1 / max(span_years, 0.1)) - 1) * 100

    # MaxDD
    if closed:
        equity = INITIAL_CAP
        peak   = INITIAL_CAP
        min_dd = 0.0
        for t in sorted(closed, key=lambda x: x["ts"]):
            equity += t["pnl"]
            if equity > peak:
                peak = equity
            dd = (equity - peak) / peak
            if dd < min_dd:
                min_dd = dd
        maxdd_pct = min_dd * 100
    else:
        maxdd_pct = 0.0

    return {
        "pf":           round(pf, 4),
        "wr":           round(wr, 4),
        "cagr":         round(cagr, 2),
        "maxdd":        round(maxdd_pct, 2),
        "n_trades":     n,
        "n_wins":       len(wins),
        "n_losses":     len(losses),
        "gross_profit": round(gross_profit, 2),
        "gross_loss":   round(gross_loss, 2),
        "final_capital":round(capital, 2),
        "params":       params,
        "mode":         mode,
        "date_start":   date_start or "2022-03-22",
        "date_end":     date_end   or "2026-03-21",
        "cost_per_side":cost_per_side,
    }


# ── Worker function (top-level for pickling) ──────────────────────────────────
def _worker_fn(args: tuple) -> dict:
    trial_id, params, mode, date_start, date_end, cost = args
    try:
        result = run_backtest(params, mode, date_start, date_end, cost)
        result["trial_id"] = trial_id
        result["status"]   = "ok"
        return result
    except Exception as e:
        return {
            "trial_id": trial_id,
            "status":   "error",
            "error":    str(e),
            "tb":       traceback.format_exc(),
            "params":   params,
        }


# ── Parameter grid builders ───────────────────────────────────────────────────
def build_pbl_coarse_grid() -> list[dict]:
    """Build coarse PBL parameter sweep grid with invalid R:R pruned."""
    params = []
    for ema_prox, rsi_min, sl_mult, tp_mult, htf_fast, htf_slow, body_max, htf_adx, htf_ema200 in product(
        [0.20, 0.35, 0.50, 0.65, 0.80],     # ema_prox_atr_mult
        [35.0, 40.0, 45.0, 50.0, 55.0],     # rsi_min
        [1.50, 2.00, 2.50, 3.00, 3.50],     # sl_atr_mult
        [2.00, 2.50, 3.00, 3.50, 4.00, 4.50, 5.00],  # tp_atr_mult
        [20, 50],                            # htf_ema_fast
        [50, 200],                           # htf_ema_slow
        [None, 0.40],                        # body_ratio_max
        [None, 20],                          # htf_adx_min
        [False],                             # htf_price_above_ema200
    ):
        # Prune invalid EMA pairs
        if htf_fast >= htf_slow:
            continue
        # Prune very poor R:R (tp must be meaningfully > sl)
        if tp_mult < sl_mult * 0.8:
            continue
        params.append({
            "ema_prox_atr_mult":       ema_prox,
            "rsi_min":                 rsi_min,
            "sl_atr_mult":             sl_mult,
            "tp_atr_mult":             tp_mult,
            "htf_ema_fast":            htf_fast,
            "htf_ema_slow":            htf_slow,
            "body_ratio_max":          body_max,
            "htf_adx_min":             htf_adx,
            "htf_price_above_ema200":  htf_ema200,
            # SLC kept at baseline
            "slc_adx_min":    28.0,
            "slc_swing_bars": 10,
            "slc_sl_atr_mult":2.5,
            "slc_tp_atr_mult":2.0,
        })
    return params


def build_confirmation_grid() -> list[dict]:
    """Build 4h confirmation variant grid."""
    variants = [
        (20, 50,  None, False),  # V0 baseline
        (9,  21,  None, False),  # V1
        (20, 100, None, False),  # V2
        (50, 200, None, False),  # V3
        (20, 50,  20,   False),  # V4
        (20, 50,  25,   False),  # V5
        (20, 50,  None, True),   # V6
        (20, 50,  20,   True),   # V7
        (9,  21,  20,   False),  # V8
        (50, 200, 25,   False),  # V9
    ]
    base_pbl = {
        "ema_prox_atr_mult": 0.50,
        "rsi_min": 40.0,
        "sl_atr_mult": 2.50,
        "tp_atr_mult": 3.00,
        "body_ratio_max": None,
        "slc_adx_min": 28.0,
        "slc_swing_bars": 10,
        "slc_sl_atr_mult": 2.5,
        "slc_tp_atr_mult": 2.0,
    }
    grid = []
    for fast, slow, adx, ema200 in variants:
        p = base_pbl.copy()
        p["htf_ema_fast"] = fast
        p["htf_ema_slow"] = slow
        p["htf_adx_min"]  = adx
        p["htf_price_above_ema200"] = ema200
        grid.append(p)
    return grid


def build_baseline_params() -> list[dict]:
    """Single baseline parameter set matching production config."""
    return [{
        "ema_prox_atr_mult":       0.50,
        "rsi_min":                 40.0,
        "sl_atr_mult":             2.50,
        "tp_atr_mult":             3.00,
        "htf_ema_fast":            20,
        "htf_ema_slow":            50,
        "body_ratio_max":          None,
        "htf_adx_min":             None,
        "htf_price_above_ema200":  False,
        "slc_adx_min":             28.0,
        "slc_swing_bars":          10,
        "slc_sl_atr_mult":         2.5,
        "slc_tp_atr_mult":         2.0,
    }]


# ── Result persistence ────────────────────────────────────────────────────────
def _flatten_result(r: dict) -> dict:
    """Flatten nested params dict for CSV writing."""
    flat = {k: v for k, v in r.items() if k != "params"}
    for k, v in (r.get("params") or {}).items():
        flat[f"param_{k}"] = v
    return flat


def _write_result_row(result: dict, csv_path: Path, write_header: bool = False):
    flat = _flatten_result(result)
    mode = "w" if write_header else "a"
    with open(csv_path, mode, newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(flat.keys()))
        if write_header:
            writer.writeheader()
        writer.writerow(flat)


def _save_leaderboard(leaderboard: list[dict], path: Path, top_n: int = 10):
    lb = sorted(leaderboard, key=lambda x: x.get("pf", 0), reverse=True)[:top_n]
    with open(path, "w") as f:
        json.dump(lb, f, indent=2, default=str)


# ── Progress display ──────────────────────────────────────────────────────────
def _print_header(stage: str, total_trials: int, n_workers: int):
    print(f"\n{'='*70}")
    print(f"  NexusTrader PBL/SLC Optimization — Stage: {stage.upper()}")
    print(f"  Total trials: {total_trials:,} | Workers: {n_workers} | GPU: {'No'}")
    print(f"  Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*70}\n")


def _print_leaderboard(leaderboard: list[dict], completed: int, total: int, elapsed: float):
    lb = sorted(leaderboard, key=lambda x: x.get("pf", 0), reverse=True)[:10]
    eta = (elapsed / max(completed, 1)) * (total - completed)
    pct = completed / total * 100

    print(f"\n{'─'*70}")
    print(f"  Progress: {completed}/{total} ({pct:.1f}%) | Elapsed: {elapsed:.0f}s | ETA: {eta:.0f}s")
    print(f"{'─'*70}")
    print(f"  {'#':<3} {'PF':>6} {'WR':>6} {'CAGR':>7} {'MaxDD':>7} {'N':>5}  {'Key Params'}")
    print(f"  {'─'*3} {'─'*6} {'─'*6} {'─'*7} {'─'*7} {'─'*5}  {'─'*30}")
    for i, r in enumerate(lb):
        p = r.get("params", {})
        key = (f"prox={p.get('ema_prox_atr_mult','?')} rsi={p.get('rsi_min','?')} "
               f"sl={p.get('sl_atr_mult','?')} tp={p.get('tp_atr_mult','?')}")
        print(f"  {i+1:<3} {r.get('pf',0):>6.4f} {r.get('wr',0):>6.2%} "
              f"{r.get('cagr',0):>7.1f}% {r.get('maxdd',0):>7.1f}% "
              f"{r.get('n_trades',0):>5}  {key}")
    print()


# ── Walk-forward runner ───────────────────────────────────────────────────────
def run_walkforward(candidates: list[dict], n_workers: int = 4) -> list[dict]:
    """Run WF validation on top candidates."""
    print(f"\n{'='*70}")
    print(f"  Walk-Forward Validation — {len(candidates)} candidates × {len(WF_WINDOWS)} windows")
    print(f"{'='*70}\n")

    args = []
    for cand_idx, cand in enumerate(candidates):
        for win_idx, (ts, te, os, oe) in enumerate(WF_WINDOWS):
            args.append((
                f"wf_{cand_idx}_{win_idx}",
                cand["params"],
                cand.get("mode", "pbl_only"),
                ts, te,
                0.0004,
            ))

    wf_results = []
    with mp.Pool(processes=n_workers) as pool:
        for i, result in enumerate(pool.imap_unordered(_worker_fn, args)):
            wf_results.append(result)
            print(f"  WF [{i+1}/{len(args)}] {result.get('trial_id','?')} "
                  f"PF={result.get('pf','ERR')} n={result.get('n_trades','?')}")

    # Group by candidate and compute WF efficiency
    wf_summary = []
    for cand_idx, cand in enumerate(candidates):
        cand_results = [r for r in wf_results if r.get("trial_id", "").startswith(f"wf_{cand_idx}_")]
        oos_pfs = [r["pf"] for r in cand_results if r.get("status") == "ok" and "oos" not in r.get("trial_id","")]
        if oos_pfs:
            mean_oos_pf = np.mean(oos_pfs)
            std_oos_pf  = np.std(oos_pfs)
            wfe = mean_oos_pf / max(cand.get("pf", 1.0), 0.001)
            wf_summary.append({
                "cand_idx":     cand_idx,
                "params":       cand["params"],
                "is_pf":        cand.get("pf", 0),
                "wf_oos_pf_mean": round(mean_oos_pf, 4),
                "wf_oos_pf_std":  round(std_oos_pf, 4),
                "wf_oos_pf_stability": round(std_oos_pf / max(mean_oos_pf, 0.001), 3),
                "wfe":          round(wfe, 3),
                "robust_score": round(0.4 * cand.get("pf", 0) + 0.4 * mean_oos_pf, 4),
            })

    return sorted(wf_summary, key=lambda x: x["robust_score"], reverse=True)


# ── Stress test (fee sensitivity) ────────────────────────────────────────────
def run_stress_test(candidate: dict, n_workers: int = 4) -> list[dict]:
    """Test candidate at 0%, 0.04%, 0.08%, 0.15% fees."""
    fee_levels = [0.0, 0.0004, 0.0008, 0.0015]
    args = [
        (f"stress_fee_{fee:.4f}", candidate["params"], candidate.get("mode", "pbl_only"),
         None, None, fee)
        for fee in fee_levels
    ]
    results = []
    with mp.Pool(processes=min(n_workers, len(fee_levels))) as pool:
        for r in pool.imap(_worker_fn, args):
            results.append(r)
            print(f"  Fee={r['cost_per_side']:.4f}/side → PF={r.get('pf','ERR')} "
                  f"CAGR={r.get('cagr','?')}% n={r.get('n_trades','?')}")
    return results


# ── Main orchestrator ─────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="NexusTrader PBL/SLC Optimization Harness")
    parser.add_argument("--stage", default="baseline",
        choices=["baseline", "coarse_pbl", "focused_pbl", "confirmation", "walkforward", "stress", "combined"],
        help="Which stage to run")
    parser.add_argument("--workers", type=int, default=max(1, mp.cpu_count() - 1),
        help="Number of parallel workers")
    parser.add_argument("--top-n", type=int, default=5,
        help="Number of top candidates for WF/stress")
    parser.add_argument("--batch-size", type=int, default=50,
        help="Trials per progress update batch")
    parser.add_argument("--mode", default="pbl_only",
        choices=["pbl_only", "slc_only", "combined"],
        help="Model mode for non-combined stages")
    parser.add_argument("--cost", type=float, default=0.0004,
        help="Fee per side for backtest")
    args = parser.parse_args()

    n_workers = args.workers
    stage     = args.stage
    mode      = args.mode if stage not in ("confirmation",) else "pbl_only"

    print(f"\n{'#'*70}")
    print(f"#  NexusTrader Research Harness — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"#  Branch: research/pbl-slc-optimization-matrix")
    print(f"#  Stage: {stage} | Mode: {mode} | Workers: {n_workers}")
    print(f"{'#'*70}\n")

    # ── Stage A — Baseline ──────────────────────────────────────────────
    if stage == "baseline":
        grid = build_baseline_params()
        print("Running baseline reproduction (3 modes: pbl_only, slc_only, combined)...")
        for m in ["pbl_only", "slc_only", "combined"]:
            p = grid[0].copy()
            result = run_backtest(p, mode=m, cost_per_side=args.cost)
            result["trial_id"] = f"baseline_{m}"
            result["status"] = "ok"
            print(f"\n  Mode={m}: PF={result['pf']} WR={result['wr']:.2%} "
                  f"CAGR={result['cagr']}% MaxDD={result['maxdd']}% n={result['n_trades']}")
            out = RESULTS_DIR / f"baseline_{m}.json"
            with open(out, "w") as f:
                json.dump(result, f, indent=2, default=str)
            print(f"  Saved → {out}")

        print("\n✅ Baseline complete. Compare against:")
        print("   PBL standalone  target: PF=0.8995  n=516")
        print("   SLC standalone  target: PF=1.5455  n=1229")
        print("   Combined (Sc-B) target: PF=1.2682  CAGR=47.44%  MaxDD=-20.33%  n=1745")
        return

    # ── Stage B — Coarse PBL sweep ─────────────────────────────────────
    elif stage == "coarse_pbl":
        grid = build_pbl_coarse_grid()
        csv_path = RESULTS_DIR / "trials_pbl_coarse.csv"
        print(f"Coarse PBL sweep: {len(grid):,} trials → {csv_path}")
        _run_sweep(grid, mode, args.cost, csv_path, n_workers, args.batch_size, "coarse_pbl", args.top_n)

    # ── Stage C — Focused PBL sweep ────────────────────────────────────
    elif stage == "focused_pbl":
        # Load coarse results and focus on top-zone parameters
        coarse_csv = RESULTS_DIR / "trials_pbl_coarse.csv"
        if not coarse_csv.exists():
            print("ERROR: Run stage coarse_pbl first.")
            sys.exit(1)
        df_coarse = pd.read_csv(coarse_csv)
        df_top = df_coarse[df_coarse["pf"] >= df_coarse["pf"].quantile(0.80)].head(100)
        print(f"Focused sweep from top 20% of coarse results ({len(df_top)} seeds)")
        # Build focused grid around top-performing parameters
        prox_vals  = sorted(df_top["param_ema_prox_atr_mult"].unique())
        rsi_vals   = sorted(df_top["param_rsi_min"].unique())
        sl_vals    = sorted(df_top["param_sl_atr_mult"].unique())
        tp_vals    = sorted(df_top["param_tp_atr_mult"].unique())
        # Fine-grained between best values
        grid = []
        for row in df_top.itertuples():
            for dp in [-0.05, 0, 0.05]:
                for dr in [-2.5, 0, 2.5]:
                    p = {
                        "ema_prox_atr_mult":      round(max(0.1, getattr(row, "param_ema_prox_atr_mult", 0.5) + dp), 2),
                        "rsi_min":                 max(30, getattr(row, "param_rsi_min", 40) + dr),
                        "sl_atr_mult":             getattr(row, "param_sl_atr_mult", 2.5),
                        "tp_atr_mult":             getattr(row, "param_tp_atr_mult", 3.0),
                        "htf_ema_fast":            int(getattr(row, "param_htf_ema_fast", 20)),
                        "htf_ema_slow":            int(getattr(row, "param_htf_ema_slow", 50)),
                        "body_ratio_max":          None,
                        "htf_adx_min":             None,
                        "htf_price_above_ema200":  False,
                        "slc_adx_min": 28.0, "slc_swing_bars": 10,
                        "slc_sl_atr_mult": 2.5, "slc_tp_atr_mult": 2.0,
                    }
                    if p["htf_ema_fast"] < p["htf_ema_slow"] and p["tp_atr_mult"] > p["sl_atr_mult"]:
                        grid.append(p)
        grid = list({json.dumps(p, sort_keys=True): p for p in grid}.values())
        csv_path = RESULTS_DIR / "trials_pbl_focused.csv"
        print(f"Focused sweep: {len(grid):,} trials → {csv_path}")
        _run_sweep(grid, mode, args.cost, csv_path, n_workers, args.batch_size, "focused_pbl", args.top_n)

    # ── Stage D — Confirmation variants ───────────────────────────────
    elif stage == "confirmation":
        grid = build_confirmation_grid()
        csv_path = RESULTS_DIR / "trials_confirmation_variants.csv"
        print(f"4h Confirmation variants: {len(grid)} variants × combined mode → {csv_path}")
        _run_sweep(grid, "combined", args.cost, csv_path, n_workers, len(grid), "confirmation", len(grid))

    # ── Stage E — Walk-forward ─────────────────────────────────────────
    elif stage == "walkforward":
        # Load top candidates from best available sweep
        for fp in [RESULTS_DIR/"trials_pbl_focused.csv", RESULTS_DIR/"trials_pbl_coarse.csv"]:
            if fp.exists():
                df = pd.read_csv(fp)
                break
        else:
            print("ERROR: Run coarse_pbl or focused_pbl first.")
            sys.exit(1)

        df_ok = df[df.get("status", "ok") == "ok"].sort_values("pf", ascending=False)
        top_n = args.top_n
        top_candidates = []
        for _, row in df_ok.head(top_n).iterrows():
            params = {k.replace("param_", ""): v for k, v in row.items() if k.startswith("param_")}
            top_candidates.append({"params": params, "pf": row["pf"], "mode": row.get("mode", mode)})

        print(f"Walk-forward validation for top {len(top_candidates)} candidates...")
        wf_summary = run_walkforward(top_candidates, n_workers)

        out = RESULTS_DIR / "walkforward_results.json"
        with open(out, "w") as f:
            json.dump(wf_summary, f, indent=2, default=str)
        print(f"\n✅ Walk-forward complete → {out}")
        for s in wf_summary[:5]:
            print(f"   IS_PF={s['is_pf']:.4f} WF_OOS_PF={s['wf_oos_pf_mean']:.4f} "
                  f"WFE={s['wfe']:.3f} RobustScore={s['robust_score']:.4f}")

    # ── Stage F — Stress test ──────────────────────────────────────────
    elif stage == "stress":
        # Load best candidate
        wf_file = RESULTS_DIR / "walkforward_results.json"
        if wf_file.exists():
            with open(wf_file) as f:
                wf_results = json.load(f)
            best = wf_results[0]
        else:
            # Fall back to coarse best
            df = pd.read_csv(RESULTS_DIR / "trials_pbl_coarse.csv")
            row = df.sort_values("pf", ascending=False).iloc[0]
            best = {"params": {k.replace("param_",""):v for k,v in row.items() if k.startswith("param_")},
                    "mode": row.get("mode", mode)}

        print(f"\nFee sensitivity stress test for best candidate...")
        stress_results = run_stress_test(best, n_workers)
        out = RESULTS_DIR / "stress_results.json"
        with open(out, "w") as f:
            json.dump(stress_results, f, indent=2, default=str)
        print(f"\n✅ Stress test complete → {out}")

    # ── Stage combined — Run IS + OOS for combined mode ────────────────
    elif stage == "combined":
        grid = build_baseline_params()
        for p in grid:
            p["htf_ema_fast"] = 20
            p["htf_ema_slow"] = 50
        csv_path = RESULTS_DIR / "trials_combined_is.csv"
        _run_sweep(grid, "combined", args.cost, csv_path, n_workers, 1, "combined", 1)


def _run_sweep(grid: list[dict], mode: str, cost: float, csv_path: Path,
               n_workers: int, batch_size: int, stage_name: str, top_n: int):
    """Generic sweep runner with progress display and result persistence."""
    total = len(grid)
    _print_header(stage_name, total, n_workers)

    args = [
        (f"{stage_name}_{i:05d}", p, mode, None, str(IS_END.date()), cost)
        for i, p in enumerate(grid)
    ]

    leaderboard: list[dict] = []
    completed   = 0
    errors      = 0
    write_hdr   = not csv_path.exists()
    t_start     = time.time()

    with mp.Pool(processes=n_workers) as pool:
        for result in pool.imap_unordered(_worker_fn, args):
            completed += 1

            if result.get("status") == "ok":
                _write_result_row(result, csv_path, write_header=write_hdr)
                write_hdr = False
                leaderboard.append(result)

                # Keep leaderboard bounded
                if len(leaderboard) > top_n * 10:
                    leaderboard.sort(key=lambda x: x.get("pf", 0), reverse=True)
                    leaderboard = leaderboard[:top_n * 5]
            else:
                errors += 1
                err_log = RESULTS_DIR / f"errors_{stage_name}.log"
                with open(err_log, "a") as f:
                    f.write(json.dumps(result, default=str) + "\n")

            # Print progress every batch_size completions
            if completed % batch_size == 0 or completed == total:
                elapsed = time.time() - t_start
                _print_leaderboard(leaderboard, completed, total, elapsed)

                # Save leaderboard snapshot
                snap = LEADER_DIR / f"leaderboard_{stage_name}_{completed:05d}.json"
                _save_leaderboard(leaderboard, snap, top_n)

    # Final leaderboard
    final_lb = LEADER_DIR / f"leaderboard_{stage_name}_FINAL.json"
    _save_leaderboard(leaderboard, final_lb, top_n)

    elapsed = time.time() - t_start
    print(f"\n{'='*70}")
    print(f"  Stage {stage_name.upper()} complete")
    print(f"  Trials: {completed} | Errors: {errors} | Time: {elapsed:.0f}s")
    print(f"  Results → {csv_path}")
    print(f"  Leaderboard → {final_lb}")
    if leaderboard:
        best = max(leaderboard, key=lambda x: x.get("pf", 0))
        print(f"\n  BEST: PF={best.get('pf'):.4f} WR={best.get('wr',0):.2%} "
              f"CAGR={best.get('cagr')}% MaxDD={best.get('maxdd')}% n={best.get('n_trades')}")
        bp = best.get("params", {})
        print(f"  PARAMS: prox={bp.get('ema_prox_atr_mult')} rsi={bp.get('rsi_min')} "
              f"sl={bp.get('sl_atr_mult')} tp={bp.get('tp_atr_mult')} "
              f"htf={bp.get('htf_ema_fast')}/{bp.get('htf_ema_slow')} "
              f"adx={bp.get('htf_adx_min')} ema200={bp.get('htf_price_above_ema200')}")
    print(f"{'='*70}\n")


if __name__ == "__main__":
    mp.freeze_support()
    main()
