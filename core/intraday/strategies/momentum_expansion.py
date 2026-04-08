# ============================================================
# NEXUS TRADER — MX: Momentum Expansion Strategy  (Phase 4)
#
# Two-stage intraday momentum breakout strategy.
#
# Setup (15m): Identifies compression → expansion transitions
#   - Bollinger Band squeeze detected (BBW below threshold)
#   - ADX rising from low base (building momentum)
#   - Volume expanding above 20-bar average
#   - Directional bias from EMA alignment
#
# Trigger (1m): Precise breakout entry
#   - Price breaks beyond Bollinger Band
#   - Volume surge on breakout bar (≥1.5× recent avg)
#   - RSI confirms direction (>55 long, <45 short)
#   - Entry at breakout close, SL at opposite band, TP at 2R
#
# Regime affinity: strongest in volatility expansion, moderate
# in trending regimes, weak in ranging/compressed.
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


class MomentumExpansionStrategy(BaseIntradayStrategy):
    """MX — Momentum expansion breakout (compression → expansion)."""

    NAME = "momentum_expansion"
    STRATEGY_CLASS = StrategyClass.MOMENTUM_EXPANSION
    SETUP_TIMEFRAME = "15m"
    TRIGGER_TIMEFRAME = "1m"
    MAX_SETUP_AGE_MS = 45 * 60 * 1000      # 45 minutes
    MAX_TRIGGER_AGE_MS = 5 * 60 * 1000      # 5 minutes
    DRIFT_TOLERANCE = 0.003                  # 0.3%
    BASE_TIME_STOP_MS = 60 * 60 * 1000      # 1 hour

    REGIME_AFFINITY = {
        "bull_trend": 0.7,
        "bear_trend": 0.7,
        "high_volatility": 1.0,
        "low_volatility": 0.3,
        "ranging": 0.2,
        "uncertain": 0.3,
        "trending_up": 0.8,
        "trending_down": 0.8,
    }

    # ── Configuration ─────────────────────────────────────────
    _BB_PERIOD = 20
    _BB_STD = 2.0
    _BBW_SQUEEZE_THRESHOLD = 0.03       # BBW below this = squeeze
    _ADX_MIN = 15.0                      # ADX rising from low base
    _ADX_RISING_BARS = 3                 # ADX must be rising over N bars
    _VOL_EXPANSION_RATIO = 1.3          # Volume > 1.3× avg
    _TRIGGER_VOL_RATIO = 1.5            # Trigger bar volume surge
    _RSI_LONG_MIN = 55.0
    _RSI_SHORT_MAX = 45.0
    _RR_TARGET = 2.0                    # Risk:reward target

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

        # Bollinger Bands
        bb_mid = close.rolling(self._BB_PERIOD).mean()
        bb_std = close.rolling(self._BB_PERIOD).std()
        bb_upper = bb_mid + self._BB_STD * bb_std
        bb_lower = bb_mid - self._BB_STD * bb_std
        bbw = (bb_upper - bb_lower) / bb_mid.replace(0, np.nan)

        last_bbw = bbw.iloc[-1]
        if pd.isna(last_bbw):
            return None

        # Check for recent squeeze (within last 5 bars)
        recent_bbw = bbw.iloc[-5:]
        had_squeeze = (recent_bbw < self._BBW_SQUEEZE_THRESHOLD).any()
        if not had_squeeze:
            return None

        # ADX check
        atr = self._atr(df)
        if atr <= 0:
            return None

        # Compute ADX manually if not in columns
        adx_val = self._col(df, "adx_14")
        if adx_val is None:
            adx_val = self._compute_adx(df)
        if adx_val is None or adx_val < self._ADX_MIN:
            return None

        # Volume expansion
        vol_avg = volume.rolling(20).mean().iloc[-1]
        if pd.isna(vol_avg) or vol_avg <= 0:
            return None
        vol_ratio = volume.iloc[-1] / vol_avg
        if vol_ratio < self._VOL_EXPANSION_RATIO:
            return None

        # Direction from EMA alignment
        ema_9 = self._ema(close, 9).iloc[-1]
        ema_21 = self._ema(close, 21).iloc[-1]
        last_close = close.iloc[-1]
        last_bb_upper = bb_upper.iloc[-1]
        last_bb_lower = bb_lower.iloc[-1]

        if ema_9 > ema_21 and last_close > bb_mid.iloc[-1]:
            direction = Direction.LONG
            stop_loss = last_bb_lower - 0.5 * atr
            entry_low = last_close
            entry_high = last_bb_upper + 0.1 * atr
            risk = entry_low - stop_loss
            take_profit = entry_high + self._RR_TARGET * risk
        elif ema_9 < ema_21 and last_close < bb_mid.iloc[-1]:
            direction = Direction.SHORT
            stop_loss = last_bb_upper + 0.5 * atr
            entry_low = last_bb_lower - 0.1 * atr
            entry_high = last_close
            risk = stop_loss - entry_high
            take_profit = entry_low - self._RR_TARGET * risk
        else:
            return None  # No clear directional bias

        # Ensure prices are valid
        if stop_loss <= 0 or take_profit <= 0 or entry_low <= 0 or entry_high <= 0:
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
                f"BB squeeze detected (BBW={last_bbw:.4f}), ADX={adx_val:.1f} rising, "
                f"vol ratio={vol_ratio:.2f}×, EMA bias={direction.value}"
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
        volume = df_trigger["volume"]
        last_close = close.iloc[-1]
        last_vol = volume.iloc[-1]

        # Volume surge on trigger bar
        vol_avg = volume.rolling(20).mean().iloc[-1]
        if pd.isna(vol_avg) or vol_avg <= 0:
            return None
        vol_ratio = last_vol / vol_avg
        if vol_ratio < self._TRIGGER_VOL_RATIO:
            return None

        # RSI confirmation
        rsi = self._rsi(close).iloc[-1]
        if pd.isna(rsi):
            return None

        # Price in entry zone + directional confirmation
        if setup.direction == Direction.LONG:
            if last_close < setup.entry_zone_low:
                return None
            if rsi < self._RSI_LONG_MIN:
                return None
            entry_price = last_close
        else:
            if last_close > setup.entry_zone_high:
                return None
            if rsi > self._RSI_SHORT_MAX:
                return None
            entry_price = last_close

        # Compute trigger quality from volume and RSI strength
        rsi_strength = (rsi - 50) / 50 if setup.direction == Direction.LONG else (50 - rsi) / 50
        quality = min(1.0, 0.3 + 0.4 * min(vol_ratio / 3.0, 1.0) + 0.3 * max(rsi_strength, 0))
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
                f"Breakout confirmed: vol surge {vol_ratio:.2f}×, RSI={rsi:.1f}, "
                f"entry={entry_price:.2f}"
            ),
            max_age_ms=self.MAX_TRIGGER_AGE_MS,
            drift_tolerance=self.DRIFT_TOLERANCE,
        )

    @staticmethod
    def _compute_adx(df: pd.DataFrame, period: int = 14) -> Optional[float]:
        """Compute ADX from OHLC data."""
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
