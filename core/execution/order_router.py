# ============================================================
# NEXUS TRADER — Order Router
#
# Routes approved OrderCandidates to the appropriate executor:
#   "paper" mode → PaperExecutor  (simulated, no real money)
#   "live"  mode → LiveExecutor   (real CCXT orders)
#
# Phase A: live mode is now fully wired.
# Auto-execution gate: conditionally auto-submit to LiveExecutor
# without requiring UI confirmation.
# ============================================================
from __future__ import annotations

import logging
import uuid
from core.meta_decision.order_candidate import OrderCandidate
from core.event_bus import bus, Topics

logger = logging.getLogger(__name__)


class OrderRouter:
    """Routes approved order candidates to paper or live execution."""

    def __init__(self, mode: str = "paper"):
        self._mode = mode  # "paper" | "live"

        # Auto-execution configuration
        self._auto_exec_enabled: bool = False
        self._auto_exec_min_confidence: float = 0.72
        self._auto_exec_min_signal: float = 0.55
        self._auto_exec_regime_whitelist: list = ["TRENDING_UP", "TRENDING_DOWN", "RECOVERY"]

    def set_mode(self, mode: str) -> None:
        """Switch between 'paper' and 'live' execution modes."""
        if mode not in ("paper", "live"):
            raise ValueError(f"OrderRouter: invalid mode '{mode}' — must be 'paper' or 'live'")
        old = self._mode
        self._mode = mode
        if old != mode:
            logger.info(
                "OrderRouter: mode switched %s → %s", old.upper(), mode.upper()
            )
            bus.publish(
                Topics.MODE_CHANGED,
                {"old_mode": old, "new_mode": mode},
                source="order_router",
            )

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def active_executor(self):
        """
        Return the executor singleton for the current mode.
        Callers (scanner, risk page) use this to read positions/capital
        without coupling to a specific executor class.
        """
        if self._mode == "live":
            from core.execution.live_executor import live_executor
            return live_executor
        from core.execution.paper_executor import paper_executor
        return paper_executor

    def set_auto_execute(
        self,
        enabled: bool,
        min_confidence: float = 0.72,
        min_signal_strength: float = 0.55,
        regime_whitelist: list = None,
    ) -> None:
        """
        Configure auto-execution gate.

        Args:
            enabled: Enable auto-execution in live mode
            min_confidence: Minimum score (0–1) to auto-execute
            min_signal_strength: Minimum strength (0–1) to auto-execute
            regime_whitelist: List of allowed regimes. None or empty list = all regimes allowed
        """
        self._auto_exec_enabled = enabled
        self._auto_exec_min_confidence = min_confidence
        self._auto_exec_min_signal = min_signal_strength

        if regime_whitelist is None:
            self._auto_exec_regime_whitelist = ["TRENDING_UP", "TRENDING_DOWN", "RECOVERY"]
        else:
            self._auto_exec_regime_whitelist = regime_whitelist

        logger.info(
            "OrderRouter: auto-execute %s | confidence_threshold=%.2f | "
            "signal_strength_threshold=%.2f | regime_whitelist=%s",
            "ENABLED" if enabled else "DISABLED",
            min_confidence,
            min_signal_strength,
            self._auto_exec_regime_whitelist if self._auto_exec_regime_whitelist else "ALL",
        )

    def _check_auto_exec_gate(self, candidate: OrderCandidate) -> bool:
        """
        Check if candidate passes auto-execution gate.

        Returns True if:
          - candidate.score >= min_confidence AND
          - candidate.strength >= min_signal_strength AND
          - (regime_whitelist is empty OR candidate.regime in whitelist)
        """
        # Check confidence (score)
        if candidate.score < self._auto_exec_min_confidence:
            logger.debug(
                "OrderRouter: %s score %.2f < threshold %.2f — gating",
                candidate.symbol,
                candidate.score,
                self._auto_exec_min_confidence,
            )
            return False

        # Check signal strength (use score as strength proxy if no strength field)
        strength = getattr(candidate, "strength", candidate.score)
        if strength < self._auto_exec_min_signal:
            logger.debug(
                "OrderRouter: %s strength %.2f < threshold %.2f — gating",
                candidate.symbol,
                strength,
                self._auto_exec_min_signal,
            )
            return False

        # Check regime whitelist (empty list means all regimes allowed)
        if self._auto_exec_regime_whitelist:
            if candidate.regime not in self._auto_exec_regime_whitelist:
                logger.debug(
                    "OrderRouter: %s regime '%s' not in whitelist %s — gating",
                    candidate.symbol,
                    candidate.regime,
                    self._auto_exec_regime_whitelist,
                )
                return False

        logger.debug(
            "OrderRouter: %s passed auto-exec gate | score=%.2f strength=%.2f regime=%s",
            candidate.symbol,
            candidate.score,
            strength,
            candidate.regime,
        )
        return True

    def get_auto_exec_config(self) -> dict:
        """Return current auto-execution configuration."""
        return {
            "enabled": self._auto_exec_enabled,
            "min_confidence": self._auto_exec_min_confidence,
            "min_signal_strength": self._auto_exec_min_signal,
            "regime_whitelist": self._auto_exec_regime_whitelist,
        }

    def submit(self, candidate: OrderCandidate) -> bool:
        if not candidate.approved:
            logger.warning("OrderRouter: received non-approved candidate — ignoring")
            return False

        executor = self.active_executor

        # Assign unique candidate_id if not already set
        if not candidate.candidate_id:
            candidate.candidate_id = str(uuid.uuid4())

        if self._mode == "live":
            logger.info(
                "OrderRouter [LIVE]: submitting %s %s (score=%.2f)",
                candidate.side, candidate.symbol, candidate.score,
            )

            # Check auto-execution gate
            if self._auto_exec_enabled and self._check_auto_exec_gate(candidate):
                # Gate passed — auto-execute
                candidate.requires_confirmation = False
                success = executor.submit(candidate)
                if success:
                    bus.publish(
                        Topics.AUTO_EXECUTE_TRIGGERED,
                        data=candidate.to_dict(),
                        source="order_router",
                    )
                return success
            else:
                # Gate failed or auto-exec disabled — require UI confirmation
                candidate.requires_confirmation = True
                success = executor.submit(candidate)
                if success:
                    bus.publish(
                        Topics.SIGNAL_PENDING_CONFIRMATION,
                        data=candidate.to_dict(),
                        source="order_router",
                    )
                return success
        else:
            # Paper mode
            logger.debug(
                "OrderRouter [PAPER]: submitting %s %s",
                candidate.side, candidate.symbol,
            )
            success = executor.submit(candidate)
            if success:
                bus.publish(Topics.ORDER_PLACED, data=candidate.to_dict(), source="order_router")
            return success

    def submit_batch(self, candidates: list[OrderCandidate]) -> int:
        n = 0
        for c in candidates:
            if self.submit(c):
                n += 1
        return n


order_router = OrderRouter(mode="paper")


def get_router() -> OrderRouter:
    """Alias for module-level singleton — used by crash_defense_controller."""
    return order_router
