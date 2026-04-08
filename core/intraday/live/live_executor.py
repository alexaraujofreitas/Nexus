"""
Phase 8: LiveExecutor

Accepts ExecutionRequest, submits to exchange via ExchangeAdapter,
tracks fill lifecycle, and returns (OrderRecord, FillRecord) —
the same contract as the paper path.

FAIL-CLOSED PRINCIPLES:
1. Never assume submission succeeded without exchange confirmation
2. Never retry rejected orders (insufficient funds, invalid params)
3. Only retry transient errors (network, rate limit)
4. Crash-after-submit → RECOVERY_PENDING (reconcile on restart)
5. Timeout → FAILED (never leave in SUBMISSION_ATTEMPTED forever)
6. All state transitions through LiveOrder.transition() — validated

IDEMPOTENCY:
- Deterministic client_order_id from request_id + symbol + side + ts
- IdempotencyStore tracks all generated IDs
- If resubmitted with same client_order_id, exchange deduplicates
- On restart, check IdempotencyStore for pending submissions

CONTRACT COMPATIBILITY:
- Input: ExecutionRequest (same as paper path)
- Output: Tuple[OrderRecord, Optional[FillRecord]] (same as paper path)
- Plugs into ExecutionEngine.execute() seamlessly

No Qt imports. No PySide6. Pure Python.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Dict, Optional, Tuple

from core.intraday.execution_contracts import (
    ExecutionRequest,
    FillRecord,
    OrderRecord,
    OrderStatus,
    OrderType,
    Side,
    _make_id,
)
from .order_lifecycle import (
    LiveOrder,
    OrderLifecycleState,
    OrderTransitionError,
    TERMINAL_STATES,
    make_client_order_id,
)
from .idempotency_store import IdempotencyStore
from .exchange_adapter import (
    ExchangeAdapter,
    ExchangeError,
    ExchangeErrorClass,
    ExchangeResponse,
)

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════
# CONFIGURATION
# ══════════════════════════════════════════════════════════════

# Maximum time to wait for order acknowledgement (30s)
ORDER_ACK_TIMEOUT_MS = 30_000

# Maximum time to wait for market order fill (60s)
MARKET_FILL_TIMEOUT_MS = 60_000

# Maximum time to wait for limit order fill before cancellation
LIMIT_FILL_TIMEOUT_MS = 300_000  # 5 minutes

# Fee rate for USDT-M perpetuals (Bybit taker)
DEFAULT_TAKER_FEE_RATE = 0.00055  # 0.055%
DEFAULT_MAKER_FEE_RATE = 0.0002   # 0.02%


# ══════════════════════════════════════════════════════════════
# LIVE EXECUTOR
# ══════════════════════════════════════════════════════════════

class LiveExecutor:
    """
    Phase 8 Live Executor — exchange order submission and fill tracking.

    Lifecycle per order:
    1. Generate deterministic client_order_id
    2. Register in IdempotencyStore (persisted)
    3. Create LiveOrder (INTENT_CREATED)
    4. Submit to exchange (SUBMISSION_ATTEMPTED)
    5. Parse response (ACKNOWLEDGED → LIVE → FILLED, or REJECTED/FAILED)
    6. Convert to (OrderRecord, FillRecord) for upstream compatibility

    Thread-safe: each execute() call is self-contained.
    """

    def __init__(
        self,
        exchange_adapter: ExchangeAdapter,
        idempotency_store: IdempotencyStore,
        now_ms_fn=None,
        taker_fee_rate: float = DEFAULT_TAKER_FEE_RATE,
        maker_fee_rate: float = DEFAULT_MAKER_FEE_RATE,
    ):
        """
        Args:
            exchange_adapter: ExchangeAdapter wrapping CCXT.
            idempotency_store: IdempotencyStore for dedup.
            now_ms_fn: Optional time function for testing.
            taker_fee_rate: Taker fee rate.
            maker_fee_rate: Maker fee rate.
        """
        self._adapter = exchange_adapter
        self._idempotency = idempotency_store
        self._now_ms_fn = now_ms_fn or (lambda: int(time.time() * 1000))
        self._taker_fee_rate = taker_fee_rate
        self._maker_fee_rate = maker_fee_rate

        # In-flight order tracking (keyed by client_order_id)
        self._live_orders: Dict[str, LiveOrder] = {}

        logger.info("LiveExecutor initialized")

    # ── Main Entry Point ──────────────────────────────────────

    def execute(
        self, request: ExecutionRequest
    ) -> Tuple[OrderRecord, Optional[FillRecord]]:
        """
        Execute an order on the live exchange.

        Args:
            request: Validated ExecutionRequest from ExecutionEngine.

        Returns:
            Tuple of (OrderRecord, Optional[FillRecord]).
            Same contract as paper path (OrderManager.submit_order).
        """
        now = self._now_ms_fn()

        # 1. Generate deterministic client_order_id
        client_order_id = make_client_order_id(
            request_id=request.request_id,
            symbol=request.symbol,
            side=request.side.value,
            timestamp_ms=request.created_at_ms,
        )

        # 2. Check idempotency — have we already submitted this?
        if self._idempotency.exists(client_order_id):
            entry = self._idempotency.get(client_order_id)
            if entry and entry.state in ("submitted", "confirmed"):
                logger.warning(
                    f"LiveExecutor: duplicate request detected for "
                    f"{client_order_id} (state={entry.state}), "
                    f"fetching existing order"
                )
                return self._recover_existing_order(
                    client_order_id, entry, request
                )

        # 3. Register in idempotency store (persisted before submission)
        self._idempotency.register(
            client_order_id=client_order_id,
            request_id=request.request_id,
            symbol=request.symbol,
            side=request.side.value,
        )

        # 4. Create LiveOrder
        live_order = LiveOrder(
            client_order_id=client_order_id,
            request_id=request.request_id,
            decision_id=request.decision_id,
            trigger_id=request.trigger_id,
            symbol=request.symbol,
            side=request.side.value,
            order_type="market",  # Phase 8 uses market orders
            requested_price=request.entry_price,
            requested_quantity=request.quantity,
            size_usdt=request.size_usdt,
            strategy_name=request.strategy_name,
            regime=request.regime,
            stop_loss=request.stop_loss,
            take_profit=request.take_profit,
            created_at_ms=now,
        )
        self._live_orders[client_order_id] = live_order

        # 5. Submit to exchange
        try:
            return self._submit_and_track(live_order, request)
        except Exception as e:
            logger.error(
                f"LiveExecutor: unhandled exception during execution "
                f"for {client_order_id}: {e}",
                exc_info=True,
            )
            return self._make_failed_result(
                live_order, request, f"Unhandled error: {e}"
            )

    # ── Submission and Tracking ───────────────────────────────

    def _submit_and_track(
        self,
        live_order: LiveOrder,
        request: ExecutionRequest,
    ) -> Tuple[OrderRecord, Optional[FillRecord]]:
        """Submit order and track through lifecycle to terminal state."""

        # Transition: INTENT_CREATED → SUBMISSION_ATTEMPTED
        live_order.transition(
            OrderLifecycleState.SUBMISSION_ATTEMPTED,
            reason="submitting to exchange",
            timestamp_ms=self._now_ms_fn(),
        )
        self._idempotency.mark_submitted(live_order.client_order_id)

        # Format symbol for exchange (e.g., "BTCUSDT" → "BTC/USDT:USDT")
        exchange_symbol = self._format_symbol(request.symbol)

        # Submit to exchange
        response = self._adapter.create_order(
            symbol=exchange_symbol,
            order_type=live_order.order_type,
            side=live_order.side,
            quantity=live_order.requested_quantity,
            price=None,  # Market order, no price
            client_order_id=live_order.client_order_id,
        )

        if not response.success:
            return self._handle_submission_failure(live_order, request, response)

        # Submission succeeded — update state
        exchange_order_id = response.exchange_order_id

        # Transition: SUBMISSION_ATTEMPTED → ACKNOWLEDGED
        live_order.transition(
            OrderLifecycleState.ACKNOWLEDGED,
            reason=f"exchange acknowledged: {exchange_order_id}",
            timestamp_ms=self._now_ms_fn(),
            exchange_order_id=exchange_order_id,
        )
        self._idempotency.mark_confirmed(
            live_order.client_order_id, exchange_order_id
        )

        # For market orders, check if already filled in response
        if response.status == "closed" and response.filled_quantity > 0:
            # Market order filled immediately
            return self._handle_immediate_fill(live_order, request, response)

        if response.filled_quantity > 0:
            # Partial fill in response
            live_order.record_fill(
                fill_price=response.avg_price,
                fill_quantity=response.filled_quantity,
                fee_usdt=response.fee,
                timestamp_ms=self._now_ms_fn(),
            )

        # Transition to LIVE
        live_order.transition(
            OrderLifecycleState.LIVE,
            reason="order is live on exchange",
            timestamp_ms=self._now_ms_fn(),
        )

        # For market orders, poll until filled or timeout
        if live_order.order_type == "market":
            return self._poll_until_filled(live_order, request, exchange_order_id)

        # For limit orders (future), return current state
        return self._make_order_result(live_order, request)

    def _handle_submission_failure(
        self,
        live_order: LiveOrder,
        request: ExecutionRequest,
        response: ExchangeResponse,
    ) -> Tuple[OrderRecord, Optional[FillRecord]]:
        """Handle failed order submission."""
        error = response.error
        error_class = error.error_class if error else ExchangeErrorClass.UNKNOWN
        reason = str(error) if error else "Unknown submission failure"

        if error_class == ExchangeErrorClass.DUPLICATE:
            # Order already exists on exchange — idempotent success
            logger.warning(
                f"LiveExecutor: duplicate order on exchange for "
                f"{live_order.client_order_id}, treating as success"
            )
            # Try to fetch the existing order
            return self._recover_from_duplicate(live_order, request)

        if error_class == ExchangeErrorClass.REJECTED:
            live_order.transition(
                OrderLifecycleState.REJECTED,
                reason=reason,
                timestamp_ms=self._now_ms_fn(),
            )
            self._idempotency.mark_failed(live_order.client_order_id, reason)
        else:
            live_order.transition(
                OrderLifecycleState.FAILED,
                reason=reason,
                timestamp_ms=self._now_ms_fn(),
            )
            self._idempotency.mark_failed(live_order.client_order_id, reason)

        return self._make_failed_result(live_order, request, reason)

    def _handle_immediate_fill(
        self,
        live_order: LiveOrder,
        request: ExecutionRequest,
        response: ExchangeResponse,
    ) -> Tuple[OrderRecord, Optional[FillRecord]]:
        """Handle market order that filled immediately in the create response."""
        live_order.record_fill(
            fill_price=response.avg_price,
            fill_quantity=response.filled_quantity,
            fee_usdt=response.fee,
            timestamp_ms=self._now_ms_fn(),
        )

        # ACKNOWLEDGED → LIVE → FILLED (two transitions)
        live_order.transition(
            OrderLifecycleState.LIVE,
            reason="order went live",
            timestamp_ms=self._now_ms_fn(),
        )
        live_order.transition(
            OrderLifecycleState.FILLED,
            reason="market order filled immediately",
            timestamp_ms=self._now_ms_fn(),
        )
        self._idempotency.mark_completed(live_order.client_order_id)

        logger.info(
            f"LiveExecutor: {live_order.client_order_id} filled immediately "
            f"@ {response.avg_price} qty={response.filled_quantity}"
        )

        return self._make_order_result(live_order, request)

    def _poll_until_filled(
        self,
        live_order: LiveOrder,
        request: ExecutionRequest,
        exchange_order_id: str,
        max_polls: int = 10,
        poll_interval_ms: int = 2_000,
    ) -> Tuple[OrderRecord, Optional[FillRecord]]:
        """
        Poll exchange until market order is filled or timeout.

        Market orders should fill nearly instantly. This handles the
        rare case where the create response didn't include fill data.
        """
        exchange_symbol = self._format_symbol(request.symbol)
        deadline_ms = self._now_ms_fn() + MARKET_FILL_TIMEOUT_MS

        for poll in range(max_polls):
            if self._now_ms_fn() >= deadline_ms:
                break

            time.sleep(poll_interval_ms / 1000.0)

            response = self._adapter.fetch_order(
                exchange_order_id=exchange_order_id,
                symbol=exchange_symbol,
            )

            if not response.success:
                logger.warning(
                    f"LiveExecutor: poll {poll + 1} failed for "
                    f"{live_order.client_order_id}: {response.error}"
                )
                continue

            if response.status == "closed" and response.filled_quantity > 0:
                # Order filled
                live_order.record_fill(
                    fill_price=response.avg_price,
                    fill_quantity=response.filled_quantity - live_order.filled_quantity,
                    fee_usdt=response.fee - live_order.fee_usdt,
                    timestamp_ms=self._now_ms_fn(),
                )
                live_order.transition(
                    OrderLifecycleState.FILLED,
                    reason="market order filled (polled)",
                    timestamp_ms=self._now_ms_fn(),
                )
                self._idempotency.mark_completed(live_order.client_order_id)

                logger.info(
                    f"LiveExecutor: {live_order.client_order_id} filled "
                    f"after {poll + 1} polls @ {response.avg_price}"
                )
                return self._make_order_result(live_order, request)

            if response.filled_quantity > live_order.filled_quantity:
                # Partial fill progress
                delta_qty = response.filled_quantity - live_order.filled_quantity
                delta_fee = max(0, response.fee - live_order.fee_usdt)
                live_order.record_fill(
                    fill_price=response.avg_price,
                    fill_quantity=delta_qty,
                    fee_usdt=delta_fee,
                    timestamp_ms=self._now_ms_fn(),
                )
                if live_order.state == OrderLifecycleState.LIVE:
                    live_order.transition(
                        OrderLifecycleState.PARTIALLY_FILLED,
                        reason=f"partial fill: {live_order.fill_pct:.1%}",
                        timestamp_ms=self._now_ms_fn(),
                    )

        # Timeout — market order should have filled by now
        if not live_order.is_terminal:
            if live_order.filled_quantity > 0:
                # Partially filled — accept what we got
                target_state = OrderLifecycleState.FILLED
                if live_order.state == OrderLifecycleState.LIVE:
                    live_order.transition(
                        OrderLifecycleState.PARTIALLY_FILLED,
                        reason="partial fill at timeout",
                        timestamp_ms=self._now_ms_fn(),
                    )
                live_order.transition(
                    target_state,
                    reason="accepting partial fill at timeout",
                    timestamp_ms=self._now_ms_fn(),
                )
            else:
                live_order.transition(
                    OrderLifecycleState.FAILED,
                    reason="market order fill timeout — no fills received",
                    timestamp_ms=self._now_ms_fn(),
                )
                self._idempotency.mark_failed(
                    live_order.client_order_id,
                    "fill_timeout",
                )

        return self._make_order_result(live_order, request)

    # ── Recovery Helpers ──────────────────────────────────────

    def _recover_existing_order(
        self,
        client_order_id: str,
        entry,
        request: ExecutionRequest,
    ) -> Tuple[OrderRecord, Optional[FillRecord]]:
        """Recover state of an order that was already submitted."""
        if not entry.exchange_order_id:
            # Submitted but no exchange_order_id — can't recover
            return self._make_failed_result_from_request(
                request, f"Duplicate but no exchange_order_id for {client_order_id}"
            )

        exchange_symbol = self._format_symbol(request.symbol)
        response = self._adapter.fetch_order(
            exchange_order_id=entry.exchange_order_id,
            symbol=exchange_symbol,
        )

        if not response.success:
            return self._make_failed_result_from_request(
                request, f"Failed to fetch existing order: {response.error}"
            )

        # Build result from exchange state
        return self._build_result_from_response(
            client_order_id, request, response
        )

    def _recover_from_duplicate(
        self,
        live_order: LiveOrder,
        request: ExecutionRequest,
    ) -> Tuple[OrderRecord, Optional[FillRecord]]:
        """Handle duplicate order error from exchange."""
        # Transition to ACKNOWLEDGED (exchange has the order)
        try:
            live_order.transition(
                OrderLifecycleState.ACKNOWLEDGED,
                reason="duplicate order acknowledged by exchange",
                timestamp_ms=self._now_ms_fn(),
            )
        except OrderTransitionError:
            pass  # Already past this state

        # We don't know the exchange_order_id in this case
        # Mark as failed for safety — reconciliation will handle it
        live_order.transition(
            OrderLifecycleState.FAILED,
            reason="duplicate order, needs reconciliation",
            timestamp_ms=self._now_ms_fn(),
        )
        return self._make_failed_result(
            live_order, request, "duplicate order — reconciliation required"
        )

    # ── Result Builders ───────────────────────────────────────

    def _make_order_result(
        self,
        live_order: LiveOrder,
        request: ExecutionRequest,
    ) -> Tuple[OrderRecord, Optional[FillRecord]]:
        """Convert LiveOrder to (OrderRecord, Optional[FillRecord])."""
        now = self._now_ms_fn()

        # Map lifecycle state to OrderStatus
        status_map = {
            OrderLifecycleState.FILLED: OrderStatus.FILLED,
            OrderLifecycleState.PARTIALLY_FILLED: OrderStatus.PARTIALLY_FILLED,
            OrderLifecycleState.CANCELLED: OrderStatus.CANCELLED,
            OrderLifecycleState.REJECTED: OrderStatus.FAILED,
            OrderLifecycleState.FAILED: OrderStatus.FAILED,
        }
        order_status = status_map.get(live_order.state, OrderStatus.PENDING)

        # Compute slippage
        slippage_pct = live_order.slippage_pct

        # Compute fee if not already known
        fee_usdt = live_order.fee_usdt
        if fee_usdt == 0 and live_order.filled_quantity > 0:
            fee_usdt = (
                live_order.filled_price_avg
                * live_order.filled_quantity
                * self._taker_fee_rate
            )

        order_record = OrderRecord(
            order_id=_make_id(
                live_order.client_order_id, "order", now
            ),
            request_id=request.request_id,
            decision_id=request.decision_id,
            trigger_id=request.trigger_id,
            symbol=request.symbol,
            side=request.side,
            order_type=OrderType.MARKET,
            requested_price=request.entry_price,
            requested_quantity=request.quantity,
            filled_price=live_order.filled_price_avg,
            filled_quantity=live_order.filled_quantity,
            fee_usdt=fee_usdt,
            slippage_pct=slippage_pct,
            status=order_status,
            failure_reason=live_order.failure_reason,
            created_at_ms=live_order.created_at_ms,
            filled_at_ms=live_order.last_fill_at_ms or now,
        )

        # Build FillRecord only if we have fills
        fill_record = None
        if live_order.filled_quantity > 0:
            fill_record = FillRecord(
                fill_id=_make_id(
                    live_order.client_order_id, "fill", now
                ),
                order_id=order_record.order_id,
                symbol=request.symbol,
                side=request.side,
                price=live_order.filled_price_avg,
                quantity=live_order.filled_quantity,
                fee_usdt=fee_usdt,
                fee_rate=self._taker_fee_rate,
                slippage_pct=slippage_pct,
                is_maker=False,  # Market orders are taker
                filled_at_ms=live_order.last_fill_at_ms or now,
            )

        return order_record, fill_record

    def _make_failed_result(
        self,
        live_order: LiveOrder,
        request: ExecutionRequest,
        reason: str,
    ) -> Tuple[OrderRecord, None]:
        """Build failed OrderRecord from LiveOrder."""
        now = self._now_ms_fn()

        order_record = OrderRecord(
            order_id=_make_id(
                live_order.client_order_id, "order", now
            ),
            request_id=request.request_id,
            decision_id=request.decision_id,
            trigger_id=request.trigger_id,
            symbol=request.symbol,
            side=request.side,
            order_type=OrderType.MARKET,
            requested_price=request.entry_price,
            requested_quantity=request.quantity,
            status=OrderStatus.FAILED,
            failure_reason=reason,
            created_at_ms=live_order.created_at_ms,
        )
        return order_record, None

    def _make_failed_result_from_request(
        self,
        request: ExecutionRequest,
        reason: str,
    ) -> Tuple[OrderRecord, None]:
        """Build failed OrderRecord directly from request (no LiveOrder)."""
        now = self._now_ms_fn()

        order_record = OrderRecord(
            order_id=_make_id(request.request_id, "order", now),
            request_id=request.request_id,
            decision_id=request.decision_id,
            trigger_id=request.trigger_id,
            symbol=request.symbol,
            side=request.side,
            order_type=OrderType.MARKET,
            requested_price=request.entry_price,
            requested_quantity=request.quantity,
            status=OrderStatus.FAILED,
            failure_reason=reason,
            created_at_ms=now,
        )
        return order_record, None

    def _build_result_from_response(
        self,
        client_order_id: str,
        request: ExecutionRequest,
        response: ExchangeResponse,
    ) -> Tuple[OrderRecord, Optional[FillRecord]]:
        """Build order result from exchange fetch response."""
        now = self._now_ms_fn()

        filled = response.status == "closed" and response.filled_quantity > 0
        status = OrderStatus.FILLED if filled else OrderStatus.FAILED

        slippage_pct = 0.0
        if response.avg_price > 0 and request.entry_price > 0:
            slippage_pct = abs(
                response.avg_price - request.entry_price
            ) / request.entry_price

        fee_usdt = response.fee
        if fee_usdt == 0 and response.filled_quantity > 0:
            fee_usdt = (
                response.avg_price
                * response.filled_quantity
                * self._taker_fee_rate
            )

        order_record = OrderRecord(
            order_id=_make_id(client_order_id, "order", now),
            request_id=request.request_id,
            decision_id=request.decision_id,
            trigger_id=request.trigger_id,
            symbol=request.symbol,
            side=request.side,
            order_type=OrderType.MARKET,
            requested_price=request.entry_price,
            requested_quantity=request.quantity,
            filled_price=response.avg_price,
            filled_quantity=response.filled_quantity,
            fee_usdt=fee_usdt,
            slippage_pct=slippage_pct,
            status=status,
            created_at_ms=now,
            filled_at_ms=now,
        )

        fill_record = None
        if filled:
            fill_record = FillRecord(
                fill_id=_make_id(client_order_id, "fill", now),
                order_id=order_record.order_id,
                symbol=request.symbol,
                side=request.side,
                price=response.avg_price,
                quantity=response.filled_quantity,
                fee_usdt=fee_usdt,
                fee_rate=self._taker_fee_rate,
                slippage_pct=slippage_pct,
                is_maker=False,
                filled_at_ms=now,
            )

        return order_record, fill_record

    # ── Symbol Formatting ─────────────────────────────────────

    @staticmethod
    def _format_symbol(symbol: str) -> str:
        """
        Convert internal symbol format to CCXT format.

        "BTCUSDT" → "BTC/USDT:USDT" (Bybit USDT-M perpetual)
        "BTC/USDT:USDT" → "BTC/USDT:USDT" (already formatted)
        """
        if "/" in symbol:
            return symbol  # Already CCXT format

        # Strip known quote currencies and format
        for quote in ("USDT", "BUSD", "USDC"):
            if symbol.endswith(quote):
                base = symbol[: -len(quote)]
                return f"{base}/{quote}:{quote}"

        # Fallback: return as-is
        return symbol

    # ── State Access ──────────────────────────────────────────

    def get_live_orders(self) -> Dict[str, LiveOrder]:
        """Get all in-flight orders (non-terminal)."""
        return {
            k: v for k, v in self._live_orders.items()
            if not v.is_terminal
        }

    def get_all_orders(self) -> Dict[str, LiveOrder]:
        """Get all tracked orders (including terminal)."""
        return dict(self._live_orders)

    def get_order(self, client_order_id: str) -> Optional[LiveOrder]:
        """Get a specific order by client_order_id."""
        return self._live_orders.get(client_order_id)

    def get_state(self) -> dict:
        """Get executor state for diagnostics."""
        live = sum(1 for o in self._live_orders.values() if not o.is_terminal)
        terminal = sum(1 for o in self._live_orders.values() if o.is_terminal)
        return {
            "total_orders": len(self._live_orders),
            "live_orders": live,
            "terminal_orders": terminal,
            "idempotency_entries": self._idempotency.entry_count,
        }
