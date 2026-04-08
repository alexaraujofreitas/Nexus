# ============================================================
# NEXUS TRADER — Periodic Reconciliation Scheduler (v2 Safety)
#
# Background scheduler that runs OrderReconciliationEngine every
# 30–60 seconds. FAIL-CLOSED: any mismatch → degraded mode.
#
# Integration: instantiated in main.py after exchange connects
# and startup recovery completes.
# ============================================================
from __future__ import annotations

import logging
import threading
from typing import Optional

from PySide6.QtCore import QObject, QTimer, Slot

from core.event_bus import bus, Topics

logger = logging.getLogger(__name__)

DEFAULT_INTERVAL_MS = 45_000
MISMATCH_PAUSE_THRESHOLD = 1  # v2: ANY mismatch blocks trading


class ReconciliationScheduler(QObject):
    """
    Periodic reconciliation. FAIL-CLOSED on mismatch.
    LiveBridge.run_reconciliation() handles entering degraded mode.
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
                    # LiveBridge.run_reconciliation() already enters degraded mode
                    logger.error(
                        "ReconciliationScheduler: %d mismatches detected — "
                        "LiveBridge entered degraded mode",
                        mismatch_count,
                    )
            else:
                self._consecutive_failures += 1
                errors = result.get("errors", [])
                logger.warning(
                    "ReconciliationScheduler: cycle failed (%d consecutive) — %s",
                    self._consecutive_failures, errors,
                )
                if self._consecutive_failures >= 3:
                    logger.error(
                        "ReconciliationScheduler: %d consecutive failures — "
                        "entering degraded mode (exchange connectivity compromised)",
                        self._consecutive_failures,
                    )
                    if hasattr(self._bridge, '_enter_degraded_mode'):
                        self._bridge._enter_degraded_mode(
                            f"reconciliation_failed_{self._consecutive_failures}_consecutive"
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
