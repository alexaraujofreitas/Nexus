# ============================================================
# NEXUS TRADER — Order Placement Optimizer (Phase 6 v3)
#
# SCORING MODEL replacing binary cascade. Each candidate strategy
# gets a score based on:
#   - Fill probability (base + regime + symbol adjustments)
#   - Edge decay cost (time × rate)
#   - Spread cost (spread_pct × cost_fraction)
#   - Fee savings (taker - effective_rate)
#   - Urgency factor (quality × weight)
#
# Highest-scoring viable strategy wins. All negative → REJECT.
#
# COMPONENT CLASSIFICATION: Advisory-only
#   - Produces PlacementDecision recommendations
#   - Does NOT bypass ExecutionEngine boundary
#   - ExecutionEngine may override or reject any recommendation
#   - Allowed outputs: PlacementStrategy, limit_price, timeout_ms
#   - Forbidden outputs: direct order submission, risk override
#
# Design invariants:
#   - All decisions logged and auditable
#   - Deterministic: identical inputs → identical output
#   - No Qt/GUI imports
#   - FAIL-CLOSED: rejects on any error or missing critical input
#     (never defaults to MARKET on error)
# ============================================================
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Deque, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════
# ORDER PLACEMENT STRATEGY
# ══════════════════════════════════════════════════════════════

class PlacementStrategy(str, Enum):
    """Order placement strategy decision."""
    MARKET = "market"       # Immediate fill, taker fee
    LIMIT_PASSIVE = "limit_passive"  # Post-only at best bid/ask
    LIMIT_AGGRESSIVE = "limit_aggressive"  # Inside spread, likely maker
    LIMIT_THEN_MARKET = "limit_then_market"  # Try limit, escalate to market
    REJECT = "reject"       # Fail-closed: conditions unsafe for any order


# ══════════════════════════════════════════════════════════════
# REJECTION REASON CODES
# ══════════════════════════════════════════════════════════════

class RejectionReason(str, Enum):
    """Explicit reason codes for REJECT decisions."""
    WIDE_SPREAD = "wide_spread"          # Spread too wide for safe execution
    MISSING_PRICE = "missing_price"      # Price is None/zero/negative
    MISSING_SPREAD = "missing_spread"    # Spread data unavailable
    INVALID_SIDE = "invalid_side"        # Side not 'buy' or 'sell'
    INVALID_SIZE = "invalid_size"        # Size is None/zero/negative
    INTERNAL_ERROR = "internal_error"    # Unexpected error in decision logic
    NO_VIABLE_STRATEGY = "no_viable_strategy"  # All scores negative


# ══════════════════════════════════════════════════════════════
# CONFIGURATION
# ══════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class OrderPlacementConfig:
    """Configuration for order placement optimization."""

    # ── Fee rates (used to compute maker savings)
    maker_fee_rate: float = 0.0002    # 0.02% (2 bps)
    taker_fee_rate: float = 0.0004    # 0.04% (4 bps)

    # ── Expected fill times (ms) for each strategy
    # Used in edge decay cost calculation
    expected_fill_time_market: int = 0         # Immediate
    expected_fill_time_limit_passive: int = 15000   # 15 seconds
    expected_fill_time_limit_aggressive: int = 5000  # 5 seconds
    expected_fill_time_limit_then_market: int = 8000  # 8 seconds

    # ── Edge decay rate (R value per ms)
    # How much signal edge decays per millisecond
    edge_decay_rate_per_ms: float = 0.0001  # 0.01% R per ms

    # ── Spread cost fractions (proportion of spread to charge)
    # What fraction of the spread width is paid as adverse selection
    spread_cost_fraction_market: float = 1.0       # Pay full spread
    spread_cost_fraction_limit_passive: float = 0.0   # Pay zero (post-only)
    spread_cost_fraction_limit_aggressive: float = 0.3  # Pay 30% of spread
    spread_cost_fraction_limit_then_market: float = 0.5  # Pay 50% of spread

    # ── Base fill probabilities (before regime/symbol adjustment)
    base_fill_prob_market: float = 1.0
    base_fill_prob_limit_passive: float = 0.5
    base_fill_prob_limit_aggressive: float = 0.75
    base_fill_prob_limit_then_market: float = 0.9

    # ── Urgency weights (how much signal quality affects this strategy)
    urgency_weight_market: float = 1.0
    urgency_weight_limit_passive: float = 0.2
    urgency_weight_limit_aggressive: float = 0.5
    urgency_weight_limit_then_market: float = 0.7

    # ── Fee saving weight (how much fee savings factor into score)
    fee_saving_weight: float = 1.0

    # ── Spread thresholds for safety gates
    # spread_pct > wide_spread → REJECT (spread too wide for safe execution)
    wide_spread_pct: float = 0.0010   # 10 bps

    # Limit price computation
    limit_spread_fraction: float = 0.30  # Place 30% inside spread

    # Max time to wait for limit fill before escalation (ms)
    limit_timeout_ms: int = 30000  # 30s

    # ── Regime fill probability adjustments
    # Per-regime multiplier on base fill probability
    regime_fill_adj: Dict[str, float] = field(default_factory=lambda: {
        "bull_trend": 0.6,
        "bear_trend": 0.6,
        "range_bound": 1.1,
        "high_volatility": 0.7,
        "uncertain": 0.8,
    })

    # Default adjustment for unknown regimes
    regime_fill_adj_default: float = 1.0

    # ── Symbol-specific fill rate floor
    min_fill_rate_from_history: float = 0.5  # Absolute floor for any symbol


# ══════════════════════════════════════════════════════════════
# PLACEMENT DECISION
# ══════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class PlacementDecision:
    """Result of order placement optimization.

    Immutable. Fully traceable — includes all inputs that drove
    the decision, winning score, all candidate scores, and factor
    breakdowns for the winner.
    """
    strategy: PlacementStrategy
    winning_score: float           # Score of winning strategy
    scores: Dict[str, float]       # All candidate scores for audit
    score_breakdown: Dict[str, float]  # Factor breakdown for winner
    limit_price: Optional[float]   # Limit price (None for MARKET/REJECT)
    timeout_ms: Optional[int]      # Escalation timeout (None for MARKET/REJECT)
    reason: str                    # Human-readable explanation
    rejection_reason: Optional[RejectionReason]  # Set when strategy=REJECT

    # ── Decision inputs (for audit)
    signal_quality: float
    signal_age_ms: int
    spread_pct: float
    regime: str
    size_usdt: float
    historical_fill_rate: float

    @property
    def is_rejected(self) -> bool:
        return self.strategy == PlacementStrategy.REJECT

    def to_dict(self) -> dict:
        return {
            "strategy": self.strategy.value,
            "winning_score": self.winning_score,
            "scores": self.scores,
            "score_breakdown": self.score_breakdown,
            "limit_price": self.limit_price,
            "timeout_ms": self.timeout_ms,
            "reason": self.reason,
            "rejection_reason": self.rejection_reason.value if self.rejection_reason else None,
            "signal_quality": self.signal_quality,
            "signal_age_ms": self.signal_age_ms,
            "spread_pct": self.spread_pct,
            "regime": self.regime,
            "size_usdt": self.size_usdt,
            "historical_fill_rate": self.historical_fill_rate,
        }


# ══════════════════════════════════════════════════════════════
# ORDER PLACEMENT OPTIMIZER
# ══════════════════════════════════════════════════════════════

class OrderPlacementOptimizer:
    """Determines optimal order type and placement parameters via scoring model.

    Consumes market context (spread, regime, signal quality) and
    produces a PlacementDecision with full audit trail and score breakdown.

    SCORING MODEL:
    For each strategy, compute:
      edge_decay_cost = expected_fill_time * edge_decay_rate_per_ms
      spread_cost = spread_pct * spread_cost_fraction
      fill_probability = base_fill_prob * regime_adj * symbol_adj
      urgency_factor = urgency_weight * signal_quality
      fee_saving = (taker_rate - effective_rate) * size_usdt

      score = (fill_probability * (urgency_factor + fee_saving_weight * fee_saving / size_usdt))
              - edge_decay_cost
              - spread_cost

    Highest score wins. All negative → REJECT(NO_VIABLE_STRATEGY).

    FAIL-CLOSED POLICY:
    - Missing/invalid price → REJECT(MISSING_PRICE)
    - Missing spread data → REJECT(MISSING_SPREAD)
    - Invalid side → REJECT(INVALID_SIDE)
    - Invalid size → REJECT(INVALID_SIZE)
    - Wide spread → REJECT(WIDE_SPREAD)
    - Any unexpected error → REJECT(INTERNAL_ERROR)
    - NEVER defaults to MARKET on error

    Usage:
        decision = optimizer.decide(
            side="buy", price=50000.0, size_usdt=500.0,
            signal_quality=0.72, signal_age_ms=3000,
            spread_pct=0.0005, regime="range_bound",
            bid=49990.0, ask=50010.0, symbol="BTCUSDT",
        )
        if decision.is_rejected:
            log.warning("Order rejected: %s", decision.reason)
        else:
            # decision.strategy, decision.limit_price, decision.timeout_ms
    """

    def __init__(self, config: OrderPlacementConfig = None):
        self.config = config or OrderPlacementConfig()

        # Historical fill tracking per symbol
        self._fill_history: Dict[str, Deque[bool]] = {}  # symbol → [filled_as_limit?]
        self._fill_window = 50

        logger.info(
            "OrderPlacementOptimizer initialized (FAIL-CLOSED, SCORING MODEL): "
            "edge_decay=%f R/ms, wide_spread=%.1f bps (→REJECT), limit_timeout=%dms",
            self.config.edge_decay_rate_per_ms,
            self.config.wide_spread_pct * 10000,
            self.config.limit_timeout_ms,
        )

    def decide(
        self,
        side: str,
        price: float,
        size_usdt: float,
        signal_quality: float,
        signal_age_ms: int,
        spread_pct: float,
        regime: str = "",
        bid: float = None,
        ask: float = None,
        symbol: str = "",
    ) -> PlacementDecision:
        """Determine optimal order placement via scoring model.

        FAIL-CLOSED: validates all inputs before scoring.
        Returns REJECT on any invalid/missing critical input.

        Args:
            side: "buy" or "sell"
            price: Current market price (mid or last)
            size_usdt: Order size in USDT
            signal_quality: Trigger quality score (0–1)
            signal_age_ms: Time since signal generation (ms)
            spread_pct: Current bid-ask spread as fraction
            regime: Market regime string
            bid: Current best bid (optional, for limit price calc)
            ask: Current best ask (optional, for limit price calc)
            symbol: Symbol for fill rate lookup

        Returns:
            PlacementDecision with strategy, scores, and audit trail
        """
        cfg = self.config

        # ── INPUT VALIDATION (fail-closed) ───────────────────
        try:
            # Validate price
            if price is None or price <= 0:
                return self._reject_decision(
                    RejectionReason.MISSING_PRICE,
                    f"Invalid price: {price}",
                    signal_quality, signal_age_ms, spread_pct or 0,
                    regime, size_usdt or 0, 0.0,
                )

            # Validate side
            if side not in ("buy", "sell"):
                return self._reject_decision(
                    RejectionReason.INVALID_SIDE,
                    f"Invalid side: {side}",
                    signal_quality, signal_age_ms, spread_pct or 0,
                    regime, size_usdt or 0, 0.0,
                )

            # Validate size
            if size_usdt is None or size_usdt <= 0:
                return self._reject_decision(
                    RejectionReason.INVALID_SIZE,
                    f"Invalid size: {size_usdt}",
                    signal_quality, signal_age_ms, spread_pct or 0,
                    regime, 0.0, 0.0,
                )

            # Validate spread (required for safe placement)
            if spread_pct is None or spread_pct < 0:
                return self._reject_decision(
                    RejectionReason.MISSING_SPREAD,
                    f"Missing/invalid spread: {spread_pct}",
                    signal_quality, signal_age_ms, 0.0,
                    regime, size_usdt, 0.0,
                )

            # Wide spread → REJECT immediately
            if spread_pct >= cfg.wide_spread_pct:
                return self._reject_decision(
                    RejectionReason.WIDE_SPREAD,
                    f"Wide spread: {spread_pct*10000:.1f}bps≥{cfg.wide_spread_pct*10000:.1f}bps "
                    f"(thin book, high adverse selection risk)",
                    signal_quality, signal_age_ms, spread_pct,
                    regime, size_usdt, self._get_fill_rate(symbol),
                )

            # Get historical fill rate
            fill_rate = self._get_fill_rate(symbol)

            # ── COMPUTE SCORES FOR ALL STRATEGIES ────────────
            scores = self._compute_all_scores(
                signal_quality, spread_pct, regime, size_usdt, fill_rate,
            )

            # Find winning strategy (highest score)
            best_strategy = None
            best_score = None
            for strategy_name, score in scores.items():
                if best_score is None or score > best_score:
                    best_score = score
                    best_strategy = strategy_name

            # All scores negative → REJECT
            if best_score < 0:
                return self._reject_decision(
                    RejectionReason.NO_VIABLE_STRATEGY,
                    f"All strategies scored negative (best={best_score:.4f}). "
                    f"Spread or conditions unfavorable.",
                    signal_quality, signal_age_ms, spread_pct,
                    regime, size_usdt, fill_rate,
                    scores=scores,
                )

            # Map strategy name to enum
            strategy_enum = PlacementStrategy[best_strategy]

            # Compute limit price if strategy is limit-based
            limit_price = None
            if strategy_enum != PlacementStrategy.MARKET:
                limit_price = self._compute_limit_price(side, price, spread_pct, bid, ask)

            # Get score breakdown for winner
            score_breakdown = self._get_score_breakdown(
                best_strategy, signal_quality, spread_pct, regime, size_usdt, fill_rate,
            )

            # Build reason
            reason = (
                f"{best_strategy} strategy selected (score={best_score:.4f}). "
                f"Alternatives: {self._scores_summary(scores, best_strategy)}"
            )

            decision = PlacementDecision(
                strategy=strategy_enum,
                winning_score=best_score,
                scores=scores,
                score_breakdown=score_breakdown,
                limit_price=limit_price,
                timeout_ms=cfg.limit_timeout_ms if strategy_enum in (
                    PlacementStrategy.LIMIT_PASSIVE,
                    PlacementStrategy.LIMIT_AGGRESSIVE,
                    PlacementStrategy.LIMIT_THEN_MARKET,
                ) else None,
                reason=reason,
                rejection_reason=None,
                signal_quality=signal_quality,
                signal_age_ms=signal_age_ms,
                spread_pct=spread_pct,
                regime=regime,
                size_usdt=size_usdt,
                historical_fill_rate=fill_rate,
            )

            logger.info(
                "OrderPlacement: %s %s @ $%.2f → %s (score=%.4f, limit=$%.2f) — %s",
                side, symbol, price, strategy_enum.value,
                best_score, limit_price or 0, reason,
            )
            return decision

        except Exception as e:
            # FAIL-CLOSED: any unexpected error → REJECT, never MARKET
            logger.error(
                "OrderPlacement INTERNAL ERROR → REJECT: %s", e, exc_info=True,
            )
            return self._reject_decision(
                RejectionReason.INTERNAL_ERROR,
                f"Internal error: {e}",
                signal_quality or 0, signal_age_ms or 0,
                spread_pct or 0, regime or "", size_usdt or 0, 0.0,
            )

    # ── Scoring model ────────────────────────────────────────

    def _compute_all_scores(
        self,
        signal_quality: float,
        spread_pct: float,
        regime: str,
        size_usdt: float,
        fill_rate: float,
    ) -> Dict[str, float]:
        """Compute scores for all strategy candidates.

        Returns:
            Dict mapping strategy name (uppercase) to score (float).
        """
        cfg = self.config
        scores = {}

        # Strategy configurations: (name, expected_fill_time, spread_cost_frac,
        #                          base_fill_prob, urgency_weight, effective_fee_rate)
        strategies = [
            ("MARKET", cfg.expected_fill_time_market,
             cfg.spread_cost_fraction_market,
             cfg.base_fill_prob_market, cfg.urgency_weight_market,
             cfg.taker_fee_rate),
            ("LIMIT_PASSIVE", cfg.expected_fill_time_limit_passive,
             cfg.spread_cost_fraction_limit_passive,
             cfg.base_fill_prob_limit_passive, cfg.urgency_weight_limit_passive,
             cfg.maker_fee_rate),
            ("LIMIT_AGGRESSIVE", cfg.expected_fill_time_limit_aggressive,
             cfg.spread_cost_fraction_limit_aggressive,
             cfg.base_fill_prob_limit_aggressive, cfg.urgency_weight_limit_aggressive,
             cfg.maker_fee_rate),
            ("LIMIT_THEN_MARKET", cfg.expected_fill_time_limit_then_market,
             cfg.spread_cost_fraction_limit_then_market,
             cfg.base_fill_prob_limit_then_market, cfg.urgency_weight_limit_then_market,
             (cfg.maker_fee_rate + cfg.taker_fee_rate) / 2.0),
        ]

        for strategy_name, expected_fill_time, spread_cost_frac, base_fill_prob, \
            urgency_weight, effective_fee_rate in strategies:

            # Edge decay cost
            edge_decay_cost = expected_fill_time * cfg.edge_decay_rate_per_ms

            # Spread cost
            spread_cost = spread_pct * spread_cost_frac

            # Fill probability (regime + symbol adjustments)
            regime_adj = cfg.regime_fill_adj.get(regime, cfg.regime_fill_adj_default)
            # Symbol adjustment: clamp history-based fill_rate to minimum
            symbol_adj = max(fill_rate, cfg.min_fill_rate_from_history)
            fill_probability = base_fill_prob * regime_adj * symbol_adj

            # Urgency factor
            urgency_factor = urgency_weight * signal_quality

            # Fee saving (as fraction of notional)
            fee_saving_pct = cfg.taker_fee_rate - effective_fee_rate
            fee_saving_term = cfg.fee_saving_weight * fee_saving_pct

            # Composite score
            score = (fill_probability * (urgency_factor + fee_saving_term)) - edge_decay_cost - spread_cost

            scores[strategy_name] = score

        return scores

    def _get_score_breakdown(
        self,
        strategy_name: str,
        signal_quality: float,
        spread_pct: float,
        regime: str,
        size_usdt: float,
        fill_rate: float,
    ) -> Dict[str, float]:
        """Get detailed factor breakdown for a single strategy."""
        cfg = self.config

        # Map strategy name to config
        strategy_map = {
            "MARKET": (cfg.expected_fill_time_market,
                       cfg.spread_cost_fraction_market,
                       cfg.base_fill_prob_market, cfg.urgency_weight_market,
                       cfg.taker_fee_rate),
            "LIMIT_PASSIVE": (cfg.expected_fill_time_limit_passive,
                              cfg.spread_cost_fraction_limit_passive,
                              cfg.base_fill_prob_limit_passive,
                              cfg.urgency_weight_limit_passive,
                              cfg.maker_fee_rate),
            "LIMIT_AGGRESSIVE": (cfg.expected_fill_time_limit_aggressive,
                                 cfg.spread_cost_fraction_limit_aggressive,
                                 cfg.base_fill_prob_limit_aggressive,
                                 cfg.urgency_weight_limit_aggressive,
                                 cfg.maker_fee_rate),
            "LIMIT_THEN_MARKET": (cfg.expected_fill_time_limit_then_market,
                                  cfg.spread_cost_fraction_limit_then_market,
                                  cfg.base_fill_prob_limit_then_market,
                                 cfg.urgency_weight_limit_then_market,
                                 (cfg.maker_fee_rate + cfg.taker_fee_rate) / 2.0),
        }

        if strategy_name not in strategy_map:
            return {}

        expected_fill_time, spread_cost_frac, base_fill_prob, \
            urgency_weight, effective_fee_rate = strategy_map[strategy_name]

        # Compute components
        edge_decay_cost = expected_fill_time * cfg.edge_decay_rate_per_ms
        spread_cost = spread_pct * spread_cost_frac
        regime_adj = cfg.regime_fill_adj.get(regime, cfg.regime_fill_adj_default)
        symbol_adj = max(fill_rate, cfg.min_fill_rate_from_history)
        fill_probability = base_fill_prob * regime_adj * symbol_adj
        urgency_factor = urgency_weight * signal_quality
        fee_saving_pct = cfg.taker_fee_rate - effective_fee_rate
        fee_saving_term = cfg.fee_saving_weight * fee_saving_pct

        return {
            "fill_probability": fill_probability,
            "base_fill_prob": base_fill_prob,
            "regime_adjustment": regime_adj,
            "symbol_adjustment": symbol_adj,
            "urgency_factor": urgency_factor,
            "fee_saving_term": fee_saving_term,
            "edge_decay_cost": edge_decay_cost,
            "spread_cost": spread_cost,
            "expected_fill_time_ms": expected_fill_time,
            "spread_cost_fraction": spread_cost_frac,
            "effective_fee_rate": effective_fee_rate,
        }

    def _scores_summary(self, scores: Dict[str, float], winner: str) -> str:
        """Format other scores for log message."""
        items = []
        for strategy, score in sorted(scores.items()):
            if strategy != winner:
                items.append(f"{strategy}={score:.4f}")
        return ", ".join(items) if items else "none"

    # ── Fill tracking ────────────────────────────────────────

    def record_fill_outcome(
        self, symbol: str, was_limit_fill: bool,
    ) -> None:
        """Record whether a limit order was filled (for fill rate tracking)."""
        if symbol not in self._fill_history:
            self._fill_history[symbol] = deque(maxlen=self._fill_window)
        self._fill_history[symbol].append(was_limit_fill)

    # ── Internal ─────────────────────────────────────────────

    def _reject_decision(
        self,
        rejection_reason: RejectionReason,
        reason: str,
        signal_quality: float,
        signal_age_ms: int,
        spread_pct: float,
        regime: str,
        size_usdt: float,
        fill_rate: float,
        scores: Dict[str, float] = None,
    ) -> PlacementDecision:
        """Create a REJECT decision (fail-closed)."""
        decision = PlacementDecision(
            strategy=PlacementStrategy.REJECT,
            winning_score=-1.0,
            scores=scores or {},
            score_breakdown={},
            limit_price=None,
            timeout_ms=None,
            reason=reason,
            rejection_reason=rejection_reason,
            signal_quality=signal_quality,
            signal_age_ms=signal_age_ms,
            spread_pct=spread_pct,
            regime=regime,
            size_usdt=size_usdt,
            historical_fill_rate=fill_rate,
        )
        logger.warning("OrderPlacement: REJECT(%s) — %s", rejection_reason.value, reason)
        return decision

    def _compute_limit_price(
        self,
        side: str,
        mid_price: float,
        spread_pct: float,
        bid: float = None,
        ask: float = None,
    ) -> float:
        """Compute aggressive limit price inside the spread.

        Places limit order at `limit_spread_fraction` inside the spread
        from the passive side. For BUY: below ask but above mid.
        For SELL: above bid but below mid.
        """
        cfg = self.config
        half_spread = mid_price * spread_pct / 2.0

        if side == "buy":
            if ask is not None and bid is not None:
                spread = ask - bid
                # Place inside spread from ask side
                limit = ask - spread * cfg.limit_spread_fraction
            else:
                # Approximate from mid
                limit = mid_price + half_spread * (1.0 - 2.0 * cfg.limit_spread_fraction)
        else:  # sell
            if ask is not None and bid is not None:
                spread = ask - bid
                limit = bid + spread * cfg.limit_spread_fraction
            else:
                limit = mid_price - half_spread * (1.0 - 2.0 * cfg.limit_spread_fraction)

        return round(limit, 8)

    def _get_fill_rate(self, symbol: str) -> float:
        """Get historical limit fill rate for a symbol.

        Returns ratio of successful limit fills to total limit attempts.
        If no history, returns 1.0 (optimistic).
        """
        if not symbol or symbol not in self._fill_history:
            return 1.0  # No data → optimistic (allow limit)
        history = self._fill_history[symbol]
        if not history:
            return 1.0
        return sum(1 for x in history if x) / len(history)

    def snapshot(self) -> dict:
        """Full audit snapshot."""
        fill_rates = {
            sym: self._get_fill_rate(sym)
            for sym in self._fill_history
        }
        return {
            "fill_rates": fill_rates,
            "config": {
                "maker_fee_rate": self.config.maker_fee_rate,
                "taker_fee_rate": self.config.taker_fee_rate,
                "expected_fill_time_market": self.config.expected_fill_time_market,
                "expected_fill_time_limit_passive": self.config.expected_fill_time_limit_passive,
                "expected_fill_time_limit_aggressive": self.config.expected_fill_time_limit_aggressive,
                "expected_fill_time_limit_then_market": self.config.expected_fill_time_limit_then_market,
                "edge_decay_rate_per_ms": self.config.edge_decay_rate_per_ms,
                "spread_cost_fraction_market": self.config.spread_cost_fraction_market,
                "spread_cost_fraction_limit_passive": self.config.spread_cost_fraction_limit_passive,
                "spread_cost_fraction_limit_aggressive": self.config.spread_cost_fraction_limit_aggressive,
                "spread_cost_fraction_limit_then_market": self.config.spread_cost_fraction_limit_then_market,
                "base_fill_prob_market": self.config.base_fill_prob_market,
                "base_fill_prob_limit_passive": self.config.base_fill_prob_limit_passive,
                "base_fill_prob_limit_aggressive": self.config.base_fill_prob_limit_aggressive,
                "base_fill_prob_limit_then_market": self.config.base_fill_prob_limit_then_market,
                "urgency_weight_market": self.config.urgency_weight_market,
                "urgency_weight_limit_passive": self.config.urgency_weight_limit_passive,
                "urgency_weight_limit_aggressive": self.config.urgency_weight_limit_aggressive,
                "urgency_weight_limit_then_market": self.config.urgency_weight_limit_then_market,
                "fee_saving_weight": self.config.fee_saving_weight,
                "wide_spread_pct": self.config.wide_spread_pct,
                "limit_spread_fraction": self.config.limit_spread_fraction,
                "limit_timeout_ms": self.config.limit_timeout_ms,
                "regime_fill_adj": self.config.regime_fill_adj,
                "regime_fill_adj_default": self.config.regime_fill_adj_default,
                "min_fill_rate_from_history": self.config.min_fill_rate_from_history,
            },
        }
