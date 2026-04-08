# ============================================================
# NEXUS TRADER — Execution Gate (Phase 5)
#
# Bridge between signal pipeline and execution engine.
# Subscribes to TRIGGER_FIRED, processes via risk engine,
# executes approved decisions, publishes lifecycle events.
#
# Production use: fully logged, error-safe, event-driven.
# ============================================================
import logging
import time
from typing import Optional

from core.event_bus import Event, Topics, bus
from core.intraday.execution_contracts import (
    DecisionStatus,
    ExecutionDecision,
    _make_id,
)
from core.intraday.execution.execution_engine import ExecutionEngine
from core.intraday.signal_contracts import Direction, StrategyClass, TriggerLifecycle

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════
# EXECUTION GATE
# ══════════════════════════════════════════════════════════════


class ExecutionGate:
    """
    Event-driven execution gate.

    Subscribes to Topics.TRIGGER_FIRED, processes triggers through
    a risk/processing engine, executes approved decisions, and publishes
    execution lifecycle events.

    Responsibilities:
    - Subscribe/unsubscribe to TRIGGER_FIRED
    - Deserialize TriggerSignal from events
    - Process through risk engine
    - Execute approved decisions
    - Publish execution events (intent_created, decision_approved/rejected, etc.)
    - Handle errors gracefully
    """

    def __init__(
        self,
        processing_engine,
        execution_engine: ExecutionEngine,
        portfolio_state,
        event_bus=None,
        now_ms_fn=None,
    ):
        """
        Initialize execution gate.

        Args:
            processing_engine: ProcessingEngine instance (builds ExecutionIntent)
            execution_engine: ExecutionEngine instance
            portfolio_state: PortfolioState instance
            event_bus: Optional EventBus (defaults to global bus)
            now_ms_fn: Optional function returning current time in ms
        """
        self.processing_engine = processing_engine
        self.execution_engine = execution_engine
        self.portfolio_state = portfolio_state
        self.event_bus = event_bus or bus
        self.now_ms_fn = now_ms_fn or (lambda: int(time.time() * 1000))
        self._running = False
        logger.info(f"ExecutionGate initialized")

    def start(self) -> None:
        """Subscribe to TRIGGER_FIRED events."""
        if self._running:
            logger.warning(f"ExecutionGate already running")
            return

        self.event_bus.subscribe(Topics.TRIGGER_FIRED, self._on_trigger_fired)
        self._running = True
        logger.info(f"ExecutionGate started: subscribed to {Topics.TRIGGER_FIRED}")

    def stop(self) -> None:
        """Unsubscribe from TRIGGER_FIRED events."""
        if not self._running:
            logger.warning(f"ExecutionGate not running")
            return

        self.event_bus.unsubscribe(Topics.TRIGGER_FIRED, self._on_trigger_fired)
        self._running = False
        logger.info(f"ExecutionGate stopped: unsubscribed from {Topics.TRIGGER_FIRED}")

    def _on_trigger_fired(self, event: Event) -> None:
        """
        Handle TRIGGER_FIRED event.

        Flow:
        1. Deserialize TriggerSignal from event.data
        2. Get portfolio snapshot
        3. Get current_price
        4. Call processing_engine.process()
        5. Publish execution.intent_created event
        6. If REJECTED: publish execution.decision_rejected, return
        7. If APPROVED: publish execution.decision_approved
        8. Call execution_engine.execute()
        9. If success: publish execution.position_opened
        10. If fail: publish execution.order_failed

        Args:
            event: Event with TriggerSignal data
        """
        try:
            now_ms = self.now_ms_fn()
            logger.debug(f"TRIGGER_FIRED event received: {event.source}")

            # Step 1: Deserialize TriggerSignal
            trigger_data = event.data
            if not isinstance(trigger_data, dict):
                logger.error(
                    f"Invalid TRIGGER_FIRED event data: expected dict, "
                    f"got {type(trigger_data)}"
                )
                return

            try:
                trigger = self._deserialize_trigger_signal(trigger_data)
                logger.debug(f"TriggerSignal deserialized: {trigger.trigger_id}")
            except Exception as e:
                logger.error(f"Failed to deserialize TriggerSignal: {e}", exc_info=True)
                return

            # Step 2: Get portfolio snapshot
            snapshot = self.portfolio_state.get_snapshot()
            logger.debug(f"Portfolio snapshot acquired")

            # Step 3: Get current_price
            current_price = trigger_data.get("entry_price", trigger.entry_price)

            # Step 4: Process through risk engine
            logger.debug(f"Processing trigger through risk engine")
            intent = self.processing_engine.process(
                trigger, snapshot, current_price, now_ms
            )

            # Step 5: Publish intent_created event
            self.event_bus.publish(
                "execution.intent_created",
                data=intent.to_dict() if intent else None,
                source="execution_gate",
            )
            logger.debug(f"Event published: execution.intent_created")

            # Handle processing rejection
            if intent is None:
                logger.warning(f"Processing rejected trigger {trigger.trigger_id}")
                self.event_bus.publish(
                    "execution.processing_rejected",
                    data={
                        "trigger_id": trigger.trigger_id,
                        "reason": "Processing failed",
                    },
                    source="execution_gate",
                )
                return

            # Step 6: Risk engine decision
            # (In full implementation, risk_engine produces ExecutionDecision)
            # For now, assume processing_engine produces ExecutionIntent
            # and we build an APPROVED ExecutionDecision from it.
            decision = self._build_decision_from_intent(intent)

            if decision.status == DecisionStatus.REJECTED:
                # Step 6: Publish decision_rejected
                logger.info(
                    f"Execution rejected: {decision.rejection_reason} | "
                    f"source={decision.rejection_source}"
                )
                self.event_bus.publish(
                    "execution.decision_rejected",
                    data=decision.to_dict(),
                    source="execution_gate",
                )
                return

            # Step 7: Publish decision_approved
            logger.info(f"Execution approved: {decision.decision_id}")
            self.event_bus.publish(
                "execution.decision_approved",
                data=decision.to_dict(),
                source="execution_gate",
            )

            # Step 8: Execute
            logger.debug(f"Executing decision: {decision.decision_id}")
            result = self.execution_engine.execute(decision, seed=None)

            if result.success:
                # Step 9: Publish position_opened
                logger.info(
                    f"Position opened: {result.position_id} | "
                    f"order={result.order_id}"
                )
                self.event_bus.publish(
                    "execution.position_opened",
                    data={
                        "position_id": result.position_id,
                        "order_id": result.order_id,
                        "decision_id": decision.decision_id,
                        "trigger_id": decision.trigger_id,
                    },
                    source="execution_gate",
                )
            else:
                # Step 10: Publish order_failed
                logger.error(
                    f"Order failed: {result.failure_reason} | "
                    f"decision={decision.decision_id}"
                )
                self.event_bus.publish(
                    "execution.order_failed",
                    data={
                        "decision_id": decision.decision_id,
                        "trigger_id": decision.trigger_id,
                        "failure_reason": result.failure_reason,
                    },
                    source="execution_gate",
                )

        except Exception as e:
            logger.error(
                f"Execution gate error: {e}",
                exc_info=True,
            )

    def _deserialize_trigger_signal(self, data: dict):
        """
        Reconstruct TriggerSignal from dict.

        Args:
            data: Dictionary representation of TriggerSignal

        Returns:
            TriggerSignal instance

        Raises:
            ValueError: If reconstruction fails
        """
        from core.intraday.signal_contracts import TriggerSignal

        try:
            direction = Direction(data["direction"])
            strategy_class = StrategyClass(data["strategy_class"])
            lifecycle = TriggerLifecycle(data.get("lifecycle", "trigger_evaluated"))

            trigger = TriggerSignal(
                trigger_id=data["trigger_id"],
                setup_id=data["setup_id"],
                strategy_name=data["strategy_name"],
                strategy_class=strategy_class,
                symbol=data["symbol"],
                direction=direction,
                entry_price=data["entry_price"],
                stop_loss=data["stop_loss"],
                take_profit=data["take_profit"],
                atr_value=data["atr_value"],
                strength=data.get("strength", 0.5),
                trigger_quality=data.get("trigger_quality", 0.5),
                setup_timeframe=data.get("setup_timeframe", "15m"),
                trigger_timeframe=data.get("trigger_timeframe", "1m"),
                regime=data.get("regime", "UNKNOWN"),
                regime_confidence=data.get("regime_confidence", 0.5),
                trigger_candle_ts=data.get("trigger_candle_ts", int(time.time() * 1000)),
                setup_candle_ts=data.get("setup_candle_ts", int(time.time() * 1000)),
                candle_trace_ids=tuple(data.get("candle_trace_ids", [])),
                setup_trace_ids=tuple(data.get("setup_trace_ids", [])),
                lifecycle=lifecycle,
                rejection_reason=data.get("rejection_reason", ""),
                rationale=data.get("rationale", ""),
                created_at_ms=data.get("created_at_ms", int(time.time() * 1000)),
                max_age_ms=data.get("max_age_ms", 60000),
                drift_tolerance=data.get("drift_tolerance", 0.01),
            )
            return trigger
        except (KeyError, ValueError) as e:
            raise ValueError(f"Failed to deserialize TriggerSignal: {e}") from e

    def _build_decision_from_intent(self, intent) -> ExecutionDecision:
        """
        Build ExecutionDecision from ExecutionIntent.

        For Phase 5a, assume all intents are approved (risk engine integrated later).
        In full implementation, this delegates to risk_engine.

        Args:
            intent: ExecutionIntent from processing_engine

        Returns:
            ExecutionDecision (always APPROVED in Phase 5a)
        """
        now_ms = self.now_ms_fn()
        decision_id = _make_id(intent.intent_id, now_ms)

        decision = ExecutionDecision(
            decision_id=decision_id,
            intent_id=intent.intent_id,
            trigger_id=intent.trigger_id,
            setup_id=intent.setup_id,
            symbol=intent.symbol,
            direction=intent.direction,
            strategy_name=intent.strategy_name,
            strategy_class=intent.strategy_class,
            entry_price=intent.entry_price,
            stop_loss=intent.stop_loss,
            take_profit=intent.take_profit,
            final_size_usdt=intent.size_usdt,
            final_quantity=intent.quantity,
            risk_usdt=intent.risk_usdt,
            risk_reward_ratio=intent.risk_reward_ratio,
            regime=intent.regime,
            status=DecisionStatus.APPROVED,
            created_at_ms=now_ms,
            candle_trace_ids=intent.candle_trace_ids,
        )

        return decision
