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

from core.signals.sub_models.trend_model                   import TrendModel
from core.signals.sub_models.momentum_breakout_model       import MomentumBreakoutModel
from core.signals.sub_models.donchian_breakout_model       import DonchianBreakoutModel
from core.signals.sub_models.funding_rate_model            import FundingRateModel
from core.signals.sub_models.sentiment_model               import SentimentModel
from core.signals.sub_models.pullback_long_model           import PullbackLongModel
from core.signals.sub_models.swing_low_continuation_model  import SwingLowContinuationModel
from core.meta_decision.order_candidate import ModelSignal

# v1.2 archived models — kept as importable modules for tests and historical
# analysis, but excluded from _ALL_MODELS so they are never instantiated or
# evaluated at runtime.  Re-enable via the disabled_models config list only
# after out-of-sample validation on live demo data (75+ trades).
#
# Archived reason (Study 4 / Phase 5):
#   MeanReversionModel   — PF 0.21 (-$18k), WR 32.2%  → disabled 2026-03-01
#   VWAPReversionModel   — PF 0.28             → disabled 2026-03-24
#   LiquiditySweepModel  — PF 0.28 (-$15k), WR 19.3%  → disabled 2026-03-01
#   OrderBookModel       — structural TF gate (PF ≤1.0 at 1h+)  → disabled 2026-03-01
#
# To re-import for tests:
#   from core.signals.sub_models.mean_reversion_model   import MeanReversionModel
#   from core.signals.sub_models.vwap_reversion_model   import VWAPReversionModel
#   from core.signals.sub_models.liquidity_sweep_model  import LiquiditySweepModel
#   from core.signals.sub_models.order_book_model       import OrderBookModel

logger = logging.getLogger(__name__)

# RL ensemble (lazy init)
try:
    from core.rl.rl_signal_model import RLSignalModel
    _RL_MODEL_AVAILABLE = True
except Exception:
    _RL_MODEL_AVAILABLE = False

# Active models — instantiated once and reused across all scan cycles.
# v1.2: Only TrendModel + MomentumBreakout active (Study 4+5 validated).
# FundingRateModel and SentimentModel remain for context enrichment (low weight).
# v1.3 (Phase 6): PullbackLongModel + SwingLowContinuationModel added.
#   Gated behind mr_pbl_slc.enabled in config.yaml (default: false).
#   Activated only after Phase 1 50-trade milestone — see integration_plan.md.
# Session 48: TrendModel disabled (net-negative at fees — PF 0.9592).
#   DonchianBreakoutModel added as replacement research candidate.
#   Gated by disabled_models config — not active in production until backtest validated.
_ALL_MODELS = [
    TrendModel(),
    MomentumBreakoutModel(),
    DonchianBreakoutModel(),        # Session 48 — replacement research candidate for Trend
    FundingRateModel(),             # Sprint 1 — contrarian funding rate signal
    SentimentModel(),               # Sprint 4 — FinBERT / VADER news sentiment
    PullbackLongModel(),            # v1.3 — 30m bull_trend pullback (Phase 5 validated)
    SwingLowContinuationModel(),    # v1.3 — 1h bear_trend swing-low continuation (Phase 5 validated)
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
        # Phase 3.3 Opt: cache inspect.signature() per model at init time.
        # inspect.signature(model.evaluate) was called 506,775 times inside
        # generate() costing ~10s per simulation run.  Caching at init reduces
        # this to a single dict lookup per model per generate() call (~0ns).
        import inspect as _inspect
        self._model_has_context: dict = {
            model: "context" in _inspect.signature(model.evaluate).parameters
            for model in self._models
        }
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
        context: Optional[dict] = None,
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
        context : dict, optional
            Extra data passed to models that need it.  Currently used by:
              - PullbackLongModel:          context["df_4h"] — 4h OHLCV DataFrame
              - SwingLowContinuationModel:  context["df_1h"] — 1h OHLCV DataFrame
            Both DataFrames should be run through calculate_scan_mode() first.

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
        # v3: Cached reference avoids per-call import lock contention.
        orch_direction = "neutral"
        orch_meta = 0.0
        try:
            if not hasattr(self, "_orch_ref"):
                from core.orchestrator.orchestrator_engine import get_orchestrator
                self._orch_ref = get_orchestrator()
            if self._orch_ref is not None:
                _orch_sig = self._orch_ref.get_signal()
                orch_direction = _orch_sig.direction
                orch_meta = _orch_sig.meta_signal
        except Exception:
            pass

        signals: list[ModelSignal] = []

        # ── Phase 3.4 Opt: read all settings ONCE before the model loop ──────
        from config.settings import settings as _sc
        _disabled       = set(_sc.get("disabled_models", []))
        _adaptive_en    = bool(_sc.get("adaptive_activation.enabled", True))
        _min_activation = float(_sc.get("adaptive_activation.min_activation_weight", 0.10))
        _mr_enabled     = bool(_sc.get("mr_pbl_slc.enabled", False))

        # ── Disabled models gate ─────────────────────────────────
        # Models listed in settings.disabled_models are skipped entirely.
        # This allows structural corrections (e.g. disabling underperforming
        # models) without code changes — just edit config.yaml.

        for model in self._models:
            if model.name in _disabled:
                continue

            # Check model_performance_tracker auto-disable
            # v3: Cached reference avoids per-model import lock contention.
            try:
                if not hasattr(self, "_mpt_ref"):
                    from core.analytics.model_performance_tracker import get_model_performance_tracker
                    self._mpt_ref = get_model_performance_tracker()
                if self._mpt_ref is not None:
                    _should_disable, _disable_reason = self._mpt_ref.should_auto_disable(model.name)
                    if _should_disable:
                        logger.warning("SignalGenerator: auto-disabling %s (%s)", model.name, _disable_reason)
                        _disabled.add(model.name)
                        continue
            except Exception:
                pass

            # ── Hard ACTIVE_REGIMES gate (Wave 3 bug fix) ──────────────────
            # ACTIVE_REGIMES is a hard constraint — it must be respected regardless
            # of whether adaptive_activation is enabled.  REGIME_AFFINITY values
            # adjust weights *within* the allowed regime set; they must not expand it.
            #
            # Bug that this fixes: when adaptive_activation.enabled=True the code
            # below used get_activation_weight() exclusively, bypassing
            # is_active_in_regime() entirely.  MomentumBreakout (ACTIVE_REGIMES=
            # [vol_expansion]) was firing in ranging/uncertain/vol_compression
            # because its REGIME_AFFINITY values for those regimes (0.1–0.2) were
            # ≥ min_activation_weight (0.1), making the < comparison False.
            # Confirmed result: 7 trades, 0% WR in those out-of-regime conditions.
            if model.ACTIVE_REGIMES and not model.is_active_in_regime(regime):
                logger.debug(
                    "SignalGenerator: hard-gating %s "
                    "(regime=%s not in ACTIVE_REGIMES, adaptive_activation cannot override)",
                    model.name, regime,
                )
                continue

            # Use probabilistic activation when regime_probs available
            # (applies within the set already validated by ACTIVE_REGIMES above)
            # Phase 3.4 Opt: use pre-read _adaptive_en / _min_activation (no settings.get() here)
            if regime_probs and _adaptive_en:
                activation_wt = model.get_activation_weight(regime_probs)
                if activation_wt < _min_activation:
                    logger.debug("SignalGenerator: skipping %s (activation_weight=%.3f < %.3f)",
                                 model.name, activation_wt, _min_activation)
                    continue
            else:
                if not model.is_active_in_regime(regime):
                    logger.debug("SignalGenerator: skipping %s (not active in regime=%s)",
                                 model.name, regime)
                    continue
            # ── mr_pbl_slc gate ────────────────────────────────────────────
            # PBL and SLC are gated behind mr_pbl_slc.enabled (default: false).
            # This prevents firing until Phase 1 milestone is reached and the
            # operator manually sets enabled=true in config.yaml.
            _pbl_slc_names = {"pullback_long", "swing_low_continuation"}
            # Phase 3.4 Opt: use pre-read _mr_enabled (no settings.get() per model)
            if model.name in _pbl_slc_names and not _mr_enabled:
                logger.debug(
                    "SignalGenerator: %s gated (mr_pbl_slc.enabled=false)", model.name
                )
                continue

            try:
                # Pass context to models that accept it (PBL and SLC).
                # Phase 3.3 Opt: use cached dict lookup instead of per-call
                # inspect.signature() which was called 506,775×/sim (~10s).
                if self._model_has_context.get(model, False):
                    sig = model.evaluate(symbol, df, regime, timeframe, context=context)
                else:
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
                    rl_shadow_only = _sc.get("rl.shadow_only", True)
                    if not rl_shadow_only:
                        signals.append(rl_sig)
                    logger.info(
                        "RL shadow: %s | %s | strength=%.2f (shadow_only=%s)",
                        symbol, rl_sig.direction, rl_sig.strength, rl_shadow_only,
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
