# ============================================================
# NEXUS TRADER — Live Data Feed (WebSocket + REST Fallback)
# Dual-mode market data feed with automatic fallback strategy
# ============================================================

import asyncio
import logging
import time
from typing import Optional, Dict, List
from enum import Enum

from PySide6.QtCore import QThread, QTimer, Signal

from core.event_bus import bus, Topics
from config.settings import settings

logger = logging.getLogger(__name__)

# ── WebSocket Availability Detection ────────────────────────
try:
    import ccxtpro
    _WS_AVAILABLE = True
except ImportError:
    try:
        import ccxt.pro as ccxtpro
        _WS_AVAILABLE = True
    except ImportError:
        _WS_AVAILABLE = False


class FeedMode(Enum):
    """Enumeration of data feed operational modes."""
    WEBSOCKET = "websocket"
    REST_POLLING = "rest_polling"
    REST_FALLBACK = "rest_fallback"


class LiveDataFeed(QThread):
    """
    Dual-mode market data feed with automatic fallback:

    Primary: CCXT Pro WebSocket (watch_ticker, watch_ohlcv) if available
    Fallback: REST polling every N seconds (default 3s) if WS unavailable or fails

    Auto-detects whether ccxtpro is available. If not, falls back to REST.
    On WebSocket disconnect: reconnects with exponential backoff.
    After 5 consecutive WS failures: switches to REST mode permanently for this session.

    Published events:
    - Topics.TICK_UPDATE: always on price update
    - Topics.OHLCV_UPDATE: on new OHLCV bar
    - Topics.FEED_STATUS: when mode changes (ws→rest or rest→ws)
    """

    tick_received = Signal(dict)   # {symbol: {last, change, volume, ...}}
    feed_mode_changed = Signal(str)  # "websocket" | "rest_polling" | "rest_fallback"

    def __init__(self, exchange_manager=None, symbols: List[str] = None, timeframes: List[str] = None):
        super().__init__()
        self._exchange_manager = exchange_manager
        self._symbols = symbols or []
        self._timeframes = timeframes or ["1h"]
        self._running = False
        self._timer: Optional[QTimer] = None

        # WebSocket mode state
        self._feed_mode = FeedMode.REST_POLLING
        self._ws_failures = 0
        self._ws_backoff_delays = [1, 2, 4, 8, 16, 32]  # exponential backoff in seconds
        self._ws_backoff_idx = 0
        self._last_ws_latency_ms = 0.0
        self._last_tick_time = 0.0

        # Settings
        self._ws_enabled = settings.get("data.websocket_enabled", True)
        self._interval_seconds = settings.get("data.feed_interval_seconds", 3)
        self._ws_reconnect_attempts = settings.get("data.ws_reconnect_attempts", 5)

    def set_symbols(self, symbols: List[str]):
        """Update active symbols (thread-safe)."""
        self._symbols = symbols
        logger.info("LiveDataFeed symbols updated: %d symbols", len(symbols))

    def set_timeframes(self, timeframes: List[str]):
        """Update active timeframes."""
        self._timeframes = timeframes or ["1h"]

    def get_feed_mode(self) -> str:
        """Return current feed mode: 'websocket', 'rest_polling', or 'rest_fallback'."""
        return self._feed_mode.value

    def get_latency_ms(self) -> float:
        """Return last measured round-trip latency in milliseconds."""
        return self._last_ws_latency_ms

    def start_feed(self):
        """Start the data feed thread."""
        if not self._running:
            self._running = True
            self.start()
            logger.info("Live data feed started — %d symbols, WS available: %s",
                        len(self._symbols), _WS_AVAILABLE)

    def stop_feed(self):
        """Stop the data feed thread cleanly."""
        self._running = False
        if self._timer:
            self._timer.stop()
        self.quit()
        self.wait(3000)
        bus.publish(Topics.FEED_STATUS, {
            "active": False,
            "mode": self._feed_mode.value
        }, source="data_feed")
        logger.info("Live data feed stopped")

    def run(self):
        """Qt thread entry — dispatcher to WebSocket or REST loop."""
        # Determine which mode to use
        should_use_ws = (
            _WS_AVAILABLE
            and self._ws_enabled
            and self._feed_mode != FeedMode.REST_FALLBACK
        )

        if should_use_ws:
            logger.info("Starting WebSocket feed loop")
            self._run_ws_loop()
        else:
            logger.info("Starting REST polling feed loop")
            self._run_rest_loop()

    def _run_ws_loop(self):
        """
        Async event loop for WebSocket subscriptions.
        On failure, increments backoff, sleeps, retries.
        After 5 failures, switches to REST permanently.
        """
        # Prefer the dedicated ccxt.pro WS instance; fall back to the REST
        # instance (which may not have watch_ticker and will fail the check below).
        exchange = None
        if self._exchange_manager:
            if hasattr(self._exchange_manager, "get_ws_exchange"):
                exchange = self._exchange_manager.get_ws_exchange()
            if exchange is None:
                exchange = getattr(self._exchange_manager, "_exchange", None)
        if not exchange or not callable(getattr(exchange, "watch_ticker", None)):
            logger.warning("Exchange does not support WebSocket watch_ticker; switching to REST")
            self._switch_to_rest_fallback()
            return

        try:
            asyncio.run(self._ws_loop())
        except Exception as e:
            logger.error("WebSocket loop crashed: %s", e)

        # After asyncio.run() completes (WS loop exited), switch to REST if not already
        if self._feed_mode != FeedMode.REST_FALLBACK:
            self._switch_to_rest_fallback()
        else:
            self._run_rest_loop()

    async def _ws_loop(self):
        """
        Main WebSocket loop:
        - Subscribes to watch_ticker and watch_ohlcv
        - Handles incoming updates
        - Manages reconnection with backoff
        """
        if not self._exchange_manager:
            logger.warning("Exchange manager not available; falling back to REST")
            return

        # Prefer the dedicated ccxt.pro WS instance for WebSocket subscriptions.
        # Fall back to the private _exchange attribute only if get_ws_exchange()
        # is unavailable (e.g. during unit tests with a mock exchange_manager).
        if hasattr(self._exchange_manager, "get_ws_exchange"):
            exchange = self._exchange_manager.get_ws_exchange()
        else:
            exchange = None
        if exchange is None:
            exchange = getattr(self._exchange_manager, "_exchange", None)
        if not exchange:
            logger.warning("Cannot access exchange object; falling back to REST")
            return

        # Signal that WS feed is active
        bus.publish(Topics.FEED_STATUS, {
            "mode": "websocket",
            "active": True
        }, source="data_feed")

        while self._running and self._feed_mode != FeedMode.REST_FALLBACK:
            try:
                self._ws_backoff_idx = 0  # reset on successful connection
                tasks = []

                # Subscribe to ticker updates for all symbols
                for symbol in self._symbols:
                    tasks.append(self._watch_ticker_task(exchange, symbol))

                # Subscribe to OHLCV updates for all symbols and timeframes
                for symbol in self._symbols:
                    for tf in self._timeframes:
                        tasks.append(self._watch_ohlcv_task(exchange, symbol, tf))

                # Run all subscriptions concurrently
                if tasks:
                    results = await asyncio.gather(*tasks, return_exceptions=True)
                    # Count task-level failures toward WS failure limit
                    failures = sum(1 for r in results if isinstance(r, Exception))
                    if failures > 0:
                        self._ws_failures += failures
                        logger.warning("WebSocket: %d task(s) failed (total failures: %d/%d)",
                                      failures, self._ws_failures, self._ws_reconnect_attempts)

            except Exception as e:
                self._ws_failures += 1
                logger.warning("WebSocket error (attempt %d/%d): %s",
                              self._ws_failures, self._ws_reconnect_attempts, e)

            if self._ws_failures >= self._ws_reconnect_attempts:
                logger.error("WebSocket failed %d times; switching to REST fallback",
                            self._ws_reconnect_attempts)
                self._feed_mode = FeedMode.REST_FALLBACK
                bus.publish(Topics.FEED_STATUS, {
                    "mode": "rest_fallback",
                    "reason": "WebSocket failures exceeded limit"
                }, source="data_feed")
                return  # Exit async loop cleanly; _run_ws_loop() will start REST

            if self._running and self._feed_mode != FeedMode.REST_FALLBACK:
                # Exponential backoff before retry
                backoff_secs = self._ws_backoff_delays[
                    min(self._ws_backoff_idx, len(self._ws_backoff_delays) - 1)
                ]
                self._ws_backoff_idx += 1
                logger.info("WebSocket reconnecting in %d seconds...", backoff_secs)
                await asyncio.sleep(backoff_secs)

    async def _watch_ticker_task(self, exchange, symbol: str):
        """Watch ticker stream for a symbol."""
        try:
            while self._running and self._feed_mode != FeedMode.REST_FALLBACK:
                ticker = await exchange.watch_ticker(symbol)
                self._process_ticker(symbol, ticker)
                await asyncio.sleep(0.1)  # small delay to avoid overwhelming
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.debug("Ticker watch error for %s: %s", symbol, e)
            raise  # Re-raise so gather() sees it as a failure and counts it

    async def _watch_ohlcv_task(self, exchange, symbol: str, timeframe: str):
        """Watch OHLCV stream for a symbol and timeframe."""
        try:
            while self._running and self._feed_mode != FeedMode.REST_FALLBACK:
                ohlcv_list = await exchange.watch_ohlcv(symbol, timeframe)
                if ohlcv_list and len(ohlcv_list) > 0:
                    latest_bar = ohlcv_list[-1]
                    self._process_ohlcv(symbol, timeframe, latest_bar)
                await asyncio.sleep(0.1)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.debug("OHLCV watch error for %s/%s: %s", symbol, timeframe, e)
            raise  # Re-raise so gather() sees it as a failure and counts it

    def _switch_to_rest_fallback(self):
        """Switch to REST polling permanently (called from thread context, not from asyncio)."""
        self._feed_mode = FeedMode.REST_FALLBACK
        bus.publish(Topics.FEED_STATUS, {
            "mode": "rest_fallback",
            "reason": "WebSocket not available or failed"
        }, source="data_feed")
        logger.warning("Switched to REST fallback mode")
        self._run_rest_loop()

    def _run_rest_loop(self):
        """
        REST polling loop using QTimer.
        Fallback to previous polling behavior.
        """
        self._feed_mode = FeedMode.REST_POLLING
        bus.publish(Topics.FEED_STATUS, {
            "mode": "rest_polling",
            "active": True
        }, source="data_feed")

        # Create timer in this thread context
        self._timer = QTimer()
        self._timer.setInterval(int(self._interval_seconds * 1000))
        self._timer.timeout.connect(self._poll_rest)
        self._timer.start()

        # Immediate first poll
        self._poll_rest()

        # Block on Qt event loop
        self.exec()

    def _poll_rest(self):
        """REST polling implementation — fetches tickers."""
        if not self._symbols or not self._exchange_manager:
            return

        if not self._exchange_manager.is_connected():
            return

        try:
            start_time = time.time()
            tickers = self._exchange_manager.fetch_tickers(self._symbols[:50])
            elapsed_ms = (time.time() - start_time) * 1000
            self._last_ws_latency_ms = elapsed_ms

            if tickers:
                self.tick_received.emit(tickers)
                bus.publish(Topics.TICK_UPDATE, tickers, source="data_feed")

                # Notify PaperExecutor for stop-loss/take-profit checks
                try:
                    from core.execution.paper_executor import paper_executor as _pe
                    for symbol, ticker in tickers.items():
                        price = ticker.get("last") or ticker.get("close")
                        if price:
                            _pe.on_tick(symbol, float(price))
                except Exception as _exc:
                    logger.debug("PaperExecutor tick update error: %s", _exc)

        except Exception as e:
            logger.warning("REST feed poll error: %s", e)
            # Retry after a longer interval on error
            self._timer.setInterval(5000)

    def _process_ticker(self, symbol: str, ticker: dict):
        """
        Normalize ticker data and publish updates.
        Publishes: TICK_UPDATE
        """
        try:
            normalized = {symbol: ticker}
            self.tick_received.emit(normalized)
            bus.publish(Topics.TICK_UPDATE, normalized, source="data_feed")

            # Update latency from timestamp
            if "timestamp" in ticker:
                elapsed_ms = time.time() * 1000 - ticker["timestamp"]
                if elapsed_ms > 0:
                    self._last_ws_latency_ms = elapsed_ms

            # Notify PaperExecutor
            try:
                from core.execution.paper_executor import paper_executor as _pe
                price = ticker.get("last") or ticker.get("close")
                if price:
                    _pe.on_tick(symbol, float(price))
            except Exception as _exc:
                logger.debug("PaperExecutor tick update error: %s", _exc)

        except Exception as e:
            logger.debug("Ticker process error for %s: %s", symbol, e)

    def _process_ohlcv(self, symbol: str, timeframe: str, ohlcv_data: list):
        """
        Process OHLCV bar data and publish updates.
        Expected format: [timestamp, open, high, low, close, volume]
        Publishes: OHLCV_UPDATE
        """
        try:
            if not ohlcv_data or len(ohlcv_data) < 6:
                return

            bar = {
                "symbol": symbol,
                "timeframe": timeframe,
                "timestamp": int(ohlcv_data[0]),
                "open": float(ohlcv_data[1]),
                "high": float(ohlcv_data[2]),
                "low": float(ohlcv_data[3]),
                "close": float(ohlcv_data[4]),
                "volume": float(ohlcv_data[5])
            }

            bus.publish(Topics.OHLCV_UPDATE, {
                "symbol": symbol,
                "timeframe": timeframe,
                "bar": bar
            }, source="data_feed")

        except Exception as e:
            logger.debug("OHLCV process error for %s/%s: %s", symbol, timeframe, e)


# Global singleton feed instance — initialized with exchange_manager
def _create_feed() -> LiveDataFeed:
    try:
        from core.market_data.exchange_manager import exchange_manager
        return LiveDataFeed(exchange_manager=exchange_manager)
    except Exception:
        return LiveDataFeed()

live_data_feed: LiveDataFeed = _create_feed()

# Alias used by dashboard and other modules
live_feed: LiveDataFeed = live_data_feed
