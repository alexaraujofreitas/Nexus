# ============================================================
# NEXUS TRADER — Liquidity Sweep Model (Sub-Model 4) — ENHANCED
#
# Active in: ANY regime (overlay filter — regime-agnostic)
# Detects: sharp spike that sweeps below a swing low (long)
#          or above a swing high (short), then immediately
#          reverses back — a classic liquidity grab pattern.
#
# Long setup:
#   - Current candle low < previous N-bar swing low
#   - Current candle CLOSE > swing low (reversal confirmed)
#   - Volume spike > 1.3× average
#
# Short setup: mirror
#
# ENHANCED: Incorporates LiquidationIntelligenceAgent data:
#   - If liq_density_long > 0.6 and signal is long: boost strength by 0.10
#   - If liq_density_short > 0.6 and signal is short: boost strength by 0.10
#   - If cascade_risk > 0.70: suppress signal (return None)
#
# Stop:  below sweep low (for long) / above sweep high (for short)
# Target: swing high of the pre-sweep range
# ============================================================
from __future__ import annotations

import logging
from typing import Optional
import pandas as pd

from config.settings import settings as _s
from core.signals.sub_models.base import BaseSubModel
from core.meta_decision.order_candidate import ModelSignal

logger = logging.getLogger(__name__)

# Module-level agent singleton
_liq_agent = None
_liq_agent_import_failed = False


def _get_liq_agent():
    """Lazy-load liquidation intelligence agent (avoid circular imports)."""
    global _liq_agent, _liq_agent_import_failed

    if _liq_agent_import_failed:
        return None

    if _liq_agent is not None:
        return _liq_agent

    try:
        from core.agents.liquidation_intelligence_agent import LiquidationIntelligenceAgent
        _liq_agent = LiquidationIntelligenceAgent(exchange=None)
        return _liq_agent
    except ImportError as e:
        logger.warning("LiquidationIntelligenceAgent import failed: %s (continuing without liquidation overlay)", e)
        _liq_agent_import_failed = True
        return None
    except Exception as e:
        logger.error("Unexpected error loading LiquidationIntelligenceAgent: %s", e)
        _liq_agent_import_failed = True
        return None


class LiquiditySweepModel(BaseSubModel):
    """
    Regime-agnostic liquidity sweep / stop-hunt detector.
    Enhanced with LiquidationIntelligenceAgent integration for cascade risk awareness.

    Higher affinity in ranging/accumulation where stop hunts are common.
    Lower affinity in trending regimes (sweeps are rarer in clean trends).
    """

    ACTIVE_REGIMES = []  # Empty = active in ALL regimes

    REGIME_AFFINITY: dict[str, float] = {
        "bull_trend": 0.4, "bear_trend": 0.6, "ranging": 0.9,
        "volatility_expansion": 0.5, "volatility_compression": 0.5,
        "uncertain": 0.4, "crisis": 0.2, "liquidation_cascade": 0.3,
        "squeeze": 0.5, "recovery": 0.5, "accumulation": 0.7, "distribution": 0.8,
    }

    SWING_LOOKBACK  = 15  # bars to define swing high/low
    MIN_SWEEP_PCT   = 0.10  # minimum sweep depth as % of price
    VOL_MULT_MIN    = 1.3   # minimum volume spike multiple

    @property
    def name(self) -> str:
        return "liquidity_sweep"

    def evaluate(
        self,
        symbol: str,
        df: pd.DataFrame,
        regime: str,
        timeframe: str,
    ) -> Optional[ModelSignal]:
        # Read tunable parameters from settings with fallback defaults
        _swing_lookback = int(_s.get('models.liquidity_sweep.swing_lookback', 15))
        _min_sweep_pct = float(_s.get('models.liquidity_sweep.min_sweep_pct', 0.10))
        _vol_mult_min = float(_s.get('models.liquidity_sweep.vol_mult_min', 1.3))
        _cascade_risk_cutoff = float(_s.get('models.liquidity_sweep.cascade_risk_cutoff', 0.70))
        _liq_density_threshold = float(_s.get('models.liquidity_sweep.liq_density_threshold', 0.60))

        if len(df) < _swing_lookback + 3:
            return None

        atr   = self._atr(df, 14)
        last  = df.iloc[-1]
        close = float(last["close"])
        low   = float(last["low"])
        high  = float(last["high"])

        # Swing levels from prior bars (excluding current)
        prior = df.iloc[-((_swing_lookback) + 1):-1]
        swing_low  = float(prior["low"].min())
        swing_high = float(prior["high"].max())

        # Volume confirmation
        vol_now  = float(last["volume"])
        vol_avg  = float(df["volume"].iloc[-((_swing_lookback)):-1].mean())
        vol_mult = vol_now / vol_avg if vol_avg > 0 else 0.0

        # Fetch liquidation state (optional)
        liq_state = None
        liq_agent = _get_liq_agent()
        if liq_agent:
            try:
                result = liq_agent.update(symbol, df, {})
                liq_state = result.get("liquidation_state")
            except Exception as e:
                logger.debug("LiquidationIntelligenceAgent.update() failed: %s", e)
                liq_state = None

        # Check cascade risk suppression first
        if liq_state and liq_state.get("cascade_risk", 0.0) > _cascade_risk_cutoff:
            logger.debug(
                "Signal suppressed for %s: cascade_risk=%.3f (>%.2f)",
                symbol, liq_state["cascade_risk"], _cascade_risk_cutoff,
            )
            return None

        rationale_parts: list[str] = []

        # ── Bullish sweep: wick below swing low, close above ─
        swept_low = low < swing_low
        reversal_up = close > swing_low
        sweep_depth_pct = (swing_low - low) / swing_low * 100 if swept_low else 0.0

        if swept_low and reversal_up and sweep_depth_pct >= _min_sweep_pct and vol_mult >= _vol_mult_min:
            rationale_parts.append(
                f"Wick swept below swing low ({swing_low:.4g}) by {sweep_depth_pct:.2f}% ✓"
            )
            rationale_parts.append(f"Closed back above sweep level ✓")
            rationale_parts.append(f"Volume {vol_mult:.1f}× avg ✓")

            depth_score = min(1.0, sweep_depth_pct / 1.0)
            vol_score   = min(1.0, (vol_mult - _vol_mult_min) / 2.0)
            strength    = min(1.0, 0.4 + depth_score * 0.35 + vol_score * 0.25)

            # Liquidity boost: if short liq density > threshold, boost by 0.10
            if liq_state and liq_state.get("liq_density_short", 0.0) > _liq_density_threshold:
                strength = min(1.0, strength + 0.10)
                rationale_parts.append(
                    f"Short liq density {liq_state['liq_density_short']:.2f} (>0.60) — strength boost ✓"
                )

            stop_loss   = low - atr * 0.5        # just below the wick
            take_profit = swing_high              # target: pre-sweep high
            direction   = "long"

            rationale = f"[Liquidity Sweep | {regime}] Bullish sweep: " + " | ".join(rationale_parts)

            return ModelSignal(
                symbol      = symbol,
                model_name  = self.name,
                direction   = direction,
                strength    = round(strength, 4),
                entry_price = close,
                stop_loss   = round(stop_loss, 8),
                take_profit = round(take_profit, 8),
                timeframe   = timeframe,
                regime      = regime,
                rationale   = rationale,
                atr_value   = atr,
            )

        # ── Bearish sweep: wick above swing high, close below ─
        swept_high = high > swing_high
        reversal_dn = close < swing_high
        sweep_height_pct = (high - swing_high) / swing_high * 100 if swept_high else 0.0

        if swept_high and reversal_dn and sweep_height_pct >= _min_sweep_pct and vol_mult >= _vol_mult_min:
            rationale_parts.append(
                f"Wick swept above swing high ({swing_high:.4g}) by {sweep_height_pct:.2f}% ✓"
            )
            rationale_parts.append("Closed back below sweep level ✓")
            rationale_parts.append(f"Volume {vol_mult:.1f}× avg ✓")

            depth_score = min(1.0, sweep_height_pct / 1.0)
            vol_score   = min(1.0, (vol_mult - _vol_mult_min) / 2.0)
            strength    = min(1.0, 0.4 + depth_score * 0.35 + vol_score * 0.25)

            # Liquidity boost: if long liq density > threshold, boost by 0.10
            if liq_state and liq_state.get("liq_density_long", 0.0) > _liq_density_threshold:
                strength = min(1.0, strength + 0.10)
                rationale_parts.append(
                    f"Long liq density {liq_state['liq_density_long']:.2f} (>0.60) — strength boost ✓"
                )

            stop_loss   = high + atr * 0.5
            take_profit = swing_low
            direction   = "short"

            rationale = f"[Liquidity Sweep | {regime}] Bearish sweep: " + " | ".join(rationale_parts)

            return ModelSignal(
                symbol      = symbol,
                model_name  = self.name,
                direction   = direction,
                strength    = round(strength, 4),
                entry_price = close,
                stop_loss   = round(stop_loss, 8),
                take_profit = round(take_profit, 8),
                timeframe   = timeframe,
                regime      = regime,
                rationale   = rationale,
                atr_value   = atr,
            )

        return None
