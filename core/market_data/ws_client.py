# ============================================================
# NEXUS TRADER — WebSocket Client  (Phase 3, Module 3.1)
#
# Pure-Python WebSocket client for real-time market data.
# ZERO PySide6 imports.  Uses threading.Thread + asyncio.
#
# Wraps ccxt.pro watch_ohlcv() for 1m candle streaming.
# Features:
#   - Reconnect with exponential backoff (1s → 64s)
#   - Per-symbol subscription management
#   - Gap detection (sequence-aware)
#   - Latency tracking per message
#   - Graceful shutdown via threading.Event
#   - Publishes raw 1m candle data to EventBus
#
# This module is the PRIMARY data source.  rest_poller.py is
# the fallback activated when WS is unavailable or fails.
# ============================================================
from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Callable

logger = logging.getLogger(__name__)


# ── Constants ──────────────────────────────────────────────────
_MAX_WS_FAILURES = 5          # Switch to REST fallback after this many consecutive failures
_BACKOFF_BASE_S = 1.0         # Initial backoff
_BACKOFF_MAX_S = 64.0         # Cap
_BACKOFF_MULTIPLIER = 2.0
_HEARTBEAT_INTERVAL_S = 30.0  # Log heartbeat every N seconds
_STALE_THRESHOLD_S = 120.0    # Consider feed stale if no message for this long


class WSState(str, Enum):
    """WebSocket client lifecycle states."""
    IDLE = "idle"
    CONNECTING = "connecting"
    STREAMING = "streaming"
    RECONNECTING = "reconnecting"
    FAILED = "failed"          # Exceeded max failures — REST fallback needed
    STOPPED = "stopped"


@dataclass
class WSMetrics:
    """Per-client latency and health metrics."""
    messages_received: int = 0
    last_message_at: float = 0.0        # epoch seconds
    last_latency_ms: float = 0.0        # WS message → local receipt
    avg_latency_ms: float = 0.0
    _latency_sum: float = 0.0
    _latency_count: int = 0
    consecutive_failures: int = 0
    total_reconnects: int = 0
    gaps_detected: int = 0

    def record_latency(self, latency_ms: float) -> None:
        self.last_latency_ms = latency_ms
        self._latency_sum += latency_ms
        self._latency_count += 1
        self.avg_latency_ms = self._latency_sum / self._latency_count

    def record_message(self) -> None:
        self.messages_received += 1
        self.last_message_at = time.time()

    def record_failure(self) -> None:
        self.consecutive_failures += 1

    def reset_failures(self) -> None:
        self.consecutive_failures = 0

    def record_reconnect(self) -> None:
        self.total_reconnects += 1

    def record_gap(self) -> None:
        self.gaps_detected += 1

    @property
    def is_stale(self) -> bool:
        if self.last_message_at == 0.0:
            return False  # Never received a message yet
        return (time.time() - self.last_message_at) > _STALE_THRESHOLD_S

    def snapshot(self) -> dict:
        return {
            "messages_received": self.messages_received,
            "last_message_at": self.last_message_at,
            "last_latency_ms": round(self.last_latency_ms, 2),
            "avg_latency_ms": round(self.avg_latency_ms, 2),
            "consecutive_failures": self.consecutive_failures,
            "total_reconnects": self.total_reconnects,
            "gaps_detected": self.gaps_detected,
            "is_stale": self.is_stale,
        }


# Type alias for the callback that receives raw candle data
# Signature: callback(symbol, timeframe, candle_list) where candle_list = [[ts_ms, o, h, l, c, v], ...]
RawCandleCallback = Callable[[str, str, list], None]


class WebSocketClient(threading.Thread):
    """
    Pure-Python WebSocket client for streaming 1m OHLCV candles.

    Uses ccxt.pro's ``watch_ohlcv()`` in an asyncio event loop
    running on a dedicated daemon thread.

    Lifecycle::

        client = WebSocketClient(exchange_manager, symbols, on_candle=callback)
        client.start()      # non-blocking — runs in background thread
        ...
        client.stop()        # signals shutdown, joins thread

    The ``on_candle`` callback is invoked synchronously on the WS thread.
    It MUST be lightweight (e.g. enqueue work, publish to EventBus).
    """

    def __init__(
        self,
        exchange_manager,
        symbols: list[str],
        on_candle: Optional[RawCandleCallback] = None,
        on_state_change: Optional[Callable[[WSState], None]] = None,
        timeframe: str = "1m",
    ):
        super().__init__(name="WSClient", daemon=True)
        self._em = exchange_manager
        self._symbols = list(symbols)
        self._timeframe = timeframe
        self._on_candle = on_candle
        self._on_state_change = on_state_change

        self._stop_event = threading.Event()
        self._state = WSState.IDLE
        self._metrics = WSMetrics()
        self._lock = threading.Lock()

        # Per-symbol last-seen candle timestamp for gap detection
        self._last_ts: dict[str, int] = {}  # symbol → last candle timestamp (ms)

    # ── Public API ─────────────────────────────────────────────

    @property
    def state(self) -> WSState:
        return self._state

    @property
    def metrics(self) -> WSMetrics:
        return self._metrics

    def get_symbols(self) -> list[str]:
        with self._lock:
            return list(self._symbols)

    def set_symbols(self, symbols: list[str]) -> None:
        """Update subscribed symbols. Takes effect on next reconnect cycle."""
        with self._lock:
            self._symbols = list(symbols)
        logger.info("WSClient: symbols updated → %d symbols", len(symbols))

    def stop(self) -> None:
        """Signal the client to stop and wait for thread exit."""
        logger.info("WSClient: stop requested")
        self._stop_event.set()
        if self.is_alive():
            self.join(timeout=10.0)
        self._set_state(WSState.STOPPED)

    # ── Thread entry ───────────────────────────────────────────

    def run(self) -> None:
        """Thread entry point — runs the asyncio event loop."""
        logger.info("WSClient: thread started for %d symbols, tf=%s",
                     len(self._symbols), self._timeframe)
        try:
            asyncio.run(self._main_loop())
        except Exception as exc:
            logger.error("WSClient: event loop crashed: %s", exc, exc_info=True)
        finally:
            self._set_state(WSState.STOPPED)
            logger.info("WSClient: thread exited")

    # ── Core async loop ────────────────────────────────────────

    async def _main_loop(self) -> None:
        """
        Main reconnection loop.  Each iteration:
        1. Acquire the ccxt.pro WS exchange instance
        2. Launch watch_ohlcv tasks for all symbols
        3. On failure, backoff and retry
        4. After _MAX_WS_FAILURES consecutive failures, set FAILED state and exit
        """
        backoff_s = _BACKOFF_BASE_S

        while not self._stop_event.is_set():
            # ── Check failure limit ────────────────────────────
            if self._metrics.consecutive_failures >= _MAX_WS_FAILURES:
                logger.error(
                    "WSClient: %d consecutive failures — switching to FAILED state",
                    self._metrics.consecutive_failures,
                )
                self._set_state(WSState.FAILED)
                return

            # ── Get WS exchange ────────────────────────────────
            ws_exchange = self._em.get_ws_exchange() if self._em else None
            if ws_exchange is None or not callable(getattr(ws_exchange, "watch_ohlcv", None)):
                logger.warning("WSClient: no ccxt.pro WS exchange available — FAILED")
                self._set_state(WSState.FAILED)
                return

            # ── Stream ─────────────────────────────────────────
            self._set_state(WSState.CONNECTING)
            try:
                await self._stream_all(ws_exchange)
                # If _stream_all returns cleanly, it means stop was requested
                if self._stop_event.is_set():
                    return
            except asyncio.CancelledError:
                return
            except Exception as exc:
                self._metrics.record_failure()
                self._metrics.record_reconnect()
                logger.warning(
                    "WSClient: stream error (failure %d/%d): %s",
                    self._metrics.consecutive_failures, _MAX_WS_FAILURES, exc,
                )

            # ── Backoff before retry ───────────────────────────
            if not self._stop_event.is_set():
                self._set_state(WSState.RECONNECTING)
                logger.info("WSClient: reconnecting in %.1fs...", backoff_s)
                # Use stop_event.wait() so we can break out immediately on stop
                if self._stop_event.wait(timeout=backoff_s):
                    return  # Stop was requested during backoff
                backoff_s = min(backoff_s * _BACKOFF_MULTIPLIER, _BACKOFF_MAX_S)

        logger.info("WSClient: stop event set — exiting main loop")

    async def _stream_all(self, ws_exchange) -> None:
        """
        Launch one watch_ohlcv task per symbol. All tasks run concurrently.
        If any task fails, we cancel all tasks and let the caller handle retry.
        """
        self._set_state(WSState.STREAMING)
        self._metrics.reset_failures()

        with self._lock:
            symbols = list(self._symbols)

        if not symbols:
            logger.warning("WSClient: no symbols to stream — waiting")
            # Wait a bit then return to let the main loop re-check
            await asyncio.sleep(5.0)
            return

        tasks = []
        for symbol in symbols:
            task = asyncio.create_task(
                self._watch_symbol(ws_exchange, symbol),
                name=f"ws-{symbol}",
            )
            tasks.append(task)

        logger.info("WSClient: streaming %d symbols via watch_ohlcv", len(symbols))

        try:
            # Wait for first failure or stop
            done, pending = await asyncio.wait(
                tasks, return_when=asyncio.FIRST_EXCEPTION,
            )
            # Check if any task raised
            for t in done:
                if t.exception() is not None:
                    raise t.exception()
        finally:
            # Cancel all remaining tasks
            for t in tasks:
                if not t.done():
                    t.cancel()
            # Wait for cancellations to propagate
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _watch_symbol(self, ws_exchange, symbol: str) -> None:
        """
        Watch 1m OHLCV for a single symbol. Runs indefinitely until
        cancelled or an unrecoverable error occurs.
        """
        logger.debug("WSClient: starting watch_ohlcv for %s/%s", symbol, self._timeframe)
        last_heartbeat = time.time()

        while not self._stop_event.is_set():
            try:
                t0 = time.time()
                ohlcv_list = await ws_exchange.watch_ohlcv(symbol, self._timeframe)
                t1 = time.time()

                if not ohlcv_list:
                    continue

                # Latency: time from call to response
                latency_ms = (t1 - t0) * 1000
                self._metrics.record_latency(latency_ms)
                self._metrics.record_message()

                # Gap detection
                if symbol in self._last_ts and ohlcv_list:
                    expected_step_ms = 60_000  # 1m = 60000ms
                    newest_ts = ohlcv_list[-1][0]
                    last_ts = self._last_ts[symbol]
                    if last_ts > 0 and newest_ts > last_ts:
                        gap_candles = (newest_ts - last_ts) // expected_step_ms
                        if gap_candles > 1:
                            self._metrics.record_gap()
                            logger.warning(
                                "WSClient: gap detected for %s — %d missing candle(s) "
                                "between %d and %d",
                                symbol, gap_candles - 1, last_ts, newest_ts,
                            )

                # Update last seen timestamp
                if ohlcv_list:
                    self._last_ts[symbol] = ohlcv_list[-1][0]

                # Deliver to callback
                if self._on_candle and ohlcv_list:
                    try:
                        self._on_candle(symbol, self._timeframe, ohlcv_list)
                    except Exception as cb_exc:
                        logger.error("WSClient: on_candle callback error: %s", cb_exc)

                # Heartbeat logging
                now = time.time()
                if now - last_heartbeat > _HEARTBEAT_INTERVAL_S:
                    logger.debug(
                        "WSClient: %s alive — %d msgs, lat=%.1fms, gaps=%d",
                        symbol, self._metrics.messages_received,
                        self._metrics.last_latency_ms, self._metrics.gaps_detected,
                    )
                    last_heartbeat = now

            except asyncio.CancelledError:
                raise  # Propagate cancellation
            except Exception as exc:
                logger.warning("WSClient: watch_ohlcv error for %s: %s", symbol, exc)
                raise  # Let _stream_all handle retry

    # ── Internal helpers ───────────────────────────────────────

    def _set_state(self, new_state: WSState) -> None:
        old = self._state
        self._state = new_state
        if old != new_state:
            logger.info("WSClient: state %s → %s", old.value, new_state.value)
            if self._on_state_change:
                try:
                    self._on_state_change(new_state)
                except Exception:
                    pass  # Never let callback errors affect state machine
