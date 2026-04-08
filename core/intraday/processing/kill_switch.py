# ============================================================
# NEXUS TRADER — Kill Switch  (Phase 5)
#
# Manual emergency stop mechanism. Persists state to disk
# so it survives system restarts.
#
# States: ARMED (default, execution enabled) or
#         DISARMED (manual halt, all execution rejected)
#
# Persisted to JSON; loaded on init. Audit trail in logs.
#
# ZERO PySide6 imports.
# ============================================================
import json
import logging
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Optional

from core.intraday.execution_contracts import KillSwitchState

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class KillSwitchConfig:
    """Configuration for kill switch."""
    persistence_path: str = "data/intraday_kill_switch.json"


class KillSwitch:
    """
    Manual emergency stop mechanism with disk persistence.

    ARMED = execution enabled (default)
    DISARMED = all execution halted (manual intervention required)

    State is persisted to JSON on every change and loaded on init.
    """

    def __init__(self, config: KillSwitchConfig = None):
        """
        Initialize kill switch and load persisted state.

        If persistence file exists and indicates DISARMED, starts DISARMED.
        Otherwise defaults to ARMED.

        Parameters
        ----------
        config : KillSwitchConfig, optional
            Configuration. Uses defaults if None.
        """
        self.config = config or KillSwitchConfig()
        self._state = KillSwitchState.ARMED
        self._disarmed_at_ms: Optional[int] = None
        self._disarm_reason: str = ""

        # Load persisted state
        self._load_persisted_state()

        logger.info(
            "KillSwitch initialized: state=%s, persistence_path=%s",
            self._state.value,
            self.config.persistence_path,
        )

    def _load_persisted_state(self) -> None:
        """Load kill switch state from persistence file if it exists."""
        try:
            persistence_path = Path(self.config.persistence_path)
            if persistence_path.exists():
                with open(persistence_path, "r") as f:
                    data = json.load(f)
                    state_str = data.get("state", "armed")
                    self._state = (
                        KillSwitchState.DISARMED
                        if state_str == "disarmed"
                        else KillSwitchState.ARMED
                    )
                    self._disarmed_at_ms = data.get("disarmed_at_ms")
                    self._disarm_reason = data.get("disarm_reason", "")
                    logger.info(
                        "KillSwitch: loaded persisted state=%s (reason: %s)",
                        self._state.value,
                        self._disarm_reason or "(none)",
                    )
            else:
                logger.debug("KillSwitch: no persistence file found, starting ARMED")
        except Exception as e:
            logger.error("KillSwitch: failed to load persisted state: %s, defaulting to ARMED", e)
            self._state = KillSwitchState.ARMED
            self._disarmed_at_ms = None
            self._disarm_reason = ""

    def _save_persisted_state(self) -> None:
        """Save kill switch state to persistence file."""
        try:
            persistence_path = Path(self.config.persistence_path)
            persistence_path.parent.mkdir(parents=True, exist_ok=True)

            data = {
                "state": self._state.value,
                "disarmed_at_ms": self._disarmed_at_ms,
                "disarm_reason": self._disarm_reason,
                "saved_at_ms": int(time.time() * 1000),
            }

            with open(persistence_path, "w") as f:
                json.dump(data, f, indent=2)

            logger.debug("KillSwitch: persisted state to %s", persistence_path)
        except Exception as e:
            logger.error("KillSwitch: failed to persist state: %s", e)

    def is_halted(self) -> bool:
        """
        Check if execution is halted.

        Returns
        -------
        bool: True if DISARMED (halted), False if ARMED (executing)
        """
        return self._state == KillSwitchState.DISARMED

    def disarm(self, reason: str, now_ms: Optional[int] = None) -> None:
        """
        Disarm kill switch (halt execution).

        Records reason and timestamp. Persists state to disk.

        Parameters
        ----------
        reason : str
            Human-readable reason for disarming (e.g., "Manual intervention")
        now_ms : int, optional
            Current time in ms (defaults to wall clock)
        """
        if now_ms is None:
            now_ms = int(time.time() * 1000)

        self._state = KillSwitchState.DISARMED
        self._disarmed_at_ms = now_ms
        self._disarm_reason = reason

        logger.warning("KillSwitch: DISARMED (reason: %s)", reason)
        self._save_persisted_state()

    def arm(self) -> None:
        """
        Arm kill switch (resume execution).

        Requires explicit call. Persists state to disk.
        """
        self._state = KillSwitchState.ARMED
        self._disarmed_at_ms = None
        self._disarm_reason = ""

        logger.warning("KillSwitch: ARMED (execution resumed)")
        self._save_persisted_state()

    def get_status(self) -> dict:
        """
        Get current kill switch status.

        Returns
        -------
        dict with keys:
            - state: "armed" or "disarmed"
            - is_halted: bool
            - disarmed_at_ms: time when disarmed (or None)
            - disarm_reason: reason string (or empty)
            - uptime_since_disarm_s: seconds since disarm (or None)
        """
        uptime_since_disarm = None
        if self._disarmed_at_ms is not None:
            uptime_since_disarm = (int(time.time() * 1000) - self._disarmed_at_ms) / 1000

        return {
            "state": self._state.value,
            "is_halted": self.is_halted(),
            "disarmed_at_ms": self._disarmed_at_ms,
            "disarm_reason": self._disarm_reason,
            "uptime_since_disarm_s": uptime_since_disarm,
        }
