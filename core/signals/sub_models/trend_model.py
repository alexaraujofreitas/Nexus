# ============================================================
# NEXUS TRADER — Trend Model (Sub-Model 1)
#
# Active in: bull_trend, bear_trend
# Logic:
#   Long:  EMA9 > EMA21, EMA20 > EMA100, ADX14 > 25,
#          RSI14 in [45, 70], MACD above Signal Line
#   Short: EMA9 < EMA21, EMA20 < EMA100, ADX14 > 25,
#          RSI14 in [30, 55], MACD below Signal Line
#
# Stop:   ATR14 * 1.5 against entry
# Target: ATR14 * 2.5 in direction of entry
# ============================================================
from __future__ import annotations

import logging
from typing import Optional
import pandas as pd

from config.settings import settings as _s
from core.signals.sub_models.base import BaseSubModel
from core.meta_decision.order_candidate import ModelSignal
from core.regime.regime_classifier import REGIME_BULL_TREND, REGIME_BEAR_TREND

logger = logging.getLogger(__name__)


class TrendModel(BaseSubModel):
    """Trend-following model active in directional regimes."""

    ACTIVE_REGIMES = [REGIME_BULL_TREND, REGIME_BEAR_TREND]

    # Trend model chases the move: entry is set slightly beyond close to confirm
    # momentum continuation rather than picking a top/bottom.
    ENTRY_BUFFER_ATR: float = 0.20  # long: close + 0.20*ATR; short: close - 0.20*ATR

    # High affinity in trending regimes; near-zero in crash/ranging environments
    REGIME_AFFINITY: dict[str, float] = {
        "bull_trend": 1.0, "bear_trend": 0.9, "ranging": 0.1,
        "volatility_expansion": 0.6, "volatility_compression": 0.2,
        "uncertain": 0.3, "crisis": 0.0, "liquidation_cascade": 0.0,
        "squeeze": 0.3, "recovery": 0.7, "accumulation": 0.2, "distribution": 0.2,
    }

    @property
    def name(self) -> str:
        return "trend"

    def evaluate(
        self,
        symbol: str,
        df: pd.DataFrame,
        regime: str,
        timeframe: str,
    ) -> Optional[ModelSignal]:
        if len(df) < 50:
            return None

        # Read tunable parameters from settings with fallback defaults
        _adx_min = float(_s.get('models.trend.adx_min', 25.0))
        _rsi_long_min = float(_s.get('models.trend.rsi_long_min', 45))
        _rsi_long_max = float(_s.get('models.trend.rsi_long_max', 70))
        _rsi_short_min = float(_s.get('models.trend.rsi_short_min', 30))
        _rsi_short_max = float(_s.get('models.trend.rsi_short_max', 55))
        _strength_base = float(_s.get('models.trend.strength_base', 0.15))
        _ema20_bonus = float(_s.get('models.trend.ema20_bonus', 0.25))
        _macd_bonus = float(_s.get('models.trend.macd_bonus', 0.20))
        _adx_bonus_max = float(_s.get('models.trend.adx_bonus_max', 0.40))
        _entry_buffer = float(_s.get('models.trend.entry_buffer_atr', 0.20))

        atr    = self._atr(df, 14)
        atr_mult = self.get_atr_multiplier(regime)
        close  = float(df["close"].iloc[-1])
        ema9   = self._col(df, "ema_9")
        ema21  = self._col(df, "ema_21")
        ema20  = self._col(df, "ema_20")
        ema100 = self._col(df, "ema_100")
        adx    = self._col(df, "adx")
        rsi    = self._col(df, "rsi_14")
        macd   = self._col(df, "macd")
        signal = self._col(df, "macd_signal")

        # Require essential indicators
        if any(v is None for v in [ema9, ema21, adx, rsi]):
            return None
        if adx < _adx_min:
            return None  # Only trade in confirmed trend

        rationale_parts: list[str] = []
        strength = 0.0

        if regime == REGIME_BULL_TREND:
            # ── Long conditions ───────────────────────────────
            if not (ema9 > ema21):
                return None
            rationale_parts.append(f"EMA9({ema9:.2f}) > EMA21({ema21:.2f}) ✓")

            if ema20 and ema100 and ema20 > ema100:
                rationale_parts.append(f"EMA20 > EMA100 (multi-TF trend confirmed) ✓")
                strength += _ema20_bonus
            else:
                rationale_parts.append("EMA20/EMA100 not aligned (single-TF signal)")

            if not (_rsi_long_min <= rsi <= _rsi_long_max):
                return None  # Not in momentum zone
            rationale_parts.append(f"RSI14={rsi:.1f} (momentum zone {_rsi_long_min}–{_rsi_long_max}) ✓")

            if macd is not None and signal is not None and macd > signal:
                rationale_parts.append(f"MACD above Signal Line ✓")
                strength += _macd_bonus
            else:
                rationale_parts.append("MACD not confirming (signal absent or below)")

            # ADX strength bonus
            adx_bonus = min(_adx_bonus_max, (adx - _adx_min) / _adx_min * _adx_bonus_max)
            strength += adx_bonus + _strength_base  # base strength for passing EMA check
            rationale_parts.append(f"ADX={adx:.1f} (trend strength)")

            strength = min(1.0, strength)

            entry_price = close + _entry_buffer * atr
            stop_loss   = entry_price - atr * atr_mult
            take_profit = entry_price + atr * (atr_mult + 1.0)
            direction   = "long"

        elif regime == REGIME_BEAR_TREND:
            # ── Short conditions ──────────────────────────────
            if not (ema9 < ema21):
                return None
            rationale_parts.append(f"EMA9({ema9:.2f}) < EMA21({ema21:.2f}) ✓")

            if ema20 and ema100 and ema20 < ema100:
                rationale_parts.append("EMA20 < EMA100 (multi-TF bear confirmed) ✓")
                strength += _ema20_bonus

            if not (_rsi_short_min <= rsi <= _rsi_short_max):
                return None
            rationale_parts.append(f"RSI14={rsi:.1f} (bear momentum zone {_rsi_short_min}–{_rsi_short_max}) ✓")

            if macd is not None and signal is not None and macd < signal:
                rationale_parts.append("MACD below Signal Line ✓")
                strength += _macd_bonus

            adx_bonus = min(_adx_bonus_max, (adx - _adx_min) / _adx_min * _adx_bonus_max)
            strength += adx_bonus + _strength_base
            strength = min(1.0, strength)

            entry_price = close - _entry_buffer * atr
            stop_loss   = entry_price + atr * atr_mult
            take_profit = entry_price - atr * (atr_mult + 1.0)
            direction   = "short"

        elif regime == "uncertain":
            # ── Uncertain regime: fire whichever direction has the stronger case ──
            # Only fire when clear momentum exists (ADX already > _adx_min above).
            # Prefer long if EMA9>EMA21 and RSI in bull zone; prefer short otherwise.
            if ema9 > ema21 and (_rsi_long_min <= rsi <= _rsi_long_max):
                rationale_parts.append(f"EMA9({ema9:.2f}) > EMA21({ema21:.2f}) [uncertain-long] ✓")
                rationale_parts.append(f"RSI14={rsi:.1f} ({_rsi_long_min}–{_rsi_long_max} zone) ✓")
                if macd is not None and signal is not None and macd > signal:
                    rationale_parts.append("MACD above Signal Line ✓")
                    strength += _macd_bonus
                if ema20 and ema100 and ema20 > ema100:
                    strength += _ema20_bonus
                adx_bonus = min(_adx_bonus_max, (adx - _adx_min) / _adx_min * _adx_bonus_max)
                strength += adx_bonus + _strength_base * 0.667  # slightly lower base for uncertain
                strength = min(1.0, strength)
                entry_price = close + _entry_buffer * atr
                stop_loss   = entry_price - atr * atr_mult
                take_profit = entry_price + atr * (atr_mult + 1.0)
                direction   = "long"
            elif ema9 < ema21 and (_rsi_short_min <= rsi <= _rsi_short_max):
                rationale_parts.append(f"EMA9({ema9:.2f}) < EMA21({ema21:.2f}) [uncertain-short] ✓")
                rationale_parts.append(f"RSI14={rsi:.1f} ({_rsi_short_min}–{_rsi_short_max} zone) ✓")
                if macd is not None and signal is not None and macd < signal:
                    rationale_parts.append("MACD below Signal Line ✓")
                    strength += _macd_bonus
                if ema20 and ema100 and ema20 < ema100:
                    strength += _ema20_bonus
                adx_bonus = min(_adx_bonus_max, (adx - _adx_min) / _adx_min * _adx_bonus_max)
                strength += adx_bonus + _strength_base * 0.667
                strength = min(1.0, strength)
                entry_price = close - _entry_buffer * atr
                stop_loss   = entry_price + atr * atr_mult
                take_profit = entry_price - atr * (atr_mult + 1.0)
                direction   = "short"
            else:
                return None
        else:
            return None

        if regime == "uncertain":
            rationale_parts.append(f"ADX={adx:.1f} (trend strength)")
        rationale = f"[Trend Model | {regime}] " + " | ".join(rationale_parts)

        return ModelSignal(
            symbol      = symbol,
            model_name  = self.name,
            direction   = direction,
            strength    = round(strength, 4),
            entry_price = round(entry_price, 8),
            stop_loss   = round(stop_loss, 8),
            take_profit = round(take_profit, 8),
            timeframe   = timeframe,
            regime      = regime,
            rationale   = rationale,
            atr_value   = atr,
        )
