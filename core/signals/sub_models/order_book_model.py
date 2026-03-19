# ============================================================
# NEXUS TRADER — Order Book Sub-Model  (Sprint 2)
#
# Reads cached order book imbalance from the OrderBookAgent.
# Fires only when imbalance signal is strong and confidence is high.
# Active in ALL regimes but confidence is weighted toward short TFs.
# ============================================================
from __future__ import annotations

import logging
from typing import Optional

import pandas as pd

from config.settings import settings as _s
from core.signals.sub_models.base import BaseSubModel
from core.meta_decision.order_candidate import ModelSignal

logger = logging.getLogger(__name__)

# Timeframe weight: order book signal is most reliable on short timeframes
_TF_WEIGHT = {
    "1m": 1.0, "3m": 1.0, "5m": 0.9, "15m": 0.8, "30m": 0.7,
    "1h": 0.55, "2h": 0.45, "4h": 0.35, "6h": 0.25,
    "12h": 0.15, "1d": 0.10,
}


class OrderBookModel(BaseSubModel):
    """
    Order book microstructure signal — best on short timeframes.
    Signals from this model have their confidence discounted
    on higher timeframes where the short-lived imbalance data
    becomes less meaningful.
    """

    ACTIVE_REGIMES: list[str] = []   # active in all regimes

    @property
    def name(self) -> str:
        return "order_book"

    def evaluate(
        self,
        symbol: str,
        df: pd.DataFrame,
        regime: str,
        timeframe: str,
    ) -> Optional[ModelSignal]:
        # Read tunable parameters from settings with fallback defaults
        _min_signal = float(_s.get('models.order_book.min_signal', 0.35))
        _min_confidence = float(_s.get('models.order_book.min_confidence', 0.60))
        _sl_atr_mult = float(_s.get('models.order_book.sl_atr_mult', 1.5))
        _tp_atr_mult = float(_s.get('models.order_book.tp_atr_mult', 2.0))

        try:
            from core.agents.order_book_agent import order_book_agent
            if order_book_agent is None:
                logger.debug("OrderBookModel: agent singleton is None for %s", symbol)
                return None
            cached = order_book_agent.get_symbol_signal(symbol)
        except Exception as exc:
            logger.debug("OrderBookModel: agent unavailable for %s — %s", symbol, exc)
            return None

        if cached.get("stale", True):
            return None

        signal_val = cached.get("signal", 0.0)
        confidence = cached.get("confidence", 0.0)
        imbalance  = cached.get("imbalance", 0.5)

        # Apply timeframe discount
        tf_weight  = _TF_WEIGHT.get(timeframe, 0.5)
        confidence = confidence * tf_weight

        if abs(signal_val) < _min_signal or confidence < _min_confidence:
            return None

        direction = "long" if signal_val > 0 else "short"
        close     = float(df["close"].iloc[-1])
        atr       = self._atr(df, 14)

        # Compute entry_price via base class buffer method (ENTRY_BUFFER_ATR = 0.0
        # for this model → entry = close, but SL/TP anchor to entry_price for
        # consistency with other models).
        entry_price = self._entry_price(close, atr, direction)

        if direction == "long":
            stop_loss   = entry_price - atr * _sl_atr_mult
            take_profit = entry_price + atr * _tp_atr_mult
        else:
            stop_loss   = entry_price + atr * _sl_atr_mult
            take_profit = entry_price - atr * _tp_atr_mult

        rationale = (
            f"Order book imbalance: {imbalance:.1%} bid pressure | "
            f"signal={signal_val:.3f} | TF weight={tf_weight:.2f}"
        )

        return ModelSignal(
            model_name   = self.name,
            symbol       = symbol,
            direction    = direction,
            strength     = round(abs(signal_val) * confidence, 4),
            entry_price  = round(entry_price, 8),
            stop_loss    = round(stop_loss, 8),
            take_profit  = round(take_profit, 8),
            atr_value    = atr,
            regime       = regime,
            timeframe    = timeframe,
            rationale    = rationale,
        )
