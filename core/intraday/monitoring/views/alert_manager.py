"""
Phase 9: Alert Manager

Event-driven alert system. Subscribes to EventBus topics and
produces AlertRecord objects for critical conditions:

- Kill switch activation
- Circuit breaker state changes
- Execution anomalies (high slippage, fill timeout)
- Reconciliation failures
- Failure mode tier escalations
- Recovery failures

STRICT OBSERVER: reads events, produces alerts. Never mutates
upstream state. Never participates in trading decisions.

No Qt imports. Pure Python.
"""

from __future__ import annotations

import hashlib
import logging
import time
import threading
from collections import deque
from typing import Any, Callable, Dict, List, Optional

from .view_contracts import AlertRecord, AlertSeverity

logger = logging.getLogger(__name__)

# Maximum alert history
MAX_ALERT_HISTORY = 500

# Dedup window: suppress identical alerts within this window
DEDUP_WINDOW_MS = 60_000  # 60 seconds


class AlertManager:
    """
    Event-driven alert manager for critical system conditions.

    Usage:
        manager = AlertManager()

        # Manual alert creation
        manager.raise_alert(
            severity=AlertSeverity.CRITICAL,
            reason_code="kill_switch_activated",
            message="Kill switch disarmed: drawdown exceeded 10%",
            source="kill_switch",
        )

        # Process an event from EventBus
        manager.process_event(topic="system.alert", data={...})

        # Query alerts
        recent = manager.get_recent_alerts(limit=20)
        critical = manager.get_alerts_by_severity(AlertSeverity.CRITICAL)
    """

    def __init__(self, now_ms_fn=None):
        self._now_ms_fn = now_ms_fn or (lambda: int(time.time() * 1000))
        self._alerts: deque = deque(maxlen=MAX_ALERT_HISTORY)
        self._dedup_cache: Dict[str, int] = {}  # hash → last_raised_ms
        self._lock = threading.RLock()
        self._listeners: List[Callable[[AlertRecord], None]] = []

        logger.info("AlertManager initialized")

    # ── Alert Creation ────────────────────────────────────────

    def raise_alert(
        self,
        severity: AlertSeverity,
        reason_code: str,
        message: str,
        source: str,
        symbol: str = "",
        metadata: str = "",
    ) -> Optional[AlertRecord]:
        """
        Create and store an alert.

        Deduplicates: identical alerts within DEDUP_WINDOW_MS are suppressed.

        Args:
            severity: Alert severity level.
            reason_code: Machine-readable reason code.
            message: Human-readable description.
            source: Component that raised the alert.
            symbol: Symbol context (optional).
            metadata: Extra JSON context (optional).

        Returns:
            AlertRecord if created, None if suppressed by dedup.
        """
        now = self._now_ms_fn()

        # Dedup check
        dedup_key = self._dedup_hash(reason_code, source, symbol)
        with self._lock:
            if dedup_key in self._dedup_cache:
                last_raised = self._dedup_cache[dedup_key]
                if now - last_raised < DEDUP_WINDOW_MS:
                    return None  # Suppressed

            # Create alert
            alert_id = self._make_alert_id(reason_code, source, now)
            alert = AlertRecord(
                alert_id=alert_id,
                timestamp_ms=now,
                severity=severity,
                reason_code=reason_code,
                message=message,
                source=source,
                symbol=symbol,
                metadata=metadata,
            )

            self._alerts.append(alert)
            self._dedup_cache[dedup_key] = now

            # Notify listeners
            for listener in self._listeners:
                try:
                    listener(alert)
                except Exception as e:
                    logger.warning(f"AlertManager: listener error: {e}")

        logger.info(
            f"Alert [{severity.value}] {reason_code}: {message} "
            f"(source={source}, symbol={symbol})"
        )

        return alert

    # ── Event Processing ──────────────────────────────────────

    def process_event(self, topic: str, data: Any) -> Optional[AlertRecord]:
        """
        Process an EventBus event and generate alerts if warranted.

        Maps known topics to alert rules:
        - system.alert → INFO/WARNING depending on content
        - risk events → WARNING/CRITICAL
        - kill switch → EMERGENCY
        - circuit breaker → CRITICAL
        - reconciliation → CRITICAL

        Args:
            topic: EventBus topic string.
            data: Event data (usually dict).

        Returns:
            AlertRecord if alert was created, None otherwise.
        """
        if not isinstance(data, dict):
            data = {"raw": str(data)}

        # Kill switch
        if topic in ("kill_switch.disarmed", "emergency.stop"):
            return self.raise_alert(
                severity=AlertSeverity.EMERGENCY,
                reason_code="kill_switch_activated",
                message=str(data.get("reason", "Kill switch activated")),
                source="kill_switch",
                metadata=str(data.get("details", "")),
            )

        # Circuit breaker
        if topic in ("circuit_breaker.tripped",):
            return self.raise_alert(
                severity=AlertSeverity.CRITICAL,
                reason_code="circuit_breaker_tripped",
                message=str(data.get("reason", "Circuit breaker tripped")),
                source="circuit_breaker",
            )

        if topic in ("circuit_breaker.warning",):
            return self.raise_alert(
                severity=AlertSeverity.WARNING,
                reason_code="circuit_breaker_warning",
                message=str(data.get("reason", "Circuit breaker warning")),
                source="circuit_breaker",
            )

        # Reconciliation failure
        if topic in ("reconciliation.failure", "reconciliation.mismatch"):
            mismatch_count = data.get("mismatch_count", 0)
            return self.raise_alert(
                severity=AlertSeverity.CRITICAL,
                reason_code="reconciliation_failure",
                message=f"Reconciliation found {mismatch_count} mismatches",
                source="reconciliation",
                metadata=str(data.get("mismatches", "")),
            )

        # Execution anomaly (high slippage)
        if topic in ("execution.high_slippage",):
            symbol = data.get("symbol", "")
            slippage = data.get("slippage_bps", 0)
            return self.raise_alert(
                severity=AlertSeverity.WARNING,
                reason_code="high_slippage",
                message=f"High slippage on {symbol}: {slippage:.1f} bps",
                source="execution_quality",
                symbol=symbol,
            )

        # Failure mode escalation
        if topic in ("failure_mode.escalation",):
            tier = data.get("tier", "unknown")
            severity = (
                AlertSeverity.CRITICAL if tier in ("suspended", "degraded")
                else AlertSeverity.WARNING
            )
            return self.raise_alert(
                severity=severity,
                reason_code="failure_mode_escalation",
                message=f"Failure mode escalated to {tier}",
                source="failure_mode",
            )

        # Recovery failure
        if topic in ("recovery.failed",):
            return self.raise_alert(
                severity=AlertSeverity.CRITICAL,
                reason_code="recovery_failure",
                message=str(data.get("reason", "Recovery failed")),
                source="recovery",
            )

        # Generic system alert
        if topic in ("system.alert",):
            level = str(data.get("level", "info")).lower()
            severity_map = {
                "info": AlertSeverity.INFO,
                "warning": AlertSeverity.WARNING,
                "error": AlertSeverity.CRITICAL,
                "critical": AlertSeverity.CRITICAL,
                "emergency": AlertSeverity.EMERGENCY,
            }
            return self.raise_alert(
                severity=severity_map.get(level, AlertSeverity.INFO),
                reason_code=str(data.get("reason_code", "system_alert")),
                message=str(data.get("message", str(data))),
                source=str(data.get("source", "system")),
                symbol=str(data.get("symbol", "")),
            )

        return None

    # ── Queries ───────────────────────────────────────────────

    def get_recent_alerts(self, limit: int = 50) -> List[AlertRecord]:
        """Get most recent alerts, newest first."""
        with self._lock:
            alerts = list(self._alerts)
        alerts.reverse()
        return alerts[:limit]

    def get_alerts_by_severity(
        self, severity: AlertSeverity, limit: int = 50
    ) -> List[AlertRecord]:
        """Get alerts filtered by severity."""
        with self._lock:
            alerts = [a for a in self._alerts if a.severity == severity]
        alerts.reverse()
        return alerts[:limit]

    def get_alerts_since(self, since_ms: int) -> List[AlertRecord]:
        """Get all alerts since a timestamp."""
        with self._lock:
            return [a for a in self._alerts if a.timestamp_ms >= since_ms]

    def get_alert_count_by_severity(self) -> Dict[str, int]:
        """Get count of alerts by severity level."""
        with self._lock:
            counts: Dict[str, int] = {}
            for a in self._alerts:
                counts[a.severity.value] = counts.get(a.severity.value, 0) + 1
            return counts

    @property
    def total_alerts(self) -> int:
        """Total number of alerts in history."""
        with self._lock:
            return len(self._alerts)

    # ── Listeners ─────────────────────────────────────────────

    def add_listener(self, callback: Callable[[AlertRecord], None]) -> None:
        """Register a callback for new alerts."""
        with self._lock:
            self._listeners.append(callback)

    def remove_listener(self, callback: Callable[[AlertRecord], None]) -> None:
        """Remove a previously registered callback."""
        with self._lock:
            self._listeners = [l for l in self._listeners if l is not callback]

    # ── Helpers ────────────────────────────────────────────────

    @staticmethod
    def _dedup_hash(reason_code: str, source: str, symbol: str) -> str:
        raw = f"{reason_code}|{source}|{symbol}"
        return hashlib.md5(raw.encode()).hexdigest()[:12]

    @staticmethod
    def _make_alert_id(reason_code: str, source: str, ts: int) -> str:
        raw = f"{reason_code}|{source}|{ts}"
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    def get_state(self) -> dict:
        """Get alert manager state for diagnostics."""
        with self._lock:
            return {
                "total_alerts": len(self._alerts),
                "by_severity": self.get_alert_count_by_severity(),
                "dedup_cache_size": len(self._dedup_cache),
                "listener_count": len(self._listeners),
            }
