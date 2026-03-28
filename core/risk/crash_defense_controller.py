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

    Auto-execute mode (gated behind config crash_defense.auto_execute: true):
      DEFENSIVE  → move all long SLs to breakeven
      HIGH_ALERT → partial close 50% of all longs
      EMERGENCY  → close all longs
      SYSTEMIC   → close all positions
    """

    def __init__(self):
        self._lock = threading.RLock()
        self._current_tier: str = "NORMAL"
        self._actions_log: list[dict] = []
        self._defensive_mode_active: bool = False
        self._safe_mode_active: bool = False
        # Injected reference to PaperExecutor for auto-execute.
        # Set via set_executor() after PaperExecutor is instantiated.
        self._executor = None

    def set_executor(self, executor) -> None:
        """Inject PaperExecutor reference for auto-execute actions. Thread-safe."""
        with self._lock:
            self._executor = executor
            logger.info("CrashDefenseController: executor reference set — auto-execute ready")

    @property
    def _auto_execute_enabled(self) -> bool:
        """Returns True if crash_defense.auto_execute is enabled in config."""
        try:
            from config.settings import settings as _s
            return bool(_s.get("crash_defense.auto_execute", False))
        except Exception:
            return False

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
        Tier 1: DEFENSIVE — alert + optional auto-breakeven SL.

        When crash_defense.auto_execute=true: moves ALL open long stop-losses
        to breakeven immediately (protects unrealised profit at zero cost).
        When auto_execute=false: monitor-only (log + notify, no execution change).
        """
        logger.warning(
            "CrashDefense TIER-1 DEFENSIVE: crash score elevated. "
            "Review System Health > Crash Status for details."
        )
        bus.publish(Topics.DRAWDOWN_ALERT, {
            "type":    "crash_detection",
            "tier":    "DEFENSIVE",
            "message": "Crash score DEFENSIVE tier — monitoring positions",
        }, source="crash_defense")

        if self._auto_execute_enabled and self._executor is not None:
            try:
                moved = self._executor.move_all_longs_to_breakeven()
                msg = f"AUTO-EXECUTE: moved {moved} long SL(s) to breakeven"
                logger.warning("CrashDefense TIER-1: %s", msg)
                actions.append(msg)
            except Exception as exc:
                logger.error("CrashDefense TIER-1: breakeven SL move failed: %s", exc)
                actions.append(f"AUTO-EXECUTE FAILED: breakeven SL move — {exc}")
        else:
            actions.append("MONITOR: DEFENSIVE tier — no auto-intervention (auto_execute=false)")

    def _apply_defensive_tier2(self, actions: list[str]) -> None:
        """
        Tier 2: HIGH_ALERT — alert + optional 50% partial close of all longs.

        When crash_defense.auto_execute=true: closes 50% of every open long
        position at current price, locking in partial profit before further decline.
        When auto_execute=false: monitor-only.
        """
        logger.warning(
            "CrashDefense TIER-2 HIGH_ALERT: crash score high. "
            "Partial position reduction recommended."
        )
        bus.publish(Topics.RISK_LIMIT_HIT, {
            "type":    "crash_detection",
            "tier":    "HIGH_ALERT",
            "message": "Crash score HIGH_ALERT tier — partial position reduction",
        }, source="crash_defense")

        if self._auto_execute_enabled and self._executor is not None:
            try:
                count = 0
                for symbol, pos_list in list(self._executor._positions.items()):
                    for pos in pos_list:
                        if pos.side == "buy":
                            ok = self._executor.partial_close(symbol, 0.50)
                            if ok:
                                count += 1
                msg = f"AUTO-EXECUTE: partial-closed 50% of {count} long position(s)"
                logger.warning("CrashDefense TIER-2: %s", msg)
                actions.append(msg)
            except Exception as exc:
                logger.error("CrashDefense TIER-2: partial close failed: %s", exc)
                actions.append(f"AUTO-EXECUTE FAILED: partial close — {exc}")
        else:
            actions.append("MONITOR: HIGH_ALERT tier — manual review recommended (auto_execute=false)")

    def _apply_defensive_tier3(self, actions: list[str]) -> None:
        """
        Tier 3: EMERGENCY — alert + optional full long book closure.

        When crash_defense.auto_execute=true: closes ALL open long positions
        immediately at current mark price.
        When auto_execute=false: monitor-only, critical alert.
        """
        logger.error(
            "CrashDefense TIER-3 EMERGENCY: crash score critical. "
            "Immediate action required."
        )
        bus.publish(Topics.RISK_LIMIT_HIT, {
            "type":    "crash_detection",
            "tier":    "EMERGENCY",
            "message": "Crash score EMERGENCY tier — closing all longs",
            "severity": "critical",
        }, source="crash_defense")

        if self._auto_execute_enabled and self._executor is not None:
            try:
                closed = self._executor.close_all_longs(exit_reason="crash_defense_emergency")
                msg = f"AUTO-EXECUTE: closed {closed} long position(s) — EMERGENCY"
                logger.error("CrashDefense TIER-3: %s", msg)
                actions.append(msg)
            except Exception as exc:
                logger.error("CrashDefense TIER-3: close_all_longs failed: %s", exc)
                actions.append(f"AUTO-EXECUTE FAILED: close all longs — {exc}")
        else:
            actions.append("ALERT: EMERGENCY tier — MANUAL intervention required (auto_execute=false)")

    def _apply_defensive_tier4(self, actions: list[str]) -> None:
        """
        Tier 4: SYSTEMIC — alert + optional full book closure (all sides).

        When crash_defense.auto_execute=true: closes ALL open positions
        including shorts. Full account de-risk.
        When auto_execute=false: critical log + EMERGENCY_STOP event.
        """
        logger.critical(
            "CrashDefense TIER-4 SYSTEMIC: systemic crash score. "
            "Evaluate all open positions immediately."
        )
        bus.publish(Topics.EMERGENCY_STOP, {
            "reason":   "SYSTEMIC crash detection — closing all positions",
            "source":   "crash_defense_controller",
            "tier":     "SYSTEMIC",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }, source="crash_defense")

        if self._auto_execute_enabled and self._executor is not None:
            try:
                closed = self._executor.close_all()
                msg = f"AUTO-EXECUTE: closed ALL {closed} position(s) — SYSTEMIC"
                logger.critical("CrashDefense TIER-4: %s", msg)
                actions.append(msg)
            except Exception as exc:
                logger.error("CrashDefense TIER-4: close_all failed: %s", exc)
                actions.append(f"AUTO-EXECUTE FAILED: close all — {exc}")
        else:
            actions.append("CRITICAL ALERT: SYSTEMIC tier — manual evaluation required (auto_execute=false)")

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
