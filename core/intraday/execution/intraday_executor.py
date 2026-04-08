# ============================================================
# NEXUS TRADER — Intraday Executor (Phase 5)
#
# Validates execution requests and delegates to OrderManager.
# Single responsibility: request validation + delegation.
#
# Production use: fully logged, contract-enforced.
# ============================================================
import logging
from typing import Optional, Tuple

from core.intraday.execution_contracts import (
    ExecutionRequest,
    FillRecord,
    OrderRecord,
    validate_execution_request_strict,
)
from core.intraday.execution.order_manager import OrderManager

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════
# INTRADAY EXECUTOR
# ══════════════════════════════════════════════════════════════


class IntradayExecutor:
    """
    Validates and executes market orders.

    Responsibilities:
    - Validate ExecutionRequest against contracts
    - Delegate to OrderManager for execution
    - Log execution results
    """

    def __init__(self, order_manager: OrderManager):
        """
        Initialize intraday executor.

        Args:
            order_manager: OrderManager instance
        """
        self.order_manager = order_manager
        logger.info(f"IntradayExecutor initialized")

    def execute(
        self, request: ExecutionRequest, seed: int = None
    ) -> Tuple[OrderRecord, Optional[FillRecord]]:
        """
        Execute a market order.

        Flow:
        1. Validate request via validate_execution_request_strict
        2. Delegate to order_manager.submit_order()
        3. Log result
        4. Return (order, fill)

        Args:
            request: ExecutionRequest (frozen, validated)
            seed: Random seed for deterministic fill simulation

        Returns:
            Tuple of (OrderRecord, Optional[FillRecord])

        Raises:
            ContractViolation: If request validation fails
        """
        logger.debug(f"Executing order for {request.symbol} {request.side.value}")

        # Validate request
        validate_execution_request_strict(request)
        logger.debug(f"Request validation passed: {request.request_id}")

        # Delegate to order manager
        order, fill = self.order_manager.submit_order(request, seed=seed)

        logger.info(
            f"Execution result: {order.status.value} | "
            f"request_id={request.request_id}, "
            f"order_id={order.order_id}"
        )

        return order, fill
