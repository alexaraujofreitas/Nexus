# ============================================================
# NEXUS TRADER — Funding Rate Sub-Model  (Sprint 1)
#
# Reads cached funding rate data from the FundingRateAgent
# and converts it into a ModelSignal if the signal is strong
# enough to warrant attention.
#
# Active in ALL regimes — funding rate is regime-agnostic.
# The signal is CONTRARIAN to funding:
#   Extreme positive funding → bearish ModelSignal
#   Extreme negative funding → bullish ModelSignal
#
# Threshold: |signal| >= 0.40 and confidence >= 0.55 to fire.
# ============================================================
from __future__ import annotations

import logging
from typing import Optional

import pandas as pd

from config.settings import settings as _s
from core.signals.sub_models.base import BaseSubModel
from core.meta_decision.order_candidate import ModelSignal

logger = logging.getLogger(__name__)


class FundingRateModel(BaseSubModel):
    """
    Adds a funding-rate-derived ModelSignal to the signal pipeline.

    Reads from the FundingRateAgent singleton (non-blocking cache read).
    If the agent is unavailable or data is stale, returns None silently.
    """

    # Active in all regimes — funding is always relevant
    ACTIVE_REGIMES: list[str] = []   # empty → active in all

    @property
    def name(self) -> str:
        return "funding_rate"

    def evaluate(
        self,
        symbol: str,
        df: pd.DataFrame,
        regime: str,
        timeframe: str,
    ) -> Optional[ModelSignal]:
        # Read tunable parameters from settings with fallback defaults
        _min_signal = float(_s.get('models.funding_rate.min_signal', 0.40))
        _min_confidence = float(_s.get('models.funding_rate.min_confidence', 0.55))
        _sl_atr_mult = float(_s.get('models.funding_rate.sl_atr_mult', 1.5))
        _tp_atr_mult = float(_s.get('models.funding_rate.tp_atr_mult', 2.5))

        # ── Get cached signal from agent ──────────────────────
        try:
            from core.agents.funding_rate_agent import funding_rate_agent
            if funding_rate_agent is None:
                logger.debug("FundingRateModel: agent singleton is None for %s", symbol)
                return None
            cached = funding_rate_agent.get_symbol_signal(symbol)
        except Exception as exc:
            logger.debug("FundingRateModel: agent unavailable for %s — %s", symbol, exc)
            return None

        if cached.get("stale", True):
            logger.debug("FundingRateModel: stale data for %s — skipping", symbol)
            return None

        signal_val = cached.get("signal", 0.0)
        confidence = cached.get("confidence", 0.0)
        rate_pct   = cached.get("rate_pct", 0.0)
        direction_str = cached.get("direction", "neutral")
        explanation   = cached.get("explanation", "")

        # ── Threshold check ───────────────────────────────────
        if abs(signal_val) < _min_signal or confidence < _min_confidence:
            logger.debug(
                "FundingRateModel: %s signal=%.3f conf=%.2f — below threshold",
                symbol, signal_val, confidence,
            )
            return None

        # ── Translate to ModelSignal ──────────────────────────
        direction = "long" if signal_val > 0 else "short"
        close     = float(df["close"].iloc[-1])
        atr       = self._atr(df, 14)

        # Compute entry_price via the base class buffer method for consistency
        # with all other models (ENTRY_BUFFER_ATR = 0.0 → entry = close for
        # this model, but SL/TP anchor to entry_price not close).
        entry_price = self._entry_price(close, atr, direction)

        # ATR-based SL/TP anchored to entry_price (not raw close)
        if direction == "long":
            stop_loss   = entry_price - atr * _sl_atr_mult
            take_profit = entry_price + atr * _tp_atr_mult
        else:
            stop_loss   = entry_price + atr * _sl_atr_mult
            take_profit = entry_price - atr * _tp_atr_mult

        rationale = (
            f"Funding Rate signal ({rate_pct:+.3f}%/8h): {explanation}"
        )

        logger.info(
            "FundingRateModel: %s | %s | rate=%+.3f%% | signal=%.3f | conf=%.2f",
            symbol, direction, rate_pct, signal_val, confidence,
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
