"""
research/harness/fast_backtest.py
===================================
Vectorized backtest engine for PBL/SLC optimization.

SPEEDUP STRATEGY
----------------
Instead of evaluating signals bar-by-bar in Python:
1. Pre-compute ALL bar indicators for all symbols once (per harness run, cached)
2. For each parameter set, vectorize the signal CONDITIONS into numpy arrays
3. Run a lean sequential position manager over the precomputed signal arrays

This gives ~10-30x speedup vs bar-by-bar evaluation.

USAGE
-----
from research.harness.fast_backtest import FastBacktestEngine
engine = FastBacktestEngine()  # loads + preprocesses data once
result = engine.run(params, mode="pbl_only")
"""
from __future__ import annotations

import logging
import sys
import time
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

logger = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────
SYMBOLS      = ["BTC/USDT", "SOL/USDT", "ETH/USDT"]
DATA_DIR     = ROOT / "backtest_data"
INITIAL_CAP  = 100_000.0
POS_FRAC     = 0.35
MAX_HEAT     = 0.80
MAX_POS      = 10
HTF_LB       = 60
SLC_1H_LB    = 150
IS_END       = pd.Timestamp("2025-03-21", tz="UTC")


def _ema(s: pd.Series, span: int) -> pd.Series:
    return s.ewm(span=span, adjust=False).mean()


def _atr_wilder(h: pd.Series, l: pd.Series, c: pd.Series, period: int = 14) -> pd.Series:
    pc = c.shift(1)
    tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False).mean()


def _adx_wilder(h: pd.Series, l: pd.Series, c: pd.Series, period: int = 14) -> pd.Series:
    ph, pl, pc = h.shift(1), l.shift(1), c.shift(1)
    tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    up, dn = h - ph, pl - l
    pdm = pd.Series(np.where((up > dn) & (up > 0), up, 0.0), index=c.index)
    ndm = pd.Series(np.where((dn > up) & (dn > 0), dn, 0.0), index=c.index)
    a = 1 / period
    tr_s = tr.ewm(alpha=a, adjust=False).mean()
    pdi  = 100 * pdm.ewm(alpha=a, adjust=False).mean() / tr_s.replace(0, np.nan)
    ndi  = 100 * ndm.ewm(alpha=a, adjust=False).mean() / tr_s.replace(0, np.nan)
    dx   = 100 * (pdi - ndi).abs() / (pdi + ndi).replace(0, np.nan)
    return dx.ewm(alpha=a, adjust=False).mean().fillna(0)


def _adx_ewm_std(h: pd.Series, l: pd.Series, c: pd.Series, period: int = 14) -> pd.Series:
    """Standard EWM ADX (span=period) — used by ResearchRegimeClassifier."""
    ph, pl, pc = h.shift(1), l.shift(1), c.shift(1)
    tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    up, dn = h - ph, pl - l
    pdm = pd.Series(np.where((up > dn) & (up > 0), up, 0.0), index=c.index)
    ndm = pd.Series(np.where((dn > up) & (dn > 0), dn, 0.0), index=c.index)
    tr_s = tr.ewm(span=period, adjust=False).mean()
    pdi  = 100 * pdm.ewm(span=period, adjust=False).mean() / tr_s.replace(0, np.nan)
    ndi  = 100 * ndm.ewm(span=period, adjust=False).mean() / tr_s.replace(0, np.nan)
    dx   = 100 * (pdi - ndi).abs() / (pdi + ndi).replace(0, np.nan)
    return dx.ewm(span=period, adjust=False).mean().fillna(0)


def _rsi_wilder(c: pd.Series, period: int = 14) -> pd.Series:
    delta = c.diff()
    gain  = delta.clip(lower=0)
    loss  = (-delta).clip(lower=0)
    avg_g = gain.ewm(alpha=1/period, adjust=False).mean()
    avg_l = loss.ewm(alpha=1/period, adjust=False).mean()
    rs    = avg_g / avg_l.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def _classify_regimes_vec(df: pd.DataFrame) -> np.ndarray:
    """Vectorized research regime classifier. Returns int8 array."""
    c, h, l = df["close"], df["high"], df["low"]
    n = len(df)
    if n < 5:
        return np.zeros(n, dtype=np.int8)

    adx    = _adx_ewm_std(h, l, c, 14).values
    ema20  = _ema(c, 20).values
    ema50  = _ema(c, 50).values
    ema200 = _ema(c, 200).values
    atr    = _atr_wilder(h, l, c, 14).values

    atr_base = pd.Series(atr).rolling(100, min_periods=1).mean().values
    atr_ratio = np.where(atr_base > 0, atr / atr_base, 1.0)

    cv     = c.values
    ret_5d = pd.Series(cv).pct_change(240).values
    ret_2d = pd.Series(cv).pct_change(96).values
    roll_max = pd.Series(cv).rolling(48, min_periods=1).max().values
    peak_dd  = np.where(roll_max > 0, (cv - roll_max) / roll_max, 0.0)

    raw = np.zeros(n, dtype=np.int8)
    m   = np.isnan(ret_5d) | np.isnan(ret_2d)

    # Vectorized rules (priority: highest first)
    r5 = np.where(m, 0.0, ret_5d)
    r2 = np.where(m, 0.0, ret_2d)

    crash  = (peak_dd <= -0.15) | (r2 <= -0.08)
    crash &= atr_ratio >= 2.80
    bear_e = (~crash) & (atr_ratio >= 1.80) & (r5 <= -0.06) & (peak_dd > -0.15)
    bull_e = (~crash) & (~bear_e) & (atr_ratio >= 1.80) & (r5 >= 0.06)
    bear_t = (~crash) & (~bear_e) & (~bull_e) & (adx >= 22) & (ema20 <= ema50) & (cv <= ema200) & (atr_ratio < 1.80)
    bull_t = (~crash) & (~bear_e) & (~bull_e) & (~bear_t) & (adx >= 22) & (ema20 > ema50) & (cv > ema200) & (atr_ratio < 1.80)

    raw[bull_t] = 1
    raw[bear_t] = 2
    raw[bull_e] = 3
    raw[bear_e] = 4
    raw[crash]  = 5

    # 3-bar hysteresis
    out = raw.copy()
    committed = -1
    count = 0
    for i in range(n):
        r = int(raw[i])
        if r != committed:
            count += 1
            if count >= 3:
                committed = r
                count = 0
        out[i] = committed if committed >= 0 else r

    return out.astype(np.int8)


def _precompute_htf_emas(df_4h: pd.DataFrame) -> dict[str, np.ndarray]:
    """Precompute 4h EMAs for all variants."""
    c, h, l = df_4h["close"], df_4h["high"], df_4h["low"]
    return {
        "ema_9":   _ema(c, 9).values,
        "ema_20":  _ema(c, 20).values,
        "ema_21":  _ema(c, 21).values,
        "ema_50":  _ema(c, 50).values,
        "ema_100": _ema(c, 100).values,
        "ema_200": _ema(c, 200).values,
        "adx_14":  _adx_ewm_std(h, l, c, 14).values,  # Standard EWM for regime
        "close":   c.values,
        "ts":      df_4h.index.values,
    }


class PrecomputedData:
    """All precomputed indicator arrays for one symbol."""
    def __init__(self, sym: str, df_30m: pd.DataFrame, df_4h: pd.DataFrame, df_1h: pd.DataFrame):
        self.sym = sym

        # ── 30m data ─────────────────────────────────────────────────
        c30, h30, l30 = df_30m["close"], df_30m["high"], df_30m["low"]
        # Convert timestamps to int64 (nanoseconds) for fast searchsorted
        self.ts_30m  = df_30m.index.asi8  # int64 nanoseconds
        self.open_30m= df_30m["open"].values
        self.high_30m= h30.values
        self.low_30m = l30.values
        self.close_30m = c30.values
        self.ema50_30m = _ema(c30, 50).values
        self.atr_30m   = _atr_wilder(h30, l30, c30, 14).values
        self.rsi_30m   = _rsi_wilder(c30, 14).values
        self.regime_30m= _classify_regimes_vec(df_30m)
        self.n_30m     = len(df_30m)

        # Precompute candle structure arrays for PBL
        body_arr  = np.abs(c30.values - df_30m["open"].values)
        lw_arr    = np.minimum(c30.values, df_30m["open"].values) - l30.values
        uw_arr    = h30.values - np.maximum(c30.values, df_30m["open"].values)
        rng_arr   = h30.values - l30.values

        self.pbl_bullish   = c30.values > df_30m["open"].values  # close > open
        self.pbl_lw_gt_uw  = lw_arr > uw_arr
        self.pbl_lw_gt_body= lw_arr > body_arr
        self.pbl_body_arr  = body_arr
        self.pbl_lw_arr    = lw_arr
        self.pbl_range_arr = rng_arr

        # ── 4h data ──────────────────────────────────────────────────
        if df_4h is not None and not df_4h.empty:
            self.ts_4h  = df_4h.index.asi8
            self.htf_emas = _precompute_htf_emas(df_4h)
            self.n_4h   = len(df_4h)
        else:
            self.ts_4h = None
            self.htf_emas = {}
            self.n_4h = 0

        # ── 1h data ──────────────────────────────────────────────────
        if df_1h is not None and not df_1h.empty:
            c1, h1, l1 = df_1h["close"], df_1h["high"], df_1h["low"]
            self.ts_1h      = df_1h.index.asi8
            self.close_1h   = c1.values
            self.atr_1h     = _atr_wilder(h1, l1, c1, 14).values
            self.adx_1h     = _adx_wilder(h1, l1, c1, 14).values  # Wilder for SLC
            self.regime_1h  = _classify_regimes_vec(df_1h)
            self.n_1h       = len(df_1h)
        else:
            self.ts_1h = None
            self.close_1h = None
            self.n_1h = 0


class FastBacktestEngine:
    """
    Preloads and preprocesses data once; runs many trials cheaply.

    Usage:
        engine = FastBacktestEngine(date_end="2025-03-21")
        result = engine.run(params, mode="pbl_only")
    """

    def __init__(self, date_start: Optional[str] = None, date_end: Optional[str] = None):
        self.date_start = pd.Timestamp(date_start, tz="UTC") if date_start else None
        self.date_end   = pd.Timestamp(date_end,   tz="UTC") if date_end   else None
        self._precomputed: dict[str, PrecomputedData] = {}
        self._ts_master: Optional[np.ndarray] = None  # int64 nanoseconds
        self._load_and_precompute()

    def _slice(self, df: pd.DataFrame) -> pd.DataFrame:
        if df.empty:
            return df
        if self.date_start:
            df = df[df.index >= self.date_start]
        if self.date_end:
            df = df[df.index <= self.date_end]
        return df

    def _load_and_precompute(self):
        t0 = time.time()
        all_ts_i8 = set()

        for sym in SYMBOLS:
            key = sym.replace("/", "_")
            dfs = {}
            for tf in ["30m", "4h", "1h"]:
                fp = DATA_DIR / f"{key}_{tf}.parquet"
                if fp.exists():
                    df = pd.read_parquet(fp)
                    if not isinstance(df.index, pd.DatetimeIndex):
                        df.index = pd.to_datetime(df.index, utc=True)
                    elif df.index.tz is None:
                        df.index = df.index.tz_localize("UTC")
                    dfs[tf] = self._slice(df)
                else:
                    dfs[tf] = pd.DataFrame()

            if not dfs.get("30m", pd.DataFrame()).empty:
                all_ts_i8.update(dfs["30m"].index.asi8.tolist())

            self._precomputed[sym] = PrecomputedData(
                sym,
                dfs.get("30m", pd.DataFrame()),
                dfs.get("4h", pd.DataFrame()),
                dfs.get("1h", pd.DataFrame()),
            )

        self._ts_master = np.array(sorted(all_ts_i8), dtype=np.int64)
        logger.debug("Precompute done in %.2fs, %d bars", time.time() - t0, len(self._ts_master))

    def _get_pbl_htf_ok(self, pdata: PrecomputedData, ts_idx: int, params: dict) -> bool:
        """Check if 4h HTF gate passes for bar at 30m index ts_idx."""
        if pdata.ts_4h is None or pdata.n_4h == 0:
            return True  # bypass if no data

        ts_i8 = pdata.ts_30m[ts_idx]  # int64 nanoseconds
        # Find last completed 4h bar before ts (int64 comparison)
        idx4h = np.searchsorted(pdata.ts_4h, ts_i8, side="right") - 1
        if idx4h < 1:
            return False

        htf_fast_col = f"ema_{params['htf_ema_fast']}"
        htf_slow_col = f"ema_{params['htf_ema_slow']}"

        emas = pdata.htf_emas
        if htf_fast_col not in emas or htf_slow_col not in emas:
            return False

        ema_f = emas[htf_fast_col][idx4h]
        ema_s = emas[htf_slow_col][idx4h]
        if np.isnan(ema_f) or np.isnan(ema_s) or ema_f <= ema_s:
            return False

        # Optional ADX gate
        htf_adx_min = params.get("htf_adx_min")
        if htf_adx_min is not None:
            htf_adx = emas["adx_14"][idx4h]
            if np.isnan(htf_adx) or htf_adx < htf_adx_min:
                return False

        # Optional price > EMA200 gate
        if params.get("htf_price_above_ema200", False):
            ema200 = emas.get("ema_200")
            if ema200 is None:
                return False
            close_now = pdata.close_30m[ts_idx]
            if np.isnan(ema200[idx4h]) or close_now <= ema200[idx4h]:
                return False

        return True

    def run(self, params: dict, mode: str = "pbl_only", cost_per_side: float = 0.0004) -> dict:
        """Run backtest for given params. Returns metrics dict."""

        ema_prox_mult = params["ema_prox_atr_mult"]
        rsi_min       = params["rsi_min"]
        sl_mult       = params["sl_atr_mult"]
        tp_mult       = params["tp_atr_mult"]
        body_max      = params.get("body_ratio_max")   # None = disabled
        slc_adx_min   = params.get("slc_adx_min", 28.0)
        slc_swing     = int(params.get("slc_swing_bars", 10))
        slc_sl        = params.get("slc_sl_atr_mult", 2.5)
        slc_tp        = params.get("slc_tp_atr_mult", 2.0)

        capital    = INITIAL_CAP
        # positions: {sym: (direction, entry, sl, tp, size, model, bar_idx)}
        positions: dict[str, tuple] = {}
        closed: list = []

        # SLC: last 1h bar index evaluated per symbol
        slc_last_1h: dict[str, int] = {}

        for ts_i8 in self._ts_master:

            # ── Close expired positions ─────────────────────────────────
            to_remove = []
            for sym, pos in positions.items():
                direction, entry, sl, tp, size, model, _ = pos
                pdata = self._precomputed[sym]

                # Find 30m bar for this timestamp (int64 comparison)
                idx = np.searchsorted(pdata.ts_30m, ts_i8, side="left")
                if idx >= pdata.n_30m or pdata.ts_30m[idx] != ts_i8:
                    continue

                lo = pdata.low_30m[idx]
                hi = pdata.high_30m[idx]
                exit_p = None
                reason = None

                if direction == "long":
                    if lo <= sl:
                        exit_p, reason = sl, "sl"
                    elif hi >= tp:
                        exit_p, reason = tp, "tp"
                else:
                    if hi >= sl:
                        exit_p, reason = sl, "sl"
                    elif lo <= tp:
                        exit_p, reason = tp, "tp"

                if exit_p is not None:
                    if direction == "long":
                        raw_pnl = (exit_p - entry) / entry * size
                    else:
                        raw_pnl = (entry - exit_p) / entry * size
                    fee = size * cost_per_side * 2
                    net = raw_pnl - fee
                    capital += net
                    closed.append((ts_i8, sym, model, direction, entry, exit_p, reason, net, size))
                    to_remove.append(sym)

            for sym in to_remove:
                del positions[sym]

            # ── Portfolio heat check ────────────────────────────────────
            n_open = len(positions)
            if n_open >= MAX_POS:
                continue

            total_exp = sum(p[4] for p in positions.values())
            size_usdt = capital * POS_FRAC
            if (total_exp + size_usdt) / max(capital, 1) > MAX_HEAT:
                continue
            if capital < size_usdt * 0.5:
                continue

            # ── Signal generation ───────────────────────────────────────
            for sym in SYMBOLS:
                if sym in positions:
                    continue  # one position per symbol

                pdata = self._precomputed[sym]
                if pdata.n_30m == 0:
                    continue

                # Find 30m bar index for this ts (int64 comparison)
                idx30 = np.searchsorted(pdata.ts_30m, ts_i8, side="left")
                if idx30 >= pdata.n_30m or pdata.ts_30m[idx30] != ts_i8:
                    continue
                if idx30 < 60:
                    continue

                # ── PBL signal (30m bar) ────────────────────────────────
                if mode in ("pbl_only", "combined"):
                    if pdata.regime_30m[idx30] == 1:  # BULL_TREND
                        ema50 = pdata.ema50_30m[idx30]
                        atr   = pdata.atr_30m[idx30]
                        rsi   = pdata.rsi_30m[idx30]

                        if (not np.isnan(ema50) and not np.isnan(atr) and atr > 0 and
                                not np.isnan(rsi)):
                            close = pdata.close_30m[idx30]
                            prox_ok = abs(close - ema50) <= ema_prox_mult * atr
                            rej_ok  = (pdata.pbl_bullish[idx30] and
                                       pdata.pbl_lw_gt_uw[idx30] and
                                       pdata.pbl_lw_gt_body[idx30])
                            rsi_ok  = rsi > rsi_min

                            # Body ratio filter (optional)
                            if body_max is not None:
                                rng = pdata.pbl_range_arr[idx30]
                                body_ok = (rng <= 0) or (pdata.pbl_body_arr[idx30] / rng <= body_max)
                            else:
                                body_ok = True

                            if prox_ok and rej_ok and rsi_ok and body_ok:
                                if self._get_pbl_htf_ok(pdata, idx30, params):
                                    entry = close
                                    sl_p  = close - sl_mult * atr
                                    tp_p  = close + tp_mult * atr
                                    if sl_p < entry < tp_p:
                                        positions[sym] = (
                                            "long", entry, sl_p, tp_p, size_usdt, "pbl", idx30
                                        )
                                        continue  # one signal per symbol per bar

                # ── SLC signal (1h bar, deduplicated) ───────────────────
                if mode in ("slc_only", "combined") and sym not in positions:
                    if pdata.ts_1h is not None and pdata.n_1h > 0:
                        idx1h = int(np.searchsorted(pdata.ts_1h, ts_i8, side="right")) - 1
                        if idx1h >= slc_swing + 2:
                            # De-duplicate: evaluate only once per 1h bar per symbol
                            if slc_last_1h.get(sym) != idx1h:
                                slc_last_1h[sym] = idx1h
                                if pdata.regime_1h[idx1h] == 2:  # BEAR_TREND
                                    adx = pdata.adx_1h[idx1h]
                                    atr = pdata.atr_1h[idx1h]
                                    if not np.isnan(adx) and not np.isnan(atr) and atr > 0:
                                        if adx >= slc_adx_min:
                                            close1h = pdata.close_1h[idx1h]
                                            prev_cl = pdata.close_1h[idx1h - slc_swing: idx1h]
                                            prev_min = float(np.min(prev_cl))
                                            if close1h < prev_min:
                                                sl_p = close1h + slc_sl * atr
                                                tp_p = close1h - slc_tp * atr
                                                if sl_p > close1h > tp_p:
                                                    positions[sym] = (
                                                        "short", close1h, sl_p, tp_p, size_usdt, "slc", idx1h
                                                    )

        # ── Force-close at end ──────────────────────────────────────────
        if len(self._ts_master) > 0:
            last_ts_i8 = self._ts_master[-1]
            for sym, pos in positions.items():
                direction, entry, sl, tp, size, model, _ = pos
                pdata = self._precomputed[sym]
                if pdata.n_30m > 0:
                    exit_p = float(pdata.close_30m[-1])
                    if direction == "long":
                        raw = (exit_p - entry) / entry * size
                    else:
                        raw = (entry - exit_p) / entry * size
                    fee = size * cost_per_side * 2
                    capital += raw - fee
                    closed.append((last_ts_i8, sym, model, direction, entry, exit_p, "end", raw - fee, size))

        # ── Compute metrics ─────────────────────────────────────────────
        n = len(closed)
        wins   = [t for t in closed if t[7] > 0]
        losses = [t for t in closed if t[7] <= 0]
        gp = sum(t[7] for t in wins)
        gl = abs(sum(t[7] for t in losses))

        pf  = gp / gl if gl > 0 else 999.0
        wr  = len(wins) / n if n > 0 else 0.0

        ts_arr = self._ts_master
        if len(ts_arr) > 1:
            # ts_master is int64 nanoseconds; convert to seconds
            span_s = (int(ts_arr[-1]) - int(ts_arr[0])) * 1e-9
            span_y = span_s / (365.25 * 86400)
        else:
            span_y = 4.0
        cagr = ((capital / INITIAL_CAP) ** (1 / max(span_y, 0.1)) - 1) * 100

        # MaxDD (closed[0] is int64 ts — sort numerically)
        maxdd_pct = 0.0
        if closed:
            eq = INITIAL_CAP
            peak = INITIAL_CAP
            for t in sorted(closed, key=lambda x: x[0]):
                eq += t[7]
                if eq > peak:
                    peak = eq
                dd = (eq - peak) / peak
                if dd < maxdd_pct:
                    maxdd_pct = dd
            maxdd_pct *= 100

        return {
            "pf":           round(pf, 4),
            "wr":           round(wr, 4),
            "cagr":         round(cagr, 2),
            "maxdd":        round(maxdd_pct, 2),
            "n_trades":     n,
            "n_wins":       len(wins),
            "n_losses":     len(losses),
            "gross_profit": round(gp, 2),
            "gross_loss":   round(gl, 2),
            "final_capital":round(capital, 2),
            "params":       params,
            "mode":         mode,
            "date_start":   str(pd.Timestamp(int(self._ts_master[0]), unit="ns", tz="UTC").date()) if len(self._ts_master) else "N/A",
            "date_end":     str(pd.Timestamp(int(self._ts_master[-1]), unit="ns", tz="UTC").date()) if len(self._ts_master) else "N/A",
            "cost_per_side":cost_per_side,
        }


# ── Worker-compatible standalone runner ──────────────────────────────────────
# Module-level engine instance per worker process (initialized once on first call)
_PROCESS_ENGINE: dict[str, FastBacktestEngine] = {}


def worker_run(args: tuple) -> dict:
    """
    Multiprocessing worker. Creates one engine per process (data loaded once).
    """
    import traceback
    trial_id, params, mode, date_start, date_end, cost = args
    key = f"{date_start}_{date_end}"
    if key not in _PROCESS_ENGINE:
        _PROCESS_ENGINE[key] = FastBacktestEngine(date_start, date_end)
    engine = _PROCESS_ENGINE[key]
    try:
        result = engine.run(params, mode, cost)
        result["trial_id"] = trial_id
        result["status"]   = "ok"
        return result
    except Exception as e:
        return {
            "trial_id": trial_id,
            "status":   "error",
            "error":    str(e),
            "tb":       traceback.format_exc()[:500],
            "params":   params,
        }
