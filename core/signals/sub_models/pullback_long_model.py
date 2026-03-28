# ============================================================
# NEXUS TRADER — Pullback Long Model (Sub-Model 6)
#
# Active in: bull_trend
# Timeframe: 30m (primary); requires 4h HTF confirmation
#
# Logic — EXACT reproduction of Phase 5 backtest (v7_final):
#   Regime gate:    bull_trend only
#   4h HTF gate:    4h EMA20 > 4h EMA50  (bullish higher-TF bias)
#   EMA50 proximity: |close − EMA50| ≤ 0.5 × ATR14  (pulling back)
#   Rejection candle: close > open  (bullish candle body)
#   RSI gate:       RSI14 > 40  (momentum not oversold)
#   Stop-loss:      entry − 2.5 × ATR14
#   Take-profit:    entry + 3.0 × ATR14
#
# Strength calculation:
#   base      = 0.25
#   RSI bonus = (RSI14 − 40) / 60 × 0.25  (scaled 0→0.25 as RSI 40→100)
#   prox_bonus = max(0, 0.5 − prox_ratio) / 0.5 × 0.25 (closer → bonus)
#   HTF bonus = 0.15 if 4h EMA spread > 1%
#   Capped at 0.90
#
# HTF data injection:
#   The scanner passes pre-fetched 4h OHLCV as context["df_4h"].
#   If absent, HTF gate is bypassed with a debug warning
#   (graceful degradation, no hard failure).
# ============================================================
from __future__ import annotations

import logging
from typing import Optional
import pandas as pd

from core.signals.sub_models.base import BaseSubModel
from core.meta_decision.order_candidate import ModelSignal
from core.regime.regime_classifier import REGIME_BULL_TREND

logger = logging.getLogger(__name__)

# ── Tunable constants (can be overridden via config.yaml: mr_pbl_slc.*) ──────
_EMA50_PROX_ATR_MULT   = 0.5    # |close − EMA50| ≤ mult × ATR14
_SL_ATR_MULT           = 2.5
_TP_ATR_MULT           = 3.0
_RSI_MIN               = 40.0
_HTF_EMA_FAST          = 20     # 4h fast EMA for HTF gate
_HTF_EMA_SLOW          = 50     # 4h slow EMA for HTF gate


class PullbackLongModel(BaseSubModel):
    """
    Pullback-to-EMA50 long model active in bull_trend regime.

    Fires when price pulls back to the 30m EMA50 during a bullish 4h trend,
    then shows a rejection (bullish) candle with RSI above 40.
    """

    ACTIVE_REGIMES = [REGIME_BULL_TREND]

    # Pullback model waits for price to come to it — no entry buffer
    ENTRY_BUFFER_ATR: float = 0.0

    # High affinity only in bull_trend; near-zero elsewhere
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
        context : dict, optional
            May contain 'df_4h': pre-fetched 4h DataFrame with at least 60 rows,
            already through calculate_scan_mode(). If absent, HTF gate is skipped
            (graceful degradation — signal may still fire, at reduced strength).
        """
        # ── Regime guard (hard gate — evaluate() can be called directly in tests) ──
        if regime != REGIME_BULL_TREND:
            return None

        # ── Read config overrides ──────────────────────────────────────
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

        # Need at least 60 bars for EMA50
        if len(df) < 60:
            logger.debug("PBL %s: insufficient bars (%d < 60)", symbol, len(df))
            return None

        # ── Extract indicators ────────────────────────────────────────
        close = float(df["close"].iloc[-1])
        open_ = float(df["open"].iloc[-1])
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
                "PBL %s: prox fail |%.4f − %.4f| = %.4f > %.4f (%.1f×ATR)",
                symbol, close, ema50, prox_distance, prox_threshold, ema_prox_mult,
            )
            return None

        # ── Condition 2: Rejection (bullish) candle ───────────────────
        if close <= open_:
            logger.debug("PBL %s: non-bullish candle close=%.4f open=%.4f", symbol, close, open_)
            return None

        # ── Condition 3: RSI gate ─────────────────────────────────────
        if rsi <= rsi_min:
            logger.debug("PBL %s: RSI=%.1f ≤ %.1f", symbol, rsi, rsi_min)
            return None

        # ── Condition 4: 4h HTF gate ──────────────────────────────────
        htf_confirmed = False
        htf_strength_bonus = 0.0
        df_4h: Optional[pd.DataFrame] = (context or {}).get("df_4h")

        if df_4h is not None and len(df_4h) >= max(_HTF_EMA_FAST, _HTF_EMA_SLOW) + 5:
            try:
                # EMA columns should already be computed by calculate_scan_mode()
                ema_fast_col = f"ema_{_HTF_EMA_FAST}"
                ema_slow_col = f"ema_{_HTF_EMA_SLOW}"

                if ema_fast_col in df_4h.columns and ema_slow_col in df_4h.columns:
                    htf_ema_fast = float(df_4h[ema_fast_col].iloc[-1])
                    htf_ema_slow = float(df_4h[ema_slow_col].iloc[-1])
                else:
                    # Compute inline if missing
                    htf_close = df_4h["close"]
                    htf_ema_fast = float(htf_close.ewm(span=_HTF_EMA_FAST, adjust=False).mean().iloc[-1])
                    htf_ema_slow = float(htf_close.ewm(span=_HTF_EMA_SLOW, adjust=False).mean().iloc[-1])

                if htf_ema_fast > htf_ema_slow:
                    htf_confirmed = True
                    # Bonus proportional to spread — wider spread = stronger trend
                    spread_pct = (htf_ema_fast - htf_ema_slow) / htf_ema_slow
                    htf_strength_bonus = 0.15 if spread_pct > 0.01 else 0.08
                    logger.debug(
                        "PBL %s: 4h HTF gate PASS — EMA%d=%.2f > EMA%d=%.2f (spread=%.2f%%)",
                        symbol, _HTF_EMA_FAST, htf_ema_fast, _HTF_EMA_SLOW, htf_ema_slow,
                        spread_pct * 100,
                    )
                else:
                    logger.debug(
                        "PBL %s: 4h HTF gate FAIL — EMA%d=%.2f ≤ EMA%d=%.2f",
                        symbol, _HTF_EMA_FAST, htf_ema_fast, _HTF_EMA_SLOW, htf_ema_slow,
                    )
                    return None  # HTF gate required — reject if bearish 4h
            except Exception as exc:
                logger.warning("PBL %s: HTF gate compute error: %s — bypassing", symbol, exc)
                htf_confirmed = False  # bypass on error, don't block
        else:
            # No 4h data available — bypass HTF gate with warning
            logger.debug(
                "PBL %s: no 4h data in context (df_4h=%s) — HTF gate bypassed",
                symbol, type(df_4h).__name__ if df_4h is not None else "None",
            )
            # Still allow signal but with reduced strength (no HTF bonus)
            htf_confirmed = False

        # ── Strength calculation ──────────────────────────────────────
        strength = 0.25  # base for passing all 3 primary conditions

        # RSI bonus: RSI 40→100 maps to 0→0.25
        rsi_bonus = (min(rsi, 100.0) - rsi_min) / (100.0 - rsi_min) * 0.25
        strength += rsi_bonus

        # Proximity bonus: closer to EMA50 = better entry quality
        if prox_threshold > 0:
            prox_ratio = prox_distance / prox_threshold  # 0 (at EMA) → 1 (at threshold)
            prox_bonus = (1.0 - prox_ratio) * 0.25       # max 0.25 when exactly at EMA
        else:
            prox_bonus = 0.0
        strength += prox_bonus

        # HTF bonus
        strength += htf_strength_bonus

        strength = round(min(0.90, strength), 4)

        # ── Price levels ──────────────────────────────────────────────
        entry_price = close  # at-close market-order entry (no buffer for pullback)
        stop_loss   = entry_price - sl_mult * atr
        take_profit = entry_price + tp_mult * atr

        # ── Rationale ────────────────────────────────────────────────
        htf_str = "4h HTF confirmed ✓" if htf_confirmed else "4h HTF bypassed (no data)"
        rationale = (
            f"[PullbackLong | {regime}] "
            f"EMA50={ema50:.4f} prox={prox_distance:.4f}≤{prox_threshold:.4f}({ema_prox_mult}×ATR) ✓ | "
            f"Bullish candle close={close:.4f}>open={open_:.4f} ✓ | "
            f"RSI14={rsi:.1f}>{rsi_min:.0f} ✓ | "
            f"{htf_str} | "
            f"SL={stop_loss:.4f}(-{sl_mult}×ATR) TP={take_profit:.4f}(+{tp_mult}×ATR)"
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
            stop_loss   = round(stop_loss, 8),
            take_profit = round(take_profit, 8),
            timeframe   = timeframe,
            regime      = regime,
            rationale   = rationale,
            atr_value   = atr,
        )
