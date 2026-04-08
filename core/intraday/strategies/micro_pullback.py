# ============================================================
# NEXUS TRADER — MPC: Micro Pullback Continuation  (Phase 4)
#
# Two-stage intraday trend continuation strategy.
#
# Setup (15m): Identifies strong trend with pullback opportunity
#   - EMA 9/21 aligned in trend direction
#   - ADX > 25 (established trend)
#   - Price pulls back toward EMA 9 or 21 zone
#   - RSI pulls back but stays in trend range (40-60 zone)
#
# Trigger (3m): Precise pullback entry
#   - Price touches or enters EMA pullback zone
#   - Momentum candle in trend direction
#   - Volume confirmation on entry bar
#   - Entry at close of momentum candle
#
# Regime affinity: strongest in trending regimes, moderate
# in volatility expansion, weak in ranging.
#
# ZERO PySide6 imports.
# ============================================================
from __future__ import annotations

import logging
from typing import Optional

import numpy as np
import pandas as pd

from core.intraday.base_strategy import BaseIntradayStrategy, RegimeInfo
from core.intraday.signal_contracts import (
    Direction,
    SetupLifecycle,
    SetupSignal,
    StrategyClass,
    TriggerLifecycle,
    TriggerSignal,
    make_setup_id,
    make_trigger_id,
)

logger = logging.getLogger(__name__)


class MicroPullbackStrategy(BaseIntradayStrategy):
    """MPC — Micro pullback continuation (trend + retracement → continuation)."""

    NAME = "micro_pullback_continuation"
    STRATEGY_CLASS = StrategyClass.MICRO_PULLBACK_CONTINUATION
    SETUP_TIMEFRAME = "15m"
    TRIGGER_TIMEFRAME = "3m"
    MAX_SETUP_AGE_MS = 60 * 60 * 1000      # 60 minutes
    MAX_TRIGGER_AGE_MS = 6 * 60 * 1000      # 6 minutes
    DRIFT_TOLERANCE = 0.0025                 # 0.25%
    BASE_TIME_STOP_MS = 90 * 60 * 1000      # 90 minutes

    REGIME_AFFINITY = {
        "bull_trend": 1.0,
        "bear_trend": 1.0,
        "high_volatility": 0.6,
        "low_volatility": 0.3,
        "ranging": 0.1,
        "uncertain": 0.3,
        "trending_up": 0.9,
        "trending_down": 0.9,
    }

    # ── Configuration ─────────────────────────────────────────
    _ADX_MIN = 25.0
    _EMA_FAST = 9
    _EMA_SLOW = 21
    _PULLBACK_ATR_MAX = 1.5     # Max pullback depth (ATR from EMA)
    _RSI_LONG_RANGE = (40, 65)  # RSI range during long pullback
    _RSI_SHORT_RANGE = (35, 60) # RSI range during short pullback
    _RR_TARGET = 2.5

    def evaluate_setup(
        self,
        symbol: str,
        df_setup: pd.DataFrame,
        regime_info: RegimeInfo,
    ) -> Optional[SetupSignal]:
        df = df_setup.copy()
        close = df["close"]

        atr = self._atr(df)
        if atr <= 0:
            return None

        ema_fast = self._ema(close, self._EMA_FAST)
        ema_slow = self._ema(close, self._EMA_SLOW)
        last_close = close.iloc[-1]
        last_ema_fast = ema_fast.iloc[-1]
        last_ema_slow = ema_slow.iloc[-1]

        if pd.isna(last_ema_fast) or pd.isna(last_ema_slow):
            return None

        # ADX check
        adx_val = self._col(df, "adx_14")
        if adx_val is None:
            adx_val = self._compute_adx(df)
        if adx_val is None or adx_val < self._ADX_MIN:
            return None

        # RSI
        rsi = self._rsi(close).iloc[-1]
        if pd.isna(rsi):
            return None

        # Determine trend and pullback
        if last_ema_fast > last_ema_slow:
            # Uptrend — look for pullback to EMA zone
            direction = Direction.LONG
            pullback_zone_top = last_ema_fast
            pullback_zone_bottom = last_ema_slow - 0.5 * atr

            # Price must be near EMA zone (pulled back)
            dist_to_fast = last_close - last_ema_fast
            if dist_to_fast > self._PULLBACK_ATR_MAX * atr:
                return None  # Not pulled back enough
            if last_close < pullback_zone_bottom:
                return None  # Pulled back too far

            # RSI in pullback range
            if not (self._RSI_LONG_RANGE[0] <= rsi <= self._RSI_LONG_RANGE[1]):
                return None

            stop_loss = last_ema_slow - 1.0 * atr
            entry_low = pullback_zone_bottom
            entry_high = pullback_zone_top + 0.2 * atr
            risk = entry_low - stop_loss
            take_profit = entry_high + self._RR_TARGET * risk

        elif last_ema_fast < last_ema_slow:
            # Downtrend — look for pullback to EMA zone
            direction = Direction.SHORT
            pullback_zone_top = last_ema_slow + 0.5 * atr
            pullback_zone_bottom = last_ema_fast

            dist_to_fast = last_ema_fast - last_close
            if dist_to_fast > self._PULLBACK_ATR_MAX * atr:
                return None
            if last_close > pullback_zone_top:
                return None

            if not (self._RSI_SHORT_RANGE[0] <= rsi <= self._RSI_SHORT_RANGE[1]):
                return None

            stop_loss = last_ema_slow + 1.0 * atr
            entry_low = pullback_zone_bottom - 0.2 * atr
            entry_high = pullback_zone_top
            risk = stop_loss - entry_high
            take_profit = entry_low - self._RR_TARGET * risk
        else:
            return None

        if stop_loss <= 0 or take_profit <= 0 or entry_low <= 0 or entry_high <= 0:
            return None
        if entry_low > entry_high:
            return None

        last_ts = int(df["timestamp"].iloc[-1]) if "timestamp" in df.columns else 0
        setup_id = make_setup_id(self.NAME, symbol, direction.value, last_ts)

        return SetupSignal(
            setup_id=setup_id,
            strategy_name=self.NAME,
            strategy_class=self.STRATEGY_CLASS,
            symbol=symbol,
            direction=direction,
            setup_timeframe=self.SETUP_TIMEFRAME,
            trigger_timeframe=self.TRIGGER_TIMEFRAME,
            entry_zone_low=round(entry_low, 8),
            entry_zone_high=round(entry_high, 8),
            stop_loss=round(stop_loss, 8),
            take_profit=round(take_profit, 8),
            atr_value=atr,
            regime=regime_info.label,
            regime_confidence=regime_info.confidence,
            setup_candle_ts=last_ts,
            candle_trace_ids=self._candle_trace_ids(df),
            lifecycle=SetupLifecycle.QUALIFIED,
            rationale=(
                f"Trend pullback: EMA{self._EMA_FAST}={'>' if direction==Direction.LONG else '<'}EMA{self._EMA_SLOW}, "
                f"ADX={adx_val:.1f}, RSI={rsi:.1f}, pullback to EMA zone"
            ),
            max_age_ms=self.MAX_SETUP_AGE_MS,
            drift_tolerance=self.DRIFT_TOLERANCE,
            base_time_stop_ms=self.BASE_TIME_STOP_MS,
        )

    def evaluate_trigger(
        self,
        symbol: str,
        df_trigger: pd.DataFrame,
        setup: SetupSignal,
        regime_info: RegimeInfo,
    ) -> Optional[TriggerSignal]:
        close = df_trigger["close"]
        open_ = df_trigger["open"]
        volume = df_trigger["volume"]

        last_close = close.iloc[-1]
        last_open = open_.iloc[-1]

        # Momentum candle in trend direction
        if setup.direction == Direction.LONG:
            if last_close <= last_open:
                return None  # Need bullish candle
            # Price in entry zone
            if last_close < setup.entry_zone_low or last_close > setup.entry_zone_high + setup.atr_value:
                return None
            entry_price = last_close
        else:
            if last_close >= last_open:
                return None  # Need bearish candle
            if last_close > setup.entry_zone_high or last_close < setup.entry_zone_low - setup.atr_value:
                return None
            entry_price = last_close

        # Volume confirmation
        vol_avg = volume.rolling(10).mean().iloc[-1]
        vol_ratio = volume.iloc[-1] / vol_avg if pd.notna(vol_avg) and vol_avg > 0 else 1.0

        # Quality from momentum strength and volume
        body = abs(last_close - last_open)
        bar_range = df_trigger["high"].iloc[-1] - df_trigger["low"].iloc[-1]
        body_ratio = body / bar_range if bar_range > 0 else 0
        quality = min(1.0, 0.3 + 0.35 * body_ratio + 0.35 * min(vol_ratio / 2.0, 1.0))
        strength = min(1.0, quality * self.get_regime_weight(regime_info.label))

        atr = self._atr(df_trigger)
        if atr <= 0:
            atr = setup.atr_value

        last_ts = int(df_trigger["timestamp"].iloc[-1]) if "timestamp" in df_trigger.columns else 0
        trigger_id = make_trigger_id(setup.setup_id, last_ts)

        return TriggerSignal(
            trigger_id=trigger_id,
            setup_id=setup.setup_id,
            strategy_name=self.NAME,
            strategy_class=self.STRATEGY_CLASS,
            symbol=symbol,
            direction=setup.direction,
            entry_price=round(entry_price, 8),
            stop_loss=round(setup.stop_loss, 8),
            take_profit=round(setup.take_profit, 8),
            atr_value=atr,
            strength=round(strength, 4),
            trigger_quality=round(quality, 4),
            setup_timeframe=self.SETUP_TIMEFRAME,
            trigger_timeframe=self.TRIGGER_TIMEFRAME,
            regime=regime_info.label,
            regime_confidence=regime_info.confidence,
            trigger_candle_ts=last_ts,
            setup_candle_ts=setup.setup_candle_ts,
            candle_trace_ids=self._candle_trace_ids(df_trigger),
            setup_trace_ids=setup.candle_trace_ids,
            lifecycle=TriggerLifecycle.FIRED,
            rationale=(
                f"Pullback entry: momentum candle body={body_ratio:.2f}, "
                f"vol_ratio={vol_ratio:.2f}, entry={entry_price:.2f}"
            ),
            max_age_ms=self.MAX_TRIGGER_AGE_MS,
            drift_tolerance=self.DRIFT_TOLERANCE,
        )

    @staticmethod
    def _compute_adx(df: pd.DataFrame, period: int = 14) -> Optional[float]:
        if len(df) < period * 2:
            return None
        high = df["high"]
        low = df["low"]
        close = df["close"]
        plus_dm = high.diff()
        minus_dm = -low.diff()
        plus_dm = plus_dm.where((plus_dm > minus_dm) & (plus_dm > 0), 0.0)
        minus_dm = minus_dm.where((minus_dm > plus_dm) & (minus_dm > 0), 0.0)
        tr = pd.concat([
            high - low,
            (high - close.shift(1)).abs(),
            (low - close.shift(1)).abs(),
        ], axis=1).max(axis=1)
        atr = tr.rolling(period).mean()
        plus_di = 100 * (plus_dm.rolling(period).mean() / atr.replace(0, np.nan))
        minus_di = 100 * (minus_dm.rolling(period).mean() / atr.replace(0, np.nan))
        dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
        adx = dx.rolling(period).mean()
        val = adx.iloc[-1]
        return float(val) if pd.notna(val) else None
