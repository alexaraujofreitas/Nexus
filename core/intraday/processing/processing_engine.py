# ============================================================
# NEXUS TRADER — Processing Engine  (Phase 5B Wave 1 v3 + Wave 2)
#
# End-to-end intraday signal → execution decision pipeline.
#
# CONCURRENCY MODEL: Single-threaded event loop (headless-first).
# ProcessingEngine.process() is called sequentially from the
# OrchestratorEngine scan loop. No locking required. No PySide6
# dependency. GUI observes results via read-only properties —
# it NEVER participates in the decision path.
#
# DATA FLOW (v3 + Wave 2 v2.1):
#   Signal → [1:kill] → [2:expiry] → [3:duplicate]
#          → [4:TQS] → [5:Filter]
#          → [W2a: FailureMode → REJECT if SUSPENDED, else capture multiplier]
#          → [W2b: EdgeValidity → REJECT if SUSPENDED, else capture multiplier]
#          → [6:PositionSizer.calculate() → base size_usdt, quantity, risk_usdt]
#          → [6b:Apply wave2_exposure_mult to base sizing outputs]
#          → [7:min_size check on adjusted values]
#          → [8:Concentration amplifies adjusted base]
#          → [9:intent from post-concentration values]
#          → [10:validate intent] → [11:RiskEngine validates FINAL values]
#          → [12:hash]
#
# EXACT WAVE 2 EXPOSURE APPLICATION POINT: Step 6b.
#   wave2_exposure_mult = fm_multiplier × ev_multiplier  (both default 1.0)
#   Computed at Steps W2a/W2b. Applied at Step 6b:
#     size_usdt  = sizer.size_usdt  × wave2_exposure_mult
#     quantity   = sizer.quantity    × wave2_exposure_mult
#     risk_usdt  = sizer.risk_usdt  × wave2_exposure_mult
#   Concentration (Step 8) then amplifies these ADJUSTED values.
#   RiskEngine (Step 11) validates the FINAL post-concentration values.
#
# PROOF: RiskEngine sees final values because ExecutionIntent (Step 9)
# is built from post-concentration size_usdt/quantity/risk_usdt, and
# RiskEngine.validate() receives that intent directly.
#
# Wave 2 modules NEVER bypass RiskEngine or 4%/6% hard caps.
# They can only REDUCE exposure below sizer output, never increase.
#
# REJECTION PRECEDENCE (early exits):
#   kill_switch > stale_signal > duplicate > tqs_floor > global_filter
#   > failure_mode_suspended > edge_validity_suspended
#   > size_too_small > validation > risk_engine
#
# MODULE COMPATIBILITY MATRIX (validated at construction):
#   Wave 1: same as v3
#   Wave 2: failure_mode and edge_validity are fully independent
#           (no dependencies on each other or Wave 1 modules)
#
# ZERO PySide6 imports.
# ============================================================
import logging
import time
from typing import Callable, Optional, Tuple

from core.intraday.execution_contracts import (
    DecisionStatus,
    ExecutionDecision,
    ExecutionIntent,
    PortfolioSnapshot,
    RejectionSource,
    _make_id,
    validate_execution_intent_strict,
)
from core.intraday.signal_contracts import TriggerSignal, validate_trigger_signal_strict
from core.intraday.signal_expiry import validate_signal_expiry
from core.intraday.pipeline_context import PipelineContext, EMPTY_CONTEXT

logger = logging.getLogger(__name__)


# ── Phase 5B rejection source constants ─────────────────────
# String values (not enum members) to avoid modifying frozen contract.
REJECTION_TQS_FLOOR = "tqs_floor"
REJECTION_GLOBAL_FILTER = "global_filter"
REJECTION_FAILURE_MODE = "failure_mode_suspended"
REJECTION_EDGE_VALIDITY = "edge_validity_suspended"


class ProcessingEngine:
    """
    Single-threaded processing pipeline: TriggerSignal → ExecutionDecision.

    Concurrency guarantee: process() is only called from the
    OrchestratorEngine's sequential scan loop. No locking needed.
    GUI is observer-only — it reads last_pipeline_context and
    last_decision_hash but never participates in the decision path.

    Phase 5B modules (Wave 1 + Wave 2) are injected optionally.
    When absent, their pipeline steps are skipped entirely.

    Module compatibility validated at construction time.
    """

    def __init__(
        self,
        position_sizer,
        risk_engine,
        circuit_breaker,
        kill_switch,
        now_ms_fn: Optional[Callable[[], int]] = None,
        # Phase 5B Wave 1 optional injections
        tqs_scorer=None,
        global_filter=None,
        concentration_engine=None,
        # Phase 5B Wave 2 optional injections
        failure_mode_protection=None,
        edge_validity_monitor=None,
    ):
        # ── Wave 1 module compatibility validation ───────────
        if concentration_engine is not None and tqs_scorer is None:
            raise ValueError(
                "Invalid module combination: concentration_engine requires "
                "tqs_scorer. Concentration depends on explicit TQS score — "
                "it cannot operate without TQS. Valid combinations:\n"
                "  - TQS only\n"
                "  - Filter only\n"
                "  - TQS + Filter\n"
                "  - TQS + Concentration\n"
                "  - TQS + Filter + Concentration (full pipeline)\n"
                "Invalid:\n"
                "  - Concentration only\n"
                "  - Filter + Concentration (without TQS)"
            )

        self.position_sizer = position_sizer
        self.risk_engine = risk_engine
        self.circuit_breaker = circuit_breaker
        self.kill_switch = kill_switch
        self.now_ms_fn = now_ms_fn or (lambda: int(time.time() * 1000))

        # Wave 1: Optional[T] = None means module is absent
        self._tqs_scorer = tqs_scorer
        self._global_filter = global_filter
        self._concentration_engine = concentration_engine

        # Wave 2: Optional[T] = None means module is absent
        self._failure_mode = failure_mode_protection
        self._edge_validity = edge_validity_monitor

        # Last pipeline result — immutable, replaced each process() call
        self._last_context: PipelineContext = EMPTY_CONTEXT
        self._last_decision_hash: str = ""

        phase5b_status = []
        if tqs_scorer:
            phase5b_status.append("TQS")
        if global_filter:
            phase5b_status.append("GlobalFilter")
        if concentration_engine:
            phase5b_status.append("Concentration")
        if failure_mode_protection:
            phase5b_status.append("FailureMode")
        if edge_validity_monitor:
            phase5b_status.append("EdgeValidity")

        logger.info(
            "ProcessingEngine initialized%s",
            f" [Phase 5B: {', '.join(phase5b_status)}]" if phase5b_status else "",
        )

    def process(
        self,
        trigger: TriggerSignal,
        snapshot: PortfolioSnapshot,
        current_price: float,
        now_ms: Optional[int] = None,
    ) -> ExecutionDecision:
        """
        Process a trigger signal into an execution decision.

        Pipeline order (v3 + Wave 2 v2.1):
          1. kill → 2. expiry → 3. duplicate
          4. TQS → 5. Filter
          W2a. FailureMode → reject if SUSPENDED; capture multiplier
          W2b. EdgeValidity → reject if SUSPENDED/probe-limit; capture multiplier
          6.  PositionSizer.calculate() → base sizing
          6b. Apply wave2_exposure_mult to base sizing
          7.  Min size check (on adjusted values)
          8.  Concentration (amplifies adjusted base)
          9.  Build intent (from post-concentration values)
          10. Validate intent → 11. RiskEngine (validates FINAL) → 12. Hash

        Returns ExecutionDecision with status APPROVED or REJECTED.
        """
        if now_ms is None:
            now_ms = self.now_ms_fn()

        # Start with empty immutable context
        ctx = EMPTY_CONTEXT

        # Wave 2 exposure multiplier (stacks multiplicatively)
        wave2_exposure_mult = 1.0

        logger.debug(
            "ProcessingEngine.process: trigger_id=%s symbol=%s direction=%s",
            trigger.trigger_id, trigger.symbol, trigger.direction.value,
        )

        # ── Validate trigger signal schema ───────────────────
        try:
            validate_trigger_signal_strict(trigger)
        except Exception as e:
            logger.error("ProcessingEngine: trigger validation failed: %s", e)
            decision = self._make_rejection_decision(
                trigger, RejectionSource.STALE_SIGNAL,
                f"Signal validation failed: {e}", now_ms,
            )
            self._finalise(ctx, decision)
            return decision

        # ── Step 1: Kill switch ──────────────────────────────
        if self.kill_switch.is_halted():
            decision = self._make_rejection_decision(
                trigger, RejectionSource.KILL_SWITCH,
                "Kill switch is DISARMED; execution halted", now_ms,
            )
            self._finalise(ctx, decision)
            return decision

        # ── Step 2: Signal expiry ────────────────────────────
        expiry_result = validate_signal_expiry(trigger, current_price, now_ms)
        if not expiry_result.is_valid:
            decision = self._make_rejection_decision(
                trigger, RejectionSource.STALE_SIGNAL,
                expiry_result.detail, now_ms,
            )
            self._finalise(ctx, decision)
            return decision

        # ── Step 3: Duplicate symbol+direction ───────────────
        for pos in snapshot.open_positions:
            if pos.symbol == trigger.symbol and pos.direction == trigger.direction:
                decision = self._make_rejection_decision(
                    trigger, RejectionSource.DUPLICATE_SYMBOL,
                    f"Open {trigger.direction.value} position exists for {trigger.symbol}",
                    now_ms,
                )
                self._finalise(ctx, decision)
                return decision

        # ── Step 4: TQS evaluation (Wave 1) ─────────────────
        if self._tqs_scorer is not None:
            tqs_result = self._tqs_scorer.evaluate(trigger, snapshot)
            ctx = ctx.with_tqs(tqs_result)

            if not tqs_result.passed:
                logger.info(
                    "ProcessingEngine: TQS rejection score=%.3f symbol=%s: %s",
                    tqs_result.score, trigger.symbol, tqs_result.reason,
                )
                decision = self._make_rejection_decision_ext(
                    trigger, REJECTION_TQS_FLOOR, tqs_result.reason, now_ms,
                )
                self._finalise(ctx, decision)
                return decision

        # ── Step 5: Global filter (Wave 1) ───────────────────
        if self._global_filter is not None:
            tqs_score_for_filter = None
            if self._tqs_scorer is not None:
                if ctx.tqs is None:
                    raise RuntimeError(
                        "BUG: TQS scorer is present but ctx.tqs is None "
                        "at filter step. This should be unreachable."
                    )
                tqs_score_for_filter = ctx.tqs.score

            filter_result = self._global_filter.evaluate(
                strategy_class=trigger.strategy_class.value,
                symbol=trigger.symbol,
                regime=trigger.regime,
                tqs_score=tqs_score_for_filter,
                now_ms=now_ms,
            )
            filter_state = self._global_filter.state_snapshot()
            ctx = ctx.with_filter(filter_result, filter_state)

            if not filter_result.passed:
                logger.info(
                    "ProcessingEngine: GlobalFilter rejection gate=%s symbol=%s: %s",
                    filter_result.gate, trigger.symbol, filter_result.reason,
                )
                decision = self._make_rejection_decision_ext(
                    trigger, REJECTION_GLOBAL_FILTER,
                    f"[{filter_result.gate}] {filter_result.reason}", now_ms,
                )
                self._finalise(ctx, decision)
                return decision

        # ── Wave 2a: Failure Mode Protection ─────────────────
        if self._failure_mode is not None:
            fm_result = self._failure_mode.evaluate(now_ms)
            ctx = ctx.with_failure_mode(fm_result)

            if not fm_result.passed:
                logger.info(
                    "ProcessingEngine: FailureMode SUSPENDED symbol=%s: %s",
                    trigger.symbol, fm_result.reason,
                )
                decision = self._make_rejection_decision_ext(
                    trigger, REJECTION_FAILURE_MODE, fm_result.reason, now_ms,
                )
                self._finalise(ctx, decision)
                return decision

            # Apply exposure multiplier (stacks with edge validity)
            if fm_result.exposure_multiplier < 1.0:
                wave2_exposure_mult *= fm_result.exposure_multiplier
                logger.debug(
                    "ProcessingEngine: FailureMode exposure=%.2f (%s)",
                    fm_result.exposure_multiplier, fm_result.severity,
                )

        # ── Wave 2b: Edge Validity Monitor ───────────────────
        if self._edge_validity is not None:
            ev_result = self._edge_validity.evaluate(
                trigger.strategy_class.value, now_ms,
            )
            ctx = ctx.with_edge_validity(ev_result)

            if not ev_result.passed:
                logger.info(
                    "ProcessingEngine: EdgeValidity %s for %s: %s",
                    ev_result.state, trigger.strategy_class.value, ev_result.reason,
                )
                decision = self._make_rejection_decision_ext(
                    trigger, REJECTION_EDGE_VALIDITY, ev_result.reason, now_ms,
                )
                self._finalise(ctx, decision)
                return decision

            # Apply exposure multiplier (stacks with failure mode)
            if ev_result.exposure_multiplier < 1.0:
                wave2_exposure_mult *= ev_result.exposure_multiplier
                logger.debug(
                    "ProcessingEngine: EdgeValidity exposure=%.2f (%s for %s)",
                    ev_result.exposure_multiplier, ev_result.state,
                    trigger.strategy_class.value,
                )

        # ── Step 6: Size position ────────────────────────────
        sizing_result = self.position_sizer.calculate(
            trigger.entry_price,
            trigger.stop_loss,
            snapshot.capital.available_capital,
            snapshot.capital.total_capital,
        )
        size_usdt = sizing_result["size_usdt"]
        quantity = sizing_result["quantity"]
        risk_usdt = sizing_result["risk_usdt"]

        # ── Step 6b: Apply Wave 2 exposure multiplier to base sizing ──
        # EXACT APPLICATION POINT: after PositionSizer, before min_size check.
        # Formulas: size_usdt *= mult, quantity *= mult, risk_usdt *= mult.
        # Concentration (Step 8) amplifies these adjusted values.
        # RiskEngine (Step 11) validates the final post-concentration result.
        if wave2_exposure_mult < 1.0:
            size_usdt *= wave2_exposure_mult
            quantity *= wave2_exposure_mult
            risk_usdt *= wave2_exposure_mult
            logger.debug(
                "ProcessingEngine: Wave 2 exposure adjustment %.2fx applied to sizing",
                wave2_exposure_mult,
            )

        # ── Step 7: Minimum size check ───────────────────────
        if size_usdt == 0:
            decision = self._make_rejection_decision(
                trigger, RejectionSource.SIZE_TOO_SMALL,
                "Position sizer rejected: size below minimum", now_ms,
            )
            self._finalise(ctx, decision)
            return decision

        # ── Step 8: Capital concentration (Wave 1) ───────────
        if self._concentration_engine is not None:
            if ctx.tqs is None:
                raise RuntimeError(
                    "BUG: Concentration engine is present but TQS result is "
                    "missing. Concentration depends on explicit TQS score. "
                    "Ensure tqs_scorer is injected when concentration_engine is."
                )

            conc_result = self._concentration_engine.calculate(
                tqs_score=ctx.tqs.score,
                asset_score=ctx.tqs.execution_context,
                execution_score=ctx.tqs.execution_context,
                base_size_usdt=size_usdt,
                base_quantity=quantity,
                total_capital=snapshot.capital.total_capital,
                entry_price=trigger.entry_price,
            )
            ctx = ctx.with_concentration(conc_result)

            size_usdt = conc_result.adjusted_size_usdt
            quantity = conc_result.adjusted_quantity
            risk_usdt = risk_usdt * conc_result.multiplier

            logger.debug(
                "ProcessingEngine: concentration applied %.3fx: "
                "size=%.2f→%.2f capped=%s",
                conc_result.multiplier,
                conc_result.adjusted_size_usdt / max(conc_result.multiplier, 0.001),
                conc_result.adjusted_size_usdt,
                conc_result.capped,
            )

        # ── Step 9: Build ExecutionIntent (post-concentration) ──
        intent = ExecutionIntent(
            intent_id=_make_id(trigger.trigger_id, now_ms),
            trigger_id=trigger.trigger_id,
            setup_id=trigger.setup_id,
            symbol=trigger.symbol,
            direction=trigger.direction,
            strategy_name=trigger.strategy_name,
            strategy_class=trigger.strategy_class,
            entry_price=trigger.entry_price,
            stop_loss=trigger.stop_loss,
            take_profit=trigger.take_profit,
            atr_value=trigger.atr_value,
            size_usdt=size_usdt,
            quantity=quantity,
            risk_usdt=risk_usdt,
            risk_reward_ratio=trigger.risk_reward_ratio,
            regime=trigger.regime,
            regime_confidence=trigger.regime_confidence,
            trigger_strength=trigger.strength,
            trigger_quality=trigger.trigger_quality,
            created_at_ms=now_ms,
            candle_trace_ids=trigger.candle_trace_ids,
            setup_trace_ids=trigger.setup_trace_ids,
        )

        # ── Step 10: Validate intent contract ────────────────
        try:
            validate_execution_intent_strict(intent)
        except Exception as e:
            logger.error("ProcessingEngine: intent validation failed: %s", e)
            decision = self._make_rejection_decision(
                trigger, RejectionSource.SIZE_TOO_SMALL,
                f"Intent validation failed: {e}", now_ms,
            )
            self._finalise(ctx, decision)
            return decision

        # ── Step 11: Risk engine ─────────────────────────────
        decision = self.risk_engine.validate(intent, snapshot, self.circuit_breaker)

        # ── Step 12: Finalise ────────────────────────────────
        self._finalise(ctx, decision)
        return decision

    # ── Finalisation (hash + store context) ──────────────────

    def _finalise(self, ctx: PipelineContext, decision: ExecutionDecision) -> None:
        """Store pipeline context and compute decision hash."""
        self._last_context = ctx
        self._last_decision_hash = ctx.decision_hash(decision.to_dict())

    # ── Properties ───────────────────────────────────────────

    @property
    def last_pipeline_context(self) -> PipelineContext:
        """Immutable PipelineContext from the most recent process() call."""
        return self._last_context

    @property
    def last_decision_hash(self) -> str:
        """SHA-256 hash of the full decision path from the last process() call."""
        return self._last_decision_hash

    # ── Rejection helpers ────────────────────────────────────

    def _make_rejection_decision(
        self, trigger: TriggerSignal, source: RejectionSource,
        reason: str, now_ms: int,
    ) -> ExecutionDecision:
        return self._make_rejection_decision_ext(
            trigger, source.value, reason, now_ms,
        )

    def _make_rejection_decision_ext(
        self, trigger: TriggerSignal, source_value: str,
        reason: str, now_ms: int,
    ) -> ExecutionDecision:
        decision = ExecutionDecision(
            decision_id=_make_id(trigger.trigger_id, now_ms),
            intent_id=_make_id(trigger.trigger_id, now_ms),
            trigger_id=trigger.trigger_id,
            setup_id=trigger.setup_id,
            symbol=trigger.symbol,
            direction=trigger.direction,
            strategy_name=trigger.strategy_name,
            strategy_class=trigger.strategy_class,
            entry_price=trigger.entry_price,
            stop_loss=trigger.stop_loss,
            take_profit=trigger.take_profit,
            final_size_usdt=0.0,
            final_quantity=0.0,
            risk_usdt=0.0,
            risk_reward_ratio=trigger.risk_reward_ratio,
            regime=trigger.regime,
            status=DecisionStatus.REJECTED,
            rejection_reason=reason,
            rejection_source=source_value,
            risk_scaling_applied=1.0,
            created_at_ms=now_ms,
            candle_trace_ids=trigger.candle_trace_ids,
        )
        logger.debug(
            "ProcessingEngine: rejection source=%s reason=%s",
            source_value, reason,
        )
        return decision
