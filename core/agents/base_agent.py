# ============================================================
# NEXUS TRADER — Base Agent
#
# Abstract base class for all intelligence agents.
# Handles:
#   • QThread lifecycle (start / stop / error recovery)
#   • Staleness tracking with confidence decay
#   • Standardised signal publication to EventBus
#   • Settings hot-apply via SETTINGS_CHANGED
#   • Exponential backoff on consecutive errors
# ============================================================
from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any, Optional

from PySide6.QtCore import QThread, Signal

from core.event_bus import bus, Topics

logger = logging.getLogger(__name__)

# Maximum consecutive errors before the agent pauses for a long interval
_MAX_CONSECUTIVE_ERRORS = 5
# Base backoff in seconds — doubles on each consecutive error, capped at 5 minutes
_BASE_BACKOFF_S = 30
_MAX_BACKOFF_S  = 300


class BaseAgent(QThread):
    """
    Abstract base for all NexusTrader intelligence agents.

    Subclasses must implement:
        poll_interval_seconds() → int
        fetch()                 → dict  (raw data from source)
        process(raw)            → dict  (normalised signal dict)
        event_topic             → str   (Topics.XXXX constant)

    The run loop calls fetch() → process() → publish() on every interval.
    Errors are caught, logged, and cause backoff but never crash the thread.
    """

    # Qt signals for UI integration
    signal_ready = Signal(dict)   # emitted after every successful process()
    agent_error  = Signal(str)    # emitted on fetch/process failure

    def __init__(self, name: str, parent=None):
        super().__init__(parent)
        self._name            = name
        self._stop_requested  = False
        self._consecutive_errors = 0
        self._last_signal: Optional[dict] = None
        self._last_updated: Optional[datetime] = None
        bus.subscribe(Topics.SETTINGS_CHANGED, self._on_settings_changed)

    # ── Abstract interface ────────────────────────────────────

    @property
    @abstractmethod
    def event_topic(self) -> str:
        """EventBus topic to publish signals on."""
        ...

    @property
    @abstractmethod
    def poll_interval_seconds(self) -> int:
        """How often to poll the data source (seconds)."""
        ...

    @property
    def max_staleness_seconds(self) -> int:
        """After this many seconds without a successful fetch, mark stale."""
        return self.poll_interval_seconds * 3

    @abstractmethod
    def fetch(self) -> Any:
        """
        Fetch raw data from the external source.
        May raise any exception — the run loop will catch and back off.
        """
        ...

    @abstractmethod
    def process(self, raw: Any) -> dict:
        """
        Convert raw data into a normalised signal dict.
        Must include at minimum: {signal, confidence, source, updated_at, stale}
        """
        ...

    def on_settings_changed(self) -> None:
        """Override to react to settings changes (e.g. update API keys)."""
        pass

    # ── Run loop ──────────────────────────────────────────────

    def run(self) -> None:
        logger.info("Agent [%s] started", self._name)
        bus.publish(Topics.AGENT_STARTED, {"agent": self._name}, source=self._name)

        while not self._stop_requested:
            try:
                raw    = self.fetch()
                result = self.process(raw)

                # Inject mandatory fields
                result["source"]     = self._name
                result["updated_at"] = datetime.now(timezone.utc).isoformat()
                result["stale"]      = False

                self._last_signal     = result
                self._last_updated    = datetime.now(timezone.utc)
                self._consecutive_errors = 0

                bus.publish(self.event_topic, result, source=self._name)
                self.signal_ready.emit(result)

                # Persist to DB (non-blocking, best-effort)
                self._persist_signal(result)

                logger.debug(
                    "Agent [%s]: signal=%.3f  confidence=%.2f",
                    self._name,
                    result.get("signal", 0),
                    result.get("confidence", 0),
                )

            except Exception as exc:
                # "No exchange connected" is a normal startup condition — the
                # exchange handshake happens asynchronously and the agents start
                # before it completes.  Suppress the WARNING spam and just wait
                # quietly until the connection is established.
                if "No exchange connected" in str(exc):
                    logger.debug(
                        "Agent [%s]: exchange not yet connected — retrying in %ds",
                        self._name, _BASE_BACKOFF_S,
                    )
                    self._interruptible_sleep(_BASE_BACKOFF_S)
                    continue

                self._consecutive_errors += 1
                backoff = min(
                    _BASE_BACKOFF_S * (2 ** min(self._consecutive_errors - 1, 4)),
                    _MAX_BACKOFF_S,
                )
                logger.warning(
                    "Agent [%s] error #%d: %s — backing off %ds",
                    self._name, self._consecutive_errors, exc, backoff,
                )
                self.agent_error.emit(str(exc))

                # Publish stale/zero signal so downstream knows data is unavailable
                stale_signal = self._stale_signal()
                bus.publish(self.event_topic, stale_signal, source=self._name)

                self._interruptible_sleep(backoff)
                continue

            self._interruptible_sleep(self.poll_interval_seconds)

        logger.info("Agent [%s] stopped", self._name)
        bus.publish(Topics.AGENT_STOPPED, {"agent": self._name}, source=self._name)

    def stop(self) -> None:
        """Request the agent to stop after its current sleep."""
        self._stop_requested = True
        self.quit()

    # ── Helpers ───────────────────────────────────────────────

    def _interruptible_sleep(self, seconds: int) -> None:
        """Sleep in 0.5s increments so stop() takes effect quickly."""
        deadline = time.monotonic() + seconds
        while not self._stop_requested and time.monotonic() < deadline:
            time.sleep(0.5)

    def _stale_signal(self) -> dict:
        """Return a zero-confidence stale signal to preserve the schema."""
        return {
            "signal":     0.0,
            "confidence": 0.0,
            "source":     self._name,
            "updated_at": (self._last_updated or datetime.now(timezone.utc)).isoformat(),
            "stale":      True,
            "error":      f"consecutive_errors={self._consecutive_errors}",
        }

    @property
    def is_stale(self) -> bool:
        """True if last successful fetch was too long ago."""
        if self._last_updated is None:
            return True
        age = (datetime.now(timezone.utc) - self._last_updated).total_seconds()
        return age > self.max_staleness_seconds

    @property
    def last_signal(self) -> Optional[dict]:
        """Most recent signal dict (may be stale)."""
        if self._last_signal is None:
            return self._stale_signal()
        if self.is_stale:
            sig = dict(self._last_signal)
            sig["stale"] = True
            return sig
        return self._last_signal

    def _on_settings_changed(self, _event) -> None:
        try:
            self.on_settings_changed()
        except Exception as exc:
            logger.warning("Agent [%s] settings update error: %s", self._name, exc)

    def _persist_signal(self, result: dict) -> None:
        """Persist agent signal to the AgentSignal table (best-effort)."""
        try:
            from datetime import timezone as _tz
            from core.database.engine import get_session
            from core.database.models import AgentSignal

            # Get orchestrator context if available
            regime_bias      = None
            macro_risk_score = None
            macro_veto       = None
            try:
                from core.orchestrator.orchestrator_engine import get_orchestrator
                orch = get_orchestrator()
                sig  = orch.get_signal()
                regime_bias      = sig.regime_bias
                macro_risk_score = sig.macro_risk_score
                macro_veto       = sig.macro_veto
            except Exception:
                pass

            row = AgentSignal(
                agent_name       = self._name,
                timestamp        = datetime.now(_tz.utc),
                signal           = float(result.get("signal", 0.0)),
                confidence       = float(result.get("confidence", 0.0)),
                is_stale         = bool(result.get("stale", False)),
                symbol           = result.get("symbol"),
                topic            = self.event_topic,
                payload          = {
                    k: v for k, v in result.items()
                    if k not in ("source", "updated_at", "stale")
                    and isinstance(v, (str, int, float, bool, type(None)))
                },
                regime_bias      = regime_bias,
                macro_risk_score = macro_risk_score,
                macro_veto       = macro_veto,
            )
            with get_session() as session:
                session.add(row)
                session.commit()
        except Exception as exc:
            logger.debug("Agent [%s] DB persist failed: %s", self._name, exc)
