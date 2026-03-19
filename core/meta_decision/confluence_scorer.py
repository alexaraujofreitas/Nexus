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
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
import pandas as pd

from core.meta_decision.order_candidate import ModelSignal, OrderCandidate
from core.meta_decision.position_sizer import PositionSizer
from config.settings import settings as _s

logger = logging.getLogger(__name__)

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
    "trend":              0.35,
    "mean_reversion":     0.25,
    "momentum_breakout":  0.25,
    "vwap_reversion":     0.28,   # Phase 1c/1d — VWAP mean reversion
    "liquidity_sweep":    0.15,
    "funding_rate":       0.20,   # Sprint 1 — contrarian funding signal
    "order_book":         0.18,   # Sprint 2 — microstructure imbalance
    "sentiment":          0.12,   # Sprint 4 — FinBERT / VADER news NLP
    "rl_ensemble":        0.0,    # DISABLED — untrained agents; re-enable after 50+ trades with ≥45% WR
    "orchestrator":       0.22,   # A-1 — OrchestratorEngine meta-signal vote
}

# Minimum confluence score to generate a candidate
# The OrchestratorEngine can raise this dynamically in uncertain environments
SCORE_THRESHOLD = 0.55

# Default position size base (USDT) — adjusted by score below.
# Raised from $100 to $500 so that P&L data points are large enough
# to be statistically meaningful against a $100k account.
# The PositionSizer's Kelly/cap logic still applies as a safety net.
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
        self._sizer         = PositionSizer(max_size_usdt=500.0)  # Demo cap: $500/trade

        # Zero RL ensemble if disabled
        try:
            from config.settings import settings
            if not settings.get("rl.enabled", False):
                self._weights = dict(self._weights)
                self._weights["rl_ensemble"] = 0.0
        except Exception:
            pass

    def score(
        self,
        signals: list[ModelSignal],
        symbol: str,
        regime_probs: Optional[dict] = None,
    ) -> Optional[OrderCandidate]:
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
        if not signals:
            return None

        # ── Orchestrator macro veto check ─────────────────────
        # If macro conditions are hostile, block new trades entirely
        effective_threshold = float(_s.get('idss.min_confluence_score', self._threshold))
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

        # ── Inject OrchestratorEngine as weighted vote (A-1 fix) ───
        try:
            if self._orchestrator is None:
                from core.orchestrator.orchestrator_engine import get_orchestrator
                self._orchestrator = get_orchestrator()
            orch_sig = self._orchestrator.get_signal()
            orch_meta = orch_sig.meta_signal
            orch_conf = orch_sig.meta_confidence
            orch_direction = orch_sig.direction
            if abs(orch_meta) > 0.10 and orch_conf >= 0.20:
                # Compute average entry price from existing signals
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
            from core.learning.adaptive_weight_engine import get_adaptive_weight_engine
            _adaptive_engine = get_adaptive_weight_engine()
        except Exception:
            _adaptive_engine = None

        def _get_adaptive_weight(model_name: str, base_weight: float) -> float:
            """Compute adaptive weight: base × regime_affinity × (L1 × L2) multiplier."""
            if not regime_probs:
                return base_weight
            affinity = _get_regime_affinity(model_name)
            activation = sum(affinity.get(r, 0.3) * p for r, p in regime_probs.items())
            activation = max(0.0, min(1.0, activation))
            # Combined L1 (global WR) + L2 (regime×model, asset×model)
            if _adaptive_engine is not None:
                combined_adj = _adaptive_engine.get_multiplier(
                    model=model_name,
                    regime=_dominant_regime,
                    asset=symbol,
                )
            else:
                combined_adj = _outcome_tracker.get_weight_adjustment(model_name)
            return base_weight * activation * combined_adj

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

        if long_weight_sum >= short_weight_sum:
            active_signals = long_signals
            side = "buy"
        else:
            active_signals = short_signals
            side = "sell"

        # ── Weighted score ────────────────────────────────────
        total_weight = 0.0
        for s in active_signals:
            w = _get_adaptive_weight(s.model_name, _get_model_weight(s.model_name))
            if w == 0.0 and s.model_name not in MODEL_WEIGHTS:
                logger.debug("ConfluenceScorer: unknown model '%s' excluded from scoring", s.model_name)
            total_weight += w
        if total_weight == 0:
            return None

        weighted_score = sum(
            (_get_adaptive_weight(s.model_name, _get_model_weight(s.model_name)) / total_weight) * s.strength
            for s in active_signals
            if _get_adaptive_weight(s.model_name, _get_model_weight(s.model_name)) > 0.0
        ) if total_weight > 0 else 0.0

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

        if weighted_score < effective_threshold:
            logger.debug(
                "Score %.3f below threshold %.3f — no candidate",
                weighted_score, effective_threshold,
            )
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

        # ── Position sizing by confidence (Kelly-based) ──────────
        # Dynamic ATR/Kelly-based position sizing
        atr_for_sizing = atr if atr and atr > 0 else (entry_price * 0.008 if entry_price else 1.0)
        entry_for_sizing = entry_price if entry_price and entry_price > 0 else 1.0
        base_size = float(_s.get('execution.base_size_usdt', BASE_SIZE_USDT))
        logger.debug("ConfluenceScorer: %s — calling PositionSizer.calculate()", symbol)
        position_size = self._sizer.calculate(
            available_capital_usdt=base_size * 100,  # treat base_size*100 as proxy capital
            atr_value=atr_for_sizing,
            entry_price=entry_for_sizing,
            score=weighted_score,
            regime=regime,
            drawdown_pct=0.0,
        )
        logger.debug("ConfluenceScorer: %s — PositionSizer returned size=%.2f", symbol, position_size)

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
