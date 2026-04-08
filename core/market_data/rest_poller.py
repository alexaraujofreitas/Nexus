# ============================================================
# NEXUS TRADER — REST Poller  (Phase 3, Module 3.1)
#
# Pure-Python REST polling fallback for market data.
# ZERO PySide6 imports.  Uses threading.Thread + threading.Event.
#
# Activated when:
#   1. WebSocket client enters FAILED state
#   2. ccxt.pro is not installed
#   3. Config has websocket_enabled: false
#
# Features:
#   - Configurable poll interval (default 3s for tickers, 10s for OHLCV)
#   - Gap detection via timestamp continuity checks
#   - Latency tracking per poll cycle
#   - Graceful shutdown via threading.Event
#   - Backfill support: fetches missing candles on startup / after gap
#   - Publishes normalized candle data via callback (same interface as ws_client)
# ============================================================
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Optional, Callable

logger = logging.getLogger(__name__)


# ── Constants ──────────────────────────────────────────────────
_DEFAULT_OHLCV_INTERVAL_S = 10.0    # Poll OHLCV every 10s
_DEFAULT_TICKER_INTERVAL_S = 3.0    # Poll tickers every 3s
_BACKFILL_LIMIT = 500               # Max candles to request for backfill
_TF_MS = {
    "1m": 60_000, "3m": 180_000, "5m": 300_000,
    "15m": 900_000, "30m": 1_800_000, "1h": 3_600_000,
    "2h": 7_200_000, "4h": 14_400_000, "1d": 86_400_000,
}


class PollerState:
    IDLE = "idle"
    RUNNING = "running"
    STOPPED = "stopped"


@dataclass
class PollerMetrics:
    """Metrics for REST polling health monitoring."""
    polls_completed: int = 0
    polls_failed: int = 0
    last_poll_at: float = 0.0           # epoch seconds
    last_poll_latency_ms: float = 0.0
    avg_poll_latency_ms: float = 0.0
    _latency_sum: float = 0.0
    _latency_count: int = 0
    candles_received: int = 0
    gaps_detected: int = 0
    backfills_triggered: int = 0

    def record_poll(self, latency_ms: float, candle_count: int) -> None:
        self.polls_completed += 1
        self.last_poll_at = time.time()
        self.last_poll_latency_ms = latency_ms
        self._latency_sum += latency_ms
        self._latency_count += 1
        self.avg_poll_latency_ms = self._latency_sum / self._latency_count
        self.candles_received += candle_count

    def record_failure(self) -> None:
        self.polls_failed += 1

    def record_gap(self) -> None:
        self.gaps_detected += 1

    def record_backfill(self) -> None:
        self.backfills_triggered += 1

    def snapshot(self) -> dict:
        return {
            "polls_completed": self.polls_completed,
            "polls_failed": self.polls_failed,
            "last_poll_at": self.last_poll_at,
            "last_poll_latency_ms": round(self.last_poll_latency_ms, 2),
            "avg_poll_latency_ms": round(self.avg_poll_latency_ms, 2),
            "candles_received": self.candles_received,
            "gaps_detected": self.gaps_detected,
            "backfills_triggered": self.backfills_triggered,
        }


# Same callback type as ws_client.py for uniform interface
RawCandleCallback = Callable[[str, str, list], None]


class RESTPoller(threading.Thread):
    """
    Pure-Python REST polling fallback for 1m OHLCV candle data.

    Uses ExchangeManager.fetch_ohlcv() in a polling loop on a
    dedicated daemon thread.

    Lifecycle::

        poller = RESTPoller(exchange_manager, symbols, on_candle=callback)
        poller.start()       # non-blocking
        ...
        poller.stop()        # signals shutdown, joins thread

    The ``on_candle`` callback has the same signature as WSClient's:
    ``callback(symbol, timeframe, candle_list)`` where candle_list is
    ``[[ts_ms, o, h, l, c, v], ...]``
    """

    def __init__(
        self,
        exchange_manager,
        symbols: list[str],
        on_candle: Optional[RawCandleCallback] = None,
        on_ticker: Optional[Callable[[dict], None]] = None,
        timeframe: str = "1m",
        ohlcv_interval_s: float = _DEFAULT_OHLCV_INTERVAL_S,
        ticker_interval_s: float = _DEFAULT_TICKER_INTERVAL_S,
    ):
        super().__init__(name="RESTPoller", daemon=True)
        self._em = exchange_manager
        self._symbols = list(symbols)
        self._timeframe = timeframe
        self._on_candle = on_candle
        self._on_ticker = on_ticker
        self._ohlcv_interval_s = ohlcv_interval_s
        self._ticker_interval_s = ticker_interval_s

        self._stop_event = threading.Event()
        self._state = PollerState.IDLE
        self._metrics = PollerMetrics()
        self._lock = threading.Lock()

        # Per-symbol last-seen candle timestamp for gap detection
        self._last_ts: dict[str, int] = {}  # symbol → last candle timestamp (ms)

    # ── Public API ─────────────────────────────────────────────

    @property
    def state(self) -> str:
        return self._state

    @property
    def metrics(self) -> PollerMetrics:
        return self._metrics

    def get_symbols(self) -> list[str]:
        with self._lock:
            return list(self._symbols)

    def set_symbols(self, symbols: list[str]) -> None:
        with self._lock:
            self._symbols = list(symbols)
        logger.info("RESTPoller: symbols updated → %d symbols", len(symbols))

    def stop(self) -> None:
        logger.info("RESTPoller: stop requested")
        self._stop_event.set()
        if self.is_alive():
            self.join(timeout=10.0)
        self._state = PollerState.STOPPED

    # ── Thread entry ───────────────────────────────────────────

    def run(self) -> None:
        logger.info("RESTPoller: thread started for %d symbols, tf=%s, interval=%.1fs",
                     len(self._symbols), self._timeframe, self._ohlcv_interval_s)
        self._state = PollerState.RUNNING

        try:
            self._poll_loop()
        except Exception as exc:
            logger.error("RESTPoller: poll loop crashed: %s", exc, exc_info=True)
        finally:
            self._state = PollerState.STOPPED
            logger.info("RESTPoller: thread exited")

    # ── Core poll loop ─────────────────────────────────────────

    def _poll_loop(self) -> None:
        """
        Main polling loop.  Each cycle:
        1. Fetch latest 1m candles for all symbols
        2. Detect gaps and trigger backfill if needed
        3. Deliver candles via callback
        4. Sleep until next interval (interruptible via stop_event)
        """
        # Initial backfill: fetch recent history for all symbols
        self._initial_backfill()

        last_ticker_poll = 0.0

        while not self._stop_event.is_set():
            cycle_start = time.time()

            with self._lock:
                symbols = list(self._symbols)

            if not symbols:
                self._stop_event.wait(timeout=self._ohlcv_interval_s)
                continue

            # ── OHLCV poll ─────────────────────────────────────
            for symbol in symbols:
                if self._stop_event.is_set():
                    return
                self._poll_symbol(symbol)

            # ── Ticker poll (less critical, separate cadence) ──
            now = time.time()
            if self._on_ticker and (now - last_ticker_poll) >= self._ticker_interval_s:
                self._poll_tickers(symbols)
                last_ticker_poll = time.time()

            # ── Sleep until next cycle ─────────────────────────
            elapsed = time.time() - cycle_start
            sleep_s = max(0.1, self._ohlcv_interval_s - elapsed)
            self._stop_event.wait(timeout=sleep_s)

    def _poll_symbol(self, symbol: str) -> None:
        """Fetch latest 1m candles for a single symbol."""
        try:
            t0 = time.time()
            # Fetch last 5 candles to handle slight delays
            candles = self._em.fetch_ohlcv(
                symbol, self._timeframe, limit=5,
            )
            latency_ms = (time.time() - t0) * 1000

            if not candles:
                self._metrics.record_failure()
                return

            self._metrics.record_poll(latency_ms, len(candles))

            # ── Gap detection ──────────────────────────────────
            if symbol in self._last_ts:
                step_ms = _TF_MS.get(self._timeframe, 60_000)
                oldest_new = candles[0][0]
                last_known = self._last_ts[symbol]

                if last_known > 0 and oldest_new > last_known + step_ms:
                    gap_candles = (oldest_new - last_known) // step_ms - 1
                    if gap_candles > 0:
                        self._metrics.record_gap()
                        logger.warning(
                            "RESTPoller: gap for %s — %d missing candle(s) "
                            "between %d and %d; triggering backfill",
                            symbol, gap_candles, last_known, oldest_new,
                        )
                        self._backfill_symbol(symbol, last_known + step_ms, oldest_new)

            # Update last seen
            if candles:
                self._last_ts[symbol] = candles[-1][0]

            # Deliver via callback
            if self._on_candle and candles:
                try:
                    self._on_candle(symbol, self._timeframe, candles)
                except Exception as cb_exc:
                    logger.error("RESTPoller: on_candle callback error: %s", cb_exc)

        except Exception as exc:
            self._metrics.record_failure()
            logger.warning("RESTPoller: poll error for %s: %s", symbol, exc)

    def _poll_tickers(self, symbols: list[str]) -> None:
        """Fetch tickers for all symbols in one call."""
        try:
            tickers = self._em.fetch_tickers(symbols[:50])
            if tickers and self._on_ticker:
                try:
                    self._on_ticker(tickers)
                except Exception as cb_exc:
                    logger.error("RESTPoller: on_ticker callback error: %s", cb_exc)
        except Exception as exc:
            logger.warning("RESTPoller: ticker poll error: %s", exc)

    # ── Backfill ───────────────────────────────────────────────

    def _initial_backfill(self) -> None:
        """
        On startup, fetch recent candle history for all symbols
        so the CandleBuilder has enough data for TF derivation.
        """
        with self._lock:
            symbols = list(self._symbols)

        logger.info("RESTPoller: initial backfill for %d symbols", len(symbols))

        for symbol in symbols:
            if self._stop_event.is_set():
                return
            try:
                candles = self._em.fetch_ohlcv(
                    symbol, self._timeframe, limit=_BACKFILL_LIMIT,
                )
                if candles:
                    self._last_ts[symbol] = candles[-1][0]
                    self._metrics.record_backfill()
                    if self._on_candle:
                        self._on_candle(symbol, self._timeframe, candles)
                    logger.info(
                        "RESTPoller: backfilled %d candles for %s",
                        len(candles), symbol,
                    )
                else:
                    logger.warning("RESTPoller: backfill returned 0 candles for %s", symbol)
            except Exception as exc:
                logger.warning("RESTPoller: backfill error for %s: %s", symbol, exc)

    def _backfill_symbol(self, symbol: str, since_ms: int, until_ms: int) -> None:
        """
        Backfill missing candles for a specific time range.
        Called when a gap is detected during normal polling.
        """
        try:
            step_ms = _TF_MS.get(self._timeframe, 60_000)
            needed = (until_ms - since_ms) // step_ms
            limit = min(int(needed) + 5, _BACKFILL_LIMIT)

            candles = self._em.fetch_ohlcv(
                symbol, self._timeframe, since=since_ms, limit=limit,
            )
            if candles:
                self._metrics.record_backfill()
                if self._on_candle:
                    self._on_candle(symbol, self._timeframe, candles)
                logger.info(
                    "RESTPoller: backfilled %d candles for %s (gap fill)",
                    len(candles), symbol,
                )
        except Exception as exc:
            logger.warning("RESTPoller: gap backfill error for %s: %s", symbol, exc)
