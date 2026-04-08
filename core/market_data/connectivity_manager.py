# ============================================================
# NEXUS TRADER — Connectivity Manager  (Phase 3, Module 3.1)
#
# Orchestrates WebSocket + REST data sources with automatic
# failover.  Pure Python — ZERO PySide6 imports.
#
# Responsibilities:
#   - Decides primary data source (WS vs REST) from config
#   - Manages WS → REST failover on WS failure
#   - Publishes raw candle data to EventBus (CONNECTIVITY layer)
#   - Exposes unified metrics from both sources
#   - Handles symbol subscription changes
#
# Replaces: data_feed.py (PySide6/QThread) for headless path.
# data_feed.py remains available for GUI mode.
# ============================================================
from __future__ import annotations

import logging
import threading
import time
from typing import Optional, Callable

from core.event_bus import bus, Topics
from core.market_data.ws_client import WebSocketClient, WSState, WSMetrics
from core.market_data.rest_poller import RESTPoller, PollerMetrics

logger = logging.getLogger(__name__)


class FeedSource:
    """Active data source identifier."""
    WEBSOCKET = "websocket"
    REST = "rest"
    NONE = "none"


class ConnectivityManager:
    """
    Orchestrates WS and REST data sources for the intraday data engine.

    Usage::

        cm = ConnectivityManager(exchange_manager, symbols=["BTC/USDT", "ETH/USDT"])
        cm.start()
        ...
        cm.stop()

    On start:
      - If websocket_enabled=True and ccxt.pro available → start WSClient
      - Otherwise → start RESTPoller directly
      - If WSClient enters FAILED state → auto-start RESTPoller

    All raw candle data flows through ``_on_raw_candle()`` regardless
    of source, providing a single integration point for Module 3.2
    (Data Engine).
    """

    def __init__(
        self,
        exchange_manager,
        symbols: list[str],
        ws_enabled: bool = True,
        ohlcv_interval_s: float = 10.0,
        ticker_interval_s: float = 3.0,
        on_candle: Optional[Callable[[str, str, list], None]] = None,
        on_ticker: Optional[Callable[[dict], None]] = None,
    ):
        self._em = exchange_manager
        self._symbols = list(symbols)
        self._ws_enabled = ws_enabled
        self._ohlcv_interval_s = ohlcv_interval_s
        self._ticker_interval_s = ticker_interval_s

        # External callbacks (used by Data Engine / Module 3.2)
        self._on_candle_ext = on_candle
        self._on_ticker_ext = on_ticker

        # Data sources
        self._ws_client: Optional[WebSocketClient] = None
        self._rest_poller: Optional[RESTPoller] = None
        self._active_source = FeedSource.NONE
        self._lock = threading.Lock()
        self._started = False

    # ── Public API ─────────────────────────────────────────────

    @property
    def active_source(self) -> str:
        return self._active_source

    @property
    def ws_metrics(self) -> Optional[WSMetrics]:
        return self._ws_client.metrics if self._ws_client else None

    @property
    def rest_metrics(self) -> Optional[PollerMetrics]:
        return self._rest_poller.metrics if self._rest_poller else None

    def get_metrics_snapshot(self) -> dict:
        """Combined metrics from active source."""
        result = {"active_source": self._active_source}
        if self._ws_client:
            result["ws"] = self._ws_client.metrics.snapshot()
        if self._rest_poller:
            result["rest"] = self._rest_poller.metrics.snapshot()
        return result

    def set_symbols(self, symbols: list[str]) -> None:
        """Update symbols on active data source."""
        self._symbols = list(symbols)
        if self._ws_client:
            self._ws_client.set_symbols(symbols)
        if self._rest_poller:
            self._rest_poller.set_symbols(symbols)

    def start(self) -> None:
        """Start the connectivity layer."""
        if self._started:
            logger.warning("ConnectivityManager: already started")
            return

        self._started = True
        logger.info(
            "ConnectivityManager: starting — ws_enabled=%s, %d symbols",
            self._ws_enabled, len(self._symbols),
        )

        # Determine if WS is viable
        ws_viable = self._ws_enabled and self._check_ws_available()

        if ws_viable:
            self._start_ws()
        else:
            self._start_rest()

        bus.publish(Topics.FEED_STATUS, {
            "active": True,
            "mode": self._active_source,
            "symbols": len(self._symbols),
        }, source="connectivity_manager")

    def stop(self) -> None:
        """Stop all data sources."""
        logger.info("ConnectivityManager: stopping")
        self._started = False

        if self._ws_client:
            self._ws_client.stop()
            self._ws_client = None

        if self._rest_poller:
            self._rest_poller.stop()
            self._rest_poller = None

        self._active_source = FeedSource.NONE

        bus.publish(Topics.FEED_STATUS, {
            "active": False,
            "mode": "none",
        }, source="connectivity_manager")
        logger.info("ConnectivityManager: stopped")

    # ── Internal: source management ────────────────────────────

    def _check_ws_available(self) -> bool:
        """Check if ccxt.pro WS exchange is available."""
        if not self._em:
            return False
        ws_ex = self._em.get_ws_exchange()
        return ws_ex is not None and callable(getattr(ws_ex, "watch_ohlcv", None))

    def _start_ws(self) -> None:
        """Start WebSocket client as primary source."""
        logger.info("ConnectivityManager: starting WebSocket client")
        self._ws_client = WebSocketClient(
            exchange_manager=self._em,
            symbols=self._symbols,
            on_candle=self._on_raw_candle,
            on_state_change=self._on_ws_state_change,
            timeframe="1m",
        )
        self._ws_client.start()
        self._active_source = FeedSource.WEBSOCKET

    def _start_rest(self) -> None:
        """Start REST poller (either as primary or as failover)."""
        if self._rest_poller and self._rest_poller.is_alive():
            logger.debug("ConnectivityManager: REST poller already running")
            return

        logger.info("ConnectivityManager: starting REST poller")
        self._rest_poller = RESTPoller(
            exchange_manager=self._em,
            symbols=self._symbols,
            on_candle=self._on_raw_candle,
            on_ticker=self._on_raw_ticker,
            timeframe="1m",
            ohlcv_interval_s=self._ohlcv_interval_s,
            ticker_interval_s=self._ticker_interval_s,
        )
        self._rest_poller.start()
        self._active_source = FeedSource.REST

    def _on_ws_state_change(self, new_state: WSState) -> None:
        """
        Handle WebSocket state transitions.
        If WS enters FAILED state, automatically switch to REST.
        """
        if new_state == WSState.FAILED:
            logger.warning(
                "ConnectivityManager: WS client FAILED — activating REST fallback"
            )
            bus.publish(Topics.FEED_STATUS, {
                "mode": "rest_fallback",
                "reason": "WebSocket failures exceeded limit",
            }, source="connectivity_manager")
            self._start_rest()

        elif new_state == WSState.STREAMING:
            bus.publish(Topics.FEED_STATUS, {
                "mode": "websocket",
                "active": True,
            }, source="connectivity_manager")

    # ── Unified data callbacks ─────────────────────────────────

    def _on_raw_candle(self, symbol: str, timeframe: str, candle_list: list) -> None:
        """
        Central candle data handler. Receives raw candles from either
        WS or REST source and:
        1. Publishes RAW OHLCV to EventBus (CONNECTIVITY layer — raw transport)
        2. Forwards to external callback (Data Engine for normalization/validation)

        NOTE: ConnectivityManager publishes ONLY raw transport topics
        (OHLCV_UPDATE, TICK_UPDATE). Normalized candle topics (CANDLE_1M..1H)
        are published by CandleBuilder in the DATA layer. This separation is
        enforced by TOPIC_LAYER_OWNERSHIP in contracts.py.
        """
        # Publish RAW OHLCV to EventBus (connectivity layer topic)
        if candle_list:
            bus.publish(Topics.OHLCV_UPDATE, {
                "symbol": symbol,
                "timeframe": timeframe,
                "candles": candle_list,
                "source": self._active_source,
                "received_at": time.time(),
            }, source="connectivity_manager")

        # Forward to Data Engine callback
        if self._on_candle_ext:
            try:
                self._on_candle_ext(symbol, timeframe, candle_list)
            except Exception as exc:
                logger.error("ConnectivityManager: external candle callback error: %s", exc)

    def _on_raw_ticker(self, tickers: dict) -> None:
        """Forward ticker data to EventBus and external callback."""
        if tickers:
            bus.publish(Topics.TICK_UPDATE, tickers, source="connectivity_manager")

        if self._on_ticker_ext:
            try:
                self._on_ticker_ext(tickers)
            except Exception as exc:
                logger.error("ConnectivityManager: external ticker callback error: %s", exc)
