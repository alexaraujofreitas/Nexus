# ============================================================
# NEXUS TRADER — Risk Engine  (Phase 5)
#
# Multi-gate risk validation for execution intents.
# 10 ordered checks; short-circuit on first failure.
# Produces ExecutionDecision with detailed rejection tracing.
#
# Gates:
#   1. Circuit breaker TRIPPED
#   2. Daily loss limit
#   3. Drawdown limit
#   4. Max concurrent positions
#   5. Duplicate symbol+direction
#   6. Per-asset exposure cap
#   7. Portfolio heat (total risk)
#   8. Available capital
#   9. Risk:reward ratio floor
#   10. Minimum position size
#
# WARNING state: applies 0.5x risk scaling to final sizing.
#
# ZERO PySide6 imports.
# ============================================================
import logging
import time
from dataclasses import dataclass
from typing import Optional

from core.intraday.execution_contracts import (
    CircuitBreakerState,
    DecisionStatus,
    ExecutionDecision,
    ExecutionIntent,
    PortfolioSnapshot,
    RejectionSource,
    _make_id,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RiskEngineConfig:
    """Configuration for risk engine."""
    max_concurrent_positions: int = 5
    max_portfolio_heat_pct: float = 0.06      # 6% total risk
    max_asset_exposure_pct: float = 0.20      # 20% per asset
    max_daily_loss_pct: float = 0.03          # 3% daily loss
    max_drawdown_pct: float = 0.10            # 10% drawdown
    min_risk_reward: float = 1.0              # Minimum 1:1 R:R
    min_size_usdt: float = 10.0               # Minimum position size


class RiskEngine:
    """
    Multi-gate risk validation engine.

    Validates ExecutionIntent against portfolio state and circuit breaker,
    producing ExecutionDecision with decision_id, risk scaling, and
    detailed rejection metadata.
    """

    def __init__(self, config: RiskEngineConfig = None):
        """
        Initialize risk engine.

        Parameters
        ----------
        config : RiskEngineConfig, optional
            Configuration. Uses defaults if None.
        """
        self.config = config or RiskEngineConfig()
        logger.info(
            "RiskEngine initialized: max_positions=%d, max_heat=%.2f%%, "
            "max_exposure=%.2f%%, min_rr=%.1f",
            self.config.max_concurrent_positions,
            self.config.max_portfolio_heat_pct * 100,
            self.config.max_asset_exposure_pct * 100,
            self.config.min_risk_reward,
        )

    def validate(
        self,
        intent: ExecutionIntent,
        snapshot: PortfolioSnapshot,
        circuit_breaker,
    ) -> ExecutionDecision:
        """
        Validate execution intent and produce decision.

        Applies 10 ordered risk gates. Short-circuits on first failure.
        If circuit breaker is WARNING state, scales risk 0.5x in APPROVED decision.

        Parameters
        ----------
        intent : ExecutionIntent
            Pre-risk proposed trade
        snapshot : PortfolioSnapshot
            Current portfolio state
        circuit_breaker : CircuitBreaker
            Circuit breaker for loss protection

        Returns
        -------
        ExecutionDecision: APPROVED or REJECTED verdict

        Notes
        -----
        - All rejections include rejection_source and rejection_reason
        - decision_id is deterministic: _make_id(intent.intent_id, now_ms)
        - WARNING state: final_size_usdt and final_quantity are 0.5x scaled
        """
        total_capital = snapshot.capital.total_capital

        logger.debug(
            "RiskEngine.validate: intent_id=%s symbol=%s size=%.2f",
            intent.intent_id,
            intent.symbol,
            intent.size_usdt,
        )

        # ── Gate 1: Circuit breaker TRIPPED ──────────────────
        cb_state = circuit_breaker._state  # Current state
        if cb_state == CircuitBreakerState.TRIPPED:
            return self._make_rejection(
                intent,
                RejectionSource.CIRCUIT_BREAKER,
                "Circuit breaker is TRIPPED; execution halted",
            )

        # ── Gate 2: Daily loss limit ─────────────────────────
        max_daily_loss_usdt = self.config.max_daily_loss_pct * total_capital
        if snapshot.capital.realized_pnl_today < -max_daily_loss_usdt:
            return self._make_rejection(
                intent,
                RejectionSource.DAILY_LOSS,
                f"Daily loss {snapshot.capital.realized_pnl_today:.2f} exceeds "
                f"limit {-max_daily_loss_usdt:.2f}",
            )

        # ── Gate 3: Drawdown limit ───────────────────────────
        if snapshot.capital.drawdown_pct > self.config.max_drawdown_pct:
            return self._make_rejection(
                intent,
                RejectionSource.DRAWDOWN,
                f"Drawdown {snapshot.capital.drawdown_pct:.2%} exceeds "
                f"limit {self.config.max_drawdown_pct:.2%}",
            )

        # ── Gate 4: Max concurrent positions ──────────────────
        if snapshot.open_position_count >= self.config.max_concurrent_positions:
            return self._make_rejection(
                intent,
                RejectionSource.MAX_POSITIONS,
                f"Open positions {snapshot.open_position_count} >= "
                f"max {self.config.max_concurrent_positions}",
            )

        # ── Gate 5: Duplicate symbol+direction ───────────────
        for pos in snapshot.open_positions:
            if pos.symbol == intent.symbol and pos.direction == intent.direction:
                return self._make_rejection(
                    intent,
                    RejectionSource.DUPLICATE_SYMBOL,
                    f"Open {intent.direction.value} position exists for {intent.symbol}",
                )

        # ── Gate 6: Per-asset exposure cap ───────────────────
        current_exposure = snapshot.exposure.per_symbol.get(intent.symbol, 0.0)
        new_exposure = intent.size_usdt / total_capital
        total_exposure = current_exposure + new_exposure
        if total_exposure > self.config.max_asset_exposure_pct:
            return self._make_rejection(
                intent,
                RejectionSource.ASSET_EXPOSURE,
                f"Asset exposure {total_exposure:.4f} ({total_exposure*100:.2f}%) exceeds "
                f"limit {self.config.max_asset_exposure_pct:.4f} ({self.config.max_asset_exposure_pct*100:.2f}%)",
            )

        # ── Gate 7: Portfolio heat (total risk) ───────────────
        new_heat = intent.risk_usdt / total_capital
        total_heat = snapshot.exposure.portfolio_heat + new_heat
        if total_heat > self.config.max_portfolio_heat_pct:
            return self._make_rejection(
                intent,
                RejectionSource.PORTFOLIO_HEAT,
                f"Portfolio heat {total_heat:.4f} ({total_heat*100:.2f}%) exceeds "
                f"limit {self.config.max_portfolio_heat_pct:.4f} ({self.config.max_portfolio_heat_pct*100:.2f}%)",
            )

        # ── Gate 8: Available capital ────────────────────────
        if snapshot.capital.available_capital < intent.size_usdt:
            return self._make_rejection(
                intent,
                RejectionSource.INSUFFICIENT_CAPITAL,
                f"Available capital {snapshot.capital.available_capital:.2f} < "
                f"required {intent.size_usdt:.2f}",
            )

        # ── Gate 9: Risk:reward ratio ────────────────────────
        if intent.risk_reward_ratio < self.config.min_risk_reward:
            return self._make_rejection(
                intent,
                RejectionSource.RR_TOO_LOW,
                f"Risk:reward {intent.risk_reward_ratio:.3f} < "
                f"minimum {self.config.min_risk_reward:.1f}",
            )

        # ── Gate 10: Minimum position size ───────────────────
        if intent.size_usdt < self.config.min_size_usdt:
            return self._make_rejection(
                intent,
                RejectionSource.SIZE_TOO_SMALL,
                f"Position size {intent.size_usdt:.2f} < "
                f"minimum {self.config.min_size_usdt:.2f}",
            )

        # ── All gates passed: build APPROVED decision ────────
        final_size = intent.size_usdt
        final_quantity = intent.quantity
        risk_scaling = 1.0

        # Apply WARNING state risk scaling
        if cb_state == CircuitBreakerState.WARNING:
            risk_scaling = 0.5
            final_size = intent.size_usdt * risk_scaling
            final_quantity = intent.quantity * risk_scaling
            logger.info(
                "RiskEngine: WARNING state detected, applying 0.5x risk scaling: "
                "size %.2f → %.2f, quantity %.8f → %.8f",
                intent.size_usdt,
                final_size,
                intent.quantity,
                final_quantity,
            )

        decision = ExecutionDecision(
            decision_id=_make_id(intent.intent_id, int(time.time() * 1000)),
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
            final_size_usdt=final_size,
            final_quantity=final_quantity,
            risk_usdt=intent.risk_usdt,
            risk_reward_ratio=intent.risk_reward_ratio,
            regime=intent.regime,
            status=DecisionStatus.APPROVED,
            rejection_reason="",
            rejection_source="",
            risk_scaling_applied=risk_scaling,
            candle_trace_ids=intent.candle_trace_ids,
        )

        logger.debug(
            "RiskEngine: APPROVED decision_id=%s symbol=%s final_size=%.2f",
            decision.decision_id,
            decision.symbol,
            decision.final_size_usdt,
        )

        return decision

    def _make_rejection(
        self,
        intent: ExecutionIntent,
        source: RejectionSource,
        reason: str,
    ) -> ExecutionDecision:
        """
        Create a REJECTED ExecutionDecision.

        Parameters
        ----------
        intent : ExecutionIntent
            The rejected intent
        source : RejectionSource
            Rejection gate/reason enum
        reason : str
            Detailed human-readable reason

        Returns
        -------
        ExecutionDecision with status=REJECTED
        """
        decision = ExecutionDecision(
            decision_id=_make_id(intent.intent_id, int(time.time() * 1000)),
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
            final_size_usdt=0.0,
            final_quantity=0.0,
            risk_usdt=0.0,
            risk_reward_ratio=0.0,
            regime=intent.regime,
            status=DecisionStatus.REJECTED,
            rejection_reason=reason,
            rejection_source=source.value,
            risk_scaling_applied=1.0,
            candle_trace_ids=intent.candle_trace_ids,
        )

        logger.warning(
            "RiskEngine: REJECTED intent_id=%s source=%s reason=%s",
            intent.intent_id,
            source.value,
            reason,
        )

        return decision
