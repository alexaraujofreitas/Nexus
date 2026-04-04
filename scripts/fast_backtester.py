"""
FastBacktester — Vectorized optimization backtester for NexusTrader
=======================================================================
Replaces the O(n²) bar-by-bar IDSSBacktester with vectorized numpy/pandas
operations for parameter sweeps during optimization campaigns.

Speed vs fidelity tradeoff:
  - ~100x faster than IDSSBacktester for 5m 4yr data
  - Replicates TrendModel + MomentumBreakoutModel signal logic exactly
  - Same ATR-based SL/TP as production (REGIME_ATR_MULTIPLIERS)
  - Same fee/slippage model
  - Same single-position constraint

Key differences vs IDSSBacktester:
  - Regime classification uses pre-computed indicator columns (vectorized)
    rather than bar-by-bar expanding-window HMM fitting
  - Signal logic replicated from source; no LLM approximation
  - No RiskGate EV gate (for speed), threshold is the only filter
  - Multi-TF confirmation implemented via higher-TF regime pre-alignment

Usage:
  from scripts.fast_backtester import FastBacktester, run_parallel
  fb = FastBacktester()
  result = fb.run(df_indicators, '4h', params={'confluence_threshold': 0.55})

Parallel sweep:
  results = run_parallel(df_i, '4h', param_grid, max_workers=7)
"""

from __future__ import annotations

import os
import math
import logging
import statistics
import multiprocessing as mp
from concurrent.futures import ProcessPoolExecutor, as_completed
from typing import Any

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# ─── Regime constants (must match regime_classifier.py) ──────────────────────
R_BULL      = "bull_trend"
R_BEAR      = "bear_trend"
R_RANGING   = "ranging"
R_VOL_EXP   = "volatility_expansion"
R_VOL_COMP  = "volatility_compression"
R_UNCERTAIN = "uncertain"

# ATR multipliers for SL (TP = SL mult + 1.0 for trend; measured move for MB)
REGIME_ATR_SL: dict[str, float] = {
    R_BULL: 1.875, R_BEAR: 1.875, R_RANGING: 3.125,
    R_VOL_EXP: 3.75, R_VOL_COMP: 2.25, R_UNCERTAIN: 2.5,
}

DEFAULT_PARAMS: dict[str, Any] = {
    # Confluence
    "confluence_threshold": 0.45,
    # TrendModel params
    "trend_adx_min":        25.0,
    "trend_rsi_long_min":   45.0,
    "trend_rsi_long_max":   70.0,
    "trend_rsi_short_min":  30.0,
    "trend_rsi_short_max":  55.0,
    "trend_strength_base":  0.15,
    "trend_adx_bonus_max":  0.40,
    # MomentumBreakout params
    "mb_lookback":          20,
    "mb_vol_mult_min":      1.5,
    "mb_rsi_bullish":       55.0,
    "mb_rsi_bearish":       45.0,
    "mb_strength_base":     0.35,
    # Regime detection
    "bb_expansion_ratio":   2.5,   # bb_width / rolling_mean > this → vol_expansion
    "bb_compression_ratio": 0.5,   # bb_width / rolling_mean < this → vol_compression
    "adx_trend_thresh":     25.0,
    "adx_ranging_thresh":   20.0,
    # ATR SL multiplier override (None = use regime table)
    "atr_sl_mult_override": None,
    # Execution
    "fee_pct":              0.04,
    "slippage_pct":         0.05,
    "initial_capital":      10_000.0,
    "warmup_bars":          100,
    # Multi-TF filter: if set, string key pointing to higher-TF regime Series
    # This is passed in externally, not a scalar param
    "htf_regime_filter":    None,   # pd.Series aligned to df_i index, or None
    "htf_allowed_regimes":  None,   # list of regimes that allow entry, or None
}


class FastBacktester:
    """
    Vectorized backtester optimized for parameter sweep campaigns.

    All heavy computation (regime classification, signal generation) is done
    via pandas/numpy operations on pre-computed indicator columns.
    The bar-by-bar simulation loop only handles position state tracking,
    which requires sequential processing by nature.
    """

    def run(
        self,
        df_i:       pd.DataFrame,
        timeframe:  str,
        params:     dict | None = None,
    ) -> dict:
        """
        Run a full backtest on indicator-augmented DataFrame.

        Parameters
        ----------
        df_i : pd.DataFrame
            Output of calculate_all(). Must contain: open, high, low, close,
            volume, ema_9, ema_21, adx, rsi, atr, bb_width.
        timeframe : str
            Timeframe string e.g. '1h', '4h'. Used for regime ATR table.
        params : dict
            Parameter overrides. See DEFAULT_PARAMS for available keys.

        Returns
        -------
        dict with keys: trades, metrics, n_bars, timeframe
        """
        p = {**DEFAULT_PARAMS, **(params or {})}
        warmup = p["warmup_bars"]

        # ── Step 1: Vectorized regime classification ──────────────────────
        regime_arr = self._classify_regime(df_i, p)   # np.array of strings

        # ── Step 2: Vectorized signal generation ─────────────────────────
        sig_long, sig_short, sig_score = self._generate_signals(df_i, regime_arr, p)

        # ── Step 3: Apply confluence threshold ───────────────────────────
        threshold = p["confluence_threshold"]
        entry_long  = (sig_long  & (sig_score >= threshold)).values
        entry_short = (sig_short & (sig_score >= threshold)).values

        # ── Step 4: Apply higher-TF filter if provided ───────────────────
        htf_filter = p.get("htf_regime_filter")
        if htf_filter is not None:
            htf_allowed = set(p.get("htf_allowed_regimes") or [R_BULL, R_BEAR, R_VOL_EXP])
            htf_arr = htf_filter.reindex(df_i.index, method='ffill').fillna(R_UNCERTAIN).values
            htf_pass = np.isin(htf_arr, list(htf_allowed))
            entry_long  &= htf_pass
            entry_short &= htf_pass

        # Enforce warmup
        entry_long[:warmup]  = False
        entry_short[:warmup] = False

        # ── Step 5: Sequential trade simulation ───────────────────────────
        # (must be sequential — each bar depends on current equity/position)
        trades = self._simulate(
            df_i, regime_arr, entry_long, entry_short, sig_score.values, p)

        metrics = self._calc_metrics(trades, p["initial_capital"])
        return {
            "trades":    trades,
            "metrics":   metrics,
            "n_bars":    len(df_i),
            "timeframe": timeframe,
        }

    # ─────────────────────────────────────────────────────────────────────────
    # Vectorized regime classification
    # Replicates RegimeClassifier._classify_rule_based() logic using pre-computed
    # indicator columns — no bar-by-bar expanding window required.
    # ─────────────────────────────────────────────────────────────────────────
    def _classify_regime(self, df: pd.DataFrame, p: dict) -> np.ndarray:
        n = len(df)
        regime = np.full(n, R_UNCERTAIN, dtype=object)

        # Guard: require minimum indicator columns
        required = {"adx", "ema_9", "ema_21", "bb_width", "rsi"}
        if not required.issubset(df.columns):
            missing = required - set(df.columns)
            raise ValueError(f"FastBacktester: missing indicator columns: {missing}")

        adx       = df["adx"].values.astype(float)
        ema9      = df["ema_9"].values.astype(float)
        ema21     = df["ema_21"].values.astype(float)
        bb_width  = df["bb_width"].values.astype(float)

        # Rolling mean of bb_width over 20 bars for ratio
        bb_roll   = pd.Series(bb_width).rolling(20, min_periods=5).mean().values
        with np.errstate(divide='ignore', invalid='ignore'):
            bb_ratio = np.where(bb_roll > 0, bb_width / bb_roll, 1.0)

        ema_slope = np.sign(ema9 - ema21)   # +1 = bullish, -1 = bearish

        adx_trend   = p["adx_trend_thresh"]
        adx_ranging = p["adx_ranging_thresh"]
        bb_exp      = p["bb_expansion_ratio"]
        bb_comp     = p["bb_compression_ratio"]

        # Apply rules in priority order (higher priority overwrites)
        # 1. Ranging (ADX low)
        mask_ranging = (adx < adx_ranging) & ~np.isnan(adx)
        regime[mask_ranging] = R_RANGING

        # 2. ADX dead zone (20–25): also treat as ranging
        mask_dead = (adx >= adx_ranging) & (adx < adx_trend) & ~np.isnan(adx)
        regime[mask_dead] = R_RANGING

        # 3. Trend regimes (ADX >= trend threshold)
        mask_trend = (adx >= adx_trend) & ~np.isnan(adx)
        regime[mask_trend & (ema_slope > 0)]  = R_BULL
        regime[mask_trend & (ema_slope <= 0)] = R_BEAR

        # 4. Volatility expansion (overrides everything — highest priority)
        mask_vol_exp  = bb_ratio > bb_exp
        mask_vol_comp = bb_ratio < bb_comp
        regime[mask_vol_exp]  = R_VOL_EXP
        regime[mask_vol_comp] = R_VOL_COMP

        # 5. NaN ADX → uncertain
        regime[np.isnan(adx)] = R_UNCERTAIN

        return regime

    # ─────────────────────────────────────────────────────────────────────────
    # Vectorized signal generation
    # Replicates TrendModel and MomentumBreakoutModel evaluate() logic.
    # ─────────────────────────────────────────────────────────────────────────
    def _generate_signals(
        self,
        df: pd.DataFrame,
        regime: np.ndarray,
        p: dict,
    ) -> tuple[pd.Series, pd.Series, pd.Series]:
        """Returns (sig_long, sig_short, score) as boolean/float Series."""
        n   = len(df)
        rsi = df["rsi"].values.astype(float)
        adx = df["adx"].values.astype(float)
        ema9  = df["ema_9"].values.astype(float)
        ema21 = df["ema_21"].values.astype(float)

        sig_long  = np.zeros(n, dtype=bool)
        sig_short = np.zeros(n, dtype=bool)
        score     = np.zeros(n, dtype=float)

        # ── TrendModel ───────────────────────────────────────────────────
        adx_min       = p["trend_adx_min"]
        rsi_l_min     = p["trend_rsi_long_min"]
        rsi_l_max     = p["trend_rsi_long_max"]
        rsi_s_min     = p["trend_rsi_short_min"]
        rsi_s_max     = p["trend_rsi_short_max"]
        str_base      = p["trend_strength_base"]
        adx_bonus_max = p["trend_adx_bonus_max"]

        in_trend = np.isin(regime, [R_BULL, R_BEAR])
        has_adx  = (adx >= adx_min) & ~np.isnan(adx)

        # Long condition: in bull trend, EMA9>EMA21, RSI in momentum zone
        trend_long_cond = (
            in_trend & has_adx &
            (ema9 > ema21) &
            (rsi >= rsi_l_min) & (rsi <= rsi_l_max)
        )
        # Short condition: in bear trend, EMA9<EMA21, RSI in bear zone
        trend_short_cond = (
            in_trend & has_adx &
            (ema9 < ema21) &
            (rsi >= rsi_s_min) & (rsi <= rsi_s_max)
        )

        # Score: base + adx bonus
        adx_bonus_arr = np.minimum(adx_bonus_max,
                                   np.where(has_adx, (adx - adx_min) / adx_min * adx_bonus_max, 0))
        trend_score = str_base + adx_bonus_arr

        sig_long  |= trend_long_cond
        sig_short |= trend_short_cond
        score = np.where(trend_long_cond | trend_short_cond,
                         np.maximum(score, trend_score), score)

        # ── MomentumBreakoutModel ─────────────────────────────────────────
        lookback    = p["mb_lookback"]
        vol_mult    = p["mb_vol_mult_min"]
        rsi_bull    = p["mb_rsi_bullish"]
        rsi_bear    = p["mb_rsi_bearish"]
        mb_str      = p["mb_strength_base"]

        close  = df["close"].values.astype(float)
        volume = df["volume"].values.astype(float)
        in_vol_exp = (regime == R_VOL_EXP)

        # Rolling range high/low and volume average using pandas for vectorization
        close_s  = pd.Series(close)
        vol_s    = pd.Series(volume)
        # Shift by 1 so r_high/r_low is the range of the PRIOR lookback bars,
        # allowing close > r_high to detect breakouts (same logic as live model)
        r_high   = close_s.shift(1).rolling(lookback, min_periods=lookback).max().values
        r_low    = close_s.shift(1).rolling(lookback, min_periods=lookback).min().values
        vol_avg  = vol_s.rolling(lookback, min_periods=lookback).mean().values

        range_size = np.where(
            (r_high is not None) & (r_low is not None),
            np.abs(r_high - r_low), 0)

        vol_pass = (volume > vol_avg * vol_mult) & (vol_avg > 0)

        mb_long_cond = (
            in_vol_exp & vol_pass &
            (close > r_high) &
            (rsi > rsi_bull)
        )
        mb_short_cond = (
            in_vol_exp & vol_pass &
            (close < r_low) &
            (rsi < rsi_bear)
        )

        # Breakout score
        with np.errstate(divide='ignore', invalid='ignore'):
            breakout_pct = np.where(r_high > 0, (close - r_high) / r_high * 100, 0)
        breakout_score = np.minimum(1.0, breakout_pct / 2.0)
        with np.errstate(divide='ignore', invalid='ignore'):
            vol_ratio = np.where(vol_avg > 0, volume / vol_avg, 1.0)
        vol_score = np.minimum(1.0, (vol_ratio - vol_mult) / vol_mult)
        mb_score = mb_str + vol_score * 0.35 + breakout_score * 0.3

        sig_long  |= mb_long_cond
        sig_short |= mb_short_cond
        score = np.where(mb_long_cond | mb_short_cond,
                         np.maximum(score, mb_score), score)

        return pd.Series(sig_long), pd.Series(sig_short), pd.Series(score)

    # ─────────────────────────────────────────────────────────────────────────
    # Sequential trade simulation  (O(n) — cannot vectorize, state-dependent)
    # ─────────────────────────────────────────────────────────────────────────
    def _simulate(
        self,
        df: pd.DataFrame,
        regime: np.ndarray,
        entry_long: np.ndarray,
        entry_short: np.ndarray,
        score_arr: np.ndarray,
        p: dict,
    ) -> list[dict]:
        fee_pct  = p["fee_pct"] / 100.0
        slip_pct = p["slippage_pct"] / 100.0
        capital  = p["initial_capital"]

        open_  = df["open"].values.astype(float)
        high_  = df["high"].values.astype(float)
        low_   = df["low"].values.astype(float)
        close_ = df["close"].values.astype(float)
        atr_   = df["atr"].values.astype(float)

        n = len(df)
        trades = []
        position = None       # {direction, entry_i, entry_px, qty, sl, tp, regime, score}
        pending  = None       # (bar_index, direction, sl, tp, regime, score)
        equity   = capital

        for i in range(1, n):
            # ── Fill pending order at this bar's open (next-bar fill) ──
            if pending is not None and position is None:
                pdir, psl, ptp, pr, ps = pending
                px = open_[i]
                if pdir == "long":
                    fill = px * (1 + slip_pct)
                    sl   = psl
                    tp   = ptp
                else:
                    fill = px * (1 - slip_pct)
                    sl   = psl
                    tp   = ptp
                trade_val = equity * 0.10   # 10% position size (approx)
                if trade_val > 0 and fill > 0:
                    qty      = trade_val / fill
                    entry_fee = trade_val * fee_pct
                    equity   -= entry_fee
                    position  = dict(
                        direction=pdir, entry_i=i, entry_px=fill,
                        qty=qty, sl=sl, tp=tp, regime=pr, score=ps,
                    )
                pending = None

            # ── Check SL / TP ─────────────────────────────────────────
            if position is not None:
                d   = position["direction"]
                sl_ = position["sl"]
                tp_ = position["tp"]
                exit_px = None
                reason  = None

                if d == "long":
                    if low_[i] <= sl_:
                        exit_px, reason = sl_, "stop_loss"
                    elif high_[i] >= tp_:
                        exit_px, reason = tp_, "take_profit"
                else:
                    if high_[i] >= sl_:
                        exit_px, reason = sl_, "stop_loss"
                    elif low_[i] <= tp_:
                        exit_px, reason = tp_, "take_profit"

                if reason is not None:
                    if d == "long":
                        fill_ex = exit_px * (1 - slip_pct)
                    else:
                        fill_ex = exit_px * (1 + slip_pct)
                    qty  = position["qty"]
                    fee_ex = fill_ex * qty * fee_pct
                    if d == "long":
                        pnl = (fill_ex - position["entry_px"]) * qty - fee_ex - equity * fee_pct
                    else:
                        pnl = (position["entry_px"] - fill_ex) * qty - fee_ex - equity * fee_pct
                    equity += pnl
                    trades.append(dict(
                        entry_i=position["entry_i"], exit_i=i,
                        entry_px=position["entry_px"], exit_px=fill_ex,
                        direction=d, pnl=round(pnl, 4),
                        duration_bars=i - position["entry_i"],
                        exit_reason=reason,
                        regime=position["regime"],
                        score=position["score"],
                    ))
                    position = None

            # ── New entry signal ──────────────────────────────────────
            if position is None and pending is None:
                r_i   = regime[i]
                atr_i = atr_[i] if not math.isnan(atr_[i]) else close_[i] * 0.01
                atr_sl = p.get("atr_sl_mult_override") or REGIME_ATR_SL.get(r_i, 2.0)

                if entry_long[i]:
                    sl = close_[i] - atr_i * atr_sl
                    tp = close_[i] + atr_i * (atr_sl + 1.0)
                    pending = ("long", sl, tp, r_i, score_arr[i])
                elif entry_short[i]:
                    sl = close_[i] + atr_i * atr_sl
                    tp = close_[i] - atr_i * (atr_sl + 1.0)
                    pending = ("short", sl, tp, r_i, score_arr[i])

        # Force-close open position at final bar
        if position is not None:
            d = position["direction"]
            px = close_[n-1]
            fill_ex = px * (1 - slip_pct) if d == "long" else px * (1 + slip_pct)
            qty = position["qty"]
            fee_ex = fill_ex * qty * fee_pct
            if d == "long":
                pnl = (fill_ex - position["entry_px"]) * qty - fee_ex
            else:
                pnl = (position["entry_px"] - fill_ex) * qty - fee_ex
            equity += pnl
            trades.append(dict(
                entry_i=position["entry_i"], exit_i=n-1,
                entry_px=position["entry_px"], exit_px=fill_ex,
                direction=d, pnl=round(pnl, 4),
                duration_bars=n-1-position["entry_i"],
                exit_reason="end_of_data",
                regime=position["regime"],
                score=position["score"],
            ))

        return trades

    # ─────────────────────────────────────────────────────────────────────────
    # Metrics
    # ─────────────────────────────────────────────────────────────────────────
    def _calc_metrics(self, trades: list[dict], initial_capital: float) -> dict:
        if not trades:
            return dict(total_trades=0, win_rate=0, profit_factor=0,
                        total_return_pct=0, max_drawdown_pct=0, sharpe_ratio=0)

        wins   = [t for t in trades if t["pnl"] > 0]
        losses = [t for t in trades if t["pnl"] <= 0]
        gw = sum(t["pnl"] for t in wins)
        gl = abs(sum(t["pnl"] for t in losses))
        net = sum(t["pnl"] for t in trades)

        # Equity curve for drawdown
        running = initial_capital
        peak = initial_capital
        max_dd_abs = 0.0
        for t in trades:
            running += t["pnl"]
            if running > peak: peak = running
            dd = peak - running
            if dd > max_dd_abs: max_dd_abs = dd

        max_dd_pct = max_dd_abs / initial_capital * 100.0
        pnls = [t["pnl"] for t in trades]
        avg  = statistics.mean(pnls)
        std  = statistics.stdev(pnls) if len(pnls) > 1 else 1e-9

        return dict(
            total_trades       = len(trades),
            win_rate           = len(wins) / len(trades) * 100,
            profit_factor      = gw / gl if gl > 0 else 999.0,
            total_return_pct   = net / initial_capital * 100.0,
            max_drawdown_pct   = max_dd_pct,
            sharpe_ratio       = avg / std,
            avg_pnl            = avg,
        )


# ─────────────────────────────────────────────────────────────────────────────
# Parallel sweep infrastructure
# ─────────────────────────────────────────────────────────────────────────────

def _worker_fn(args: tuple) -> dict:
    """Top-level worker function — must be picklable (module-level)."""
    df_path, tf, params, label = args
    df_i = pd.read_parquet(df_path)
    fb   = FastBacktester()
    result = fb.run(df_i, tf, params)
    return {"label": label, "params": params, "result": result}


def run_parallel(
    df_path:     str,
    timeframe:   str,
    param_grid:  list[dict],
    max_workers: int | None = None,
    labels:      list[str] | None = None,
) -> list[dict]:
    """
    Run FastBacktester in parallel across a list of parameter dicts.

    Parameters
    ----------
    df_path : str
        Path to parquet file with pre-computed indicators.
    timeframe : str
        Timeframe string.
    param_grid : list[dict]
        List of parameter dicts to sweep.
    max_workers : int
        Number of parallel processes. Defaults to os.cpu_count() - 1.
    labels : list[str]
        Optional labels for each config. Defaults to param stringification.

    Returns
    -------
    List of result dicts, sorted by profit_factor descending.
    """
    if max_workers is None:
        max_workers = max(1, (os.cpu_count() or 2) - 1)

    if labels is None:
        labels = [str(i) for i in range(len(param_grid))]

    args_list = [
        (df_path, timeframe, params, label)
        for params, label in zip(param_grid, labels)
    ]

    t0 = __import__("time").time()
    results = []

    print(f"  [parallel] workers={max_workers}, configs={len(param_grid)}", flush=True)

    with ProcessPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(_worker_fn, arg): arg for arg in args_list}
        done = 0
        for future in as_completed(futures):
            try:
                res = future.result()
                results.append(res)
                done += 1
                elapsed = __import__("time").time() - t0
                rate = done / elapsed if elapsed > 0 else 0
                pf = res["result"]["metrics"].get("profit_factor", 0)
                print(f"  [{done:3d}/{len(param_grid)}] {res['label']:40s} "
                      f"PF={pf:.3f}  rate={rate:.1f} cfg/s", flush=True)
            except Exception as exc:
                print(f"  Worker error: {exc}", flush=True)

    elapsed = __import__("time").time() - t0
    print(f"  [parallel] done in {elapsed:.1f}s  "
          f"({len(results)/elapsed:.1f} cfg/s with {max_workers} workers)")

    results.sort(key=lambda x: -x["result"]["metrics"].get("profit_factor", 0))
    return results


if __name__ == "__main__":
    # Quick smoke test
    import pandas as pd
    import time

    print("FastBacktester smoke test...")
    df = pd.read_parquet("/tmp/BTC_1h_indicators.parquet")
    fb = FastBacktester()

    t0 = time.time()
    r  = fb.run(df, "1h")
    elapsed = time.time() - t0

    m = r["metrics"]
    print(f"  1h 4yr: {m['total_trades']} trades, "
          f"WR={m['win_rate']:.1f}%, PF={m['profit_factor']:.3f}, "
          f"ret={m['total_return_pct']:+.2f}%  [{elapsed:.2f}s]")
    print("OK")
