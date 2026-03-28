# ============================================================
# NEXUS TRADER — Swing Low Continuation Model (Sub-Model 7)
#
# Active in: bear_trend
# Timeframe: 1h (requires df_1h in context)
#
# Logic — EXACT reproduction of Phase 5 backtest (v7_final):
#   Regime gate:     bear_trend only
#   ADX gate:        ADX14 ≥ 28  (confirmed downtrend strength)
#   Swing-low gate:  close < min(close[-10:-1]) on 1h bars
#                    (current bar makes a new 10-bar closing low)
#   Stop-loss:       entry + 2.5 × ATR14  (above entry, short trade)
#   Take-profit:     entry − 2.0 × ATR14  (below entry, short trade)
#
# Strength calculation:
#   base       = 0.30
#   ADX bonus  = (ADX14 − 28) / 72 × 0.30  (scaled 0→0.30 as ADX 28→100)
#   swing_depth = (min10 − close) / (ATR14 × 2) clamped [0,1]  (depth bonus)
#   depth bonus = swing_depth × 0.25
#   Capped at 0.85
#
# Timeframe routing:
#   The scanner's primary TF is 30m.  SLC requires 1h data.
#   The scanner fetches 1h OHLCV as a secondary fetch and passes it
#   as context["df_1h"].  If absent, model returns None (hard requirement
#   — SLC cannot synthesize 1h data from 30m alone without resampling
#   and without knowing which bars are complete).
#
# Direction: SHORT only (bear-trend continuation)
# ============================================================
from __future__ import annotations

import logging
from typing import Optional
import pandas as pd

from core.signals.sub_models.base import BaseSubModel
from core.meta_decision.order_candidate import ModelSignal
from core.regime.regime_classifier import REGIME_BEAR_TREND

logger = logging.getLogger(__name__)

# ── Tunable constants (can be overridden via config.yaml: mr_pbl_slc.*) ──────
_ADX_MIN          = 28.0
_SWING_BARS       = 10       # look-back bars for new closing low
_SL_ATR_MULT      = 2.5
_TP_ATR_MULT      = 2.0


class SwingLowContinuationModel(BaseSubModel):
    """
    Swing-low continuation short model active in bear_trend regime.

    Fires when price makes a new 10-bar closing low on 1h data during a
    confirmed bear trend (ADX ≥ 28), indicating downtrend continuation.
    """

    ACTIVE_REGIMES = [REGIME_BEAR_TREND]

    # Short trade: entry is set at or below close — no buffer needed
    ENTRY_BUFFER_ATR: float = 0.0

    # High affinity only in bear_trend; zero in anything bullish
    REGIME_AFFINITY: dict[str, float] = {
        "bull_trend":             0.0,
        "bear_trend":             1.0,
        "ranging":                0.05,
        "volatility_expansion":   0.3,
        "volatility_compression": 0.05,
        "uncertain":              0.1,
        "crisis":                 0.0,
        "liquidation_cascade":    0.0,
        "squeeze":                0.1,
        "recovery":               0.0,
        "accumulation":           0.0,
        "distribution":           0.2,
    }

    @property
    def name(self) -> str:
        return "swing_low_continuation"

    def evaluate(
        self,
        symbol: str,
        df: pd.DataFrame,
        regime: str,
        timeframe: str,
        context: Optional[dict] = None,
    ) -> Optional[ModelSignal]:
        """
        Evaluate swing-low continuation (short) opportunity.

        Parameters
        ----------
        df : pd.DataFrame
            Primary timeframe data (30m).  Not used for SLC signal logic —
            only used as fallback if df_1h is absent.
        context : dict, optional
            Must contain 'df_1h': pre-fetched 1h DataFrame with at least
            _SWING_BARS + 20 rows and indicators computed.
            If absent, model returns None (1h data is mandatory for SLC).
        """
        # ── Regime guard (hard gate) ──────────────────────────────────────
        if regime != REGIME_BEAR_TREND:
            return None

        # ── Read config overrides ──────────────────────────────────────
        try:
            from config.settings import settings as _s
            adx_min    = float(_s.get("mr_pbl_slc.swing_low_continuation.adx_min",    _ADX_MIN))
            swing_bars = int(  _s.get("mr_pbl_slc.swing_low_continuation.swing_bars", _SWING_BARS))
            sl_mult    = float(_s.get("mr_pbl_slc.swing_low_continuation.sl_atr_mult", _SL_ATR_MULT))
            tp_mult    = float(_s.get("mr_pbl_slc.swing_low_continuation.tp_atr_mult", _TP_ATR_MULT))
        except Exception:
            adx_min    = _ADX_MIN
            swing_bars = _SWING_BARS
            sl_mult    = _SL_ATR_MULT
            tp_mult    = _TP_ATR_MULT

        # ── Require 1h DataFrame from context ─────────────────────────
        df_1h: Optional[pd.DataFrame] = (context or {}).get("df_1h")

        if df_1h is None or len(df_1h) < swing_bars + 20:
            logger.debug(
                "SLC %s: no 1h data in context (df_1h=%s, len=%s) — skipping",
                symbol,
                type(df_1h).__name__ if df_1h is not None else "None",
                len(df_1h) if df_1h is not None else "N/A",
            )
            return None

        # Work on 1h data for all signal logic
        df_work = df_1h

        # ── Extract indicators ────────────────────────────────────────
        close  = float(df_work["close"].iloc[-1])
        atr    = self._atr(df_work, 14)
        adx    = self._col(df_work, "adx")
        if adx is None:
            adx = self._col(df_work, "adx_14")

        if adx is None:
            logger.debug("SLC %s: missing ADX indicator on 1h data", symbol)
            return None

        # ── Condition 1: ADX gate ─────────────────────────────────────
        if adx < adx_min:
            logger.debug("SLC %s: ADX=%.1f < %.1f — no trend strength", symbol, adx, adx_min)
            return None

        # ── Condition 2: New 10-bar closing low ───────────────────────
        # Current bar close < all previous 10 closes (bars −10 to −1 inclusive)
        # "Previous" means bars BEFORE the current bar (no lookahead).
        if len(df_work) < swing_bars + 2:
            logger.debug("SLC %s: insufficient 1h bars for swing_bars=%d", symbol, swing_bars)
            return None

        prev_closes = df_work["close"].iloc[-(swing_bars + 1):-1]   # last `swing_bars` before current
        prev_min    = float(prev_closes.min())

        if close >= prev_min:
            logger.debug(
                "SLC %s: close=%.4f ≥ min10=%.4f — not a new 10-bar low",
                symbol, close, prev_min,
            )
            return None

        # ── Strength calculation ──────────────────────────────────────
        strength = 0.30  # base for passing both primary conditions

        # ADX bonus: ADX 28→100 maps to 0→0.30
        adx_range = 100.0 - adx_min
        adx_bonus = min(0.30, (adx - adx_min) / adx_range * 0.30)
        strength += adx_bonus

        # Swing depth bonus: how far below the 10-bar min is this close?
        # depth = (prev_min - close) / (2 × ATR).  Deeper break → more bonus.
        if atr > 0:
            swing_depth = max(0.0, min(1.0, (prev_min - close) / (2.0 * atr)))
        else:
            swing_depth = 0.0
        depth_bonus = swing_depth * 0.25
        strength += depth_bonus

        strength = round(min(0.85, strength), 4)

        # ── Price levels ──────────────────────────────────────────────
        entry_price = close  # at-close market entry on 1h signal
        stop_loss   = entry_price + sl_mult * atr   # above entry (short)
        take_profit = entry_price - tp_mult * atr   # below entry (short)

        # ── Rationale ────────────────────────────────────────────────
        rationale = (
            f"[SwingLowContinuation | {regime}] "
            f"ADX14={adx:.1f}≥{adx_min:.0f} ✓ | "
            f"New 10-bar closing low: close={close:.4f}<prev_min10={prev_min:.4f} ✓ | "
            f"ADX_bonus={adx_bonus:.3f} depth_bonus={depth_bonus:.3f} | "
            f"SL={stop_loss:.4f}(+{sl_mult}×ATR) TP={take_profit:.4f}(-{tp_mult}×ATR)"
        )

        logger.info(
            "SLC SIGNAL: %s | strength=%.3f | ADX=%.1f | swing_depth=%.3f | SL=%.4f | TP=%.4f",
            symbol, strength, adx, swing_depth, stop_loss, take_profit,
        )

        return ModelSignal(
            symbol      = symbol,
            model_name  = self.name,
            direction   = "short",
            strength    = strength,
            entry_price = round(entry_price, 8),
            stop_loss   = round(stop_loss, 8),
            take_profit = round(take_profit, 8),
            timeframe   = "1h",      # SLC always runs on 1h data
            regime      = regime,
            rationale   = rationale,
            atr_value   = atr,
        )
