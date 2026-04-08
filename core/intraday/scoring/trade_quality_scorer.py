# ============================================================
# NEXUS TRADER — Trade Quality Score (TQS)  (Phase 5B Wave 1)
#
# Composite quality score (0.0–1.0) for inbound TriggerSignals.
# Pure function: no state, no side effects, fully deterministic.
#
# Components (weighted):
#   1. Setup Quality     (0.30) — trigger_quality from strategy
#   2. Trigger Strength  (0.25) — trigger.strength
#   3. Market Context    (0.25) — regime confidence + R:R quality
#   4. Execution Context (0.20) — capital availability + portfolio heat
#
# Integration: ProcessingEngine._apply_tqs() → reject if below floor
#
# ZERO PySide6 imports.
# ============================================================
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Dict

from core.intraday.execution_contracts import PortfolioSnapshot
from core.intraday.signal_contracts import TriggerSignal

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TQSConfig:
    """Configuration for Trade Quality Scorer."""
    # Component weights (must sum to 1.0)
    w_setup_quality: float = 0.30
    w_trigger_strength: float = 0.25
    w_market_context: float = 0.25
    w_execution_context: float = 0.20

    # Rejection floor: trades below this TQS are rejected
    min_tqs: float = 0.25

    # Market context sub-weights
    regime_conf_weight: float = 0.60   # Within market context
    rr_quality_weight: float = 0.40    # Within market context

    # R:R reference for quality mapping
    rr_excellent: float = 3.0   # R:R >= 3.0 → 1.0 quality
    rr_good: float = 2.0       # R:R >= 2.0 → 0.7 quality
    rr_minimum: float = 1.0    # R:R >= 1.0 → 0.3 quality (below = 0.0)

    # Execution context sub-parameters
    capital_avail_floor: float = 0.10   # Below 10% available → 0.0
    capital_avail_good: float = 0.50    # Above 50% available → 1.0
    heat_ceiling: float = 0.06          # At 6% heat → 0.0 execution score

    def __post_init__(self):
        total = (self.w_setup_quality + self.w_trigger_strength
                 + self.w_market_context + self.w_execution_context)
        if abs(total - 1.0) > 0.001:
            raise ValueError(
                f"TQS weights must sum to 1.0, got {total:.3f}"
            )


@dataclass(frozen=True)
class TQSResult:
    """Immutable result of TQS evaluation."""
    score: float                    # Final composite 0.0–1.0
    setup_quality: float            # Component 1
    trigger_strength: float         # Component 2
    market_context: float           # Component 3
    execution_context: float        # Component 4
    passed: bool                    # score >= min_tqs
    reason: str = ""                # Non-empty if rejected

    def to_dict(self) -> dict:
        return {
            "tqs_score": round(self.score, 4),
            "setup_quality": round(self.setup_quality, 4),
            "trigger_strength": round(self.trigger_strength, 4),
            "market_context": round(self.market_context, 4),
            "execution_context": round(self.execution_context, 4),
            "passed": self.passed,
            "reason": self.reason,
        }


class TradeQualityScorer:
    """
    Pure-function Trade Quality Scorer.

    Computes a composite 0.0–1.0 score from four orthogonal components:
    setup quality, trigger strength, market context, and execution context.

    Stateless: safe to call from any thread, fully deterministic for
    identical inputs.
    """

    def __init__(self, config: TQSConfig = None):
        self.config = config or TQSConfig()
        logger.info(
            "TradeQualityScorer initialized: min_tqs=%.2f, "
            "weights=[%.2f, %.2f, %.2f, %.2f]",
            self.config.min_tqs,
            self.config.w_setup_quality,
            self.config.w_trigger_strength,
            self.config.w_market_context,
            self.config.w_execution_context,
        )

    def evaluate(
        self,
        trigger: TriggerSignal,
        snapshot: PortfolioSnapshot,
    ) -> TQSResult:
        """
        Evaluate trade quality for a trigger signal.

        Parameters
        ----------
        trigger : TriggerSignal
            The trigger signal to score
        snapshot : PortfolioSnapshot
            Current portfolio state for execution context

        Returns
        -------
        TQSResult with composite score and component breakdown
        """
        cfg = self.config

        # ── Component 1: Setup Quality (from strategy evaluation) ──
        setup_q = _clamp(trigger.trigger_quality)

        # ── Component 2: Trigger Strength ──────────────────────────
        trig_s = _clamp(trigger.strength)

        # ── Component 3: Market Context ────────────────────────────
        regime_score = _clamp(trigger.regime_confidence)
        rr_score = self._score_risk_reward(trigger.risk_reward_ratio)
        market_ctx = (
            cfg.regime_conf_weight * regime_score
            + cfg.rr_quality_weight * rr_score
        )

        # ── Component 4: Execution Context ─────────────────────────
        exec_ctx = self._score_execution_context(snapshot)

        # ── Weighted composite ─────────────────────────────────────
        score = (
            cfg.w_setup_quality * setup_q
            + cfg.w_trigger_strength * trig_s
            + cfg.w_market_context * market_ctx
            + cfg.w_execution_context * exec_ctx
        )
        score = _clamp(score)

        passed = score >= cfg.min_tqs
        reason = ""
        if not passed:
            reason = (
                f"TQS {score:.3f} below floor {cfg.min_tqs:.2f} "
                f"(setup={setup_q:.2f}, trigger={trig_s:.2f}, "
                f"market={market_ctx:.2f}, exec={exec_ctx:.2f})"
            )

        logger.debug(
            "TQS: score=%.3f [setup=%.2f trig=%.2f market=%.2f exec=%.2f] "
            "passed=%s symbol=%s",
            score, setup_q, trig_s, market_ctx, exec_ctx,
            passed, trigger.symbol,
        )

        return TQSResult(
            score=score,
            setup_quality=setup_q,
            trigger_strength=trig_s,
            market_context=market_ctx,
            execution_context=exec_ctx,
            passed=passed,
            reason=reason,
        )

    def _score_risk_reward(self, rr: float) -> float:
        """Map R:R ratio to 0.0–1.0 quality score."""
        cfg = self.config
        if rr >= cfg.rr_excellent:
            return 1.0
        if rr >= cfg.rr_good:
            # Linear interpolation between good and excellent
            return 0.7 + 0.3 * (rr - cfg.rr_good) / (cfg.rr_excellent - cfg.rr_good)
        if rr >= cfg.rr_minimum:
            # Linear interpolation between minimum and good
            return 0.3 + 0.4 * (rr - cfg.rr_minimum) / (cfg.rr_good - cfg.rr_minimum)
        return 0.0

    def _score_execution_context(self, snapshot: PortfolioSnapshot) -> float:
        """Score execution conditions from portfolio state."""
        cfg = self.config

        # Capital availability score
        if snapshot.capital.total_capital <= 0:
            capital_score = 0.0
        else:
            avail_ratio = (
                snapshot.capital.available_capital
                / snapshot.capital.total_capital
            )
            if avail_ratio <= cfg.capital_avail_floor:
                capital_score = 0.0
            elif avail_ratio >= cfg.capital_avail_good:
                capital_score = 1.0
            else:
                capital_score = (avail_ratio - cfg.capital_avail_floor) / (
                    cfg.capital_avail_good - cfg.capital_avail_floor
                )

        # Portfolio heat score (inverse: more heat = lower score)
        heat = snapshot.exposure.portfolio_heat
        if heat >= cfg.heat_ceiling:
            heat_score = 0.0
        elif heat <= 0:
            heat_score = 1.0
        else:
            heat_score = 1.0 - (heat / cfg.heat_ceiling)

        # Average the two sub-scores
        return (capital_score + heat_score) / 2.0


def _clamp(v: float, lo: float = 0.0, hi: float = 1.0) -> float:
    """Clamp value to [lo, hi]."""
    return max(lo, min(hi, v))
