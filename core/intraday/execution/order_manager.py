# ============================================================
# NEXUS TRADER — Order Manager (Phase 5)
#
# Manages order lifecycle: creation, fill simulation, state transitions.
# Deterministic, fully logged, contract-validated.
#
# Single responsibility: OrderRecord creation and fill simulation.
# ============================================================
import logging
import time
from typing import Optional, Tuple

from core.intraday.execution_contracts import (
    ExecutionRequest,
    FillRecord,
    OrderRecord,
    OrderStatus,
    OrderType,
    _make_id,
)
from core.intraday.execution.fill_simulator import FillSimulator

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════
# ORDER MANAGER
# ══════════════════════════════════════════════════════════════


class OrderManager:
    """
    Manages order lifecycle: creation, simulation, state transitions.

    Responsibilities:
    - Create OrderRecord from ExecutionRequest
    - Delegate fill simulation to FillSimulator
    - Produce filled OrderRecord and FillRecord
    - Handle errors gracefully with failed OrderRecord
    """

    def __init__(
        self,
        fill_simulator: FillSimulator,
        now_ms_fn=None,
    ):
        """
        Initialize order manager.

        Args:
            fill_simulator: FillSimulator instance
            now_ms_fn: Optional function returning current time in ms (defaults to time.time()*1000)
        """
        self.fill_simulator = fill_simulator
        self.now_ms_fn = now_ms_fn or (lambda: int(time.time() * 1000))
        logger.info(f"OrderManager initialized with FillSimulator")

    def submit_order(
        self, request: ExecutionRequest, seed: int = None
    ) -> Tuple[OrderRecord, Optional[FillRecord]]:
        """
        Submit and process an order.

        Flow:
        1. Create PENDING OrderRecord
        2. Simulate fill via fill_simulator
        3. Update order to FILLED with fill results
        4. Return (filled_order, fill_record)

        On exception: return (failed_order, None)

        Args:
            request: ExecutionRequest with all order details
            seed: Random seed for deterministic fill simulation

        Returns:
            Tuple of (OrderRecord, Optional[FillRecord])
            - OrderRecord.status is either FILLED or FAILED
            - FillRecord is None if FAILED
        """
        now_ms = self.now_ms_fn()
        logger.debug(f"Submitting order for {request.symbol} {request.side.value}")

        try:
            # Step 1: Create PENDING order
            order_id = _make_id(request.request_id, now_ms)
            pending_order = OrderRecord(
                order_id=order_id,
                request_id=request.request_id,
                decision_id=request.decision_id,
                trigger_id=request.trigger_id,
                symbol=request.symbol,
                side=request.side,
                order_type=OrderType.MARKET,
                requested_price=request.entry_price,
                requested_quantity=request.quantity,
                status=OrderStatus.PENDING,
                created_at_ms=now_ms,
            )

            logger.info(
                f"PENDING order created: {order_id} | "
                f"{request.symbol} {request.side.value} "
                f"{request.quantity:.4f} @ {request.entry_price:.2f}"
            )

            # Step 2: Simulate fill
            fill_record = self.fill_simulator.simulate_fill(
                pending_order, now_ms=now_ms, seed=seed
            )

            logger.debug(f"Fill simulated: {fill_record.fill_id}")

            # Step 3: Create FILLED order
            filled_order = OrderRecord(
                order_id=order_id,
                request_id=request.request_id,
                decision_id=request.decision_id,
                trigger_id=request.trigger_id,
                symbol=request.symbol,
                side=request.side,
                order_type=OrderType.MARKET,
                requested_price=request.entry_price,
                requested_quantity=request.quantity,
                filled_price=fill_record.price,
                filled_quantity=fill_record.quantity,
                fee_usdt=fill_record.fee_usdt,
                slippage_pct=fill_record.slippage_pct,
                status=OrderStatus.FILLED,
                filled_at_ms=now_ms,
            )

            logger.info(
                f"FILLED order: {order_id} | "
                f"fill_price={fill_record.price:.2f}, "
                f"fee={fill_record.fee_usdt:.6f} USDT, "
                f"slippage={fill_record.slippage_pct*100:.4f}%"
            )

            return filled_order, fill_record

        except Exception as e:
            logger.error(
                f"Order submission failed for {request.symbol}: {e}",
                exc_info=True,
            )

            # Return FAILED order
            order_id = _make_id(request.request_id, now_ms)
            failed_order = OrderRecord(
                order_id=order_id,
                request_id=request.request_id,
                decision_id=request.decision_id,
                trigger_id=request.trigger_id,
                symbol=request.symbol,
                side=request.side,
                order_type=OrderType.MARKET,
                requested_price=request.entry_price,
                requested_quantity=request.quantity,
                status=OrderStatus.FAILED,
                failure_reason=str(e),
                created_at_ms=now_ms,
            )

            logger.warning(f"FAILED order: {order_id} | reason: {str(e)}")
            return failed_order, None
