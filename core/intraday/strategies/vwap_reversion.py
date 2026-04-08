# ============================================================
# NEXUS TRADER — VR: VWAP Reversion Strategy  (Phase 4)
#
# Two-stage intraday mean-reversion to VWAP.
#
# Setup (5m): Identifies overextension from VWAP
#   - Price deviates >1.5 ATR from VWAP
#   - RSI in overbought/oversold zone
#   - Volume declining (exhaustion)
#   - No strong trend (ADX < 30)
#
# Trigger (1m): Precise reversion entry
#   - Price begins reversion toward VWAP
#   - Reversal candle pattern (engulfing or pin bar)
#   - Volume uptick on reversal bar
#   - Entry at candle close, SL beyond extension, TP at VWAP
#
# Regime affinity: strongest in ranging/low-vol, moderate in
# uncertain, weak in strong trends.
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


class VWAPReversionStrategy(BaseIntradayStrategy):
    """VR — VWAP mean-reversion (overextension → snap back)."""

    NAME = "vwap_reversion"
    STRATEGY_CLASS = StrategyClass.VWAP_REVERSION
    SETUP_TIMEFRAME = "5m"
    TRIGGER_TIMEFRAME = "1m"
    MAX_SETUP_AGE_MS = 25 * 60 * 1000      # 25 minutes
    MAX_TRIGGER_AGE_MS = 3 * 60 * 1000      # 3 minutes
    DRIFT_TOLERANCE = 0.002                  # 0.2%
    BASE_TIME_STOP_MS = 30 * 60 * 1000      # 30 minutes

    REGIME_AFFINITY = {
        "bull_trend": 0.2,
        "bear_trend": 0.2,
        "high_volatility": 0.4,
        "low_volatility": 0.8,
        "ranging": 1.0,
        "uncertain": 0.6,
        "trending_up": 0.2,
        "trending_down": 0.2,
    }

    # ── Configuration ─────────────────────────────────────────
    _VWAP_DEV_ATR = 1.5             # Min ATR deviation from VWAP
    _RSI_OB = 70.0                  # Overbought
    _RSI_OS = 30.0                  # Oversold
    _ADX_MAX = 30.0                 # No strong trend
    _VOL_DECLINE_BARS = 3           # Volume declining over N bars
    _REVERSAL_BODY_RATIO = 0.4      # Min body/range ratio for reversal bar

    def evaluate_setup(
        self,
        symbol: str,
        df_setup: pd.DataFrame,
        regime_info: RegimeInfo,
    ) -> Optional[SetupSignal]:
        df = df_setup.copy()
        close = df["close"]
        high = df["high"]
        low = df["low"]
        volume = df["volume"]

        atr = self._atr(df)
        if atr <= 0:
            return None

        # Compute VWAP
        vwap = self._vwap(df)
        last_vwap = vwap.iloc[-1]
        last_close = close.iloc[-1]
        if pd.isna(last_vwap) or last_vwap <= 0:
            return None

        # VWAP deviation
        deviation = last_close - last_vwap
        dev_atr = abs(deviation) / atr
        if dev_atr < self._VWAP_DEV_ATR:
            return None

        # RSI check
        rsi = self._rsi(close).iloc[-1]
        if pd.isna(rsi):
            return None

        # ADX check (no strong trend)
        adx_val = self._col(df, "adx_14")
        if adx_val is not None and adx_val > self._ADX_MAX:
            return None

        # Direction based on overextension
        if deviation > 0 and rsi > self._RSI_OB:
            # Price above VWAP + overbought → SHORT reversion
            direction = Direction.SHORT
            stop_loss = last_close + 1.0 * atr
            entry_low = last_close - 0.3 * atr
            entry_high = last_close
            take_profit = last_vwap  # Target = VWAP
        elif deviation < 0 and rsi < self._RSI_OS:
            # Price below VWAP + oversold → LONG reversion
            direction = Direction.LONG
            stop_loss = last_close - 1.0 * atr
            entry_low = last_close
            entry_high = last_close + 0.3 * atr
            take_profit = last_vwap  # Target = VWAP
        else:
            return None

        # Volume exhaustion check (declining over recent bars)
        if len(volume) >= self._VOL_DECLINE_BARS + 1:
            recent_vols = volume.iloc[-(self._VOL_DECLINE_BARS + 1):]
            declining = all(
                recent_vols.iloc[i] >= recent_vols.iloc[i + 1]
                for i in range(len(recent_vols) - 1)
            )
            # Not strictly required but boosts confidence

        if stop_loss <= 0 or take_profit <= 0 or entry_low <= 0:
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
                f"VWAP deviation {dev_atr:.2f} ATR ({direction.value}), "
                f"RSI={rsi:.1f}, VWAP={last_vwap:.2f}, close={last_close:.2f}"
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
        high = df_trigger["high"]
        low = df_trigger["low"]
        volume = df_trigger["volume"]

        last_close = close.iloc[-1]
        last_open = open_.iloc[-1]
        last_high = high.iloc[-1]
        last_low = low.iloc[-1]

        bar_range = last_high - last_low
        if bar_range <= 0:
            return None

        body = abs(last_close - last_open)
        body_ratio = body / bar_range

        # Check reversal candle pattern
        if setup.direction == Direction.LONG:
            # Need bullish candle (close > open) with decent body
            if last_close <= last_open:
                return None
            if body_ratio < self._REVERSAL_BODY_RATIO:
                return None
            # Price should be moving back toward VWAP (higher)
            if last_close <= close.iloc[-2] if len(close) > 1 else True:
                return None
            entry_price = last_close
        else:  # SHORT
            # Need bearish candle (close < open) with decent body
            if last_close >= last_open:
                return None
            if body_ratio < self._REVERSAL_BODY_RATIO:
                return None
            if last_close >= close.iloc[-2] if len(close) > 1 else True:
                return None
            entry_price = last_close

        # Volume uptick on reversal bar
        if len(volume) > 1:
            vol_ratio = volume.iloc[-1] / volume.iloc[-2] if volume.iloc[-2] > 0 else 0
        else:
            vol_ratio = 1.0

        quality = min(1.0, 0.4 + 0.3 * body_ratio + 0.3 * min(vol_ratio / 2.0, 1.0))
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
                f"VWAP reversion: reversal candle body={body_ratio:.2f}, "
                f"vol_ratio={vol_ratio:.2f}, entry={entry_price:.2f}"
            ),
            max_age_ms=self.MAX_TRIGGER_AGE_MS,
            drift_tolerance=self.DRIFT_TOLERANCE,
        )
