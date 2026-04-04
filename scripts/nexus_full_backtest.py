"""
NexusTrader Full 4-Year Parallel Backtest
==========================================
Fetches FULL historical data from Bybit (4 years of 30m + 4H OHLCV),
then runs all 5 capital allocation models in parallel across CPU cores.

Usage (run from the NexusTrader root directory):
    python scripts/nexus_full_backtest.py

Requirements:
    pip install ccxt pandas numpy pyarrow tqdm

Features:
    - Downloads 30m + 4H BTC data from Bybit (4 years or more)
    - Caches data locally to avoid re-downloading
    - Runs Models A–E in parallel using all available CPU cores
    - Full compounding (equity grows/shrinks with each trade)
    - ATR-based position sizing with per-model risk caps
    - Partial exit at 1R (33%) + breakeven SL (production v1.2 config)
    - 4H HTF regime gate (production multi-TF confirmation)
    - Yearly breakdown, monthly returns, regime analysis
    - Saves comprehensive results JSON + prints formatted tables

Capital Models:
    A  0.5% risk,  4% max cap  (current Phase 1 demo)
    B  1.0% risk,  8% max cap  (Phase 2 standard)
    C  1.5% risk, 12% max cap  (Phase 2 aggressive)
    D  Conviction-based 0.5–1.5% scaled by confluence score
    E  2.0% risk, 15% max cap  (maximum — use with caution)
"""

from __future__ import annotations

import os
import sys
import json
import math
import time
import logging
import argparse
import statistics
import multiprocessing as mp
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import Optional

import numpy as np
import pandas as pd

# ── Paths ──────────────────────────────────────────────────────────────────────
SCRIPT_DIR   = Path(__file__).parent
ROOT_DIR     = SCRIPT_DIR.parent
CACHE_DIR    = ROOT_DIR / "data" / "backtest_cache"
RESULTS_DIR  = ROOT_DIR / "reports"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
RESULTS_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# ── Regime constants ───────────────────────────────────────────────────────────
R_BULL      = "bull_trend"
R_BEAR      = "bear_trend"
R_RANGING   = "ranging"
R_VOL_EXP   = "volatility_expansion"
R_VOL_COMP  = "volatility_compression"
R_UNCERTAIN = "uncertain"

REGIME_ATR_SL: dict[str, float] = {
    R_BULL: 1.875, R_BEAR: 1.875, R_RANGING: 3.125,
    R_VOL_EXP: 3.75, R_VOL_COMP: 2.25, R_UNCERTAIN: 2.5,
}

# ── Production config (matches CLAUDE.md v1.2 Phase 5) ────────────────────────
PROD_PARAMS = {
    "confluence_threshold": 0.45,
    "trend_adx_min":        31.0,   # v1.2 Phase 5 lever
    "trend_rsi_long_min":   45.0,
    "trend_rsi_long_max":   70.0,
    "trend_rsi_short_min":  30.0,
    "trend_rsi_short_max":  55.0,
    "trend_strength_base":  0.15,
    "trend_adx_bonus_max":  0.40,
    "mb_lookback":          20,
    "mb_vol_mult_min":      1.5,
    "mb_rsi_bullish":       55.0,
    "mb_rsi_bearish":       45.0,
    "mb_strength_base":     0.35,
    "bb_expansion_ratio":   2.5,
    "bb_compression_ratio": 0.5,
    "adx_trend_thresh":     25.0,
    "adx_ranging_thresh":   20.0,
    "fee_pct":              0.04,
    "slippage_pct":         0.05,
    "warmup_bars":          100,
    "partial_pct":          0.33,
    "partial_r_trigger":    1.0,
    "htf_allowed_regimes":  [R_BULL, R_BEAR, R_VOL_EXP],
}

INITIAL_CAPITAL = 100_000.0

# ── Capital model definitions ──────────────────────────────────────────────────
CAPITAL_MODELS = {
    "A_Current": {
        "label": "Model A — Current Phase 1 (0.5% risk, 4% cap)",
        "risk_pct": 0.005,
        "max_cap_pct": 0.04,
        "description": "Production Phase 1 demo config. Conservative. Use until 50+ live trades confirm PF >= 1.3.",
    },
    "B_Moderate": {
        "label": "Model B — Moderate / Phase 2 Standard (1.0% risk, 8% cap)",
        "risk_pct": 0.010,
        "max_cap_pct": 0.08,
        "description": "Standard algorithmic trading allocation. Appropriate for Phase 2 after 50+ validated trades.",
    },
    "C_Aggressive": {
        "label": "Model C — Aggressive / Phase 2 Advanced (1.5% risk, 12% cap)",
        "risk_pct": 0.015,
        "max_cap_pct": 0.12,
        "description": "High-growth allocation. Matches Phase 2 tiered solo standard cap. Requires PF >= 1.5 live.",
    },
    "D_Conviction": {
        "label": "Model D — Conviction-Based (0.5%–1.5% by confluence score)",
        "risk_pct": None,   # dynamic — computed per trade
        "max_cap_pct": 0.12,
        "description": "Scales risk with confluence score. Score 0.45 → 0.5%, Score 0.75+ → 1.5%. Phase 2 eligible.",
    },
    "E_MaxRisk": {
        "label": "Model E — Maximum Utilization (2.0% risk, 15% cap)",
        "risk_pct": 0.020,
        "max_cap_pct": 0.15,
        "description": "Extreme allocation. Kelly fraction is ~37.9% — this is 1/19th Kelly. For analysis only.",
    },
}


# ══════════════════════════════════════════════════════════════════════════════
# DATA FETCHING
# ══════════════════════════════════════════════════════════════════════════════

def fetch_ohlcv_from_bybit(symbol: str, timeframe: str, years: int = 4) -> pd.DataFrame:
    """
    Fetch OHLCV data from Bybit using ccxt.
    Paginates backwards to get the full history.
    Caches result as parquet for reuse.
    """
    cache_file = CACHE_DIR / f"{symbol}_{timeframe}_{years}yr.parquet"
    if cache_file.exists():
        logger.info(f"Loading cached data: {cache_file}")
        df = pd.read_parquet(cache_file)
        logger.info(f"  {len(df)} bars from {df['timestamp'].iloc[0]} to {df['timestamp'].iloc[-1]}")
        return df

    try:
        import ccxt
    except ImportError:
        raise ImportError("ccxt not installed. Run: pip install ccxt")

    exchange = ccxt.bybit({"enableRateLimit": True})
    exchange.load_markets()

    tf_ms = {
        "1m": 60_000, "5m": 300_000, "15m": 900_000,
        "30m": 1_800_000, "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000,
    }
    ms_per_bar = tf_ms.get(timeframe)
    if ms_per_bar is None:
        raise ValueError(f"Unknown timeframe: {timeframe}")

    end_ms   = int(time.time() * 1000)
    start_ms = end_ms - years * 365 * 24 * 3600 * 1000
    limit    = 1000  # Bybit max per request

    all_bars = []
    since    = start_ms
    logger.info(f"Fetching {symbol} {timeframe} from Bybit (since {datetime.fromtimestamp(since/1000).date()})...")

    try:
        from tqdm import tqdm
        pbar = tqdm(total=(end_ms - start_ms) // ms_per_bar, desc=f"{symbol} {timeframe}")
    except ImportError:
        pbar = None

    while since < end_ms:
        try:
            bars = exchange.fetch_ohlcv(symbol, timeframe, since=since, limit=limit)
        except Exception as e:
            logger.warning(f"Fetch error at {datetime.fromtimestamp(since/1000)}: {e}. Retrying...")
            time.sleep(2)
            try:
                bars = exchange.fetch_ohlcv(symbol, timeframe, since=since, limit=limit)
            except Exception:
                break

        if not bars:
            break

        all_bars.extend(bars)
        since = bars[-1][0] + ms_per_bar

        if pbar:
            pbar.update(len(bars))
        else:
            pct = (since - start_ms) / (end_ms - start_ms) * 100
            logger.info(f"  Progress: {pct:.1f}% | {len(all_bars)} bars")

        time.sleep(exchange.rateLimit / 1000)

    if pbar:
        pbar.close()

    if not all_bars:
        raise ValueError(f"No data fetched for {symbol} {timeframe}")

    df = pd.DataFrame(all_bars, columns=["ts_ms", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["ts_ms"], unit="ms", utc=True)
    df = df.drop_duplicates("ts_ms").sort_values("ts_ms").reset_index(drop=True)
    df = df[["timestamp", "open", "high", "low", "close", "volume"]]

    df.to_parquet(cache_file, index=False)
    logger.info(f"Saved: {cache_file} ({len(df)} bars, {df['timestamp'].iloc[0].date()} to {df['timestamp'].iloc[-1].date()})")
    return df


def load_or_fetch(symbol: str, timeframe: str, years: int = 4,
                  local_path: Optional[str] = None) -> pd.DataFrame:
    """Load from local parquet or fetch from Bybit."""
    if local_path and Path(local_path).exists():
        logger.info(f"Loading local data: {local_path}")
        df = pd.read_parquet(local_path)
        df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
        return df.sort_values("timestamp").reset_index(drop=True)
    return fetch_ohlcv_from_bybit(symbol, timeframe, years)


# ══════════════════════════════════════════════════════════════════════════════
# INDICATOR COMPUTATION
# ══════════════════════════════════════════════════════════════════════════════

def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Compute all required indicators. Pure numpy/pandas — no Qt dependency."""
    df = df.copy()
    n     = len(df)
    close = df["close"].values.astype(np.float64)
    high  = df["high"].values.astype(np.float64)
    low   = df["low"].values.astype(np.float64)
    vol   = df["volume"].values.astype(np.float64)

    # ── EMA ────────────────────────────────────────────────────────────────
    def ema(src: np.ndarray, period: int) -> np.ndarray:
        result = np.full(n, np.nan)
        k = 2.0 / (period + 1)
        s = 0
        while s < n and np.isnan(src[s]):
            s += 1
        if s >= n:
            return result
        result[s] = src[s]
        for i in range(s + 1, n):
            result[i] = src[i] * k + result[i-1] * (1.0 - k)
        return result

    df["ema_9"]  = ema(close, 9)
    df["ema_21"] = ema(close, 21)
    df["ema_50"] = ema(close, 50)

    # ── ATR (Wilder's) ────────────────────────────────────────────────────
    prev_close = np.roll(close, 1)
    prev_close[0] = close[0]
    tr = np.maximum(high - low, np.maximum(np.abs(high - prev_close), np.abs(low - prev_close)))
    atr = np.full(n, np.nan)
    p14 = 14
    if n >= p14:
        atr[p14-1] = np.mean(tr[:p14])
        for i in range(p14, n):
            atr[i] = (atr[i-1] * (p14-1) + tr[i]) / p14
    df["atr"] = atr

    # ── RSI ───────────────────────────────────────────────────────────────
    delta = np.diff(close, prepend=close[0])
    gain  = np.where(delta > 0, delta, 0.0)
    loss  = np.where(delta < 0, -delta, 0.0)
    rp = 14
    avg_g = np.full(n, np.nan)
    avg_l = np.full(n, np.nan)
    if n >= rp + 1:
        avg_g[rp] = np.mean(gain[1:rp+1])
        avg_l[rp] = np.mean(loss[1:rp+1])
        for i in range(rp+1, n):
            avg_g[i] = (avg_g[i-1] * (rp-1) + gain[i]) / rp
            avg_l[i] = (avg_l[i-1] * (rp-1) + loss[i]) / rp
    with np.errstate(divide='ignore', invalid='ignore'):
        rs = np.where(avg_l > 0, avg_g / avg_l, np.where(avg_g > 0, 100.0, 1.0))
    df["rsi"] = np.where(avg_l == 0, 100.0, 100.0 - 100.0 / (1.0 + rs))

    # ── ADX ───────────────────────────────────────────────────────────────
    pdm  = np.maximum(np.diff(high, prepend=high[0]), 0.0)
    mdm  = np.maximum(-np.diff(low,  prepend=low[0]), 0.0)
    both = pdm < mdm;  pdm[both] = 0.0
    both2= mdm <= pdm; mdm[both2]= 0.0
    p = 14
    sm_tr  = np.full(n, np.nan)
    sm_pdm = np.full(n, np.nan)
    sm_mdm = np.full(n, np.nan)
    if n >= p:
        sm_tr[p-1]  = np.sum(tr[1:p+1]) if n > p else np.sum(tr[:p])
        sm_pdm[p-1] = np.sum(pdm[1:p+1]) if n > p else np.sum(pdm[:p])
        sm_mdm[p-1] = np.sum(mdm[1:p+1]) if n > p else np.sum(mdm[:p])
        for i in range(p, n):
            sm_tr[i]  = sm_tr[i-1]  - sm_tr[i-1]/p  + tr[i]
            sm_pdm[i] = sm_pdm[i-1] - sm_pdm[i-1]/p + pdm[i]
            sm_mdm[i] = sm_mdm[i-1] - sm_mdm[i-1]/p + mdm[i]
    with np.errstate(divide='ignore', invalid='ignore'):
        pdi = np.where(sm_tr > 0, sm_pdm / sm_tr * 100.0, 0.0)
        mdi = np.where(sm_tr > 0, sm_mdm / sm_tr * 100.0, 0.0)
        dx  = np.where((pdi + mdi) > 0, np.abs(pdi - mdi) / (pdi + mdi) * 100.0, 0.0)
    adx_raw = np.full(n, np.nan)
    s2 = 2 * p - 1
    if n > s2:
        adx_raw[s2] = np.mean(dx[p-1:s2+1])
        for i in range(s2+1, n):
            adx_raw[i] = (adx_raw[i-1] * (p-1) + dx[i]) / p
    df["adx"] = adx_raw

    # ── Bollinger Bands width ─────────────────────────────────────────────
    cs = pd.Series(close)
    bb_mid = cs.rolling(20, min_periods=20).mean()
    bb_std = cs.rolling(20, min_periods=20).std()
    df["bb_width"] = ((bb_mid + 2.0*bb_std) - (bb_mid - 2.0*bb_std)).values

    # ── MACD (for reference / extended analysis) ──────────────────────────
    df["macd"]        = ema(close, 12) - ema(close, 26)
    macd_arr = ema(close, 12) - ema(close, 26)
    df["macd_signal"] = ema(macd_arr, 9)

    return df


# ══════════════════════════════════════════════════════════════════════════════
# REGIME CLASSIFICATION
# ══════════════════════════════════════════════════════════════════════════════

def classify_regime(df: pd.DataFrame, p: dict) -> np.ndarray:
    """Vectorized rule-based regime classifier (matches production logic)."""
    n = len(df)
    regime = np.full(n, R_UNCERTAIN, dtype=object)

    adx   = df["adx"].values.astype(np.float64)
    ema9  = df["ema_9"].values.astype(np.float64)
    ema21 = df["ema_21"].values.astype(np.float64)
    bb_w  = df["bb_width"].values.astype(np.float64)

    bb_roll = pd.Series(bb_w).rolling(20, min_periods=5).mean().values
    with np.errstate(divide='ignore', invalid='ignore'):
        bb_r = np.where(bb_roll > 0, bb_w / bb_roll, 1.0)

    slope = np.sign(ema9 - ema21)
    at = p["adx_trend_thresh"]; ar = p["adx_ranging_thresh"]
    be = p["bb_expansion_ratio"]; bc = p["bb_compression_ratio"]

    valid = ~np.isnan(adx)
    regime[valid & (adx < ar)]                    = R_RANGING
    regime[valid & (adx >= ar) & (adx < at)]      = R_RANGING
    mask_t = valid & (adx >= at)
    regime[mask_t & (slope > 0)]                  = R_BULL
    regime[mask_t & (slope <= 0)]                 = R_BEAR
    regime[bb_r > be]                             = R_VOL_EXP
    regime[bb_r < bc]                             = R_VOL_COMP
    regime[np.isnan(adx)]                         = R_UNCERTAIN
    return regime


# ══════════════════════════════════════════════════════════════════════════════
# SIGNAL GENERATION
# ══════════════════════════════════════════════════════════════════════════════

def generate_signals(df: pd.DataFrame, regime: np.ndarray,
                     p: dict) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Generate TrendModel + MomentumBreakout signals."""
    n     = len(df)
    rsi   = df["rsi"].values.astype(np.float64)
    adx   = df["adx"].values.astype(np.float64)
    ema9  = df["ema_9"].values.astype(np.float64)
    ema21 = df["ema_21"].values.astype(np.float64)
    close = df["close"].values.astype(np.float64)
    vol   = df["volume"].values.astype(np.float64)

    sl = np.zeros(n, dtype=bool)
    ss = np.zeros(n, dtype=bool)
    sc = np.zeros(n, dtype=np.float64)

    # TrendModel
    in_trend = np.isin(regime, [R_BULL, R_BEAR])
    has_adx  = (adx >= p["trend_adx_min"]) & ~np.isnan(adx)
    tlc = in_trend & has_adx & (ema9 > ema21) & (rsi >= p["trend_rsi_long_min"])  & (rsi <= p["trend_rsi_long_max"])
    tsc = in_trend & has_adx & (ema9 < ema21) & (rsi >= p["trend_rsi_short_min"]) & (rsi <= p["trend_rsi_short_max"])
    adx_bonus = np.minimum(p["trend_adx_bonus_max"],
                np.where(has_adx, (adx - p["trend_adx_min"]) / p["trend_adx_min"] * p["trend_adx_bonus_max"], 0.0))
    ts = p["trend_strength_base"] + adx_bonus
    sl |= tlc; ss |= tsc
    sc = np.where(tlc | tsc, np.maximum(sc, ts), sc)

    # MomentumBreakout
    ive  = regime == R_VOL_EXP
    cs2  = pd.Series(close); vs2 = pd.Series(vol)
    rh   = cs2.shift(1).rolling(p["mb_lookback"], min_periods=p["mb_lookback"]).max().values
    rl   = cs2.shift(1).rolling(p["mb_lookback"], min_periods=p["mb_lookback"]).min().values
    va   = vs2.rolling(p["mb_lookback"], min_periods=p["mb_lookback"]).mean().values
    vp   = (vol > va * p["mb_vol_mult_min"]) & (va > 0)
    rhs  = np.where(np.isnan(rh), close * 1e9, rh)
    rls  = np.where(np.isnan(rl), 0.0, rl)
    mlc  = ive & vp & (close > rhs) & (rsi > p["mb_rsi_bullish"])
    msc  = ive & vp & (close < rls) & (rsi < p["mb_rsi_bearish"])
    with np.errstate(divide='ignore', invalid='ignore'):
        bpct = np.where(rhs > 0, (close - rhs) / rhs * 100.0, 0.0)
    bscore = np.minimum(1.0, bpct / 2.0)
    with np.errstate(divide='ignore', invalid='ignore'):
        vr = np.where(va > 0, vol / va, 1.0)
    vscore = np.minimum(1.0, (vr - p["mb_vol_mult_min"]) / p["mb_vol_mult_min"])
    ms = p["mb_strength_base"] + vscore * 0.35 + bscore * 0.3
    sl |= mlc; ss |= msc
    sc = np.where(mlc | msc, np.maximum(sc, ms), sc)

    return sl, ss, sc


# ══════════════════════════════════════════════════════════════════════════════
# POSITION SIZING
# ══════════════════════════════════════════════════════════════════════════════

def size_position(equity: float, stop_pct: float, model_id: str,
                  risk_pct: Optional[float], max_cap_pct: float,
                  score: float) -> float:
    if stop_pct <= 0:
        return 0.0
    if model_id == "D_Conviction":
        # Score 0.45 → 0.5%, Score 0.75+ → 1.5%
        s_norm   = min(1.0, max(0.0, (score - 0.45) / 0.30))
        risk_pct = 0.005 + s_norm * 0.010
    rp = risk_pct if risk_pct is not None else 0.005
    risk_usdt = equity * rp
    raw_size  = risk_usdt / stop_pct
    return min(raw_size, equity * max_cap_pct)


# ══════════════════════════════════════════════════════════════════════════════
# SEQUENTIAL BAR-BY-BAR SIMULATOR
# ══════════════════════════════════════════════════════════════════════════════

def simulate_model(args: tuple) -> dict:
    """
    Top-level worker function — must be module-level for multiprocessing pickling.
    Runs a single capital model on pre-computed signals.
    """
    (model_id, model_cfg, df_dict, regime_arr, el, es, sc_arr, p, cap0) = args

    df = pd.DataFrame(df_dict)
    fee  = p["fee_pct"] / 100.0
    slip = p["slippage_pct"] / 100.0
    pp   = p.get("partial_pct", 0.33)
    pr   = p.get("partial_r_trigger", 1.0)
    risk_pct    = model_cfg["risk_pct"]
    max_cap_pct = model_cfg["max_cap_pct"]

    o_  = df["open"].values.astype(np.float64)
    h_  = df["high"].values.astype(np.float64)
    l_  = df["low"].values.astype(np.float64)
    c_  = df["close"].values.astype(np.float64)
    a_  = df["atr"].values.astype(np.float64)
    ts_ = df["timestamp"].astype(str).values
    n   = len(df)

    trades   = []
    position = None
    pending  = None
    equity   = cap0
    eq_curve = [{"ts": ts_[0], "equity": equity, "bar": 0}]

    for i in range(1, n):
        # ── Fill pending entry at next bar's open ─────────────────────
        if pending is not None and position is None:
            pd_, psl, ptp, pr2, ps, r1r = pending
            fill = o_[i] * (1.0 + slip) if pd_ == "long" else o_[i] * (1.0 - slip)
            sdist = abs(fill - psl)
            spct  = sdist / fill if fill > 0 else 0.001
            sz    = size_position(equity, spct, model_id, risk_pct, max_cap_pct, ps)
            if sz > 0 and fill > 0:
                qty    = sz / fill
                e_fee  = sz * fee
                equity -= e_fee
                position = {
                    "dir": pd_, "i0": i, "px0": fill, "qty": qty, "qty0": qty,
                    "sl": psl, "sl0": psl, "tp": ptp, "regime": pr2,
                    "score": ps, "r1r": r1r, "partial": False,
                    "ts0": ts_[i], "sz": sz,
                }
            pending = None

        # ── Manage open position ──────────────────────────────────────
        if position is not None:
            d   = position["dir"]
            sl2 = position["sl"]
            tp2 = position["tp"]
            epx = position["px0"]
            qty = position["qty"]
            sl0 = position["sl0"]

            # Partial exit at 1R
            if not position["partial"]:
                pnl_if_close = (h_[i] - epx) if d == "long" else (epx - l_[i])
                threshold_dist = abs(epx - sl0) * pr
                if pnl_if_close >= threshold_dist:
                    ppx   = epx + threshold_dist if d == "long" else epx - threshold_dist
                    pfill = ppx * (1.0 - slip) if d == "long" else ppx * (1.0 + slip)
                    pqty  = qty * pp
                    ppnl  = ((pfill - epx) * pqty if d == "long" else (epx - pfill) * pqty) \
                            - pfill * pqty * fee
                    equity           += ppnl
                    position["qty"]  -= pqty
                    position["partial"] = True
                    position["sl"]   = epx   # breakeven SL
                    trades.append({
                        "entry_i": position["i0"], "exit_i": i,
                        "entry_px": epx, "exit_px": round(pfill, 4),
                        "dir": d, "pnl": round(ppnl, 4),
                        "dur_bars": i - position["i0"],
                        "exit_reason": "partial_close",
                        "regime": position["regime"], "score": position["score"],
                        "entry_ts": position["ts0"], "exit_ts": ts_[i],
                        "sz": position["sz"] * pp,
                    })

            # Full exit
            xpx = None; rsn = None
            if d == "long":
                if l_[i] <= sl2: xpx, rsn = sl2, "stop_loss"
                elif h_[i] >= tp2: xpx, rsn = tp2, "take_profit"
            else:
                if h_[i] >= sl2: xpx, rsn = sl2, "stop_loss"
                elif l_[i] <= tp2: xpx, rsn = tp2, "take_profit"

            if rsn:
                xfill = xpx * (1.0 - slip) if d == "long" else xpx * (1.0 + slip)
                xqty  = position["qty"]
                xfee  = xfill * xqty * fee
                pnl   = ((xfill - epx) * xqty - xfee if d == "long"
                         else (epx - xfill) * xqty - xfee)
                equity += pnl
                eq_curve.append({"ts": ts_[i], "equity": round(equity, 2), "bar": i})
                trades.append({
                    "entry_i": position["i0"], "exit_i": i,
                    "entry_px": epx, "exit_px": round(xfill, 4),
                    "dir": d, "pnl": round(pnl, 4),
                    "dur_bars": i - position["i0"],
                    "exit_reason": rsn,
                    "regime": position["regime"], "score": position["score"],
                    "entry_ts": position["ts0"], "exit_ts": ts_[i],
                    "sz": position["sz"],
                })
                position = None

        # ── New entry signal ──────────────────────────────────────────
        if position is None and pending is None:
            ri    = regime_arr[i]
            ai    = a_[i] if not math.isnan(a_[i]) else c_[i] * 0.02
            asl_m = REGIME_ATR_SL.get(ri, 2.0)
            if el[i]:
                sl_ = c_[i] - ai * asl_m
                tp_ = c_[i] + ai * (asl_m + 1.0)
                pending = ("long",  sl_, tp_, ri, sc_arr[i], ai * asl_m)
            elif es[i]:
                sl_ = c_[i] + ai * asl_m
                tp_ = c_[i] - ai * (asl_m + 1.0)
                pending = ("short", sl_, tp_, ri, sc_arr[i], ai * asl_m)

        # Append equity snapshot every 48 bars (~8 days at 4H)
        if i % 48 == 0:
            eq_curve.append({"ts": ts_[i], "equity": round(equity, 2), "bar": i})

    # Force-close remaining position
    if position is not None:
        d     = position["dir"]
        xfill = c_[n-1] * (1.0 - slip if d == "long" else 1.0 + slip)
        xqty  = position["qty"]
        xfee  = xfill * xqty * fee
        pnl   = ((xfill - position["px0"]) * xqty - xfee if d == "long"
                 else (position["px0"] - xfill) * xqty - xfee)
        equity += pnl
        trades.append({
            "entry_i": position["i0"], "exit_i": n-1,
            "entry_px": position["px0"], "exit_px": round(xfill, 4),
            "dir": d, "pnl": round(pnl, 4),
            "dur_bars": n-1 - position["i0"],
            "exit_reason": "end_of_data",
            "regime": position["regime"], "score": position["score"],
            "entry_ts": position["ts0"], "exit_ts": ts_[n-1],
            "sz": position["sz"],
        })
    eq_curve.append({"ts": ts_[n-1], "equity": round(equity, 2), "bar": n-1})

    return {
        "model_id":  model_id,
        "model_cfg": model_cfg,
        "trades":    trades,
        "eq_curve":  eq_curve,
        "final_equity": round(equity, 2),
    }


# ══════════════════════════════════════════════════════════════════════════════
# METRICS
# ══════════════════════════════════════════════════════════════════════════════

def compute_full_metrics(trades: list, eq_curve: list, cap0: float) -> dict:
    """Compute comprehensive performance metrics."""
    if not trades:
        return {"error": "no_trades"}

    fc = [t for t in trades if t["exit_reason"] != "partial_close"]
    pc = [t for t in trades if t["exit_reason"] == "partial_close"]

    wins = [t for t in fc if t["pnl"] > 0]
    loss = [t for t in fc if t["pnl"] <= 0]
    gw   = sum(t["pnl"] for t in wins)
    gl   = abs(sum(t["pnl"] for t in loss))
    net  = sum(t["pnl"] for t in trades)
    final = cap0 + net

    pf = gw / gl if gl > 0 else 999.0

    # Drawdown from equity curve
    equities = np.array([e["equity"] for e in eq_curve])
    peak     = np.maximum.accumulate(equities)
    dd_abs   = peak - equities
    dd_pct   = dd_abs / peak * 100.0
    max_dd   = float(np.max(dd_pct))
    max_dd_abs = float(np.max(dd_abs))

    # Duration
    try:
        start = pd.to_datetime(eq_curve[0]["ts"]).tz_localize(None) if pd.to_datetime(eq_curve[0]["ts"]).tzinfo is None else pd.to_datetime(eq_curve[0]["ts"]).tz_localize(None)
    except:
        start = pd.to_datetime(eq_curve[0]["ts"])
    try:
        end = pd.to_datetime(eq_curve[-1]["ts"]).tz_localize(None) if pd.to_datetime(eq_curve[-1]["ts"]).tzinfo is None else pd.to_datetime(eq_curve[-1]["ts"]).tz_localize(None)
    except:
        end = pd.to_datetime(eq_curve[-1]["ts"])

    try:
        yrs = max(0.01, (end - start).days / 365.25)
    except:
        yrs = 4.0

    # CAGR
    cagr = (final / cap0) ** (1.0 / yrs) - 1.0 if final > 0 else -1.0

    # Monthly returns
    monthly: dict[str, float] = {}
    for t in fc:
        try:
            k = str(t["exit_ts"])[:7]  # "YYYY-MM"
            monthly[k] = monthly.get(k, 0.0) + t["pnl"]
        except:
            pass

    # Yearly returns
    yearly: dict[str, dict] = {}
    for t in fc:
        try:
            y = str(t["exit_ts"])[:4]
            if y not in yearly:
                yearly[y] = {"pnl": 0.0, "trades": 0, "wins": 0}
            yearly[y]["pnl"]    += t["pnl"]
            yearly[y]["trades"] += 1
            if t["pnl"] > 0:
                yearly[y]["wins"] += 1
        except:
            pass

    # Regime breakdown
    by_regime: dict[str, dict] = {}
    for t in fc:
        r = t.get("regime", "unknown")
        if r not in by_regime:
            by_regime[r] = {"count": 0, "wins": 0, "pnl": 0.0}
        by_regime[r]["count"] += 1
        if t["pnl"] > 0:
            by_regime[r]["wins"] += 1
        by_regime[r]["pnl"] += t["pnl"]

    # Average hold time
    holds = [t["dur_bars"] for t in fc]
    avg_hold = statistics.mean(holds) if holds else 0

    # Sharpe per trade (unit: per-trade Sharpe)
    pnls = [t["pnl"] for t in fc]
    sharpe = (statistics.mean(pnls) / statistics.stdev(pnls)
              if len(pnls) > 1 and statistics.stdev(pnls) > 0 else 0.0)

    # Consecutive losses
    max_consec_loss = 0
    consec = 0
    for t in fc:
        if t["pnl"] <= 0:
            consec += 1
            max_consec_loss = max(max_consec_loss, consec)
        else:
            consec = 0

    # Avg R (normalized to stop-risk)
    # We can estimate avg_r from PF and WR: avg_r = WR * W/L - (1-WR)
    wr  = len(wins) / len(fc) if fc else 0
    if wins and loss:
        avg_w = gw / len(wins)
        avg_l = gl / len(loss) if loss else 1
        avg_r = wr * (avg_w / avg_l) - (1 - wr)
    else:
        avg_r = 0.0

    return {
        "total_trades_full":     len(fc),
        "total_trades_partial":  len(pc),
        "total_trades_all":      len(trades),
        "win_rate_pct":          round(wr * 100, 2),
        "profit_factor":         round(pf, 4),
        "gross_profit":          round(gw, 2),
        "gross_loss":            round(gl, 2),
        "net_pnl_total":         round(net, 2),
        "net_pnl_full_closes":   round(sum(t["pnl"] for t in fc), 2),
        "net_pnl_partials":      round(sum(t["pnl"] for t in pc), 2),
        "initial_capital":       cap0,
        "final_equity":          round(final, 2),
        "total_return_pct":      round((final - cap0) / cap0 * 100, 2),
        "max_drawdown_pct":      round(max_dd, 2),
        "max_drawdown_abs":      round(max_dd_abs, 2),
        "cagr_pct":              round(cagr * 100, 2),
        "years":                 round(yrs, 2),
        "sharpe_per_trade":      round(sharpe, 4),
        "avg_hold_bars":         round(avg_hold, 1),
        "avg_r_per_trade":       round(avg_r, 3),
        "max_consec_losses":     max_consec_loss,
        "monthly_returns":       {k: round(v, 2) for k, v in sorted(monthly.items())},
        "yearly_returns":        {y: {
                                    "pnl": round(s["pnl"], 2),
                                    "trades": s["trades"],
                                    "wr_pct": round(s["wins"]/s["trades"]*100 if s["trades"] else 0, 1),
                                  } for y, s in sorted(yearly.items())},
        "regime_breakdown":      {r: {
                                    "count": s["count"],
                                    "wr_pct": round(s["wins"]/s["count"]*100 if s["count"] else 0, 1),
                                    "pnl": round(s["pnl"], 2),
                                  } for r, s in sorted(by_regime.items())},
        "stop_loss_pct":  round(sum(1 for t in fc if t["exit_reason"]=="stop_loss")    / len(fc) * 100 if fc else 0, 1),
        "take_profit_pct":round(sum(1 for t in fc if t["exit_reason"]=="take_profit")  / len(fc) * 100 if fc else 0, 1),
        "end_of_data_pct":round(sum(1 for t in fc if t["exit_reason"]=="end_of_data")  / len(fc) * 100 if fc else 0, 1),
    }


# ══════════════════════════════════════════════════════════════════════════════
# PRINT UTILITIES
# ══════════════════════════════════════════════════════════════════════════════

def print_separator(char="=", width=90):
    print(char * width)

def print_section(title):
    print_separator()
    print(f"  {title}")
    print_separator()

def fmt_money(v):
    return f"${v:>12,.0f}"

def print_comparison_table(results: dict):
    """Print multi-model comparison table."""
    print_separator()
    print(f"  {'CAPITAL MODEL COMPARISON':^86}")
    print_separator()
    hdr = (f"  {'Model':<22} {'Final':>12} {'Return':>8} {'CAGR':>7} {'MaxDD':>7} "
           f"{'PF':>6} {'WR':>7} {'Trades':>7} {'AvgR':>6}")
    print(hdr)
    print("  " + "-" * 86)

    for mid, res in sorted(results.items()):
        m = res["metrics"]
        label = mid.replace("_", " ")
        print(f"  {label:<22} "
              f"${m.get('final_equity',0):>11,.0f} "
              f"{m.get('total_return_pct',0):>+7.1f}% "
              f"{m.get('cagr_pct',0):>6.1f}% "
              f"{m.get('max_drawdown_pct',0):>6.1f}% "
              f"{m.get('profit_factor',0):>6.3f} "
              f"{m.get('win_rate_pct',0):>6.1f}% "
              f"{m.get('total_trades_full',0):>6}  "
              f"{m.get('avg_r_per_trade',0):>5.2f}R")

def print_yearly_table(results: dict):
    """Print year-by-year returns for all models."""
    # Collect all years
    all_years = set()
    for res in results.values():
        all_years.update(res["metrics"].get("yearly_returns", {}).keys())
    all_years = sorted(all_years)

    print_separator("-")
    print(f"  {'YEARLY RETURNS (PnL $)':<20}", end="")
    for y in all_years:
        print(f"  {y:>10}", end="")
    print()
    print("  " + "-" * (20 + 12 * len(all_years)))

    for mid, res in sorted(results.items()):
        yr = res["metrics"].get("yearly_returns", {})
        label = mid.replace("_", " ")
        print(f"  {label:<20}", end="")
        for y in all_years:
            pnl = yr.get(y, {}).get("pnl", 0)
            print(f"  {pnl:>+10,.0f}", end="")
        print()


# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="NexusTrader 4-Year Parallel Backtest")
    parser.add_argument("--symbol",     default="BTC/USDT", help="Symbol (default: BTC/USDT)")
    parser.add_argument("--years",      type=int, default=4,  help="Years of history to fetch (default: 4)")
    parser.add_argument("--tf",         default="30m",        help="Primary timeframe (default: 30m)")
    parser.add_argument("--htf",        default="4h",         help="Higher timeframe for HTF gate (default: 4h)")
    parser.add_argument("--btc30m",     default=None,         help="Path to local 30m parquet file")
    parser.add_argument("--btc4h",      default=None,         help="Path to local 4H parquet file")
    parser.add_argument("--workers",    type=int, default=0,  help="Parallel workers (0=auto)")
    parser.add_argument("--no-htf",     action="store_true",  help="Disable 4H HTF gate (for audit)")
    parser.add_argument("--capital",    type=float, default=INITIAL_CAPITAL, help="Starting capital")
    parser.add_argument("--out",        default=None,         help="Output JSON path (auto if not set)")
    args = parser.parse_args()

    cap0 = args.capital
    n_workers = args.workers if args.workers > 0 else min(5, mp.cpu_count())

    print_separator()
    print(f"  NexusTrader v1.2 — Full {args.years}-Year Parallel Backtest")
    print_separator()
    print(f"  Symbol:    {args.symbol}  ({args.tf} primary + {args.htf} HTF gate)")
    print(f"  Capital:   ${cap0:,.0f}")
    print(f"  Workers:   {n_workers} (parallel model runs)")
    print(f"  HTF Gate:  {'DISABLED (audit mode)' if args.no_htf else 'ENABLED'}")
    print()

    # ── Load Data ─────────────────────────────────────────────────────────
    print("[1/5] Loading data...")
    t0 = time.time()

    df_primary = load_or_fetch(args.symbol, args.tf, args.years, args.btc30m)
    df_htf     = load_or_fetch(args.symbol, args.htf, args.years, args.btc4h)

    start_dt = df_primary["timestamp"].iloc[0]
    end_dt   = df_primary["timestamp"].iloc[-1]
    btc_start = float(df_primary["close"].iloc[0])
    btc_end   = float(df_primary["close"].iloc[-1])

    print(f"   Primary ({args.tf}):  {start_dt.date()} → {end_dt.date()}  ({len(df_primary):,} bars)")
    print(f"   HTF ({args.htf}):      {df_htf['timestamp'].iloc[0].date()} → {df_htf['timestamp'].iloc[-1].date()}  ({len(df_htf):,} bars)")
    print(f"   BTC price: ${btc_start:,.0f} → ${btc_end:,.0f}  ({(btc_end/btc_start-1)*100:+.1f}%)")

    # ── Compute Indicators ────────────────────────────────────────────────
    print(f"\n[2/5] Computing indicators...")
    df_primary = compute_indicators(df_primary)
    df_htf     = compute_indicators(df_htf)
    print(f"   OK: {df_primary['adx'].notna().sum()} valid ADX bars on primary TF")

    # ── Classify Regimes ─────────────────────────────────────────────────
    print(f"\n[3/5] Classifying regimes...")
    regime_primary = classify_regime(df_primary, PROD_PARAMS)
    regime_htf     = classify_regime(df_htf,     PROD_PARAMS)

    from collections import Counter
    rc = Counter(regime_primary[PROD_PARAMS["warmup_bars"]:])
    total_rc = sum(rc.values())
    print(f"   Primary regime distribution:")
    for r, c in sorted(rc.items(), key=lambda x: -x[1]):
        print(f"     {r:28s}: {c:5d} bars ({c/total_rc*100:.1f}%)")

    # ── Generate Signals + HTF Gate ───────────────────────────────────────
    print(f"\n[4/5] Generating signals (threshold={PROD_PARAMS['confluence_threshold']})...")
    sl, ss, sc = generate_signals(df_primary, regime_primary, PROD_PARAMS)
    thresh  = PROD_PARAMS["confluence_threshold"]
    warmup  = PROD_PARAMS["warmup_bars"]

    if not args.no_htf:
        # Align 4H regime to primary TF index
        htf_ts_naive = pd.to_datetime(df_htf["timestamp"].values).tz_localize(None)
        htf_series   = pd.Series(regime_htf, index=pd.DatetimeIndex(htf_ts_naive))
        pri_ts_naive = pd.to_datetime(df_primary["timestamp"].values).tz_localize(None)
        htf_aligned  = htf_series.reindex(pd.DatetimeIndex(pri_ts_naive), method="ffill").fillna(R_UNCERTAIN)
        htf_arr      = htf_aligned.values
        htf_pass     = np.array([r in PROD_PARAMS["htf_allowed_regimes"] for r in htf_arr])
    else:
        htf_pass = np.ones(len(df_primary), dtype=bool)

    # Apply threshold + HTF gate + warmup
    el = (sl & (sc >= thresh) & htf_pass)
    es = (ss & (sc >= thresh) & htf_pass)
    if hasattr(el, "values"): el = el.values
    if hasattr(es, "values"): es = es.values
    if hasattr(sc, "values"): sc_arr = sc.values
    else: sc_arr = sc
    el[:warmup] = False
    es[:warmup] = False

    print(f"   Long signals:  {el.sum():,}")
    print(f"   Short signals: {es.sum():,}")
    print(f"   Total:         {el.sum() + es.sum():,}")

    # Pre-convert df to dict for pickling (multiprocessing)
    df_dict = df_primary.to_dict(orient="list")

    # ── Run All Models in Parallel ────────────────────────────────────────
    print(f"\n[5/5] Running {len(CAPITAL_MODELS)} capital models in parallel ({n_workers} workers)...")
    print(f"   Starting capital: ${cap0:,.0f}\n")

    all_args = [
        (mid, mcfg, df_dict, regime_primary, el, es, sc_arr, PROD_PARAMS, cap0)
        for mid, mcfg in CAPITAL_MODELS.items()
    ]

    raw_results = {}
    t_run = time.time()

    if n_workers == 1:
        # Single process (for debugging)
        for arg in all_args:
            r = simulate_model(arg)
            raw_results[r["model_id"]] = r
            print(f"   {r['model_id']:<22}: ${r['final_equity']:>12,.0f}")
    else:
        with ProcessPoolExecutor(max_workers=n_workers) as pool:
            futures = {pool.submit(simulate_model, arg): arg[0] for arg in all_args}
            for future in as_completed(futures):
                mid = futures[future]
                try:
                    r = future.result()
                    raw_results[r["model_id"]] = r
                    trades_fc = [t for t in r["trades"] if t["exit_reason"] != "partial_close"]
                    wr_pct = sum(1 for t in trades_fc if t["pnl"] > 0) / len(trades_fc) * 100 if trades_fc else 0
                    gw_ = sum(t["pnl"] for t in trades_fc if t["pnl"] > 0)
                    gl_ = abs(sum(t["pnl"] for t in trades_fc if t["pnl"] <= 0))
                    pf_ = gw_/gl_ if gl_ > 0 else 999
                    print(f"   {mid:<22}: ${r['final_equity']:>12,.0f}  "
                          f"PF={pf_:.3f}  WR={wr_pct:.1f}%  "
                          f"Trades={len(trades_fc)}")
                except Exception as e:
                    print(f"   {mid}: ERROR — {e}")

    elapsed = time.time() - t_run
    print(f"\n   Parallel run time: {elapsed:.1f}s")

    # ── Compute Metrics ───────────────────────────────────────────────────
    print("\nComputing metrics...")
    final_results = {}
    for mid, r in raw_results.items():
        m = compute_full_metrics(r["trades"], r["eq_curve"], cap0)
        final_results[mid] = {
            "config":   CAPITAL_MODELS[mid],
            "metrics":  m,
            "eq_curve": r["eq_curve"][::10],  # downsample for output
        }

    # ── Print Results ─────────────────────────────────────────────────────
    print("\n")
    print_comparison_table(final_results)
    print()
    print_yearly_table(final_results)
    print()

    # Model A detailed regime breakdown
    print_separator("-")
    print("  MODEL A — REGIME BREAKDOWN")
    print_separator("-")
    rbd = final_results.get("A_Current", {}).get("metrics", {}).get("regime_breakdown", {})
    print(f"  {'Regime':<28} {'Trades':>7} {'WR':>7} {'PnL':>12}")
    print("  " + "-" * 57)
    for r, s in sorted(rbd.items(), key=lambda x: -x[1]["count"]):
        print(f"  {r:<28} {s['count']:>7}  {s['wr_pct']:>6.1f}%  ${s['pnl']:>10,.0f}")

    print()

    # 4-year projection summary
    print_separator()
    print(f"  4-YEAR GROWTH SUMMARY (Starting ${cap0:,.0f})")
    print_separator()
    for mid, res in sorted(final_results.items()):
        m   = res["metrics"]
        cfg = res["config"]
        lbl = cfg["label"]
        print(f"  {lbl}")
        print(f"    Final:  ${m.get('final_equity',0):>12,.0f}   "
              f"Return: {m.get('total_return_pct',0):>+8.2f}%   "
              f"CAGR: {m.get('cagr_pct',0):>6.2f}%/yr   "
              f"Max DD: {m.get('max_drawdown_pct',0):>5.1f}%")
        print()

    # ── Save JSON ─────────────────────────────────────────────────────────
    ts_str  = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = Path(args.out) if args.out else RESULTS_DIR / f"backtest_4yr_{ts_str}.json"

    output = {
        "run_ts":          datetime.now().isoformat(),
        "symbol":          args.symbol,
        "primary_tf":      args.tf,
        "htf":             args.htf,
        "htf_gate_active": not args.no_htf,
        "initial_capital": cap0,
        "data_start":      str(start_dt),
        "data_end":        str(end_dt),
        "years":           round((end_dt - start_dt).days / 365.25, 2),
        "btc_start_price": round(btc_start, 0),
        "btc_end_price":   round(btc_end, 0),
        "btc_return_pct":  round((btc_end / btc_start - 1) * 100, 2),
        "signals_long":    int(el.sum()),
        "signals_short":   int(es.sum()),
        "regime_dist":     {k: int(v) for k, v in rc.items()},
        "models":          final_results,
        "prod_params":     {k: v for k, v in PROD_PARAMS.items() if isinstance(v, (int, float, str))},
    }

    with open(out_path, "w") as f:
        json.dump(output, f, indent=2, default=str)

    print_separator()
    print(f"  Results saved: {out_path}")
    print_separator()
    print(f"\n  Total runtime: {time.time()-t0:.1f}s")


if __name__ == "__main__":
    main()
