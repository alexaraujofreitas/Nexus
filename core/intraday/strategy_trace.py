# ============================================================
# NEXUS TRADER — Strategy Trace  (Phase 4)
#
# Extends the Phase 3 candle traceability model to decisions.
# Every setup and trigger carries a StrategyTrace recording:
#   - deterministic ID (chained from candle trace IDs)
#   - lifecycle stages with timestamps
#   - rejection/expiry reasons
#   - parent candle trace references
#
# Supports end-to-end reconstruction:
#   candle → setup_evaluated → setup_qualified → trigger_evaluated
#   → trigger_fired → signal_expired (if applicable)
#
# ZERO PySide6 imports.
# ============================================================
from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

logger = logging.getLogger(__name__)

_REGISTRY_MAX_SIZE = 10_000


class DecisionStage(str, Enum):
    """Lifecycle stages for strategy decisions."""
    # Setup stages
    SETUP_EVALUATED = "setup_evaluated"
    SETUP_QUALIFIED = "setup_qualified"
    SETUP_REJECTED = "setup_rejected"
    SETUP_EXPIRED = "setup_expired"
    SETUP_CONSUMED = "setup_consumed"

    # Trigger stages
    TRIGGER_EVALUATED = "trigger_evaluated"
    TRIGGER_FIRED = "trigger_fired"
    TRIGGER_REJECTED = "trigger_rejected"

    # Post-trigger stages
    SIGNAL_EXPIRED = "signal_expired"
    SIGNAL_FORWARDED = "signal_forwarded"


@dataclass
class StrategyTrace:
    """
    Tracks one setup or trigger decision through the pipeline.

    Mutable (not frozen) because lifecycle stages are added incrementally.
    Thread-safe via the StrategyTraceRegistry.
    """
    trace_id: str                   # setup_id or trigger_id
    trace_type: str                 # "setup" or "trigger"
    strategy_name: str
    symbol: str
    direction: str
    parent_candle_traces: tuple     # Candle trace IDs that fed this decision
    parent_setup_id: str = ""       # For triggers: the setup_id they derive from
    stages: dict = field(default_factory=dict)   # {stage: {"at": timestamp, "reason": str}}

    def record_stage(self, stage: DecisionStage, reason: str = "") -> None:
        """Record that this decision reached a lifecycle stage."""
        self.stages[stage.value] = {
            "at": time.time(),
            "reason": reason,
        }

    def has_stage(self, stage: DecisionStage) -> bool:
        return stage.value in self.stages

    def get_reason(self, stage: DecisionStage) -> str:
        entry = self.stages.get(stage.value)
        return entry["reason"] if entry else ""

    def snapshot(self) -> dict:
        return {
            "trace_id": self.trace_id,
            "trace_type": self.trace_type,
            "strategy_name": self.strategy_name,
            "symbol": self.symbol,
            "direction": self.direction,
            "parent_candle_traces": list(self.parent_candle_traces),
            "parent_setup_id": self.parent_setup_id,
            "stages": {k: dict(v) for k, v in self.stages.items()},
        }


class StrategyTraceRegistry:
    """
    Bounded registry of strategy decision traces.
    Thread-safe, LRU eviction.
    """

    def __init__(self, max_size: int = _REGISTRY_MAX_SIZE):
        self._traces: OrderedDict[str, StrategyTrace] = OrderedDict()
        self._max_size = max_size
        self._lock = threading.Lock()

    def register(self, trace: StrategyTrace) -> None:
        with self._lock:
            if trace.trace_id in self._traces:
                self._traces.move_to_end(trace.trace_id)
                self._traces[trace.trace_id] = trace
            else:
                self._traces[trace.trace_id] = trace
                while len(self._traces) > self._max_size:
                    self._traces.popitem(last=False)

    def get(self, trace_id: str) -> Optional[StrategyTrace]:
        with self._lock:
            return self._traces.get(trace_id)

    def get_by_symbol(self, symbol: str, limit: int = 50) -> list[StrategyTrace]:
        with self._lock:
            results = []
            for trace in reversed(self._traces.values()):
                if trace.symbol == symbol:
                    results.append(trace)
                    if len(results) >= limit:
                        break
            return results

    def get_by_strategy(self, strategy_name: str,
                        limit: int = 50) -> list[StrategyTrace]:
        with self._lock:
            results = []
            for trace in reversed(self._traces.values()):
                if trace.strategy_name == strategy_name:
                    results.append(trace)
                    if len(results) >= limit:
                        break
            return results

    def get_chain(self, trigger_id: str) -> list[StrategyTrace]:
        """Get the full decision chain: setup → trigger."""
        with self._lock:
            trigger = self._traces.get(trigger_id)
            if not trigger:
                return []
            chain = []
            if trigger.parent_setup_id:
                setup = self._traces.get(trigger.parent_setup_id)
                if setup:
                    chain.append(setup)
            chain.append(trigger)
            return chain

    def __len__(self) -> int:
        with self._lock:
            return len(self._traces)

    def clear(self) -> None:
        with self._lock:
            self._traces.clear()


# ── Global singleton ──────────────────────────────────────────
strategy_trace_registry = StrategyTraceRegistry()
