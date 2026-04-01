# ============================================================
# NEXUS TRADER — RangeBreakoutModel (Sub-Model — Phase 2c)
#
# Standalone execution model triggered by range_breakout
# TransitionEvents from FeatureTransitionDetector.
#
# Architecture:
#   4h trend gate (optional) → 1h transition detection (event) → 30m entry
#
# Entry Logic:
#   1. Requires context["transition_event"] of type "range_breakout"
#   2. Direction: event.direction ("long" or "short")
#   3. Entry price:
#      - Without buffer: range_high (long) or range_low (short) from event
#      - With buffer:    range_high + 0.1×ATR (long), range_low - 0.1×ATR (short)
#   4. SL: inside the range — range_low - cushion (long), range_high + cushion (short)
#      cushion = sl_range_pct × range_width (default 10%)
#   5. TP: range projection — entry + tp_range_mult × range_width (long)
#
# Confirmation: OFF (per Step 1 validation — confirmation OFF outperforms)
#
# Config gate: phase_2c.range_breakout.enabled (default False)
# ============================================================
from __future__ import annotations

import logging
from typing import Optional

import pandas as pd

from core.signals.sub_models.base import BaseSubModel
from core.meta_decision.order_candidate import ModelSignal

logger = logging.getLogger(__name__)

# ── Tunable constants ─────────────────────────────────────────────────────────
_SL_RANGE_PCT      = 0.10   # SL cushion as % of range width
_TP_RANGE_MULT     = 1.0    # TP = entry ± mult × range_width
_ENTRY_BUFFER_ATR  = 0.1    # Entry offset in ATR units (0 = at breakout level)
_MIN_CONFIDENCE    = 0.35   # Minimum event confidence to act on
_STRENGTH_BASE     = 0.35   # Base strength before bonuses
_STRENGTH_CAP      = 0.80   # Maximum strength


class RangeBreakoutModel(BaseSubModel):
    """
    Standalone execution model for range breakout transitions.

    Fires when FeatureTransitionDetector emits a range_breakout event.
    Entry is placed at the breakout boundary (± optional ATR buffer).
    SL sits inside the range with a small cushion; TP projects the
    range width beyond entry.

    ACTIVE_REGIMES = [] — fires in all NX regimes via HMM/NX path.
    Regime filtering is implicit in the transition detector itself
    (adx_max_ranging gate) and via REGIME_AFFINITY suppression.

    Config gate: phase_2c.range_breakout.enabled (default False).
    """

    ACTIVE_REGIMES: list[str] = []
    ENTRY_BUFFER_ATR: float = 0.0  # Not used by base; we handle buffer ourselves

    REGIME_AFFINITY: dict[str, float] = {
        "bull_trend":             0.6,
        "bear_trend":             0.6,
        "ranging":                0.8,   # Breakouts from ranges are high-value
        "volatility_expansion":   0.5,
        "volatility_compression": 0.7,   # Compression often precedes breakout
        "uncertain":              0.3,
        "crisis":                 0.0,
        "liquidation_cascade":    0.0,
        "squeeze":                0.5,
        "recovery":               0.4,
        "accumulation":           0.7,
        "distribution":           0.5,
    }

    @property
    def name(self) -> str:
        return "range_breakout"

    def evaluate(
        self,
        symbol: str,
        df: pd.DataFrame,
        regime: str,
        timeframe: str,
        context: Optional[dict] = None,
    ) -> Optional[ModelSignal]:
        """
        Evaluate range breakout opportunity.

        Parameters
        ----------
        context : dict, optional
            Expected keys:
              "transition_event" : TransitionEvent — must be event_type="range_breakout"
        """
        ctx = context or {}

        # ── Config gate ───────────────────────────────────────────────
        _enabled = False
        _entry_buffer = _ENTRY_BUFFER_ATR
        _sl_range_pct = _SL_RANGE_PCT
        _tp_range_mult = _TP_RANGE_MULT
        _min_confidence = _MIN_CONFIDENCE
        _strength_base = _STRENGTH_BASE
        _strength_cap = _STRENGTH_CAP
        try:
            from config.settings import settings as _s
            _enabled = bool(_s.get("phase_2c.range_breakout.enabled", False))
            _entry_buffer = float(_s.get("phase_2c.range_breakout.entry_buffer_atr", _ENTRY_BUFFER_ATR))
            _sl_range_pct = float(_s.get("phase_2c.range_breakout.sl_range_pct", _SL_RANGE_PCT))
            _tp_range_mult = float(_s.get("phase_2c.range_breakout.tp_range_mult", _TP_RANGE_MULT))
            _min_confidence = float(_s.get("phase_2c.range_breakout.min_confidence", _MIN_CONFIDENCE))
            _strength_base = float(_s.get("phase_2c.range_breakout.strength_base", _STRENGTH_BASE))
            _strength_cap = float(_s.get("phase_2c.range_breakout.strength_cap", _STRENGTH_CAP))
        except Exception:
            pass

        if not _enabled:
            return None

        # ── Transition event gate ─────────────────────────────────────
        transition_ev = ctx.get("transition_event")
        if transition_ev is None:
            return None
        if getattr(transition_ev, "event_type", "") != "range_breakout":
            return None
        if getattr(transition_ev, "expires_bars", 0) <= 0:
            return None

        direction = getattr(transition_ev, "direction", "")
        if direction not in ("long", "short"):
            return None

        ev_conf = getattr(transition_ev, "confidence", 0.0)
        if ev_conf < _min_confidence:
            logger.debug(
                "RB %s: event confidence %.3f < min %.3f — skip",
                symbol, ev_conf, _min_confidence,
            )
            return None

        # ── Extract features from event snapshot ──────────────────────
        snap = getattr(transition_ev, "features_snapshot", {})
        range_high = snap.get("range_high")
        range_low = snap.get("range_low")
        vol_ratio = snap.get("vol_ratio", 1.0)
        mom_count = snap.get("mom_count", 0)
        range_bars = snap.get("range_bars", 10)

        if range_high is None or range_low is None:
            logger.debug("RB %s: missing range_high/range_low in event snapshot", symbol)
            return None

        range_width = range_high - range_low
        if range_width <= 0:
            return None

        # ── Extract current price + ATR from 30m data ─────────────────
        if len(df) < 30:
            logger.debug("RB %s: insufficient bars (%d < 30)", symbol, len(df))
            return None

        close = float(df["close"].iloc[-1])
        atr = self._atr(df, 14)

        # ── Entry price ───────────────────────────────────────────────
        buffer = _entry_buffer * atr

        if direction == "long":
            entry_price = range_high + buffer
        else:
            entry_price = range_low - buffer

        # ── SL / TP ──────────────────────────────────────────────────
        cushion = _sl_range_pct * range_width

        if direction == "long":
            stop_loss = range_low - cushion
            take_profit = entry_price + _tp_range_mult * range_width
        else:
            stop_loss = range_high + cushion
            take_profit = entry_price - _tp_range_mult * range_width

        # Validate SL < EP < TP (long) or TP < EP < SL (short)
        if direction == "long":
            if not (stop_loss < entry_price < take_profit):
                logger.debug(
                    "RB %s: invalid levels SL=%.4f EP=%.4f TP=%.4f (long)",
                    symbol, stop_loss, entry_price, take_profit,
                )
                return None
        else:
            if not (take_profit < entry_price < stop_loss):
                logger.debug(
                    "RB %s: invalid levels TP=%.4f EP=%.4f SL=%.4f (short)",
                    symbol, take_profit, entry_price, stop_loss,
                )
                return None

        # ── Strength calculation ──────────────────────────────────────
        strength = _strength_base

        # Confidence bonus (from event)
        conf_bonus = min(0.20, (ev_conf - 0.35) * 0.40)
        strength += max(0.0, conf_bonus)

        # Volume ratio bonus
        vol_bonus = min(0.15, (vol_ratio - 1.5) * 0.15 / 0.5) if vol_ratio > 1.5 else 0.0
        strength += vol_bonus

        # Momentum count bonus
        mom_bonus = min(0.10, mom_count * 0.05)
        strength += mom_bonus

        # Range duration bonus (longer ranges = stronger breakouts)
        dur_bonus = min(0.10, (range_bars - 10) / 30 * 0.10)
        strength += max(0.0, dur_bonus)

        strength = round(min(_strength_cap, strength), 4)

        # ── R:R sanity check ──────────────────────────────────────────
        risk = abs(entry_price - stop_loss)
        reward = abs(take_profit - entry_price)
        if risk <= 0 or reward / risk < 0.8:
            logger.debug("RB %s: R:R=%.2f < 0.8 — skip", symbol, reward / risk if risk > 0 else 0)
            return None

        # ── Build signal ──────────────────────────────────────────────
        rationale = (
            f"[RB | regime={regime}] "
            f"Range [{range_low:.2f}, {range_high:.2f}] width={range_width:.2f} "
            f"bars={range_bars} | "
            f"Break {direction} conf={ev_conf:.3f} vol_ratio={vol_ratio:.2f} "
            f"mom={mom_count} | "
            f"Entry={entry_price:.4f} buffer={'%.3f' % buffer if buffer > 0 else 'OFF'} | "
            f"SL={stop_loss:.4f} TP={take_profit:.4f} R:R={reward/risk:.2f}"
        )

        logger.info(
            "RB SIGNAL: %s %s | strength=%.3f | entry=%.4f SL=%.4f TP=%.4f | "
            "range=[%.2f,%.2f] conf=%.3f",
            symbol, direction, strength, entry_price, stop_loss, take_profit,
            range_low, range_high, ev_conf,
        )

        return ModelSignal(
            symbol=symbol,
            model_name=self.name,
            direction=direction,
            strength=strength,
            entry_price=round(entry_price, 8),
            stop_loss=round(stop_loss, 8),
            take_profit=round(take_profit, 8),
            timeframe=timeframe,
            regime=regime,
            rationale=rationale,
            atr_value=atr,
        )
