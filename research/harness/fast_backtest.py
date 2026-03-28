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


def _apply_hysteresis_vec(labels: np.ndarray, min_bars: int = 3) -> np.ndarray:
    """
    Exact port of core/regime/research_regime_classifier.py _apply_hysteresis().
    Merges runs shorter than min_bars into the preceding regime (two passes).
    """
    if min_bars <= 1:
        return labels.copy()
    out = labels.copy()
    n   = len(out)
    for _ in range(2):
        i = 0
        while i < n:
            j = i + 1
            while j < n and out[j] == out[i]:
                j += 1
            run_len = j - i
            if run_len < min_bars and i > 0:
                out[i:j] = out[i - 1]
            i = j
    return out


def _classify_regimes_vec(df: pd.DataFrame) -> np.ndarray:
    """Vectorized research regime classifier. Returns int8 array.

    Exact port of core/regime/research_regime_classifier.py classify_series():
      - ATR: tr.ewm(span=14, adjust=False)  [NOT Wilder alpha=1/14]
      - ATR baseline: rolling(100, min_periods=10)
      - peak_dd: (roll_peak − close) / roll_peak  [positive fraction ≥0]
      - Hysteresis: _apply_hysteresis_vec (merge short runs, NOT commitment counter)
      - All thresholds/bars: ADX≥22, 1.80/2.80, 240/96/48
    """
    c, h, l = df["close"], df["high"], df["low"]
    n = len(df)
    if n < 5:
        return np.zeros(n, dtype=np.int8)

    adx    = _adx_ewm_std(h, l, c, 14).values
    ema20  = _ema(c, 20).values
    ema50  = _ema(c, 50).values
    ema200 = _ema(c, 200).values

    # ATR: span=14 (alpha≈0.133) — matches classify_series() tr.ewm(span=14).
    # Do NOT use Wilder alpha=1/14 (≈0.071) — that's for SLC's adx_1h check only.
    pc = c.shift(1)
    tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    atr_series = tr.ewm(span=14, adjust=False).mean()
    # ATR baseline: rolling(100, min_periods=10) — matches production min_periods
    atr_base   = atr_series.rolling(100, min_periods=10).mean()
    atr_ratio  = (atr_series / atr_base.replace(0, np.nan)).fillna(1.0).clip(0, 20).values

    cv     = c.values
    ret_5d = c.pct_change(240).values
    ret_2d = c.pct_change(96).values

    # peak_dd: positive fraction (roll_peak − close) / roll_peak
    # Matches: roll_peak = close.rolling(48, min_periods=1).max()
    #          peak_dd   = (roll_peak − close) / roll_peak   (≥ 0)
    roll_peak = c.rolling(48, min_periods=1).max().values
    peak_dd   = np.where(roll_peak > 0, (roll_peak - cv) / roll_peak, 0.0)

    # ── Priority-ordered labeling (matches classify_series()) ────────────
    labels = np.zeros(n, dtype=np.int8)

    valid5 = ~np.isnan(ret_5d)
    valid2 = ~np.isnan(ret_2d)
    ema_bull = ema20 > ema50
    abv_slow = cv > ema200

    # 1. BULL_TREND
    bull = (adx >= 22) & ema_bull & abv_slow & (atr_ratio < 1.80)
    labels[bull] = 1

    # 2. BEAR_TREND (overrides BULL_TREND — but conditions are mutually exclusive)
    bear = (adx >= 22) & (~ema_bull) & (~abv_slow) & (atr_ratio < 1.80)
    labels[bear] = 2

    # 3. BULL_EXPANSION (overrides trend — also mutually exclusive via atr_ratio)
    bull_exp = valid5 & (atr_ratio >= 1.80) & (ret_5d >= 0.06)
    labels[bull_exp] = 3

    # 4. BEAR_EXPANSION
    bear_exp = valid5 & (atr_ratio >= 1.80) & (ret_5d <= -0.06) & (peak_dd < 0.15)
    labels[bear_exp] = 4

    # 5. CRASH_PANIC (highest priority — overrides everything)
    crash = valid2 & ((peak_dd >= 0.15) | (ret_2d <= -0.08)) & (atr_ratio >= 2.80)
    labels[crash] = 5

    return _apply_hysteresis_vec(labels, 3)


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
                # Master timeline: BTC/USDT 30m ONLY — matches research backtest_v9_system.py
                # line 287: `master_ts = list(btc_30m.index)`.
                # Using union-of-all-symbols would add SOL/ETH-exclusive timestamps that
                # never appear in the research run, producing extra SLC signals and a
                # different position-management cadence (confirmed cause of PF discrepancy).
                if sym == "BTC/USDT":
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

        # pending_entries: signal buffered at bar i → filled at bar i+1 open
        # Matches research backtest_v9_system.py pending_entries behaviour:
        #   signal fires at bar close → enter at NEXT bar's open with sl<ep<tp validation
        pending_entries: dict[str, dict] = {}

        # NOTE: slc_last_1h dedup is intentionally removed.
        # The reference backtest_v9_system.py has no per-1h-bar dedup —
        # dedup is handled naturally by `sym in positions` and `sym in pending_entries`.
        # Adding explicit dedup blocked SLC from re-firing after a failed pending
        # fill (which is a valid entry attempt in the reference), producing ~7
        # fewer SLC trades than the reference.

        # Warmup: match research backtest_v9_system.py `warmup_bars = 120`
        # The research skips the ENTIRE bar (fills, SL/TP, signals) for bar_idx < 120.
        # Using 60 in the harness generated trades in bars 60-119 that don't exist
        # in the research run.
        WARMUP_BARS = 120

        for bar_idx, ts_i8 in enumerate(self._ts_master):
            if bar_idx < WARMUP_BARS:
                continue

            # ── Fill pending entries at this bar's OPEN ────────────────────
            # MUST happen BEFORE the SL/TP exit check so that a position
            # opened at this bar's open can also be closed by this bar's
            # high/low (same-bar fill+exit).  Matches backtest_v9_system.py
            # ordering: "Execute pending entries" → "Update open positions".
            for sym in list(pending_entries.keys()):
                if sym in positions:
                    # Another signal already opened this symbol — cancel pending
                    del pending_entries[sym]
                    continue
                pdata = self._precomputed[sym]
                idx30_pe = np.searchsorted(pdata.ts_30m, ts_i8, side="left")
                if idx30_pe >= pdata.n_30m or pdata.ts_30m[idx30_pe] != ts_i8:
                    # No bar for this symbol at this ts — try next bar
                    continue
                pe = pending_entries[sym]
                ep_raw = float(pdata.open_30m[idx30_pe])
                sl_pe  = pe["sl"]
                tp_pe  = pe["tp"]
                direction_pe = pe["direction"]
                if direction_pe == "long":
                    valid_pe = sl_pe < ep_raw < tp_pe
                else:
                    valid_pe = tp_pe < ep_raw < sl_pe
                del pending_entries[sym]
                if valid_pe:
                    positions[sym] = (
                        direction_pe, ep_raw, sl_pe, tp_pe,
                        pe["size"], pe["model"], idx30_pe,
                    )

            # ── Close expired positions (SL/TP check using this bar's H/L) ──
            # Runs AFTER pending fills so same-bar open+close is possible,
            # matching backtest_v9_system.py which does fills then SL/TP.
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

            # ── Portfolio heat check (max-positions guard) ─────────────
            n_open = len(positions)
            if n_open >= MAX_POS:
                continue

            # ── Signal generation ───────────────────────────────────────
            for sym in SYMBOLS:
                if sym in positions:
                    continue  # one position per symbol

                # Heat check — matches production PositionSizer.calculate_pos_frac():
                #   deployed_est = n_open * pos_frac * equity  (ESTIMATED, not actual)
                # Using actual sum(sizes) diverges as capital grows because old positions
                # were sized at smaller capital; the sizer uses n_open × pos_frac × equity
                # which grows with capital, rejecting more signals in later profitable runs.
                # Reference: core/meta_decision/position_sizer.py lines 502-504.
                n_open_pre = len(positions)
                size_usdt  = capital * POS_FRAC
                deployed_est = n_open_pre * POS_FRAC * capital
                if (deployed_est + size_usdt) / max(capital, 1) > MAX_HEAT:
                    break  # heat cap reached; no further entries this bar
                if capital < size_usdt * 0.5:
                    break  # insufficient capital for any more entries

                # Skip symbols that already have a pending entry buffered
                if sym in pending_entries:
                    continue

                pdata = self._precomputed[sym]
                if pdata.n_30m == 0:
                    continue

                # Find 30m bar index for this ts (int64 comparison)
                idx30 = np.searchsorted(pdata.ts_30m, ts_i8, side="left")
                if idx30 >= pdata.n_30m or pdata.ts_30m[idx30] != ts_i8:
                    continue
                # Require at least 70 bars for reliable indicator values —
                # matches research: `if len(df_window) < 70: continue` (line 439).
                if idx30 < 70:
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
                                    sl_p  = close - sl_mult * atr
                                    tp_p  = close + tp_mult * atr
                                    if sl_p < close < tp_p:
                                        # Buffer entry — fill at next bar's open
                                        # (research enters at o_v[i+1] with sl<ep<tp check)
                                        pending_entries[sym] = {
                                            "direction": "long",
                                            "sl": sl_p, "tp": tp_p,
                                            "model": "pbl", "size": size_usdt,
                                        }
                                        continue  # one signal per symbol per bar

                # ── SLC signal (1h bar) ──────────────────────────────────
                # No per-1h-bar dedup here — matches reference backtest_v9_system.py
                # which relies solely on `sym in positions` / `sym in pending_entries`.
                if mode in ("slc_only", "combined") and sym not in positions:
                    if pdata.ts_1h is not None and pdata.n_1h > 0:
                        # side="right": matches research searchsorted(ts, side="right") - 1
                        # This evaluates the 1h bar that just opened at ts_i8 (look-ahead
                        # on 1h close), which is the exact behaviour that produced PF=1.5455
                        # in the reference backtest_v9_system.py.
                        idx1h = int(np.searchsorted(pdata.ts_1h, ts_i8, side="right")) - 1
                        if idx1h >= slc_swing + 2:
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
                                                # Buffer entry — fill at next 30m bar open
                                                pending_entries[sym] = {
                                                    "direction": "short",
                                                    "sl": sl_p, "tp": tp_p,
                                                    "model": "slc", "size": size_usdt,
                                                }

        # ── Force-close at end ──────────────────────────────────────────
        # pending_entries that never filled (no bar after signal) are discarded
        pending_entries.clear()

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

        # PF: 0.0 for zero-trade configs (not 999) so they sink to the bottom
        # of the leaderboard rather than dominating it.
        if n == 0:
            pf = 0.0
        elif gl == 0:
            pf = 999.0   # all-winning trades (valid edge, rare)
        else:
            pf = gp / gl
        wr  = len(wins) / n if n > 0 else 0.0

        # Compute annualisation span from the engine's date boundaries rather than
        # from ts_master integers.  Parquet files may store datetime64[ms] so
        # asi8 returns milliseconds, not nanoseconds — arithmetic on raw int64s
        # would give a span ~1000x too small, inflating CAGR astronomically.
        if self.date_start and self.date_end:
            span_y = (self.date_end - self.date_start).total_seconds() / (365.25 * 86400)
        else:
            ts_arr = self._ts_master
            if len(ts_arr) > 1:
                # Detect unit from magnitude: ms values are ~1e12, ns values ~1e18
                raw_span = int(ts_arr[-1]) - int(ts_arr[0])
                unit_s   = 1e-3 if ts_arr[0] < 1e14 else 1e-9
                span_y   = raw_span * unit_s / (365.25 * 86400)
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
