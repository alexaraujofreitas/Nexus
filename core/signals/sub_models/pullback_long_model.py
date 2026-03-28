# ============================================================
# NEXUS TRADER — Pullback Long Model (Sub-Model 6)
#
# Active in: bull_trend (research regime integer = 1)
# Timeframe: 30m (primary); requires 4h HTF confirmation
#
# Logic — EXACT reproduction of Phase 5 backtest gen_pbl() in
# scripts/mr_pbl_slc_research/backtest_v7_final.py:
#
#   Regime gate:    research BULL_TREND (ADX≥22, EMA20>EMA50,
#                   price>EMA200, ATR_ratio<1.80) on 30m series
#   4h HTF gate:    4h EMA20 > 4h EMA50  (bullish higher-TF bias)
#   EMA50 proximity: |close − EMA50| ≤ 0.5 × ATR14
#   Rejection candle (ALL three required):
#                   (a) close > open        — bullish body
#                   (b) lower_wick > upper_wick  — tail rejection
#                   (c) lower_wick > body         — tail dominates
#   RSI gate:       RSI14 > 40
#   Stop-loss:      close_signal − 2.5 × ATR14
#   Take-profit:    close_signal + 3.0 × ATR14
#
# Regime source (priority):
#   1. context["research_regime_30m"]  (int from ResearchRegimeClassifier)
#      → set by scanner / backtest at each bar
#   2. Fallback: NexusTrader regime string (for test compatibility)
#
# HTF data injection:
#   Scanner passes pre-fetched 4h OHLCV as context["df_4h"].
#   If absent, HTF gate is bypassed with a debug warning.
# ============================================================
from __future__ import annotations

import logging
from typing import Optional
import pandas as pd

from core.signals.sub_models.base import BaseSubModel
from core.meta_decision.order_candidate import ModelSignal
from core.regime.regime_classifier import REGIME_BULL_TREND
from core.regime.research_regime_classifier import BULL_TREND as _RES_BULL_TREND

logger = logging.getLogger(__name__)

# ── Tunable constants ─────────────────────────────────────────────────────────
_EMA50_PROX_ATR_MULT   = 0.5
_SL_ATR_MULT           = 2.5
_TP_ATR_MULT           = 3.0
_RSI_MIN               = 40.0
_HTF_EMA_FAST          = 20
_HTF_EMA_SLOW          = 50


class PullbackLongModel(BaseSubModel):
    """
    Pullback-to-EMA50 long model active in research BULL_TREND regime.

    Fires when price pulls back to 30m EMA50 during a bullish 4h trend,
    then shows a REJECTION candle (long lower wick, short upper wick,
    lower wick dominates body) with RSI > 40.

    Regime is read from context["research_regime_30m"] (int, preferred)
    or falls back to the NexusTrader regime string for test compatibility.
    """

    # ACTIVE_REGIMES is empty so the SignalGenerator ACTIVE_REGIMES hard gate
    # always passes — regime control is handled entirely inside evaluate() via
    # context["research_regime_30m"] (primary) or the NexusTrader regime string
    # (fallback).  REGIME_AFFINITY still applies via adaptive activation so the
    # model is suppressed in crisis / liquidation_cascade regimes as expected.
    ACTIVE_REGIMES: list[str] = []
    ENTRY_BUFFER_ATR: float = 0.0

    REGIME_AFFINITY: dict[str, float] = {
        "bull_trend":             1.0,
        "bear_trend":             0.0,
        "ranging":                0.05,
        "volatility_expansion":   0.2,
        "volatility_compression": 0.05,
        "uncertain":              0.1,
        "crisis":                 0.0,
        "liquidation_cascade":    0.0,
        "squeeze":                0.05,
        "recovery":               0.3,
        "accumulation":           0.15,
        "distribution":           0.0,
    }

    @property
    def name(self) -> str:
        return "pullback_long"

    def evaluate(
        self,
        symbol: str,
        df: pd.DataFrame,
        regime: str,
        timeframe: str,
        context: Optional[dict] = None,
    ) -> Optional[ModelSignal]:
        """
        Evaluate pullback-long opportunity.

        Parameters
        ----------
        regime : str
            NexusTrader regime string — used as FALLBACK if research regime
            is not in context.
        context : dict, optional
            Expected keys:
              "research_regime_30m" : int  — regime from ResearchRegimeClassifier
              "df_4h"               : pd.DataFrame  — 4h OHLCV with indicators
        """
        ctx = context or {}

        # ── Regime gate (research regime takes priority) ──────────────
        res_regime = ctx.get("research_regime_30m")
        if res_regime is not None:
            # Use research integer regime
            if int(res_regime) != _RES_BULL_TREND:
                logger.debug("PBL %s: research_regime_30m=%d ≠ BULL_TREND — skip", symbol, res_regime)
                return None
        else:
            # Fallback: NexusTrader string (test compatibility)
            if regime != REGIME_BULL_TREND:
                logger.debug("PBL %s: NexusTrader regime=%s ≠ bull_trend — skip", symbol, regime)
                return None

        # ── Read config overrides ─────────────────────────────────────
        try:
            from config.settings import settings as _s
            ema_prox_mult = float(_s.get("mr_pbl_slc.pullback_long.ema_prox_atr_mult", _EMA50_PROX_ATR_MULT))
            sl_mult       = float(_s.get("mr_pbl_slc.pullback_long.sl_atr_mult",       _SL_ATR_MULT))
            tp_mult       = float(_s.get("mr_pbl_slc.pullback_long.tp_atr_mult",       _TP_ATR_MULT))
            rsi_min       = float(_s.get("mr_pbl_slc.pullback_long.rsi_min",           _RSI_MIN))
        except Exception:
            ema_prox_mult = _EMA50_PROX_ATR_MULT
            sl_mult       = _SL_ATR_MULT
            tp_mult       = _TP_ATR_MULT
            rsi_min       = _RSI_MIN

        if len(df) < 60:
            logger.debug("PBL %s: insufficient bars (%d < 60)", symbol, len(df))
            return None

        # ── Extract OHLC for current (last) bar ──────────────────────
        last       = df.iloc[-1]
        close      = float(last["close"])
        open_      = float(last["open"])
        high_bar   = float(last["high"])
        low_bar    = float(last["low"])

        atr   = self._atr(df, 14)
        ema50 = self._col(df, "ema_50")
        rsi   = self._col(df, "rsi_14")

        if ema50 is None or rsi is None:
            logger.debug("PBL %s: missing ema_50 or rsi_14", symbol)
            return None

        # ── Condition 1: EMA50 proximity ──────────────────────────────
        prox_distance = abs(close - ema50)
        prox_threshold = ema_prox_mult * atr
        if prox_distance > prox_threshold:
            logger.debug(
                "PBL %s: prox fail |%.4f−%.4f|=%.4f > %.4f",
                symbol, close, ema50, prox_distance, prox_threshold,
            )
            return None

        # ── Condition 2: Rejection candle (3 sub-conditions) ─────────
        # Research gen_pbl():
        #   body = abs(close - open)
        #   lw   = min(open, close) - low   (lower wick length)
        #   uw   = high - max(open, close)  (upper wick length)
        #   rej  = (close > open) & (lw > uw) & (lw > body)
        body = abs(close - open_)
        lw   = min(close, open_) - low_bar      # lower wick
        uw   = high_bar - max(close, open_)     # upper wick

        if close <= open_:
            logger.debug("PBL %s: candle not bullish (close=%.4f ≤ open=%.4f)", symbol, close, open_)
            return None
        if lw <= uw:
            logger.debug("PBL %s: lower_wick=%.4f ≤ upper_wick=%.4f (not rejection)", symbol, lw, uw)
            return None
        if lw <= body:
            logger.debug("PBL %s: lower_wick=%.4f ≤ body=%.4f (not rejection)", symbol, lw, body)
            return None

        # ── Condition 3: RSI gate ─────────────────────────────────────
        if rsi <= rsi_min:
            logger.debug("PBL %s: RSI=%.1f ≤ %.1f", symbol, rsi, rsi_min)
            return None

        # ── Condition 4: 4h HTF gate ──────────────────────────────────
        htf_confirmed      = False
        htf_strength_bonus = 0.0
        df_4h: Optional[pd.DataFrame] = ctx.get("df_4h")

        if df_4h is not None and len(df_4h) >= max(_HTF_EMA_FAST, _HTF_EMA_SLOW) + 5:
            try:
                ema_fast_col = f"ema_{_HTF_EMA_FAST}"
                ema_slow_col = f"ema_{_HTF_EMA_SLOW}"

                if ema_fast_col in df_4h.columns and ema_slow_col in df_4h.columns:
                    htf_ema_fast = float(df_4h[ema_fast_col].iloc[-1])
                    htf_ema_slow = float(df_4h[ema_slow_col].iloc[-1])
                else:
                    htf_close    = df_4h["close"]
                    htf_ema_fast = float(htf_close.ewm(span=_HTF_EMA_FAST, adjust=False).mean().iloc[-1])
                    htf_ema_slow = float(htf_close.ewm(span=_HTF_EMA_SLOW, adjust=False).mean().iloc[-1])

                if htf_ema_fast > htf_ema_slow:
                    htf_confirmed = True
                    spread_pct = (htf_ema_fast - htf_ema_slow) / htf_ema_slow
                    htf_strength_bonus = 0.15 if spread_pct > 0.01 else 0.08
                    logger.debug(
                        "PBL %s: 4h HTF PASS EMA%d=%.4f > EMA%d=%.4f",
                        symbol, _HTF_EMA_FAST, htf_ema_fast, _HTF_EMA_SLOW, htf_ema_slow,
                    )
                else:
                    logger.debug(
                        "PBL %s: 4h HTF FAIL EMA%d=%.4f ≤ EMA%d=%.4f",
                        symbol, _HTF_EMA_FAST, htf_ema_fast, _HTF_EMA_SLOW, htf_ema_slow,
                    )
                    return None  # HTF gate required
            except Exception as exc:
                logger.warning("PBL %s: HTF gate error: %s — bypassing", symbol, exc)
        else:
            # No 4h data — bypass (graceful degradation)
            logger.debug("PBL %s: no df_4h in context — HTF gate bypassed", symbol)

        # ── Strength calculation ──────────────────────────────────────
        strength = 0.25  # base

        # RSI bonus: 0 at RSI=40, +0.25 at RSI=100
        rsi_bonus  = (min(rsi, 100.0) - rsi_min) / (100.0 - rsi_min) * 0.25
        strength  += rsi_bonus

        # Proximity bonus: 0 at threshold, +0.25 exactly at EMA50
        if prox_threshold > 0:
            prox_bonus = (1.0 - prox_distance / prox_threshold) * 0.25
        else:
            prox_bonus = 0.0
        strength += prox_bonus

        strength += htf_strength_bonus
        strength  = round(min(0.90, strength), 4)

        # ── Price levels (match research: SL/TP from signal-bar close+ATR) ──
        entry_price = close
        stop_loss   = close - sl_mult * atr
        take_profit = close + tp_mult * atr

        rationale = (
            f"[PBL | regime={res_regime if res_regime is not None else regime}] "
            f"EMA50={ema50:.4f} prox={prox_distance:.4f}≤{prox_threshold:.4f} ✓ | "
            f"Rejection: lw={lw:.4f}>uw={uw:.4f}>body={body:.4f} ✓ | "
            f"RSI={rsi:.1f}>{rsi_min:.0f} ✓ | "
            f"HTF={'confirmed' if htf_confirmed else 'bypassed'} | "
            f"SL={stop_loss:.4f} TP={take_profit:.4f}"
        )

        logger.info(
            "PBL SIGNAL: %s | strength=%.3f | SL=%.4f | TP=%.4f | HTF=%s",
            symbol, strength, stop_loss, take_profit, htf_confirmed,
        )

        return ModelSignal(
            symbol      = symbol,
            model_name  = self.name,
            direction   = "long",
            strength    = strength,
            entry_price = round(entry_price, 8),
            stop_loss   = round(stop_loss,   8),
            take_profit = round(take_profit,  8),
            timeframe   = timeframe,
            regime      = regime,
            rationale   = rationale,
            atr_value   = atr,
        )
