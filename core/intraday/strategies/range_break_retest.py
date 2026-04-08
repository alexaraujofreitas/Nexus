# ============================================================
# NEXUS TRADER — RBR: Range Break Retest Strategy  (Phase 4)
#
# Two-stage intraday breakout-retest strategy.
#
# Setup (15m): Identifies range boundaries and breakout
#   - Defined range: price confined between support/resistance
#     for ≥5 bars (consolidation)
#   - Breakout: close beyond range boundary
#   - Volume increase on breakout bar
#
# Trigger (1m): Precise retest entry
#   - Price retests the broken level (resistance→support or vice versa)
#   - Rejection candle at retest (shows level is holding)
#   - Entry at close of rejection candle
#   - SL below/above retest level, TP at range projection
#
# Regime affinity: strongest in volatility expansion and trending,
# moderate in ranging (breakout from range), weak in compressed.
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


class RangeBreakRetestStrategy(BaseIntradayStrategy):
    """RBR — Range breakout with retest entry (consolidation → break → retest)."""

    NAME = "range_break_retest"
    STRATEGY_CLASS = StrategyClass.RANGE_BREAK_RETEST
    SETUP_TIMEFRAME = "15m"
    TRIGGER_TIMEFRAME = "1m"
    MAX_SETUP_AGE_MS = 60 * 60 * 1000      # 60 minutes
    MAX_TRIGGER_AGE_MS = 10 * 60 * 1000     # 10 minutes
    DRIFT_TOLERANCE = 0.003                  # 0.3%
    BASE_TIME_STOP_MS = 120 * 60 * 1000     # 2 hours

    REGIME_AFFINITY = {
        "bull_trend": 0.7,
        "bear_trend": 0.7,
        "high_volatility": 0.9,
        "low_volatility": 0.4,
        "ranging": 0.8,
        "uncertain": 0.4,
        "trending_up": 0.8,
        "trending_down": 0.8,
    }

    # ── Configuration ─────────────────────────────────────────
    _RANGE_LOOKBACK = 20            # Bars to identify range
    _RANGE_MIN_BARS = 5             # Min bars price must be in range
    _RANGE_ATR_WIDTH_MAX = 4.0      # Max range width in ATR
    _BREAKOUT_VOL_RATIO = 1.3       # Volume on breakout bar
    _RETEST_TOLERANCE_ATR = 0.3     # How close to broken level for retest
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

        n = min(self._RANGE_LOOKBACK, len(df) - 1)
        if n < self._RANGE_MIN_BARS + 1:
            return None

        # Identify range from the lookback window (excluding last bar)
        range_high = high.iloc[-(n + 1):-1]
        range_low = low.iloc[-(n + 1):-1]
        range_close = close.iloc[-(n + 1):-1]

        resistance = range_high.max()
        support = range_low.min()
        range_width = resistance - support

        if range_width <= 0 or range_width > self._RANGE_ATR_WIDTH_MAX * atr:
            return None

        # Check that price was confined in range for min bars
        bars_in_range = ((range_close >= support) & (range_close <= resistance)).sum()
        if bars_in_range < self._RANGE_MIN_BARS:
            return None

        # Check for breakout on the last bar
        last_close = close.iloc[-1]
        last_vol = volume.iloc[-1]
        vol_avg = volume.iloc[-(n + 1):-1].mean()
        vol_ratio = last_vol / vol_avg if vol_avg > 0 else 0

        if vol_ratio < self._BREAKOUT_VOL_RATIO:
            return None

        if last_close > resistance:
            # Bullish breakout
            direction = Direction.LONG
            broken_level = resistance
            stop_loss = support - 0.5 * atr
            entry_low = broken_level - self._RETEST_TOLERANCE_ATR * atr
            entry_high = broken_level + 0.5 * atr
            risk = entry_low - stop_loss
            take_profit = entry_high + self._RR_TARGET * risk
        elif last_close < support:
            # Bearish breakout
            direction = Direction.SHORT
            broken_level = support
            stop_loss = resistance + 0.5 * atr
            entry_low = broken_level - 0.5 * atr
            entry_high = broken_level + self._RETEST_TOLERANCE_ATR * atr
            risk = stop_loss - entry_high
            take_profit = entry_low - self._RR_TARGET * risk
        else:
            return None  # No breakout

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
                f"Range break: S={support:.2f} R={resistance:.2f}, "
                f"breakout {direction.value} @ {last_close:.2f}, "
                f"vol={vol_ratio:.2f}×, range_width={range_width/atr:.1f}ATR"
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

        last_close = close.iloc[-1]
        last_open = open_.iloc[-1]
        last_high = high.iloc[-1]
        last_low = low.iloc[-1]

        atr = self._atr(df_trigger)
        if atr <= 0:
            atr = setup.atr_value

        # Check if price is retesting the entry zone (broken level)
        entry_mid = (setup.entry_zone_low + setup.entry_zone_high) / 2

        if setup.direction == Direction.LONG:
            # Price should dip into entry zone then close above
            if last_low > setup.entry_zone_high + 0.5 * atr:
                return None  # Hasn't retested
            if last_close < setup.entry_zone_low:
                return None  # Failed retest
            # Rejection candle: low touched zone but closed above
            if last_close <= last_open:
                return None  # Need bullish rejection
            entry_price = last_close
        else:
            if last_high < setup.entry_zone_low - 0.5 * atr:
                return None
            if last_close > setup.entry_zone_high:
                return None
            if last_close >= last_open:
                return None  # Need bearish rejection
            entry_price = last_close

        # Quality from rejection strength
        bar_range = last_high - last_low
        if bar_range <= 0:
            return None

        body = abs(last_close - last_open)
        if setup.direction == Direction.LONG:
            wick_ratio = (last_close - last_low) / bar_range  # Lower wick = rejection
        else:
            wick_ratio = (last_high - last_close) / bar_range

        quality = min(1.0, 0.3 + 0.4 * wick_ratio + 0.3 * (body / bar_range))
        strength = min(1.0, quality * self.get_regime_weight(regime_info.label))

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
                f"Retest confirmed: rejection wick={wick_ratio:.2f}, "
                f"entry={entry_price:.2f}, broken level ~{entry_mid:.2f}"
            ),
            max_age_ms=self.MAX_TRIGGER_AGE_MS,
            drift_tolerance=self.DRIFT_TOLERANCE,
        )
