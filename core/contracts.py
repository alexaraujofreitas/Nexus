# ============================================================
# NEXUS TRADER — Interface Contracts  (Phase 2 Addendum)
#
# Strict schemas and validation for inter-layer communication.
# Enforces modular boundaries between:
#   - Connectivity layer  (market data, exchange)
#   - Strategy layer      (signal models)
#   - Core processing     (confluence, risk gate)
#   - Execution layer     (paper/live executor)
#   - UI layer            (optional observer)
#
# Every payload crossing a layer boundary MUST pass validation.
# ============================================================
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════
# 1. ENUMS — Constrain values at the type level
# ══════════════════════════════════════════════════════════════

class Direction(str, Enum):
    """Trade direction. Only two values are valid."""
    LONG = "long"
    SHORT = "short"


class Side(str, Enum):
    """Order side. Maps from Direction for execution."""
    BUY = "buy"
    SELL = "sell"

    @classmethod
    def from_direction(cls, d: Direction) -> "Side":
        return cls.BUY if d == Direction.LONG else cls.SELL


class SignalLayer(str, Enum):
    """Which layer produced a signal — used for audit and enforcement."""
    CONNECTIVITY = "connectivity"    # Raw exchange transport: tickers, WS status, feed events
    DATA = "data"                    # Normalized, validated candle publication (1m..1h)
    STRATEGY = "strategy"
    PROCESSING = "processing"
    EXECUTION = "execution"
    UI = "ui"


class TradeDecisionStatus(str, Enum):
    """Lifecycle of a trade decision through the pipeline."""
    PROPOSED = "proposed"         # Created by ConfluenceScorer
    RISK_APPROVED = "approved"    # Passed RiskGate
    RISK_REJECTED = "rejected"    # Failed RiskGate
    EXECUTED = "executed"         # Accepted by PaperExecutor
    EXPIRED = "expired"           # TTL exceeded


# ══════════════════════════════════════════════════════════════
# 2. LAYER-BOUNDARY CONTRACTS (dataclasses)
# ══════════════════════════════════════════════════════════════

VALID_MODEL_NAMES = frozenset({
    "trend", "momentum_breakout", "mean_reversion",
    "liquidity_sweep", "vwap_reversion", "order_book",
    "funding_rate", "sentiment", "pullback_long",
    "swing_low_continuation", "custom_rule",
})

VALID_REGIMES = frozenset({
    "bull_trend", "bear_trend", "ranging",
    "high_volatility", "low_volatility", "uncertain",
    "trending_up", "trending_down", "",  # empty string = not yet classified
})

VALID_TIMEFRAMES = frozenset({
    "1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "1d",
})


@dataclass(frozen=True)
class SignalContract:
    """
    Defines what a valid ModelSignal must contain.
    Used by validate_signal() to enforce the strategy→processing boundary.
    """
    symbol: str
    model_name: str
    direction: str
    strength: float
    entry_price: float
    stop_loss: float
    take_profit: float
    timeframe: str
    regime: str


@dataclass(frozen=True)
class TradeDecisionContract:
    """
    Defines what a valid OrderCandidate must contain before it can
    enter the execution layer. Enforces the processing→execution boundary.
    """
    symbol: str
    side: str
    entry_price: Optional[float]
    stop_loss_price: float
    take_profit_price: float
    position_size_usdt: float
    score: float
    approved: bool
    risk_reward_ratio: float


@dataclass(frozen=True)
class ExecutionRequestContract:
    """
    Minimal validated payload that the execution layer accepts.
    Only created from a validated, approved TradeDecisionContract.
    """
    symbol: str
    side: str
    entry_price: Optional[float]
    stop_loss_price: float
    take_profit_price: float
    size_usdt: float
    score: float
    risk_reward_ratio: float
    models_fired: tuple[str, ...]
    regime: str
    generated_at: datetime


# ══════════════════════════════════════════════════════════════
# 3. VALIDATION FUNCTIONS
# ══════════════════════════════════════════════════════════════

class ContractViolation(ValueError):
    """Raised when a payload violates an interface contract."""
    pass


def validate_signal(signal) -> list[str]:
    """
    Validate a ModelSignal object against the strategy→processing contract.

    Parameters
    ----------
    signal : ModelSignal or any object with matching attributes

    Returns
    -------
    list[str] : empty if valid, otherwise list of violation messages

    Raises
    ------
    ContractViolation : if called with reject_invalid=True (see validate_signal_strict)
    """
    violations = []

    # Required fields
    for attr in ("symbol", "model_name", "direction", "strength",
                 "entry_price", "stop_loss", "take_profit", "timeframe", "regime"):
        if not hasattr(signal, attr):
            violations.append(f"Missing required field: {attr}")

    if violations:
        return violations  # Can't check values if fields are missing

    # Type/value checks
    if not isinstance(signal.symbol, str) or "/" not in signal.symbol:
        violations.append(f"Invalid symbol format: {signal.symbol!r} (expected 'BASE/QUOTE')")

    if signal.model_name not in VALID_MODEL_NAMES:
        violations.append(f"Unknown model_name: {signal.model_name!r}")

    if signal.direction not in ("long", "short"):
        violations.append(f"Invalid direction: {signal.direction!r} (must be 'long' or 'short')")

    if not (0.0 <= signal.strength <= 1.0):
        violations.append(f"strength out of range [0, 1]: {signal.strength}")

    if signal.entry_price <= 0:
        violations.append(f"entry_price must be positive: {signal.entry_price}")

    if signal.stop_loss <= 0:
        violations.append(f"stop_loss must be positive: {signal.stop_loss}")

    if signal.take_profit <= 0:
        violations.append(f"take_profit must be positive: {signal.take_profit}")

    # SL/TP direction consistency
    if signal.direction == "long":
        if signal.stop_loss >= signal.entry_price:
            violations.append(
                f"Long signal: stop_loss ({signal.stop_loss}) must be < entry_price ({signal.entry_price})"
            )
        if signal.take_profit <= signal.entry_price:
            violations.append(
                f"Long signal: take_profit ({signal.take_profit}) must be > entry_price ({signal.entry_price})"
            )
    elif signal.direction == "short":
        if signal.stop_loss <= signal.entry_price:
            violations.append(
                f"Short signal: stop_loss ({signal.stop_loss}) must be > entry_price ({signal.entry_price})"
            )
        if signal.take_profit >= signal.entry_price:
            violations.append(
                f"Short signal: take_profit ({signal.take_profit}) must be < entry_price ({signal.entry_price})"
            )

    if signal.timeframe not in VALID_TIMEFRAMES:
        violations.append(f"Invalid timeframe: {signal.timeframe!r}")

    return violations


def validate_signal_strict(signal) -> None:
    """Validate a signal and raise ContractViolation if invalid."""
    violations = validate_signal(signal)
    if violations:
        raise ContractViolation(
            f"Signal contract violation ({len(violations)} issues): "
            + "; ".join(violations)
        )


def validate_trade_decision(candidate) -> list[str]:
    """
    Validate an OrderCandidate against the processing→execution contract.

    Parameters
    ----------
    candidate : OrderCandidate or any object with matching attributes

    Returns
    -------
    list[str] : empty if valid, otherwise list of violation messages
    """
    violations = []

    for attr in ("symbol", "side", "stop_loss_price", "take_profit_price",
                 "position_size_usdt", "score", "approved"):
        if not hasattr(candidate, attr):
            violations.append(f"Missing required field: {attr}")

    if violations:
        return violations

    if not isinstance(candidate.symbol, str) or "/" not in candidate.symbol:
        violations.append(f"Invalid symbol format: {candidate.symbol!r}")

    if candidate.side not in ("buy", "sell"):
        violations.append(f"Invalid side: {candidate.side!r} (must be 'buy' or 'sell')")

    if candidate.stop_loss_price <= 0:
        violations.append(f"stop_loss_price must be positive: {candidate.stop_loss_price}")

    if candidate.take_profit_price <= 0:
        violations.append(f"take_profit_price must be positive: {candidate.take_profit_price}")

    if candidate.position_size_usdt <= 0:
        violations.append(f"position_size_usdt must be positive: {candidate.position_size_usdt}")

    if not (0.0 <= candidate.score <= 1.0):
        violations.append(f"score out of range [0, 1]: {candidate.score}")

    return violations


def validate_trade_decision_strict(candidate) -> None:
    """Validate a trade decision and raise ContractViolation if invalid."""
    violations = validate_trade_decision(candidate)
    if violations:
        raise ContractViolation(
            f"Trade decision contract violation ({len(violations)} issues): "
            + "; ".join(violations)
        )


def validate_execution_request(candidate) -> list[str]:
    """
    Final validation before execution. The candidate MUST be approved.

    Parameters
    ----------
    candidate : OrderCandidate that has passed RiskGate

    Returns
    -------
    list[str] : empty if valid, otherwise list of violation messages
    """
    violations = validate_trade_decision(candidate)

    if hasattr(candidate, "approved") and not candidate.approved:
        violations.append("Execution request rejected: candidate.approved is False (must pass RiskGate first)")

    if hasattr(candidate, "risk_reward_ratio"):
        if candidate.risk_reward_ratio < 1.0:
            violations.append(
                f"Execution request rejected: risk_reward_ratio ({candidate.risk_reward_ratio}) < 1.0 minimum"
            )

    return violations


def validate_execution_request_strict(candidate) -> None:
    """Validate an execution request and raise ContractViolation if invalid."""
    violations = validate_execution_request(candidate)
    if violations:
        raise ContractViolation(
            f"Execution request contract violation ({len(violations)} issues): "
            + "; ".join(violations)
        )


# ══════════════════════════════════════════════════════════════
# 4. EVENTBUS TOPIC OWNERSHIP — Layer boundary enforcement
# ══════════════════════════════════════════════════════════════

# Maps EventBus topic prefixes to the layer that MAY publish them.
# Any other layer publishing to these topics is a boundary violation.
TOPIC_LAYER_OWNERSHIP = {
    # Connectivity layer topics — raw exchange transport only
    "market.tick": SignalLayer.CONNECTIVITY,           # Raw ticker data from WS/REST
    "market.ohlcv": SignalLayer.CONNECTIVITY,          # Raw OHLCV from exchange
    "market.orderbook": SignalLayer.CONNECTIVITY,      # Raw order book
    "market.trades": SignalLayer.CONNECTIVITY,          # Raw trade stream
    "system.exchange.": SignalLayer.CONNECTIVITY,
    "system.feed.": SignalLayer.CONNECTIVITY,

    # Data layer topics — normalized, validated candle publication
    "market.candle.": SignalLayer.DATA,                 # All CANDLE_1M..CANDLE_1H topics
    "market.candle_close": SignalLayer.DATA,            # Legacy candle close event
    "market.candle_closed": SignalLayer.DATA,           # Legacy candle closed event

    # Strategy layer topics
    "strategy.": SignalLayer.STRATEGY,

    # Processing layer topics (agents, orchestrator, risk, scoring)
    "agent.": SignalLayer.PROCESSING,
    "orchestrator.": SignalLayer.PROCESSING,
    "crash.": SignalLayer.PROCESSING,
    "risk.": SignalLayer.PROCESSING,
    "scanner.": SignalLayer.PROCESSING,
    "filter.": SignalLayer.PROCESSING,
    "scoring.": SignalLayer.PROCESSING,
    "monitor.": SignalLayer.PROCESSING,
    "intelligence.": SignalLayer.PROCESSING,

    # Execution layer topics
    "order.": SignalLayer.EXECUTION,
    "trade.": SignalLayer.EXECUTION,
    "position.": SignalLayer.EXECUTION,
    "portfolio.": SignalLayer.EXECUTION,
    "account.": SignalLayer.EXECUTION,

    # UI layer topics
    "ui.": SignalLayer.UI,
    "settings.": SignalLayer.UI,
}


def get_topic_owner(topic: str) -> Optional[SignalLayer]:
    """
    Return the layer that owns the given topic, or None if unregistered.
    Matches the longest prefix first.
    """
    best_match = None
    best_len = 0
    for prefix, layer in TOPIC_LAYER_OWNERSHIP.items():
        if topic.startswith(prefix) and len(prefix) > best_len:
            best_match = layer
            best_len = len(prefix)
    return best_match


def check_topic_boundary(topic: str, publishing_layer: SignalLayer) -> Optional[str]:
    """
    Check whether a layer is allowed to publish to a given topic.

    Returns None if allowed, or a violation message if not.
    """
    owner = get_topic_owner(topic)
    if owner is None:
        return None  # Unregistered topic — no enforcement
    if owner != publishing_layer:
        return (
            f"Layer boundary violation: {publishing_layer.value} attempted to publish "
            f"to topic '{topic}' owned by {owner.value}"
        )
    return None


# ══════════════════════════════════════════════════════════════
# 5. CANDLE PAYLOAD CONTRACTS  (Phase 3 — Data Engine)
# ══════════════════════════════════════════════════════════════

VALID_CANDLE_TIMEFRAMES = frozenset({
    "1m", "3m", "5m", "15m", "30m", "1h",
})


@dataclass(frozen=True)
class CandleContract:
    """
    Defines what a valid candle payload must contain when crossing
    any layer boundary (connectivity → processing, processing → strategy).

    Candle payloads published on CANDLE_1M .. CANDLE_1H topics MUST
    conform to this schema.
    """
    timestamp: int          # Open time in epoch milliseconds
    open: float
    high: float
    low: float
    close: float
    volume: float
    symbol: str
    timeframe: str          # One of VALID_CANDLE_TIMEFRAMES
    is_closed: bool


def validate_candle(candle) -> list[str]:
    """
    Validate a candle dict or object against the CandleContract schema.

    Parameters
    ----------
    candle : dict or object with matching attributes

    Returns
    -------
    list[str] : empty if valid, otherwise list of violation messages
    """
    violations = []

    # Support both dict and object access
    def _get(key):
        if isinstance(candle, dict):
            return candle.get(key)
        return getattr(candle, key, None)

    def _has(key):
        if isinstance(candle, dict):
            return key in candle
        return hasattr(candle, key)

    # Required fields
    for field_name in ("timestamp", "open", "high", "low", "close", "volume",
                       "symbol", "timeframe", "is_closed"):
        if not _has(field_name):
            violations.append(f"Missing required candle field: {field_name}")

    if violations:
        return violations  # Can't check values if fields are missing

    ts = _get("timestamp")
    o = _get("open")
    h = _get("high")
    low = _get("low")
    c = _get("close")
    vol = _get("volume")
    sym = _get("symbol")
    tf = _get("timeframe")

    # Timestamp
    if not isinstance(ts, (int, float)) or ts <= 0:
        violations.append(f"Invalid candle timestamp: {ts}")

    # OHLCV positivity
    for name, val in [("open", o), ("high", h), ("low", low), ("close", c)]:
        if not isinstance(val, (int, float)) or val <= 0:
            violations.append(f"Candle {name} must be positive: {val}")

    # Volume non-negative
    if isinstance(vol, (int, float)) and vol < 0:
        violations.append(f"Candle volume must be non-negative: {vol}")

    # High >= Low
    if isinstance(h, (int, float)) and isinstance(low, (int, float)):
        if h < low:
            violations.append(f"Candle high ({h}) < low ({low})")

    # Symbol format
    if not isinstance(sym, str) or "/" not in sym:
        violations.append(f"Invalid candle symbol format: {sym!r}")

    # Timeframe
    if tf not in VALID_CANDLE_TIMEFRAMES:
        violations.append(f"Invalid candle timeframe: {tf!r}")

    return violations


def validate_candle_strict(candle) -> None:
    """Validate a candle and raise ContractViolation if invalid."""
    violations = validate_candle(candle)
    if violations:
        raise ContractViolation(
            f"Candle contract violation ({len(violations)} issues): "
            + "; ".join(violations)
        )


# ══════════════════════════════════════════════════════════════
# 6. CONVENIENCE — Build ExecutionRequestContract from candidate
# ══════════════════════════════════════════════════════════════

def to_execution_request(candidate) -> ExecutionRequestContract:
    """
    Convert a validated, approved OrderCandidate to an ExecutionRequestContract.
    Raises ContractViolation if the candidate is not valid for execution.
    """
    validate_execution_request_strict(candidate)
    return ExecutionRequestContract(
        symbol=candidate.symbol,
        side=candidate.side,
        entry_price=getattr(candidate, "entry_price", None),
        stop_loss_price=candidate.stop_loss_price,
        take_profit_price=candidate.take_profit_price,
        size_usdt=candidate.position_size_usdt,
        score=candidate.score,
        risk_reward_ratio=candidate.risk_reward_ratio,
        models_fired=tuple(getattr(candidate, "models_fired", [])),
        regime=getattr(candidate, "regime", ""),
        generated_at=getattr(candidate, "generated_at", datetime.utcnow()),
    )
