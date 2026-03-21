# ============================================================
# NEXUS TRADER — Crash Defense Controller  (Sprint 13)
#
# Implements 4-tier graduated defensive response to crash signals.
#
# Tier 1 — DEFENSIVE   (score ≥ 5.0):
#   - Halt new long entries
#   - Tighten stop losses to 1.5x ATR (from 2x)
#   - Reduce position size cap to 15% of capital (from 25%)
#   - Notify: "Defensive mode activated"
#
# Tier 2 — HIGH_ALERT  (score ≥ 7.0):
#   - Tier 1 actions +
#   - Close 50% of each long position (partial exit)
#   - Enable trailing stops at 1% from price
#   - Publish RISK_LIMIT_HIT event
#   - Notify: "High alert — partial position reduction"
#
# Tier 3 — EMERGENCY   (score ≥ 8.0):
#   - Tier 2 actions +
#   - Close remaining long positions (full long book exit)
#   - Switch to read-only mode (no new trades)
#   - Notify: "Emergency — all longs closed"
#
# Tier 4 — SYSTEMIC    (score ≥ 9.0):
#   - Tier 3 actions +
#   - Close ALL positions including shorts
#   - Activate safe mode (emergency stop equivalent)
#   - Publish EMERGENCY_STOP event
#   - Notify: "Systemic crisis — all positions closed"
# ============================================================
from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from typing import Optional

from core.event_bus import bus, Topics

logger = logging.getLogger(__name__)


class CrashDefenseController:
    """
    Executes graduated defensive actions in response to crash tier escalations.
    Stateful: tracks current defensive tier to avoid duplicate actions.
    """

    def __init__(self):
        self._lock = threading.RLock()
        self._current_tier: str = "NORMAL"
        self._actions_log: list[dict] = []
        self._defensive_mode_active: bool = False
        self._safe_mode_active: bool = False

    @property
    def current_tier(self) -> str:
        with self._lock:
            return self._current_tier

    @property
    def is_defensive(self) -> bool:
        with self._lock:
            return self._defensive_mode_active

    @property
    def is_safe_mode(self) -> bool:
        with self._lock:
            return self._safe_mode_active

    def respond_to_tier(self, tier: str, score: float, components: dict) -> list[str]:
        """
        Execute defensive actions appropriate for the given tier.
        Returns list of actions taken (for logging/notification).
        Thread-safe.
        """
        with self._lock:
            actions_taken: list[str] = []

            if tier == "NORMAL":
                if self._defensive_mode_active:
                    self._deactivate_defensive_mode(actions_taken)
                return actions_taken

            # Apply appropriate tier actions
            if tier in ("DEFENSIVE", "HIGH_ALERT", "EMERGENCY", "SYSTEMIC"):
                self._apply_defensive_tier1(actions_taken)

            if tier in ("HIGH_ALERT", "EMERGENCY", "SYSTEMIC"):
                self._apply_defensive_tier2(actions_taken)

            if tier in ("EMERGENCY", "SYSTEMIC"):
                self._apply_defensive_tier3(actions_taken)

            if tier == "SYSTEMIC":
                self._apply_defensive_tier4(actions_taken)

            self._current_tier = tier
            self._defensive_mode_active = True
            if tier == "SYSTEMIC":
                self._safe_mode_active = True   # diagnostic flag only — no auto-execution changes

            # Log actions
            log_entry = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "tier": tier,
                "score": score,
                "actions": actions_taken,
            }
            self._actions_log.append(log_entry)
            if len(self._actions_log) > 100:
                self._actions_log = self._actions_log[-100:]

            # Publish defensive mode activation
            bus.publish(Topics.DEFENSIVE_MODE_ACTIVATED, {
                "tier":         tier,
                "score":        score,
                "actions":      actions_taken,
                "timestamp":    datetime.now(timezone.utc).isoformat(),
                "components":   components,
            }, source="crash_defense")

            # Send notification
            self._notify_defensive_action(tier, score, actions_taken)

            logger.warning(
                "CrashDefenseController [%s]: score=%.2f | actions=%d",
                tier, score, len(actions_taken),
            )

            return actions_taken

    def _apply_defensive_tier1(self, actions: list[str]) -> None:
        """
        Tier 1: MONITOR-ONLY — log crash alert, do NOT auto-modify execution.

        Production hardening decision (Study 4 + Session 24):
        Automatic execution intervention based on unvalidated crash scores caused
        false positives during normal market volatility. The 10% drawdown circuit
        breaker in PaperExecutor.submit() is the ONLY automatic execution block.
        CrashDefense tiers are now diagnostic/notification only.
        """
        logger.warning(
            "CrashDefense TIER-1 DEFENSIVE: crash score elevated. "
            "Monitoring only — execution unchanged. "
            "Review System Health > Crash Status for details."
        )
        bus.publish(Topics.DRAWDOWN_ALERT, {
            "type":    "crash_detection",
            "tier":    "DEFENSIVE",
            "message": "Crash score DEFENSIVE tier — monitor open positions",
        }, source="crash_defense")
        actions.append("MONITOR: crash score reached DEFENSIVE tier — no auto-intervention")

    def _apply_defensive_tier2(self, actions: list[str]) -> None:
        """Tier 2: HIGH_ALERT — monitor only. No automatic position changes."""
        logger.warning(
            "CrashDefense TIER-2 HIGH_ALERT: crash score high. "
            "Monitoring only — review positions manually."
        )
        bus.publish(Topics.RISK_LIMIT_HIT, {
            "type":    "crash_detection",
            "tier":    "HIGH_ALERT",
            "message": "Crash score HIGH_ALERT tier — review positions manually",
        }, source="crash_defense")
        actions.append("MONITOR: crash score reached HIGH_ALERT tier — manual review recommended")

    def _apply_defensive_tier3(self, actions: list[str]) -> None:
        """Tier 3: EMERGENCY — monitor only. No automatic position closure."""
        logger.error(
            "CrashDefense TIER-3 EMERGENCY: crash score critical. "
            "Monitoring only — manual intervention required."
        )
        bus.publish(Topics.RISK_LIMIT_HIT, {
            "type":    "crash_detection",
            "tier":    "EMERGENCY",
            "message": "Crash score EMERGENCY tier — MANUAL intervention required",
            "severity": "critical",
        }, source="crash_defense")
        actions.append("ALERT: crash score reached EMERGENCY tier — manual action required")

    def _apply_defensive_tier4(self, actions: list[str]) -> None:
        """Tier 4: SYSTEMIC — monitor only. Log critical event, notify channels."""
        logger.critical(
            "CrashDefense TIER-4 SYSTEMIC: systemic crash score. "
            "This is a monitoring alert only — no auto-trading changes. "
            "Manually evaluate all open positions immediately."
        )
        bus.publish(Topics.EMERGENCY_STOP, {
            "reason":   "SYSTEMIC crash detection alert — manual evaluation required",
            "source":   "crash_defense_controller",
            "tier":     "SYSTEMIC",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }, source="crash_defense")
        actions.append("CRITICAL ALERT: crash score reached SYSTEMIC tier — evaluate immediately")

    def _deactivate_defensive_mode(self, actions: list[str]) -> None:
        """Deactivate defensive mode when crash score returns to NORMAL."""
        self._defensive_mode_active = False
        self._safe_mode_active = False
        self._current_tier = "NORMAL"
        actions.append("defensive_mode: deactivated — market conditions normalized")
        bus.publish(Topics.SYSTEM_ALERT, {
            "title":   "Defensive Mode Deactivated",
            "message": "Crash score returned to NORMAL. Defensive constraints lifted.",
        }, source="crash_defense")
        logger.info("CrashDefenseController: defensive mode deactivated")

    def _notify_defensive_action(self, tier: str, score: float, actions: list[str]) -> None:
        """Send crash alert notification through NotificationManager."""
        try:
            from core.notifications.notification_manager import notification_manager as nm

            template_map = {
                "DEFENSIVE":  "crash_defensive",
                "HIGH_ALERT": "crash_high_alert",
                "EMERGENCY":  "crash_emergency",
                "SYSTEMIC":   "crash_systemic",
            }
            template = template_map.get(tier, "crash_defensive")

            nm.notify(template, {
                "tier":        tier,
                "score":       score,
                "actions":     "\n  ".join(actions),
                "timestamp":   datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
            })
        except Exception as exc:
            logger.debug("CrashDefenseController: notification failed — %s", exc)

    def get_actions_log(self) -> list[dict]:
        """Return recent defensive actions log."""
        with self._lock:
            return list(self._actions_log)


# ── Module-level singleton ────────────────────────────────────
_controller: Optional[CrashDefenseController] = None


def get_crash_defense_controller() -> CrashDefenseController:
    global _controller
    if _controller is None:
        _controller = CrashDefenseController()
    return _controller
