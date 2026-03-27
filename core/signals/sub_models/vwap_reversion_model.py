# ============================================================
# NEXUS TRADER — VWAP Mean-Reversion Model (Phase 1c)
#
# ⚠️  ARCHIVED — v1.2  (2026-03-26)
# Reason : Study 4 backtest — PF 0.28, below 1.0 viability threshold.
#           Added to disabled_models in config.yaml on 2026-03-24.
# Status : Code retained for test compatibility and historical analysis only.
#           DO NOT re-enable without OOS validation on ≥75 live demo trades.
#           Re-enable path: remove "vwap_reversion" from disabled_models in
#           config.yaml after out-of-sample confirmation.
#
# Active in: ranging, accumulation, volatility_compression, recovery
# Trades mean reversion around VWAP with z-score and RSI confirmation.
#
# Long:  price 1.5σ below VWAP, oversold RSI, early recovery
# Short: price 1.5σ above VWAP, overbought RSI, early reversal
# ============================================================
from __future__ import annotations

import logging
from typing import Optional
import pandas as pd
import numpy as np

from config.settings import settings as _s
from core.signals.sub_models.base import BaseSubModel
from core.meta_decision.order_candidate import ModelSignal
from core.regime.regime_classifier import (
    REGIME_RANGING,
    REGIME_ACCUMULATION,
    REGIME_VOL_COMPRESS,
    REGIME_RECOVERY,
)

logger = logging.getLogger(__name__)

ACTIVE_REGIMES = [REGIME_RANGING, REGIME_ACCUMULATION, REGIME_VOL_COMPRESS, REGIME_RECOVERY]


class VWAPReversionModel(BaseSubModel):
    """
    VWAP mean-reversion sub-model.
    Trades pullbacks/rallies toward Volume-Weighted Average Price.
    """

    ACTIVE_REGIMES = ACTIVE_REGIMES

    # Mean-reversion against VWAP: wait for a slightly better fill,
    # same logic as MeanReversionModel.
    ENTRY_BUFFER_ATR: float = -0.10

    REGIME_AFFINITY: dict[str, float] = {
        "bull_trend": 0.5, "bear_trend": 0.5, "ranging": 0.8,
        "volatility_expansion": 0.3, "volatility_compression": 0.7,
        "uncertain": 0.5, "crisis": 0.1, "liquidation_cascade": 0.1,
        "squeeze": 0.4, "recovery": 0.5, "accumulation": 0.7, "distribution": 0.6,
    }

    @property
    def name(self) -> str:
        return "vwap_reversion"

    def evaluate(
        self,
        symbol: str,
        df: pd.DataFrame,
        regime: str,
        timeframe: str,
    ) -> Optional[ModelSignal]:
        """
        Evaluate VWAP reversion signals.

        Requires: close, vwap, bb_upper, bb_lower, rsi_14, atr_14
        """
        # Read tunable parameters from settings with fallback defaults
        _z_threshold = float(_s.get('models.vwap_reversion.z_threshold', 1.5))
        _rsi_oversold = float(_s.get('models.vwap_reversion.rsi_oversold', 42))
        _rsi_overbought = float(_s.get('models.vwap_reversion.rsi_overbought', 58))
        _deviation_window = int(_s.get('models.vwap_reversion.deviation_window', 20))
        _sl_atr_mult = float(_s.get('models.vwap_reversion.sl_atr_mult', 1.2))
        _tp_atr_offset = float(_s.get('models.vwap_reversion.tp_atr_offset', 0.5))
        _entry_buffer = float(_s.get('models.vwap_reversion.entry_buffer_atr', -0.10))

        if len(df) < 30:
            return None

        # Extract required columns
        close = self._col(df, "close")
        vwap = self._col(df, "vwap")
        bb_upper = self._col(df, "bb_upper")
        bb_lower = self._col(df, "bb_lower")
        rsi = self._col(df, "rsi_14")
        atr = self._atr(df, 14)

        if any(v is None for v in [close, vwap, bb_upper, bb_lower, rsi]):
            return None

        # Compute VWAP deviation as % of VWAP
        try:
            vwap_deviation = (close - vwap) / vwap * 100.0
        except (ZeroDivisionError, TypeError):
            return None

        # Compute rolling std of VWAP deviation over last _deviation_window bars
        if len(df) < _deviation_window:
            return None

        deviations = []
        for i in range(max(0, len(df) - _deviation_window), len(df)):
            try:
                c = df["close"].iloc[i]
                v = df["vwap"].iloc[i]
                if pd.notna(c) and pd.notna(v) and v > 0:
                    dev = (c - v) / v * 100.0
                    deviations.append(dev)
            except Exception:
                continue

        if len(deviations) < 3:
            return None

        std_dev = float(np.std(deviations))
        if std_dev <= 0:
            return None

        # Z-score of current deviation
        deviation_z = vwap_deviation / std_dev

        # Get close from 5 bars ago for momentum confirmation
        close_5_bars_ago = None
        if len(df) >= 5:
            try:
                c5 = df["close"].iloc[-5]
                if pd.notna(c5):
                    close_5_bars_ago = float(c5)
            except Exception:
                pass

        rationale_parts: list[str] = []
        strength = 0.0
        direction = None

        # ── Long: mean reversion from below ───────────────────────
        if deviation_z < -_z_threshold and (close < bb_lower or close < vwap * 0.985):
            if rsi < _rsi_oversold:
                # Check if price is starting to recover (not free-falling)
                recovering = True
                if close_5_bars_ago is not None and close <= close_5_bars_ago:
                    recovering = False

                if recovering:
                    rationale_parts.append(
                        f"Deviation z={deviation_z:.2f} (< -{_z_threshold}σ) ✓"
                    )
                    rationale_parts.append(
                        f"Price < VWAP (dev={vwap_deviation:.2f}%) ✓"
                    )
                    rationale_parts.append(f"RSI14={rsi:.1f} (oversold < {_rsi_oversold}) ✓")
                    rationale_parts.append("Recovery signal starting ✓")

                    # Base strength
                    strength = 0.50

                    # Bonus for extreme deviation
                    if abs(deviation_z) > 2.0:
                        rationale_parts.append(f"Very deep z={abs(deviation_z):.2f} (+0.15)")
                        strength += 0.15
                    if abs(deviation_z) > 2.5:
                        rationale_parts.append(f"Extreme z={abs(deviation_z):.2f} (+0.10)")
                        strength += 0.10

                    # Bonus for strong RSI alignment
                    if rsi < _rsi_oversold - 7:
                        rationale_parts.append(f"Strong oversold (RSI < {_rsi_oversold - 7}) (+0.10)")
                        strength += 0.10

                    # Bonus for recovery confirmation
                    if close_5_bars_ago is not None and close > close_5_bars_ago:
                        rationale_parts.append("Price bouncing (+0.05)")
                        strength += 0.05

                    strength = min(1.0, strength)
                    entry_price = close + _entry_buffer * atr
                    stop_loss = min(entry_price - atr * _sl_atr_mult, vwap - atr * 2.0)
                    take_profit = vwap + atr * _tp_atr_offset
                    direction = "long"

        # ── Short: mean reversion from above ──────────────────────
        if (
            direction is None
            and deviation_z > _z_threshold
            and (close > bb_upper or close > vwap * 1.015)
        ):
            if rsi > _rsi_overbought:
                # Check if price is starting to reverse (not still rallying)
                reversing = True
                if close_5_bars_ago is not None and close >= close_5_bars_ago:
                    reversing = False

                if reversing:
                    rationale_parts.append(
                        f"Deviation z={deviation_z:.2f} (> {_z_threshold}σ) ✓"
                    )
                    rationale_parts.append(
                        f"Price > VWAP (dev={vwap_deviation:.2f}%) ✓"
                    )
                    rationale_parts.append(f"RSI14={rsi:.1f} (overbought > {_rsi_overbought}) ✓")
                    rationale_parts.append("Reversal signal starting ✓")

                    # Base strength
                    strength = 0.50

                    # Bonus for extreme deviation
                    if abs(deviation_z) > 2.0:
                        rationale_parts.append(f"Very deep z={abs(deviation_z):.2f} (+0.15)")
                        strength += 0.15
                    if abs(deviation_z) > 2.5:
                        rationale_parts.append(f"Extreme z={abs(deviation_z):.2f} (+0.10)")
                        strength += 0.10

                    # Bonus for strong RSI alignment
                    if rsi > _rsi_overbought + 7:
                        rationale_parts.append(f"Strong overbought (RSI > {_rsi_overbought + 7}) (+0.10)")
                        strength += 0.10

                    # Bonus for reversal confirmation
                    if close_5_bars_ago is not None and close < close_5_bars_ago:
                        rationale_parts.append("Price falling (+0.05)")
                        strength += 0.05

                    strength = min(1.0, strength)
                    entry_price = close - _entry_buffer * atr
                    stop_loss = max(entry_price + atr * _sl_atr_mult, vwap + atr * 2.0)
                    take_profit = vwap - atr * _tp_atr_offset
                    direction = "short"

        if direction is None:
            return None

        strength = min(1.0, max(0.0, strength))
        rationale = f"[VWAP Reversion | {regime}] " + " | ".join(rationale_parts)

        return ModelSignal(
            symbol=symbol,
            model_name=self.name,
            direction=direction,
            strength=round(strength, 4),
            entry_price=round(entry_price, 8),
            stop_loss=round(stop_loss, 8),
            take_profit=round(take_profit, 8),
            timeframe=timeframe,
            regime=regime,
            rationale=rationale,
            atr_value=atr,
        )
