# ============================================================
# NEXUS TRADER — Donchian Breakout Model (Sub-Model — Replacement Research)
#
# Thesis: price breaking the N-period Donchian channel high/low
#         on elevated volume signals a high-probability continuation.
#
# Long:  close > N-period high of preceding bars
#        volume > vol_mult_min × lookback average
# Short: close < N-period low of preceding bars
#        volume > vol_mult_min × lookback average
#
# ATR-based SL (1.5× below breakout level for long, above for short)
# ATR-based TP (multiplied by tp_atr_mult from config)
#
# ACTIVE_REGIMES = [] — fires in all regimes; REGIME_AFFINITY
# suppresses it in crisis / liquidation_cascade.
# ============================================================
from __future__ import annotations

import logging
from typing import Optional
import pandas as pd

from config.settings import settings as _s
from core.signals.sub_models.base import BaseSubModel
from core.meta_decision.order_candidate import ModelSignal

logger = logging.getLogger(__name__)


class DonchianBreakoutModel(BaseSubModel):
    """
    Donchian-channel breakout model.

    A clean directional-breakout model intended as a replacement candidate
    for TrendModel (which was net-negative at 0.04 %/side fees).

    The Donchian channel is computed over the *preceding* ``lookback`` bars
    (bar i−lookback … bar i−1), so the current bar is never included in the
    channel — this avoids look-ahead bias.

    SL is placed 1.5 ATR from the channel boundary (the breakout level),
    TP is placed ``tp_atr_mult`` ATR from entry.
    """

    # Fire in all regimes; rely on REGIME_AFFINITY to weight correctly
    ACTIVE_REGIMES: list[str] = []

    REGIME_AFFINITY: dict[str, float] = {
        "bull_trend":            0.85,
        "bear_trend":            0.85,
        "ranging":               0.15,   # false-break risk in ranging markets
        "volatility_expansion":  0.90,   # ideal environment
        "volatility_compression": 0.10,  # avoid — low follow-through
        "uncertain":             0.25,
        "crisis":                0.0,
        "liquidation_cascade":   0.0,
        "squeeze":               0.75,
        "recovery":              0.65,
        "accumulation":          0.35,
        "distribution":          0.50,
    }

    # Confirm breakout by a small buffer so we don't chase false breaks
    ENTRY_BUFFER_ATR: float = 0.10

    @property
    def name(self) -> str:
        return "donchian_breakout"

    def evaluate(
        self,
        symbol: str,
        df: pd.DataFrame,
        regime: str,
        timeframe: str,
    ) -> Optional[ModelSignal]:
        # ── Config ──────────────────────────────────────────────────
        _lookback      = int(_s.get("models.donchian_breakout.lookback",      20))
        _vol_mult_min  = float(_s.get("models.donchian_breakout.vol_mult_min", 1.3))
        _sl_atr_mult   = float(_s.get("models.donchian_breakout.sl_atr_mult",  1.5))
        _tp_atr_mult   = float(_s.get("models.donchian_breakout.tp_atr_mult",  3.0))
        _rsi_long_min  = float(_s.get("models.donchian_breakout.rsi_long_min", 50.0))
        _rsi_short_max = float(_s.get("models.donchian_breakout.rsi_short_max", 50.0))
        _strength_base = float(_s.get("models.donchian_breakout.strength_base", 0.35))
        _entry_buffer  = float(_s.get("models.donchian_breakout.entry_buffer_atr", self.ENTRY_BUFFER_ATR))

        if len(df) < _lookback + 5:
            return None

        atr   = self._atr(df, 14)
        close = float(df["close"].iloc[-1])
        rsi   = self._col(df, "rsi_14")

        # Donchian channel of the PRECEDING lookback bars (exclude current bar)
        prev = df.iloc[-(_lookback + 1):-1]
        chan_high = float(prev["high"].max())
        chan_low  = float(prev["low"].min())
        chan_size = chan_high - chan_low

        if chan_size <= 0:
            return None

        # Volume confirmation
        vol_now = float(df["volume"].iloc[-1])
        vol_avg = float(df["volume"].iloc[-_lookback:-1].mean()) if _lookback > 1 else vol_now
        vol_mult = vol_now / vol_avg if vol_avg > 0 else 0.0

        rationale_parts: list[str] = []
        strength = 0.0

        # ── Long: close broke above the Donchian high ──────────────
        if (
            close > chan_high
            and vol_mult >= _vol_mult_min
            and (rsi is None or rsi >= _rsi_long_min)
        ):
            breakout_pct = (close - chan_high) / chan_high * 100
            rationale_parts.append(
                f"Broke above {_lookback}-bar Donchian high ({chan_high:.4g}) "
                f"by {breakout_pct:.2f}% ✓"
            )
            rationale_parts.append(f"Volume {vol_mult:.1f}× avg ✓")
            if rsi is not None:
                rationale_parts.append(f"RSI={rsi:.1f} ≥ {_rsi_long_min} ✓")

            vol_score     = min(1.0, (vol_mult - _vol_mult_min) / 2.0)
            breakout_score = min(1.0, breakout_pct / 2.0)
            strength      = _strength_base + vol_score * 0.35 + breakout_score * 0.30
            strength      = min(1.0, strength)

            entry_price = close + _entry_buffer * atr
            stop_loss   = chan_high - _sl_atr_mult * atr   # below the channel boundary
            take_profit = entry_price + _tp_atr_mult * atr
            direction   = "long"

        # ── Short: close broke below the Donchian low ──────────────
        elif (
            close < chan_low
            and vol_mult >= _vol_mult_min
            and (rsi is None or rsi <= _rsi_short_max)
        ):
            breakdown_pct = (chan_low - close) / chan_low * 100
            rationale_parts.append(
                f"Broke below {_lookback}-bar Donchian low ({chan_low:.4g}) "
                f"by {breakdown_pct:.2f}% ✓"
            )
            rationale_parts.append(f"Volume {vol_mult:.1f}× avg ✓")
            if rsi is not None:
                rationale_parts.append(f"RSI={rsi:.1f} ≤ {_rsi_short_max} ✓")

            vol_score      = min(1.0, (vol_mult - _vol_mult_min) / 2.0)
            breakdown_score = min(1.0, breakdown_pct / 2.0)
            strength       = _strength_base + vol_score * 0.35 + breakdown_score * 0.30
            strength       = min(1.0, strength)

            entry_price = close - _entry_buffer * atr
            stop_loss   = chan_low + _sl_atr_mult * atr   # above the channel boundary
            take_profit = entry_price - _tp_atr_mult * atr
            direction   = "short"

        else:
            return None

        rationale = "[Donchian Breakout] " + " | ".join(rationale_parts)

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
