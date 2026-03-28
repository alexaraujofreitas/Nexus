"""
core/regime/research_regime_classifier.py
==========================================
Exact port of scripts/btc_regime_labeler.py regime-labeling logic.

Used exclusively by PullbackLongModel (30m regime) and
SwingLowContinuationModel (1h regime) so that their signal generation
exactly matches the Phase 5 research system (v7_final, commit daf41dc).

Parameters match PARAMS in btc_regime_labeler.py verbatim:
  adx_trend_min      = 22        (vs NexusTrader HMM which uses ~25)
  atr_expansion_mult = 1.80
  ATR baseline bars  = 100
  BARS_5D            = 240       (fixed, NOT scaled to timeframe)
  BARS_2D            = 96        (fixed, NOT scaled to timeframe)
  crash_peak_bars    = 48        (fixed, NOT scaled to timeframe)
  hysteresis_bars    = 3

Regime integer codes:
  SIDEWAYS       = 0   (default — everything else)
  BULL_TREND     = 1   — ADX≥22, EMA20>EMA50, price>EMA200, ATR_ratio<1.80
  BEAR_TREND     = 2   — ADX≥22, EMA20≤EMA50, price≤EMA200, ATR_ratio<1.80
  BULL_EXPANSION = 3   — ATR_ratio≥1.80, 5d_ret≥6%
  BEAR_EXPANSION = 4   — ATR_ratio≥1.80, 5d_ret≤-6%, peak_dd<15%
  CRASH_PANIC    = 5   — (peak_dd≥15% OR 2d_ret≤-8%) AND ATR_ratio≥2.80

Priority: CRASH_PANIC > BEAR_EXPANSION > BULL_EXPANSION > BEAR_TREND > BULL_TREND > SIDEWAYS

ADX note: regime classifier uses ewm(span=14) — standard EWM, alpha=2/15 ≈ 0.133.
          This differs from Wilder smoothing (ewm(alpha=1/14)) used by signal
          conditions.  Both are intentional and match the research scripts.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

# ── Regime integer codes ────────────────────────────────────────────────────
SIDEWAYS       = 0
BULL_TREND     = 1
BEAR_TREND     = 2
BULL_EXPANSION = 3
BEAR_EXPANSION = 4
CRASH_PANIC    = 5

# ── Labeling parameters (exact copy of PARAMS in btc_regime_labeler.py) ────
_ADX_TREND_MIN     = 22
_ATR_EXPANSION     = 1.80
_ATR_CRASH         = 2.80
_ATR_BASELINE_BARS = 100
_BARS_5D           = 240     # 5 days at 30m — intentionally NOT scaled to TF
_BARS_2D           = 96      # 2 days at 30m — intentionally NOT scaled to TF
_CRASH_PEAK_BARS   = 48      # 24h at 30m — intentionally NOT scaled to TF
_HYSTERESIS_BARS   = 3


# ── Internal helpers (matching btc_regime_labeler.py exactly) ───────────────

def _ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()


def _adx_standard_ewm(high: pd.Series, low: pd.Series, close: pd.Series,
                       period: int = 14) -> pd.Series:
    """
    ADX using standard EWM (span=period) — matches btc_regime_labeler._adx().
    NOTE: This is NOT Wilder smoothing.  Uses alpha=2/(period+1) not alpha=1/period.
    """
    ph = high.shift(1)
    pl = low.shift(1)
    pc = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - pc).abs(),
        (low  - pc).abs(),
    ], axis=1).max(axis=1)
    up   = high - ph
    down = pl   - low
    pdm  = pd.Series(np.where((up > down) & (up > 0),   up,   0.0), index=close.index)
    ndm  = pd.Series(np.where((down > up) & (down > 0), down, 0.0), index=close.index)
    tr_s = tr.ewm(span=period, adjust=False).mean()
    pdi  = 100 * pdm.ewm(span=period, adjust=False).mean() / tr_s.replace(0, np.nan)
    ndi  = 100 * ndm.ewm(span=period, adjust=False).mean() / tr_s.replace(0, np.nan)
    dx   = 100 * (pdi - ndi).abs() / (pdi + ndi).replace(0, np.nan)
    return dx.ewm(span=period, adjust=False).mean().fillna(0)


# ── Public API ──────────────────────────────────────────────────────────────

def classify_series(df: pd.DataFrame) -> np.ndarray:
    """
    Classify regime for every bar in df.

    Parameters
    ----------
    df : pd.DataFrame
        Must contain 'open', 'high', 'low', 'close'.
        Any timeframe is supported; returns one label per row.

    Returns
    -------
    np.ndarray of dtype int8
        Regime integer for each bar (SIDEWAYS=0 … CRASH_PANIC=5).
        Includes hysteresis smoothing (3 bars).
    """
    close = df["close"]
    high  = df["high"]
    low   = df["low"]
    n     = len(df)

    if n < 5:
        return np.zeros(n, dtype=np.int8)

    # ── Compute indicators ────────────────────────────────────────────────
    adx   = _adx_standard_ewm(high, low, close, 14)
    ema20 = _ema(close, 20)
    ema50 = _ema(close, 50)
    ema200 = _ema(close, 200)

    # ATR ratio (current ATR / rolling 100-bar mean ATR)
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low  - prev_close).abs(),
    ], axis=1).max(axis=1)
    atr = tr.ewm(span=14, adjust=False).mean()
    atr_baseline = atr.rolling(_ATR_BASELINE_BARS, min_periods=10).mean()
    atr_ratio = (atr / atr_baseline.replace(0, np.nan)).fillna(1.0).clip(0, 20)

    # Rolling returns (fixed bar counts — NOT scaled to timeframe)
    ret5d = close.pct_change(_BARS_5D)
    ret2d = close.pct_change(_BARS_2D)

    # Rolling peak drawdown
    roll_peak = close.rolling(_CRASH_PEAK_BARS, min_periods=1).max()
    peak_dd   = ((roll_peak - close) / roll_peak.replace(0, np.nan)).fillna(0).clip(0, 1)

    # ── Convert to numpy for vectorized labeling ──────────────────────────
    ema_bull  = (ema20.values  > ema50.values).astype(np.int8)
    abv_slow  = (close.values  > ema200.values).astype(np.int8)
    adx_v     = adx.values
    atr_r_v   = atr_ratio.values
    ret5d_v   = ret5d.values
    ret2d_v   = ret2d.values
    peak_dd_v = peak_dd.values

    # ── Apply priority-ordered labeling (matches label_regimes() exactly) ─
    labels = np.zeros(n, dtype=np.int8)  # SIDEWAYS = 0 (default)

    # Priority 1: BULL_TREND
    bull = (
        (adx_v >= _ADX_TREND_MIN) &
        (ema_bull == 1)            &
        (abv_slow == 1)            &
        (atr_r_v < _ATR_EXPANSION)
    )
    labels[bull] = BULL_TREND

    # Priority 2: BEAR_TREND (overrides BULL_TREND when conditions match)
    bear = (
        (adx_v >= _ADX_TREND_MIN) &
        (ema_bull == 0)            &
        (abv_slow == 0)            &
        (atr_r_v < _ATR_EXPANSION)
    )
    labels[bear] = BEAR_TREND

    # Priority 3: BULL_EXPANSION (overrides trend labels)
    valid5 = ~np.isnan(ret5d_v)
    bull_exp = valid5 & (atr_r_v >= _ATR_EXPANSION) & (ret5d_v >= 0.06)
    labels[bull_exp] = BULL_EXPANSION

    # Priority 4: BEAR_EXPANSION (overrides trend + bull expansion)
    bear_exp = valid5 & (atr_r_v >= _ATR_EXPANSION) & (ret5d_v <= -0.06) & (peak_dd_v < 0.15)
    labels[bear_exp] = BEAR_EXPANSION

    # Priority 5: CRASH_PANIC — highest, overrides everything
    valid2 = ~np.isnan(ret2d_v)
    crash = valid2 & (
        ((peak_dd_v >= 0.15) | (ret2d_v <= -0.08)) &
        (atr_r_v >= _ATR_CRASH)
    )
    labels[crash] = CRASH_PANIC

    # ── Apply hysteresis smoothing (3-bar minimum run) ────────────────────
    return _apply_hysteresis(labels, _HYSTERESIS_BARS)


def classify_latest_bar(df: pd.DataFrame) -> int:
    """
    Classify only the LAST bar of df.

    Efficient shortcut for live scanner: compute the full series but return
    only the final label.  Includes hysteresis.

    Parameters
    ----------
    df : pd.DataFrame
        Rolling window (recommend ≥ 300 bars for EMA200 to be stable).

    Returns
    -------
    int  (SIDEWAYS=0 … CRASH_PANIC=5)
    """
    labels = classify_series(df)
    return int(labels[-1]) if len(labels) > 0 else SIDEWAYS


def _apply_hysteresis(labels: np.ndarray, min_bars: int) -> np.ndarray:
    """
    Merge runs shorter than min_bars into the preceding regime.
    Two passes handle consecutive short runs.
    Exact port of apply_hysteresis() in btc_regime_labeler.py.
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
