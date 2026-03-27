# ============================================================
# NEXUS TRADER — Mean Reversion Model (Sub-Model 2)
#
# ⚠️  ARCHIVED — v1.2  (2026-03-26)
# Reason : Study 4 backtest — PF 0.21 (-$18k), WR 32.2% over 13 months.
#           Disabled in production via config.yaml `disabled_models` list.
# Status : Code retained for test compatibility and historical analysis only.
#           DO NOT re-enable without OOS validation on ≥75 live demo trades.
#           Re-enable path: remove "mean_reversion" from disabled_models in
#           config.yaml after out-of-sample confirmation.
#
# Active in: ranging
# Long:  Price near BB lower, RSI14 < 35, StochRSI %K < 25
# Short: Price near BB upper, RSI14 > 65, StochRSI %K > 75
# Stop:   ATR14 * 1.0 (tight — mean reversion fails fast)
# Target: BB mid (the mean)
# ============================================================
from __future__ import annotations

import logging
from typing import Optional
import pandas as pd

from config.settings import settings as _s
from core.signals.sub_models.base import BaseSubModel
from core.meta_decision.order_candidate import ModelSignal
from core.regime.regime_classifier import REGIME_RANGING

logger = logging.getLogger(__name__)


class MeanReversionModel(BaseSubModel):
    ACTIVE_REGIMES = [REGIME_RANGING]

    # Mean-reversion model waits for a slightly better fill.
    # Negative buffer: long entry = close - 0.15*ATR (limit order below price).
    # Short entry = close + 0.15*ATR (wait for slight continuation then reverse).
    ENTRY_BUFFER_ATR: float = -0.15

    # Stop multipliers scale with regime volatility so that we avoid premature
    # stop-outs in regimes with higher noise levels.  In the primary ranging
    # regime (1.2×) the stop is tighter than in compression/accumulation where
    # false moves are larger.
    REGIME_ATR_MULTIPLIERS: dict[str, float] = {
        "ranging":               1.5,
        "volatility_compression": 1.875,
        "accumulation":          1.625,
        "distribution":          1.625,
        "bull_trend":            1.875,  # counter-trend in trend → wider stop
        "bear_trend":            1.875,
        "uncertain":             1.625,
        "recovery":              1.75,
        "squeeze":               2.25,   # squeezes break violently
        "crisis":                0.0,    # model excluded from crisis via REGIME_AFFINITY
        "liquidation_cascade":   0.0,
        "volatility_expansion":  2.25,
    }

    REGIME_AFFINITY: dict[str, float] = {
        "bull_trend": 0.05, "bear_trend": 0.08, "ranging": 1.0,
        "volatility_expansion": 0.02, "volatility_compression": 0.8,
        "uncertain": 0.20, "crisis": 0.0, "liquidation_cascade": 0.0,
        "squeeze": 0.4, "recovery": 0.4, "accumulation": 0.8, "distribution": 0.7,
    }

    @property
    def name(self) -> str:
        return "mean_reversion"

    def evaluate(
        self,
        symbol: str,
        df: pd.DataFrame,
        regime: str,
        timeframe: str,
    ) -> Optional[ModelSignal]:
        # Read tunable parameters from settings with fallback defaults
        _bb_lower_dist = float(_s.get('models.mean_reversion.bb_lower_dist', 0.15))
        _rsi_oversold = float(_s.get('models.mean_reversion.rsi_oversold', 35))
        _rsi_overbought = float(_s.get('models.mean_reversion.rsi_overbought', 65))
        _stoch_rsi_oversold = float(_s.get('models.mean_reversion.stoch_rsi_oversold', 25))
        _stoch_rsi_overbought = float(_s.get('models.mean_reversion.stoch_rsi_overbought', 75))
        _entry_buffer = float(_s.get('models.mean_reversion.entry_buffer_atr', -0.15))

        if len(df) < 30:
            return None

        atr     = self._atr(df, 14)
        close   = float(df["close"].iloc[-1])
        bb_upper = self._col(df, "bb_upper")
        bb_lower = self._col(df, "bb_lower")
        bb_mid   = self._col(df, "bb_mid")
        rsi      = self._col(df, "rsi_14")
        stoch_k  = self._col(df, "stoch_rsi_k")

        if any(v is None for v in [bb_upper, bb_lower, bb_mid, rsi]):
            return None

        band_range = bb_upper - bb_lower
        if band_range <= 0:
            return None

        # Distance from bands as percentage of band range
        dist_from_lower = (close - bb_lower) / band_range  # 0 = at lower, 1 = at upper
        dist_from_upper = (bb_upper - close) / band_range

        rationale_parts: list[str] = []
        strength = 0.0

        # ── Long: price near lower band ───────────────────────
        if dist_from_lower <= _bb_lower_dist and rsi < _rsi_oversold:
            rationale_parts.append(
                f"Price near BB_lower (dist={dist_from_lower:.2%}) ✓"
            )
            rationale_parts.append(f"RSI14={rsi:.1f} (oversold < {_rsi_oversold}) ✓")

            # RSI strength: deeper oversold = stronger
            rsi_score = max(0.0, (_rsi_oversold - rsi) / _rsi_oversold)
            band_score = max(0.0, (_bb_lower_dist - dist_from_lower) / _bb_lower_dist)
            strength = 0.3 + rsi_score * 0.4 + band_score * 0.3

            if stoch_k is not None and stoch_k < _stoch_rsi_oversold:
                rationale_parts.append(f"StochRSI %K={stoch_k:.1f} (oversold) ✓")
                strength = min(1.0, strength + 0.15)

            entry_price = close + _entry_buffer * atr
            # Stop multiplier scales with regime volatility to avoid premature
            # stop-outs from noise in non-ranging regimes.
            atr_mult    = self.get_atr_multiplier(regime)
            stop_loss   = entry_price - atr * atr_mult
            take_profit = bb_mid                     # target = BB midline (the mean)
            direction   = "long"

        # ── Short: price near upper band ──────────────────────
        elif dist_from_upper <= _bb_lower_dist and rsi > _rsi_overbought:
            rationale_parts.append(
                f"Price near BB_upper (dist={dist_from_upper:.2%}) ✓"
            )
            rationale_parts.append(f"RSI14={rsi:.1f} (overbought > {_rsi_overbought}) ✓")

            rsi_score  = max(0.0, (rsi - _rsi_overbought) / (100 - _rsi_overbought))
            band_score = max(0.0, (_bb_lower_dist - dist_from_upper) / _bb_lower_dist)
            strength   = 0.3 + rsi_score * 0.4 + band_score * 0.3

            if stoch_k is not None and stoch_k > _stoch_rsi_overbought:
                rationale_parts.append(f"StochRSI %K={stoch_k:.1f} (overbought) ✓")
                strength = min(1.0, strength + 0.15)

            entry_price = close - _entry_buffer * atr
            atr_mult    = self.get_atr_multiplier(regime)
            stop_loss   = entry_price + atr * atr_mult
            take_profit = bb_mid
            direction   = "short"
        else:
            return None  # No mean-reversion opportunity

        strength = min(1.0, strength)
        rationale = f"[Mean Reversion | ranging] " + " | ".join(rationale_parts)

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
