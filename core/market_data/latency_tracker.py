# ============================================================
# NEXUS TRADER — Latency Tracker  (Phase 3, Module 3.4)
#
# Centralized latency and throughput monitoring for the
# entire data pipeline: WS → DataEngine → CandleBuilder → EventBus.
#
# ZERO PySide6 imports.
#
# Collects metrics from:
#   - ConnectivityManager (WS message latency, REST poll latency)
#   - DataEngine (ingestion + normalization latency)
#   - CandleBuilder (TF derivation latency)
#   - EventBus publish latency (instrumented publish wrapper)
#
# Exposes a unified dashboard-ready snapshot and optional
# periodic logging for observability.
# ============================================================
from __future__ import annotations

import logging
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────
_WINDOW_SIZE = 100          # Rolling window for percentile calculations
_LOG_INTERVAL_S = 60.0      # Log summary every 60s


@dataclass
class LatencyBucket:
    """Rolling window of latency samples for a single pipeline stage."""
    name: str
    _samples: deque = field(default_factory=lambda: deque(maxlen=_WINDOW_SIZE))
    total_count: int = 0
    total_sum_ms: float = 0.0
    last_ms: float = 0.0

    def record(self, latency_ms: float) -> None:
        self._samples.append(latency_ms)
        self.total_count += 1
        self.total_sum_ms += latency_ms
        self.last_ms = latency_ms

    @property
    def avg_ms(self) -> float:
        return self.total_sum_ms / self.total_count if self.total_count else 0.0

    @property
    def p50_ms(self) -> float:
        return self._percentile(50)

    @property
    def p95_ms(self) -> float:
        return self._percentile(95)

    @property
    def p99_ms(self) -> float:
        return self._percentile(99)

    @property
    def max_ms(self) -> float:
        return max(self._samples) if self._samples else 0.0

    def _percentile(self, pct: int) -> float:
        if not self._samples:
            return 0.0
        sorted_samples = sorted(self._samples)
        idx = int(len(sorted_samples) * pct / 100)
        idx = min(idx, len(sorted_samples) - 1)
        return sorted_samples[idx]

    def snapshot(self) -> dict:
        return {
            "name": self.name,
            "count": self.total_count,
            "last_ms": round(self.last_ms, 2),
            "avg_ms": round(self.avg_ms, 2),
            "p50_ms": round(self.p50_ms, 2),
            "p95_ms": round(self.p95_ms, 2),
            "p99_ms": round(self.p99_ms, 2),
            "max_ms": round(self.max_ms, 2),
        }


class LatencyTracker:
    """
    Centralized pipeline latency tracker.

    Usage::

        tracker = LatencyTracker()
        tracker.start()

        # Record latencies from various pipeline stages:
        tracker.record("ws_message", 12.5)
        tracker.record("candle_normalize", 0.3)
        tracker.record("tf_derivation", 1.2)
        tracker.record("event_publish", 0.1)

        # Get dashboard snapshot:
        snapshot = tracker.snapshot()

        tracker.stop()
    """

    # Well-known stage names
    STAGE_WS_MESSAGE = "ws_message"
    STAGE_REST_POLL = "rest_poll"
    STAGE_NORMALIZE = "candle_normalize"
    STAGE_TF_DERIVE = "tf_derivation"
    STAGE_EVENT_PUBLISH = "event_publish"
    STAGE_END_TO_END = "end_to_end"       # WS receipt → EventBus publish

    def __init__(self, log_interval_s: float = _LOG_INTERVAL_S):
        self._buckets: dict[str, LatencyBucket] = {}
        self._lock = threading.Lock()
        self._log_interval_s = log_interval_s
        self._last_log_at = 0.0
        self._started = False

        # Throughput tracking
        self._throughput: dict[str, int] = defaultdict(int)  # stage → count per interval
        self._throughput_last_reset = time.time()

        # End-to-end tracking: record arrival time per symbol
        self._arrival_times: dict[str, float] = {}  # symbol → epoch seconds

    # ── Public API ─────────────────────────────────────────────

    def start(self) -> None:
        self._started = True
        self._last_log_at = time.time()
        logger.info("LatencyTracker: started")

    def stop(self) -> None:
        self._started = False
        logger.info("LatencyTracker: stopped")

    def record(self, stage: str, latency_ms: float) -> None:
        """Record a latency measurement for a pipeline stage."""
        if not self._started:
            return

        with self._lock:
            if stage not in self._buckets:
                self._buckets[stage] = LatencyBucket(name=stage)
            self._buckets[stage].record(latency_ms)
            self._throughput[stage] += 1

        # Periodic logging
        now = time.time()
        if now - self._last_log_at > self._log_interval_s:
            self._log_summary()
            self._last_log_at = now

    def mark_arrival(self, symbol: str) -> None:
        """Mark the arrival time for end-to-end latency tracking."""
        self._arrival_times[symbol] = time.time()

    def mark_published(self, symbol: str) -> None:
        """
        Mark the publish time and record end-to-end latency.
        Call this after EventBus.publish() for a candle event.
        """
        arrival = self._arrival_times.pop(symbol, None)
        if arrival:
            e2e_ms = (time.time() - arrival) * 1000
            self.record(self.STAGE_END_TO_END, e2e_ms)

    def snapshot(self) -> dict:
        """Return a dashboard-ready metrics snapshot."""
        with self._lock:
            result = {
                "stages": {
                    name: bucket.snapshot()
                    for name, bucket in self._buckets.items()
                },
                "throughput": dict(self._throughput),
            }
        return result

    def get_stage(self, stage: str) -> Optional[dict]:
        """Get metrics for a specific stage."""
        with self._lock:
            bucket = self._buckets.get(stage)
            return bucket.snapshot() if bucket else None

    # ── Internal ───────────────────────────────────────────────

    def _log_summary(self) -> None:
        """Log a periodic summary of pipeline latencies."""
        with self._lock:
            if not self._buckets:
                return

            parts = []
            for name, bucket in sorted(self._buckets.items()):
                if bucket.total_count > 0:
                    parts.append(
                        f"{name}: p50={bucket.p50_ms:.1f}ms p95={bucket.p95_ms:.1f}ms "
                        f"n={self._throughput.get(name, 0)}"
                    )

            # Reset throughput counters
            self._throughput = defaultdict(int)
            self._throughput_last_reset = time.time()

        if parts:
            logger.info("LatencyTracker: %s", " | ".join(parts))


# ── Global singleton ───────────────────────────────────────────
latency_tracker = LatencyTracker()
