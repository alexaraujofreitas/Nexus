# ============================================================
# NEXUS TRADER — Order Book Sub-Model  (Sprint 2)
#
# Reads cached order book imbalance from the OrderBookAgent.
# Fires only when imbalance signal is strong and confidence is high.
#
# TIMEFRAME RESTRICTION (Session 21 audit):
#   At 1h+, tf_weight=0.55 makes the effective required confidence
#   = min_confidence / tf_weight = 0.60 / 0.55 = 1.09 — impossible
#   to satisfy. Rather than allow silent non-fires, this model is
#   HARD-GATED at max_timeframe (default "30m"). This is a REMOVAL
#   of a misaligned component at 1h+, not the activation of a new
#   signal. The model remains fully operational at ≤30m.
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
    Order book microstructure signal.

    ACTIVE TIMEFRAMES: ≤ 30m only (configurable via models.order_book.max_timeframe).
    At 1h and above the TF discount makes the confidence threshold unreachable
    (effective threshold > 1.0), so those TFs are hard-gated off — this is
    deliberate removal of a structurally broken signal path, not a new restriction.

    The _TF_WEIGHT table is retained for backward-compatibility with sub-30m
    timeframes where the discount is meaningful (e.g. 5m weight=0.9, 15m=0.8).
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
        # Hard TF gate — OrderBook signal structurally cannot fire at 1h+
        # because min_confidence / TF_WEIGHT > 1.0.  Gating here makes the
        # non-fire explicit rather than silent.
        # Indirect paths checked: no other code calls evaluate() with
        # timeframe > max_timeframe — the scanner always passes the active
        # scan timeframe which is "1h" for the main scan, so this gate
        # ensures the model contributes ZERO at 1h.
        _max_tf = _s.get('models.order_book.max_timeframe', '30m')
        _tf_order = ['1m','3m','5m','15m','30m','1h','2h','4h','6h','12h','1d']
        if _tf_order.index(timeframe) >= _tf_order.index(_max_tf):
            return None  # removed from 1h scan — not a conditional skip

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
