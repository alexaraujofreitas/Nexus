"""
Phase 8: Order Lifecycle State Machine

Defines the 10-state order lifecycle for live trading.
Enforces valid transitions at the type level — invalid transitions
raise OrderTransitionError immediately. No silent state corruption.

State Machine:
    INTENT_CREATED ──► SUBMISSION_ATTEMPTED ──► ACKNOWLEDGED ──► LIVE
         │                     │                                  │
         │              (crash-after-submit)              ┌───────┴───────┐
         │                     │                          ▼               ▼
         │              RECOVERY_PENDING          PARTIALLY_FILLED    CANCELLED
         │                                              │
         │                                              ▼
         │                                           FILLED
         │
         ├──► REJECTED  (pre-submission validation fail)
         └──► FAILED    (any unrecoverable error)

    SUBMISSION_ATTEMPTED ──► FAILED      (timeout / network error)
    SUBMISSION_ATTEMPTED ──► REJECTED    (exchange rejects)
    SUBMISSION_ATTEMPTED ──► RECOVERY_PENDING  (crash-after-submit)
    ACKNOWLEDGED ──► CANCELLED
    ACKNOWLEDGED ──► FAILED
    LIVE ──► PARTIALLY_FILLED ──► FILLED
    LIVE ──► PARTIALLY_FILLED ──► CANCELLED  (partial fill + cancel rest)
    LIVE ──► FILLED
    LIVE ──► CANCELLED
    LIVE ──► FAILED
    RECOVERY_PENDING ──► ACKNOWLEDGED  (found on exchange)
    RECOVERY_PENDING ──► FILLED        (filled during downtime)
    RECOVERY_PENDING ──► CANCELLED     (cancelled during downtime)
    RECOVERY_PENDING ──► FAILED        (not found, unrecoverable)

Terminal states: FILLED, CANCELLED, REJECTED, FAILED
All transitions are auditable — every LiveOrder records transition history.

No Qt imports. No execution imports. Pure Python.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, FrozenSet, List, Optional, Set, Tuple


# ══════════════════════════════════════════════════════════════
# 1. ORDER LIFECYCLE STATE
# ══════════════════════════════════════════════════════════════

class OrderLifecycleState(str, Enum):
    """
    10-state order lifecycle.

    Non-terminal states: INTENT_CREATED, SUBMISSION_ATTEMPTED,
    ACKNOWLEDGED, LIVE, PARTIALLY_FILLED, RECOVERY_PENDING.
    Terminal states: FILLED, CANCELLED, REJECTED, FAILED.
    """
    INTENT_CREATED = "intent_created"
    SUBMISSION_ATTEMPTED = "submission_attempted"
    ACKNOWLEDGED = "acknowledged"
    LIVE = "live"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    FAILED = "failed"
    RECOVERY_PENDING = "recovery_pending"


# ══════════════════════════════════════════════════════════════
# 2. TRANSITION TABLE
# ══════════════════════════════════════════════════════════════

VALID_TRANSITIONS: Dict[OrderLifecycleState, FrozenSet[OrderLifecycleState]] = {
    OrderLifecycleState.INTENT_CREATED: frozenset({
        OrderLifecycleState.SUBMISSION_ATTEMPTED,
        OrderLifecycleState.REJECTED,
        OrderLifecycleState.FAILED,
    }),
    OrderLifecycleState.SUBMISSION_ATTEMPTED: frozenset({
        OrderLifecycleState.ACKNOWLEDGED,
        OrderLifecycleState.REJECTED,
        OrderLifecycleState.FAILED,
        OrderLifecycleState.RECOVERY_PENDING,
    }),
    OrderLifecycleState.ACKNOWLEDGED: frozenset({
        OrderLifecycleState.LIVE,
        OrderLifecycleState.CANCELLED,
        OrderLifecycleState.FAILED,
    }),
    OrderLifecycleState.LIVE: frozenset({
        OrderLifecycleState.PARTIALLY_FILLED,
        OrderLifecycleState.FILLED,
        OrderLifecycleState.CANCELLED,
        OrderLifecycleState.FAILED,
    }),
    OrderLifecycleState.PARTIALLY_FILLED: frozenset({
        OrderLifecycleState.FILLED,
        OrderLifecycleState.CANCELLED,
        OrderLifecycleState.FAILED,
    }),
    OrderLifecycleState.RECOVERY_PENDING: frozenset({
        OrderLifecycleState.ACKNOWLEDGED,
        OrderLifecycleState.FILLED,
        OrderLifecycleState.CANCELLED,
        OrderLifecycleState.FAILED,
    }),
    # Terminal states — no outgoing transitions
    OrderLifecycleState.FILLED: frozenset(),
    OrderLifecycleState.CANCELLED: frozenset(),
    OrderLifecycleState.REJECTED: frozenset(),
    OrderLifecycleState.FAILED: frozenset(),
}

TERMINAL_STATES: FrozenSet[OrderLifecycleState] = frozenset({
    OrderLifecycleState.FILLED,
    OrderLifecycleState.CANCELLED,
    OrderLifecycleState.REJECTED,
    OrderLifecycleState.FAILED,
})

NON_TERMINAL_STATES: FrozenSet[OrderLifecycleState] = frozenset(
    s for s in OrderLifecycleState if s not in TERMINAL_STATES
)


# ══════════════════════════════════════════════════════════════
# 3. TRANSITION ERROR
# ══════════════════════════════════════════════════════════════

class OrderTransitionError(Exception):
    """Raised when an invalid state transition is attempted."""

    def __init__(
        self,
        order_id: str,
        from_state: OrderLifecycleState,
        to_state: OrderLifecycleState,
    ):
        self.order_id = order_id
        self.from_state = from_state
        self.to_state = to_state
        super().__init__(
            f"Invalid transition for order {order_id}: "
            f"{from_state.value} -> {to_state.value}. "
            f"Valid targets: {sorted(s.value for s in VALID_TRANSITIONS.get(from_state, frozenset()))}"
        )


# ══════════════════════════════════════════════════════════════
# 4. TRANSITION RECORD
# ══════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class TransitionRecord:
    """
    Immutable record of a state transition. Stored in LiveOrder.history
    for full audit trail.
    """
    from_state: OrderLifecycleState
    to_state: OrderLifecycleState
    timestamp_ms: int
    reason: str = ""
    exchange_order_id: str = ""
    metadata: str = ""   # JSON-serializable string for extra context

    def to_dict(self) -> dict:
        return {
            "from_state": self.from_state.value,
            "to_state": self.to_state.value,
            "timestamp_ms": self.timestamp_ms,
            "reason": self.reason,
            "exchange_order_id": self.exchange_order_id,
            "metadata": self.metadata,
        }


# ══════════════════════════════════════════════════════════════
# 5. LIVE ORDER
# ══════════════════════════════════════════════════════════════

@dataclass
class LiveOrder:
    """
    Mutable order tracker for live trading. Owns the state machine.

    All state transitions go through transition() which validates
    against VALID_TRANSITIONS and records audit history.

    Fields are deliberately mutable — LiveExecutor updates them
    as exchange events arrive. Only transition() modifies `state`.
    """
    # ── Identity
    client_order_id: str         # Deterministic, idempotent key
    request_id: str              # Links back to ExecutionRequest
    decision_id: str
    trigger_id: str

    # ── Trade parameters
    symbol: str
    side: str                    # "buy" or "sell"
    order_type: str              # "market" or "limit"
    requested_price: float
    requested_quantity: float
    size_usdt: float

    # ── Strategy context
    strategy_name: str
    regime: str

    # ── Stop / Target (for position management)
    stop_loss: float
    take_profit: float

    # ── State
    state: OrderLifecycleState = OrderLifecycleState.INTENT_CREATED

    # ── Exchange data (populated on acknowledgement)
    exchange_order_id: str = ""

    # ── Fill tracking
    filled_quantity: float = 0.0
    filled_price_avg: float = 0.0
    fee_usdt: float = 0.0
    fill_count: int = 0

    # ── Timing
    created_at_ms: int = 0
    submitted_at_ms: int = 0
    acknowledged_at_ms: int = 0
    last_fill_at_ms: int = 0
    completed_at_ms: int = 0

    # ── Failure tracking
    failure_reason: str = ""
    retry_count: int = 0

    # ── Audit trail
    history: List[TransitionRecord] = field(default_factory=list)

    def __post_init__(self):
        if self.created_at_ms == 0:
            self.created_at_ms = int(time.time() * 1000)

    # ── State machine ─────────────────────────────────────────

    def transition(
        self,
        to_state: OrderLifecycleState,
        reason: str = "",
        timestamp_ms: int = 0,
        exchange_order_id: str = "",
        metadata: str = "",
    ) -> TransitionRecord:
        """
        Attempt a state transition. Validates against VALID_TRANSITIONS.

        Args:
            to_state: Target state.
            reason: Human-readable reason for the transition.
            timestamp_ms: Timestamp of the transition (0 = now).
            exchange_order_id: Exchange-assigned order ID (if available).
            metadata: Optional JSON metadata string.

        Returns:
            TransitionRecord for the successful transition.

        Raises:
            OrderTransitionError: If the transition is invalid.
        """
        valid_targets = VALID_TRANSITIONS.get(self.state, frozenset())
        if to_state not in valid_targets:
            raise OrderTransitionError(self.client_order_id, self.state, to_state)

        ts = timestamp_ms or int(time.time() * 1000)
        record = TransitionRecord(
            from_state=self.state,
            to_state=to_state,
            timestamp_ms=ts,
            reason=reason,
            exchange_order_id=exchange_order_id or self.exchange_order_id,
            metadata=metadata,
        )

        old_state = self.state
        self.state = to_state
        self.history.append(record)

        # Update exchange_order_id if provided
        if exchange_order_id:
            self.exchange_order_id = exchange_order_id

        # Update timing based on new state
        if to_state == OrderLifecycleState.SUBMISSION_ATTEMPTED:
            self.submitted_at_ms = ts
        elif to_state == OrderLifecycleState.ACKNOWLEDGED:
            self.acknowledged_at_ms = ts
        elif to_state in TERMINAL_STATES:
            self.completed_at_ms = ts

        # Store failure reason for terminal failure states
        if to_state in (
            OrderLifecycleState.FAILED,
            OrderLifecycleState.REJECTED,
        ) and reason:
            self.failure_reason = reason

        return record

    def record_fill(
        self,
        fill_price: float,
        fill_quantity: float,
        fee_usdt: float,
        timestamp_ms: int = 0,
    ) -> None:
        """
        Record a fill event. Updates average price via VWAP.

        Does NOT transition state — caller must transition
        to PARTIALLY_FILLED or FILLED after recording fill.

        Args:
            fill_price: Execution price for this fill.
            fill_quantity: Quantity filled.
            fee_usdt: Fee charged for this fill.
            timestamp_ms: Fill timestamp (0 = now).
        """
        ts = timestamp_ms or int(time.time() * 1000)

        # VWAP for average fill price
        old_notional = self.filled_price_avg * self.filled_quantity
        new_notional = fill_price * fill_quantity
        total_qty = self.filled_quantity + fill_quantity

        if total_qty > 0:
            self.filled_price_avg = (old_notional + new_notional) / total_qty

        self.filled_quantity = total_qty
        self.fee_usdt += fee_usdt
        self.fill_count += 1
        self.last_fill_at_ms = ts

    @property
    def is_terminal(self) -> bool:
        """Whether this order is in a terminal state."""
        return self.state in TERMINAL_STATES

    @property
    def is_fully_filled(self) -> bool:
        """Whether fill quantity equals or exceeds requested quantity."""
        return self.filled_quantity >= self.requested_quantity * 0.9999

    @property
    def fill_pct(self) -> float:
        """Fill percentage (0.0 to 1.0)."""
        if self.requested_quantity <= 0:
            return 0.0
        return min(1.0, self.filled_quantity / self.requested_quantity)

    @property
    def slippage_pct(self) -> float:
        """Slippage as percentage of requested price."""
        if self.requested_price <= 0 or self.filled_price_avg <= 0:
            return 0.0
        return abs(self.filled_price_avg - self.requested_price) / self.requested_price

    @property
    def elapsed_since_submit_ms(self) -> int:
        """Milliseconds since submission (0 if not submitted)."""
        if self.submitted_at_ms == 0:
            return 0
        now = int(time.time() * 1000)
        return now - self.submitted_at_ms

    def to_dict(self) -> dict:
        """Serialize to JSON-safe dict for persistence and audit."""
        return {
            "client_order_id": self.client_order_id,
            "request_id": self.request_id,
            "decision_id": self.decision_id,
            "trigger_id": self.trigger_id,
            "symbol": self.symbol,
            "side": self.side,
            "order_type": self.order_type,
            "requested_price": self.requested_price,
            "requested_quantity": self.requested_quantity,
            "size_usdt": self.size_usdt,
            "strategy_name": self.strategy_name,
            "regime": self.regime,
            "stop_loss": self.stop_loss,
            "take_profit": self.take_profit,
            "state": self.state.value,
            "exchange_order_id": self.exchange_order_id,
            "filled_quantity": self.filled_quantity,
            "filled_price_avg": self.filled_price_avg,
            "fee_usdt": self.fee_usdt,
            "fill_count": self.fill_count,
            "created_at_ms": self.created_at_ms,
            "submitted_at_ms": self.submitted_at_ms,
            "acknowledged_at_ms": self.acknowledged_at_ms,
            "last_fill_at_ms": self.last_fill_at_ms,
            "completed_at_ms": self.completed_at_ms,
            "failure_reason": self.failure_reason,
            "retry_count": self.retry_count,
            "history": [h.to_dict() for h in self.history],
        }

    @classmethod
    def from_dict(cls, d: dict) -> LiveOrder:
        """Reconstruct LiveOrder from persisted dict."""
        order = cls(
            client_order_id=d["client_order_id"],
            request_id=d["request_id"],
            decision_id=d["decision_id"],
            trigger_id=d["trigger_id"],
            symbol=d["symbol"],
            side=d["side"],
            order_type=d["order_type"],
            requested_price=d["requested_price"],
            requested_quantity=d["requested_quantity"],
            size_usdt=d["size_usdt"],
            strategy_name=d["strategy_name"],
            regime=d["regime"],
            stop_loss=d["stop_loss"],
            take_profit=d["take_profit"],
            state=OrderLifecycleState(d["state"]),
            exchange_order_id=d.get("exchange_order_id", ""),
            filled_quantity=d.get("filled_quantity", 0.0),
            filled_price_avg=d.get("filled_price_avg", 0.0),
            fee_usdt=d.get("fee_usdt", 0.0),
            fill_count=d.get("fill_count", 0),
            created_at_ms=d.get("created_at_ms", 0),
            submitted_at_ms=d.get("submitted_at_ms", 0),
            acknowledged_at_ms=d.get("acknowledged_at_ms", 0),
            last_fill_at_ms=d.get("last_fill_at_ms", 0),
            completed_at_ms=d.get("completed_at_ms", 0),
            failure_reason=d.get("failure_reason", ""),
            retry_count=d.get("retry_count", 0),
        )
        # Reconstruct history
        for h in d.get("history", []):
            order.history.append(TransitionRecord(
                from_state=OrderLifecycleState(h["from_state"]),
                to_state=OrderLifecycleState(h["to_state"]),
                timestamp_ms=h["timestamp_ms"],
                reason=h.get("reason", ""),
                exchange_order_id=h.get("exchange_order_id", ""),
                metadata=h.get("metadata", ""),
            ))
        return order


# ══════════════════════════════════════════════════════════════
# 6. FACTORY HELPERS
# ══════════════════════════════════════════════════════════════

def make_client_order_id(
    request_id: str,
    symbol: str,
    side: str,
    timestamp_ms: int,
) -> str:
    """
    Deterministic client_order_id for idempotent submission.

    Same inputs → same ID → exchange deduplicates if resubmitted.
    Format: "NT-" + SHA256[:16] of (request_id|symbol|side|timestamp_ms).
    """
    raw = f"{request_id}|{symbol}|{side}|{timestamp_ms}"
    h = hashlib.sha256(raw.encode()).hexdigest()[:16]
    return f"NT-{h}"
