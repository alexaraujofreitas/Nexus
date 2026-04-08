# ============================================================
# NEXUS TRADER — Intraday Execution Contracts  (Phase 5)
#
# Immutable schemas for the execution pipeline:
#   ExecutionIntent   — pre-risk proposed trade (ProcessingEngine)
#   ExecutionDecision  — post-risk verdict (RiskEngine)
#   ExecutionRequest   — validated execution payload (ExecutionEngine)
#   OrderRecord        — order lifecycle (OrderManager)
#   FillRecord         — fill event (FillSimulator)
#   PositionRecord     — position lifecycle (PortfolioState)
#
# Design invariants:
#   - ExecutionIntent and ExecutionDecision are frozen (immutable)
#   - State transitions produce NEW objects, never mutations
#   - Every object carries full traceability chain
#   - Validation at every boundary crossing
#   - ZERO PySide6 imports
# ============================================================
from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from core.intraday.signal_contracts import Direction, StrategyClass

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════
# 1. ENUMS
# ══════════════════════════════════════════════════════════════

class DecisionStatus(str, Enum):
    """Post-risk verdict. Exactly two terminal states."""
    APPROVED = "approved"
    REJECTED = "rejected"


class OrderType(str, Enum):
    MARKET = "market"
    LIMIT = "limit"


class OrderStatus(str, Enum):
    PENDING = "pending"
    FILLED = "filled"
    PARTIALLY_FILLED = "partially_filled"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    FAILED = "failed"


class PositionStatus(str, Enum):
    OPEN = "open"
    PARTIALLY_CLOSED = "partially_closed"
    CLOSED = "closed"


class CloseReason(str, Enum):
    SL_HIT = "sl_hit"
    TP_HIT = "tp_hit"
    TIME_STOP = "time_stop"
    MANUAL = "manual"
    CIRCUIT_BREAKER = "circuit_breaker"
    PARTIAL_TP = "partial_tp"
    KILL_SWITCH = "kill_switch"


class RejectionSource(str, Enum):
    STALE_SIGNAL = "stale_signal"
    PORTFOLIO_HEAT = "portfolio_heat"
    MAX_POSITIONS = "max_positions"
    ASSET_EXPOSURE = "asset_exposure"
    DAILY_LOSS = "daily_loss"
    DRAWDOWN = "drawdown"
    CIRCUIT_BREAKER = "circuit_breaker"
    KILL_SWITCH = "kill_switch"
    DUPLICATE_SYMBOL = "duplicate_symbol"
    INSUFFICIENT_CAPITAL = "insufficient_capital"
    RR_TOO_LOW = "rr_too_low"
    SIZE_TOO_SMALL = "size_too_small"


class CircuitBreakerState(str, Enum):
    NORMAL = "normal"
    WARNING = "warning"
    TRIPPED = "tripped"


class KillSwitchState(str, Enum):
    ARMED = "armed"
    DISARMED = "disarmed"


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"

    @classmethod
    def from_direction(cls, d: Direction) -> Side:
        return cls.BUY if d == Direction.LONG else cls.SELL


# ══════════════════════════════════════════════════════════════
# 2. ID GENERATION
# ══════════════════════════════════════════════════════════════

def _make_id(*parts) -> str:
    """Deterministic ID from arbitrary string parts: SHA-256[:16]."""
    raw = "|".join(str(p) for p in parts)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


# ══════════════════════════════════════════════════════════════
# 3. EXECUTION INTENT (Pre-Risk)
# ══════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class ExecutionIntent:
    """
    Pre-risk proposed trade. Created by ProcessingEngine after
    signal validation and position sizing, BEFORE risk evaluation.
    Immutable. RiskEngine consumes this to produce ExecutionDecision.
    """
    # ── Identity & traceability
    intent_id:          str
    trigger_id:         str
    setup_id:           str

    # ── Trade parameters
    symbol:             str
    direction:          Direction
    strategy_name:      str
    strategy_class:     StrategyClass
    entry_price:        float
    stop_loss:          float
    take_profit:        float
    atr_value:          float

    # ── Sizing (from PositionSizer)
    size_usdt:          float
    quantity:           float
    risk_usdt:          float
    risk_reward_ratio:  float

    # ── Context
    regime:             str
    regime_confidence:  float
    trigger_strength:   float
    trigger_quality:    float

    # ── Timing
    created_at_ms:      int = 0

    # ── Trace chain
    candle_trace_ids:   tuple = ()
    setup_trace_ids:    tuple = ()

    def __post_init__(self):
        if self.created_at_ms == 0:
            object.__setattr__(self, "created_at_ms", int(time.time() * 1000))

    def to_dict(self) -> dict:
        return {
            "intent_id": self.intent_id,
            "trigger_id": self.trigger_id,
            "setup_id": self.setup_id,
            "symbol": self.symbol,
            "direction": self.direction.value,
            "strategy_name": self.strategy_name,
            "strategy_class": self.strategy_class.value,
            "entry_price": self.entry_price,
            "stop_loss": self.stop_loss,
            "take_profit": self.take_profit,
            "atr_value": self.atr_value,
            "size_usdt": self.size_usdt,
            "quantity": self.quantity,
            "risk_usdt": self.risk_usdt,
            "risk_reward_ratio": self.risk_reward_ratio,
            "regime": self.regime,
            "regime_confidence": self.regime_confidence,
            "trigger_strength": self.trigger_strength,
            "trigger_quality": self.trigger_quality,
            "created_at_ms": self.created_at_ms,
            "candle_trace_ids": list(self.candle_trace_ids),
            "setup_trace_ids": list(self.setup_trace_ids),
        }


# ══════════════════════════════════════════════════════════════
# 4. EXECUTION DECISION (Post-Risk)
# ══════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class ExecutionDecision:
    """
    Post-risk verdict. Created by RiskEngine from an ExecutionIntent.
    Immutable. Status is always APPROVED or REJECTED (terminal).
    """
    # ── Identity & traceability
    decision_id:        str
    intent_id:          str
    trigger_id:         str
    setup_id:           str

    # ── Trade parameters (may differ from intent if WARNING scaled)
    symbol:             str
    direction:          Direction
    strategy_name:      str
    strategy_class:     StrategyClass
    entry_price:        float
    stop_loss:          float
    take_profit:        float

    # ── Final sizing
    final_size_usdt:    float
    final_quantity:     float
    risk_usdt:          float
    risk_reward_ratio:  float

    # ── Context
    regime:             str

    # ── Verdict
    status:             DecisionStatus
    rejection_reason:   str = ""
    rejection_source:   str = ""  # RejectionSource value or empty
    risk_scaling_applied: float = 1.0  # 1.0 = no scaling, 0.5 = WARNING

    # ── Timing
    created_at_ms:      int = 0

    # ── Trace chain
    candle_trace_ids:   tuple = ()

    def __post_init__(self):
        if self.created_at_ms == 0:
            object.__setattr__(self, "created_at_ms", int(time.time() * 1000))

    @property
    def is_approved(self) -> bool:
        return self.status == DecisionStatus.APPROVED

    def to_dict(self) -> dict:
        return {
            "decision_id": self.decision_id,
            "intent_id": self.intent_id,
            "trigger_id": self.trigger_id,
            "setup_id": self.setup_id,
            "symbol": self.symbol,
            "direction": self.direction.value,
            "strategy_name": self.strategy_name,
            "strategy_class": self.strategy_class.value,
            "entry_price": self.entry_price,
            "stop_loss": self.stop_loss,
            "take_profit": self.take_profit,
            "final_size_usdt": self.final_size_usdt,
            "final_quantity": self.final_quantity,
            "risk_usdt": self.risk_usdt,
            "risk_reward_ratio": self.risk_reward_ratio,
            "regime": self.regime,
            "status": self.status.value,
            "rejection_reason": self.rejection_reason,
            "rejection_source": self.rejection_source,
            "risk_scaling_applied": self.risk_scaling_applied,
            "created_at_ms": self.created_at_ms,
            "candle_trace_ids": list(self.candle_trace_ids),
        }


# ══════════════════════════════════════════════════════════════
# 5. EXECUTION REQUEST
# ══════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class ExecutionRequest:
    """
    Validated payload for the execution layer. Created only from
    an APPROVED ExecutionDecision by ExecutionEngine.
    """
    request_id:         str
    decision_id:        str
    trigger_id:         str
    setup_id:           str

    symbol:             str
    side:               Side
    entry_price:        float
    stop_loss:          float
    take_profit:        float
    size_usdt:          float
    quantity:           float

    strategy_name:      str
    strategy_class:     StrategyClass
    regime:             str

    created_at_ms:      int = 0
    max_fill_delay_ms:  int = 30_000

    candle_trace_ids:   tuple = ()

    def __post_init__(self):
        if self.created_at_ms == 0:
            object.__setattr__(self, "created_at_ms", int(time.time() * 1000))

    def to_dict(self) -> dict:
        return {
            "request_id": self.request_id,
            "decision_id": self.decision_id,
            "trigger_id": self.trigger_id,
            "setup_id": self.setup_id,
            "symbol": self.symbol,
            "side": self.side.value,
            "entry_price": self.entry_price,
            "stop_loss": self.stop_loss,
            "take_profit": self.take_profit,
            "size_usdt": self.size_usdt,
            "quantity": self.quantity,
            "strategy_name": self.strategy_name,
            "strategy_class": self.strategy_class.value,
            "regime": self.regime,
            "created_at_ms": self.created_at_ms,
            "max_fill_delay_ms": self.max_fill_delay_ms,
            "candle_trace_ids": list(self.candle_trace_ids),
        }


# ══════════════════════════════════════════════════════════════
# 6. ORDER RECORD
# ══════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class OrderRecord:
    """
    Represents an order. Created by OrderManager. Tracks lifecycle
    from PENDING through terminal state.
    """
    order_id:           str
    request_id:         str
    decision_id:        str
    trigger_id:         str

    symbol:             str
    side:               Side
    order_type:         OrderType
    requested_price:    float
    requested_quantity: float

    filled_price:       float = 0.0
    filled_quantity:    float = 0.0
    fee_usdt:           float = 0.0
    slippage_pct:       float = 0.0

    status:             OrderStatus = OrderStatus.PENDING
    failure_reason:     str = ""

    created_at_ms:      int = 0
    filled_at_ms:       int = 0

    def __post_init__(self):
        if self.created_at_ms == 0:
            object.__setattr__(self, "created_at_ms", int(time.time() * 1000))

    def to_dict(self) -> dict:
        return {
            "order_id": self.order_id,
            "request_id": self.request_id,
            "decision_id": self.decision_id,
            "trigger_id": self.trigger_id,
            "symbol": self.symbol,
            "side": self.side.value,
            "order_type": self.order_type.value,
            "requested_price": self.requested_price,
            "requested_quantity": self.requested_quantity,
            "filled_price": self.filled_price,
            "filled_quantity": self.filled_quantity,
            "fee_usdt": self.fee_usdt,
            "slippage_pct": self.slippage_pct,
            "status": self.status.value,
            "failure_reason": self.failure_reason,
            "created_at_ms": self.created_at_ms,
            "filled_at_ms": self.filled_at_ms,
        }


# ══════════════════════════════════════════════════════════════
# 7. FILL RECORD
# ══════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class FillRecord:
    """
    Immutable record of an order fill event. One order may produce
    one or more fills (supporting partial fills).
    """
    fill_id:            str
    order_id:           str
    symbol:             str
    side:               Side
    price:              float
    quantity:           float
    fee_usdt:           float
    fee_rate:           float
    slippage_pct:       float
    is_maker:           bool
    filled_at_ms:       int = 0

    def __post_init__(self):
        if self.filled_at_ms == 0:
            object.__setattr__(self, "filled_at_ms", int(time.time() * 1000))

    @property
    def notional_usdt(self) -> float:
        return self.price * self.quantity

    def to_dict(self) -> dict:
        return {
            "fill_id": self.fill_id,
            "order_id": self.order_id,
            "symbol": self.symbol,
            "side": self.side.value,
            "price": self.price,
            "quantity": self.quantity,
            "fee_usdt": self.fee_usdt,
            "fee_rate": self.fee_rate,
            "slippage_pct": self.slippage_pct,
            "is_maker": self.is_maker,
            "filled_at_ms": self.filled_at_ms,
        }


# ══════════════════════════════════════════════════════════════
# 8. POSITION RECORD (Mutable — managed by PortfolioState)
# ══════════════════════════════════════════════════════════════

@dataclass
class PositionRecord:
    """
    Represents an open or closed position. Mutable because it tracks
    evolving state. ALL mutations owned exclusively by PortfolioState.
    """
    # ── Identity & traceability
    position_id:        str
    order_id:           str
    decision_id:        str
    trigger_id:         str
    setup_id:           str

    # ── Trade parameters
    symbol:             str
    direction:          Direction
    strategy_name:      str
    strategy_class:     StrategyClass

    # ── Prices
    entry_price:        float
    entry_size_usdt:    float   # Immutable after open
    current_size_usdt:  float
    quantity:           float
    stop_loss:          float
    original_stop_loss: float   # For R-multiple calculation
    take_profit:        float

    # ── Live state
    current_price:      float = 0.0
    unrealized_pnl_usdt: float = 0.0
    unrealized_pnl_pct: float = 0.0
    realized_pnl_usdt:  float = 0.0
    fee_total_usdt:     float = 0.0
    bars_held:          int = 0

    # ── Status
    status:             PositionStatus = PositionStatus.OPEN
    close_reason:       str = ""
    close_price:        float = 0.0
    opened_at_ms:       int = 0
    closed_at_ms:       int = 0

    # ── Context
    regime_at_entry:    str = ""
    r_multiple:         float = 0.0

    # ── Trace
    candle_trace_ids:   tuple = ()

    # ── Flags
    auto_partial_applied: bool = False
    breakeven_applied:    bool = False

    def __post_init__(self):
        if self.opened_at_ms == 0:
            self.opened_at_ms = int(time.time() * 1000)

    def update_price(self, price: float) -> None:
        """Update current price and recalculate unrealized P&L."""
        self.current_price = price
        if self.direction == Direction.LONG:
            self.unrealized_pnl_usdt = (price - self.entry_price) * self.quantity
        else:
            self.unrealized_pnl_usdt = (self.entry_price - price) * self.quantity
        if self.entry_size_usdt > 0:
            self.unrealized_pnl_pct = self.unrealized_pnl_usdt / self.entry_size_usdt

    @property
    def risk_per_unit(self) -> float:
        """Risk per unit from entry to original stop."""
        return abs(self.entry_price - self.original_stop_loss)

    @property
    def total_risk_usdt(self) -> float:
        """Total dollar risk at entry."""
        return self.risk_per_unit * self.quantity

    def compute_r_multiple(self, exit_price: float) -> float:
        """Calculate R-multiple for a given exit price."""
        rpu = self.risk_per_unit
        if rpu <= 0:
            return 0.0
        if self.direction == Direction.LONG:
            pnl_per_unit = exit_price - self.entry_price
        else:
            pnl_per_unit = self.entry_price - exit_price
        return pnl_per_unit / rpu

    def to_dict(self) -> dict:
        return {
            "position_id": self.position_id,
            "order_id": self.order_id,
            "decision_id": self.decision_id,
            "trigger_id": self.trigger_id,
            "setup_id": self.setup_id,
            "symbol": self.symbol,
            "direction": self.direction.value,
            "strategy_name": self.strategy_name,
            "strategy_class": self.strategy_class.value,
            "entry_price": self.entry_price,
            "entry_size_usdt": self.entry_size_usdt,
            "current_size_usdt": self.current_size_usdt,
            "quantity": self.quantity,
            "stop_loss": self.stop_loss,
            "original_stop_loss": self.original_stop_loss,
            "take_profit": self.take_profit,
            "current_price": self.current_price,
            "unrealized_pnl_usdt": self.unrealized_pnl_usdt,
            "unrealized_pnl_pct": self.unrealized_pnl_pct,
            "realized_pnl_usdt": self.realized_pnl_usdt,
            "fee_total_usdt": self.fee_total_usdt,
            "bars_held": self.bars_held,
            "status": self.status.value,
            "close_reason": self.close_reason,
            "close_price": self.close_price,
            "opened_at_ms": self.opened_at_ms,
            "closed_at_ms": self.closed_at_ms,
            "regime_at_entry": self.regime_at_entry,
            "r_multiple": self.r_multiple,
            "candle_trace_ids": list(self.candle_trace_ids),
            "auto_partial_applied": self.auto_partial_applied,
            "breakeven_applied": self.breakeven_applied,
        }


# ══════════════════════════════════════════════════════════════
# 9. EXECUTION RESULT
# ══════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class ExecutionResult:
    """Result returned by ExecutionEngine.execute()."""
    success:            bool
    position_id:        str = ""
    order_id:           str = ""
    failure_reason:     str = ""


# ══════════════════════════════════════════════════════════════
# 10. PORTFOLIO SNAPSHOT (Read-only state transfer)
# ══════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class CapitalSnapshot:
    """Frozen snapshot of capital state."""
    total_capital:      float
    reserved_capital:   float
    available_capital:  float
    equity:             float
    peak_equity:        float
    drawdown_pct:       float
    realized_pnl_today: float
    total_realized_pnl: float
    total_fees:         float
    trade_count_today:  int
    consecutive_losses: int


@dataclass(frozen=True)
class ExposureSnapshot:
    """Frozen snapshot of exposure metrics."""
    per_symbol:         dict   # {symbol: fraction_of_capital}
    long_exposure:      float
    short_exposure:     float
    net_exposure:       float
    portfolio_heat:     float  # sum(risk_usdt) / total_capital


@dataclass(frozen=True)
class PortfolioSnapshot:
    """Complete read-only portfolio state for ProcessingEngine."""
    capital:            CapitalSnapshot
    exposure:           ExposureSnapshot
    open_positions:     tuple  # tuple of PositionRecord copies
    open_position_count: int


# ══════════════════════════════════════════════════════════════
# 11. TRADE RECORD (Immutable closed-trade record for ledger)
# ══════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class TradeRecord:
    """Immutable record of a completed trade for the TradeLedger."""
    position_id:        str
    order_id:           str
    decision_id:        str
    trigger_id:         str
    setup_id:           str
    symbol:             str
    direction:          str  # "long" or "short"
    strategy_name:      str
    strategy_class:     str
    entry_price:        float
    exit_price:         float
    entry_size_usdt:    float
    quantity:           float
    realized_pnl_usdt: float
    fee_total_usdt:     float
    r_multiple:         float
    close_reason:       str
    bars_held:          int
    regime_at_entry:    str
    opened_at_ms:       int
    closed_at_ms:       int
    duration_ms:        int

    def to_dict(self) -> dict:
        return {
            "position_id": self.position_id,
            "order_id": self.order_id,
            "decision_id": self.decision_id,
            "trigger_id": self.trigger_id,
            "setup_id": self.setup_id,
            "symbol": self.symbol,
            "direction": self.direction,
            "strategy_name": self.strategy_name,
            "strategy_class": self.strategy_class,
            "entry_price": self.entry_price,
            "exit_price": self.exit_price,
            "entry_size_usdt": self.entry_size_usdt,
            "quantity": self.quantity,
            "realized_pnl_usdt": self.realized_pnl_usdt,
            "fee_total_usdt": self.fee_total_usdt,
            "r_multiple": self.r_multiple,
            "close_reason": self.close_reason,
            "bars_held": self.bars_held,
            "regime_at_entry": self.regime_at_entry,
            "opened_at_ms": self.opened_at_ms,
            "closed_at_ms": self.closed_at_ms,
            "duration_ms": self.duration_ms,
        }

    @classmethod
    def from_dict(cls, d: dict) -> TradeRecord:
        return cls(**{k: d[k] for k in cls.__dataclass_fields__})


# ══════════════════════════════════════════════════════════════
# 12. VALIDATION
# ══════════════════════════════════════════════════════════════

class ContractViolation(ValueError):
    """Raised when a contract is violated at a boundary."""
    pass


class InvariantViolation(RuntimeError):
    """Raised when a capital or state invariant is violated."""
    pass


def validate_execution_intent(intent: ExecutionIntent) -> list[str]:
    """Validate an ExecutionIntent. Returns list of violations."""
    v = []
    if not intent.intent_id:
        v.append("intent_id is empty")
    if not intent.trigger_id:
        v.append("trigger_id is empty")
    if not intent.setup_id:
        v.append("setup_id is empty")
    if not intent.symbol:
        v.append("symbol is empty")
    if not isinstance(intent.direction, Direction):
        v.append(f"direction invalid: {intent.direction}")
    if intent.entry_price <= 0:
        v.append("entry_price must be positive")
    if intent.stop_loss <= 0:
        v.append("stop_loss must be positive")
    if intent.take_profit <= 0:
        v.append("take_profit must be positive")
    if intent.atr_value <= 0:
        v.append("atr_value must be positive")
    # Direction consistency
    if intent.direction == Direction.LONG:
        if intent.stop_loss >= intent.entry_price:
            v.append("LONG stop_loss must be below entry_price")
        if intent.take_profit <= intent.entry_price:
            v.append("LONG take_profit must be above entry_price")
    elif intent.direction == Direction.SHORT:
        if intent.stop_loss <= intent.entry_price:
            v.append("SHORT stop_loss must be above entry_price")
        if intent.take_profit >= intent.entry_price:
            v.append("SHORT take_profit must be below entry_price")
    if intent.size_usdt <= 0:
        v.append("size_usdt must be positive")
    if intent.quantity <= 0:
        v.append("quantity must be positive")
    if intent.risk_usdt <= 0:
        v.append("risk_usdt must be positive")
    if intent.risk_reward_ratio < 1.0:
        v.append(f"risk_reward_ratio must be >= 1.0, got {intent.risk_reward_ratio}")
    return v


def validate_execution_intent_strict(intent: ExecutionIntent) -> None:
    violations = validate_execution_intent(intent)
    if violations:
        raise ContractViolation(
            f"ExecutionIntent validation failed ({len(violations)} issues): "
            + "; ".join(violations)
        )


def validate_execution_decision(decision: ExecutionDecision) -> list[str]:
    """Validate an ExecutionDecision. Returns list of violations."""
    v = []
    if not decision.decision_id:
        v.append("decision_id is empty")
    if not decision.intent_id:
        v.append("intent_id is empty")
    if not isinstance(decision.status, DecisionStatus):
        v.append(f"status invalid: {decision.status}")
    if decision.status == DecisionStatus.APPROVED:
        if decision.final_size_usdt <= 0:
            v.append("APPROVED decision must have final_size_usdt > 0")
        if decision.final_quantity <= 0:
            v.append("APPROVED decision must have final_quantity > 0")
        if decision.entry_price <= 0:
            v.append("entry_price must be positive")
    if decision.status == DecisionStatus.REJECTED:
        if not decision.rejection_reason:
            v.append("REJECTED decision must have rejection_reason")
        if not decision.rejection_source:
            v.append("REJECTED decision must have rejection_source")
    return v


def validate_execution_decision_strict(decision: ExecutionDecision) -> None:
    violations = validate_execution_decision(decision)
    if violations:
        raise ContractViolation(
            f"ExecutionDecision validation failed ({len(violations)} issues): "
            + "; ".join(violations)
        )


def validate_execution_request(request: ExecutionRequest) -> list[str]:
    """Validate an ExecutionRequest. Returns list of violations."""
    v = []
    if not request.request_id:
        v.append("request_id is empty")
    if not request.decision_id:
        v.append("decision_id is empty")
    if not request.symbol:
        v.append("symbol is empty")
    if not isinstance(request.side, Side):
        v.append(f"side invalid: {request.side}")
    if request.entry_price <= 0:
        v.append("entry_price must be positive")
    if request.stop_loss <= 0:
        v.append("stop_loss must be positive")
    if request.take_profit <= 0:
        v.append("take_profit must be positive")
    if request.size_usdt <= 0:
        v.append("size_usdt must be positive")
    if request.quantity <= 0:
        v.append("quantity must be positive")
    if request.max_fill_delay_ms <= 0:
        v.append("max_fill_delay_ms must be positive")
    return v


def validate_execution_request_strict(request: ExecutionRequest) -> None:
    violations = validate_execution_request(request)
    if violations:
        raise ContractViolation(
            f"ExecutionRequest validation failed ({len(violations)} issues): "
            + "; ".join(violations)
        )


def validate_order_record(order: OrderRecord) -> list[str]:
    """Validate an OrderRecord. Returns list of violations."""
    v = []
    if not order.order_id:
        v.append("order_id is empty")
    if not order.request_id:
        v.append("request_id is empty")
    if not isinstance(order.side, Side):
        v.append(f"side invalid: {order.side}")
    if order.requested_price <= 0:
        v.append("requested_price must be positive")
    if order.requested_quantity <= 0:
        v.append("requested_quantity must be positive")
    if order.status == OrderStatus.FILLED:
        if order.filled_price <= 0:
            v.append("FILLED order must have filled_price > 0")
        if order.filled_quantity <= 0:
            v.append("FILLED order must have filled_quantity > 0")
    if order.status == OrderStatus.FAILED and not order.failure_reason:
        v.append("FAILED order must have failure_reason")
    return v


def validate_order_record_strict(order: OrderRecord) -> None:
    violations = validate_order_record(order)
    if violations:
        raise ContractViolation(
            f"OrderRecord validation failed ({len(violations)} issues): "
            + "; ".join(violations)
        )


def validate_fill_record(fill: FillRecord) -> list[str]:
    """Validate a FillRecord. Returns list of violations."""
    v = []
    if not fill.fill_id:
        v.append("fill_id is empty")
    if not fill.order_id:
        v.append("order_id is empty")
    if fill.price <= 0:
        v.append("price must be positive")
    if fill.quantity <= 0:
        v.append("quantity must be positive")
    if fill.fee_usdt < 0:
        v.append("fee_usdt must be non-negative")
    if fill.fee_rate < 0:
        v.append("fee_rate must be non-negative")
    if fill.slippage_pct < 0:
        v.append("slippage_pct must be non-negative")
    return v


def validate_fill_record_strict(fill: FillRecord) -> None:
    violations = validate_fill_record(fill)
    if violations:
        raise ContractViolation(
            f"FillRecord validation failed ({len(violations)} issues): "
            + "; ".join(violations)
        )


def validate_position_record(pos: PositionRecord) -> list[str]:
    """Validate a PositionRecord. Returns list of violations."""
    v = []
    if not pos.position_id:
        v.append("position_id is empty")
    if pos.entry_price <= 0:
        v.append("entry_price must be positive")
    if pos.entry_size_usdt <= 0:
        v.append("entry_size_usdt must be positive")
    if pos.quantity <= 0:
        v.append("quantity must be positive")
    if pos.stop_loss <= 0:
        v.append("stop_loss must be positive")
    if pos.take_profit <= 0:
        v.append("take_profit must be positive")
    if pos.status == PositionStatus.CLOSED:
        if pos.close_price <= 0:
            v.append("CLOSED position must have close_price > 0")
        if not pos.close_reason:
            v.append("CLOSED position must have close_reason")
    return v


def validate_position_record_strict(pos: PositionRecord) -> None:
    violations = validate_position_record(pos)
    if violations:
        raise ContractViolation(
            f"PositionRecord validation failed ({len(violations)} issues): "
            + "; ".join(violations)
        )
