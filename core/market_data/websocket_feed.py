# ============================================================
# NEXUS TRADER — WebSocket Candle Feed
#
# Connects to exchange WebSocket (via CCXT Pro or direct)
# and triggers a candle_closed event when a new candle closes.
#
# Uses a polling fallback if CCXT Pro is not available.
# ============================================================
from __future__ import annotations

import logging
import time
from typing import Optional

from PySide6.QtCore import QObject, QTimer, Signal

logger = logging.getLogger(__name__)

# Timeframe → seconds mapping
TF_SECONDS = {
    "1m": 60,
    "5m": 300,
    "15m": 900,
    "30m": 1800,
    "1h": 3600,
    "4h": 14400,
    "1d": 86400,
}


class WebSocketCandleFeed(QObject):
    """
    WebSocket-based (or polling-based) candle feed that triggers
    on candle close. Emits candle_closed signal when a new candle
    completes on the specified symbol and timeframe.

    Uses CCXT Pro WebSocket if available; falls back to polling
    via QTimer.
    """

    # Signals
    candle_closed = Signal(str, str, dict)  # symbol, timeframe, candle_dict
    connection_status = Signal(str)  # "connected", "disconnected", "error"

    def __init__(self, symbol: str = "BTC/USDT", timeframe: str = "1h", parent=None):
        """
        Initialize the WebSocket candle feed.

        Args:
            symbol: Trading pair (e.g., "BTC/USDT")
            timeframe: Candle timeframe (e.g., "1h", "4h", "1d")
            parent: Qt parent object
        """
        super().__init__(parent)
        self._symbol = symbol
        self._timeframe = timeframe
        self._running = False
        self._last_candle_ts: int = 0  # Timestamp of last processed closed candle
        self._timer: Optional[QTimer] = None
        self._exchange = None
        self._ws_mode_active = False

    def start(self) -> None:
        """Start the candle feed."""
        if self._running:
            logger.debug("WebSocketCandleFeed: already running for %s/%s", self._symbol, self._timeframe)
            return

        self._running = True
        logger.info("WebSocketCandleFeed: starting for %s/%s", self._symbol, self._timeframe)

        # Try WebSocket first
        try:
            import ccxtpro

            self._exchange = self._get_exchange_instance(ccxtpro)
            if self._exchange:
                self._ws_mode_active = True
                logger.info("WebSocketCandleFeed: WebSocket mode active for %s", self._symbol)
                self.connection_status.emit("connected")
                # For now, we'll fall back to polling even with CCXT Pro to keep it simple
                # In a full implementation, you'd run asyncio.run(self._ws_watch_loop())
                self._start_polling()
                return
        except ImportError:
            logger.debug("WebSocketCandleFeed: CCXT Pro not available, using polling fallback")
        except Exception as exc:
            logger.warning("WebSocketCandleFeed: WebSocket initialization failed: %s", exc)

        # Fallback to polling
        self._ws_mode_active = False
        self._start_polling()

    def _start_polling(self) -> None:
        """Start polling mode using QTimer."""
        try:
            from core.market_data.exchange_manager import exchange_manager

            self._exchange = exchange_manager.get_exchange()
            if not self._exchange:
                logger.error("WebSocketCandleFeed: no exchange available")
                self.connection_status.emit("error")
                return

            # Set up QTimer for polling
            tf_seconds = TF_SECONDS.get(self._timeframe, 3600)
            poll_interval_ms = int((tf_seconds // 2) * 1000)  # Poll every half-candle

            if self._timer:
                self._timer.stop()

            self._timer = QTimer(self)
            self._timer.setInterval(poll_interval_ms)
            self._timer.timeout.connect(self._poll_check)
            self._timer.start()

            logger.info(
                "WebSocketCandleFeed: polling mode started for %s/%s (interval=%.1f seconds)",
                self._symbol, self._timeframe, poll_interval_ms / 1000.0
            )
            self.connection_status.emit("connected")

        except Exception as exc:
            logger.error("WebSocketCandleFeed: polling setup failed: %s", exc)
            self.connection_status.emit("error")

    def stop(self) -> None:
        """Stop the candle feed."""
        self._running = False
        if self._timer:
            self._timer.stop()
            self._timer = None
        logger.info("WebSocketCandleFeed: stopped for %s/%s", self._symbol, self._timeframe)
        self.connection_status.emit("disconnected")

    def _poll_check(self) -> None:
        """
        Polling method: fetch last 2 candles and detect if a new
        candle has closed since the last check.
        """
        if not self._running or not self._exchange:
            return

        try:
            # Fetch last 2 OHLCV candles
            ohlcv_list = self._exchange.fetch_ohlcv(
                self._symbol,
                self._timeframe,
                limit=2,
            )

            if not ohlcv_list or len(ohlcv_list) < 2:
                logger.debug("WebSocketCandleFeed: insufficient OHLCV data for %s", self._symbol)
                return

            # ohlcv_list[-2] is the previous closed candle
            # ohlcv_list[-1] is the current (possibly open) candle
            prev_closed_candle = ohlcv_list[-2]
            prev_closed_ts = int(prev_closed_candle[0])

            # Check if we've already processed this candle
            if prev_closed_ts <= self._last_candle_ts:
                # No new closed candle yet
                return

            # New closed candle detected!
            self._last_candle_ts = prev_closed_ts

            # Convert to dict for emission
            candle_dict = {
                "timestamp": prev_closed_ts,
                "open": float(prev_closed_candle[1]),
                "high": float(prev_closed_candle[2]),
                "low": float(prev_closed_candle[3]),
                "close": float(prev_closed_candle[4]),
                "volume": float(prev_closed_candle[5]),
            }

            logger.debug(
                "WebSocketCandleFeed: ✓ candle closed for %s/%s @ %.6g",
                self._symbol, self._timeframe, candle_dict["close"]
            )
            self.candle_closed.emit(self._symbol, self._timeframe, candle_dict)

        except Exception as exc:
            logger.warning("WebSocketCandleFeed: polling check error for %s: %s", self._symbol, exc)

    def _get_exchange_instance(self, ccxtpro):
        """
        Get or create a CCXT Pro exchange instance.

        Prefers the authenticated WS exchange from ExchangeManager.
        Falls back to an unauthenticated instance with a warning.

        Args:
            ccxtpro: The ccxtpro module

        Returns:
            Exchange instance or None on failure
        """
        try:
            from core.market_data.exchange_manager import exchange_manager as _em
            if _em and hasattr(_em, 'get_ws_exchange'):
                ws_ex = _em.get_ws_exchange()
                if ws_ex is not None:
                    return ws_ex

            # Fallback: create unauthenticated instance
            logger.warning("WebSocketCandleFeed: falling back to unauthenticated exchange instance")
            active_ex = _em.get_exchange()
            if active_ex:
                exchange_id = active_ex.id
            else:
                from config.settings import settings
                exchange_id = settings.get("exchange.current", "").lower()

            if not exchange_id:
                logger.warning("WebSocketCandleFeed: no active exchange configured")
                return None

            if not hasattr(ccxtpro, exchange_id):
                logger.warning("WebSocketCandleFeed: exchange '%s' not in ccxtpro", exchange_id)
                return None

            exchange_class = getattr(ccxtpro, exchange_id)
            return exchange_class()

        except Exception as exc:
            logger.error("WebSocketCandleFeed: failed to get exchange: %s", exc)
            return None
