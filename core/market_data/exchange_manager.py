# ============================================================
# NEXUS TRADER — Exchange Manager
# Manages CCXT exchange instances, market data fetching
# ============================================================

import logging
import threading
import time as _time
from typing import Optional
from datetime import datetime, timezone

import ccxt

from core.database.engine import get_session
from core.database.models import Exchange as ExchangeModel, Asset
from core.event_bus import bus, Topics

logger = logging.getLogger(__name__)

# Exchanges that don't support sandbox via CCXT
SANDBOX_SUPPORTED = {"binance", "bybit", "okx"}


def _decrypt(value: str) -> str:
    try:
        from cryptography.fernet import Fernet
        from config.constants import DATA_DIR
        key_file = DATA_DIR / ".nexus_key"
        if not key_file.exists():
            return value
        key = key_file.read_bytes()
        return Fernet(key).decrypt(value.encode()).decode()
    except Exception:
        return value


class ExchangeManager:
    """
    Singleton managing the active CCXT exchange instance.
    Thread-safe. Publishes connectivity events on the bus.
    """

    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._initialized = False
            return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._exchange: Optional[ccxt.Exchange] = None
        self._ws_exchange: Optional[object] = None  # ccxt.pro instance for WebSocket
        self._exchange_model: Optional[ExchangeModel] = None
        self._markets: dict = {}
        self._lock = threading.RLock()
        self._last_fetch_at: float = 0.0    # epoch seconds of last successful fetch_tickers
        self._last_latency_ms: int = 0       # round-trip ms of last successful fetch_tickers
        self._mode: str = "Unknown"          # "Live", "Demo Trading", "Testnet", or "Unknown"
        self._initialized = True

    # ── Initialization ─────────────────────────────────────────
    def load_active_exchange(self) -> bool:
        """Load the active exchange from DB and build CCXT instance."""
        with get_session() as session:
            model = session.query(ExchangeModel).filter_by(is_active=True).first()
            if not model:
                logger.warning("No active exchange configured")
                return False
            # Copy data out before session closes
            exchange_data = {
                "id":           model.id,
                "exchange_id":  model.exchange_id,
                "name":         model.name,
                "sandbox":      model.sandbox_mode,
                "demo":         getattr(model, "demo_mode", False),
                "api_key":      _decrypt(model.api_key_encrypted or ""),
                "api_secret":   _decrypt(model.api_secret_encrypted or ""),
                "passphrase":   _decrypt(model.api_passphrase_encrypted or ""),
            }

        return self._build_instance(exchange_data)

    @staticmethod
    def _apply_demo_mode(exchange_obj) -> None:
        """
        Switch Bybit to its Demo Trading environment (api-demo.bybit.com).

        Bybit has THREE distinct environments:
          - Live:    api.bybit.com         (real money)
          - Testnet: api-testnet.bybit.com (CCXT sandbox=True)
          - Demo:    api-demo.bybit.com    (paper money, real market data)

        CCXT stores URL templates as 'https://api.{hostname}' — NOT the resolved
        hostname — so string replacement against 'api.bybit.com' never matches.
        CCXT already provides a dedicated 'demotrading' key in urls; we swap
        'api' to point at it so all subsequent requests hit api-demo.bybit.com.
        """
        try:
            # Preferred: CCXT built-in helper (available in some 4.x builds)
            if hasattr(exchange_obj, "enable_demo_trading"):
                exchange_obj.enable_demo_trading(True)
                logger.info("Bybit demo trading enabled via enable_demo_trading()")
                return

            # Reliable fallback: replace the 'api' URL set with 'demotrading'
            demo_urls = exchange_obj.urls.get("demotrading")
            if demo_urls:
                exchange_obj.urls["api"] = demo_urls
                logger.info("Bybit demo trading URLs applied via demotrading URL map")
            else:
                logger.warning("Bybit demotrading URL map not found in ccxt %s", ccxt.__version__)
        except Exception as e:
            logger.warning("Demo URL override failed: %s", e)

    @staticmethod
    def _classify_error(exc: Exception) -> tuple[str, str]:
        """Return (error_code, human_reason) for a connection failure.

        Returns:
            error_code  – machine-readable tag used by UI layers
            human_reason – short user-facing description
        """
        msg = str(exc)
        # KuCoin geographic restriction (code 400302)
        if "400302" in msg or ("unavailable in the U.S" in msg):
            return "geo_blocked", "Geo-blocked (US IP)"
        # Generic HTTP 451 (legal / regional block)
        if "451" in msg:
            return "geo_blocked", "Geo-blocked (region restricted)"
        # Authentication failures
        if any(k in msg.lower() for k in ("invalid key", "apikey", "unauthorized", "401")):
            return "auth_failed", "Authentication failed"
        # Network/timeout
        if any(k in msg.lower() for k in ("timeout", "connection", "network", "ssl")):
            return "network_error", "Network error"
        return "unknown", "Connection failed"

    def _build_instance(self, data: dict) -> bool:
        exchange_obj = None
        try:
            exchange_class = getattr(ccxt, data["exchange_id"])
            config = {
                "apiKey":          data["api_key"],
                "secret":          data["api_secret"],
                "enableRateLimit": True,
                "timeout":         15000,
                # Bybit rejects requests when local clock drifts >5s from
                # server.  20s recv_window tolerates NTP jitter on VPN.
                "recvWindow":      20000,
            }
            if data["passphrase"]:
                config["password"] = data["passphrase"]
            if data.get("sandbox") and data["exchange_id"] in SANDBOX_SUPPORTED:
                config["sandbox"] = True

            # Bybit Demo only supports perpetual (swap) markets — spot
            # symbols like "BCH/USDT" don't exist.  Setting defaultType
            # to "swap" makes CCXT map standard symbols to their linear
            # perpetual counterparts automatically.
            if data.get("demo") and data["exchange_id"] == "bybit":
                config.setdefault("options", {})["defaultType"] = "swap"

            # Build CCXT object outside the lock so a slow load_markets()
            # doesn't block other readers.
            exchange_obj = exchange_class(config)

            # Demo Trading: redirect Bybit REST URLs to api-demo.bybit.com.
            # Must be done BEFORE load_markets() so the right endpoint is hit.
            if data.get("demo") and data["exchange_id"] == "bybit":
                self._apply_demo_mode(exchange_obj)

            exchange_obj.load_markets()
            markets = exchange_obj.markets

            # ── Build ccxt.pro WebSocket instance (optional) ──────────
            # A separate ccxt.pro instance is required because the REST instance
            # built via ccxt.<exchange_id> does not support watch_ticker or
            # watch_ohlcv. Building it after the REST instance succeeds means
            # a WS failure never prevents REST connectivity.
            ws_obj = None
            try:
                import ccxt.pro as _ccxtpro
                ws_class = getattr(_ccxtpro, data["exchange_id"], None)
                if ws_class is not None:
                    ws_config = dict(config)  # same credentials as REST
                    ws_obj = ws_class(ws_config)
                    # Apply demo mode to WS instance for Bybit Demo
                    if data.get("demo") and data["exchange_id"] == "bybit":
                        self._apply_demo_mode(ws_obj)
                    logger.info("WebSocket (ccxt.pro) instance built for %s", data["exchange_id"])
                else:
                    logger.debug("ccxt.pro has no class for %s; WS feed unavailable", data["exchange_id"])
            except ImportError:
                logger.debug("ccxt.pro not available; WebSocket feed will use REST fallback")
            except Exception as _ws_exc:
                logger.warning("WebSocket instance build failed: %s — REST fallback will be used", _ws_exc)

            with self._lock:
                self._exchange    = exchange_obj
                self._ws_exchange = ws_obj
                self._markets     = markets
            logger.info("Exchange loaded: %s (%d markets)", data["name"], len(markets))

            # Derive human-readable exchange mode for the sidebar label
            if data.get("demo"):
                exchange_mode = "demo"
            elif data.get("sandbox"):
                exchange_mode = "sandbox"
            else:
                exchange_mode = "live"

            # Store mode for System Health display
            self._mode = {"demo": "Demo Trading", "sandbox": "Testnet",
                          "live": "Live"}.get(exchange_mode, "Unknown")

            bus.publish(Topics.EXCHANGE_CONNECTED,
                        {"name": data["name"], "connected": True,
                         "exchange_mode": exchange_mode},
                        source="exchange_manager")
            return True

        except Exception as e:
            # Always ensure _exchange is None so is_connected() returns False
            with self._lock:
                self._exchange    = None
                self._ws_exchange = None
                self._markets     = {}

            error_code, reason = self._classify_error(e)
            logger.error("Failed to build exchange instance: %s", e)
            bus.publish(
                Topics.EXCHANGE_ERROR,
                {"error": str(e), "error_code": error_code, "reason": reason,
                 "name": data.get("name", "Exchange")},
                source="exchange_manager",
            )
            return False

    # ── Public API ─────────────────────────────────────────────
    def get_exchange(self) -> Optional[ccxt.Exchange]:
        with self._lock:
            return self._exchange

    def get_ws_exchange(self) -> Optional[object]:
        """Return the ccxt.pro WebSocket exchange instance, or None if unavailable.

        The WS instance supports watch_ticker() and watch_ohlcv() for real-time
        streaming. Use get_exchange() for REST operations (order placement, etc.).
        """
        with self._lock:
            return self._ws_exchange

    @property
    def last_fetch_at(self) -> float:
        """Epoch seconds of the last successful fetch_tickers() call. 0.0 if none yet."""
        return self._last_fetch_at

    @property
    def last_latency_ms(self) -> int:
        """Round-trip milliseconds of the last successful fetch_tickers() call."""
        return self._last_latency_ms

    @property
    def mode(self) -> str:
        """Human-readable exchange mode: 'Live', 'Demo Trading', 'Testnet', or 'Unknown'."""
        return self._mode

    def is_connected(self) -> bool:
        return self._exchange is not None

    def get_markets(self) -> dict:
        with self._lock:
            return dict(self._markets)

    def get_symbols(self, quote: str = "USDT") -> list[str]:
        """Return all symbols with given quote currency, sorted by name.

        Uses the CCXT 'spot' boolean field (always present) rather than
        the 'type' string, which varies across exchanges.
        """
        with self._lock:
            return sorted([
                s for s, m in self._markets.items()
                if m.get("quote") == quote
                and m.get("active", True)
                and (m.get("spot", False) or m.get("type", "") == "spot")
            ])

    def get_available_quotes(self) -> list[str]:
        """Return distinct quote currencies from the loaded spot markets."""
        with self._lock:
            quotes: set[str] = set()
            for m in self._markets.values():
                if m.get("active", True) and (
                    m.get("spot", False) or m.get("type", "") == "spot"
                ):
                    q = m.get("quote", "")
                    if q:
                        quotes.add(q)
            # Put USDT first, then BTC, then the rest alphabetically
            ordered = []
            for priority in ("USDT", "BTC", "ETH", "BNB"):
                if priority in quotes:
                    ordered.append(priority)
                    quotes.discard(priority)
            ordered.extend(sorted(quotes))
            return ordered

    def fetch_ticker(self, symbol: str) -> Optional[dict]:
        ex = self.get_exchange()
        if not ex:
            return None
        try:
            ticker = ex.fetch_ticker(symbol)
            return {
                "symbol": symbol,
                "last":   ticker.get("last") or 0,
                "bid":    ticker.get("bid") or 0,
                "ask":    ticker.get("ask") or 0,
                "change": ticker.get("percentage") or 0,
                "volume": ticker.get("baseVolume") or 0,
                "high":   ticker.get("high") or 0,
                "low":    ticker.get("low") or 0,
            }
        except Exception as e:
            logger.warning("fetch_ticker(%s) failed: %s", symbol, e)
            return None

    def fetch_tickers(self, symbols: list[str]) -> dict:
        ex = self.get_exchange()
        if not ex:
            return {}
        try:
            _t0 = _time.time()
            raw = ex.fetch_tickers(symbols)
            _elapsed_ms = int((_time.time() - _t0) * 1000)
            self._last_fetch_at = _time.time()
            self._last_latency_ms = _elapsed_ms
            result = {}
            for sym, t in raw.items():
                result[sym] = {
                    "symbol": sym,
                    "last":   t.get("last") or 0,
                    "bid":    t.get("bid") or 0,
                    "ask":    t.get("ask") or 0,
                    "change": t.get("percentage") or 0,
                    "volume": t.get("baseVolume") or 0,
                    "high":   t.get("high") or 0,
                    "low":    t.get("low") or 0,
                }
            return result
        except Exception as e:
            logger.warning("fetch_tickers failed: %s", e)
            return {}

    def fetch_ohlcv(self, symbol: str, timeframe: str = "1h",
                    since: Optional[int] = None, limit: int = 500) -> list:
        """Fetch OHLCV candles. Returns list of [ts_ms, o, h, l, c, v]."""
        ex = self.get_exchange()
        if not ex:
            return []
        try:
            return ex.fetch_ohlcv(symbol, timeframe, since=since, limit=limit)
        except Exception as e:
            logger.warning("fetch_ohlcv(%s, %s) failed: %s", symbol, timeframe, e)
            return []

    def fetch_order_book(self, symbol: str, limit: int = 20) -> dict:
        ex = self.get_exchange()
        if not ex:
            return {"bids": [], "asks": []}
        try:
            return ex.fetch_order_book(symbol, limit=limit)
        except Exception as e:
            logger.warning("fetch_order_book(%s) failed: %s", symbol, e)
            return {"bids": [], "asks": []}

    def fetch_balance(self) -> dict:
        ex = self.get_exchange()
        if not ex:
            return {}
        try:
            return ex.fetch_balance()
        except Exception as e:
            logger.warning("fetch_balance failed: %s", e)
            return {}

    def sync_assets_to_db(self, exchange_db_id: int, quote: str = "USDT"):
        """Write all active spot symbols to the assets table."""
        symbols = self.get_symbols(quote)
        added = 0
        with get_session() as session:
            existing = {
                a.symbol for a in
                session.query(Asset).filter_by(exchange_id=exchange_db_id).all()
            }
            for sym in symbols:
                if sym not in existing:
                    market = self._markets.get(sym, {})
                    session.add(Asset(
                        exchange_id=exchange_db_id,
                        symbol=sym,
                        base_currency=market.get("base", sym.split("/")[0]),
                        quote_currency=market.get("quote", quote),
                        price_precision=market.get("precision", {}).get("price", 8),
                        amount_precision=market.get("precision", {}).get("amount", 8),
                        min_amount=market.get("limits", {}).get("amount", {}).get("min"),
                        min_cost=market.get("limits", {}).get("cost", {}).get("min"),
                        last_updated=datetime.utcnow(),
                    ))
                    added += 1
        logger.info("Synced %d new assets to DB (exchange_id=%d)", added, exchange_db_id)
        return added


# Global singleton
exchange_manager = ExchangeManager()
