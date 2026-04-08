# ============================================================
# NEXUS TRADER — Periodic Reconciliation Scheduler (F-06)
#
# Background scheduler that runs OrderReconciliationEngine every
# 30–60 seconds to detect and resolve state drift between
# internal tracking and actual exchange state.
#
# Integration: instantiated in main.py after exchange connects
# and startup recovery completes. Uses QTimer for Qt-safe
# periodic execution.
# ============================================================
from __future__ import annotations

import logging
import threading
from typing import Optional

from PySide6.QtCore import QObject, QTimer, Slot

from core.event_bus import bus, Topics

logger = logging.getLogger(__name__)

# Default interval: 45 seconds (between 30-60s requirement)
DEFAULT_INTERVAL_MS = 45_000
# If mismatches exceed this threshold, pause trading
MISMATCH_PAUSE_THRESHOLD = 2


class ReconciliationScheduler(QObject):
    """
    Periodic background reconciliation between internal state
    and exchange state. Uses LiveBridge.run_reconciliation().
    """

    def __init__(
        self,
        live_bridge,
        interval_ms: int = DEFAULT_INTERVAL_MS,
        mismatch_pause_threshold: int = MISMATCH_PAUSE_THRESHOLD,
        parent=None,
    ):
        super().__init__(parent)
        self._bridge = live_bridge
        self._interval_ms = interval_ms
        self._mismatch_pause_threshold = mismatch_pause_threshold
        self._timer: Optional[QTimer] = None
        self._running = False
        self._consecutive_failures = 0
        self._total_runs = 0
        self._total_mismatches = 0

    def start(self) -> None:
        """Start the periodic reconciliation loop."""
        if self._running:
            return
        self._timer = QTimer(self)
        self._timer.setInterval(self._interval_ms)
        self._timer.timeout.connect(self._on_tick)
        self._timer.start()
        self._running = True
        logger.info(
            "ReconciliationScheduler: started — interval=%dms threshold=%d",
            self._interval_ms, self._mismatch_pause_threshold,
        )

    def stop(self) -> None:
        """Stop the periodic reconciliation loop."""
        if self._timer:
            self._timer.stop()
        self._running = False
        logger.info("ReconciliationScheduler: stopped")

    @Slot()
    def _on_tick(self) -> None:
        """Run one reconciliation cycle."""
        if not self._bridge.is_initialised:
            return

        self._total_runs += 1
        try:
            result = self._bridge.run_reconciliation()

            if result.get("success", False):
                self._consecutive_failures = 0
                mismatch_count = result.get("mismatch_count", 0)
                if mismatch_count > 0:
                    self._total_mismatches += mismatch_count
                    if mismatch_count >= self._mismatch_pause_threshold:
                        logger.error(
                            "ReconciliationScheduler: %d mismatches >= threshold %d — "
                            "consider pausing trading",
                            mismatch_count, self._mismatch_pause_threshold,
                        )
                        bus.publish(Topics.SYSTEM_ALERT, {
                            "type": "reconciliation_threshold_exceeded",
                            "mismatch_count": mismatch_count,
                            "threshold": self._mismatch_pause_threshold,
                            "message": (
                                f"Reconciliation found {mismatch_count} mismatches "
                                f"(threshold={self._mismatch_pause_threshold})"
                            ),
                            "severity": "critical",
                        }, source="reconciliation_scheduler")
            else:
                self._consecutive_failures += 1
                errors = result.get("errors", [])
                logger.warning(
                    "ReconciliationScheduler: cycle failed (%d consecutive) — %s",
                    self._consecutive_failures, errors,
                )
                # After 5 consecutive failures, log critical
                if self._consecutive_failures >= 5:
                    logger.error(
                        "ReconciliationScheduler: %d consecutive failures — "
                        "exchange connectivity may be compromised",
                        self._consecutive_failures,
                    )
        except Exception as exc:
            self._consecutive_failures += 1
            logger.error("ReconciliationScheduler: exception in cycle: %s", exc)

    def get_state(self) -> dict:
        return {
            "running": self._running,
            "interval_ms": self._interval_ms,
            "total_runs": self._total_runs,
            "total_mismatches": self._total_mismatches,
            "consecutive_failures": self._consecutive_failures,
        }
