# ============================================================
# NEXUS TRADER — Candle Builder  (Phase 3, Module 3.3)
#
# Deterministic multi-timeframe candle construction from 1m base.
# ZERO PySide6 imports.
#
# The 1-minute candle is the SINGLE SOURCE OF TRUTH.
# All higher timeframes (3m, 5m, 15m, 30m, 1h) are derived
# deterministically by aggregating 1m candles.
#
# Algorithm:
#   For a target TF of N minutes, a candle boundary is:
#     boundary_ts = ts - (ts % (N * 60000))
#   All 1m candles whose open time falls in [boundary, boundary + N*60000)
#   are aggregated:
#     open  = first candle's open
#     high  = max of all highs
#     low   = min of all lows
#     close = last candle's close
#     volume = sum of all volumes
#
# The builder emits completed higher-TF candles to the EventBus
# via the CANDLE_* topics when the boundary window is fully filled.
# ============================================================
from __future__ import annotations

import logging
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional, Callable

from core.event_bus import bus, Topics
from core.market_data.candle_trace import (
    CandleTrace, CandleSource, CandleLifecycleStage,
    make_trace_id, trace_registry,
)

logger = logging.getLogger(__name__)


# ── Constants ──────────────────────────────────────────────────
_1M_MS = 60_000

# Supported derived timeframes and their duration in minutes
DERIVED_TIMEFRAMES = {
    "3m": 3,
    "5m": 5,
    "15m": 15,
    "30m": 30,
    "1h": 60,
}

# Map timeframe string → EventBus topic
TF_TOPIC_MAP = {
    "1m": Topics.CANDLE_1M,
    "3m": Topics.CANDLE_3M,
    "5m": Topics.CANDLE_5M,
    "15m": Topics.CANDLE_15M,
    "1h": Topics.CANDLE_1H,
}


@dataclass
class BuilderMetrics:
    """Tracks CandleBuilder performance."""
    candles_received_1m: int = 0
    candles_built: dict = field(default_factory=lambda: defaultdict(int))  # tf → count
    build_latency_ms: float = 0.0
    _latency_sum: float = 0.0
    _latency_count: int = 0
    last_build_at: float = 0.0

    def record_build(self, tf: str, latency_ms: float) -> None:
        self.candles_built[tf] = self.candles_built.get(tf, 0) + 1
        self.build_latency_ms = latency_ms
        self._latency_sum += latency_ms
        self._latency_count += 1
        self.last_build_at = time.time()

    @property
    def avg_build_latency_ms(self) -> float:
        return self._latency_sum / self._latency_count if self._latency_count else 0.0

    def snapshot(self) -> dict:
        return {
            "candles_received_1m": self.candles_received_1m,
            "candles_built": dict(self.candles_built),
            "build_latency_ms": round(self.build_latency_ms, 2),
            "avg_build_latency_ms": round(self.avg_build_latency_ms, 2),
            "last_build_at": self.last_build_at,
        }


def align_to_boundary(ts_ms: int, period_minutes: int) -> int:
    """
    Align a timestamp to the start of its containing period.

    Example: align_to_boundary(ts, 15) aligns to 15-minute boundary.
    """
    period_ms = period_minutes * _1M_MS
    return ts_ms - (ts_ms % period_ms)


def aggregate_candles(candles: list[dict]) -> Optional[dict]:
    """
    Aggregate a list of 1m candles into a single higher-TF candle.

    Parameters
    ----------
    candles : list[dict]
        Sorted list of canonical candle dicts (ascending by timestamp)

    Returns
    -------
    dict or None : aggregated candle, or None if input is empty
    """
    if not candles:
        return None

    return {
        "timestamp": candles[0]["timestamp"],
        "open": candles[0]["open"],
        "high": max(c["high"] for c in candles),
        "low": min(c["low"] for c in candles),
        "close": candles[-1]["close"],
        "volume": sum(c["volume"] for c in candles),
        "symbol": candles[0].get("symbol", ""),
        "is_closed": True,
    }


class CandleBuilder:
    """
    Deterministic multi-TF candle builder.

    Receives closed 1m candles from DataEngine and derives
    higher-TF candles (3m, 5m, 15m, 30m, 1h).

    Usage::

        builder = CandleBuilder()
        builder.start()

        # DataEngine calls this on each closed 1m candle:
        builder.ingest(symbol, candle_dict)

    When a higher-TF boundary completes, the builder:
    1. Aggregates the 1m candles in that window
    2. Publishes the derived candle to the corresponding EventBus topic
    3. Invokes the on_candle callback (if set)
    """

    def __init__(
        self,
        timeframes: Optional[list[str]] = None,
        on_candle: Optional[Callable[[str, str, dict], None]] = None,
        publish_1m: bool = True,
    ):
        """
        Parameters
        ----------
        timeframes : list[str], optional
            Which higher TFs to derive. Default: all (3m, 5m, 15m, 30m, 1h)
        on_candle : callable, optional
            Callback(symbol, timeframe, candle_dict) for each completed candle
        publish_1m : bool
            Whether to publish 1m candles to EventBus (default True)
        """
        self._timeframes = timeframes or list(DERIVED_TIMEFRAMES.keys())
        self._on_candle = on_candle
        self._publish_1m = publish_1m
        self._metrics = BuilderMetrics()
        self._lock = threading.Lock()
        self._started = False

        # Per-symbol, per-TF accumulator
        # _accumulators[symbol][tf] = {boundary_ts: [candle, candle, ...]}
        self._accumulators: dict[str, dict[str, dict[int, list[dict]]]] = defaultdict(
            lambda: defaultdict(dict)
        )

        # Track last emitted boundary per symbol+tf to avoid re-emission
        self._last_emitted: dict[str, dict[str, int]] = defaultdict(dict)

    # ── Public API ─────────────────────────────────────────────

    @property
    def metrics(self) -> BuilderMetrics:
        return self._metrics

    def start(self) -> None:
        self._started = True
        logger.info(
            "CandleBuilder: started — deriving TFs: %s", self._timeframes
        )

    def stop(self) -> None:
        self._started = False
        logger.info("CandleBuilder: stopped")

    def ingest(self, symbol: str, candle: dict) -> None:
        """
        Ingest a closed 1m candle and derive higher TFs if boundaries complete.

        Parameters
        ----------
        symbol : str
            Trading pair
        candle : dict
            Canonical candle dict with is_closed=True
        """
        if not self._started:
            return

        self._metrics.candles_received_1m += 1

        # Publish 1m candle to EventBus (DATA layer topic)
        if self._publish_1m:
            bus.publish(Topics.CANDLE_1M, {
                "symbol": symbol,
                "candle": candle,
            }, source="candle_builder")

            # Record PUBLISHED stage for 1m trace
            tid = candle.get("trace_id")
            if tid:
                trace = trace_registry.get(tid)
                if trace:
                    trace.record_stage(CandleLifecycleStage.PUBLISHED)

            if self._on_candle:
                try:
                    self._on_candle(symbol, "1m", candle)
                except Exception as exc:
                    logger.error("CandleBuilder: on_candle callback error (1m): %s", exc)

        # Accumulate into higher-TF windows and check for completion
        ts = candle["timestamp"]

        for tf in self._timeframes:
            period_min = DERIVED_TIMEFRAMES.get(tf)
            if period_min is None:
                continue

            t0 = time.time()
            boundary = align_to_boundary(ts, period_min)
            expected_count = period_min  # Number of 1m candles in this TF window

            with self._lock:
                acc = self._accumulators[symbol][tf]

                # Add candle to the correct boundary bucket
                if boundary not in acc:
                    acc[boundary] = []
                acc[boundary].append(candle)

                # Check if this boundary is complete
                # A boundary is complete when we have all expected 1m candles
                # OR when a candle from the NEXT boundary arrives
                next_boundary = boundary + period_min * _1M_MS
                current_count = len(acc[boundary])

                should_emit = False

                if current_count >= expected_count:
                    should_emit = True
                elif ts >= next_boundary:
                    # We've moved past this window — emit what we have
                    # (may be incomplete due to gaps, but still valid)
                    should_emit = True

                if should_emit:
                    last = self._last_emitted[symbol].get(tf, 0)
                    if boundary > last:
                        candles_in_window = sorted(
                            acc[boundary], key=lambda c: c["timestamp"]
                        )
                        aggregated = aggregate_candles(candles_in_window)

                        if aggregated:
                            aggregated["timeframe"] = tf
                            latency_ms = (time.time() - t0) * 1000
                            self._metrics.record_build(tf, latency_ms)
                            self._last_emitted[symbol][tf] = boundary

                            # ── Traceability: derived candle ───────
                            derived_tid = make_trace_id(symbol, tf, boundary)
                            lineage_ids = [
                                c.get("trace_id", make_trace_id(symbol, "1m", c["timestamp"]))
                                for c in candles_in_window
                            ]
                            derived_trace = CandleTrace(
                                trace_id=derived_tid,
                                symbol=symbol,
                                timeframe=tf,
                                timestamp_ms=boundary,
                                source=CandleSource.REST,  # Will be overridden by first constituent
                                lineage=lineage_ids,
                            )
                            # Inherit source from first constituent trace
                            first_src = trace_registry.get(lineage_ids[0]) if lineage_ids else None
                            if first_src:
                                derived_trace.source = first_src.source
                            derived_trace.record_stage(CandleLifecycleStage.DERIVED)
                            aggregated["trace_id"] = derived_tid
                            aggregated["lineage"] = lineage_ids

                            # Publish to EventBus (DATA layer)
                            topic = TF_TOPIC_MAP.get(tf)
                            if topic:
                                bus.publish(topic, {
                                    "symbol": symbol,
                                    "candle": aggregated,
                                }, source="candle_builder")

                            derived_trace.record_stage(CandleLifecycleStage.PUBLISHED)
                            trace_registry.register(derived_trace)

                            # External callback
                            if self._on_candle:
                                try:
                                    self._on_candle(symbol, tf, aggregated)
                                except Exception as exc:
                                    logger.error(
                                        "CandleBuilder: on_candle callback error (%s): %s",
                                        tf, exc,
                                    )

                # Cleanup old boundaries (keep only current and previous)
                self._cleanup_old_boundaries(acc, boundary, period_min)

    def get_current_candle(self, symbol: str, tf: str) -> Optional[dict]:
        """
        Get the in-progress (possibly incomplete) candle for a symbol+TF.
        Useful for live display.
        """
        period_min = DERIVED_TIMEFRAMES.get(tf)
        if period_min is None:
            return None

        now_ms = int(time.time() * 1000)
        boundary = align_to_boundary(now_ms, period_min)

        with self._lock:
            acc = self._accumulators.get(symbol, {}).get(tf, {})
            candles = acc.get(boundary, [])
            if not candles:
                return None
            return aggregate_candles(sorted(candles, key=lambda c: c["timestamp"]))

    # ── Internal helpers ───────────────────────────────────────

    @staticmethod
    def _cleanup_old_boundaries(
        acc: dict[int, list[dict]], current_boundary: int, period_min: int
    ) -> None:
        """Remove boundaries older than 2 periods ago to prevent memory leak."""
        cutoff = current_boundary - (2 * period_min * _1M_MS)
        stale = [b for b in acc if b < cutoff]
        for b in stale:
            del acc[b]
