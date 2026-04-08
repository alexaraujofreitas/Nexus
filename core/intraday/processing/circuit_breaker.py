# ============================================================
# NEXUS TRADER — Circuit Breaker  (Phase 5)
#
# Stateful loss protection. Three states:
#   NORMAL → WARNING → TRIPPED → NORMAL (after cooldown)
#
# Transitions based on drawdown and daily loss thresholds.
# WARNING reduces risk scaling; TRIPPED halts execution.
# Cooldown timer resets TRIPPED to NORMAL after expiry.
#
# ZERO PySide6 imports.
# ============================================================
import logging
import time
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from core.intraday.execution_contracts import (
    CapitalSnapshot,
    CircuitBreakerState,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CircuitBreakerConfig:
    """Configuration for circuit breaker."""
    warning_drawdown_pct: float = 0.05       # 5% drawdown → WARNING
    max_drawdown_pct: float = 0.10           # 10% drawdown → TRIPPED
    warning_daily_loss_pct: float = 0.02     # 2% daily loss → WARNING
    max_daily_loss_pct: float = 0.03         # 3% daily loss → TRIPPED
    consecutive_loss_trip: int = 3           # 3 consecutive losses → TRIPPED
    cooldown_s: int = 1800                   # 30 minutes cooldown


class CircuitBreaker:
    """
    Stateful circuit breaker for loss protection.

    Tracks drawdown and daily loss; transitions between NORMAL, WARNING, and TRIPPED.
    WARNING state scales risk 0.5x; TRIPPED state halts execution.
    """

    def __init__(self, config: CircuitBreakerConfig = None):
        """
        Initialize circuit breaker.

        Parameters
        ----------
        config : CircuitBreakerConfig, optional
            Configuration. Uses defaults if None.
        """
        self.config = config or CircuitBreakerConfig()
        self._state = CircuitBreakerState.NORMAL
        self._tripped_at_ms: Optional[int] = None
        logger.info(
            "CircuitBreaker initialized: warning_dd=%.2f%%, max_dd=%.2f%%, "
            "warning_daily_loss=%.2f%%, max_daily_loss=%.2f%%, cooldown=%ds",
            self.config.warning_drawdown_pct * 100,
            self.config.max_drawdown_pct * 100,
            self.config.warning_daily_loss_pct * 100,
            self.config.max_daily_loss_pct * 100,
            self.config.cooldown_s,
        )

    def evaluate(self, capital: CapitalSnapshot, now_ms: Optional[int] = None) -> CircuitBreakerState:
        """
        Evaluate circuit breaker state based on capital snapshot.

        Updates internal state and applies transitions. TRIPPED state expires
        after cooldown_s seconds have elapsed since trip time.

        Parameters
        ----------
        capital : CapitalSnapshot
            Current capital state (drawdown_pct, realized_pnl_today, consecutive_losses)
        now_ms : int, optional
            Current time in ms (defaults to wall clock)

        Returns
        -------
        CircuitBreakerState: New or current state
        """
        if now_ms is None:
            now_ms = int(time.time() * 1000)

        logger.debug(
            "CircuitBreaker.evaluate: current_state=%s, drawdown=%.2f%%, "
            "daily_loss=%.2f, consecutive_losses=%d",
            self._state.value,
            capital.drawdown_pct * 100,
            capital.realized_pnl_today,
            capital.consecutive_losses,
        )

        # ── Check cooldown expiry ────────────────────────────
        if self._state == CircuitBreakerState.TRIPPED and self._tripped_at_ms is not None:
            cooldown_ms = self.config.cooldown_s * 1000
            elapsed_ms = now_ms - self._tripped_at_ms
            if elapsed_ms >= cooldown_ms:
                logger.info(
                    "CircuitBreaker: TRIPPED cooldown expired (%.1fs), resetting to NORMAL",
                    elapsed_ms / 1000,
                )
                self._state = CircuitBreakerState.NORMAL
                self._tripped_at_ms = None

        # ── TRIPPED state: check if any condition cleared ─────
        if self._state == CircuitBreakerState.TRIPPED:
            # TRIPPED only expires via cooldown, no condition clearing
            logger.debug("CircuitBreaker: TRIPPED state, awaiting cooldown")
            return self._state

        # ── Evaluate trip conditions ─────────────────────────
        # Check drawdown
        if capital.drawdown_pct > self.config.max_drawdown_pct:
            logger.warning(
                "CircuitBreaker: drawdown %.2f%% exceeds max %.2f%%, TRIPPING",
                capital.drawdown_pct * 100,
                self.config.max_drawdown_pct * 100,
            )
            self._state = CircuitBreakerState.TRIPPED
            self._tripped_at_ms = now_ms
            return self._state

        # Check daily loss
        max_daily_loss_usdt = self.config.max_daily_loss_pct * capital.total_capital
        if capital.realized_pnl_today < -max_daily_loss_usdt:
            logger.warning(
                "CircuitBreaker: daily_loss %.2f exceeds max %.2f, TRIPPING",
                capital.realized_pnl_today,
                -max_daily_loss_usdt,
            )
            self._state = CircuitBreakerState.TRIPPED
            self._tripped_at_ms = now_ms
            return self._state

        # Check consecutive losses
        if capital.consecutive_losses >= self.config.consecutive_loss_trip:
            logger.warning(
                "CircuitBreaker: consecutive_losses %d >= %d, TRIPPING",
                capital.consecutive_losses,
                self.config.consecutive_loss_trip,
            )
            self._state = CircuitBreakerState.TRIPPED
            self._tripped_at_ms = now_ms
            return self._state

        # ── Evaluate WARNING conditions ──────────────────────
        if self._state == CircuitBreakerState.NORMAL:
            # Check drawdown
            if capital.drawdown_pct > self.config.warning_drawdown_pct:
                logger.info(
                    "CircuitBreaker: drawdown %.2f%% exceeds warning %.2f%%, going to WARNING",
                    capital.drawdown_pct * 100,
                    self.config.warning_drawdown_pct * 100,
                )
                self._state = CircuitBreakerState.WARNING
                return self._state

            # Check daily loss
            warning_daily_loss_usdt = self.config.warning_daily_loss_pct * capital.total_capital
            if capital.realized_pnl_today < -warning_daily_loss_usdt:
                logger.info(
                    "CircuitBreaker: daily_loss %.2f exceeds warning %.2f, going to WARNING",
                    capital.realized_pnl_today,
                    -warning_daily_loss_usdt,
                )
                self._state = CircuitBreakerState.WARNING
                return self._state

        # ── WARNING state: check if conditions cleared ────────
        elif self._state == CircuitBreakerState.WARNING:
            # Return to NORMAL if all conditions clear
            conditions_met = (
                capital.drawdown_pct <= self.config.warning_drawdown_pct
                and capital.realized_pnl_today >= -(self.config.warning_daily_loss_pct * capital.total_capital)
            )
            if conditions_met:
                logger.info("CircuitBreaker: WARNING conditions cleared, returning to NORMAL")
                self._state = CircuitBreakerState.NORMAL
                self._tripped_at_ms = None
                return self._state

        logger.debug("CircuitBreaker: state remains %s", self._state.value)
        return self._state

    def is_tripped(self) -> bool:
        """Check if circuit is TRIPPED."""
        return self._state == CircuitBreakerState.TRIPPED

    def is_warning(self) -> bool:
        """Check if circuit is in WARNING state."""
        return self._state == CircuitBreakerState.WARNING

    def get_risk_scaling(self) -> float:
        """
        Get risk scaling factor based on current state.

        Returns
        -------
        float:
            1.0 for NORMAL (no scaling)
            0.5 for WARNING (50% risk scaling)
            0.0 for TRIPPED (no execution)
        """
        if self._state == CircuitBreakerState.NORMAL:
            return 1.0
        elif self._state == CircuitBreakerState.WARNING:
            return 0.5
        else:  # TRIPPED
            return 0.0

    def reset(self) -> None:
        """
        Force reset to NORMAL state.

        Used for daily reset or manual intervention.
        """
        logger.warning("CircuitBreaker: forced reset to NORMAL")
        self._state = CircuitBreakerState.NORMAL
        self._tripped_at_ms = None

    def get_status(self) -> dict:
        """
        Get current circuit breaker status.

        Returns
        -------
        dict with keys:
            - state: current CircuitBreakerState
            - risk_scaling: current risk scaling factor
            - tripped_at_ms: time when tripped (or None)
            - cooldown_remaining_s: seconds until auto-reset (or 0 if not tripped)
        """
        cooldown_remaining_s = 0.0
        if self._state == CircuitBreakerState.TRIPPED and self._tripped_at_ms is not None:
            elapsed_ms = int(time.time() * 1000) - self._tripped_at_ms
            cooldown_remaining_s = max(0.0, (self.config.cooldown_s * 1000 - elapsed_ms) / 1000)

        return {
            "state": self._state.value,
            "risk_scaling": self.get_risk_scaling(),
            "tripped_at_ms": self._tripped_at_ms,
            "cooldown_remaining_s": cooldown_remaining_s,
        }
