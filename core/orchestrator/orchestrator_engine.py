# ============================================================
# NEXUS TRADER — Orchestrator Engine  (Sprint 8)
#
# Combines signals from ALL intelligence agents into a unified
# meta-signal that the ConfluenceScorer and RiskGate can use
# as a macro-regime filter and conviction multiplier.
#
# Architecture:
#   1. Collect latest signals from all 8 agents
#   2. Apply staleness-weighted signal decay based on agent age
#   3. Select regime-conditional weight table based on HMM regime
#   4. Compute a weighted "meta-signal" [-1, +1]
#   5. Multiply effective confidences by regime_confidence (HMM state probability)
#   6. Apply multi-factor veto in addition to macro veto
#   7. Compute "macro_veto" — if macro conditions are hostile,
#      block new trades regardless of technical signals
#   8. Compute "orchestrator_confidence" that scales ConfluenceScorer
#      threshold requirements upward in uncertain environments
#   9. Publish OrchestratorSignal to EventBus
#
# Agent weights (DEFAULT_WEIGHTS):
#   funding_rate      : 0.25  (high — direct market mechanic)
#   order_book        : 0.22  (high — immediate supply/demand)
#   options_flow      : 0.18  (medium — institutional positioning)
#   macro             : 0.17  (medium — macro regime filter)
#   social_sentiment  : 0.08  (low — noisy but confirming)
#   news              : 0.05  (FinBERT / VADER news NLP)
#   geopolitical      : 0.03  (regulatory/geopolitical risk)
#   sector_rotation   : 0.02  (sector ETF momentum proxy)
#
# Staleness decay:
#   age_seconds = now - agent.updated_at
#   effective_confidence = agent_conf * max(0.10, 1.0 - (age_seconds / (2.0 * poll_interval)))
#
# Regime-conditional weights (TRENDING_UP, TRENDING_DOWN, RANGING, HIGH_VOLATILITY, CRISIS, RECOVERY, UNKNOWN)
# HMM confidence multiplier propagates regime model uncertainty to all agent weights
#
# Macro veto logic:
#   macro_risk_score > 0.75 AND combined macro signal < -0.40
#   → veto = True (block new trades)
#
# Multi-factor veto:
#   funding_rate < -0.6 AND order_book < -0.6 AND macro < -0.4
#   → force meta_signal to max(-0.8, meta_signal) and set macro_veto=True
#
# Regime-conditional threshold tightening:
#   HIGH_VOLATILITY: +0.12 to confluence threshold
#   CRISIS: +0.20 to confluence threshold
#
# Publishes:
#   Topics.ORCHESTRATOR_SIGNAL — summary of all agents + meta-signal
#   Topics.ORCHESTRATOR_VETO   — only when veto state changes
# ============================================================
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from PySide6.QtCore import QObject, Signal

from core.event_bus import bus, Topics

logger = logging.getLogger(__name__)

# Default agent signal weights (used as fallback if regime unknown)
DEFAULT_WEIGHTS: dict[str, float] = {
    "funding_rate":     0.184,  # direct market mechanic — high relevance
    "order_book":       0.168,  # immediate supply/demand
    "options_flow":     0.137,  # institutional positioning (BTC/ETH only)
    "macro":            0.126,  # macro regime filter
    "social_sentiment": 0.068,  # social + FNG
    "news":             0.039,  # FinBERT / VADER news NLP
    "geopolitical":     0.020,  # regulatory/geopolitical risk
    "sector_rotation":  0.009,  # sector ETF momentum proxy
    "onchain":          0.074,  # on-chain metrics and flow analysis
    "volatility_surface": 0.068, # volatility surface and implied vol
    "liquidation_flow": 0.039,  # liquidation flow and cascade risk
    "crash_detection":  0.068,  # crash detection and defense
}

# Mapping from RegimeClassifier names to orchestrator weight table keys
REGIME_NAME_MAP = {
    "bull_trend": "TRENDING_UP",
    "bear_trend": "TRENDING_DOWN",
    "ranging": "RANGING",
    "volatility_expansion": "HIGH_VOLATILITY",
    "volatility_compression": "HIGH_VOLATILITY",
    "accumulation": "ACCUMULATION",
    "distribution": "DISTRIBUTION",
    "uncertain": "UNKNOWN",
}

# Regime-conditional weight tables
REGIME_WEIGHTS: dict[str, dict[str, float]] = {
    "TRENDING_UP": {
        "funding_rate":       0.10,
        "order_book":         0.12,
        "options_flow":       0.08,
        "macro":              0.10,
        "social_sentiment":   0.05,
        "news":               0.05,
        "geopolitical":       0.05,
        "sector_rotation":    0.05,
        "onchain":            0.10,
        "volatility_surface": 0.10,
        "liquidation_flow":   0.10,
        "crash_detection":    0.10,
    },
    "TRENDING_DOWN": {
        "funding_rate":       0.15,
        "order_book":         0.10,
        "options_flow":       0.12,
        "macro":              0.12,
        "social_sentiment":   0.06,
        "news":               0.05,
        "geopolitical":       0.07,
        "sector_rotation":    0.03,
        "onchain":            0.10,
        "volatility_surface": 0.08,
        "liquidation_flow":   0.07,
        "crash_detection":    0.05,
    },
    "RANGING": {
        "funding_rate":       0.12,
        "order_book":         0.15,
        "options_flow":       0.08,
        "macro":              0.08,
        "social_sentiment":   0.08,
        "news":               0.06,
        "geopolitical":       0.04,
        "sector_rotation":    0.05,
        "onchain":            0.10,
        "volatility_surface": 0.09,
        "liquidation_flow":   0.10,
        "crash_detection":    0.05,
    },
    "HIGH_VOLATILITY": {
        "funding_rate":       0.10,
        "order_book":         0.08,
        "options_flow":       0.15,
        "macro":              0.08,
        "social_sentiment":   0.05,
        "news":               0.04,
        "geopolitical":       0.05,
        "sector_rotation":    0.02,
        "onchain":            0.10,
        "volatility_surface": 0.18,
        "liquidation_flow":   0.10,
        "crash_detection":    0.05,
    },
    "CRISIS": {
        "funding_rate":       0.08,
        "order_book":         0.05,
        "options_flow":       0.10,
        "macro":              0.15,
        "social_sentiment":   0.05,
        "news":               0.04,
        "geopolitical":       0.08,
        "sector_rotation":    0.02,
        "onchain":            0.10,
        "volatility_surface": 0.08,
        "liquidation_flow":   0.10,
        "crash_detection":    0.15,
    },
    "RECOVERY": {
        "funding_rate":       0.12,
        "order_book":         0.12,
        "options_flow":       0.08,
        "macro":              0.10,
        "social_sentiment":   0.08,
        "news":               0.05,
        "geopolitical":       0.05,
        "sector_rotation":    0.05,
        "onchain":            0.12,
        "volatility_surface": 0.08,
        "liquidation_flow":   0.08,
        "crash_detection":    0.07,
    },
    "ACCUMULATION": {
        "funding_rate":       0.08,
        "order_book":         0.14,
        "options_flow":       0.07,
        "macro":              0.09,
        "social_sentiment":   0.07,
        "news":               0.05,
        "geopolitical":       0.04,
        "sector_rotation":    0.06,
        "onchain":            0.18,
        "volatility_surface": 0.07,
        "liquidation_flow":   0.10,
        "crash_detection":    0.05,
    },
    "DISTRIBUTION": {
        "funding_rate":       0.15,
        "order_book":         0.10,
        "options_flow":       0.12,
        "macro":              0.10,
        "social_sentiment":   0.08,
        "news":               0.05,
        "geopolitical":       0.05,
        "sector_rotation":    0.03,
        "onchain":            0.12,
        "volatility_surface": 0.08,
        "liquidation_flow":   0.07,
        "crash_detection":    0.05,
    },
    "UNKNOWN": {
        "funding_rate":     0.184,
        "order_book":       0.168,
        "options_flow":     0.137,
        "macro":            0.126,
        "social_sentiment": 0.068,
        "news":             0.039,
        "geopolitical":     0.020,
        "sector_rotation":  0.009,
        "onchain":          0.074,
        "volatility_surface": 0.068,
        "liquidation_flow": 0.039,
        "crash_detection":  0.068,
    },
}

# Agent poll intervals (in seconds) for staleness decay calculation
AGENT_POLL_INTERVALS: dict[str, float] = {
    "funding_rate":     300.0,   # 5 minutes
    "order_book":       30.0,    # 30 seconds
    "options_flow":     900.0,   # 15 minutes
    "macro":            3600.0,  # 1 hour
    "social_sentiment": 1800.0,  # 30 minutes
    "news":             900.0,   # 15 minutes
    "geopolitical":     21600.0, # 6 hours
    "sector_rotation":  14400.0, # 4 hours
    "onchain":          3600.0,  # 1 hour
    "volatility_surface": 900.0, # 15 minutes
    "liquidation_flow": 60.0,    # 1 minute
    "crash_detection":  60.0,    # 1 minute
}

# Minimum confidence for an agent's signal to participate in the meta-signal
_MIN_CONFIDENCE = 0.25

# Macro veto thresholds
_VETO_MACRO_RISK  = 0.75   # macro_risk_score above this is "hostile"
_VETO_SIGNAL      = -0.40  # combined macro+social signal below this → veto

# Confidence floor — if overall orchestrator confidence falls below this,
# raise the ConfluenceScorer threshold by _THRESHOLD_RAISE
_LOW_CONFIDENCE_FLOOR = 0.30
_THRESHOLD_RAISE      = 0.10   # +10% harder to get trades through in uncertainty

# Multi-factor veto thresholds
_MULTI_FACTOR_VETO_FUNDING_RATE = -0.6
_MULTI_FACTOR_VETO_ORDER_BOOK   = -0.6
_MULTI_FACTOR_VETO_MACRO        = -0.4


class OrchestratorSignal:
    """Structured output from the OrchestratorEngine."""

    __slots__ = (
        "meta_signal",
        "meta_confidence",
        "direction",
        "macro_veto",
        "macro_risk_score",
        "regime_bias",
        "confluence_threshold_adj",
        "agent_signals",
        "updated_at",
        "staleness_adjusted",
        "regime_weights_applied",
        "effective_agent_count",
        "consensus_score",
        "divergence_penalty",
        "vix_dampener",
        "agent_contributions",      # Phase S2: per-agent attribution diagnostics
    )

    def __init__(
        self,
        meta_signal:              float,
        meta_confidence:          float,
        direction:                str,
        macro_veto:               bool,
        macro_risk_score:         float,
        regime_bias:              str,
        confluence_threshold_adj: float,
        agent_signals:            dict,
        staleness_adjusted:       bool = False,
        regime_weights_applied:   str = "UNKNOWN",
        effective_agent_count:    int = 0,
        consensus_score:          float = 0.5,
        divergence_penalty:       float = 0.0,
        vix_dampener:             float = 1.0,
        agent_contributions:      dict | None = None,
    ):
        self.meta_signal              = meta_signal
        self.meta_confidence          = meta_confidence
        self.direction                = direction
        self.macro_veto               = macro_veto
        self.macro_risk_score         = macro_risk_score
        self.regime_bias              = regime_bias
        self.confluence_threshold_adj = confluence_threshold_adj
        self.agent_signals            = agent_signals
        self.updated_at               = datetime.now(timezone.utc).isoformat()
        self.staleness_adjusted       = staleness_adjusted
        self.regime_weights_applied   = regime_weights_applied
        self.effective_agent_count    = effective_agent_count
        self.consensus_score          = consensus_score
        self.divergence_penalty       = divergence_penalty
        self.vix_dampener             = vix_dampener
        self.agent_contributions      = agent_contributions or {}

    def to_dict(self) -> dict:
        return {
            "meta_signal":              round(self.meta_signal, 4),
            "meta_confidence":          round(self.meta_confidence, 4),
            "direction":                self.direction,
            "macro_veto":               self.macro_veto,
            "macro_risk_score":         round(self.macro_risk_score, 4),
            "regime_bias":              self.regime_bias,
            "confluence_threshold_adj": round(self.confluence_threshold_adj, 4),
            "agent_signals":            self.agent_signals,
            "updated_at":               self.updated_at,
            "source":                   "orchestrator",
            "stale":                    False,
            "staleness_adjusted":       self.staleness_adjusted,
            "regime_weights_applied":   self.regime_weights_applied,
            "effective_agent_count":    self.effective_agent_count,
            "consensus_score":          round(self.consensus_score, 4),
            "divergence_penalty":       round(self.divergence_penalty, 4),
            "vix_dampener":             round(self.vix_dampener, 4),
            "agent_contributions":      self.agent_contributions,
        }


class OrchestratorEngine(QObject):
    """
    Subscribes to all agent signal topics and continuously maintains
    the current OrchestratorSignal.

    The ConfluenceScorer and RiskGate query `get_signal()` during each
    scan cycle.  The signal is always available (never blocks), returning
    a neutral default if agents haven't published yet.

    Features:
    - Staleness-weighted signal decay for each agent
    - Regime-conditional weight tables (7 regimes + UNKNOWN)
    - HMM confidence multiplier from regime model uncertainty
    - Multi-factor veto (funding_rate + order_book + macro alignment)
    - Regime-conditional threshold tightening
    """

    # Qt signal emitted every time the orchestrator recalculates
    signal_updated = Signal(dict)

    def __init__(self, parent=None):
        super().__init__(parent)
        # Per-agent latest signals — populated as agents publish
        self._agent_cache: dict[str, dict] = {}
        self._current_signal: Optional[OrchestratorSignal] = None
        self._prev_veto: bool = False

        # FinBERT sentiment state (Phase 2c)
        self._finbert_sentiment: dict = {}
        self._sentiment_veto: bool = False

        # Subscribe to ALL 12 agent topics so every signal feeds the meta-calculation
        bus.subscribe(Topics.FUNDING_RATE_UPDATED,       self._on_funding_rate)
        bus.subscribe(Topics.ORDERBOOK_SIGNAL,           self._on_order_book)
        bus.subscribe(Topics.OPTIONS_SIGNAL,             self._on_options_flow)
        bus.subscribe(Topics.MACRO_UPDATED,              self._on_macro)
        bus.subscribe(Topics.SOCIAL_SIGNAL,              self._on_social_or_geo_or_sector)
        bus.subscribe(Topics.SENTIMENT_SIGNAL,           self._on_news)
        bus.subscribe(Topics.ONCHAIN_UPDATED,            self._on_onchain)
        bus.subscribe(Topics.VOLATILITY_SURFACE_UPDATED, self._on_volatility_surface)
        bus.subscribe(Topics.LIQUIDATION_FLOW_UPDATED,   self._on_liquidation_flow)
        bus.subscribe(Topics.CRASH_SCORE_UPDATED,        self._on_crash_detection)
        bus.subscribe(Topics.FINBERT_SIGNAL,             self._on_finbert_signal)

    # ── EventBus handlers ─────────────────────────────────────
    # EventBus delivers Event objects; extract .data (dict) before caching.

    @staticmethod
    def _unpack(event) -> dict:
        """Return event.data if event is an Event object, otherwise the event itself."""
        if hasattr(event, "data") and isinstance(event.data, dict):
            return event.data
        if isinstance(event, dict):
            return event
        return {}

    def _on_funding_rate(self, event) -> None:
        self._agent_cache["funding_rate"] = self._unpack(event)
        self._recalculate()

    def _on_order_book(self, event) -> None:
        self._agent_cache["order_book"] = self._unpack(event)
        self._recalculate()

    def _on_options_flow(self, event) -> None:
        self._agent_cache["options_flow"] = self._unpack(event)
        self._recalculate()

    def _on_macro(self, event) -> None:
        self._agent_cache["macro"] = self._unpack(event)
        self._recalculate()

    def _on_social_or_geo_or_sector(self, event) -> None:
        """
        SOCIAL_SIGNAL is shared by three agents, differentiated by 'source'.
        Route each to its own cache slot.
        """
        data   = self._unpack(event)
        source = data.get("source", "social_sentiment")
        if source == "geopolitical":
            self._agent_cache["geopolitical"] = data
        elif source == "sector_rotation":
            self._agent_cache["sector_rotation"] = data
        else:
            self._agent_cache["social_sentiment"] = data
        self._recalculate()

    def _on_news(self, event) -> None:
        self._agent_cache["news"] = self._unpack(event)
        self._recalculate()

    def _on_onchain(self, event) -> None:
        self._agent_cache["onchain"] = self._unpack(event)
        self._recalculate()

    def _on_volatility_surface(self, event) -> None:
        self._agent_cache["volatility_surface"] = self._unpack(event)
        self._recalculate()

    def _on_liquidation_flow(self, event) -> None:
        self._agent_cache["liquidation_flow"] = self._unpack(event)
        self._recalculate()

    def _on_crash_detection(self, event) -> None:
        """Cache crash detection data; high crash score suppresses positive signals."""
        self._agent_cache["crash_detection"] = self._unpack(event)
        self._recalculate()

    def _on_finbert_signal(self, event) -> None:
        """Handle FinBERT sentiment signal from Phase 2c NLP pipeline."""
        data = self._unpack(event)
        self.inject_finbert_signal(data)

    # ── Helper: Staleness-weighted signal decay ───────────────

    def _compute_effective_confidence(
        self, agent_name: str, agent_confidence: float, updated_at_iso: Optional[str]
    ) -> float:
        """
        Apply staleness decay to agent confidence.

        Formula:
          age_seconds = now - agent.updated_at
          effective_confidence = agent_conf * max(0.10, 1.0 - (age_seconds / (2.0 * poll_interval)))

        Returns confidence clamped to [0.0, agent_confidence].
        """
        if updated_at_iso is None:
            return agent_confidence  # No timestamp, assume fresh

        poll_interval = AGENT_POLL_INTERVALS.get(agent_name, 600.0)

        try:
            agent_updated = datetime.fromisoformat(updated_at_iso.replace('Z', '+00:00'))
            now = datetime.now(timezone.utc)
            age_seconds = (now - agent_updated).total_seconds()

            # Decay formula: 1.0 - (age / (2 * poll_interval))
            decay_factor = max(0.10, 1.0 - (age_seconds / (2.0 * poll_interval)))
            effective_conf = agent_confidence * decay_factor
            return min(agent_confidence, effective_conf)
        except Exception:
            # If timestamp parsing fails, use original confidence
            return agent_confidence

    # ── Helper: Select regime-conditional weights ──────────────

    def _get_regime_weights(self, regime: str) -> dict[str, float]:
        """
        Return weight dict for the given regime.
        Maps regime names from RegimeClassifier format to orchestrator format.
        Falls back to DEFAULT_WEIGHTS if regime not found or UNKNOWN.
        """
        # Map lowercase regime names to uppercase table keys
        mapped_regime = REGIME_NAME_MAP.get(regime.lower(), regime.upper())
        if mapped_regime in REGIME_WEIGHTS and REGIME_WEIGHTS[mapped_regime]:
            return REGIME_WEIGHTS[mapped_regime]
        return DEFAULT_WEIGHTS

    # ── Core calculation ──────────────────────────────────────

    def _recalculate(self) -> None:
        """Recompute the meta-signal from the current agent cache."""
        now = datetime.now(timezone.utc)
        agent_summaries: dict[str, dict] = {}
        weighted_parts: list[tuple[str, float, float, float]] = []  # (name, signal, eff_conf, weight)

        # ── Get macro context and regime ───────────────────────
        macro_data       = self._agent_cache.get("macro", {})
        macro_signal     = float(macro_data.get("signal", 0.0))
        macro_risk_score = float(macro_data.get("macro_risk_score", 0.5))
        regime_bias      = macro_data.get("regime_bias", "UNKNOWN")
        regime_confidence = float(macro_data.get("regime_confidence", 0.5))  # HMM state probability

        # ── Select regime-conditional weights ───────────────────
        regime_weights = self._get_regime_weights(regime_bias)

        # ── Process each agent with staleness decay ────────────
        effective_agent_count = 0

        for agent_name in DEFAULT_WEIGHTS.keys():
            cached = self._agent_cache.get(agent_name, {})
            sig   = float(cached.get("signal",     0.0))
            conf  = float(cached.get("confidence", 0.0))
            stale = bool(cached.get("stale",       True))
            updated_at_iso = cached.get("updated_at", None)

            # Compute effective confidence with staleness decay
            effective_conf = self._compute_effective_confidence(agent_name, conf, updated_at_iso)

            agent_summaries[agent_name] = {
                "signal":     round(sig, 4),
                "confidence": round(conf, 4),
                "effective_confidence": round(effective_conf, 4),
                "stale":      stale,
            }

            # Include in weighted meta-signal if not stale and meets minimum confidence
            if not stale and effective_conf >= _MIN_CONFIDENCE:
                weight = regime_weights.get(agent_name, DEFAULT_WEIGHTS[agent_name])

                # Apply HMM confidence multiplier to effective confidence
                hmm_adjusted_conf = effective_conf * regime_confidence

                weighted_parts.append((agent_name, sig, hmm_adjusted_conf, weight))
                effective_agent_count += 1

        # ── Meta-signal calculation ────────────────────────────
        if not weighted_parts:
            meta_sig  = 0.0
            meta_conf = 0.0
            total_wc  = 0.0
        else:
            total_wc = sum(w * c for _, _, c, w in weighted_parts)
            meta_sig = (
                sum(s * c * w for _, s, c, w in weighted_parts) / total_wc
                if total_wc > 0 else 0.0
            )
            meta_conf = (
                sum(c * w for _, _, c, w in weighted_parts) /
                sum(w for _, _, _, w in weighted_parts)
            )

        # ── Per-agent contribution breakdown (Phase S2 diagnostics) ────
        # signed_contribution: exact additive to pre-post-processing meta_sig
        # magnitude_share: |contribution_i| / Σ|contribution_j| — sums to 1.0
        # Computed BEFORE post-processing (consensus, divergence, VIX, veto).
        _agent_contributions: dict[str, dict] = {}
        if total_wc > 0 and weighted_parts:
            _abs_sum = sum(
                abs(s * c * w / total_wc) for _, s, c, w in weighted_parts
            )
            for _name, _s, _c, _w in weighted_parts:
                _sc = (_s * _c * _w) / total_wc
                _ms = abs(_sc) / _abs_sum if _abs_sum > 1e-12 else 0.0
                _agent_contributions[_name] = {
                    "signed_contribution": round(_sc, 6),
                    "magnitude_share":     round(_ms, 4),
                    "weight_used":         round(_w, 4),
                }

        # ── Consensus score ────────────────────────────────────
        # Fraction of active agents agreeing on direction (0.5 = no consensus, 1.0 = unanimous)
        if weighted_parts:
            bull_count = sum(1 for _, s, _, _ in weighted_parts if s > 0.05)
            bear_count = sum(1 for _, s, _, _ in weighted_parts if s < -0.05)
            neutral_count = len(weighted_parts) - bull_count - bear_count
            total_active = len(weighted_parts)
            consensus_score = max(bull_count, bear_count) / total_active if total_active > 0 else 0.5
        else:
            consensus_score = 0.5

        # Consensus confidence boost: unanimous agreement → +10% confidence
        if consensus_score >= 0.80:
            meta_conf = min(1.0, meta_conf * 1.10)

        # ── Divergence penalty ─────────────────────────────────
        # When top-weighted agents disagree with high confidence → reduce meta confidence
        # Top 4 agents by weight
        top_agents = sorted(
            [(agent_name, sig, eff_conf, regime_weights.get(agent_name, DEFAULT_WEIGHTS.get(agent_name, 0.0)))
             for agent_name, sig, eff_conf in [
                 (n, float(self._agent_cache.get(n, {}).get("signal", 0.0)),
                  float(self._agent_cache.get(n, {}).get("confidence", 0.0)))
                 for n in DEFAULT_WEIGHTS.keys()
             ]
             if eff_conf >= _MIN_CONFIDENCE and not self._agent_cache.get(agent_name, {}).get("stale", True)
            ],
            key=lambda x: x[3], reverse=True
        )[:4]
        divergence_penalty = 0.0
        if len(top_agents) >= 2:
            top_sigs = [s for _, s, _, _ in top_agents]
            has_bull = any(s > 0.1 for s in top_sigs)
            has_bear = any(s < -0.1 for s in top_sigs)
            if has_bull and has_bear:
                # Count disagreeing pairs among top agents
                disagree_pairs = sum(
                    1 for i in range(len(top_sigs))
                    for j in range(i + 1, len(top_sigs))
                    if top_sigs[i] * top_sigs[j] < 0  # opposite signs
                )
                divergence_penalty = min(0.30, disagree_pairs * 0.08)
                meta_conf = max(0.0, meta_conf - divergence_penalty)

        # ── Volatility-aware confidence dampener ───────────────
        # If VIX is elevated → dampen meta_confidence (high uncertainty environment)
        vix_dampener = 1.0
        macro_components = self._agent_cache.get("macro", {}).get("components", {})
        vix_current = float(macro_components.get("vix", {}).get("current", 0.0))
        if vix_current > 0:
            if vix_current > 40.0:
                vix_dampener = 0.50   # Crisis VIX
            elif vix_current > 30.0:
                vix_dampener = 0.65   # Elevated fear
            elif vix_current > 22.0:
                vix_dampener = 0.80   # Moderate concern
        meta_conf *= vix_dampener

        # ── Multi-factor veto (funding_rate + order_book + macro alignment) ────
        funding_rate_signal = float(self._agent_cache.get("funding_rate", {}).get("signal", 0.0))
        order_book_signal   = float(self._agent_cache.get("order_book", {}).get("signal", 0.0))

        multi_factor_veto_triggered = (
            funding_rate_signal < _MULTI_FACTOR_VETO_FUNDING_RATE and
            order_book_signal < _MULTI_FACTOR_VETO_ORDER_BOOK and
            macro_signal < _MULTI_FACTOR_VETO_MACRO
        )

        if multi_factor_veto_triggered:
            meta_sig = max(-0.8, meta_sig)

        # ── Macro veto (existing logic) ────────────────────────
        social_signal  = float(self._agent_cache.get("social_sentiment", {}).get("signal", 0.0))
        combined_risk  = (macro_signal * 0.7 + social_signal * 0.3)
        macro_veto     = (
            (macro_risk_score > _VETO_MACRO_RISK and combined_risk < _VETO_SIGNAL) or
            multi_factor_veto_triggered
        )

        # ── Confluence threshold adjustment with regime conditioning ─────
        threshold_adj = 0.0

        # Base adjustment from confidence floor
        if meta_conf < _LOW_CONFIDENCE_FLOOR:
            threshold_adj = _THRESHOLD_RAISE

        # Add veto adjustment
        if macro_veto:
            threshold_adj += _THRESHOLD_RAISE * 2

        # Add regime-conditional tightening
        if regime_bias == "HIGH_VOLATILITY":
            threshold_adj += 0.12
        elif regime_bias == "CRISIS":
            threshold_adj += 0.20

        # ── Direction ─────────────────────────────────────────
        direction = (
            "bullish" if meta_sig >  0.10 else
            "bearish" if meta_sig < -0.10 else
            "neutral"
        )

        # ── Create OrchestratorSignal ──────────────────────────
        orch_signal = OrchestratorSignal(
            meta_signal              = meta_sig,
            meta_confidence          = meta_conf,
            direction                = direction,
            macro_veto               = macro_veto,
            macro_risk_score         = macro_risk_score,
            regime_bias              = regime_bias,
            confluence_threshold_adj = threshold_adj,
            agent_signals            = agent_summaries,
            staleness_adjusted       = True,  # Staleness decay is always applied
            regime_weights_applied   = regime_bias,
            effective_agent_count    = effective_agent_count,
            consensus_score          = consensus_score,
            divergence_penalty       = divergence_penalty,
            vix_dampener             = vix_dampener,
            agent_contributions      = _agent_contributions,
        )
        self._current_signal = orch_signal

        payload = orch_signal.to_dict()
        bus.publish(Topics.ORCHESTRATOR_SIGNAL, payload, source="orchestrator")
        self.signal_updated.emit(payload)

        # Publish veto change event
        if macro_veto != self._prev_veto:
            self._prev_veto = macro_veto
            veto_reason = "macro_risk" if macro_risk_score > _VETO_MACRO_RISK else "multi_factor"
            bus.publish(
                Topics.ORCHESTRATOR_VETO,
                {
                    "veto": macro_veto,
                    "reason": f"{veto_reason} | macro_risk={macro_risk_score:.2f}",
                    "multi_factor_triggered": multi_factor_veto_triggered,
                },
                source="orchestrator",
            )
            logger.warning(
                "OrchestratorEngine: VETO %s | macro_risk=%.2f | combined_signal=%.3f | "
                "multi_factor=%s | regime=%s",
                "ACTIVATED" if macro_veto else "CLEARED",
                macro_risk_score,
                combined_risk,
                multi_factor_veto_triggered,
                regime_bias,
            )

        logger.debug(
            "OrchestratorEngine: meta_sig=%+.3f | conf=%.2f | veto=%s | bias=%s | "
            "regime_conf=%.2f | effective_agents=%d | consensus=%.2f | "
            "divergence_penalty=%.2f | vix_dampener=%.2f",
            meta_sig, meta_conf, macro_veto, regime_bias, regime_confidence,
            effective_agent_count, consensus_score, divergence_penalty, vix_dampener,
        )

    # ── FinBERT Sentiment Injection (Phase 2c) ────────────────

    def inject_finbert_signal(self, sentiment_data: dict) -> None:
        """
        Ingest FinBERT sentiment aggregate and apply sentiment veto logic.

        Parameters
        ----------
        sentiment_data : dict
            {
                "symbol": str,
                "direction": str,
                "net_score": float (in [-1, +1]),
                "headline_count": int,
                "confidence": float,
                ...
            }

        Logic:
        - If net_score < -0.60 AND headline_count >= 5: set _sentiment_veto=True
        - If net_score > 0.60 AND headline_count >= 5: lower threshold by 0.05
        - Reset veto when net_score recovers above -0.30
        """
        self._finbert_sentiment = sentiment_data
        net_score = float(sentiment_data.get("net_score", 0.0))
        headline_count = int(sentiment_data.get("headline_count", 0))

        # Strong negative sentiment veto
        if net_score < -0.60 and headline_count >= 5:
            if not self._sentiment_veto:
                self._sentiment_veto = True
                logger.warning(
                    "OrchestratorEngine: SENTIMENT VETO activated | "
                    "net_score=%.2f | headline_count=%d",
                    net_score,
                    headline_count,
                )

        # Reset veto when sentiment recovers
        if net_score > -0.30 and self._sentiment_veto:
            self._sentiment_veto = False
            logger.info(
                "OrchestratorEngine: SENTIMENT VETO cleared | net_score=%.2f",
                net_score,
            )

        # Recalculate to apply sentiment constraints
        self._recalculate()

    # ── Public API ────────────────────────────────────────────

    def get_signal(self) -> OrchestratorSignal:
        """
        Return the latest OrchestratorSignal.
        Always returns immediately (never blocks).
        Falls back to a neutral default if no data yet.
        """
        if self._current_signal is not None:
            return self._current_signal
        # Default: neutral, not vetoed
        return OrchestratorSignal(
            meta_signal=0.0, meta_confidence=0.0,
            direction="neutral", macro_veto=False,
            macro_risk_score=0.5, regime_bias="UNKNOWN",
            confluence_threshold_adj=0.0, agent_signals={},
            staleness_adjusted=False,
            regime_weights_applied="UNKNOWN",
            effective_agent_count=0,
            consensus_score=0.5,
            divergence_penalty=0.0,
            vix_dampener=1.0,
            agent_contributions={},
        )

    def is_veto_active(self) -> bool:
        """Quick check — is a macro veto currently blocking trades?"""
        return self._current_signal.macro_veto if self._current_signal else False

    def get_threshold_adjustment(self) -> float:
        """
        How much the ConfluenceScorer should raise its threshold.
        Returns 0.0 in normal conditions, +0.10+ in uncertain/veto conditions.
        """
        if self._current_signal is None:
            return 0.0
        return self._current_signal.confluence_threshold_adj

    def get_agent_contributions(self) -> dict[str, dict]:
        """Convenience accessor for per-agent attribution diagnostics.
        Primary source of truth is signal.agent_contributions."""
        if self._current_signal is not None:
            return self._current_signal.agent_contributions
        return {}


# ── Module-level singleton ────────────────────────────────────
_engine: Optional[OrchestratorEngine] = None


def get_orchestrator() -> OrchestratorEngine:
    """Return the global OrchestratorEngine, creating it if needed."""
    global _engine
    if _engine is None:
        _engine = OrchestratorEngine()
    return _engine
