# ============================================================
# NEXUS TRADER — Live Trading Module (Phase 8)
#
# Production-ready live execution with:
# - LiveExecutor: exchange order submission, fill tracking
# - OrderReconciliationEngine: internal vs exchange state sync
# - RestartRecoveryManager: deterministic recovery after restart
# - IdempotencyStore: duplicate prevention, client_order_id
# - ExchangeAdapter: CCXT wrapper with retry, timeout, error class
# - OrderLifecycle: state machine with validated transitions
#
# Design invariants:
# - Fail-closed: never assume success without confirmation
# - Reconciliation-first: no trading before reconciliation completes
# - Idempotent: same request → same client_order_id → no duplicates
# - Auditable: every state transition logged with full trace
# - Contract-compatible: produces (OrderRecord, FillRecord) — same
#   contract as paper path, plugs into ExecutionEngine seamlessly
# - ZERO PySide6 imports
# ============================================================

from .order_lifecycle import (
    OrderLifecycleState,
    LiveOrder,
    OrderTransitionError,
    VALID_TRANSITIONS,
    TERMINAL_STATES,
)
from .idempotency_store import IdempotencyStore
from .exchange_adapter import (
    ExchangeAdapter,
    ExchangeError,
    ExchangeErrorClass,
    ExchangeResponse,
)
from .live_executor import LiveExecutor
from .reconciliation_engine import (
    OrderReconciliationEngine,
    ReconciliationResult,
    ReconciliationAction,
    MismatchType,
)
from .recovery_manager import RestartRecoveryManager, RecoveryReport

__all__ = [
    # Lifecycle
    "OrderLifecycleState",
    "LiveOrder",
    "OrderTransitionError",
    "VALID_TRANSITIONS",
    "TERMINAL_STATES",
    # Idempotency
    "IdempotencyStore",
    # Exchange
    "ExchangeAdapter",
    "ExchangeError",
    "ExchangeErrorClass",
    "ExchangeResponse",
    # Executor
    "LiveExecutor",
    # Reconciliation
    "OrderReconciliationEngine",
    "ReconciliationResult",
    "ReconciliationAction",
    "MismatchType",
    # Recovery
    "RestartRecoveryManager",
    "RecoveryReport",
]
