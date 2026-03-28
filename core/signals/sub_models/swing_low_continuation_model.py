# ============================================================
# NEXUS TRADER — Swing Low Continuation Model (Sub-Model 7)
#
# Active in: bear_trend — ACTIVE_REGIMES gate enforced by SignalGenerator
# Timeframe: 1h (requires df_1h in context)
#
# Logic — EXACT reproduction of Phase 5 backtest gen_slc() in
# scripts/mr_pbl_slc_research/backtest_v7_final.py:
#
#   Regime gate:     ACTIVE_REGIMES=["bear_trend"] — SignalGenerator blocks
#                    the model unless regime=="bear_trend".  The caller
#                    (scanner/backtest) must pass regime from
#                    ResearchRegimeClassifier.regime_to_string(
#                        classify_latest_bar(df_1h)) so only real
#                    research BEAR_TREND 1h bars reach evaluate().
#   ADX gate:        1h ADX14 (Wilder) ≥ 28
#   Swing-low gate:  1h close < shift(1).rolling(10).min()
#                    i.e. close < min(close[-10], …, close[-1])
#   Stop-loss:       entry + 2.5 × 1h ATR14  (above entry, short)
#   Take-profit:     entry − 2.0 × 1h ATR14  (below entry, short)
#
# Regime source:
#   ResearchRegimeClassifier.regime_to_string(classify_latest_bar(df_1h))
#   → called by scanner (live) and backtest (historical) on 1h data
#   → passed as the `regime` parameter to generate(), which applies the
#     ACTIVE_REGIMES gate before calling evaluate().
#   NO context injection of regime integers — regime comes through the
#   official generate() → evaluate() interface only.
#
# CRITICAL: regime is for the 1h series, NOT the 30m primary series.
# The research classifies regime on 1h OHLCV independently.
# ============================================================
from __future__ import annotations

import logging
from typing import Optional
import pandas as pd

from core.signals.sub_models.base import BaseSubModel
from core.meta_decision.order_candidate import ModelSignal
from core.regime.regime_classifier import REGIME_BEAR_TREND

logger = logging.getLogger(__name__)

# ── Tunable constants ─────────────────────────────────────────────────────────
_ADX_MIN          = 28.0
_SWING_BARS       = 10
_SL_ATR_MULT      = 2.5
_TP_ATR_MULT      = 2.0


class SwingLowContinuationModel(BaseSubModel):
    """
    Swing-low continuation short model active in research BEAR_TREND regime
    on the 1h series.

    Regime enforcement:
      SignalGenerator.generate() checks ACTIVE_REGIMES=["bear_trend"] before
      calling evaluate().  The caller (scanner/backtest) must pass
      ResearchRegimeClassifier.regime_to_string(classify_latest_bar(df_1h))
      as the regime argument so only research BEAR_TREND 1h bars reach this
      model.
    """

    # Restored: SignalGenerator hard-gates this model to bear_trend only.
    # The caller supplies regime from ResearchRegimeClassifier applied to
    # the 1h series so the gate maps cleanly onto research BEAR_TREND bars.
    ACTIVE_REGIMES: list[str] = [REGIME_BEAR_TREND]
    ENTRY_BUFFER_ATR: float = 0.0

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
        Evaluate swing-low continuation (short) on 1h data.

        Parameters
        ----------
        regime : str
            NexusTrader regime string, supplied by the caller from
            ResearchRegimeClassifier.regime_to_string(classify_latest_bar(df_1h)).
            The ACTIVE_REGIMES gate in SignalGenerator guarantees this equals
            REGIME_BEAR_TREND when called via generate().  The explicit check
            below is retained as a defensive guard for direct (test) calls.
        context : dict, optional
            Expected keys:
              "df_1h" : pd.DataFrame  — 1h OHLCV with indicators (mandatory)
        """
        ctx = context or {}

        # ── 1h data is mandatory ──────────────────────────────────────
        df_1h: Optional[pd.DataFrame] = ctx.get("df_1h")
        if df_1h is None or len(df_1h) < _SWING_BARS + 20:
            logger.debug(
                "SLC %s: no 1h data (len=%s) — skipping",
                symbol, len(df_1h) if df_1h is not None else "N/A",
            )
            return None

        # ── Regime gate ────────────────────────────────────────────────
        # ACTIVE_REGIMES=["bear_trend"] ensures this is only reached via
        # generate() when regime=="bear_trend".  Guard retained for direct calls.
        if regime != REGIME_BEAR_TREND:
            logger.debug("SLC %s: regime=%s ≠ bear_trend — skip", symbol, regime)
            return None

        # ── Read config overrides ─────────────────────────────────────
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

        # Work on 1h data
        df_work = df_1h

        # ── Extract indicators from 1h data ───────────────────────────
        close = float(df_work["close"].iloc[-1])
        atr   = self._atr(df_work, 14)
        adx   = self._col(df_work, "adx")
        if adx is None:
            adx = self._col(df_work, "adx_14")
        if adx is None:
            logger.debug("SLC %s: missing ADX on 1h data", symbol)
            return None

        # ── Condition 1: ADX gate (Wilder ADX14 from indicator library) ─
        if adx < adx_min:
            logger.debug("SLC %s: ADX=%.1f < %.1f", symbol, adx, adx_min)
            return None

        # ── Condition 2: New 10-bar closing low ───────────────────────
        # Research: sw10 = close.shift(1).rolling(10).min()
        # i.e. sw10[i] = min(close[i-10], ..., close[i-1])
        # Signal: close[i] < sw10[i]
        if len(df_work) < swing_bars + 2:
            logger.debug("SLC %s: insufficient 1h bars for swing_bars=%d", symbol, swing_bars)
            return None

        prev_closes = df_work["close"].iloc[-(swing_bars + 1):-1]  # bars i-10 to i-1
        prev_min    = float(prev_closes.min())

        if close >= prev_min:
            logger.debug("SLC %s: close=%.4f ≥ prev_min10=%.4f", symbol, close, prev_min)
            return None

        # ── Strength calculation ──────────────────────────────────────
        strength = 0.30

        adx_range = 100.0 - adx_min
        adx_bonus = min(0.30, (adx - adx_min) / adx_range * 0.30)
        strength += adx_bonus

        if atr > 0:
            swing_depth = max(0.0, min(1.0, (prev_min - close) / (2.0 * atr)))
        else:
            swing_depth = 0.0
        depth_bonus = swing_depth * 0.25
        strength   += depth_bonus

        strength = round(min(0.85, strength), 4)

        # ── Price levels (match research: SL/TP from signal-bar close+ATR) ──
        entry_price = close
        stop_loss   = entry_price + sl_mult * atr   # above (short)
        take_profit = entry_price - tp_mult * atr   # below (short)

        rationale = (
            f"[SLC | regime_1h={regime}] "
            f"ADX14={adx:.1f}≥{adx_min:.0f} ✓ | "
            f"New 10-bar low: close={close:.4f}<prev_min={prev_min:.4f} ✓ | "
            f"adx_bonus={adx_bonus:.3f} depth_bonus={depth_bonus:.3f} | "
            f"SL={stop_loss:.4f} TP={take_profit:.4f}"
        )

        logger.info(
            "SLC SIGNAL: %s | strength=%.3f | ADX=%.1f | close=%.4f | SL=%.4f | TP=%.4f",
            symbol, strength, adx, close, stop_loss, take_profit,
        )

        return ModelSignal(
            symbol      = symbol,
            model_name  = self.name,
            direction   = "short",
            strength    = strength,
            entry_price = round(entry_price, 8),
            stop_loss   = round(stop_loss,   8),
            take_profit = round(take_profit,  8),
            timeframe   = "1h",
            regime      = regime,
            rationale   = rationale,
            atr_value   = atr,
        )
