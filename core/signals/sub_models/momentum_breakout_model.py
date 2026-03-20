# ============================================================
# NEXUS TRADER — Momentum Breakout Model (Sub-Model 3)
#
# Active in: volatility_expansion
# Long:  Price breaks above 20-bar high with volume > 1.5× avg
#        ATR expanding, RSI > 55
# Short: Price breaks below 20-bar low with volume > 1.5× avg
#        ATR expanding, RSI < 45
# Stop:   Below breakout level minus ATR
# Target: Breakout level + range height (measured move)
# ============================================================
from __future__ import annotations

import logging
from typing import Optional
import pandas as pd
import numpy as np

from config.settings import settings as _s
from core.signals.sub_models.base import BaseSubModel
from core.meta_decision.order_candidate import ModelSignal
from core.regime.regime_classifier import REGIME_VOL_EXPANSION

logger = logging.getLogger(__name__)


class MomentumBreakoutModel(BaseSubModel):
    ACTIVE_REGIMES = [REGIME_VOL_EXPANSION]

    REGIME_AFFINITY: dict[str, float] = {
        "bull_trend": 0.7, "bear_trend": 0.7, "ranging": 0.1,
        "volatility_expansion": 1.0, "volatility_compression": 0.1,
        "uncertain": 0.2, "crisis": 0.0, "liquidation_cascade": 0.0,
        "squeeze": 0.8, "recovery": 0.6, "accumulation": 0.3, "distribution": 0.4,
    }

    LOOKBACK = 20  # bars for range definition

    # Small breakout confirmation buffer: price has already moved past the range,
    # so a small additional buffer avoids entering right at the breakout level
    # where false-break failures are most likely.
    ENTRY_BUFFER_ATR: float = 0.10

    @property
    def name(self) -> str:
        return "momentum_breakout"

    def evaluate(
        self,
        symbol: str,
        df: pd.DataFrame,
        regime: str,
        timeframe: str,
    ) -> Optional[ModelSignal]:
        # Read tunable parameters from settings with fallback defaults
        _lookback = int(_s.get('models.momentum_breakout.lookback', 20))
        _vol_mult_min = float(_s.get('models.momentum_breakout.vol_mult_min', 1.5))
        _rsi_bullish = float(_s.get('models.momentum_breakout.rsi_bullish', 55))
        _rsi_bearish = float(_s.get('models.momentum_breakout.rsi_bearish', 45))
        _strength_base = float(_s.get('models.momentum_breakout.strength_base', 0.35))
        _entry_buffer = float(_s.get('models.momentum_breakout.entry_buffer_atr', 0.10))

        if len(df) < _lookback + 5:
            return None

        atr   = self._atr(df, 14)
        close = float(df["close"].iloc[-1])
        rsi   = self._col(df, "rsi_14")

        # Range of previous N bars (excluding current)
        prev       = df.iloc[-((_lookback) + 1):-1]
        range_high = float(prev["high"].max())
        range_low  = float(prev["low"].min())
        range_size = range_high - range_low

        # Volume confirmation
        vol_now  = float(df["volume"].iloc[-1])
        vol_avg  = float(df["volume"].iloc[-((_lookback)):-1].mean())
        vol_mult = vol_now / vol_avg if vol_avg > 0 else 0.0

        rationale_parts: list[str] = []
        strength = 0.0

        # ── Upside breakout ───────────────────────────────────
        if close > range_high and vol_mult >= _vol_mult_min and (rsi is None or rsi > _rsi_bullish):
            breakout_pct = (close - range_high) / range_high * 100
            rationale_parts.append(
                f"Broke above {_lookback}-bar high ({range_high:.4g}) "
                f"by {breakout_pct:.2f}% ✓"
            )
            rationale_parts.append(f"Volume {vol_mult:.1f}× avg ✓")
            if rsi:
                rationale_parts.append(f"RSI={rsi:.1f} > {_rsi_bullish} (momentum) ✓")

            vol_score     = min(1.0, (vol_mult - _vol_mult_min) / 2.0)
            breakout_score = min(1.0, breakout_pct / 2.0)
            strength      = _strength_base + vol_score * 0.35 + breakout_score * 0.3
            strength      = min(1.0, strength)

            entry_price = close + _entry_buffer * atr
            stop_loss   = range_high - atr          # below the breakout level
            take_profit = range_high + range_size   # measured move
            direction   = "long"

        # ── Downside breakdown ────────────────────────────────
        elif close < range_low and vol_mult >= _vol_mult_min and (rsi is None or rsi < _rsi_bearish):
            breakdown_pct = (range_low - close) / range_low * 100
            rationale_parts.append(
                f"Broke below {_lookback}-bar low ({range_low:.4g}) "
                f"by {breakdown_pct:.2f}% ✓"
            )
            rationale_parts.append(f"Volume {vol_mult:.1f}× avg ✓")
            if rsi:
                rationale_parts.append(f"RSI={rsi:.1f} < {_rsi_bearish} (bearish momentum) ✓")

            vol_score      = min(1.0, (vol_mult - _vol_mult_min) / 2.0)
            breakdown_score = min(1.0, breakdown_pct / 2.0)
            strength       = _strength_base + vol_score * 0.35 + breakdown_score * 0.3
            strength       = min(1.0, strength)

            entry_price = close - _entry_buffer * atr
            stop_loss   = range_low + atr
            take_profit = range_low - range_size
            direction   = "short"
        else:
            return None

        rationale = f"[Momentum Breakout | vol_expansion] " + " | ".join(rationale_parts)

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
