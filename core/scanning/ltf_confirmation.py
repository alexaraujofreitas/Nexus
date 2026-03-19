"""
LTF (Lower-Timeframe) Confirmation Module — 15m closed-candle entry confirmation.

This module evaluates whether a staged HTF candidate (from the 1H pipeline) is
confirmed by lower-timeframe (15m) market structure.  It is intentionally
lightweight — only 3 confirmation indicators are computed (EMA9, RSI14,
volume ratio), NOT the full 80+ indicator stack.

Confirmation logic:
  1. EMA alignment — 15m EMA9 trend matches the HTF signal direction
  2. RSI momentum  — RSI not overbought (long) or oversold (short)
  3. Volume filter  — recent 15m volume above average (participation)
  4. Anti-churn     — void candidate if RSI strongly contradicts direction

All thresholds are configurable via settings:
  - ltf_confirmation.ema_period         (default 9)
  - ltf_confirmation.rsi_period         (default 14)
  - ltf_confirmation.rsi_max_long       (default 72)  — reject long if RSI above
  - ltf_confirmation.rsi_min_short      (default 28)  — reject short if RSI below
  - ltf_confirmation.rsi_void_long      (default 78)  — void long candidate
  - ltf_confirmation.rsi_void_short     (default 22)  — void short candidate
  - ltf_confirmation.volume_ratio_min   (default 0.8) — min vol vs 20-bar avg
  - ltf_confirmation.volume_lookback    (default 20)
  - ltf_confirmation.ema_slope_bars     (default 3)   — bars for EMA trend check
  - ltf_confirmation.timeframe          (default "15m")
  - ltf_confirmation.ohlcv_limit        (default 100)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


# ── Configuration Defaults ──────────────────────────────────────────────

@dataclass
class LTFConfirmationConfig:
    """All configurable thresholds for LTF confirmation."""
    ema_period: int = 9
    rsi_period: int = 14
    rsi_max_long: float = 72.0      # reject long if RSI above this
    rsi_min_short: float = 28.0     # reject short if RSI below this
    rsi_void_long: float = 78.0     # void long candidate (anti-churn)
    rsi_void_short: float = 22.0    # void short candidate (anti-churn)
    volume_ratio_min: float = 0.6   # min volume vs 20-bar average (lowered from 0.80 based on backtest study)
    volume_lookback: int = 20
    ema_slope_bars: int = 3         # bars for EMA trend direction
    timeframe: str = "15m"
    ohlcv_limit: int = 100

    @staticmethod
    def from_settings() -> "LTFConfirmationConfig":
        """Load from NexusTrader settings, falling back to defaults."""
        cfg = LTFConfirmationConfig()
        try:
            from config.settings import settings as _s
            cfg.ema_period       = int(_s.get("ltf_confirmation.ema_period", cfg.ema_period))
            cfg.rsi_period       = int(_s.get("ltf_confirmation.rsi_period", cfg.rsi_period))
            cfg.rsi_max_long     = float(_s.get("ltf_confirmation.rsi_max_long", cfg.rsi_max_long))
            cfg.rsi_min_short    = float(_s.get("ltf_confirmation.rsi_min_short", cfg.rsi_min_short))
            cfg.rsi_void_long    = float(_s.get("ltf_confirmation.rsi_void_long", cfg.rsi_void_long))
            cfg.rsi_void_short   = float(_s.get("ltf_confirmation.rsi_void_short", cfg.rsi_void_short))
            cfg.volume_ratio_min = float(_s.get("ltf_confirmation.volume_ratio_min", cfg.volume_ratio_min))
            cfg.volume_lookback  = int(_s.get("ltf_confirmation.volume_lookback", cfg.volume_lookback))
            cfg.ema_slope_bars   = int(_s.get("ltf_confirmation.ema_slope_bars", cfg.ema_slope_bars))
            cfg.timeframe        = str(_s.get("ltf_confirmation.timeframe", cfg.timeframe))
            cfg.ohlcv_limit      = int(_s.get("ltf_confirmation.ohlcv_limit", cfg.ohlcv_limit))
        except Exception:
            pass
        return cfg


# ── Lightweight Indicator Computation ───────────────────────────────────

def compute_ema(series: pd.Series, period: int) -> pd.Series:
    """Exponential Moving Average."""
    return series.ewm(span=period, adjust=False).mean()


def compute_rsi(series: pd.Series, period: int) -> pd.Series:
    """Relative Strength Index."""
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = (-delta.clip(upper=0))
    avg_gain = gain.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    # When avg_loss == 0 (pure uptrend), RSI should be 100.
    # When avg_gain == 0 (pure downtrend), RSI should be 0.
    rsi = pd.Series(np.where(
        avg_loss == 0,
        np.where(avg_gain == 0, 50.0, 100.0),   # 0/0 → neutral; gain/0 → 100
        100.0 - (100.0 / (1.0 + avg_gain / avg_loss)),
    ), index=series.index)
    # Preserve NaN for warmup period
    rsi[:period] = np.nan
    return rsi


def compute_ltf_indicators(
    df: pd.DataFrame,
    cfg: LTFConfirmationConfig,
) -> pd.DataFrame:
    """Compute lightweight 15m indicators.  Returns the input DataFrame with
    3 new columns: ``ltf_ema``, ``ltf_rsi``, ``ltf_vol_ratio``.
    """
    df = df.copy()
    df["ltf_ema"] = compute_ema(df["close"], cfg.ema_period)
    df["ltf_rsi"] = compute_rsi(df["close"], cfg.rsi_period)

    vol_avg = df["volume"].rolling(cfg.volume_lookback, min_periods=1).mean()
    df["ltf_vol_ratio"] = df["volume"] / vol_avg.replace(0, np.nan)
    df["ltf_vol_ratio"] = df["ltf_vol_ratio"].fillna(1.0)

    return df


# ── Confirmation Result ─────────────────────────────────────────────────

@dataclass
class LTFConfirmationResult:
    """Result of a single LTF confirmation evaluation."""
    confirmed: bool                   # True → ready for execution
    voided: bool                      # True → candidate should be voided
    void_reason: Optional[str] = None # Reason if voided

    # Diagnostic data
    ema_aligned: bool = False
    ema_slope: float = 0.0            # positive = up, negative = down
    rsi: float = 50.0
    rsi_ok: bool = False
    volume_ratio: float = 1.0
    volume_ok: bool = False
    ltf_close: float = 0.0           # 15m close price at evaluation time

    @property
    def checks_passed(self) -> int:
        return sum([self.ema_aligned, self.rsi_ok, self.volume_ok])


# ── Core Confirmation Logic ─────────────────────────────────────────────

def evaluate_confirmation(
    df: pd.DataFrame,
    side: str,
    cfg: Optional[LTFConfirmationConfig] = None,
) -> LTFConfirmationResult:
    """Evaluate whether 15m data confirms an HTF signal.

    Parameters
    ----------
    df : pd.DataFrame
        15m OHLCV data with at least ``cfg.ema_period + cfg.ema_slope_bars``
        closed bars.  Must have columns: close, volume.
    side : str
        Signal direction from HTF: ``"buy"`` or ``"sell"``.
    cfg : LTFConfirmationConfig, optional
        Configuration.  Defaults to ``LTFConfirmationConfig()``.

    Returns
    -------
    LTFConfirmationResult
    """
    if cfg is None:
        cfg = LTFConfirmationConfig()

    is_long = side.lower() in ("buy", "long")

    # Compute indicators
    df = compute_ltf_indicators(df, cfg)

    # Extract last row values
    last = df.iloc[-1]
    rsi = float(last.get("ltf_rsi", 50.0))
    vol_ratio = float(last.get("ltf_vol_ratio", 1.0))
    ltf_close = float(last["close"])

    # ── EMA alignment check ─────────────────────────────────────
    # Is the EMA trending in the signal's direction over the last N bars?
    ema_values = df["ltf_ema"].iloc[-cfg.ema_slope_bars:]
    if len(ema_values) >= 2:
        ema_slope = float(ema_values.iloc[-1] - ema_values.iloc[0])
    else:
        ema_slope = 0.0

    if is_long:
        ema_aligned = ema_slope > 0
    else:
        ema_aligned = ema_slope < 0

    # ── RSI momentum check ──────────────────────────────────────
    if is_long:
        rsi_ok = rsi < cfg.rsi_max_long
    else:
        rsi_ok = rsi > cfg.rsi_min_short

    # ── Anti-churn: void if RSI strongly contradicts ────────────
    voided = False
    void_reason = None
    if is_long and rsi > cfg.rsi_void_long:
        voided = True
        void_reason = f"anti-churn: 15m RSI {rsi:.1f} > {cfg.rsi_void_long} (long signal overbought)"
    elif not is_long and rsi < cfg.rsi_void_short:
        voided = True
        void_reason = f"anti-churn: 15m RSI {rsi:.1f} < {cfg.rsi_void_short} (short signal oversold)"

    # ── Volume filter ───────────────────────────────────────────
    volume_ok = vol_ratio >= cfg.volume_ratio_min

    # ── Final verdict ───────────────────────────────────────────
    # Confirmed = all 3 checks pass AND not voided
    confirmed = (ema_aligned and rsi_ok and volume_ok) and not voided

    result = LTFConfirmationResult(
        confirmed=confirmed,
        voided=voided,
        void_reason=void_reason,
        ema_aligned=ema_aligned,
        ema_slope=ema_slope,
        rsi=rsi,
        rsi_ok=rsi_ok,
        volume_ratio=vol_ratio,
        volume_ok=volume_ok,
        ltf_close=ltf_close,
    )

    logger.info(
        "LTFConfirmation: side=%s | ema_aligned=%s (slope=%.4f) | "
        "rsi=%.1f (ok=%s) | vol_ratio=%.2f (ok=%s) | "
        "confirmed=%s | voided=%s%s",
        side, ema_aligned, ema_slope,
        rsi, rsi_ok, vol_ratio, volume_ok,
        confirmed, voided,
        f" [{void_reason}]" if void_reason else "",
    )

    return result
