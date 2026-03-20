# ============================================================
# NEXUS TRADER — Signal Generator
#
# Orchestrates all sub-models for a single asset.
# Returns a list of ModelSignal objects (one per firing model).
# ============================================================
from __future__ import annotations

import logging
from typing import Optional
import pandas as pd

from core.signals.sub_models.trend_model             import TrendModel
from core.signals.sub_models.mean_reversion_model    import MeanReversionModel
from core.signals.sub_models.momentum_breakout_model import MomentumBreakoutModel
from core.signals.sub_models.vwap_reversion_model    import VWAPReversionModel
from core.signals.sub_models.liquidity_sweep_model   import LiquiditySweepModel
from core.signals.sub_models.funding_rate_model      import FundingRateModel
from core.signals.sub_models.order_book_model        import OrderBookModel
from core.signals.sub_models.sentiment_model         import SentimentModel
from core.meta_decision.order_candidate import ModelSignal

logger = logging.getLogger(__name__)

# RL ensemble (lazy init)
try:
    from core.rl.rl_signal_model import RLSignalModel
    _RL_MODEL_AVAILABLE = True
except Exception:
    _RL_MODEL_AVAILABLE = False

# All sub-models, instantiated once and reused
# Order matters: technical models first (regime-specific),
# then agent-derived models (regime-agnostic).
_ALL_MODELS = [
    TrendModel(),
    MeanReversionModel(),
    MomentumBreakoutModel(),
    VWAPReversionModel(),  # Phase 1c/1d — VWAP mean reversion
    LiquiditySweepModel(),
    FundingRateModel(),    # Sprint 1 — contrarian funding rate signal
    OrderBookModel(),      # Sprint 2 — L2 order book imbalance
    SentimentModel(),      # Sprint 4 — FinBERT / VADER news sentiment
]


class SignalGenerator:
    """
    Runs all appropriate sub-models for a given asset and regime,
    returning the list of signals that fired.
    """

    def __init__(self, models=None):
        self._models = models or _ALL_MODELS
        # RL ensemble (lazy init to avoid startup cost)
        self._rl_model = None
        if _RL_MODEL_AVAILABLE:
            try:
                self._rl_model = RLSignalModel()
            except Exception as exc:
                logger.debug("Failed to initialize RL signal model: %s", exc)
                self._rl_model = None
        # Regime warm-up guard
        self._warmup_bars_remaining: int = 100
        self._warmup_complete: bool = False
        # Custom model support
        self._custom_model = None

    def generate(
        self,
        symbol: str,
        df: pd.DataFrame,
        regime: str,
        timeframe: str,
        regime_probs: Optional[dict] = None,
    ) -> list[ModelSignal]:
        """
        Run all regime-appropriate sub-models and collect signals.

        Parameters
        ----------
        symbol    : trading pair
        df        : indicator DataFrame (last row = most recent closed candle)
        regime    : from RegimeClassifier
        timeframe : primary timeframe
        regime_probs : dict, optional
            Regime probability distribution for probabilistic model activation

        Returns
        -------
        List of ModelSignal objects (may be empty).
        """
        # Regime warm-up guard — suppress signals for first 100 candles after session start
        if not self._warmup_complete:
            self._warmup_bars_remaining = max(0, self._warmup_bars_remaining - 1)
            if self._warmup_bars_remaining == 0:
                self._warmup_complete = True
                logger.info("SignalGenerator: regime warm-up complete — signals now active")
            else:
                logger.debug("SignalGenerator: warm-up %d bars remaining", self._warmup_bars_remaining)
                return []

        # Fetch orchestrator context for sub-model awareness (A-1 fix)
        orch_direction = "neutral"
        orch_meta = 0.0
        try:
            from core.orchestrator.orchestrator_engine import get_orchestrator
            _orch = get_orchestrator()
            _orch_sig = _orch.get_signal()
            orch_direction = _orch_sig.direction
            orch_meta = _orch_sig.meta_signal
        except Exception:
            pass

        signals: list[ModelSignal] = []

        # ── Disabled models gate ─────────────────────────────────
        # Models listed in settings.disabled_models are skipped entirely.
        # This allows structural corrections (e.g. disabling underperforming
        # models) without code changes — just edit config.yaml.
        from config.settings import settings as _sc
        _disabled = set(_sc.get("disabled_models", []))

        for model in self._models:
            if model.name in _disabled:
                continue

            # Use probabilistic activation when regime_probs available
            min_activation = float(_sc.get("adaptive_activation.min_activation_weight", 0.10))
            if regime_probs and _sc.get("adaptive_activation.enabled", True):
                activation_wt = model.get_activation_weight(regime_probs)
                if activation_wt < min_activation:
                    logger.debug("SignalGenerator: skipping %s (activation_weight=%.3f < %.3f)",
                                 model.name, activation_wt, min_activation)
                    continue
            else:
                if not model.is_active_in_regime(regime):
                    logger.debug("SignalGenerator: skipping %s (not active in regime=%s)",
                                 model.name, regime)
                    continue
            try:
                sig = model.evaluate(symbol, df, regime, timeframe)
                if sig is not None:
                    signals.append(sig)
                    logger.info(
                        "Signal fired: %s | %s | %s | strength=%.2f",
                        model.name, symbol, sig.direction, sig.strength,
                    )
            except Exception as exc:
                logger.error(
                    "SignalGenerator: model %s raised error on %s: %s",
                    model.name, symbol, exc, exc_info=True,
                )

        # ── RL Ensemble signal injection ──────────────────────────────────
        try:
            if self._rl_model is not None:
                rl_sig = self._rl_model.evaluate(symbol, df, regime, timeframe)
                if rl_sig is not None:
                    signals.append(rl_sig)
                    logger.info(
                        "RL Signal fired: %s | %s | strength=%.2f",
                        symbol, rl_sig.direction, rl_sig.strength,
                    )
        except Exception as exc:
            logger.debug("SignalGenerator: RL model error on %s: %s", symbol, exc)

        # ── Custom model signal injection ──────────────────────────────────
        if self._custom_model is not None:
            try:
                custom_sigs = self._custom_model.generate(symbol, df, regime, timeframe)
                if custom_sigs:
                    signals.extend(custom_sigs)
            except Exception as exc:
                logger.debug("SignalGenerator: custom model error: %s", exc)

        return signals

    def reset_warmup(self, bars: int = 100) -> None:
        """Reset warm-up counter (call on exchange reconnect or session restart)."""
        self._warmup_bars_remaining = bars
        self._warmup_complete = False
        logger.info("SignalGenerator: warm-up reset (%d bars)", bars)

    def register_custom_model(self, model) -> None:
        """Register a custom model for signal generation."""
        self._custom_model = model
        logger.info("SignalGenerator: custom model '%s' registered", getattr(model, '_rule_name', model.__class__.__name__))

    def unregister_custom_model(self) -> None:
        """Unregister the custom model."""
        self._custom_model = None
        logger.info("SignalGenerator: custom model unregistered")


_generator_instance = None

def get_signal_generator():
    global _generator_instance
    if _generator_instance is None:
        _generator_instance = SignalGenerator()
    return _generator_instance
