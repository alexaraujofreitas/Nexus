# ============================================================
# NEXUS TRADER — Candle Trace  (Phase 3 Addendum, Fix 3)
#
# Per-candle traceability: deterministic trace_id, data source,
# lifecycle stage tracking.
#
# ZERO PySide6 imports.
#
# Each candle flowing through the pipeline carries a CandleTrace
# that records:
#   - trace_id: deterministic key derived from symbol + timeframe + timestamp
#   - source: WS / REST / BACKFILL / REPLAY
#   - lifecycle stages with wall-clock timestamps:
#       received → normalized → buffered → derived → published
#   - lineage: for derived candles, the trace_ids of constituent 1m candles
#
# The TraceRegistry maintains a bounded LRU of recent traces for
# audit and debugging.
# ============================================================
from __future__ import annotations

import hashlib
import logging
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

logger = logging.getLogger(__name__)


# ── Constants ──────────────────────────────────────────────────
_REGISTRY_MAX_SIZE = 5000    # Max traces in registry before LRU eviction


class CandleSource(str, Enum):
    """Where a candle originated."""
    WEBSOCKET = "ws"
    REST = "rest"
    BACKFILL = "backfill"
    REPLAY = "replay"


class CandleLifecycleStage(str, Enum):
    """Pipeline stages a candle passes through."""
    RECEIVED = "received"
    NORMALIZED = "normalized"
    BUFFERED = "buffered"
    DEDUPLICATED = "deduplicated"
    FORWARDED = "forwarded"       # Forwarded to CandleBuilder
    DERIVED = "derived"           # Higher-TF candle was produced
    PUBLISHED = "published"       # Published to EventBus


def make_trace_id(symbol: str, timeframe: str, timestamp_ms: int) -> str:
    """
    Generate a deterministic trace ID for a candle.

    Format: first 12 hex chars of SHA-256(symbol|timeframe|timestamp).
    Deterministic: same inputs always produce the same trace_id.
    """
    raw = f"{symbol}|{timeframe}|{timestamp_ms}"
    return hashlib.sha256(raw.encode()).hexdigest()[:12]


@dataclass
class CandleTrace:
    """
    Tracks one candle's journey through the pipeline.

    Attributes
    ----------
    trace_id : str
        Deterministic key (see make_trace_id)
    symbol : str
        Trading pair
    timeframe : str
        Candle timeframe (1m, 3m, 5m, etc.)
    timestamp_ms : int
        Candle open timestamp in milliseconds
    source : CandleSource
        Where the candle originated (WS/REST/BACKFILL/REPLAY)
    stages : dict
        {stage_name: wall_clock_time} recording when each lifecycle
        stage was reached
    lineage : list[str]
        For derived candles: trace_ids of the constituent 1m candles.
        Empty for 1m candles.
    """
    trace_id: str
    symbol: str
    timeframe: str
    timestamp_ms: int
    source: CandleSource
    stages: dict = field(default_factory=dict)
    lineage: list = field(default_factory=list)

    def record_stage(self, stage: CandleLifecycleStage) -> None:
        """Record that this candle reached a lifecycle stage."""
        self.stages[stage.value] = time.time()

    def has_stage(self, stage: CandleLifecycleStage) -> bool:
        return stage.value in self.stages

    def snapshot(self) -> dict:
        return {
            "trace_id": self.trace_id,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "timestamp_ms": self.timestamp_ms,
            "source": self.source.value,
            "stages": dict(self.stages),
            "lineage": list(self.lineage),
        }


class TraceRegistry:
    """
    Bounded registry of recent candle traces for audit.
    Uses LRU eviction when capacity is exceeded.
    Thread-safe.
    """

    def __init__(self, max_size: int = _REGISTRY_MAX_SIZE):
        self._traces: OrderedDict[str, CandleTrace] = OrderedDict()
        self._max_size = max_size
        self._lock = threading.Lock()

    def register(self, trace: CandleTrace) -> None:
        """Add or update a trace in the registry."""
        with self._lock:
            if trace.trace_id in self._traces:
                # Move to end (most recent)
                self._traces.move_to_end(trace.trace_id)
                self._traces[trace.trace_id] = trace
            else:
                self._traces[trace.trace_id] = trace
                while len(self._traces) > self._max_size:
                    self._traces.popitem(last=False)  # Evict oldest

    def get(self, trace_id: str) -> Optional[CandleTrace]:
        """Look up a trace by ID."""
        with self._lock:
            return self._traces.get(trace_id)

    def get_by_symbol(self, symbol: str, limit: int = 50) -> list[CandleTrace]:
        """Get recent traces for a symbol."""
        with self._lock:
            results = []
            for trace in reversed(self._traces.values()):
                if trace.symbol == symbol:
                    results.append(trace)
                    if len(results) >= limit:
                        break
            return results

    def get_by_timeframe(self, tf: str, limit: int = 50) -> list[CandleTrace]:
        """Get recent traces for a timeframe."""
        with self._lock:
            results = []
            for trace in reversed(self._traces.values()):
                if trace.timeframe == tf:
                    results.append(trace)
                    if len(results) >= limit:
                        break
            return results

    def __len__(self) -> int:
        with self._lock:
            return len(self._traces)

    def clear(self) -> None:
        with self._lock:
            self._traces.clear()


# ── Global singleton ───────────────────────────────────────────
trace_registry = TraceRegistry()
