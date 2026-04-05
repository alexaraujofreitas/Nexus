# ============================================================
# NEXUS TRADER — Confluence Scorer
#
# Converts a list of ModelSignals into a single OrderCandidate
# using weighted model voting.
#
# Model weights:
#   trend              0.35
#   mean_reversion     0.25
#   momentum_breakout  0.25
#   liquidity_sweep    0.15
#
# Only models that FIRED participate. Weights are renormalized
# so they always sum to 1.0 among active models.
#
# Threshold: score > 0.55 → generate OrderCandidate
# ============================================================
from __future__ import annotations

import json
import logging
import math
import threading
import time as _time
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
import pandas as pd

from core.meta_decision.order_candidate import ModelSignal, OrderCandidate
from core.meta_decision.position_sizer import PositionSizer
from config.settings import settings as _s

logger = logging.getLogger(__name__)

# Module-level cached references to avoid import lock contention in score().
# Python's import lock blocks ALL threads when any thread is importing.
# FinBERT/HuggingFace model loading can hold the import lock for 30+ seconds,
# causing the scanner thread to hang inside lazy imports.
_adaptive_engine_ref = None

def _get_adaptive_engine_safe():
    """Get AdaptiveWeightEngine without lazy import in hot path."""
    global _adaptive_engine_ref
    if _adaptive_engine_ref is None:
        try:
            from core.learning.adaptive_weight_engine import get_adaptive_weight_engine
            _adaptive_engine_ref = get_adaptive_weight_engine()
        except Exception:
            return None
    return _adaptive_engine_ref


# Regime affinity per model: how much weight to give each model when regime prob is that regime
REGIME_AFFINITY: dict[str, dict[str, float]] = {
    "trend":              {"bull_trend": 1.0, "bear_trend": 0.9, "ranging": 0.1, "volatility_expansion": 0.25, "volatility_compression": 0.2, "uncertain": 0.3, "crisis": 0.0, "liquidation_cascade": 0.0, "squeeze": 0.3, "recovery": 0.7, "accumulation": 0.2, "distribution": 0.2},
    "mean_reversion":     {"bull_trend": 0.05, "bear_trend": 0.08, "ranging": 1.0, "volatility_expansion": 0.02, "volatility_compression": 0.8, "uncertain": 0.20, "crisis": 0.0, "liquidation_cascade": 0.0, "squeeze": 0.4, "recovery": 0.4, "accumulation": 0.8, "distribution": 0.7},
    "momentum_breakout":  {"bull_trend": 0.7, "bear_trend": 0.7, "ranging": 0.1, "volatility_expansion": 0.70, "volatility_compression": 0.1, "uncertain": 0.2, "crisis": 0.0, "liquidation_cascade": 0.0, "squeeze": 0.8, "recovery": 0.6, "accumulation": 0.3, "distribution": 0.4},
    "vwap_reversion":     {"bull_trend": 0.5, "bear_trend": 0.5, "ranging": 0.8, "volatility_expansion": 0.15, "volatility_compression": 0.7, "uncertain": 0.5, "crisis": 0.1, "liquidation_cascade": 0.1, "squeeze": 0.4, "recovery": 0.5, "accumulation": 0.7, "distribution": 0.6},
    "liquidity_sweep":    {"bull_trend": 0.4, "bear_trend": 0.6, "ranging": 0.9, "volatility_expansion": 0.25, "volatility_compression": 0.5, "uncertain": 0.4, "crisis": 0.2, "liquidation_cascade": 0.3, "squeeze": 0.5, "recovery": 0.5, "accumulation": 0.7, "distribution": 0.8},
    "funding_rate":       {"bull_trend": 0.8, "bear_trend": 0.8, "ranging": 0.5, "volatility_expansion": 0.7, "volatility_compression": 0.4, "uncertain": 0.5, "crisis": 0.6, "liquidation_cascade": 0.7, "squeeze": 0.8, "recovery": 0.7, "accumulation": 0.5, "distribution": 0.6},
    "order_book":         {"bull_trend": 0.7, "bear_trend": 0.7, "ranging": 0.6, "volatility_expansion": 0.5, "volatility_compression": 0.6, "uncertain": 0.5, "crisis": 0.3, "liquidation_cascade": 0.2, "squeeze": 0.6, "recovery": 0.6, "accumulation": 0.6, "distribution": 0.7},
    "sentiment":          {"bull_trend": 0.9, "bear_trend": 0.7, "ranging": 0.4, "volatility_expansion": 0.5, "volatility_compression": 0.3, "uncertain": 0.4, "crisis": 0.2, "liquidation_cascade": 0.1, "squeeze": 0.4, "recovery": 0.8, "accumulation": 0.7, "distribution": 0.3},
    "rl_ensemble":        {"bull_trend": 0.8, "bear_trend": 0.8, "ranging": 0.7, "volatility_expansion": 0.6, "volatility_compression": 0.6, "uncertain": 0.5, "crisis": 0.0, "liquidation_cascade": 0.0, "squeeze": 0.5, "recovery": 0.7, "accumulation": 0.7, "distribution": 0.6},
    "orchestrator":       {"bull_trend": 0.8, "bear_trend": 0.8, "ranging": 0.7, "volatility_expansion": 0.7, "volatility_compression": 0.6, "uncertain": 0.5, "crisis": 0.1, "liquidation_cascade": 0.1, "squeeze": 0.6, "recovery": 0.7, "accumulation": 0.7, "distribution": 0.7},
    # v1.3 models — fired only when research regime matches; affinities reflect their selectivity.
    # crisis/liquidation_cascade = 0.0 (hard block, same as all directional models).
    "pullback_long":           {"bull_trend": 1.0, "bear_trend": 0.0, "ranging": 0.0, "volatility_expansion": 0.1, "volatility_compression": 0.05, "uncertain": 0.1, "crisis": 0.0, "liquidation_cascade": 0.0, "squeeze": 0.1, "recovery": 0.5, "accumulation": 0.3, "distribution": 0.05},
    "swing_low_continuation":  {"bull_trend": 0.0, "bear_trend": 1.0, "ranging": 0.1, "volatility_expansion": 0.2, "volatility_compression": 0.05, "uncertain": 0.1, "crisis": 0.0, "liquidation_cascade": 0.0, "squeeze": 0.2, "recovery": 0.2, "accumulation": 0.05, "distribution": 0.5},
}

def _get_regime_affinity(model_name: str) -> dict[str, float]:
    """Read regime affinity for a model from settings with fallback to hardcoded defaults."""
    settings_affinity = _s.get(f"regime_affinity.{model_name}")
    if isinstance(settings_affinity, dict):
        return settings_affinity
    return REGIME_AFFINITY.get(model_name, {})


def get_all_regime_affinities() -> dict[str, dict[str, float]]:
    """Return all regime affinities (from settings with hardcoded fallback)."""
    result = {}
    for model_name in REGIME_AFFINITY:
        result[model_name] = _get_regime_affinity(model_name)
    return result


# Base weights (will be renormalized across only fired models)
# Agent-derived models (funding_rate, order_book) have lower base weights
# because they fire as confirming evidence, not primary signals.
MODEL_WEIGHTS: dict[str, float] = {
    "trend":                   0.35,
    "mean_reversion":          0.25,
    "momentum_breakout":       0.25,
    "vwap_reversion":          0.28,   # Phase 1c/1d — VWAP mean reversion
    "liquidity_sweep":         0.15,
    "funding_rate":            0.20,   # Sprint 1 — contrarian funding signal
    "order_book":              0.18,   # Sprint 2 — microstructure imbalance
    "sentiment":               0.12,   # Sprint 4 — FinBERT / VADER news NLP
    "rl_ensemble":             0.0,    # DISABLED — untrained agents; re-enable after 50+ trades with ≥45% WR
    "orchestrator":            0.22,   # A-1 — OrchestratorEngine meta-signal vote
    # v1.3 — research-regime models (PBL fires in bull_trend, SLC in bear_trend)
    # Previously absent from this dict; added Session 41 so the scorer can
    # arbitrate them against Trend/Momentum in full_system and custom modes.
    "pullback_long":           0.25,   # v1.3 PBL: 4-yr combined PF=1.27 (with fees)
    "swing_low_continuation":  0.30,   # v1.3 SLC: stronger partner — PF=1.55 (zero fees)
}

# Minimum confluence score to generate a candidate
# The OrchestratorEngine can raise this dynamically in uncertain environments
SCORE_THRESHOLD = 0.55

# BASE_SIZE_USDT is only used as the Kelly fallback input, NOT as a hard cap.
# Actual sizing is governed by risk_pct_per_trade × capital / stop_distance,
# capped at PositionSizer.max_capital_pct (4% of capital).
BASE_SIZE_USDT = 500.0


def _get_model_weight(model_name: str) -> float:
    """Read model weight from settings, falling back to hardcoded MODEL_WEIGHTS."""
    return float(_s.get(f'model_weights.{model_name}', MODEL_WEIGHTS.get(model_name, 0.0)))


def get_effective_weights() -> dict[str, float]:
    """Return the current effective model weights (for UI display)."""
    return {name: _get_model_weight(name) for name in MODEL_WEIGHTS}

# ATR multipliers for order expiry (in minutes per timeframe bar)
TF_MINUTES: dict[str, int] = {
    "1m": 1, "3m": 3, "5m": 5, "15m": 15, "30m": 30,
    "1h": 60, "2h": 120, "4h": 240, "6h": 360, "12h": 720,
    "1d": 1440, "1w": 10080,
}


# Persistence file for TradeOutcomeTracker win-rate data.
# Survives restarts so model adaptive weights are not lost between sessions.
_TRACKER_PERSIST_FILE = Path(__file__).parent.parent.parent / "data" / "outcome_tracker.json"


class TradeOutcomeTracker:
    """Tracks per-model win rates over a rolling window for adaptive weighting.

    Win-rate data is persisted to disk (outcome_tracker.json) so that
    adaptive weights survive scanner restarts. On startup, historical
    win rates are loaded automatically.
    """

    def __init__(self, window: int = 30):
        self._window = window
        self._outcomes: dict[str, list[bool]] = {}  # model_name -> [won, won, lost, ...]
        self._lock = threading.Lock()
        self._load()  # Restore previous session's win rates on construction

    def record(self, model_names: list[str], won: bool, source: str = "live") -> None:
        """Record a trade outcome for specified models.

        Parameters
        ----------
        source : str
            Origin tag — "live" for real trades, "test"/"synthetic" are rejected.
        """
        if source in ("test", "synthetic"):
            logger.debug("TradeOutcomeTracker: rejecting %s-sourced outcome (source tagging)", source)
            return
        with self._lock:
            for name in model_names:
                if name not in self._outcomes:
                    self._outcomes[name] = []
                self._outcomes[name].append(won)
                if len(self._outcomes[name]) > self._window:
                    self._outcomes[name] = self._outcomes[name][-self._window :]
        self._save()  # Persist after every trade outcome

    def _save(self) -> None:
        """Write win-rate data to disk (best-effort, non-blocking)."""
        try:
            _TRACKER_PERSIST_FILE.parent.mkdir(parents=True, exist_ok=True)
            with self._lock:
                data = {k: list(v) for k, v in self._outcomes.items()}
            with open(_TRACKER_PERSIST_FILE, "w") as fh:
                json.dump(data, fh, indent=2)
        except Exception as exc:
            logger.debug("TradeOutcomeTracker: save failed (non-fatal): %s", exc)

    def _load(self) -> None:
        """Load win-rate data from disk if available."""
        try:
            if _TRACKER_PERSIST_FILE.exists():
                with open(_TRACKER_PERSIST_FILE, "r") as fh:
                    data = json.load(fh)
                with self._lock:
                    self._outcomes = {k: list(v) for k, v in data.items()}
                logger.debug(
                    "TradeOutcomeTracker: loaded win-rates for %d models from disk",
                    len(self._outcomes),
                )
        except Exception as exc:
            logger.debug("TradeOutcomeTracker: load failed (starting fresh): %s", exc)

    def get_win_rate(self, model_name: str) -> Optional[float]:
        """Get win rate for a model, or None if insufficient data."""
        with self._lock:
            outcomes = self._outcomes.get(model_name, [])
            if len(outcomes) < 5:
                return None  # not enough data
            return sum(outcomes) / len(outcomes)

    def get_weight_adjustment(self, model_name: str, max_adjustment: float = 0.15) -> float:
        """
        Get weight multiplier based on win rate.

        Returns weight adjustment: 1.0 ± max_adjustment based on win rate.
        """
        wr = self.get_win_rate(model_name)
        if wr is None:
            return 1.0
        neutral = 0.50
        deviation = wr - neutral
        adjustment = deviation * (max_adjustment / 0.20)  # normalise: 70% WR → +max_adj
        return round(1.0 + max(-max_adjustment, min(max_adjustment, adjustment)), 4)


# Module-level singleton
_outcome_tracker = TradeOutcomeTracker(window=30)


def get_outcome_tracker() -> TradeOutcomeTracker:
    """Get the module-level outcome tracker."""
    return _outcome_tracker


class ConfluenceScorer:
    """
    Aggregates multiple ModelSignals for the same symbol
    into an OrderCandidate using weighted voting.
    """

    def __init__(
        self,
        weights: Optional[dict[str, float]] = None,
        threshold: float = SCORE_THRESHOLD,
        base_size_usdt: float = BASE_SIZE_USDT,
    ):
        self._weights       = weights or MODEL_WEIGHTS
        self._threshold     = threshold
        self._base_size     = base_size_usdt
        # OrchestratorEngine integration — lazily fetched
        self._orchestrator  = None
        # Position sizer for Kelly-based sizing
        self._sizer         = PositionSizer(max_size_usdt=0.0)  # 0 = no absolute cap; max_capital_pct (4%) governs

        # Zero RL ensemble if disabled
        try:
            from config.settings import settings
            if not settings.get("rl.enabled", False):
                self._weights = dict(self._weights)
                self._weights["rl_ensemble"] = 0.0
        except Exception:
            pass

        # Diagnostics from the most recent score() call.
        # Read by the scanner after calling score() to populate the rationale panel.
        # Keys: raw_score, effective_threshold, direction_split, per_model,
        #       dominant_side, below_threshold, failed_at.
        self._last_diagnostics: dict = {}

        # ── Section 5: score() performance tracking ──────────────
        # Rolling window of last 200 score() call durations (seconds).
        self._score_durations: deque[float] = deque(maxlen=200)
        self._mil_cap_trigger_times: list[float] = []

    def score(
        self,
        signals: list[ModelSignal],
        symbol: str,
        regime_probs: Optional[dict] = None,
        technical_only: bool = False,
        capital_usdt_override: Optional[float] = None,
    ) -> Optional[OrderCandidate]:
        """
        Score a list of signals for one symbol and return an OrderCandidate.

        Parameters
        ----------
        technical_only : bool
            When True, run the deterministic technical scoring path only.
            Excluded in this mode (not historically available):
              - OrchestratorEngine veto / threshold adjustment / signal injection
              - AdaptiveWeightEngine (L1+L2) and TradeOutcomeTracker (combined_adj=1.0)
              - OI and Liquidation score modifiers
              - paper_executor capital (use capital_usdt_override instead)
            Included: model weights, regime affinity, direction dominance,
            correlation dampening, dynamic threshold.
            Use this mode for ALL canonical backtesting runs.
        capital_usdt_override : float | None
            Backtest equity to use for position sizing when technical_only=True.
            Ignored when technical_only=False (paper_executor is queried instead).
        """
        """
        Score a list of signals for one symbol and return an
        OrderCandidate if the confluence threshold is met.

        All signals must be for the same symbol and same direction.
        If signals disagree on direction, the majority direction is used
        and only same-direction signals contribute to the score.

        Parameters
        ----------
        signals : list[ModelSignal]
            List of signals from sub-models
        symbol : str
            Trading pair
        regime_probs : dict, optional
            Regime probability distribution for dynamic weighting
        """
        _score_t0 = _time.perf_counter()

        if not signals:
            return None

        # Reset diagnostics for this invocation
        self._last_diagnostics = {
            "raw_score":          0.0,
            "effective_threshold": 0.0,
            "direction_split":    {},
            "per_model":          {},
            "dominant_side":      None,
            "below_threshold":    True,
            "failed_at":          None,
        }

        # ── Threshold initialisation ───────────────────────────────────────
        effective_threshold = float(_s.get('idss.min_confluence_score', self._threshold))

        if not technical_only:
            # ── Orchestrator macro veto check ──────────────────────────────
            # Excluded in technical_only: depends on live agent state.
            try:
                if self._orchestrator is None:
                    from core.orchestrator.orchestrator_engine import get_orchestrator
                    self._orchestrator = get_orchestrator()
                if self._orchestrator.is_veto_active():
                    logger.info(
                        "ConfluenceScorer: ORCHESTRATOR VETO active — suppressing %s", symbol
                    )
                    return None
                threshold_adj = self._orchestrator.get_threshold_adjustment()
                effective_threshold = min(self._threshold + threshold_adj, 0.85)
            except Exception as exc:
                logger.debug("ConfluenceScorer: orchestrator unavailable — %s", exc)

            # Per-asset threshold from MultiAssetConfig
            try:
                from core.strategy.multi_asset_config import multi_asset_config
                asset_threshold = multi_asset_config.get_min_confluence_score(symbol)
                effective_threshold = max(effective_threshold, asset_threshold)
            except Exception:
                pass

            # ── Inject OrchestratorEngine as weighted vote (A-1 fix) ───────
            # Excluded in technical_only: OrchestratorEngine aggregates live agents.
            orch_sig = None  # Phase 4B: used later for agent_contributions
            try:
                if self._orchestrator is None:
                    from core.orchestrator.orchestrator_engine import get_orchestrator
                    self._orchestrator = get_orchestrator()
                orch_sig = self._orchestrator.get_signal()
                orch_meta = orch_sig.meta_signal
                orch_conf = orch_sig.meta_confidence
                orch_direction = orch_sig.direction
                if abs(orch_meta) > 0.10 and orch_conf >= 0.20:
                    existing_entries = [s.entry_price for s in signals if s.entry_price and s.entry_price > 0]
                    avg_entry = sum(existing_entries) / len(existing_entries) if existing_entries else 0.0
                    orch_direction_str = "long" if orch_meta > 0.10 else "short"
                    from core.meta_decision.order_candidate import ModelSignal
                    orch_model_signal = ModelSignal(
                        symbol      = symbol,
                        model_name  = "orchestrator",
                        direction   = orch_direction_str,
                        strength    = min(1.0, abs(orch_meta) * orch_conf * 1.5),
                        entry_price = avg_entry,
                        stop_loss   = 0.0,
                        take_profit = 0.0,
                        atr_value   = 0.0,
                        timeframe   = signals[0].timeframe if signals else "",
                        regime      = orch_direction,
                        rationale   = (f"OrchestratorEngine meta-signal: {orch_direction_str} "
                                      f"| meta={orch_meta:+.3f} | conf={orch_conf:.2f} "
                                      f"| agents={orch_sig.effective_agent_count}"),
                    )
                    signals = list(signals) + [orch_model_signal]
                    logger.debug("ConfluenceScorer: injected orchestrator vote %s (strength=%.2f)",
                                 orch_direction_str, orch_model_signal.strength)
            except Exception as exc:
                logger.debug("ConfluenceScorer: orchestrator vote injection failed: %s", exc)

        # ── Weighted direction vote ───────────────────────────────────────
        # Instead of counting signals (count-based), we sum the adaptive weights
        # to determine direction. This prevents a cluster of weak signals from
        # overriding a single high-weight signal in the opposite direction.
        # Additionally, if the weighted dominance is below the minimum threshold,
        # the candidate is rejected as "too conflicted".
        long_signals  = [s for s in signals if s.direction == "long"]
        short_signals = [s for s in signals if s.direction == "short"]

        if not long_signals and not short_signals:
            return None

        # ── Adaptive model weights helper ─────────────────────────────
        # Combines regime affinity (probabilistic activation) with the
        # AdaptiveWeightEngine (L1 global win-rate × L2 contextual).
        _dominant_regime = (
            max(regime_probs, key=regime_probs.get)
            if regime_probs else "unknown"
        )
        try:
            _adaptive_engine = _get_adaptive_engine_safe()
        except Exception:
            _adaptive_engine = None

        def _get_adaptive_weight(model_name: str, base_weight: float) -> float:
            """Compute adaptive weight: base × regime_affinity × (L1 × L2) multiplier.

            When technical_only=True, combined_adj is fixed at 1.0 — no adaptive
            learning (L1/L2) applied.  Regime affinity still runs (purely static).
            """
            if not regime_probs:
                return base_weight
            affinity = _get_regime_affinity(model_name)
            activation = sum(affinity.get(r, 0.3) * p for r, p in regime_probs.items())
            activation = max(0.0, min(1.0, activation))
            # Combined L1 (global WR) + L2 (regime×model, asset×model)
            if technical_only:
                combined_adj = 1.0          # deterministic — no live trade-outcome data
            elif _adaptive_engine is not None:
                combined_adj = _adaptive_engine.get_multiplier(
                    model=model_name,
                    regime=_dominant_regime,
                    asset=symbol,
                )
            else:
                combined_adj = _outcome_tracker.get_weight_adjustment(model_name)
            return base_weight * activation * combined_adj

        # v3: Memoize _get_adaptive_weight — called 11+ times per score()
        # with repeated (model_name, base_weight) pairs. Cache eliminates
        # redundant regime_affinity + adaptive_engine + outcome_tracker lookups.
        _aw_cache: dict[str, float] = {}
        _raw_get_adaptive_weight = _get_adaptive_weight

        def _get_adaptive_weight(model_name: str, base_weight: float) -> float:
            _key = f"{model_name}:{base_weight}"
            if _key not in _aw_cache:
                _aw_cache[_key] = _raw_get_adaptive_weight(model_name, base_weight)
            return _aw_cache[_key]

        long_weight_sum = sum(
            _get_adaptive_weight(s.model_name, _get_model_weight(s.model_name)) * s.strength
            for s in long_signals
        )
        short_weight_sum = sum(
            _get_adaptive_weight(s.model_name, _get_model_weight(s.model_name)) * s.strength
            for s in short_signals
        )
        total_direction_weight = long_weight_sum + short_weight_sum

        # Directional dominance check — reject if signals are too evenly split
        if total_direction_weight > 0:
            dominance = abs(long_weight_sum - short_weight_sum) / total_direction_weight
            from config.settings import settings as _sd
            min_dominance = float(_sd.get("confluence.min_direction_dominance", 0.30))
            if dominance < min_dominance:
                logger.debug(
                    "ConfluenceScorer: %s rejected — directional dominance %.2f < threshold %.2f",
                    symbol, dominance, min_dominance,
                )
                return None

        _total_dir_w = long_weight_sum + short_weight_sum
        _dom_val = abs(long_weight_sum - short_weight_sum) / _total_dir_w if _total_dir_w > 0 else 0.0
        self._last_diagnostics["direction_split"] = {
            "long":      round(long_weight_sum, 4),
            "short":     round(short_weight_sum, 4),
            "dominance": round(_dom_val, 4),
        }

        if long_weight_sum >= short_weight_sum:
            active_signals = long_signals
            side = "buy"
        else:
            active_signals = short_signals
            side = "sell"

        self._last_diagnostics["dominant_side"] = side

        # ── Correlation dampening ─────────────────────────────
        # Reduce effective weight of models that belong to the same
        # correlation cluster (e.g. trend + momentum_breakout both use
        # price-momentum indicators — double-counting inflates confidence).
        # Factor: 1/sqrt(N) where N = cluster members that fired.
        # Non-fatal; falls back to factor=1.0 on any error.
        _damp_factors: dict[str, float] = {}
        try:
            from core.analytics.correlation_dampener import get_dampening_factors
            _fired_names = [s.model_name for s in active_signals]
            _damp_factors = get_dampening_factors(_fired_names)
        except Exception as _damp_exc:
            logger.debug("ConfluenceScorer: correlation dampener error (non-fatal): %s", _damp_exc)

        # ── Weighted score ────────────────────────────────────
        total_weight = 0.0
        for s in active_signals:
            w = _get_adaptive_weight(s.model_name, _get_model_weight(s.model_name))
            w *= _damp_factors.get(s.model_name, 1.0)          # apply dampening
            if w == 0.0 and s.model_name not in MODEL_WEIGHTS:
                logger.debug("ConfluenceScorer: unknown model '%s' excluded from scoring", s.model_name)
            total_weight += w
        if total_weight == 0:
            return None

        weighted_score = sum(
            (
                _get_adaptive_weight(s.model_name, _get_model_weight(s.model_name))
                * _damp_factors.get(s.model_name, 1.0)
                / total_weight
            ) * s.strength
            for s in active_signals
            if _get_adaptive_weight(s.model_name, _get_model_weight(s.model_name)) > 0.0
        ) if total_weight > 0 else 0.0

        # ── Pure technical baseline (MIL cap reference) ──────────────
        # Compute the weighted score from ONLY technical model signals,
        # excluding the orchestrator.  This is the true baseline that
        # the MIL cap is measured against.  MIL influence = orchestrator
        # injection + OI/Liq modifiers.  The cap enforces:
        #   abs(final_score - _mil_technical_baseline) <= CAP * baseline
        _tech_signals = [s for s in active_signals if s.model_name != "orchestrator"]
        _tech_total_weight = sum(
            _get_adaptive_weight(s.model_name, _get_model_weight(s.model_name))
            * _damp_factors.get(s.model_name, 1.0)
            for s in _tech_signals
        )
        _mil_technical_baseline = sum(
            (
                _get_adaptive_weight(s.model_name, _get_model_weight(s.model_name))
                * _damp_factors.get(s.model_name, 1.0)
                / _tech_total_weight
            ) * s.strength
            for s in _tech_signals
            if _get_adaptive_weight(s.model_name, _get_model_weight(s.model_name)) > 0.0
        ) if _tech_total_weight > 0 else 0.0

        # ── Populate per-model diagnostics ────────────────────────────
        _pm_diag: dict = {}
        for _ds in active_signals:
            _dw = (
                _get_adaptive_weight(_ds.model_name, _get_model_weight(_ds.model_name))
                * _damp_factors.get(_ds.model_name, 1.0)
            )
            _dc = (_dw / total_weight * _ds.strength) if total_weight > 0 else 0.0
            _pm_diag[_ds.model_name] = {
                "weight":         round(_dw, 4),
                "strength":       round(_ds.strength, 3),
                "direction":      _ds.direction,
                "contribution":   round(_dc, 4),
                "damp_factor":    round(_damp_factors.get(_ds.model_name, 1.0), 3),
            }
        self._last_diagnostics["per_model"]         = _pm_diag
        self._last_diagnostics["raw_score"]         = round(weighted_score, 4)
        self._last_diagnostics["damp_factors"]      = {m: round(f, 3) for m, f in _damp_factors.items()}

        # ── Dynamic threshold adjustment ───────────────────────────────
        dynamic_enabled = _s.get("dynamic_confluence.enabled", True)
        if dynamic_enabled and regime_probs:
            # regime_confidence_factor
            top_prob = max(regime_probs.values()) if regime_probs else 0.5
            if top_prob >= float(_s.get("dynamic_confluence.regime_confidence_high", 0.70)):
                regime_conf_factor = 1.05
            elif top_prob <= float(_s.get("dynamic_confluence.regime_confidence_low", 0.40)):
                regime_conf_factor = 0.85
            else:
                regime_conf_factor = 1.0 + (top_prob - 0.55) * 0.2  # linear

            # model_count_factor: lower threshold when few models eligible
            active_weight_sum = sum(
                _get_adaptive_weight(s.model_name, _get_model_weight(s.model_name))
                for s in active_signals
            )
            max_weight_sum = sum(_get_model_weight(name) for name in MODEL_WEIGHTS)
            model_count_factor = max(0.75, active_weight_sum / max_weight_sum) if max_weight_sum > 0 else 1.0

            # volatility factor
            best_regime = max(regime_probs, key=lambda k: regime_probs[k]) if regime_probs else ""
            if "volatility_expansion" in best_regime or "crisis" in best_regime:
                vol_factor = float(_s.get("dynamic_confluence.vol_expansion_factor", 1.15))
            elif "compression" in best_regime:
                vol_factor = float(_s.get("dynamic_confluence.vol_compression_factor", 0.95))
            else:
                vol_factor = 1.0

            raw_adjusted = effective_threshold * regime_conf_factor * model_count_factor * vol_factor
            floor_val = float(_s.get("dynamic_confluence.min_floor", 0.28))
            ceiling_val = float(_s.get("dynamic_confluence.max_ceiling", 0.65))
            effective_threshold = max(floor_val, min(ceiling_val, raw_adjusted))
            logger.debug(
                "ConfluenceScorer: dynamic threshold=%.3f (base=%.3f, conf_factor=%.2f, model_factor=%.2f, vol_factor=%.2f)",
                effective_threshold, self._threshold, regime_conf_factor, model_count_factor, vol_factor,
            )

        logger.debug(
            "ConfluenceScorer: %s %s | models=%s | score=%.3f",
            symbol, side,
            [s.model_name for s in active_signals],
            weighted_score,
        )

        # ── Phase 4: OI + Liquidation score modifiers (non-fatal) ─────────
        # Independent ablation: set oi_signal.oi_modifier_enabled / liq_modifier_enabled
        # to false in config to disable each component individually.
        # Per-fire INFO logs emitted inside get_oi_modifier / get_liquidation_modifier.
        # Excluded in technical_only mode: live Coinglass data not historically available.
        #
        # Track individual OI/Liq deltas for MIL breakdown diagnostics.
        _oi_mod_val = 0.0
        _liq_mod_val = 0.0
        if not technical_only:
            try:
                if _s.get("oi_signal.enabled", True):
                    from core.signals.oi_signal import get_oi_modifier, get_liquidation_modifier
                    _oi_mod_val, _oi_reason = get_oi_modifier(symbol=symbol, direction=side)
                    _liq_mod_val, _liq_reason = get_liquidation_modifier(symbol=symbol, direction=side)
                    _total_mod = _oi_mod_val + _liq_mod_val
                    if abs(_total_mod) > 0.001:
                        _score_before = weighted_score
                        weighted_score = max(0.0, min(1.0, weighted_score + _total_mod))
                        logger.info(
                            "ConfluenceScorer %s: OI/Liq modifier %+.3f "
                            "(oi=%+.3f '%s' | liq=%+.3f '%s') "
                            "score %.3f → %.3f",
                            symbol, _total_mod,
                            _oi_mod_val, _oi_reason,
                            _liq_mod_val, _liq_reason,
                            _score_before, weighted_score,
                        )
                        # Record in diagnostics for rationale panel
                        self._last_diagnostics["oi_modifier"] = round(_total_mod, 4)
                        self._last_diagnostics["oi_reason"] = _oi_reason
                        self._last_diagnostics["liq_reason"] = _liq_reason
            except Exception as _oi_exc:
                logger.debug("ConfluenceScorer: OI modifier error (non-fatal): %s", _oi_exc)

        # ── MIL Hard Cap Enforcement (Phase 4A) ──────────────────────────
        # Central authoritative clamp: the TOTAL MIL contribution to the
        # weighted score MUST NOT exceed MIL_INFLUENCE_CAP × baseline.
        #
        # MIL influence = orchestrator signal injection + OI modifier +
        # liquidation modifier.  The baseline is `_mil_technical_baseline`,
        # computed from ONLY technical model signals (no orchestrator).
        #
        # Enforced HERE (not in individual enhancers) so that even if
        # downstream logic changes, the cap cannot be exceeded.
        #
        # Invariant: abs(final_score - _mil_technical_baseline)
        #            <= MIL_INFLUENCE_CAP × _mil_technical_baseline
        #
        # Section 4 guardrail: if baseline < 0.05, skip MIL entirely
        # (avoids extreme ratios on near-zero scores).
        if not technical_only and _mil_technical_baseline > 0:
            from core.agents.mil.funding_rate_enhanced import MIL_INFLUENCE_CAP

            # ── Section 4 guardrail: low-baseline protection ─────────
            if _mil_technical_baseline < 0.05:
                # Revert to pure technical score — MIL too risky at this level
                weighted_score = _mil_technical_baseline
                logger.debug(
                    "ConfluenceScorer %s: MIL disabled — tech baseline %.4f < 0.05",
                    symbol, _mil_technical_baseline,
                )
                self._last_diagnostics["mil_disabled_low_baseline"] = True
            else:
                _mil_max_delta = MIL_INFLUENCE_CAP * _mil_technical_baseline
                _mil_actual_delta = weighted_score - _mil_technical_baseline

                # ── Section 4 guardrail: NaN/Inf protection ──────────
                if math.isnan(_mil_actual_delta) or math.isinf(_mil_actual_delta):
                    weighted_score = _mil_technical_baseline
                    logger.warning(
                        "ConfluenceScorer %s: MIL produced invalid delta (NaN/Inf) — "
                        "reverting to tech baseline %.4f",
                        symbol, _mil_technical_baseline,
                    )
                    _mil_actual_delta = 0.0
                    self._last_diagnostics["mil_nan_fallback"] = True

                _was_capped = abs(_mil_actual_delta) > _mil_max_delta
                if _was_capped:
                    _clamped_delta = max(-_mil_max_delta, min(_mil_max_delta, _mil_actual_delta))
                    weighted_score = max(0.0, min(1.0, _mil_technical_baseline + _clamped_delta))
                    logger.info(
                        "ConfluenceScorer %s: MIL cap enforced — raw delta %+.4f "
                        "clamped to %+.4f (cap=%.0f%% of tech_baseline %.4f)",
                        symbol, _mil_actual_delta, _clamped_delta,
                        MIL_INFLUENCE_CAP * 100, _mil_technical_baseline,
                    )

                # ── Section 1: expanded MIL diagnostics ──────────────
                _orch_delta = weighted_score - _oi_mod_val - _liq_mod_val - _mil_technical_baseline
                # Clamp orchestrator delta attribution to actual total
                # (rounding can cause micro-drift)
                _total_delta = weighted_score - _mil_technical_baseline
                _orch_delta_attr = _total_delta - _oi_mod_val - _liq_mod_val
                _mil_delta_pct = (_total_delta / _mil_technical_baseline) if _mil_technical_baseline > 0 else 0.0

                # ── Section 4 guardrail: warn on high MIL pressure ───
                if abs(_mil_delta_pct) > 0.25:
                    logger.warning(
                        "ConfluenceScorer %s: MIL delta %.1f%% of baseline (threshold 25%%)",
                        symbol, _mil_delta_pct * 100,
                    )

                self._last_diagnostics["mil_technical_baseline"] = round(_mil_technical_baseline, 4)
                self._last_diagnostics["mil_total_delta"] = round(_total_delta, 4)
                self._last_diagnostics["mil_delta_pct"] = round(_mil_delta_pct, 4)

                # ── Phase 4B: MIL breakdown via agent_contributions ──
                # Decompose orchestrator_delta using magnitude_share from
                # OrchestratorSignal.agent_contributions (Phase S2 design).
                # Attribution semantics: diagnostic proportional attribution
                # based on pre-post-processing linear contributions.
                # Invariant A: sentiment + news + other == orchestrator (by construction).
                _sentiment_delta = 0.0
                _news_delta = 0.0
                try:
                    _contribs = orch_sig.agent_contributions if orch_sig is not None else {}
                    _news_share = _contribs.get("news", {}).get("magnitude_share", 0.0)
                    _sentiment_share = _contribs.get("social_sentiment", {}).get("magnitude_share", 0.0)
                    _sentiment_delta = _orch_delta_attr * _sentiment_share
                    _news_delta = _orch_delta_attr * _news_share
                except Exception:
                    pass  # fail-open: both remain 0.0
                _other_orch_delta = _orch_delta_attr - _sentiment_delta - _news_delta

                # ── Invariant A: sentiment + news + other == orchestrator ──
                _inv_a_err = abs(
                    (_sentiment_delta + _news_delta + _other_orch_delta) - _orch_delta_attr
                )
                if _inv_a_err > 1e-6:
                    logger.error(
                        "ConfluenceScorer %s: MIL invariant A violated: "
                        "sentiment(%.4f) + news(%.4f) + other(%.4f) = %.4f != orch(%.4f)",
                        symbol, _sentiment_delta, _news_delta, _other_orch_delta,
                        _sentiment_delta + _news_delta + _other_orch_delta, _orch_delta_attr,
                    )

                # ── Invariant B: orchestrator + oi + liquidation == total ──
                _inv_b_sum = _orch_delta_attr + _oi_mod_val + _liq_mod_val
                _inv_b_err = abs(_inv_b_sum - _total_delta)
                if _inv_b_err > 1e-3:
                    logger.error(
                        "ConfluenceScorer %s: MIL invariant B violated: "
                        "orch(%.4f) + oi(%.4f) + liq(%.4f) = %.4f != total(%.4f)",
                        symbol, _orch_delta_attr, _oi_mod_val, _liq_mod_val,
                        _inv_b_sum, _total_delta,
                    )

                _breakdown = {
                    "orchestrator_delta":       round(_orch_delta_attr, 4),
                    "sentiment_delta":          round(_sentiment_delta, 4),
                    "news_delta":               round(_news_delta, 4),
                    "other_orchestrator_delta":  round(_other_orch_delta, 4),
                    "oi_delta":                 round(_oi_mod_val, 4),
                    "liquidation_delta":        round(_liq_mod_val, 4),
                }
                self._last_diagnostics["mil_breakdown"] = _breakdown

                # ── Phase S1: dominant source (computed here, passed through API) ──
                _sources = {
                    "orchestrator": abs(_orch_delta_attr),
                    "sentiment":    abs(_sentiment_delta),
                    "news":         abs(_news_delta),
                    "oi":           abs(_oi_mod_val),
                    "liquidation":  abs(_liq_mod_val),
                }
                _max_src = max(_sources, key=_sources.get)
                self._last_diagnostics["mil_dominant_source"] = (
                    _max_src if _sources[_max_src] > 0.001 else "none"
                )

                self._last_diagnostics["mil_delta_raw"] = round(_mil_actual_delta, 4)
                self._last_diagnostics["mil_delta_max"] = round(_mil_max_delta, 4)
                self._last_diagnostics["mil_capped"] = _was_capped

                # ── Section 4: cap-trigger rate tracking ─────────────
                if _was_capped:
                    _now = _time.monotonic()
                    if not hasattr(self, "_mil_cap_trigger_times"):
                        self._mil_cap_trigger_times: list[float] = []
                    self._mil_cap_trigger_times.append(_now)
                    # Keep only last 60s
                    self._mil_cap_trigger_times = [
                        t for t in self._mil_cap_trigger_times if _now - t <= 60.0
                    ]
                    if len(self._mil_cap_trigger_times) > 5:
                        logger.warning(
                            "ConfluenceScorer: MIL cap triggered %d times in last 60s "
                            "(threshold 5)",
                            len(self._mil_cap_trigger_times),
                        )

        self._last_diagnostics["effective_threshold"] = round(effective_threshold, 4)
        self._last_diagnostics["below_threshold"] = weighted_score < effective_threshold

        if weighted_score < effective_threshold:
            logger.debug(
                "Score %.3f below threshold %.3f — no candidate",
                weighted_score, effective_threshold,
            )
            self._last_diagnostics["failed_at"] = "below_threshold"
            _score_elapsed = _time.perf_counter() - _score_t0
            self._score_durations.append(_score_elapsed)
            self._last_diagnostics["score_duration_ms"] = round(_score_elapsed * 1000, 2)
            return None

        # ── Synthesize entry/stop/target from signals ─────────
        # Use the signal from the model with the highest weight as the
        # primary reference for price levels.
        primary = max(
            active_signals,
            key=lambda s: _get_model_weight(s.model_name),
        )

        entry_price    = primary.entry_price
        atr            = primary.atr_value
        timeframe      = primary.timeframe
        regime         = primary.regime

        # Use the primary model's stop and target rather than averaging across all
        # signals.  Averaging produced synthetic price levels that no individual
        # model intended, invalidating the R:R ratio that the EV gate evaluates.
        # The primary model (highest base weight) is already the reference for
        # entry_price, so its stop/target levels are internally consistent.
        stop_loss_price   = primary.stop_loss
        take_profit_price = primary.take_profit

        # ── Position sizing — Risk-based (Study 4 production config) ──
        # Uses exact stop price from primary model for precision.
        # Capital is read from paper_executor if available, else uses base proxy.
        entry_for_sizing = entry_price if entry_price and entry_price > 0 else 1.0
        if technical_only and capital_usdt_override is not None:
            # Backtest path: use injected equity directly — no paper_executor access
            _capital = float(capital_usdt_override)
        else:
            try:
                from core.execution.paper_executor import get_paper_executor
                _pe = get_paper_executor()
                _capital = _pe._capital  # current compounding capital
            except Exception:
                _capital = float(_s.get("scanner.capital_usdt", 100_000.0))

        risk_pct = float(_s.get("risk_engine.risk_pct_per_trade", 0.5))

        try:
            from config.settings import settings as _s2
            sizing_mode = _s2.get("risk_engine.sizing_mode", "risk_based")
        except Exception:
            sizing_mode = "risk_based"

        if sizing_mode == "risk_based" and stop_loss_price and stop_loss_price > 0 and entry_for_sizing > 0:
            # Pass concurrency for tiered capital model (Phase 2, gated by config)
            try:
                _open_count = sum(len(v) for v in _pe._positions.values()) if isinstance(_pe, object) and hasattr(_pe, "_positions") else 0
            except Exception:
                _open_count = 0
            position_size = self._sizer.calculate_risk_based(
                capital_usdt         = _capital,
                entry_price          = entry_for_sizing,
                stop_price           = stop_loss_price,
                risk_pct             = risk_pct,
                regime               = regime,
                open_positions_count = _open_count,
                conviction_score     = weighted_score,
            )
            logger.info(
                "ConfluenceScorer: %s risk-based size=%.2f USDT (capital=%.0f, risk=%.2f%%, stop_dist=%.6f)",
                symbol, position_size, _capital, risk_pct, abs(entry_for_sizing - stop_loss_price),
            )
        else:
            # Fallback to Kelly when stop price unavailable
            atr_for_sizing = atr if atr and atr > 0 else (entry_for_sizing * 0.008)
            position_size = self._sizer.calculate(
                available_capital_usdt=_capital,
                atr_value=atr_for_sizing,
                entry_price=entry_for_sizing,
                score=weighted_score,
                regime=regime,
                drawdown_pct=0.0,
            )
            logger.debug("ConfluenceScorer: %s — Kelly fallback size=%.2f USDT", symbol, position_size)

        # ── Order expiry (5 candles in primary TF) ────────────
        tf_min  = TF_MINUTES.get(timeframe, 60)
        expiry  = datetime.utcnow() + timedelta(minutes=tf_min * 5)

        # ── Rationale ─────────────────────────────────────────
        model_names = [s.model_name for s in active_signals]
        model_rationales = "\n  ".join(s.rationale for s in active_signals)
        rationale = (
            f"Confluence score: {weighted_score:.2%} "
            f"(threshold {effective_threshold:.0%}) | "
            f"Models: {', '.join(model_names)} | "
            f"Regime: {regime}\n  {model_rationales}"
        )

        logger.debug("ConfluenceScorer: %s — building OrderCandidate (about to return)", symbol)
        # ── Section 5: record score() duration ────────────────────────
        _score_elapsed = _time.perf_counter() - _score_t0
        self._score_durations.append(_score_elapsed)
        self._last_diagnostics["score_duration_ms"] = round(_score_elapsed * 1000, 2)

        return OrderCandidate(
            symbol             = symbol,
            side               = side,
            entry_type         = "limit",
            entry_price        = round(entry_price, 8),
            stop_loss_price    = round(stop_loss_price, 8),
            take_profit_price  = round(take_profit_price, 8),
            position_size_usdt = position_size,
            score              = round(weighted_score, 4),
            models_fired       = model_names,
            regime             = regime,
            rationale          = rationale,
            timeframe          = timeframe,
            atr_value          = atr,
            expiry             = expiry,
        )

    def get_score_perf_stats(self) -> dict:
        """Return p50 and p95 score() duration in milliseconds."""
        if not self._score_durations:
            return {"p50_ms": 0.0, "p95_ms": 0.0, "n": 0}
        sorted_d = sorted(self._score_durations)
        n = len(sorted_d)
        p50 = sorted_d[int(n * 0.50)] * 1000
        p95 = sorted_d[min(int(n * 0.95), n - 1)] * 1000
        return {"p50_ms": round(p50, 2), "p95_ms": round(p95, 2), "n": n}
