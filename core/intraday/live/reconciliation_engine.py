"""
Phase 8: Order Reconciliation Engine

Compares internal order/position state against exchange state
and produces reconciliation actions. Detects and resolves:

1. Orders we think are open but exchange says filled/cancelled
2. Orders we think are filled but exchange says open
3. Positions we have internally but exchange doesn't
4. Positions on exchange that we don't have internally
5. Quantity mismatches (partial fills not recorded)
6. Orphan orders (on exchange, not in our system)

RECONCILIATION-FIRST RULE:
System MUST NOT resume trading before reconciliation completes.
Any mismatch → logged, reported, and must be resolved before
new orders are allowed.

RESOLUTION STRATEGY:
- Exchange state is AUTHORITATIVE for what actually happened
- Internal state is AUTHORITATIVE for intent (what we wanted)
- Mismatches are resolved by trusting exchange for fills/positions
  and syncing internal state to match

No Qt imports. No PySide6. Pure Python.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════
# 1. TYPES
# ══════════════════════════════════════════════════════════════

class MismatchType(str, Enum):
    """Types of state mismatches between internal and exchange."""
    ORDER_MISSING_ON_EXCHANGE = "order_missing_on_exchange"
    ORDER_STATE_MISMATCH = "order_state_mismatch"
    ORDER_FILL_MISMATCH = "order_fill_mismatch"
    ORPHAN_ORDER_ON_EXCHANGE = "orphan_order_on_exchange"
    POSITION_MISSING_ON_EXCHANGE = "position_missing_on_exchange"
    POSITION_MISSING_INTERNALLY = "position_missing_internally"
    POSITION_SIZE_MISMATCH = "position_size_mismatch"
    POSITION_SIDE_MISMATCH = "position_side_mismatch"


class ReconciliationAction(str, Enum):
    """Actions to resolve mismatches."""
    SYNC_FROM_EXCHANGE = "sync_from_exchange"       # Trust exchange state
    CANCEL_ON_EXCHANGE = "cancel_on_exchange"        # Cancel orphan order
    CLOSE_INTERNAL_POSITION = "close_internal_pos"   # Position gone from exchange
    OPEN_INTERNAL_POSITION = "open_internal_pos"     # Position on exchange, not internal
    UPDATE_FILL_DATA = "update_fill_data"            # Sync fill quantities
    UPDATE_POSITION_SIZE = "update_position_size"    # Sync position size
    MARK_ORDER_FAILED = "mark_order_failed"          # Order disappeared
    NO_ACTION = "no_action"                          # Match confirmed


@dataclass(frozen=True)
class Mismatch:
    """A single detected mismatch between internal and exchange state."""
    mismatch_type: MismatchType
    symbol: str
    internal_id: str              # Our order/position ID
    exchange_id: str              # Exchange order/position ID
    internal_state: str           # What we think the state is
    exchange_state: str           # What exchange says
    internal_value: str = ""      # E.g., quantity we think we have
    exchange_value: str = ""      # E.g., quantity exchange reports
    recommended_action: ReconciliationAction = ReconciliationAction.NO_ACTION
    details: str = ""

    def to_dict(self) -> dict:
        return {
            "mismatch_type": self.mismatch_type.value,
            "symbol": self.symbol,
            "internal_id": self.internal_id,
            "exchange_id": self.exchange_id,
            "internal_state": self.internal_state,
            "exchange_state": self.exchange_state,
            "internal_value": self.internal_value,
            "exchange_value": self.exchange_value,
            "recommended_action": self.recommended_action.value,
            "details": self.details,
        }


@dataclass
class ReconciliationResult:
    """Complete result of a reconciliation run."""
    success: bool
    timestamp_ms: int
    orders_checked: int = 0
    positions_checked: int = 0
    mismatches: List[Mismatch] = field(default_factory=list)
    actions_taken: List[Dict[str, Any]] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    duration_ms: int = 0

    @property
    def has_mismatches(self) -> bool:
        return len(self.mismatches) > 0

    @property
    def is_clean(self) -> bool:
        """True if reconciliation found no mismatches and no errors."""
        return not self.mismatches and not self.errors

    @property
    def mismatch_count(self) -> int:
        return len(self.mismatches)

    def to_dict(self) -> dict:
        return {
            "success": self.success,
            "timestamp_ms": self.timestamp_ms,
            "orders_checked": self.orders_checked,
            "positions_checked": self.positions_checked,
            "mismatch_count": self.mismatch_count,
            "mismatches": [m.to_dict() for m in self.mismatches],
            "actions_taken": self.actions_taken,
            "errors": self.errors,
            "duration_ms": self.duration_ms,
        }


# ══════════════════════════════════════════════════════════════
# 2. RECONCILIATION ENGINE
# ══════════════════════════════════════════════════════════════

class OrderReconciliationEngine:
    """
    Compares internal state against exchange state and produces
    reconciliation actions.

    Usage:
        engine = OrderReconciliationEngine(exchange_adapter)
        result = engine.reconcile(
            internal_orders=live_executor.get_all_orders(),
            internal_positions=portfolio_state.get_open_positions(),
        )
        if result.has_mismatches:
            for mismatch in result.mismatches:
                # Handle each mismatch
                ...
    """

    def __init__(
        self,
        exchange_adapter,  # ExchangeAdapter
        now_ms_fn=None,
    ):
        self._adapter = exchange_adapter
        self._now_ms_fn = now_ms_fn or (lambda: int(time.time() * 1000))
        self._last_result: Optional[ReconciliationResult] = None

        logger.info("OrderReconciliationEngine initialized")

    def reconcile(
        self,
        internal_orders: Dict[str, Any],
        internal_positions: Optional[Dict[str, Any]] = None,
        symbols: Optional[List[str]] = None,
        auto_resolve: bool = False,
    ) -> ReconciliationResult:
        """
        Run full reconciliation.

        Args:
            internal_orders: Dict of client_order_id → LiveOrder.
            internal_positions: Dict of position_id → PositionRecord.
            symbols: Symbols to reconcile (None = all).
            auto_resolve: If True, automatically apply resolution actions.

        Returns:
            ReconciliationResult with all mismatches and actions.
        """
        start_ms = self._now_ms_fn()
        result = ReconciliationResult(
            success=True,
            timestamp_ms=start_ms,
        )

        try:
            # Phase 1: Reconcile orders
            self._reconcile_orders(internal_orders, symbols, result)

            # Phase 2: Reconcile positions
            if internal_positions is not None:
                self._reconcile_positions(internal_positions, symbols, result)

            # Phase 3: Auto-resolve if requested
            if auto_resolve and result.has_mismatches:
                self._auto_resolve(result)

        except Exception as e:
            logger.error(f"Reconciliation failed: {e}", exc_info=True)
            result.success = False
            result.errors.append(f"Reconciliation error: {e}")

        result.duration_ms = self._now_ms_fn() - start_ms
        self._last_result = result

        logger.info(
            f"Reconciliation completed in {result.duration_ms}ms: "
            f"orders={result.orders_checked}, positions={result.positions_checked}, "
            f"mismatches={result.mismatch_count}, errors={len(result.errors)}"
        )

        return result

    # ── Order Reconciliation ──────────────────────────────────

    def _reconcile_orders(
        self,
        internal_orders: Dict[str, Any],
        symbols: Optional[List[str]],
        result: ReconciliationResult,
    ) -> None:
        """Compare internal orders against exchange orders."""

        # Fetch open orders from exchange
        exchange_orders = self._adapter.fetch_open_orders(symbol=None)
        exchange_by_client_id: Dict[str, Any] = {}
        exchange_by_id: Dict[str, Any] = {}

        for eo in exchange_orders:
            if hasattr(eo, "raw") and eo.raw:
                raw = eo.raw
                # Bybit uses 'orderLinkId' for client order ID
                client_id = raw.get("clientOrderId") or raw.get("orderLinkId", "")
                exch_id = raw.get("id", "")
                if client_id:
                    exchange_by_client_id[client_id] = eo
                if exch_id:
                    exchange_by_id[exch_id] = eo

        # Check each internal non-terminal order
        for client_order_id, order in internal_orders.items():
            if symbols and hasattr(order, "symbol") and order.symbol not in symbols:
                continue

            result.orders_checked += 1

            # Skip terminal orders — they're done
            if hasattr(order, "is_terminal") and order.is_terminal:
                continue

            # Check if order exists on exchange
            exchange_order = exchange_by_client_id.get(client_order_id)

            if exchange_order is None:
                # Not in open orders — might be filled or cancelled
                # Try to fetch by exchange_order_id if we have one
                exch_id = getattr(order, "exchange_order_id", "")
                if exch_id:
                    fetched = self._adapter.fetch_order(
                        exchange_order_id=exch_id,
                        symbol=self._format_symbol(order.symbol),
                    )
                    if fetched.success:
                        self._check_order_state(
                            client_order_id, order, fetched, result
                        )
                    else:
                        # Can't find on exchange at all
                        result.mismatches.append(Mismatch(
                            mismatch_type=MismatchType.ORDER_MISSING_ON_EXCHANGE,
                            symbol=order.symbol,
                            internal_id=client_order_id,
                            exchange_id=exch_id,
                            internal_state=getattr(order, "state", "unknown"),
                            exchange_state="not_found",
                            recommended_action=ReconciliationAction.MARK_ORDER_FAILED,
                            details="Order not found on exchange",
                        ))
                else:
                    # No exchange_order_id — submitted but never acknowledged
                    state = getattr(order, "state", "unknown")
                    if state == "submission_attempted":
                        result.mismatches.append(Mismatch(
                            mismatch_type=MismatchType.ORDER_MISSING_ON_EXCHANGE,
                            symbol=order.symbol,
                            internal_id=client_order_id,
                            exchange_id="",
                            internal_state=state,
                            exchange_state="not_found",
                            recommended_action=ReconciliationAction.MARK_ORDER_FAILED,
                            details="Submitted but never acknowledged — likely crash-after-submit",
                        ))

        # Check for orphan orders on exchange (orders we don't know about)
        for client_id, eo in exchange_by_client_id.items():
            if client_id.startswith("NT-") and client_id not in internal_orders:
                symbol = ""
                if hasattr(eo, "raw") and eo.raw:
                    symbol = eo.raw.get("symbol", "")
                result.mismatches.append(Mismatch(
                    mismatch_type=MismatchType.ORPHAN_ORDER_ON_EXCHANGE,
                    symbol=symbol,
                    internal_id="",
                    exchange_id=eo.exchange_order_id if hasattr(eo, "exchange_order_id") else "",
                    internal_state="not_found",
                    exchange_state="open",
                    recommended_action=ReconciliationAction.CANCEL_ON_EXCHANGE,
                    details=f"Orphan order with client_id={client_id}",
                ))

    def _check_order_state(
        self,
        client_order_id: str,
        internal_order: Any,
        exchange_response: Any,
        result: ReconciliationResult,
    ) -> None:
        """Check if internal order state matches exchange state."""
        internal_state = getattr(internal_order, "state", "unknown")
        exchange_status = getattr(exchange_response, "status", "unknown")

        # Map exchange status to expected lifecycle states
        if exchange_status == "closed":
            # Exchange says filled
            if internal_state not in ("filled",):
                result.mismatches.append(Mismatch(
                    mismatch_type=MismatchType.ORDER_STATE_MISMATCH,
                    symbol=internal_order.symbol,
                    internal_id=client_order_id,
                    exchange_id=getattr(internal_order, "exchange_order_id", ""),
                    internal_state=str(internal_state),
                    exchange_state="filled",
                    recommended_action=ReconciliationAction.SYNC_FROM_EXCHANGE,
                    details="Exchange shows filled, internal state disagrees",
                ))

            # Check fill quantity match
            internal_qty = getattr(internal_order, "filled_quantity", 0)
            exchange_qty = getattr(exchange_response, "filled_quantity", 0)
            if exchange_qty > 0 and abs(internal_qty - exchange_qty) > 1e-8:
                result.mismatches.append(Mismatch(
                    mismatch_type=MismatchType.ORDER_FILL_MISMATCH,
                    symbol=internal_order.symbol,
                    internal_id=client_order_id,
                    exchange_id=getattr(internal_order, "exchange_order_id", ""),
                    internal_state=str(internal_state),
                    exchange_state="filled",
                    internal_value=str(internal_qty),
                    exchange_value=str(exchange_qty),
                    recommended_action=ReconciliationAction.UPDATE_FILL_DATA,
                    details=f"Fill qty mismatch: internal={internal_qty} vs exchange={exchange_qty}",
                ))

        elif exchange_status == "canceled":
            if internal_state not in ("cancelled", "failed"):
                result.mismatches.append(Mismatch(
                    mismatch_type=MismatchType.ORDER_STATE_MISMATCH,
                    symbol=internal_order.symbol,
                    internal_id=client_order_id,
                    exchange_id=getattr(internal_order, "exchange_order_id", ""),
                    internal_state=str(internal_state),
                    exchange_state="cancelled",
                    recommended_action=ReconciliationAction.SYNC_FROM_EXCHANGE,
                    details="Exchange shows cancelled, internal state disagrees",
                ))

    # ── Position Reconciliation ───────────────────────────────

    def _reconcile_positions(
        self,
        internal_positions: Dict[str, Any],
        symbols: Optional[List[str]],
        result: ReconciliationResult,
    ) -> None:
        """Compare internal positions against exchange positions."""

        # Fetch positions from exchange
        exchange_positions = self._adapter.fetch_positions()
        exchange_by_symbol: Dict[str, Any] = {}

        for ep in exchange_positions:
            if isinstance(ep, dict):
                sym = ep.get("symbol", "")
                size = float(ep.get("contracts", 0) or ep.get("contractSize", 0) or 0)
                if size > 0:
                    exchange_by_symbol[sym] = ep

        # Check each internal open position
        internal_symbols_checked = set()
        for pos_id, position in internal_positions.items():
            symbol = getattr(position, "symbol", "")
            if symbols and symbol not in symbols:
                continue

            result.positions_checked += 1
            internal_symbols_checked.add(symbol)

            # Find matching exchange position
            exchange_pos = exchange_by_symbol.get(symbol)

            # Try CCXT format too
            if not exchange_pos:
                ccxt_symbol = self._format_symbol(symbol)
                exchange_pos = exchange_by_symbol.get(ccxt_symbol)

            if exchange_pos is None:
                result.mismatches.append(Mismatch(
                    mismatch_type=MismatchType.POSITION_MISSING_ON_EXCHANGE,
                    symbol=symbol,
                    internal_id=pos_id,
                    exchange_id="",
                    internal_state="open",
                    exchange_state="not_found",
                    recommended_action=ReconciliationAction.CLOSE_INTERNAL_POSITION,
                    details="Position exists internally but not on exchange",
                ))
                continue

            # Check size match
            internal_qty = float(getattr(position, "quantity", 0))
            exchange_qty = float(
                exchange_pos.get("contracts", 0) or
                exchange_pos.get("contractSize", 0) or 0
            )
            if abs(internal_qty - exchange_qty) > 1e-8:
                result.mismatches.append(Mismatch(
                    mismatch_type=MismatchType.POSITION_SIZE_MISMATCH,
                    symbol=symbol,
                    internal_id=pos_id,
                    exchange_id=str(exchange_pos.get("id", "")),
                    internal_state=f"qty={internal_qty}",
                    exchange_state=f"qty={exchange_qty}",
                    internal_value=str(internal_qty),
                    exchange_value=str(exchange_qty),
                    recommended_action=ReconciliationAction.UPDATE_POSITION_SIZE,
                    details=f"Position size mismatch: internal={internal_qty} vs exchange={exchange_qty}",
                ))

        # Check for positions on exchange that we don't have internally
        internal_symbols = {
            getattr(p, "symbol", "") for p in internal_positions.values()
        }
        for sym, ep in exchange_by_symbol.items():
            # Normalize symbol for comparison
            if sym not in internal_symbols and sym not in internal_symbols_checked:
                result.mismatches.append(Mismatch(
                    mismatch_type=MismatchType.POSITION_MISSING_INTERNALLY,
                    symbol=sym,
                    internal_id="",
                    exchange_id=str(ep.get("id", "")),
                    internal_state="not_found",
                    exchange_state="open",
                    recommended_action=ReconciliationAction.OPEN_INTERNAL_POSITION,
                    details="Position exists on exchange but not internally",
                ))

    # ── Auto-Resolution ───────────────────────────────────────

    def _auto_resolve(self, result: ReconciliationResult) -> None:
        """
        Automatically apply safe resolution actions.

        Only resolves clear-cut mismatches. Ambiguous cases are
        left for manual review.
        """
        for mismatch in result.mismatches:
            if mismatch.recommended_action == ReconciliationAction.CANCEL_ON_EXCHANGE:
                # Cancel orphan orders
                if mismatch.exchange_id:
                    try:
                        resp = self._adapter.cancel_order(
                            exchange_order_id=mismatch.exchange_id,
                            symbol=self._format_symbol(mismatch.symbol),
                        )
                        result.actions_taken.append({
                            "action": "cancel_orphan",
                            "exchange_id": mismatch.exchange_id,
                            "symbol": mismatch.symbol,
                            "success": resp.success,
                        })
                    except Exception as e:
                        result.errors.append(
                            f"Failed to cancel orphan {mismatch.exchange_id}: {e}"
                        )

            elif mismatch.recommended_action == ReconciliationAction.SYNC_FROM_EXCHANGE:
                # Log for manual resolution — we don't auto-sync fills
                result.actions_taken.append({
                    "action": "requires_manual_sync",
                    "mismatch_type": mismatch.mismatch_type.value,
                    "symbol": mismatch.symbol,
                    "internal_id": mismatch.internal_id,
                    "details": mismatch.details,
                })

    # ── Helpers ────────────────────────────────────────────────

    @staticmethod
    def _format_symbol(symbol: str) -> str:
        """Convert internal symbol to CCXT format."""
        if "/" in symbol:
            return symbol
        for quote in ("USDT", "BUSD", "USDC"):
            if symbol.endswith(quote):
                base = symbol[: -len(quote)]
                return f"{base}/{quote}:{quote}"
        return symbol

    @property
    def last_result(self) -> Optional[ReconciliationResult]:
        """Get the result of the most recent reconciliation."""
        return self._last_result

    def get_state(self) -> dict:
        """Get engine state for diagnostics."""
        return {
            "last_reconciliation": (
                self._last_result.to_dict() if self._last_result else None
            ),
        }
