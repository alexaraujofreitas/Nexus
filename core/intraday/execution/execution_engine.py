# ============================================================
# NEXUS TRADER — Execution Engine (Phase 5)
#
# Orchestrates execution: decision → request → order → position.
# Enforces APPROVED status, validates, handles persistence.
#
# Production use: fully logged, contract-enforced, error-safe.
# ============================================================
import logging
import time
from typing import Optional

from core.intraday.execution_contracts import (
    DecisionStatus,
    ExecutionDecision,
    ExecutionRequest,
    ExecutionResult,
    Side,
    _make_id,
    validate_execution_request_strict,
)
from core.intraday.execution.intraday_executor import IntradayExecutor

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════
# EXECUTION ENGINE
# ══════════════════════════════════════════════════════════════


class ExecutionEngine:
    """
    Orchestrates execution: decision → request → order → position.

    Responsibilities:
    - Validate APPROVED status
    - Build ExecutionRequest from ExecutionDecision
    - Validate request
    - Execute via intraday_executor
    - Update portfolio state
    - Persist state (if manager provided)
    - Handle errors gracefully
    """

    def __init__(
        self,
        intraday_executor: IntradayExecutor,
        portfolio_state,
        persistence_manager=None,
        now_ms_fn=None,
    ):
        """
        Initialize execution engine.

        Args:
            intraday_executor: IntradayExecutor instance
            portfolio_state: PortfolioState instance
            persistence_manager: Optional persistence manager
            now_ms_fn: Optional function returning current time in ms
        """
        self.intraday_executor = intraday_executor
        self.portfolio_state = portfolio_state
        self.persistence_manager = persistence_manager
        self.now_ms_fn = now_ms_fn or (lambda: int(time.time() * 1000))
        logger.info(f"ExecutionEngine initialized")

    def execute(
        self, decision: ExecutionDecision, seed: int = None
    ) -> ExecutionResult:
        """
        Execute an approved decision.

        Flow:
        1. Validate decision is APPROVED
        2. Build ExecutionRequest from decision
        3. Validate request
        4. Execute via intraday_executor
        5. Open position in portfolio
        6. Persist snapshot (if manager available)
        7. Return ExecutionResult

        On any exception: return failure ExecutionResult

        Args:
            decision: ExecutionDecision (must be APPROVED)
            seed: Random seed for deterministic fill simulation

        Returns:
            ExecutionResult with success flag and details
        """
        now_ms = self.now_ms_fn()

        logger.debug(
            f"Processing execution decision: {decision.decision_id} | "
            f"status={decision.status.value}"
        )

        try:
            # Step 1: Validate APPROVED status
            if decision.status != DecisionStatus.APPROVED:
                reason = f"Decision is {decision.status.value}, not APPROVED"
                logger.critical(
                    f"Execution rejected: {reason} | decision_id={decision.decision_id}"
                )
                return ExecutionResult(
                    success=False,
                    failure_reason=reason,
                )

            logger.debug(f"Decision validated as APPROVED")

            # Step 2: Build ExecutionRequest
            request_id = _make_id(decision.decision_id, now_ms)
            side = Side.from_direction(decision.direction)

            request = ExecutionRequest(
                request_id=request_id,
                decision_id=decision.decision_id,
                trigger_id=decision.trigger_id,
                setup_id=decision.setup_id,
                symbol=decision.symbol,
                side=side,
                entry_price=decision.entry_price,
                stop_loss=decision.stop_loss,
                take_profit=decision.take_profit,
                size_usdt=decision.final_size_usdt,
                quantity=decision.final_quantity,
                strategy_name=decision.strategy_name,
                strategy_class=decision.strategy_class,
                regime=decision.regime,
                created_at_ms=now_ms,
                candle_trace_ids=decision.candle_trace_ids,
            )

            logger.debug(f"ExecutionRequest built: {request_id}")

            # Step 3: Validate request
            validate_execution_request_strict(request)
            logger.debug(f"Request validation passed")

            # Step 4: Execute
            order, fill = self.intraday_executor.execute(request, seed=seed)
            logger.info(f"Order executed: {order.order_id} | status={order.status.value}")

            # Step 5: Check fill result
            if fill is None:
                reason = f"Order failed: {order.failure_reason}"
                logger.error(f"Execution failed: {reason}")
                return ExecutionResult(
                    success=False,
                    order_id=order.order_id,
                    failure_reason=reason,
                )

            logger.debug(f"Fill successful: {fill.fill_id}")

            # Step 6: Open position in portfolio
            metadata = {
                "decision_id": decision.decision_id,
                "trigger_id": decision.trigger_id,
                "setup_id": decision.setup_id,
                "strategy_name": decision.strategy_name,
                "strategy_class": decision.strategy_class.value,
                "direction": decision.direction.value,
                "stop_loss": decision.stop_loss,
                "take_profit": decision.take_profit,
                "original_stop_loss": decision.stop_loss,
                "regime": decision.regime,
                "candle_trace_ids": list(decision.candle_trace_ids),
            }

            position = self.portfolio_state.open_position(fill, metadata)
            logger.info(
                f"Position opened: {position.position_id} | "
                f"{decision.symbol} {decision.direction.value} "
                f"{decision.final_quantity:.4f} @ {fill.price:.2f}"
            )

            # Step 7: Persist snapshot (optional)
            if self.persistence_manager:
                try:
                    self.persistence_manager.save_snapshot(
                        self.portfolio_state.get_snapshot()
                    )
                    logger.debug(f"Portfolio snapshot persisted")
                except Exception as e:
                    logger.error(
                        f"Failed to persist snapshot: {e}",
                        exc_info=True,
                    )
                    # Non-fatal: continue

            logger.info(
                f"Execution complete: decision={decision.decision_id}, "
                f"position={position.position_id}, "
                f"order={order.order_id}"
            )

            return ExecutionResult(
                success=True,
                position_id=position.position_id,
                order_id=order.order_id,
            )

        except Exception as e:
            logger.error(
                f"Execution engine error: {e}",
                exc_info=True,
            )
            return ExecutionResult(
                success=False,
                failure_reason=str(e),
            )

    def _feedback_learning(self, position) -> None:
        """
        Phase 5b slot: feedback learning (currently no-op).

        Args:
            position: PositionRecord to learn from
        """
        logger.debug(f"Feedback learning slot (Phase 5b): {position.position_id}")
        pass
