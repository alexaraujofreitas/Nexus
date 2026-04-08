"""
Phase 8: Restart Recovery Manager

Deterministic recovery sequence after system restart:

    1. LOAD    — Read internal state from persistence (idempotency store,
                 position snapshots, open order records)
    2. FETCH   — Query exchange for current state (open orders, positions,
                 balance)
    3. RECONCILE — Compare internal vs exchange via ReconciliationEngine
    4. REBUILD — Apply resolution actions, sync state
    5. RESUME  — Set system ready flag only if reconciliation is clean

RECONCILIATION-FIRST RULE:
Trading is BLOCKED until recovery completes successfully.
Any unresolved mismatch → system stays in RECOVERY mode.

FAIL-CLOSED:
If any step fails, system does NOT resume trading.
Manual intervention required.

No Qt imports. No PySide6. Pure Python.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

from .idempotency_store import IdempotencyStore
from .reconciliation_engine import (
    OrderReconciliationEngine,
    ReconciliationResult,
    ReconciliationAction,
    MismatchType,
)

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════
# 1. RECOVERY PHASE ENUM
# ══════════════════════════════════════════════════════════════

class RecoveryPhase(str, Enum):
    """Phases of the recovery sequence."""
    NOT_STARTED = "not_started"
    LOADING = "loading"
    FETCHING = "fetching"
    RECONCILING = "reconciling"
    REBUILDING = "rebuilding"
    COMPLETED = "completed"
    FAILED = "failed"


# ══════════════════════════════════════════════════════════════
# 2. RECOVERY REPORT
# ══════════════════════════════════════════════════════════════

@dataclass
class RecoveryReport:
    """Complete report of a recovery run."""
    phase: RecoveryPhase = RecoveryPhase.NOT_STARTED
    started_at_ms: int = 0
    completed_at_ms: int = 0
    duration_ms: int = 0

    # Load phase
    idempotency_entries_loaded: int = 0
    pending_submissions_found: int = 0
    internal_positions_loaded: int = 0

    # Fetch phase
    exchange_open_orders: int = 0
    exchange_positions: int = 0
    exchange_balance_usdt: float = 0.0

    # Reconcile phase
    reconciliation: Optional[ReconciliationResult] = None

    # Rebuild phase
    orders_recovered: int = 0
    orders_failed: int = 0
    positions_synced: int = 0

    # Errors
    errors: List[str] = field(default_factory=list)

    @property
    def success(self) -> bool:
        return self.phase == RecoveryPhase.COMPLETED and len(self.errors) == 0

    @property
    def is_clean(self) -> bool:
        """True if recovery completed with no mismatches and no errors."""
        return (
            self.success
            and self.reconciliation is not None
            and self.reconciliation.is_clean
        )

    @property
    def trading_allowed(self) -> bool:
        """Whether trading can resume after this recovery."""
        return self.is_clean

    def to_dict(self) -> dict:
        return {
            "phase": self.phase.value,
            "started_at_ms": self.started_at_ms,
            "completed_at_ms": self.completed_at_ms,
            "duration_ms": self.duration_ms,
            "success": self.success,
            "is_clean": self.is_clean,
            "trading_allowed": self.trading_allowed,
            "idempotency_entries_loaded": self.idempotency_entries_loaded,
            "pending_submissions_found": self.pending_submissions_found,
            "internal_positions_loaded": self.internal_positions_loaded,
            "exchange_open_orders": self.exchange_open_orders,
            "exchange_positions": self.exchange_positions,
            "exchange_balance_usdt": self.exchange_balance_usdt,
            "orders_recovered": self.orders_recovered,
            "orders_failed": self.orders_failed,
            "positions_synced": self.positions_synced,
            "reconciliation": (
                self.reconciliation.to_dict() if self.reconciliation else None
            ),
            "errors": self.errors,
        }


# ══════════════════════════════════════════════════════════════
# 3. RESTART RECOVERY MANAGER
# ══════════════════════════════════════════════════════════════

class RestartRecoveryManager:
    """
    Manages the 5-phase restart recovery sequence.

    Usage:
        manager = RestartRecoveryManager(
            exchange_adapter=adapter,
            idempotency_store=store,
            reconciliation_engine=reconciliation,
        )
        report = manager.recover(
            internal_positions=portfolio_state.get_open_positions(),
        )
        if report.trading_allowed:
            # Safe to resume trading
        else:
            # Manual intervention required
    """

    def __init__(
        self,
        exchange_adapter,     # ExchangeAdapter
        idempotency_store: IdempotencyStore,
        reconciliation_engine: OrderReconciliationEngine,
        now_ms_fn=None,
    ):
        self._adapter = exchange_adapter
        self._idempotency = idempotency_store
        self._reconciliation = reconciliation_engine
        self._now_ms_fn = now_ms_fn or (lambda: int(time.time() * 1000))
        self._last_report: Optional[RecoveryReport] = None
        self._recovery_complete = False

        logger.info("RestartRecoveryManager initialized")

    @property
    def recovery_complete(self) -> bool:
        """Whether recovery has completed successfully."""
        return self._recovery_complete

    @property
    def trading_allowed(self) -> bool:
        """Whether trading is allowed (recovery complete + clean)."""
        return (
            self._recovery_complete
            and self._last_report is not None
            and self._last_report.trading_allowed
        )

    # ── Main Recovery Sequence ────────────────────────────────

    def recover(
        self,
        internal_orders: Optional[Dict[str, Any]] = None,
        internal_positions: Optional[Dict[str, Any]] = None,
        auto_resolve: bool = False,
    ) -> RecoveryReport:
        """
        Run the 5-phase recovery sequence.

        Args:
            internal_orders: Internal orders (from LiveExecutor state).
            internal_positions: Internal positions (from PortfolioState).
            auto_resolve: If True, auto-resolve clear-cut mismatches.

        Returns:
            RecoveryReport with full diagnostics.
        """
        report = RecoveryReport(started_at_ms=self._now_ms_fn())

        try:
            # Phase 1: LOAD
            report.phase = RecoveryPhase.LOADING
            self._phase_load(report)

            # Phase 2: FETCH
            report.phase = RecoveryPhase.FETCHING
            self._phase_fetch(report)

            # Phase 3: RECONCILE
            report.phase = RecoveryPhase.RECONCILING
            self._phase_reconcile(
                report,
                internal_orders or {},
                internal_positions or {},
                auto_resolve,
            )

            # Phase 4: REBUILD
            report.phase = RecoveryPhase.REBUILDING
            self._phase_rebuild(report)

            # Phase 5: Complete
            report.phase = RecoveryPhase.COMPLETED
            self._recovery_complete = True

        except Exception as e:
            logger.error(f"Recovery failed in phase {report.phase.value}: {e}", exc_info=True)
            report.phase = RecoveryPhase.FAILED
            report.errors.append(f"Recovery failed: {e}")
            self._recovery_complete = False

        report.completed_at_ms = self._now_ms_fn()
        report.duration_ms = report.completed_at_ms - report.started_at_ms
        self._last_report = report

        logger.info(
            f"Recovery {report.phase.value} in {report.duration_ms}ms: "
            f"clean={report.is_clean}, trading_allowed={report.trading_allowed}, "
            f"errors={len(report.errors)}"
        )

        return report

    # ── Phase Implementations ─────────────────────────────────

    def _phase_load(self, report: RecoveryReport) -> None:
        """Phase 1: Load internal state from persistence."""
        logger.info("Recovery Phase 1: LOAD — loading internal state")

        # Load idempotency store
        count = self._idempotency.load()
        report.idempotency_entries_loaded = count

        # Check for pending submissions (crash-after-submit)
        pending = self._idempotency.get_pending_submissions()
        report.pending_submissions_found = len(pending)

        if pending:
            logger.warning(
                f"Recovery: found {len(pending)} pending submissions "
                f"(crash-after-submit scenario). Will reconcile."
            )
            for p in pending:
                logger.warning(
                    f"  Pending: {p.client_order_id} {p.symbol} {p.side} "
                    f"state={p.state} exchange_id={p.exchange_order_id}"
                )

        # Check for generated-but-not-submitted (crash-before-submit)
        generated = self._idempotency.get_generated_not_submitted()
        if generated:
            logger.info(
                f"Recovery: found {len(generated)} generated-not-submitted "
                f"entries (crash-before-submit). These are safe to discard."
            )
            for g in generated:
                self._idempotency.mark_failed(
                    g.client_order_id, "crash_before_submit"
                )

        logger.info(
            f"Recovery Phase 1 complete: {count} idempotency entries, "
            f"{len(pending)} pending, {len(generated)} discarded"
        )

    def _phase_fetch(self, report: RecoveryReport) -> None:
        """Phase 2: Fetch current state from exchange."""
        logger.info("Recovery Phase 2: FETCH — querying exchange state")

        # Fetch open orders
        open_orders = self._adapter.fetch_open_orders()
        report.exchange_open_orders = len(open_orders)

        # Fetch positions
        positions = self._adapter.fetch_positions()
        active_positions = [
            p for p in positions
            if isinstance(p, dict) and (
                float(p.get("contracts", 0) or 0) > 0
                or float(p.get("contractSize", 0) or 0) > 0
            )
        ]
        report.exchange_positions = len(active_positions)

        # Fetch balance
        balance = self._adapter.fetch_balance()
        if isinstance(balance, dict):
            usdt = balance.get("USDT", balance.get("total", {}))
            if isinstance(usdt, dict):
                report.exchange_balance_usdt = float(usdt.get("total", 0) or 0)
            elif isinstance(usdt, (int, float)):
                report.exchange_balance_usdt = float(usdt)

        logger.info(
            f"Recovery Phase 2 complete: {report.exchange_open_orders} open orders, "
            f"{report.exchange_positions} positions, "
            f"balance={report.exchange_balance_usdt:.2f} USDT"
        )

    def _phase_reconcile(
        self,
        report: RecoveryReport,
        internal_orders: Dict[str, Any],
        internal_positions: Dict[str, Any],
        auto_resolve: bool,
    ) -> None:
        """Phase 3: Reconcile internal vs exchange state."""
        logger.info("Recovery Phase 3: RECONCILE — comparing states")

        report.internal_positions_loaded = len(internal_positions)

        # Build order dict including pending submissions from idempotency
        orders_to_reconcile = dict(internal_orders)

        # Add pending submissions that aren't in internal_orders
        pending = self._idempotency.get_pending_submissions()
        for p in pending:
            if p.client_order_id not in orders_to_reconcile:
                # Create a minimal order-like object for reconciliation
                orders_to_reconcile[p.client_order_id] = _PendingOrderProxy(
                    client_order_id=p.client_order_id,
                    symbol=p.symbol,
                    side=p.side,
                    state="submission_attempted",
                    exchange_order_id=p.exchange_order_id,
                    is_terminal=False,
                )

        result = self._reconciliation.reconcile(
            internal_orders=orders_to_reconcile,
            internal_positions=internal_positions,
            auto_resolve=auto_resolve,
        )
        report.reconciliation = result

        if result.has_mismatches:
            logger.warning(
                f"Recovery Phase 3: {result.mismatch_count} mismatches found"
            )
            for m in result.mismatches:
                logger.warning(
                    f"  Mismatch: {m.mismatch_type.value} {m.symbol} "
                    f"internal={m.internal_state} exchange={m.exchange_state} "
                    f"action={m.recommended_action.value}"
                )
        else:
            logger.info("Recovery Phase 3: reconciliation clean — no mismatches")

    def _phase_rebuild(self, report: RecoveryReport) -> None:
        """Phase 4: Apply resolution actions and sync state."""
        logger.info("Recovery Phase 4: REBUILD — applying resolutions")

        if report.reconciliation is None:
            report.errors.append("No reconciliation result to rebuild from")
            return

        if not report.reconciliation.has_mismatches:
            logger.info("Recovery Phase 4: nothing to rebuild — clean state")
            return

        # Process each mismatch
        for mismatch in report.reconciliation.mismatches:
            try:
                resolved = self._resolve_mismatch(mismatch, report)
                if resolved:
                    report.orders_recovered += 1
                else:
                    report.orders_failed += 1
            except Exception as e:
                logger.error(f"Failed to resolve mismatch: {e}")
                report.orders_failed += 1
                report.errors.append(f"Resolution failed for {mismatch.internal_id}: {e}")

        logger.info(
            f"Recovery Phase 4 complete: "
            f"recovered={report.orders_recovered}, failed={report.orders_failed}"
        )

    def _resolve_mismatch(self, mismatch, report: RecoveryReport) -> bool:
        """Attempt to resolve a single mismatch. Returns True if resolved."""
        action = mismatch.recommended_action

        if action == ReconciliationAction.MARK_ORDER_FAILED:
            # Mark the order as failed in idempotency store
            if mismatch.internal_id:
                self._idempotency.mark_failed(
                    mismatch.internal_id,
                    f"Recovery: {mismatch.details}",
                )
            logger.info(
                f"Recovery: marked {mismatch.internal_id} as failed"
            )
            return True

        if action == ReconciliationAction.CANCEL_ON_EXCHANGE:
            # This was handled by auto_resolve in reconciliation
            logger.info(
                f"Recovery: orphan order {mismatch.exchange_id} "
                f"flagged for cancellation"
            )
            return True

        if action == ReconciliationAction.SYNC_FROM_EXCHANGE:
            logger.warning(
                f"Recovery: mismatch for {mismatch.internal_id} requires "
                f"sync from exchange — logging for manual review"
            )
            return True  # Logged, not auto-resolved

        if action == ReconciliationAction.NO_ACTION:
            return True

        # Other actions log as unresolved
        logger.warning(
            f"Recovery: unresolved mismatch {mismatch.mismatch_type.value} "
            f"for {mismatch.symbol} — requires manual intervention"
        )
        return False

    # ── State Access ──────────────────────────────────────────

    @property
    def last_report(self) -> Optional[RecoveryReport]:
        """Get the most recent recovery report."""
        return self._last_report

    def get_state(self) -> dict:
        """Get manager state for diagnostics."""
        return {
            "recovery_complete": self._recovery_complete,
            "trading_allowed": self.trading_allowed,
            "last_report": (
                self._last_report.to_dict() if self._last_report else None
            ),
        }


# ══════════════════════════════════════════════════════════════
# 4. HELPER PROXY
# ══════════════════════════════════════════════════════════════

class _PendingOrderProxy:
    """Minimal order-like object for reconciling pending idempotency entries."""

    def __init__(
        self,
        client_order_id: str,
        symbol: str,
        side: str,
        state: str,
        exchange_order_id: str,
        is_terminal: bool,
    ):
        self.client_order_id = client_order_id
        self.symbol = symbol
        self.side = side
        self.state = state
        self.exchange_order_id = exchange_order_id
        self.is_terminal = is_terminal
