# ============================================================
# NEXUS TRADER — Data Engine  (Phase 3, Module 3.2)
#
# Pure-Python data ingestion, normalization, and storage layer.
# ZERO PySide6 imports.
#
# Receives raw candle data from ConnectivityManager (Module 3.1),
# normalizes it, detects/fills gaps, deduplicates, and feeds
# the CandleBuilder (Module 3.3) for multi-TF derivation.
#
# Responsibilities:
#   1. Ingest raw OHLCV data from WS or REST
#   2. Normalize to canonical candle dict format
#   3. Maintain per-symbol 1m candle buffers (ring buffers)
#   4. Detect gaps and request backfill
#   5. Deduplicate (by timestamp)
#   6. Forward complete 1m candles to CandleBuilder
#   7. Track ingestion metrics (latency, throughput, errors)
#
# Canonical candle dict format:
#   {
#     "timestamp": int,     # open time in epoch milliseconds
#     "open": float,
#     "high": float,
#     "low": float,
#     "close": float,
#     "volume": float,
#     "symbol": str,
#     "timeframe": str,     # always "1m" at this layer
#     "is_closed": bool,    # True if candle is finalized
#   }
# ============================================================
from __future__ import annotations

import bisect
import logging
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional, Callable

from core.market_data.candle_trace import (
    CandleTrace, CandleSource, CandleLifecycleStage,
    make_trace_id, trace_registry,
)

logger = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────
_1M_MS = 60_000                     # 1 minute in milliseconds
_BUFFER_SIZE = 1500                 # Keep last 1500 1m candles per symbol (~25h)
_MAX_CLOCK_SKEW_MS = 5_000          # Tolerate 5s clock skew


@dataclass
class IngestionMetrics:
    """Tracks data engine health."""
    candles_ingested: int = 0
    candles_deduplicated: int = 0
    candles_normalized: int = 0
    candles_forwarded: int = 0        # To CandleBuilder
    gaps_detected: int = 0
    invalid_candles: int = 0
    last_ingest_at: float = 0.0
    ingest_latency_ms: float = 0.0    # Last normalization latency
    _latency_sum: float = 0.0
    _latency_count: int = 0

    def record_ingest(self, latency_ms: float) -> None:
        self.candles_ingested += 1
        self.last_ingest_at = time.time()
        self.ingest_latency_ms = latency_ms
        self._latency_sum += latency_ms
        self._latency_count += 1

    @property
    def avg_ingest_latency_ms(self) -> float:
        return self._latency_sum / self._latency_count if self._latency_count else 0.0

    def snapshot(self) -> dict:
        return {
            "candles_ingested": self.candles_ingested,
            "candles_deduplicated": self.candles_deduplicated,
            "candles_normalized": self.candles_normalized,
            "candles_forwarded": self.candles_forwarded,
            "gaps_detected": self.gaps_detected,
            "invalid_candles": self.invalid_candles,
            "last_ingest_at": self.last_ingest_at,
            "ingest_latency_ms": round(self.ingest_latency_ms, 2),
            "avg_ingest_latency_ms": round(self.avg_ingest_latency_ms, 2),
        }


class CandleBuffer:
    """
    Sorted ring buffer of 1m candles for a single symbol.
    Candles are stored sorted by timestamp. Deduplication by timestamp.
    Thread-safe.
    """

    def __init__(self, max_size: int = _BUFFER_SIZE):
        self._candles: list[dict] = []       # Sorted by timestamp
        self._timestamps: list[int] = []     # Parallel sorted list for bisect
        self._max_size = max_size
        self._lock = threading.Lock()

    def insert(self, candle: dict) -> bool:
        """
        Insert a candle into the buffer.
        Returns True if inserted (new), False if duplicate.
        """
        ts = candle["timestamp"]
        with self._lock:
            idx = bisect.bisect_left(self._timestamps, ts)
            # Check for duplicate
            if idx < len(self._timestamps) and self._timestamps[idx] == ts:
                # Update in place if the new candle is "more closed"
                existing = self._candles[idx]
                if candle.get("is_closed") and not existing.get("is_closed"):
                    self._candles[idx] = candle
                    return True
                return False  # Duplicate

            # Insert at sorted position
            self._timestamps.insert(idx, ts)
            self._candles.insert(idx, candle)

            # Trim if over capacity
            while len(self._candles) > self._max_size:
                self._candles.pop(0)
                self._timestamps.pop(0)

            return True

    def get_latest(self, n: int = 1) -> list[dict]:
        """Return the latest N candles (most recent last)."""
        with self._lock:
            return list(self._candles[-n:])

    def get_since(self, since_ms: int) -> list[dict]:
        """Return all candles with timestamp >= since_ms."""
        with self._lock:
            idx = bisect.bisect_left(self._timestamps, since_ms)
            return list(self._candles[idx:])

    def get_range(self, start_ms: int, end_ms: int) -> list[dict]:
        """Return candles in [start_ms, end_ms)."""
        with self._lock:
            start_idx = bisect.bisect_left(self._timestamps, start_ms)
            end_idx = bisect.bisect_left(self._timestamps, end_ms)
            return list(self._candles[start_idx:end_idx])

    def get_last_timestamp(self) -> Optional[int]:
        """Return the timestamp of the most recent candle, or None."""
        with self._lock:
            return self._timestamps[-1] if self._timestamps else None

    def __len__(self) -> int:
        with self._lock:
            return len(self._candles)

    def detect_gaps(self) -> list[tuple[int, int]]:
        """
        Detect gaps in the 1m candle series.
        Returns list of (gap_start_ms, gap_end_ms) tuples.
        """
        gaps = []
        with self._lock:
            for i in range(1, len(self._timestamps)):
                expected = self._timestamps[i - 1] + _1M_MS
                actual = self._timestamps[i]
                if actual > expected + _MAX_CLOCK_SKEW_MS:
                    gaps.append((expected, actual))
        return gaps


# Type alias for candle-forwarding callback
CandleForwardCallback = Callable[[str, dict], None]


class DataEngine:
    """
    Central data ingestion and normalization engine.

    Receives raw OHLCV arrays from ConnectivityManager, normalizes
    them to canonical candle dicts, stores in per-symbol buffers,
    and forwards closed candles to the CandleBuilder.

    Usage::

        engine = DataEngine(on_candle_closed=candle_builder.ingest)
        engine.start()

        # ConnectivityManager calls this:
        engine.ingest_candles("BTC/USDT", "1m", [[ts, o, h, l, c, v], ...])

        engine.stop()
    """

    def __init__(
        self,
        on_candle_closed: Optional[CandleForwardCallback] = None,
        on_candle_update: Optional[CandleForwardCallback] = None,
        buffer_size: int = _BUFFER_SIZE,
    ):
        self._on_candle_closed = on_candle_closed
        self._on_candle_update = on_candle_update
        self._buffer_size = buffer_size

        self._buffers: dict[str, CandleBuffer] = {}  # symbol → buffer
        self._metrics = IngestionMetrics()
        self._lock = threading.Lock()
        self._started = False

        # Track which candles we've already forwarded as "closed"
        # to avoid re-forwarding on REST re-polls
        self._forwarded_closed: dict[str, set[int]] = defaultdict(set)

    # ── Public API ─────────────────────────────────────────────

    @property
    def metrics(self) -> IngestionMetrics:
        return self._metrics

    def start(self) -> None:
        self._started = True
        logger.info("DataEngine: started")

    def stop(self) -> None:
        self._started = False
        logger.info("DataEngine: stopped")

    def get_buffer(self, symbol: str) -> Optional[CandleBuffer]:
        """Get the candle buffer for a symbol."""
        return self._buffers.get(symbol)

    def get_latest_candles(self, symbol: str, n: int = 100) -> list[dict]:
        """Get the latest N candles for a symbol."""
        buf = self._buffers.get(symbol)
        return buf.get_latest(n) if buf else []

    def get_candles_since(self, symbol: str, since_ms: int) -> list[dict]:
        """Get all candles for a symbol since a given timestamp."""
        buf = self._buffers.get(symbol)
        return buf.get_since(since_ms) if buf else []

    def get_active_symbols(self) -> list[str]:
        """Return symbols that have data in their buffers."""
        return [s for s, b in self._buffers.items() if len(b) > 0]

    def detect_gaps(self, symbol: str) -> list[tuple[int, int]]:
        """Detect gaps in a symbol's candle buffer."""
        buf = self._buffers.get(symbol)
        return buf.detect_gaps() if buf else []

    # ── Ingestion entry point ──────────────────────────────────

    def ingest_candles(
        self, symbol: str, timeframe: str, raw_candles: list,
        source: CandleSource = CandleSource.REST,
    ) -> int:
        """
        Ingest raw candles from connectivity layer.

        Parameters
        ----------
        symbol : str
            Trading pair (e.g. "BTC/USDT")
        timeframe : str
            Candle timeframe (should be "1m" for Phase 3)
        raw_candles : list
            List of [ts_ms, open, high, low, close, volume]
        source : CandleSource
            Origin of this data (WS, REST, BACKFILL, REPLAY)

        Returns
        -------
        int : number of new candles ingested (excluding duplicates)
        """
        if not self._started:
            return 0

        if not raw_candles:
            return 0

        t0 = time.time()

        # Ensure buffer exists
        if symbol not in self._buffers:
            with self._lock:
                if symbol not in self._buffers:
                    self._buffers[symbol] = CandleBuffer(self._max_size)

        buf = self._buffers[symbol]
        new_count = 0

        for raw in raw_candles:
            candle = self._normalize(symbol, timeframe, raw)
            if candle is None:
                self._metrics.invalid_candles += 1
                continue

            self._metrics.candles_normalized += 1

            # ── Traceability: create trace for this candle ─────
            tid = make_trace_id(symbol, timeframe, candle["timestamp"])
            trace = CandleTrace(
                trace_id=tid,
                symbol=symbol,
                timeframe=timeframe,
                timestamp_ms=candle["timestamp"],
                source=source,
            )
            trace.record_stage(CandleLifecycleStage.RECEIVED)
            trace.record_stage(CandleLifecycleStage.NORMALIZED)

            # Attach trace_id to candle dict for downstream use
            candle["trace_id"] = tid

            # Insert into buffer (dedup handled by CandleBuffer)
            is_new = buf.insert(candle)
            if is_new:
                new_count += 1
                trace.record_stage(CandleLifecycleStage.BUFFERED)

                # Forward closed candles to CandleBuilder
                if candle.get("is_closed"):
                    ts = candle["timestamp"]
                    if ts not in self._forwarded_closed[symbol]:
                        self._forwarded_closed[symbol].add(ts)
                        self._metrics.candles_forwarded += 1
                        trace.record_stage(CandleLifecycleStage.FORWARDED)
                        if self._on_candle_closed:
                            try:
                                self._on_candle_closed(symbol, candle)
                            except Exception as exc:
                                logger.error(
                                    "DataEngine: candle_closed callback error: %s", exc
                                )

                # Always forward updates (for live candle display)
                if self._on_candle_update:
                    try:
                        self._on_candle_update(symbol, candle)
                    except Exception as exc:
                        logger.error("DataEngine: candle_update callback error: %s", exc)

                # Register trace
                trace_registry.register(trace)
            else:
                self._metrics.candles_deduplicated += 1
                trace.record_stage(CandleLifecycleStage.DEDUPLICATED)
                trace_registry.register(trace)

        latency_ms = (time.time() - t0) * 1000
        self._metrics.record_ingest(latency_ms)

        # Trim forwarded-closed set to avoid memory leak
        if len(self._forwarded_closed[symbol]) > self._buffer_size * 2:
            # Keep only timestamps in the current buffer
            buf_timestamps = set(c["timestamp"] for c in buf.get_latest(self._buffer_size))
            self._forwarded_closed[symbol] &= buf_timestamps

        return new_count

    @property
    def buffer_size(self) -> int:
        return self._buffer_size

    @buffer_size.setter
    def buffer_size(self, value: int) -> None:
        self._buffer_size = value
        self._max_size = value

    # ── Normalization ──────────────────────────────────────────

    @staticmethod
    def _normalize(symbol: str, timeframe: str, raw: list) -> Optional[dict]:
        """
        Normalize a raw OHLCV array to canonical candle dict.

        Input:  [ts_ms, open, high, low, close, volume]
        Output: canonical candle dict (see module docstring)

        Returns None if the raw data is invalid.
        """
        try:
            if not raw or len(raw) < 6:
                return None

            ts = int(raw[0])
            o = float(raw[1])
            h = float(raw[2])
            l = float(raw[3])
            c = float(raw[4])
            v = float(raw[5])

            # Basic validation
            if ts <= 0:
                return None
            if o <= 0 or h <= 0 or l <= 0 or c <= 0:
                return None
            if h < l:
                return None  # High must be >= Low
            if v < 0:
                return None  # Volume can't be negative

            # Determine if candle is closed
            # A candle is closed if the current time is past the candle's close time
            now_ms = int(time.time() * 1000)
            step_ms = _1M_MS  # For 1m candles
            candle_close_ms = ts + step_ms
            is_closed = now_ms >= candle_close_ms - _MAX_CLOCK_SKEW_MS

            return {
                "timestamp": ts,
                "open": o,
                "high": h,
                "low": l,
                "close": c,
                "volume": v,
                "symbol": symbol,
                "timeframe": timeframe,
                "is_closed": is_closed,
            }
        except (TypeError, ValueError, IndexError) as exc:
            logger.debug("DataEngine: normalization error: %s (raw=%s)", exc, raw)
            return None

    # ── Buffer property (for DataEngine.buffer_size setter) ────

    @property
    def _max_size(self) -> int:
        return self._buffer_size

    @_max_size.setter
    def _max_size(self, value: int) -> None:
        self._buffer_size = value
