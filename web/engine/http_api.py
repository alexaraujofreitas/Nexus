# ============================================================
# NEXUS TRADER Web — Lightweight HTTP API (Engine-Embedded)
#
# Runs an aiohttp server on :8000 INSIDE the engine process,
# replacing the need for the separate FastAPI backend,
# PostgreSQL, and Redis.
#
# The Vite frontend proxies /api → :8000, so no frontend
# changes are needed.
#
# All endpoints call engine command handlers directly.
# Asset storage uses an in-memory store with JSON persistence.
# ============================================================
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from aiohttp import web

logger = logging.getLogger("nexus.http_api")

# ── Credential encryption (Fernet AES-256) ──────────────────
_CRED_KEY_PATH = Path(os.path.dirname(os.path.abspath(__file__))).parent.parent / "data" / ".nexus_web_key"

def _get_fernet():
    """Return a Fernet instance, creating the key file if absent."""
    from cryptography.fernet import Fernet
    if _CRED_KEY_PATH.exists():
        key = _CRED_KEY_PATH.read_bytes().strip()
    else:
        key = Fernet.generate_key()
        _CRED_KEY_PATH.parent.mkdir(parents=True, exist_ok=True)
        _CRED_KEY_PATH.write_bytes(key)
    return Fernet(key)

def _encrypt_cred(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    return _get_fernet().encrypt(value.encode()).decode()

def _decrypt_cred(token: Optional[str]) -> Optional[str]:
    if not token:
        return None
    try:
        return _get_fernet().decrypt(token.encode()).decode()
    except Exception:
        return None

def _mask(value: Optional[str]) -> str:
    if not value:
        return ""
    return "\u2022" * 4 + value[-4:] if len(value) > 4 else "\u2022" * len(value)


# ── Logger-name → component mapping ──────────────────────────
# Maps Python logger name prefixes to the component names the
# frontend filter expects.
_COMPONENT_MAP: list[tuple[str, str]] = [
    ("core.scanning",           "scanner"),
    ("core.signals",            "signals"),
    ("core.risk",               "risk"),
    ("core.execution",          "executor"),
    ("core.market_data",        "exchange"),
    ("core.agents",             "engine"),
    ("core.regime",             "signals"),
    ("core.meta_decision",      "signals"),
    ("core.learning",           "engine"),
    ("core.monitoring",         "engine"),
    ("core.evaluation",         "engine"),
    ("core.analytics",          "engine"),
    ("core.notifications",      "engine"),
    ("core.nlp",                "engine"),
    ("nexus.http_api",          "engine"),
    ("nexus.engine",            "engine"),
]


def _logger_to_component(name: str) -> str:
    """Map a Python logger name to a frontend-friendly component."""
    for prefix, comp in _COMPONENT_MAP:
        if name.startswith(prefix):
            return comp
    return "engine"


# ── Ring-buffer logging handler ──────────────────────────────
# Attaches to the root logger and captures every record into a
# thread-safe deque.  The HTTP endpoint reads from this buffer.

_LOG_RING: deque[dict] = deque(maxlen=2000)


class _RingBufferHandler(logging.Handler):
    """Logging handler that stores formatted entries in a deque."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            entry = {
                "timestamp": datetime.fromtimestamp(
                    record.created, tz=timezone.utc
                ).isoformat(),
                "level": record.levelname,
                "component": _logger_to_component(record.name),
                "message": self.format(record),
                "extra": {
                    "logger": record.name,
                    "func": record.funcName,
                    "lineno": record.lineno,
                },
            }
            _LOG_RING.appendleft(entry)

            # Broadcast to connected WS clients (fire-and-forget).
            # The frontend wsStore routes on msg.channel and stores
            # msg.data as lastMessage[channel].
            if _ws_clients:
                payload = json.dumps({"channel": "logs", "data": entry})
                stale: list[web.WebSocketResponse] = []
                for ws in _ws_clients:
                    if ws.closed:
                        stale.append(ws)
                        continue
                    try:
                        # send_str is a coroutine — schedule it on the
                        # event loop from the logging thread.
                        if _event_loop is not None and _event_loop.is_running():
                            asyncio.run_coroutine_threadsafe(
                                ws.send_str(payload), _event_loop
                            )
                    except Exception:
                        stale.append(ws)
                for ws in stale:
                    _ws_clients.discard(ws)
        except Exception:
            pass  # Never let logging handler raise


def install_ring_buffer_handler() -> None:
    """Install the ring-buffer handler on the root logger.

    Call once from main.py after basicConfig so every logger in the
    process feeds into the ring buffer.
    """
    handler = _RingBufferHandler()
    # Use a minimal format — the structured dict already has metadata
    handler.setFormatter(logging.Formatter("%(message)s"))
    handler.setLevel(logging.DEBUG)
    logging.getLogger().addHandler(handler)


# ── WS client tracking ──────────────────────────────────────
_ws_clients: set[web.WebSocketResponse] = set()
_event_loop: Optional[asyncio.AbstractEventLoop] = None

# ── Data directory for persistent JSON store ──────────────
_DATA_DIR = Path(os.path.dirname(os.path.abspath(__file__))).parent.parent / "data"

# ── Supported Exchanges (matches desktop & backend) ──────
SUPPORTED_EXCHANGES = {
    "kucoin":   {"name": "KuCoin",    "has_sandbox": False, "has_demo": False, "needs_passphrase": True},
    "binance":  {"name": "Binance",   "has_sandbox": True,  "has_demo": False, "needs_passphrase": False},
    "bybit":    {"name": "Bybit",     "has_sandbox": True,  "has_demo": True,  "needs_passphrase": False},
    "coinbase": {"name": "Coinbase",  "has_sandbox": False, "has_demo": False, "needs_passphrase": False},
    "kraken":   {"name": "Kraken",    "has_sandbox": False, "has_demo": False, "needs_passphrase": False},
    "okx":      {"name": "OKX",       "has_sandbox": True,  "has_demo": False, "needs_passphrase": True},
}

# Default assets to pre-seed when store is empty
DEFAULT_ASSETS = [
    {"symbol": "BTC/USDT", "base_currency": "BTC", "quote_currency": "USDT",
     "price_precision": 2, "amount_precision": 5, "min_amount": 0.00001, "min_cost": 5.0,
     "is_tradable": True, "allocation_weight": 1.0},
    {"symbol": "ETH/USDT", "base_currency": "ETH", "quote_currency": "USDT",
     "price_precision": 2, "amount_precision": 4, "min_amount": 0.0001, "min_cost": 5.0,
     "is_tradable": True, "allocation_weight": 1.2},
    {"symbol": "SOL/USDT", "base_currency": "SOL", "quote_currency": "USDT",
     "price_precision": 3, "amount_precision": 2, "min_amount": 0.01, "min_cost": 5.0,
     "is_tradable": True, "allocation_weight": 1.3},
    {"symbol": "BNB/USDT", "base_currency": "BNB", "quote_currency": "USDT",
     "price_precision": 2, "amount_precision": 3, "min_amount": 0.001, "min_cost": 5.0,
     "is_tradable": True, "allocation_weight": 0.8},
    {"symbol": "XRP/USDT", "base_currency": "XRP", "quote_currency": "USDT",
     "price_precision": 4, "amount_precision": 1, "min_amount": 0.1, "min_cost": 5.0,
     "is_tradable": True, "allocation_weight": 0.8},
]


# ============================================================
# Asset Store — in-memory with JSON persistence
# ============================================================
class AssetStore:
    """Manages exchange and asset data in memory, persisted to JSON."""

    def __init__(self):
        self._store_path = _DATA_DIR / "web_assets.json"
        self._exchanges: list[dict] = []
        self._assets: list[dict] = []
        self._next_exchange_id = 1
        self._next_asset_id = 1
        self._load()

    def _load(self):
        """Load from JSON file or initialise defaults."""
        if self._store_path.exists():
            try:
                data = json.loads(self._store_path.read_text())
                self._exchanges = data.get("exchanges", [])
                self._assets = data.get("assets", [])
                self._next_exchange_id = data.get("next_exchange_id", 1)
                self._next_asset_id = data.get("next_asset_id", 1)
                logger.info("AssetStore: loaded %d exchanges, %d assets from %s",
                            len(self._exchanges), len(self._assets), self._store_path)
                return
            except Exception as e:
                logger.warning("AssetStore: failed to load %s: %s", self._store_path, e)

        # Initialise with default exchange + assets
        self._init_defaults()

    def _init_defaults(self):
        """Pre-seed with Bybit Demo exchange and default 5 assets."""
        now = datetime.now(timezone.utc).isoformat()
        ex = {
            "id": 1,
            "name": "Bybit Demo",
            "exchange_id": "bybit",
            "has_api_key": True,
            "has_api_secret": True,
            "has_passphrase": False,
            "api_key_masked": "••••••••",
            "api_secret_masked": "••••••••",
            "passphrase_masked": "",
            "sandbox_mode": False,
            "demo_mode": True,
            "mode": "demo",
            "is_active": True,
            "testnet_url": None,
            "created_at": now,
            "updated_at": now,
        }
        self._exchanges = [ex]
        self._next_exchange_id = 2

        self._assets = []
        for i, a in enumerate(DEFAULT_ASSETS, start=1):
            self._assets.append({
                "id": i,
                "exchange_id": 1,
                **a,
                "is_active": True,
                "market_snapshot": None,
                "snapshot_updated_at": None,
                "last_updated": now,
            })
        self._next_asset_id = len(DEFAULT_ASSETS) + 1
        self._save()
        logger.info("AssetStore: initialised defaults — 1 exchange, %d assets", len(self._assets))

    def _save(self):
        """Persist to JSON file."""
        try:
            _DATA_DIR.mkdir(parents=True, exist_ok=True)
            data = {
                "exchanges": self._exchanges,
                "assets": self._assets,
                "next_exchange_id": self._next_exchange_id,
                "next_asset_id": self._next_asset_id,
            }
            self._store_path.write_text(json.dumps(data, indent=2, default=str))
        except Exception as e:
            logger.error("AssetStore: save failed: %s", e)

    # ── Exchange CRUD ──────────────────────────────────────
    def get_exchanges(self) -> list[dict]:
        return self._exchanges

    def get_exchange(self, eid: int) -> Optional[dict]:
        return next((e for e in self._exchanges if e["id"] == eid), None)

    def get_active_exchange(self) -> Optional[dict]:
        return next((e for e in self._exchanges if e.get("is_active")), None)

    def create_exchange(self, data: dict) -> dict:
        """Create a new exchange entry with encrypted credentials."""
        now = datetime.now(timezone.utc).isoformat()
        exchange_id = data.get("exchange_id", "bybit")
        mode = data.get("mode", "live")
        api_key = data.get("api_key") or ""
        api_secret = data.get("api_secret") or ""
        passphrase = data.get("passphrase") or ""

        ex = {
            "id": self._next_exchange_id,
            "name": data.get("name", exchange_id),
            "exchange_id": exchange_id,
            "has_api_key": bool(api_key),
            "has_api_secret": bool(api_secret),
            "has_passphrase": bool(passphrase),
            "api_key_masked": _mask(api_key),
            "api_secret_masked": _mask(api_secret),
            "passphrase_masked": _mask(passphrase),
            "_api_key_enc": _encrypt_cred(api_key) if api_key else None,
            "_api_secret_enc": _encrypt_cred(api_secret) if api_secret else None,
            "_passphrase_enc": _encrypt_cred(passphrase) if passphrase else None,
            "sandbox_mode": mode == "sandbox",
            "demo_mode": mode == "demo",
            "mode": mode,
            "is_active": False,
            "testnet_url": None,
            "created_at": now,
            "updated_at": now,
        }
        self._next_exchange_id += 1
        self._exchanges.append(ex)
        self._save()
        logger.info("AssetStore: created exchange %s (id=%d, mode=%s)", ex["name"], ex["id"], mode)
        return self._sanitize(ex)

    def update_exchange(self, eid: int, data: dict) -> Optional[dict]:
        """Update an existing exchange entry."""
        ex = self.get_exchange(eid)
        if not ex:
            return None

        if "name" in data and data["name"]:
            ex["name"] = data["name"]

        if "mode" in data and data["mode"]:
            mode = data["mode"]
            ex["mode"] = mode
            ex["sandbox_mode"] = mode == "sandbox"
            ex["demo_mode"] = mode == "demo"

        # Only update credentials if provided and not masked placeholder
        bullet = "\u2022"
        api_key = data.get("api_key")
        if api_key and bullet not in api_key:
            ex["has_api_key"] = True
            ex["api_key_masked"] = _mask(api_key)
            ex["_api_key_enc"] = _encrypt_cred(api_key)

        api_secret = data.get("api_secret")
        if api_secret and bullet not in api_secret:
            ex["has_api_secret"] = True
            ex["api_secret_masked"] = _mask(api_secret)
            ex["_api_secret_enc"] = _encrypt_cred(api_secret)

        passphrase = data.get("passphrase")
        if passphrase and bullet not in passphrase:
            ex["has_passphrase"] = True
            ex["passphrase_masked"] = _mask(passphrase)
            ex["_passphrase_enc"] = _encrypt_cred(passphrase)

        ex["updated_at"] = datetime.now(timezone.utc).isoformat()
        self._save()
        logger.info("AssetStore: updated exchange %s (id=%d)", ex["name"], ex["id"])
        return self._sanitize(ex)

    def delete_exchange(self, eid: int) -> Optional[dict]:
        """Delete an exchange and its assets."""
        ex = self.get_exchange(eid)
        if not ex:
            return None
        self._exchanges = [e for e in self._exchanges if e["id"] != eid]
        removed_count = len([a for a in self._assets if a["exchange_id"] == eid])
        self._assets = [a for a in self._assets if a["exchange_id"] != eid]
        self._save()
        logger.info("AssetStore: deleted exchange %s (id=%d, removed %d assets)", ex["name"], eid, removed_count)
        return ex

    def get_decrypted_creds(self, eid: int) -> Optional[dict]:
        """Return decrypted credentials for an exchange (for CCXT connection)."""
        ex = self.get_exchange(eid)
        if not ex:
            return None
        return {
            "api_key": _decrypt_cred(ex.get("_api_key_enc")),
            "api_secret": _decrypt_cred(ex.get("_api_secret_enc")),
            "passphrase": _decrypt_cred(ex.get("_passphrase_enc")),
        }

    @staticmethod
    def _sanitize(ex: dict) -> dict:
        """Return a copy of the exchange dict without encrypted fields."""
        return {k: v for k, v in ex.items() if not k.startswith("_")}

    # ── Asset CRUD ─────────────────────────────────────────
    def get_assets(self, exchange_id: int, quote: str = None,
                   search: str = None, is_tradable: bool = None) -> list[dict]:
        result = [a for a in self._assets if a["exchange_id"] == exchange_id]
        if quote:
            result = [a for a in result if a.get("quote_currency") == quote]
        if search:
            s = search.upper()
            result = [a for a in result if s in a.get("symbol", "").upper()]
        if is_tradable is not None:
            result = [a for a in result if a.get("is_tradable") == is_tradable]
        return result

    def get_tradable_assets(self, exchange_id: int) -> list[dict]:
        return [a for a in self._assets
                if a["exchange_id"] == exchange_id and a.get("is_tradable")]

    def get_asset(self, asset_id: int) -> Optional[dict]:
        return next((a for a in self._assets if a["id"] == asset_id), None)

    def update_asset(self, asset_id: int, updates: dict) -> Optional[dict]:
        for a in self._assets:
            if a["id"] == asset_id:
                if "is_tradable" in updates:
                    a["is_tradable"] = updates["is_tradable"]
                if "allocation_weight" in updates:
                    a["allocation_weight"] = updates["allocation_weight"]
                a["last_updated"] = datetime.now(timezone.utc).isoformat()
                self._save()
                return a
        return None

    def bulk_update_assets(self, asset_ids: list[int], updates: dict) -> int:
        count = 0
        for a in self._assets:
            if a["id"] in asset_ids:
                if "is_tradable" in updates:
                    a["is_tradable"] = updates["is_tradable"]
                if "allocation_weight" in updates:
                    a["allocation_weight"] = updates["allocation_weight"]
                a["last_updated"] = datetime.now(timezone.utc).isoformat()
                count += 1
        if count:
            self._save()
        return count

    def sync_from_exchange(self, exchange_id: int, market_assets: list[dict]) -> dict:
        """Upsert assets from exchange market data. Preserves is_tradable/allocation_weight."""
        now = datetime.now(timezone.utc).isoformat()
        existing = {a["symbol"]: a for a in self._assets if a["exchange_id"] == exchange_id}
        new_count = 0

        for ma in market_assets:
            symbol = ma["symbol"]
            if symbol in existing:
                # Update exchange-sourced fields, preserve user fields
                a = existing[symbol]
                a["base_currency"] = ma.get("base_currency", a["base_currency"])
                a["quote_currency"] = ma.get("quote_currency", a["quote_currency"])
                a["price_precision"] = ma.get("price_precision", a["price_precision"])
                a["amount_precision"] = ma.get("amount_precision", a["amount_precision"])
                a["min_amount"] = ma.get("min_amount", a["min_amount"])
                a["min_cost"] = ma.get("min_cost", a["min_cost"])
                a["is_active"] = True
                a["last_updated"] = now
            else:
                # New asset
                self._assets.append({
                    "id": self._next_asset_id,
                    "exchange_id": exchange_id,
                    "symbol": symbol,
                    "base_currency": ma.get("base_currency", ""),
                    "quote_currency": ma.get("quote_currency", ""),
                    "price_precision": ma.get("price_precision", 8),
                    "amount_precision": ma.get("amount_precision", 8),
                    "min_amount": ma.get("min_amount"),
                    "min_cost": ma.get("min_cost"),
                    "is_active": True,
                    "is_tradable": False,
                    "allocation_weight": 1.0,
                    "market_snapshot": None,
                    "snapshot_updated_at": None,
                    "last_updated": now,
                })
                self._next_asset_id += 1
                new_count += 1

        self._save()
        return {"count": len(market_assets), "new_count": new_count}


# ============================================================
# HTTP API Server
# ============================================================
class EngineHttpApi:
    """aiohttp-based HTTP API server embedded in the engine."""

    def __init__(self, engine: Any):
        self._engine = engine
        self._store = AssetStore()
        self._app = web.Application(middlewares=[self._cors_middleware])
        self._runner: Optional[web.AppRunner] = None
        # Instance-level ticker cache (NOT class-level)
        self._ticker_cache: dict[str, dict] = {}
        self._ticker_cache_ts: str = ""
        self._setup_routes()

    @web.middleware
    async def _cors_middleware(self, request: web.Request, handler):
        """Handle CORS for Vite dev server."""
        if request.method == "OPTIONS":
            resp = web.Response(status=204)
        else:
            try:
                resp = await handler(request)
            except web.HTTPException as ex:
                resp = ex
        resp.headers["Access-Control-Allow-Origin"] = "*"
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, PATCH, DELETE, OPTIONS"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
        return resp

    def _setup_routes(self):
        r = self._app.router

        # ── Auth stubs ─────────────────────────────────────
        r.add_post("/api/v1/auth/login", self._auth_login)
        r.add_post("/api/v1/auth/setup", self._auth_setup)
        r.add_get("/api/v1/auth/me", self._auth_me)
        r.add_post("/api/v1/auth/logout", self._auth_logout)
        r.add_post("/api/v1/auth/refresh", self._auth_refresh)

        # ── Exchange Management ────────────────────────────
        r.add_get("/api/v1/exchanges/supported", self._exchanges_supported)
        r.add_get("/api/v1/exchanges/", self._exchanges_list)
        r.add_post("/api/v1/exchanges/", self._exchange_create)
        r.add_get("/api/v1/exchanges/{id}", self._exchange_get)
        r.add_put("/api/v1/exchanges/{id}", self._exchange_update)
        r.add_delete("/api/v1/exchanges/{id}", self._exchange_delete)
        r.add_post("/api/v1/exchanges/{id}/activate", self._exchange_activate)
        r.add_post("/api/v1/exchanges/{id}/deactivate", self._exchange_deactivate)
        r.add_post("/api/v1/exchanges/test-connection", self._exchange_test)

        # ── Asset Management ───────────────────────────────
        r.add_get("/api/v1/exchanges/{id}/assets/tradable", self._assets_tradable)
        r.add_get("/api/v1/exchanges/{id}/assets", self._assets_list)
        r.add_post("/api/v1/exchanges/{id}/sync-assets", self._assets_sync)
        r.add_patch("/api/v1/exchanges/{id}/assets/bulk", self._assets_bulk_update)
        r.add_patch("/api/v1/exchanges/{id}/assets/{asset_id}", self._assets_update)

        # ── Scanner ────────────────────────────────────────
        r.add_get("/api/v1/scanner/results", self._scanner_results)
        r.add_get("/api/v1/scanner/pipeline-status", self._scanner_pipeline)
        r.add_get("/api/v1/scanner/watchlist", self._scanner_watchlist)
        r.add_post("/api/v1/scanner/trigger", self._scanner_trigger)

        # ── Dashboard / System ─────────────────────────────
        r.add_get("/api/v1/dashboard/summary", self._dashboard_summary)
        r.add_get("/api/v1/dashboard/crash-defense", self._dashboard_crash_defense)
        r.add_get("/api/v1/system/health", self._system_health)

        # ── Monitor ────────────────────────────────────────
        r.add_get("/api/v1/monitor/positions", self._monitor_positions)
        r.add_get("/api/v1/monitor/portfolio", self._monitor_portfolio)
        r.add_get("/api/v1/monitor/pnl", self._monitor_pnl)
        r.add_get("/api/v1/monitor/risk", self._monitor_risk)
        r.add_get("/api/v1/monitor/trades", self._monitor_trades)

        # ── Trading ────────────────────────────────────────
        r.add_get("/api/v1/trading/positions", self._trading_positions)
        r.add_post("/api/v1/trading/close", self._trading_close)
        r.add_post("/api/v1/trading/close-all", self._trading_close_all)
        r.add_get("/api/v1/trades/history", self._trades_history)

        # ── Analytics ──────────────────────────────────────
        r.add_get("/api/v1/analytics/equity-curve", self._analytics_equity_curve)
        r.add_get("/api/v1/analytics/metrics", self._analytics_metrics)
        r.add_get("/api/v1/analytics/trade-distribution", self._analytics_trade_dist)
        r.add_get("/api/v1/analytics/by-model", self._analytics_by_model)
        r.add_get("/api/v1/analytics/drawdown-curve", self._analytics_drawdown)
        r.add_get("/api/v1/analytics/rolling-metrics", self._analytics_rolling)
        r.add_get("/api/v1/analytics/r-distribution", self._analytics_r_dist)
        r.add_get("/api/v1/analytics/duration-analysis", self._analytics_duration)
        r.add_get("/api/v1/analytics/by-regime", self._analytics_by_regime)
        r.add_get("/api/v1/analytics/regime-transitions", self._analytics_regime_trans)
        r.add_get("/api/v1/analytics/current-regime", self._analytics_current_regime)
        r.add_get("/api/v1/analytics/regime-history", self._analytics_regime_history)

        # ── Signals ────────────────────────────────────────
        r.add_get("/api/v1/signals/agents", self._signals_agents)
        r.add_get("/api/v1/signals/confluence", self._signals_confluence)

        # ── Charts ─────────────────────────────────────────
        r.add_get("/api/v1/charts/ohlcv", self._charts_ohlcv)

        # ── Market Data ────────────────────────────────────
        r.add_get("/api/v1/market-data/snapshots", self._market_snapshots)
        r.add_get("/api/v1/market-data/snapshots/{assetId}", self._market_snapshot_single)

        # ── Risk ───────────────────────────────────────────
        r.add_get("/api/v1/risk/status", self._risk_status)

        # ── Settings ───────────────────────────────────────
        r.add_get("/api/v1/settings/", self._settings_get)
        r.add_patch("/api/v1/settings/", self._settings_update)

        # ── Notifications ──────────────────────────────────
        r.add_get("/api/v1/settings/notifications/history", self._notif_history)
        r.add_get("/api/v1/settings/notifications/stats", self._notif_stats)

        # ── Validation ─────────────────────────────────────
        r.add_get("/api/v1/validation/health", self._validation_health)
        r.add_get("/api/v1/validation/readiness", self._validation_readiness)
        r.add_get("/api/v1/validation/data-integrity", self._validation_integrity)

        # ── Logs ───────────────────────────────────────────
        r.add_get("/api/v1/logs/recent", self._logs_recent)

        # ── Backtest ───────────────────────────────────────
        r.add_post("/api/v1/backtest/start", self._backtest_start)
        r.add_get("/api/v1/backtest/status/{jobId}", self._backtest_status)
        r.add_get("/api/v1/backtest/results/{jobId}", self._backtest_results)

        # ── WebSocket stub ─────────────────────────────────
        r.add_get("/ws", self._ws_handler)

    async def start(self, host: str = "0.0.0.0", port: int = 8000):
        """Start the HTTP server and background ticker loop."""
        global _event_loop
        _event_loop = asyncio.get_running_loop()

        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, host, port)
        await site.start()
        logger.info("HTTP API server started on http://%s:%d", host, port)

        # Start background ticker refresh (every 10s)
        self._ticker_task = asyncio.ensure_future(self._ticker_loop())

    async def stop(self):
        """Stop the HTTP server and background tasks."""
        if hasattr(self, "_ticker_task") and self._ticker_task:
            self._ticker_task.cancel()
            try:
                await self._ticker_task
            except asyncio.CancelledError:
                pass
        if self._runner:
            await self._runner.cleanup()
            logger.info("HTTP API server stopped")

    # ── Background ticker refresh ─────────────────────────

    async def _ticker_loop(self):
        """Periodically fetch tickers from exchange for tradable assets."""
        logger.info("Ticker loop started — waiting 5s for engine init")
        await asyncio.sleep(5)  # Let engine finish startup + exchange connect
        while True:
            try:
                await self._refresh_tickers()
            except asyncio.CancelledError:
                return
            except Exception as e:
                logger.warning("Ticker refresh error: %s", e, exc_info=True)
            await asyncio.sleep(10)  # 10s refresh interval

    async def _refresh_tickers(self):
        """Fetch tickers from exchange manager for all tradable symbols."""
        em = self._engine._exchange_manager
        if not em:
            logger.info("Ticker skip: no ExchangeManager")
            return
        if not em.get_exchange():
            logger.info("Ticker skip: ExchangeManager has no active exchange")
            return

        active_ex = self._store.get_active_exchange()
        if not active_ex:
            logger.info("Ticker skip: no active exchange in AssetStore")
            return

        tradable = self._store.get_tradable_assets(active_ex["id"])
        if not tradable:
            logger.info("Ticker skip: no tradable assets for exchange %s", active_ex.get("name"))
            return

        symbols = [a["symbol"] for a in tradable]

        try:
            loop = asyncio.get_running_loop()
            # Use ExchangeManager.fetch_tickers() — returns processed dict
            tickers = await loop.run_in_executor(
                None, lambda: em.fetch_tickers(symbols)
            )
        except Exception as e:
            logger.warning("fetch_tickers executor failed: %s", e)
            return

        if not tickers:
            logger.info("Ticker skip: fetch_tickers returned empty for %s", symbols)
            return

        # Fetch 1h candle for each symbol to compute 1h % change
        change_1h_map: dict[str, float | None] = {}
        try:
            exchange = em.get_exchange()
            if exchange:
                for symbol in symbols:
                    try:
                        bars = await loop.run_in_executor(
                            None, lambda s=symbol: exchange.fetch_ohlcv(s, "1h", limit=2)
                        )
                        if bars and len(bars) >= 2:
                            prev_close = bars[-2][4]
                            curr_close = bars[-1][4]
                            if prev_close and prev_close > 0:
                                change_1h_map[symbol] = ((curr_close - prev_close) / prev_close) * 100
                    except Exception as e:
                        logger.debug("1h OHLCV fetch failed for %s: %s", symbol, e)
        except Exception as e:
            logger.debug("1h change computation failed: %s", e)

        now = datetime.now(timezone.utc).isoformat()
        for symbol, t in tickers.items():
            bid = t.get("bid", 0) or 0
            ask = t.get("ask", 0) or 0
            self._ticker_cache[symbol] = {
                "price": t.get("last"),
                "bid": bid,
                "ask": ask,
                "spread_pct": (
                    ((ask - bid) / bid * 100) if bid > 0 and ask > 0 else None
                ),
                "change_1h": change_1h_map.get(symbol),
                "change_24h": t.get("change"),  # ExchangeManager maps percentage→change
                "volume_1h": None,
                "volume_24h": t.get("volume"),  # ExchangeManager maps baseVolume→volume
                "high_24h": t.get("high"),
                "low_24h": t.get("low"),
                "vwap_24h": None,
            }
        self._ticker_cache_ts = now
        logger.info("Tickers refreshed: %d symbols (e.g. %s=$%.2f)",
                     len(tickers),
                     next(iter(tickers), "?"),
                     next(iter(self._ticker_cache.values()), {}).get("price", 0) or 0)

    # ── Helper: call engine command handler directly ───────
    async def _engine_cmd(self, action: str, params: dict = None) -> dict:
        """Call the engine's command handler directly (no Redis)."""
        return await self._engine._handle_command(action, params or {})

    def _json(self, data: Any, status: int = 200) -> web.Response:
        return web.json_response(data, status=status)

    # ================================================================
    # AUTH STUBS (local dev — no real authentication)
    # ================================================================

    async def _auth_login(self, req: web.Request) -> web.Response:
        return self._json({
            "access_token": "nexus-dev-token",
            "refresh_token": "nexus-dev-refresh",
            "token_type": "bearer",
        })

    async def _auth_setup(self, req: web.Request) -> web.Response:
        return self._json({
            "access_token": "nexus-dev-token",
            "refresh_token": "nexus-dev-refresh",
            "token_type": "bearer",
        })

    async def _auth_me(self, req: web.Request) -> web.Response:
        return self._json({"sub": "local-dev", "email": "admin@localhost"})

    async def _auth_logout(self, req: web.Request) -> web.Response:
        return self._json({"status": "ok"})

    async def _auth_refresh(self, req: web.Request) -> web.Response:
        return self._json({
            "access_token": "nexus-dev-token",
            "refresh_token": "nexus-dev-refresh",
        })

    # ================================================================
    # EXCHANGE MANAGEMENT
    # ================================================================

    async def _exchanges_supported(self, req: web.Request) -> web.Response:
        return self._json({
            "exchanges": [
                {"exchange_id": eid, **info}
                for eid, info in SUPPORTED_EXCHANGES.items()
            ]
        })

    async def _exchanges_list(self, req: web.Request) -> web.Response:
        exchanges = [AssetStore._sanitize(e) for e in self._store.get_exchanges()]
        return self._json({"exchanges": exchanges})

    async def _exchange_get(self, req: web.Request) -> web.Response:
        eid = int(req.match_info["id"])
        ex = self._store.get_exchange(eid)
        if not ex:
            raise web.HTTPNotFound(text="Exchange not found")
        return self._json(AssetStore._sanitize(ex))

    async def _exchange_create(self, req: web.Request) -> web.Response:
        body = await req.json()
        exchange_id = body.get("exchange_id", "")
        if exchange_id not in SUPPORTED_EXCHANGES:
            return self._json(
                {"detail": f"Unsupported exchange: {exchange_id}. "
                           f"Supported: {', '.join(sorted(SUPPORTED_EXCHANGES))}"},
                status=400,
            )
        mode = body.get("mode", "live")
        if mode not in ("live", "sandbox", "demo"):
            return self._json({"detail": f"Invalid mode: {mode}"}, status=400)
        info = SUPPORTED_EXCHANGES[exchange_id]
        if mode == "sandbox" and not info["has_sandbox"]:
            return self._json({"detail": f"{info['name']} does not support sandbox/testnet mode"}, status=400)
        if mode == "demo" and not info["has_demo"]:
            return self._json({"detail": "Demo trading is only available on Bybit"}, status=400)

        ex = self._store.create_exchange(body)
        return self._json(ex)

    async def _exchange_update(self, req: web.Request) -> web.Response:
        eid = int(req.match_info["id"])
        body = await req.json()
        ex = self._store.update_exchange(eid, body)
        if not ex:
            raise web.HTTPNotFound(text="Exchange not found")
        return self._json(ex)

    async def _exchange_delete(self, req: web.Request) -> web.Response:
        eid = int(req.match_info["id"])
        ex = self._store.delete_exchange(eid)
        if not ex:
            raise web.HTTPNotFound(text="Exchange not found")
        return self._json({"status": "ok", "name": ex["name"]})

    async def _exchange_activate(self, req: web.Request) -> web.Response:
        eid = int(req.match_info["id"])
        ex = self._store.get_exchange(eid)
        if not ex:
            raise web.HTTPNotFound(text="Exchange not found")
        # Deactivate all others, activate this one
        for e in self._store._exchanges:
            e["is_active"] = (e["id"] == eid)
        self._store._save()
        return self._json({"status": "ok", "name": ex["name"], "mode": ex.get("mode", "live")})

    async def _exchange_deactivate(self, req: web.Request) -> web.Response:
        eid = int(req.match_info["id"])
        ex = self._store.get_exchange(eid)
        if not ex:
            raise web.HTTPNotFound(text="Exchange not found")
        ex["is_active"] = False
        self._store._save()
        return self._json({"status": "ok", "name": ex["name"]})

    async def _exchange_test(self, req: web.Request) -> web.Response:
        """Test exchange connection with provided credentials via CCXT."""
        try:
            body = await req.json()
        except Exception:
            body = {}

        exchange_id = body.get("exchange_id", "bybit")
        api_key = body.get("api_key", "")
        api_secret = body.get("api_secret", "")
        passphrase = body.get("passphrase", "")
        mode = body.get("mode", "live")
        stored_exchange_id = body.get("stored_exchange_id")

        # When editing an existing exchange, the user may not re-enter
        # credentials.  Fall back to the stored (encrypted) values.
        if (not api_key or not api_secret) and stored_exchange_id:
            creds = self._store.get_decrypted_creds(int(stored_exchange_id))
            if creds:
                if not api_key:
                    api_key = creds.get("api_key", "") or ""
                if not api_secret:
                    api_secret = creds.get("api_secret", "") or ""
                if not passphrase:
                    passphrase = creds.get("passphrase", "") or ""
                logger.info(
                    "Test connection: using stored credentials for exchange %s",
                    stored_exchange_id,
                )

        if not api_key or not api_secret:
            return self._json({"status": "error", "error": "API key and secret are required"})

        try:
            import ccxt
            exchange_class = getattr(ccxt, exchange_id, None)
            if not exchange_class:
                return self._json({"status": "error", "error": f"Unknown exchange: {exchange_id}"})

            config = {
                "apiKey": api_key,
                "secret": api_secret,
                "enableRateLimit": True,
                "timeout": 15000,
                "recvWindow": 20000,
            }
            if passphrase:
                config["password"] = passphrase

            if mode == "sandbox":
                config["sandbox"] = True

            ex = exchange_class(config)

            if mode == "demo" and exchange_id == "bybit":
                if hasattr(ex, "enable_demo_trading"):
                    ex.enable_demo_trading(True)
                else:
                    demo_urls = ex.urls.get("demotrading")
                    if demo_urls:
                        ex.urls["api"] = demo_urls
                    else:
                        ex.urls["api"] = {
                            "public": "https://api-demo.bybit.com",
                            "private": "https://api-demo.bybit.com",
                        }

            loop = asyncio.get_event_loop()
            balance = await loop.run_in_executor(None, ex.fetch_balance)

            usdt_free = 0.0
            if "USDT" in balance:
                usdt_free = float(balance["USDT"].get("free", 0) or 0)
            elif "free" in balance and "USDT" in balance["free"]:
                usdt_free = float(balance["free"]["USDT"] or 0)

            mode_labels = {"live": "Live", "sandbox": "Testnet", "demo": "Demo"}
            return self._json({
                "status": "ok",
                "message": f"Connected to {SUPPORTED_EXCHANGES.get(exchange_id, {}).get('name', exchange_id)} ({mode_labels.get(mode, mode)})",
                "balance_usdt": round(usdt_free, 2),
                "mode_label": mode_labels.get(mode, mode),
            })
        except Exception as e:
            err_msg = str(e)
            if "auth" in err_msg.lower() or "key" in err_msg.lower() or "signature" in err_msg.lower():
                return self._json({"status": "error", "error": f"Authentication failed: {err_msg}"})
            return self._json({"status": "error", "error": err_msg})

    # ================================================================
    # ASSET MANAGEMENT
    # ================================================================

    async def _assets_list(self, req: web.Request) -> web.Response:
        eid = int(req.match_info["id"])
        quote = req.query.get("quote")
        search = req.query.get("search")
        is_tradable = req.query.get("is_tradable")
        if is_tradable is not None:
            is_tradable = is_tradable.lower() == "true"

        assets = self._store.get_assets(eid, quote=quote, search=search, is_tradable=is_tradable)
        return self._json({"assets": assets, "count": len(assets), "total": len(assets)})

    async def _assets_tradable(self, req: web.Request) -> web.Response:
        eid = int(req.match_info["id"])
        assets = self._store.get_tradable_assets(eid)
        symbols = [a["symbol"] for a in assets]
        return self._json({"symbols": symbols, "assets": assets, "count": len(assets)})

    async def _assets_sync(self, req: web.Request) -> web.Response:
        eid = int(req.match_info["id"])
        # Call engine to fetch markets from CCXT
        result = await self._engine_cmd("exchange.sync_assets", {"exchange_db_id": eid})
        if result.get("status") != "ok":
            return self._json({"status": "error", "detail": result.get("detail", "Sync failed")}, status=500)

        # Upsert into asset store
        market_assets = result.get("assets", [])
        sync_result = self._store.sync_from_exchange(eid, market_assets)
        return self._json({"status": "ok", "new_count": sync_result["new_count"], "count": sync_result["count"]})

    async def _assets_update(self, req: web.Request) -> web.Response:
        asset_id = int(req.match_info["asset_id"])
        body = await req.json()
        updated = self._store.update_asset(asset_id, body)
        if not updated:
            raise web.HTTPNotFound(text="Asset not found")
        return self._json(updated)

    async def _assets_bulk_update(self, req: web.Request) -> web.Response:
        body = await req.json()
        asset_ids = body.get("asset_ids", [])
        updates = {k: v for k, v in body.items() if k in ("is_tradable", "allocation_weight")}
        count = self._store.bulk_update_assets(asset_ids, updates)
        return self._json({"updated": count, "asset_ids": asset_ids})

    # ================================================================
    # SCANNER
    # ================================================================

    async def _scanner_results(self, req: web.Request) -> web.Response:
        result = await self._engine_cmd("get_scanner_results")
        return self._json(result)

    async def _scanner_pipeline(self, req: web.Request) -> web.Response:
        result = await self._engine_cmd("get_pipeline_status")

        # Enrich with asset store data if pipeline is empty
        pipeline = result.get("pipeline", [])
        if not pipeline:
            # Build pipeline rows from tradable assets
            active_ex = self._store.get_active_exchange()
            if active_ex:
                tradable = self._store.get_tradable_assets(active_ex["id"])
                pipeline = []
                for a in tradable:
                    pipeline.append({
                        "asset_id": a["id"],
                        "symbol": a["symbol"],
                        "allocation_weight": a.get("allocation_weight", 1.0),
                        "price": None,
                        "regime": "",
                        "regime_confidence": 0.0,
                        "models_fired": [],
                        "models_no_signal": [],
                        "score": 0.0,
                        "direction": "",
                        "status": "Waiting",
                        "reason": "No scan data yet",
                        "is_approved": False,
                        "entry_price": None,
                        "stop_loss": 0.0,
                        "take_profit": 0.0,
                        "rr_ratio": 0.0,
                        "position_size_usdt": 0.0,
                        "scanned_at": "",
                        "technical_score": 0.0,
                        "final_score": 0.0,
                        "mil_active": False,
                        "mil_total_delta": 0.0,
                        "mil_influence_pct": 0.0,
                        "mil_capped": False,
                        "mil_dominant_source": "",
                        "mil_breakdown": {},
                        "decision_explanation": "Waiting for first scan cycle",
                        "block_reasons": [],
                        "diagnostics": {},
                    })
                result["pipeline"] = pipeline
                result["summary"] = {
                    "total": len(pipeline),
                    "eligible": 0,
                    "active_signals": 0,
                    "blocked": 0,
                }

        # Merge in any tradable assets missing from scanner results
        pipeline = result.get("pipeline", [])
        scanned_syms = {r["symbol"] for r in pipeline}
        active_ex = self._store.get_active_exchange()
        if active_ex:
            tradable = self._store.get_tradable_assets(active_ex["id"])
            for a in tradable:
                if a["symbol"] not in scanned_syms:
                    pipeline.append({
                        "asset_id": a["id"], "symbol": a["symbol"],
                        "allocation_weight": a.get("allocation_weight", 1.0),
                        "price": None, "regime": "", "regime_confidence": 0.0,
                        "models_fired": [], "models_no_signal": [],
                        "score": 0.0, "direction": "", "status": "Waiting",
                        "reason": "Not yet scanned", "is_approved": False,
                        "entry_price": None, "stop_loss": 0.0, "take_profit": 0.0,
                        "rr_ratio": 0.0, "position_size_usdt": 0.0, "scanned_at": "",
                        "technical_score": 0.0, "final_score": 0.0,
                        "mil_active": False, "mil_total_delta": 0.0,
                        "mil_influence_pct": 0.0, "mil_capped": False,
                        "mil_dominant_source": "", "mil_breakdown": {},
                        "decision_explanation": "Awaiting scanner cycle",
                        "block_reasons": [], "diagnostics": {},
                    })
            result["pipeline"] = pipeline
            if "summary" in result:
                result["summary"]["total"] = len(pipeline)

        # Enrich pipeline rows with live prices from ticker cache
        pipeline = result.get("pipeline", [])
        if pipeline and self._ticker_cache:
            for row in pipeline:
                if row.get("price") is None:
                    cached = self._ticker_cache.get(row["symbol"])
                    if cached and cached.get("price") is not None:
                        row["price"] = cached["price"]

        scanner_running = False
        if self._engine._scanner:
            scanner_running = getattr(self._engine._scanner, "_running", False)
        result["scanner_running"] = scanner_running
        result.setdefault("last_scan_at", getattr(self._engine, "_last_pipeline_ts", ""))
        result.setdefault("source", "engine")
        return self._json(result)

    async def _scanner_watchlist(self, req: web.Request) -> web.Response:
        # Build watchlist from asset store (local mode) instead of PostgreSQL
        active_ex = self._store.get_active_exchange()
        if active_ex:
            tradable = self._store.get_tradable_assets(active_ex["id"])
            if tradable:
                symbols = [a["symbol"] for a in tradable]
                weights = {a["symbol"]: a.get("allocation_weight", 1.0) for a in tradable}
                return self._json({"status": "ok", "symbols": symbols, "weights": weights, "source": "local_store"})

        # Fall back to engine (config.yaml fallback)
        result = await self._engine_cmd("get_watchlist")
        return self._json(result)

    async def _scanner_trigger(self, req: web.Request) -> web.Response:
        result = await self._engine_cmd("trigger_scan")
        return self._json(result)

    # ================================================================
    # DASHBOARD / SYSTEM
    # ================================================================

    async def _dashboard_summary(self, req: web.Request) -> web.Response:
        result = await self._engine_cmd("get_dashboard")
        return self._json(result)

    async def _dashboard_crash_defense(self, req: web.Request) -> web.Response:
        result = await self._engine_cmd("get_crash_defense")
        return self._json(result)

    async def _system_health(self, req: web.Request) -> web.Response:
        result = await self._engine_cmd("get_system_health")
        return self._json(result)

    # ================================================================
    # MONITOR
    # ================================================================

    async def _monitor_positions(self, req: web.Request) -> web.Response:
        result = await self._engine_cmd("get_active_positions")
        return self._json(result)

    async def _monitor_portfolio(self, req: web.Request) -> web.Response:
        result = await self._engine_cmd("get_portfolio_state")
        return self._json(result)

    async def _monitor_pnl(self, req: web.Request) -> web.Response:
        result = await self._engine_cmd("get_live_pnl")
        return self._json(result)

    async def _monitor_risk(self, req: web.Request) -> web.Response:
        result = await self._engine_cmd("get_risk_state")
        return self._json(result)

    async def _monitor_trades(self, req: web.Request) -> web.Response:
        result = await self._engine_cmd("get_recent_trades_monitor")
        trades = result.get("trades", [])

        # Supplement with SQLite trades if in-memory list is small
        if len(trades) < 20:
            try:
                import sqlite3
                from pathlib import Path
                db_path = Path(__file__).resolve().parent.parent.parent / "data" / "nexus_trader.db"
                if db_path.exists():
                    conn = sqlite3.connect(str(db_path))
                    conn.row_factory = sqlite3.Row
                    c = conn.cursor()
                    c.execute("""SELECT symbol, side, entry_price, exit_price, pnl_usdt, pnl_pct,
                                       duration_s, exit_reason, regime, models_fired, opened_at, closed_at,
                                       size_usdt, score
                                FROM paper_trades ORDER BY closed_at DESC LIMIT 100""")
                    existing_keys = {(t.get("symbol",""), t.get("closed_at","")) for t in trades}
                    for row in c.fetchall():
                        key = (row["symbol"], row["closed_at"] or "")
                        if key not in existing_keys:
                            models = row["models_fired"] or "[]"
                            try:
                                models = json.loads(models) if isinstance(models, str) else models
                            except Exception:
                                models = []
                            trades.append({
                                "symbol": row["symbol"],
                                "side": row["side"],
                                "entry_price": row["entry_price"] or 0,
                                "exit_price": row["exit_price"] or 0,
                                "pnl_usdt": row["pnl_usdt"] or 0,
                                "pnl_pct": row["pnl_pct"] or 0,
                                "r_multiple": round((row["pnl_usdt"] or 0) / max(abs(row["size_usdt"] or 1) * 0.02, 0.01), 2),
                                "duration_s": row["duration_s"] or 0,
                                "regime": row["regime"] or "",
                                "exit_reason": row["exit_reason"] or "",
                                "models_fired": models,
                                "fees_estimated": 0,
                                "slippage": 0,
                                "closed_at": row["closed_at"] or "",
                                "score": row["score"] or 0,
                            })
                    conn.close()
            except Exception as e:
                logger.debug("SQLite trade supplement failed: %s", e)

        result["trades"] = trades
        result["count"] = len(trades)
        return self._json(result)

    # ================================================================
    # TRADING
    # ================================================================

    async def _trading_positions(self, req: web.Request) -> web.Response:
        result = await self._engine_cmd("get_positions")
        return self._json(result)

    async def _trading_close(self, req: web.Request) -> web.Response:
        body = await req.json()
        result = await self._engine_cmd("close_position", body)
        return self._json(result)

    async def _trading_close_all(self, req: web.Request) -> web.Response:
        result = await self._engine_cmd("close_all_positions")
        return self._json(result)

    async def _trades_history(self, req: web.Request) -> web.Response:
        page = int(req.rel_url.query.get("page", 1))
        per_page = int(req.rel_url.query.get("per_page", 50))
        result = await self._engine_cmd("get_trade_history", {"page": page, "per_page": per_page})
        return self._json(result)

    # ================================================================
    # ANALYTICS (delegate to engine commands)
    # ================================================================

    async def _analytics_equity_curve(self, req: web.Request) -> web.Response:
        result = await self._engine_cmd("get_portfolio")
        # Build minimal equity curve from portfolio
        capital = result.get("portfolio", {}).get("capital_usdt", 100000)
        return self._json({
            "status": "ok",
            "equity_curve": [{"timestamp": datetime.now(timezone.utc).isoformat(), "equity": capital}],
        })

    async def _analytics_metrics(self, req: web.Request) -> web.Response:
        result = await self._engine_cmd("get_portfolio")
        portfolio = result.get("portfolio", {})
        return self._json({
            "status": "ok",
            "metrics": {
                "total_trades": portfolio.get("total_trades", 0),
                "win_rate": portfolio.get("win_rate", 0.0),
                "profit_factor": portfolio.get("profit_factor", 0.0),
                "total_pnl_usdt": portfolio.get("total_pnl_usdt", 0.0),
                "max_drawdown_pct": portfolio.get("drawdown_pct", 0.0),
            },
        })

    async def _analytics_trade_dist(self, req: web.Request) -> web.Response:
        return self._json({"status": "ok", "distribution": []})

    async def _analytics_by_model(self, req: web.Request) -> web.Response:
        return self._json({"status": "ok", "models": []})

    async def _analytics_drawdown(self, req: web.Request) -> web.Response:
        result = await self._engine_cmd("get_drawdown_curve")
        return self._json(result)

    async def _analytics_rolling(self, req: web.Request) -> web.Response:
        result = await self._engine_cmd("get_rolling_metrics")
        return self._json(result)

    async def _analytics_r_dist(self, req: web.Request) -> web.Response:
        result = await self._engine_cmd("get_r_distribution")
        return self._json(result)

    async def _analytics_duration(self, req: web.Request) -> web.Response:
        result = await self._engine_cmd("get_duration_analysis")
        return self._json(result)

    async def _analytics_by_regime(self, req: web.Request) -> web.Response:
        result = await self._engine_cmd("get_performance_by_regime")
        return self._json(result)

    async def _analytics_regime_trans(self, req: web.Request) -> web.Response:
        result = await self._engine_cmd("get_regime_transitions")
        return self._json(result)

    async def _analytics_current_regime(self, req: web.Request) -> web.Response:
        result = await self._engine_cmd("get_current_regime")
        return self._json(result)

    async def _analytics_regime_history(self, req: web.Request) -> web.Response:
        result = await self._engine_cmd("get_regime_history")
        return self._json(result)

    # ================================================================
    # SIGNALS
    # ================================================================

    async def _signals_agents(self, req: web.Request) -> web.Response:
        """Return agent statuses. Synthesizes from pipeline scan results
        when the real AgentCoordinator has no data (headless web mode)."""
        result = await self._engine_cmd("get_agent_status")
        agents = result.get("agents", {}) if isinstance(result, dict) else {}

        # If engine returned empty agents, synthesize from pipeline data
        if not agents:
            import datetime
            pipeline = getattr(self._engine, '_last_pipeline_results', None)
            if not pipeline:
                scanner = getattr(self._engine, '_scanner', None)
                if scanner:
                    pipeline = getattr(scanner, '_last_scan_results', None)
            if pipeline and isinstance(pipeline, list):
                now = datetime.datetime.utcnow().isoformat() + "Z"
                all_models: dict = {}
                for asset in pipeline:
                    if not isinstance(asset, dict):
                        continue
                    scanned_at = asset.get("scanned_at", now)
                    regime_conf = asset.get("regime_confidence", 0.0)
                    for m in asset.get("models_fired", []):
                        if m not in all_models:
                            all_models[m] = {
                                "running": True, "stale": False,
                                "signal": round(asset.get("score", 0.0), 4),
                                "confidence": round(regime_conf, 4),
                                "updated_at": scanned_at,
                                "errors": 0,
                            }
                    for m in asset.get("models_no_signal", []):
                        if m not in all_models:
                            all_models[m] = {
                                "running": True, "stale": False,
                                "signal": 0.0,
                                "confidence": round(regime_conf, 4),
                                "updated_at": scanned_at,
                                "errors": 0,
                            }
                agents = all_models

        return self._json({"status": "ok", "agents": agents, "count": len(agents)})

    async def _signals_confluence(self, req: web.Request) -> web.Response:
        """Return confluence signals from pipeline scan results."""
        result = await self._engine_cmd("get_signals")
        signals = result.get("signals", []) if isinstance(result, dict) else []

        # If engine returned empty signals, synthesize from pipeline
        if not signals:
            pipeline = getattr(self._engine, '_last_pipeline_results', None)
            if pipeline and isinstance(pipeline, list):
                for r in pipeline:
                    if not isinstance(r, dict):
                        continue
                    # Include assets with any score or models fired
                    if r.get("score", 0) > 0 or r.get("models_fired"):
                        signals.append({
                            "symbol": r.get("symbol", ""),
                            "direction": r.get("direction", ""),
                            "score": r.get("score", 0.0),
                            "models": r.get("models_fired", []),
                            "regime": r.get("regime", ""),
                            "entry_price": r.get("entry_price"),
                            "stop_loss": r.get("stop_loss", 0.0),
                            "take_profit": r.get("take_profit", 0.0),
                            "approved": r.get("is_approved", False),
                            "rejection_reason": r.get("reason", ""),
                        })

        return self._json({"status": "ok", "signals": signals, "count": len(signals)})

    # ================================================================
    # CHARTS
    # ================================================================

    async def _charts_ohlcv(self, req: web.Request) -> web.Response:
        symbol = req.query.get("symbol", "BTC/USDT")
        timeframe = req.query.get("timeframe", "30m")
        limit = int(req.query.get("limit", "300"))

        try:
            em = self._engine._exchange_manager
            if em and em.get_exchange():
                exchange = em.get_exchange()
                bars = await asyncio.get_event_loop().run_in_executor(
                    None, lambda: exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
                )
                ohlcv = [
                    {"time": b[0] / 1000, "open": b[1], "high": b[2],
                     "low": b[3], "close": b[4], "volume": b[5]}
                    for b in (bars or [])
                ]
                return self._json({"status": "ok", "bars": ohlcv, "count": len(ohlcv)})
        except Exception as e:
            logger.warning("OHLCV fetch failed: %s", e)

        return self._json({"status": "ok", "bars": [], "count": 0})

    # ================================================================
    # MARKET DATA
    # ================================================================

    async def _market_snapshots(self, req: web.Request) -> web.Response:
        active_ex = self._store.get_active_exchange()
        if not active_ex:
            return self._json({"snapshots": [], "count": 0})
        assets = self._store.get_tradable_assets(active_ex["id"])
        snapshots = []
        for a in assets:
            snap = self._ticker_cache.get(a["symbol"])
            snapshots.append({
                "asset_id": a["id"],
                "symbol": a["symbol"],
                "is_tradable": a.get("is_tradable", False),
                "allocation_weight": a.get("allocation_weight", 1.0),
                "snapshot": snap,
                "snapshot_updated_at": self._ticker_cache_ts if snap else None,
                "data_source": "live" if snap else None,
            })
        return self._json({"snapshots": snapshots, "count": len(snapshots)})

    async def _market_snapshot_single(self, req: web.Request) -> web.Response:
        asset_id = int(req.match_info["assetId"])
        a = self._store.get_asset(asset_id)
        if not a:
            raise web.HTTPNotFound(text="Asset not found")
        snap = self._ticker_cache.get(a["symbol"])
        return self._json({
            "asset_id": a["id"],
            "symbol": a["symbol"],
            "is_tradable": a.get("is_tradable", False),
            "allocation_weight": a.get("allocation_weight", 1.0),
            "snapshot": snap,
            "snapshot_updated_at": self._ticker_cache_ts if snap else None,
            "data_source": "live" if snap else None,
        })

    # ================================================================
    # RISK
    # ================================================================

    async def _risk_status(self, req: web.Request) -> web.Response:
        result = await self._engine_cmd("get_risk_status")
        return self._json(result)

    # ================================================================
    # SETTINGS
    # ================================================================

    async def _settings_get(self, req: web.Request) -> web.Response:
        result = await self._engine_cmd("get_config", {"section": req.query.get("section")})
        return self._json(result)

    async def _settings_update(self, req: web.Request) -> web.Response:
        body = await req.json()
        result = await self._engine_cmd("update_config", body)
        return self._json(result)

    # ================================================================
    # NOTIFICATIONS
    # ================================================================

    async def _notif_history(self, req: web.Request) -> web.Response:
        return self._json({"status": "ok", "notifications": [], "count": 0})

    async def _notif_stats(self, req: web.Request) -> web.Response:
        return self._json({"status": "ok", "stats": {"total": 0, "delivered": 0, "failed": 0}})

    # ================================================================
    # VALIDATION
    # ================================================================

    async def _validation_health(self, req: web.Request) -> web.Response:
        result = await self._engine_cmd("get_system_health")
        return self._json(result)

    async def _validation_readiness(self, req: web.Request) -> web.Response:
        return self._json({"status": "ok", "ready": True, "checks": []})

    async def _validation_integrity(self, req: web.Request) -> web.Response:
        return self._json({"status": "ok", "integrity": True, "issues": []})

    # ================================================================
    # LOGS
    # ================================================================

    async def _logs_recent(self, req: web.Request) -> web.Response:
        limit = min(int(req.query.get("limit", "200")), 2000)
        level_filter = req.query.get("level", "").upper() or None
        comp_filter = req.query.get("component", "") or None
        search_filter = (req.query.get("search", "") or "").lower() or None

        # Valid log levels for filtering
        _LEVEL_ORDER = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}

        entries: list[dict] = []
        for entry in _LOG_RING:
            if level_filter and entry.get("level") != level_filter:
                continue
            if comp_filter and entry.get("component") != comp_filter:
                continue
            if search_filter and search_filter not in entry.get("message", "").lower():
                continue
            entries.append(entry)
            if len(entries) >= limit:
                break

        return self._json({"entries": entries, "count": len(entries)})

    # ================================================================
    # BACKTEST
    # ================================================================

    # backtest job store: {job_id: {state, progress, result, error, thread}}
    _backtest_jobs: dict = {}

    async def _backtest_start(self, req: web.Request) -> web.Response:
        body = await req.json()
        symbols = body.get("symbols", [
            "BTC/USDT", "ETH/USDT", "BNB/USDT", "XRP/USDT", "SOL/USDT",
            "TRX/USDT", "DOGE/USDT", "ADA/USDT", "BCH/USDT", "HYPE/USDT",
            "LINK/USDT", "XLM/USDT", "AVAX/USDT", "HBAR/USDT", "SUI/USDT",
            "NEAR/USDT", "ICP/USDT", "ONDO/USDT", "ALGO/USDT", "RENDER/USDT",
        ])
        start_date = body.get("start_date", "2024-01-01")
        end_date = body.get("end_date", "2026-03-01")
        timeframe = body.get("timeframe", "30m")
        fee_pct = float(body.get("fee_pct", 0.04))

        import uuid, threading
        job_id = str(uuid.uuid4())[:8]
        job = {"state": "running", "progress": 0, "result": None, "error": None}
        self._backtest_jobs[job_id] = job

        def _run():
            try:
                job["progress"] = 10
                from research.engine.backtest_runner import BacktestRunner
                # Map short names to full symbols
                sym_map = {
                    "BTC": "BTC/USDT", "ETH": "ETH/USDT", "BNB": "BNB/USDT",
                    "XRP": "XRP/USDT", "SOL": "SOL/USDT", "TRX": "TRX/USDT",
                    "DOGE": "DOGE/USDT", "ADA": "ADA/USDT", "BCH": "BCH/USDT",
                    "HYPE": "HYPE/USDT", "LINK": "LINK/USDT", "XLM": "XLM/USDT",
                    "AVAX": "AVAX/USDT", "HBAR": "HBAR/USDT", "SUI": "SUI/USDT",
                    "NEAR": "NEAR/USDT", "ICP": "ICP/USDT", "ONDO": "ONDO/USDT",
                    "ALGO": "ALGO/USDT", "RENDER": "RENDER/USDT",
                }
                full_syms = [sym_map.get(s, s) for s in symbols]

                job["progress"] = 20
                runner = BacktestRunner(
                    date_start=start_date,
                    date_end=end_date,
                    symbols=full_syms,
                    mode="pbl_slc",
                )
                job["progress"] = 30
                runner.load_data()
                job["progress"] = 60
                result = runner.run(cost=fee_pct / 100.0)
                job["progress"] = 100
                job["state"] = "completed"
                # Extract summary metrics
                m = result.get("metrics", result) if isinstance(result, dict) else {}
                job["result"] = {
                    "total_trades": m.get("total_trades", 0),
                    "win_rate": m.get("win_rate", 0),
                    "profit_factor": m.get("profit_factor", 0),
                    "cagr": m.get("cagr", 0),
                    "max_drawdown": m.get("max_drawdown", 0),
                    "sharpe": m.get("sharpe", 0),
                    "total_pnl": m.get("total_pnl", 0),
                }
            except Exception as e:
                import traceback
                job["state"] = "failed"
                job["error"] = str(e)
                job["progress"] = 100
                logger.error("Backtest failed: %s\n%s", e, traceback.format_exc())

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        return self._json({"status": "ok", "job_id": job_id})

    async def _backtest_status(self, req: web.Request) -> web.Response:
        job_id = req.match_info.get("jobId", "")
        job = self._backtest_jobs.get(job_id)
        if not job:
            return self._json({"status": "error", "detail": f"Job {job_id} not found"}, status=404)
        return self._json({
            "status": "ok",
            "state": job["state"],
            "progress_pct": job["progress"],
            "error": job.get("error"),
        })

    async def _backtest_results(self, req: web.Request) -> web.Response:
        job_id = req.match_info.get("jobId", "")
        job = self._backtest_jobs.get(job_id)
        if not job:
            return self._json({"status": "error", "detail": f"Job {job_id} not found"}, status=404)
        if job["state"] == "running":
            return self._json({"status": "ok", "state": "running", "results": None})
        return self._json({
            "status": "ok",
            "state": job["state"],
            "results": job.get("result"),
            "error": job.get("error"),
        })

    # ================================================================
    # WEBSOCKET STUB
    # ================================================================

    async def _ws_handler(self, req: web.Request) -> web.WebSocketResponse:
        """WebSocket endpoint with server-side ping keepalive."""
        ws = web.WebSocketResponse(heartbeat=30.0)  # aiohttp built-in ping every 30s
        await ws.prepare(req)
        _ws_clients.add(ws)
        # Send initial connection confirmation
        await ws.send_json({"type": "connected", "message": "Engine WS connected"})

        # Also start a periodic application-level ping (for frontend pong handling)
        async def _ping_loop():
            try:
                while not ws.closed:
                    await asyncio.sleep(25)
                    if not ws.closed:
                        await ws.send_json({"type": "ping"})
            except (ConnectionResetError, asyncio.CancelledError):
                pass
            except Exception:
                pass

        ping_task = asyncio.ensure_future(_ping_loop())

        try:
            async for msg in ws:
                if msg.type == 1:  # TEXT
                    try:
                        data = json.loads(msg.data)
                        action = data.get("action", "")
                        if action == "pong":
                            pass  # keepalive ack, ignore
                        elif action == "subscribe":
                            await ws.send_json({"type": "ack", "action": "subscribed", "channel": data.get("channel", "")})
                        elif action == "unsubscribe":
                            await ws.send_json({"type": "ack", "action": "unsubscribed", "channel": data.get("channel", "")})
                        else:
                            await ws.send_json({"type": "ack", "data": data})
                    except json.JSONDecodeError:
                        pass
        except Exception:
            pass
        finally:
            ping_task.cancel()
            _ws_clients.discard(ws)
        return ws
