# ============================================================
# NEXUS TRADER — RangeAccumulationModel (Sub-Model 8)
#
# Session 51 — Phase 2 Regime-Orchestrated Architecture
#
# Active in: ranging, accumulation — ACTIVE_REGIMES gate enforced
# Timeframe: 30m (primary)
#
# Thesis:
#   In ranging/accumulation regimes price oscillates between
#   identifiable support and resistance.  This model detects:
#   - Oversold bounces near validated range lows
#   - Overbought rejections near validated range highs
#   Confirmed by RSI extremes, declining ADX, volume contraction,
#   and robust range detection (touch counting, drift stability).
#
# Range Detection (robust, not naive BB):
#   1. Rolling lookback window (default 30 bars)
#   2. range_high = max(highs), range_low = min(lows)
#   3. Validity checks:
#      a) min_touches: ≥3 closes within tolerance of high/low
#      b) low drift: range width stable (no expanding ranges)
#      c) breakout rejection: skip if range already breached
#
# Entry Logic:
#   Long:  price near range_low + rejection candle + RSI < oversold
#   Short: price near range_high + rejection candle + RSI > overbought
#
# Filters:
#   - Volume contraction (declining vs recent average)
#   - ATR spike filter (reject high-volatility entries)
#   - Breakout-in-progress guard (no entry if range breached)
#
# Exit Logic (encoded in ModelSignal metadata):
#   - SL: outside range boundary (wide enough for noise)
#   - TP: near opposite range boundary (conservative)
#   - ModelSignal.rationale carries range_high/range_low for
#     downstream partial-exit and breakout-trailing logic
#
# Config gate: phase_2.range_accumulation.enabled (default False)
# ============================================================
from __future__ import annotations

import logging
from typing import Optional

import numpy as np
import pandas as pd

from core.signals.sub_models.base import BaseSubModel
from core.meta_decision.order_candidate import ModelSignal

logger = logging.getLogger(__name__)


class RangeAccumulationModel(BaseSubModel):
    """
    Mean-reversion model targeting ranging and accumulation regimes.

    Uses robust range detection (touch counting, drift stability) to
    identify high/low boundaries, then enters on rejection candles
    near those boundaries with RSI confirmation and volume contraction.

    Key parameters (configurable via config.yaml under phase_2.range_accumulation):
    - lookback: rolling window for range detection (bars)
    - touch_tolerance_atr: how close to boundary counts as a "touch" (ATR mult)
    - min_touches: minimum touches near high/low to validate range
    - max_drift_pct: maximum allowed range width change (stability check)
    - rsi_oversold / rsi_overbought: RSI extremes required
    - adx_max: maximum ADX value (confirms no trend)
    - sl_atr_mult / tp_mode: stop loss ATR mult; TP targets opposite boundary
    - volume_contraction_mult: reject if volume > this × avg (breakout likely)
    - atr_spike_mult: reject if current ATR > this × rolling avg ATR
    - max_hold_bars: maximum bars to hold (stagnation protection)
    """

    # Only fires in ranging and accumulation regimes — hard gate
    ACTIVE_REGIMES = ["ranging", "accumulation"]

    # Regime affinity — high in ranging/accumulation, zero in crisis
    REGIME_AFFINITY = {
        "bull_trend": 0.05,
        "bear_trend": 0.05,
        "ranging": 1.0,
        "volatility_expansion": 0.05,
        "volatility_compression": 0.6,
        "uncertain": 0.3,
        "crisis": 0.0,
        "liquidation_cascade": 0.0,
        "squeeze": 0.2,
        "recovery": 0.3,
        "accumulation": 1.0,
        "distribution": 0.4,
    }

    # Mean-reversion ATR multipliers — wider stops in range
    REGIME_ATR_MULTIPLIERS = {
        "ranging": 2.5,
        "accumulation": 2.5,
        "volatility_compression": 2.0,
        "uncertain": 2.0,
        "distribution": 2.5,
        "recovery": 2.0,
    }

    # Mean-reversion: wait for slightly better fill
    ENTRY_BUFFER_ATR = -0.05

    # ── Default parameters (overridable via config.yaml) ─────────
    _LOOKBACK             = 30      # Rolling window for range detection (bars)
    _TOUCH_TOLERANCE_ATR  = 0.3     # Close within 0.3×ATR of boundary = "touch"
    _MIN_TOUCHES          = 3       # Minimum touches near high/low to validate
    _MAX_DRIFT_PCT        = 0.15    # Range width change > 15% = unstable
    _RSI_OVERSOLD         = 35.0
    _RSI_OVERBOUGHT       = 65.0
    _ADX_MAX              = 25.0    # Must be below trend threshold
    _SL_ATR_MULT          = 2.5     # Stop-loss ATR multiplier (outside range)
    _VOL_CONTRACTION_MULT = 2.0     # Reject if volume > 2× average (breakout)
    _ATR_SPIKE_MULT       = 1.5     # Reject if ATR > 1.5× rolling avg
    _WICK_STRENGTH        = 1.0     # Rejection candle: wick ≥ mult × body
    _MAX_HOLD_BARS        = 40      # Stagnation protection
    _STRENGTH_BASE        = 0.30
    _ENTRY_PROXIMITY_ATR  = 0.5     # Must be within this × ATR of boundary

    @property
    def name(self) -> str:
        return "range_accumulation"

    def evaluate(
        self,
        symbol: str,
        df: pd.DataFrame,
        regime: str,
        timeframe: str,
        context: Optional[dict] = None,
    ) -> Optional[ModelSignal]:
        """
        Evaluate mean-reversion entry within a validated ranging/accumulation range.

        Returns ModelSignal for:
        - Long: price near range_low + rejection candle + RSI oversold + ADX low + vol normal
        - Short: price near range_high + rejection candle + RSI overbought + ADX low + vol normal
        """
        # ── Regime guard (redundant with ACTIVE_REGIMES but defensive) ──
        if regime not in ("ranging", "accumulation"):
            return None

        # ── Read config parameters ───────────────────────────────
        try:
            from config.settings import settings as _s
            _cfg = "phase_2.range_accumulation"
            lookback           = int(_s.get(f"{_cfg}.lookback", self._LOOKBACK))
            touch_tol_atr      = float(_s.get(f"{_cfg}.touch_tolerance_atr", self._TOUCH_TOLERANCE_ATR))
            min_touches        = int(_s.get(f"{_cfg}.min_touches", self._MIN_TOUCHES))
            max_drift_pct      = float(_s.get(f"{_cfg}.max_drift_pct", self._MAX_DRIFT_PCT))
            rsi_oversold       = float(_s.get(f"{_cfg}.rsi_oversold", self._RSI_OVERSOLD))
            rsi_overbought     = float(_s.get(f"{_cfg}.rsi_overbought", self._RSI_OVERBOUGHT))
            adx_max            = float(_s.get(f"{_cfg}.adx_max", self._ADX_MAX))
            sl_atr_mult        = float(_s.get(f"{_cfg}.sl_atr_mult", self._SL_ATR_MULT))
            vol_contraction    = float(_s.get(f"{_cfg}.volume_contraction_mult", self._VOL_CONTRACTION_MULT))
            atr_spike_mult     = float(_s.get(f"{_cfg}.atr_spike_mult", self._ATR_SPIKE_MULT))
            wick_strength      = float(_s.get(f"{_cfg}.wick_strength", self._WICK_STRENGTH))
            max_hold_bars      = int(_s.get(f"{_cfg}.max_hold_bars", self._MAX_HOLD_BARS))
            strength_base      = float(_s.get(f"{_cfg}.strength_base", self._STRENGTH_BASE))
            entry_prox_atr     = float(_s.get(f"{_cfg}.entry_proximity_atr", self._ENTRY_PROXIMITY_ATR))
        except Exception:
            lookback           = self._LOOKBACK
            touch_tol_atr      = self._TOUCH_TOLERANCE_ATR
            min_touches        = self._MIN_TOUCHES
            max_drift_pct      = self._MAX_DRIFT_PCT
            rsi_oversold       = self._RSI_OVERSOLD
            rsi_overbought     = self._RSI_OVERBOUGHT
            adx_max            = self._ADX_MAX
            sl_atr_mult        = self._SL_ATR_MULT
            vol_contraction    = self._VOL_CONTRACTION_MULT
            atr_spike_mult     = self._ATR_SPIKE_MULT
            wick_strength      = self._WICK_STRENGTH
            max_hold_bars      = self._MAX_HOLD_BARS
            strength_base      = self._STRENGTH_BASE
            entry_prox_atr     = self._ENTRY_PROXIMITY_ATR

        # ── Minimum data check ───────────────────────────────────
        min_bars = lookback + 10
        if len(df) < min_bars:
            logger.debug("RAM %s: insufficient bars (%d < %d)", symbol, len(df), min_bars)
            return None

        # ── Extract indicators ───────────────────────────────────
        close = float(df["close"].iloc[-1])
        open_ = float(df["open"].iloc[-1])
        high_ = float(df["high"].iloc[-1])
        low_  = float(df["low"].iloc[-1])
        atr   = self._atr(df, 14)

        rsi = self._col(df, "rsi_14")
        if rsi is None:
            logger.debug("RAM %s: missing rsi_14", symbol)
            return None

        adx = self._col(df, "adx")
        if adx is None:
            logger.debug("RAM %s: missing adx", symbol)
            return None

        # ── Filter 1: ADX gate — must be below max (no trend) ────
        if adx >= adx_max:
            logger.debug("RAM %s: ADX=%.1f >= %.1f — trend detected", symbol, adx, adx_max)
            return None

        # ── Filter 2: ATR spike filter — reject volatile entries ─
        atr_col = "atr_14"
        if atr_col in df.columns and len(df) >= 20:
            atr_series = df[atr_col].tail(20)
            atr_avg = float(atr_series.mean())
            if atr_avg > 0 and atr > atr_spike_mult * atr_avg:
                logger.debug(
                    "RAM %s: ATR spike %.4f > %.1f × avg %.4f — skip",
                    symbol, atr, atr_spike_mult, atr_avg,
                )
                return None

        # ── Filter 3: Volume contraction — reject breakout volume ─
        vol = self._col(df, "volume")
        vol_sma = None
        if "volume_sma_20" in df.columns:
            vol_sma = self._col(df, "volume_sma_20")
        elif vol is not None and len(df) >= 20:
            vol_sma = float(df["volume"].tail(20).mean())

        vol_ratio = 1.0
        if vol is not None and vol_sma is not None and vol_sma > 0:
            vol_ratio = vol / vol_sma
            if vol_ratio > vol_contraction:
                logger.debug(
                    "RAM %s: vol_ratio=%.2f > %.2f — volume spike, skip",
                    symbol, vol_ratio, vol_contraction,
                )
                return None

        # ════════════════════════════════════════════════════════════
        # RANGE DETECTION — robust multi-check validation
        # ════════════════════════════════════════════════════════════

        window = df.tail(lookback)
        highs  = window["high"].values.astype(np.float64)
        lows   = window["low"].values.astype(np.float64)
        closes = window["close"].values.astype(np.float64)

        # Use percentile-based boundaries for robust range detection.
        # Absolute max/min is skewed by single wicks — the 90th/10th
        # percentile of closes gives boundaries that price actually visits.
        range_high = float(np.percentile(closes, 90))
        range_low  = float(np.percentile(closes, 10))
        range_width = range_high - range_low

        if range_width <= 0 or range_width < atr * 0.5:
            logger.debug("RAM %s: range too narrow (width=%.4f, atr=%.4f)", symbol, range_width, atr)
            return None

        # ── Range validity: touch counting ───────────────────────
        # Count closes within tolerance of range boundaries
        tolerance = touch_tol_atr * atr

        touches_high = int(np.sum(closes >= (range_high - tolerance)))
        touches_low  = int(np.sum(closes <= (range_low + tolerance)))

        if touches_high < min_touches:
            logger.debug(
                "RAM %s: insufficient high touches (%d < %d, tol=%.4f)",
                symbol, touches_high, min_touches, tolerance,
            )
            return None

        if touches_low < min_touches:
            logger.debug(
                "RAM %s: insufficient low touches (%d < %d, tol=%.4f)",
                symbol, touches_low, min_touches, tolerance,
            )
            return None

        # ── Range validity: drift stability ──────────────────────
        # Compare range width of first half vs second half
        half = lookback // 2
        if half >= 5:
            first_half_width = float(np.max(highs[:half]) - np.min(lows[:half]))
            second_half_width = float(np.max(highs[half:]) - np.min(lows[half:]))
            if first_half_width > 0:
                drift = abs(second_half_width - first_half_width) / first_half_width
                if drift > max_drift_pct:
                    logger.debug(
                        "RAM %s: range unstable drift=%.2f > %.2f (w1=%.4f, w2=%.4f)",
                        symbol, drift, max_drift_pct, first_half_width, second_half_width,
                    )
                    return None

        # ── Range validity: breakout check ───────────────────────
        # If the current bar has breached the absolute range extremes, skip
        # (breakout-in-progress — do NOT enter mean-reversion)
        abs_high = float(np.max(highs))
        abs_low  = float(np.min(lows))
        breach_tolerance = atr * 0.5
        if high_ > abs_high + breach_tolerance:
            logger.debug("RAM %s: upside breach high=%.4f > abs_high=%.4f + tol", symbol, high_, abs_high)
            return None
        if low_ < abs_low - breach_tolerance:
            logger.debug("RAM %s: downside breach low=%.4f < abs_low=%.4f - tol", symbol, low_, abs_low)
            return None

        # ════════════════════════════════════════════════════════════
        # ENTRY LOGIC — direction determination
        # ════════════════════════════════════════════════════════════

        direction = None
        rationale_parts = []

        # Distance from boundaries
        dist_to_low  = close - range_low
        dist_to_high = range_high - close

        # ── LONG: near range_low + rejection candle + RSI oversold ─
        if dist_to_low <= entry_prox_atr * atr and rsi < rsi_oversold:
            # Rejection candle check: bullish body + long lower wick
            body = abs(close - open_)
            lower_wick = min(close, open_) - low_
            upper_wick = high_ - max(close, open_)

            if close > open_ and lower_wick > upper_wick and (body == 0 or lower_wick >= wick_strength * body):
                direction = "long"
                rationale_parts.append(f"near range_low={range_low:.4f} (dist={dist_to_low:.4f})")
                rationale_parts.append(f"RSI={rsi:.1f} (< {rsi_oversold})")
                rationale_parts.append(f"rejection: lw={lower_wick:.4f} uw={upper_wick:.4f} body={body:.4f}")
            else:
                logger.debug(
                    "RAM %s: near range_low but no rejection candle (c=%.4f o=%.4f lw=%.4f)",
                    symbol, close, open_, lower_wick if close > open_ else 0,
                )
                return None

        # ── SHORT: near range_high + rejection candle + RSI overbought ─
        elif dist_to_high <= entry_prox_atr * atr and rsi > rsi_overbought:
            # Rejection candle check: bearish body + long upper wick
            body = abs(close - open_)
            lower_wick = min(close, open_) - low_
            upper_wick = high_ - max(close, open_)

            if close < open_ and upper_wick > lower_wick and (body == 0 or upper_wick >= wick_strength * body):
                direction = "short"
                rationale_parts.append(f"near range_high={range_high:.4f} (dist={dist_to_high:.4f})")
                rationale_parts.append(f"RSI={rsi:.1f} (> {rsi_overbought})")
                rationale_parts.append(f"rejection: uw={upper_wick:.4f} lw={lower_wick:.4f} body={body:.4f}")
            else:
                logger.debug(
                    "RAM %s: near range_high but no rejection candle (c=%.4f o=%.4f uw=%.4f)",
                    symbol, close, open_, upper_wick if close < open_ else 0,
                )
                return None
        else:
            # Not near either boundary — no entry
            return None

        # ── Common rationale ─────────────────────────────────────
        rationale_parts.append(f"ADX={adx:.1f} (< {adx_max})")
        rationale_parts.append(f"vol_ratio={vol_ratio:.2f}")
        rationale_parts.append(f"touches: high={touches_high} low={touches_low} (min={min_touches})")
        rationale_parts.append(f"range=[{range_low:.4f}, {range_high:.4f}] width={range_width:.4f}")

        # ════════════════════════════════════════════════════════════
        # STRENGTH CALCULATION
        # ════════════════════════════════════════════════════════════

        # Base strength
        strength = strength_base

        # RSI extremity bonus (0 – 0.20)
        if direction == "long":
            rsi_extremity = max(0, (rsi_oversold - rsi) / rsi_oversold) * 0.20
        else:
            rsi_extremity = max(0, (rsi - rsi_overbought) / (100.0 - rsi_overbought)) * 0.20
        strength += rsi_extremity

        # Boundary proximity bonus (0 – 0.15): closer = stronger
        if direction == "long":
            prox_norm = max(0, 1.0 - dist_to_low / (entry_prox_atr * atr)) if entry_prox_atr * atr > 0 else 0
        else:
            prox_norm = max(0, 1.0 - dist_to_high / (entry_prox_atr * atr)) if entry_prox_atr * atr > 0 else 0
        strength += prox_norm * 0.15

        # ADX weakness bonus (0 – 0.10): lower ADX = stronger confirmation
        adx_bonus = max(0, (adx_max - adx) / adx_max) * 0.10
        strength += adx_bonus

        # Touch count bonus (0 – 0.10): more touches = stronger range
        avg_touches = (touches_high + touches_low) / 2.0
        touch_bonus = min(0.10, (avg_touches - min_touches) / 5.0 * 0.10) if avg_touches > min_touches else 0
        strength += touch_bonus

        strength = round(min(0.90, strength), 4)

        # ════════════════════════════════════════════════════════════
        # SL / TP / ENTRY CALCULATION
        # ════════════════════════════════════════════════════════════

        entry = self._entry_price(close, atr, direction)

        if direction == "long":
            # SL below range low (outside the range, wide enough for noise)
            sl = range_low - sl_atr_mult * atr
            # TP near opposite boundary (slightly inside to be conservative)
            tp = range_high - atr * 0.2
        else:
            # SL above range high (outside the range)
            sl = range_high + sl_atr_mult * atr
            # TP near opposite boundary
            tp = range_low + atr * 0.2

        # Validate geometry: long: sl < entry < tp; short: tp < entry < sl
        if direction == "long" and not (sl < entry < tp):
            logger.debug("RAM %s: invalid long levels sl=%.4f ep=%.4f tp=%.4f", symbol, sl, entry, tp)
            return None
        if direction == "short" and not (tp < entry < sl):
            logger.debug("RAM %s: invalid short levels tp=%.4f ep=%.4f sl=%.4f", symbol, tp, entry, sl)
            return None

        # ── Build rationale ──────────────────────────────────────
        rationale_parts.append(f"max_hold={max_hold_bars}")
        rationale = (
            f"[RAM | regime={regime}] {direction.upper()} — "
            + ", ".join(rationale_parts)
        )

        logger.info(
            "RAM SIGNAL: %s %s str=%.3f entry=%.4f sl=%.4f tp=%.4f | "
            "range=[%.4f, %.4f] touches_h=%d touches_l=%d ADX=%.1f RSI=%.1f",
            symbol, direction, strength, entry, sl, tp,
            range_low, range_high, touches_high, touches_low, adx, rsi,
        )

        return ModelSignal(
            symbol=symbol,
            model_name=self.name,
            direction=direction,
            strength=round(strength, 4),
            entry_price=round(entry, 8),
            stop_loss=round(sl, 8),
            take_profit=round(tp, 8),
            timeframe=timeframe,
            regime=regime,
            rationale=rationale,
            atr_value=atr,
        )
