# ============================================================
# NEXUS TRADER — LSR: Liquidity Sweep Reversal  (Phase 4)
#
# Two-stage intraday stop-hunt reversal strategy.
#
# Setup (5m): Identifies liquidity sweep conditions
#   - Clear swing high/low from recent price action
#   - Price sweeps beyond the swing level (stop hunt)
#   - Quick rejection: close back inside the range
#   - Volume spike on sweep bar (stop orders triggering)
#
# Trigger (1m): Precise reversal entry
#   - Strong reversal candle after sweep
#   - Close firmly back inside range
#   - Entry at reversal candle close
#   - SL beyond sweep extreme, TP at opposite swing
#
# Regime affinity: strongest in ranging and volatile regimes
# where stops are clustered at obvious levels.
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


class LiquiditySweepReversalStrategy(BaseIntradayStrategy):
    """LSR — Liquidity sweep reversal (stop hunt → reversal)."""

    NAME = "liquidity_sweep_reversal"
    STRATEGY_CLASS = StrategyClass.LIQUIDITY_SWEEP_REVERSAL
    SETUP_TIMEFRAME = "5m"
    TRIGGER_TIMEFRAME = "1m"
    MAX_SETUP_AGE_MS = 15 * 60 * 1000      # 15 minutes (fast reversal)
    MAX_TRIGGER_AGE_MS = 3 * 60 * 1000      # 3 minutes
    DRIFT_TOLERANCE = 0.002                  # 0.2%
    BASE_TIME_STOP_MS = 30 * 60 * 1000      # 30 minutes

    REGIME_AFFINITY = {
        "bull_trend": 0.4,
        "bear_trend": 0.4,
        "high_volatility": 0.9,
        "low_volatility": 0.5,
        "ranging": 1.0,
        "uncertain": 0.6,
        "trending_up": 0.3,
        "trending_down": 0.3,
    }

    # ── Configuration ─────────────────────────────────────────
    _SWING_LOOKBACK = 15            # Bars to find swing points
    _SWEEP_EXCEED_ATR = 0.2         # Min sweep beyond swing level (ATR)
    _REJECTION_RATIO = 0.5          # Wick/range ratio showing rejection
    _VOL_SPIKE_RATIO = 1.5          # Volume spike on sweep
    _RR_TARGET = 2.0

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

        n = min(self._SWING_LOOKBACK, len(df) - 1)
        if n < 5:
            return None

        # Find swing high/low from lookback (excluding last bar)
        lookback_high = high.iloc[-(n + 1):-1]
        lookback_low = low.iloc[-(n + 1):-1]
        swing_high = lookback_high.max()
        swing_low = lookback_low.min()

        last_close = close.iloc[-1]
        last_high = high.iloc[-1]
        last_low = low.iloc[-1]
        last_open = df["open"].iloc[-1]
        last_vol = volume.iloc[-1]
        vol_avg = volume.iloc[-(n + 1):-1].mean()
        vol_ratio = last_vol / vol_avg if vol_avg > 0 else 0

        # Check for sweep of swing high (bullish trap → short)
        if last_high > swing_high + self._SWEEP_EXCEED_ATR * atr:
            if last_close < swing_high:
                # Swept above then closed below = bearish reversal setup
                direction = Direction.SHORT
                sweep_extreme = last_high
                bar_range = last_high - last_low
                if bar_range <= 0:
                    return None
                upper_wick = last_high - max(last_close, last_open)
                wick_ratio = upper_wick / bar_range
                if wick_ratio < self._REJECTION_RATIO:
                    return None
                if vol_ratio < self._VOL_SPIKE_RATIO:
                    return None
                stop_loss = sweep_extreme + 0.5 * atr
                entry_low = last_close - 0.2 * atr
                entry_high = last_close
                risk = stop_loss - entry_high
                take_profit = entry_low - self._RR_TARGET * risk
            else:
                return None

        # Check for sweep of swing low (bearish trap → long)
        elif last_low < swing_low - self._SWEEP_EXCEED_ATR * atr:
            if last_close > swing_low:
                direction = Direction.LONG
                sweep_extreme = last_low
                bar_range = last_high - last_low
                if bar_range <= 0:
                    return None
                lower_wick = min(last_close, last_open) - last_low
                wick_ratio = lower_wick / bar_range
                if wick_ratio < self._REJECTION_RATIO:
                    return None
                if vol_ratio < self._VOL_SPIKE_RATIO:
                    return None
                stop_loss = sweep_extreme - 0.5 * atr
                entry_low = last_close
                entry_high = last_close + 0.2 * atr
                risk = entry_low - stop_loss
                take_profit = entry_high + self._RR_TARGET * risk
            else:
                return None
        else:
            return None  # No sweep detected

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
                f"Liquidity sweep: swing {'high' if direction==Direction.SHORT else 'low'} "
                f"swept, wick_ratio={wick_ratio:.2f}, vol_spike={vol_ratio:.2f}×, "
                f"closed back inside range"
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

        # Strong reversal candle in setup direction
        if setup.direction == Direction.LONG:
            if last_close <= last_open:
                return None  # Need bullish reversal
            # Must be within or above entry zone
            if last_close < setup.entry_zone_low:
                return None
            entry_price = last_close
        else:
            if last_close >= last_open:
                return None  # Need bearish reversal
            if last_close > setup.entry_zone_high:
                return None
            entry_price = last_close

        # Reversal strength
        bar_range = high.iloc[-1] - low.iloc[-1]
        if bar_range <= 0:
            return None
        body = abs(last_close - last_open)
        body_ratio = body / bar_range

        # Volume on reversal bar
        vol_avg = volume.rolling(10).mean().iloc[-1]
        vol_ratio = volume.iloc[-1] / vol_avg if pd.notna(vol_avg) and vol_avg > 0 else 1.0

        quality = min(1.0, 0.3 + 0.4 * body_ratio + 0.3 * min(vol_ratio / 2.0, 1.0))
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
                f"Sweep reversal: body_ratio={body_ratio:.2f}, "
                f"vol={vol_ratio:.2f}×, entry={entry_price:.2f}"
            ),
            max_age_ms=self.MAX_TRIGGER_AGE_MS,
            drift_tolerance=self.DRIFT_TOLERANCE,
        )
