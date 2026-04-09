# ============================================================
# NEXUS TRADER Web — Headless Trading Engine Service
#
# Standalone Python process that:
#   1. Installs Qt shim (de-PySide6-ification)
#   2. Initializes OrchestratorEngine, AgentCoordinator,
#      PaperExecutor, CrashDefenseController, NotificationManager
#   3. Publishes all EventBus events to Redis
#   4. Accepts commands from Redis request/reply queue
#   5. Manages lifecycle: startup, run loop, graceful shutdown
#
# Run: python -m engine.main
# ============================================================
from __future__ import annotations

import asyncio
import json
import logging
import logging.handlers
import os
import signal
import sys
import time
import traceback

# ── Path setup ──────────────────────────────────────────────
_ENGINE_DIR = os.path.dirname(os.path.abspath(__file__))
_WEB_DIR = os.path.dirname(_ENGINE_DIR)
_BACKEND_DIR = os.path.join(_WEB_DIR, "backend")
_PROJECT_ROOT = os.path.dirname(_WEB_DIR)

for p in [_BACKEND_DIR, _PROJECT_ROOT, _WEB_DIR]:
    if p not in sys.path:
        sys.path.insert(0, p)

# ── Install Qt shim BEFORE any core/ imports ────────────────
from core_patch import install_qt_shim  # noqa: E402
install_qt_shim()

# ── Now safe to import ──────────────────────────────────────
from core_patch.event_bus import EventBus, Topics  # noqa: E402
from core_patch.redis_bridge import RedisBridge  # noqa: E402

# ── Logging: stdout + rotating file ────────────────────────
_LOG_FORMAT = "%(asctime)s | %(name)-30s | %(levelname)-7s | %(message)s"
_LOG_DIR = os.path.join(_PROJECT_ROOT, "logs")
os.makedirs(_LOG_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format=_LOG_FORMAT,
    handlers=[
        logging.StreamHandler(),                          # stdout
        logging.handlers.RotatingFileHandler(             # file
            os.path.join(_LOG_DIR, "web_engine.log"),
            maxBytes=10 * 1024 * 1024,  # 10 MB
            backupCount=5,
            encoding="utf-8",
        ),
    ],
)
logger = logging.getLogger("nexus.engine")

# Install the ring-buffer handler so the Logs page can read entries
from engine.http_api import install_ring_buffer_handler  # noqa: E402
install_ring_buffer_handler()

# Redis config from environment
REDIS_URL = os.getenv("NEXUS_REDIS_URL", "redis://localhost:6379/0")


class TradingEngineService:
    """
    Headless trading engine that runs as a standalone service.
    Communicates with the API service via Redis pub/sub and
    request/reply queues.

    All command handlers are wired to real NexusTrader core/
    components — no stubs, no placeholders.
    """

    def __init__(self):
        self._running = False
        self._event_bus = EventBus()
        self._redis_bridge: RedisBridge | None = None
        self._command_task: asyncio.Task | None = None
        self._http_api = None          # EngineHttpApi (aiohttp server)

        # ── Core component references (populated during init) ──
        self._settings = None          # config.settings.AppSettings
        self._pe = None                # active executor (PaperExecutor or LiveBridge via order_router)
        self._scanner = None           # core.scanning.scanner.AssetScanner
        self._exchange_manager = None  # core.market_data.exchange_manager
        self._orchestrator = None      # core.orchestrator.orchestrator_engine
        self._notification_mgr = None  # core.notifications.notification_manager
        self._coordinator = None       # core.agents.agent_coordinator

        # ── Engine-level trading pause flag ─────────────────────
        # The desktop app has no pause/resume API on OrchestratorEngine.
        # We implement it here: when _trading_paused is True, the engine
        # will not submit new trades (checked in the scan cycle via
        # EventBus publication). Existing positions are unaffected.
        self._trading_paused = False

        # ── Cached CCXT exchange instance (built from web store) ──
        self._web_ccxt: object = None       # ccxt.Exchange instance
        self._web_ccxt_ts: float = 0.0
        self._WEB_CCXT_TTL: float = 300.0   # rebuild every 5 min

        # ── Cached exchange balance (for dashboard) ────────────
        self._cached_balance_usdt: float = 0.0
        self._cached_balance_ts: float = 0.0
        self._BALANCE_CACHE_TTL: float = 30.0  # seconds

        # ── Cached exchange closed orders ──────────────────────
        self._cached_exchange_trades: list[dict] = []
        self._cached_exchange_trades_ts: float = 0.0
        self._TRADES_CACHE_TTL: float = 30.0  # seconds

        # ── Cached exchange open positions ─────────────────────
        self._cached_exchange_positions: list[dict] = []
        self._cached_exchange_positions_ts: float = 0.0
        self._POSITIONS_CACHE_TTL: float = 10.0  # seconds

    async def start(self):
        """Full 14-step startup sequence."""
        logger.info("=== NexusTrader Engine Service Starting ===")
        self._running = True
        self._start_time = time.time()

        # Step 1: Connect Redis bridge (non-fatal — not needed for local dev)
        logger.info("[1/14] Connecting Redis bridge")
        try:
            self._redis_bridge = RedisBridge(
                redis_url=REDIS_URL,
                service_name="engine",
            )
            self._event_bus.attach_redis_bridge(self._redis_bridge)
            self._redis_bridge.start(self._event_bus)
        except Exception as e:
            logger.warning("[1/14] Redis unavailable (local dev mode): %s", e)
            self._redis_bridge = None

        # Step 2: Publish engine state
        logger.info("[2/14] Publishing initial engine state")
        await self._update_state("initializing")

        # Step 3-12: Core component initialization
        # Each step is wrapped in try/except to report initialization
        # failures.  Non-critical components can fail without halting.
        init_steps = [
            (3, "Loading configuration",       self._init_config),
            (4, "Initializing database",        self._init_database),
            (5, "Creating ExchangeManager",     self._init_exchange),
            (6, "Creating OrchestratorEngine",  self._init_orchestrator),
            (7, "Creating AgentCoordinator",    self._init_agents),
            (8, "Initializing active executor",  self._init_executor),
            (9, "Creating CrashDefenseCtrl",    self._init_crash_defense),
            (10, "Creating NotificationManager", self._init_notifications),
            (11, "Loading positions & history",  self._init_positions),
            (12, "Starting scanner timers",      self._init_scanner),
        ]

        for step_num, step_name, step_fn in init_steps:
            try:
                logger.info("[%d/14] %s", step_num, step_name)
                await step_fn()
            except Exception as e:
                logger.error(
                    "[%d/14] FAILED: %s — %s\n%s",
                    step_num, step_name, e, traceback.format_exc(),
                )
                await self._update_state("init_error", error=str(e))

        # Step 13: Start command listener (Redis — non-fatal for local dev)
        logger.info("[13/14] Starting command listener")
        try:
            self._command_task = asyncio.create_task(self._command_loop())
        except Exception as e:
            logger.warning("[13/14] Command listener failed (no Redis): %s", e)

        # Step 14: Start embedded HTTP API server
        logger.info("[14/14] Starting HTTP API server on :8000")
        try:
            # Import from same directory as this file
            if _WEB_DIR not in sys.path:
                sys.path.insert(0, _WEB_DIR)
            from engine.http_api import EngineHttpApi
            self._http_api = EngineHttpApi(self)
            await self._http_api.start(host="0.0.0.0", port=8000)
        except Exception as e:
            logger.error("[14/14] HTTP API server failed: %s\n%s", e, traceback.format_exc())

        await self._update_state("running")
        logger.info("=== NexusTrader Engine Service Ready ===")

        # Publish startup complete event
        self._event_bus.publish(Topics.SYSTEM_ALERT, data={
            "type": "engine_started",
            "message": "Trading Engine started successfully",
            "timestamp": time.time(),
        })

    async def stop(self):
        """Graceful shutdown."""
        logger.info("Engine shutting down...")
        self._running = False

        # Stop HTTP API server
        if self._http_api:
            try:
                await self._http_api.stop()
            except Exception as e:
                logger.error("HTTP API stop error: %s", e)

        # Stop intelligence agents
        if hasattr(self, '_coordinator') and self._coordinator:
            try:
                self._coordinator.stop_all()
                logger.info("AgentCoordinator stopped")
            except Exception as e:
                logger.error("AgentCoordinator stop error: %s", e)

        # Stop scanner if running
        if self._scanner and self._scanner._running:
            try:
                self._scanner.stop()
                logger.info("Scanner stopped")
            except Exception as e:
                logger.error("Scanner stop error: %s", e)

        if self._command_task:
            self._command_task.cancel()
            try:
                await self._command_task
            except asyncio.CancelledError:
                pass

        if self._redis_bridge:
            self._redis_bridge.stop()

        await self._update_state("stopped")
        logger.info("Engine stopped")

    # ── Initialization Steps ────────────────────────────────

    async def _init_config(self):
        """Load NexusTrader config.yaml via AppSettings singleton."""
        from config.settings import settings
        self._settings = settings
        key_count = len(self._settings._config) if self._settings._config else 0
        logger.info("Config loaded: %d top-level keys", key_count)

    async def _init_database(self):
        """Initialize core SQLite database (desktop schema)."""
        from core.database.engine import init_database
        init_database()
        logger.info("Database initialized (SQLite)")

    async def _init_exchange(self):
        """Import and store reference to ExchangeManager singleton.

        Syncs the SQLite exchanges table from web_assets.json so that
        exchange_manager.load_active_exchange() connects to the correct
        endpoint (testnet vs demo vs live).  web_assets.json is the
        single source of truth for exchange configuration in the web UI.
        """
        from core.market_data.exchange_manager import exchange_manager
        self._exchange_manager = exchange_manager

        # ── Sync DB from web_assets.json (single source of truth) ──
        self._sync_exchange_db_from_assets()

        # Auto-connect: load DB-configured exchange so scanner has data
        if not exchange_manager.get_exchange():
            try:
                ok = exchange_manager.load_active_exchange()
                if ok:
                    logger.info("ExchangeManager auto-connected to active exchange")
                else:
                    logger.warning("ExchangeManager: no active exchange in DB")
            except Exception as exc:
                logger.warning("ExchangeManager auto-connect failed: %s", exc)
        logger.info("ExchangeManager ready")

    def _sync_exchange_db_from_assets(self):
        """Sync SQLite exchanges table from web_assets.json.

        The web UI manages exchanges via AssetStore (web_assets.json),
        but exchange_manager reads from SQLite.  This method ensures
        the DB reflects the web_assets.json config so the ccxt instance
        connects to the correct endpoint (testnet vs demo vs live).

        Only updates mode flags (sandbox_mode, demo_mode) for the active
        exchange.  Credentials are NOT touched — they remain encrypted
        in the DB as-is.
        """
        try:
            import json as _json
            from pathlib import Path
            from core.database.engine import get_session
            from core.database.models import Exchange as ExchangeModel

            store_path = Path(__file__).resolve().parent.parent.parent / "data" / "web_assets.json"
            if not store_path.exists():
                logger.debug("web_assets.json not found — skipping DB sync")
                return

            data = _json.loads(store_path.read_text())
            active_asset = None
            for ex in data.get("exchanges", []):
                if ex.get("is_active"):
                    active_asset = ex
                    break

            if not active_asset:
                logger.debug("No active exchange in web_assets.json — skipping DB sync")
                return

            asset_mode = active_asset.get("mode", "unknown")
            asset_name = active_asset.get("name", "Unknown")
            target_sandbox = (asset_mode == "sandbox")
            target_demo = (asset_mode == "demo")

            with get_session() as session:
                db_model = session.query(ExchangeModel).filter_by(is_active=True).first()
                if not db_model:
                    logger.debug("No active exchange in DB — skipping sync")
                    return

                changed = False
                if db_model.sandbox_mode != target_sandbox:
                    logger.info(
                        "DB sync: sandbox_mode %s → %s (from web_assets.json '%s')",
                        db_model.sandbox_mode, target_sandbox, asset_name,
                    )
                    db_model.sandbox_mode = target_sandbox
                    changed = True
                if getattr(db_model, "demo_mode", False) != target_demo:
                    logger.info(
                        "DB sync: demo_mode %s → %s (from web_assets.json '%s')",
                        getattr(db_model, "demo_mode", False), target_demo, asset_name,
                    )
                    db_model.demo_mode = target_demo
                    changed = True

                if changed:
                    session.commit()
                    logger.info(
                        "Exchange DB synced from web_assets.json: mode='%s' "
                        "(sandbox=%s, demo=%s)",
                        asset_mode, target_sandbox, target_demo,
                    )
                else:
                    logger.debug("Exchange DB already matches web_assets.json — no sync needed")

        except Exception as exc:
            logger.warning("Exchange DB sync from web_assets.json failed: %s", exc)

    async def _init_orchestrator(self):
        """Import and store reference to OrchestratorEngine singleton."""
        from core.orchestrator.orchestrator_engine import get_orchestrator
        self._orchestrator = get_orchestrator()
        logger.info("OrchestratorEngine ready")

    async def _init_agents(self):
        """Initialize AgentCoordinator and start all 23 intelligence agents.

        The Qt shim (core_patch/qt_shim.py) replaces QThread with
        threading.Thread, so agents run as daemon threads — no Qt
        event loop required.

        Because _init_exchange (step 5) already published
        EXCHANGE_CONNECTED before this step runs, the coordinator
        won't receive that event.  We call start_all() explicitly.
        """
        from core.agents.agent_coordinator import get_coordinator

        coordinator = get_coordinator()          # creates singleton
        self._coordinator = coordinator          # keep reference for shutdown

        # Exchange was already connected in step 5 — start agents now
        if not coordinator.is_running:
            coordinator.start_all()

        logger.info(
            "AgentCoordinator ready — %d agents running",
            len(coordinator._agents),
        )

    async def _init_executor(self):
        """Set order_router mode from exchange config, then get active executor.

        Reads the active exchange from web_assets.json:
          - mode "sandbox" or "live" → order_router "live" → LiveBridge
          - mode "demo" or absent    → order_router "paper" → PaperExecutor

        When live mode is selected, initializes the full Phase 8 subsystem
        (ExchangeAdapter, IdempotencyStore, LiveExecutor, ReconciliationEngine,
        RecoveryManager) and injects them into LiveBridge via set_components().
        This mirrors the desktop main.py Phase 8 initialization.

        All downstream ``self._pe`` references automatically get the
        correct executor.
        """
        from core.execution.order_router import order_router

        # Determine execution mode from the active exchange config
        exchange_mode = self._get_active_exchange_mode()
        if exchange_mode in ("sandbox", "live"):
            order_router.set_mode("live")
            logger.info(
                "Exchange mode '%s' → order_router set to LIVE (LiveBridge)",
                exchange_mode,
            )
            # Initialize Phase 8 LiveBridge subsystem
            self._init_phase8_live_bridge()
        else:
            logger.info(
                "Exchange mode '%s' → order_router stays PAPER (PaperExecutor)",
                exchange_mode,
            )

        self._pe = order_router.active_executor

        # Log which executor we got
        executor_name = type(self._pe).__name__
        router_mode = getattr(order_router, '_mode', 'unknown')
        logger.info(
            "Executor ready: %s (mode=%s, exchange=%s)",
            executor_name, router_mode, exchange_mode,
        )

    def _init_phase8_live_bridge(self):
        """Initialize Phase 8 live execution subsystem and inject into LiveBridge.

        Mirrors the desktop main.py Phase 8 initialization sequence:
        1. Get CCXT exchange instance from exchange_manager
        2. Create ExchangeAdapter (CCXT wrapper with retry/error classification)
        3. Create IdempotencyStore (persistent order dedup)
        4. Create Phase8 LiveExecutor (10-state order FSM)
        5. Create ReconciliationEngine + RecoveryManager
        6. Inject all into LiveBridge via set_components()
        7. Run startup recovery (exchange state → local state)
        """
        try:
            from core.market_data.exchange_manager import exchange_manager as _em

            ccxt_exchange = _em.get_exchange()
            if ccxt_exchange is None:
                logger.warning(
                    "Phase 8 init: no CCXT exchange instance — "
                    "LiveBridge will report capital=0 until exchange connects"
                )
                return

            logger.info("=" * 50)
            logger.info("  PHASE 8 LIVE SUBSYSTEM INITIALIZATION (web)")
            logger.info("  Exchange mode: %s", _em.mode)
            logger.info("=" * 50)

            # 1. ExchangeAdapter
            from core.intraday.live import (
                ExchangeAdapter,
                IdempotencyStore,
                LiveExecutor as Phase8LiveExecutor,
                OrderReconciliationEngine,
                RestartRecoveryManager,
            )
            from pathlib import Path

            adapter = ExchangeAdapter(exchange=ccxt_exchange)
            logger.info("Phase 8 (web): ExchangeAdapter ready")

            # 2. IdempotencyStore
            store_path = Path(__file__).resolve().parent.parent.parent / "data" / "idempotency_store.json"
            store_path.parent.mkdir(parents=True, exist_ok=True)
            idem_store = IdempotencyStore(store_path=store_path)
            loaded = idem_store.load()
            logger.info("Phase 8 (web): IdempotencyStore ready (%d entries loaded)", loaded)

            # 3. Phase 8 LiveExecutor (10-state FSM)
            p8_executor = Phase8LiveExecutor(
                exchange_adapter=adapter,
                idempotency_store=idem_store,
            )
            logger.info("Phase 8 (web): LiveExecutor ready (10-state FSM active)")

            # 4. ReconciliationEngine + RecoveryManager
            recon_engine = OrderReconciliationEngine(exchange_adapter=adapter)
            recovery_mgr = RestartRecoveryManager(
                exchange_adapter=adapter,
                idempotency_store=idem_store,
                reconciliation_engine=recon_engine,
            )
            logger.info("Phase 8 (web): ReconciliationEngine + RecoveryManager ready")

            # 5. Inject into LiveBridge
            from core.execution.live_bridge import live_bridge
            live_bridge.set_components(
                exchange_adapter=adapter,
                idempotency_store=idem_store,
                phase8_executor=p8_executor,
                reconciliation_engine=recon_engine,
                recovery_manager=recovery_mgr,
            )
            logger.info("Phase 8 (web): LiveBridge components injected")

            # 6. Run startup recovery (exchange = single source of truth)
            logger.info("Phase 8 (web): === STARTUP RECOVERY ===")
            report = live_bridge.run_startup_recovery(auto_resolve=True)
            trading_ok = report.get("trading_allowed", False)
            logger.info(
                "Phase 8 (web): Recovery complete — trading_allowed=%s",
                trading_ok,
            )

        except Exception as exc:
            logger.error(
                "Phase 8 (web) initialization failed: %s\n%s",
                exc, traceback.format_exc(),
            )

    def _get_web_ccxt(self):
        """Build or return a cached CCXT instance from web_assets.json credentials.

        Uses the same decryption key as Test Connection (`.nexus_web_key`),
        bypassing exchange_manager which uses a different key (`.nexus_key`).
        """
        now = time.time()
        if self._web_ccxt and (now - self._web_ccxt_ts < self._WEB_CCXT_TTL):
            return self._web_ccxt

        try:
            if not self._http_api:
                return None
            store = self._http_api._store
            active = store.get_active_exchange()
            if not active:
                return None

            creds = store.get_decrypted_creds(active["id"])
            if not creds or not creds.get("api_key") or not creds.get("api_secret"):
                return None

            import ccxt
            exchange_id = active.get("exchange_id", "bybit")
            exchange_class = getattr(ccxt, exchange_id, None)
            if not exchange_class:
                return None

            config = {
                "apiKey": creds["api_key"],
                "secret": creds["api_secret"],
                "enableRateLimit": True,
                "timeout": 15000,
                "recvWindow": 20000,
            }
            if creds.get("passphrase"):
                config["password"] = creds["passphrase"]

            mode = active.get("mode", "live")
            if mode == "sandbox":
                config["sandbox"] = True

            # Bybit Demo: set swap default type
            if mode == "demo" and exchange_id == "bybit":
                config.setdefault("options", {})["defaultType"] = "swap"

            ex = exchange_class(config)

            # Apply demo trading URLs (same logic as Test Connection)
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

            self._web_ccxt = ex
            self._web_ccxt_ts = now
            logger.info("Web CCXT instance built for %s (mode=%s)", exchange_id, mode)
            return ex
        except Exception as exc:
            logger.warning("Failed to build web CCXT instance: %s", exc)
            return None

    async def _fetch_exchange_balance_cached(self) -> float:
        """Return the exchange USDT balance, cached for 30s."""
        now = time.time()
        if now - self._cached_balance_ts < self._BALANCE_CACHE_TTL:
            return self._cached_balance_usdt
        try:
            ex = self._get_web_ccxt()
            if not ex:
                return self._cached_balance_usdt

            loop = asyncio.get_event_loop()
            balance = await loop.run_in_executor(None, ex.fetch_balance)

            usdt_free = 0.0
            if "USDT" in balance:
                usdt_free = float(balance["USDT"].get("free", 0) or 0)
            elif "free" in balance and "USDT" in balance["free"]:
                usdt_free = float(balance["free"]["USDT"] or 0)

            self._cached_balance_usdt = round(usdt_free, 2)
            self._cached_balance_ts = now
        except Exception as exc:
            logger.debug("Exchange balance fetch failed (cached): %s", exc)
        return self._cached_balance_usdt

    async def _fetch_exchange_trades_cached(self) -> list[dict]:
        """Return closed orders from the exchange, cached for 30s."""
        now = time.time()
        if now - self._cached_exchange_trades_ts < self._TRADES_CACHE_TTL:
            return self._cached_exchange_trades
        try:
            ex = self._get_web_ccxt()
            if not ex:
                return self._cached_exchange_trades

            loop = asyncio.get_event_loop()
            raw_orders = await loop.run_in_executor(
                None, lambda: ex.fetch_closed_orders(None, limit=200)
            )

            trades = []
            for o in raw_orders:
                if o.get("status") != "closed":
                    continue
                info = o.get("info", {})
                symbol = o.get("symbol", "")
                side = o.get("side", "")
                avg_price = float(o.get("average", 0) or o.get("price", 0) or 0)
                filled = float(o.get("filled", 0) or 0)
                cost = float(o.get("cost", 0) or 0)
                fee_cost = 0.0
                fee_obj = o.get("fee")
                if fee_obj and isinstance(fee_obj, dict):
                    fee_cost = float(fee_obj.get("cost", 0) or 0)
                closed_pnl = float(info.get("closedPnl", 0) or info.get("realisedPnl", 0) or 0)

                trades.append({
                    "id": o.get("id", ""),
                    "symbol": symbol,
                    "side": side,
                    "entry_price": avg_price,
                    "exit_price": avg_price,
                    "size_usdt": round(cost, 2),
                    "quantity": filled,
                    "pnl_usdt": round(closed_pnl, 2),
                    "fees": round(abs(fee_cost), 4),
                    "closed_at": o.get("datetime", ""),
                    "exit_reason": o.get("type", "limit"),
                    "order_type": o.get("type", ""),
                    "status": o.get("status", ""),
                    "reduce_only": info.get("reduceOnly", False),
                })

            trades.sort(key=lambda t: t.get("closed_at", ""), reverse=True)
            self._cached_exchange_trades = trades
            self._cached_exchange_trades_ts = now
            logger.debug("Fetched %d closed orders from exchange", len(trades))
        except Exception as exc:
            logger.debug("Exchange trades fetch failed (cached): %s", exc)
        return self._cached_exchange_trades

    async def _fetch_exchange_positions_cached(self) -> list[dict]:
        """Return open positions from the exchange, cached for 10s."""
        now = time.time()
        if now - self._cached_exchange_positions_ts < self._POSITIONS_CACHE_TTL:
            return self._cached_exchange_positions
        try:
            ex = self._get_web_ccxt()
            if not ex:
                return self._cached_exchange_positions

            loop = asyncio.get_event_loop()
            raw_positions = await loop.run_in_executor(None, ex.fetch_positions)

            positions = []
            for p in raw_positions:
                contracts = float(p.get("contracts", 0) or 0)
                if contracts == 0:
                    continue
                side_raw = p.get("side", "")
                entry_price = float(p.get("entryPrice", 0) or 0)
                mark_price = float(p.get("markPrice", 0) or 0)
                notional = float(p.get("notional", 0) or 0)
                unrealized_pnl = float(p.get("unrealizedPnl", 0) or 0)
                info = p.get("info", {})

                positions.append({
                    "symbol": p.get("symbol", ""),
                    "side": "long" if side_raw == "long" else "short",
                    "entry_price": entry_price,
                    "current_price": mark_price,
                    "size_usdt": round(abs(notional), 2),
                    "quantity": contracts,
                    "pnl_unrealized": round(unrealized_pnl, 2),
                    "pnl_pct": round(unrealized_pnl / abs(notional) * 100, 2) if notional else 0,
                    "stop_loss": float(info.get("stopLoss", 0) or 0) or None,
                    "take_profit": float(info.get("takeProfit", 0) or 0) or None,
                    "leverage": p.get("leverage", 1),
                    "margin_mode": p.get("marginMode", "cross"),
                    "opened_at": p.get("datetime", ""),
                })

            self._cached_exchange_positions = positions
            self._cached_exchange_positions_ts = now
            logger.debug("Fetched %d open positions from exchange", len(positions))
        except Exception as exc:
            logger.debug("Exchange positions fetch failed (cached): %s", exc)
        return self._cached_exchange_positions

    def _get_active_exchange_mode(self) -> str:
        """Read the active exchange mode from web_assets.json.

        Returns 'demo', 'sandbox', 'live', or 'unknown'.
        """
        try:
            import json as _json
            from pathlib import Path
            store_path = Path(__file__).resolve().parent.parent.parent / "data" / "web_assets.json"
            if store_path.exists():
                data = _json.loads(store_path.read_text())
                for ex in data.get("exchanges", []):
                    if ex.get("is_active"):
                        return ex.get("mode", "unknown")
        except Exception as exc:
            logger.warning("Could not read exchange mode from web_assets.json: %s", exc)
        return "unknown"

    async def _init_crash_defense(self):
        """Initialize CrashDefenseController and inject executor."""
        from core.risk.crash_defense_controller import (
            get_crash_defense_controller,
        )
        cdc = get_crash_defense_controller()
        if self._pe:
            cdc.set_executor(self._pe)
            logger.info("CrashDefenseController ready (executor injected)")
        else:
            logger.warning(
                "CrashDefenseController ready but no PaperExecutor to inject",
            )

    async def _init_notifications(self):
        """Import NotificationManager singleton."""
        from core.notifications.notification_manager import notification_manager
        self._notification_mgr = notification_manager
        logger.info(
            "NotificationManager ready: %s",
            type(notification_manager).__name__,
        )

    async def _init_positions(self):
        """Load open positions and trade history into PaperExecutor."""
        if not self._pe:
            logger.warning("No PaperExecutor — skipping position load")
            return
        # Capital load order per CLAUDE.md:
        #   1. _load_open_positions (JSON) first
        #   2. _load_history (SQLite) overwrites capital — authoritative
        if hasattr(self._pe, "_load_open_positions"):
            self._pe._load_open_positions()
            logger.info("Open positions loaded from JSON")
        if hasattr(self._pe, "_load_history"):
            self._pe._load_history()
            logger.info("Trade history loaded from SQLite (capital authoritative)")

    async def _init_scanner(self):
        """Import scanner singleton and auto-start if configured."""
        from core.scanning.scanner import scanner
        self._scanner = scanner

        # Phase 3B: capture full per-symbol scan results (all symbols, not just approved)
        # for the pipeline-status endpoint.
        self._last_pipeline_results: list[dict] = []
        self._last_pipeline_ts: str = ""
        self._last_scan_metrics: dict = {}
        # Regime snapshot ring buffer — survives page navigation/refresh
        self._regime_snapshots: list[dict] = []  # newest first, max 20
        self._MAX_REGIME_SNAPSHOTS = 20
        if hasattr(self._scanner, "scan_all_results"):
            self._scanner.scan_all_results.connect(self._on_scan_all_results)
            logger.info("Engine: connected scan_all_results signal for pipeline-status")
        if hasattr(self._scanner, "scan_metrics_updated"):
            self._scanner.scan_metrics_updated.connect(self._on_scan_metrics_updated)
            logger.info("Engine: connected scan_metrics_updated signal for phase timing")

        # ── Auto-execute: confirmed_ready → execution pathway ──────
        if hasattr(self._scanner, "confirmed_ready"):
            self._scanner.confirmed_ready.connect(self._on_confirmed_ready)
            logger.info("Engine: connected confirmed_ready signal for auto-execution")
        from core.scanning.auto_execute_guard import AutoExecuteState
        self._ae_state = AutoExecuteState()

        # config.yaml: scanner.auto_execute MUST always be true per CLAUDE.md
        auto_start = True
        if self._settings:
            auto_start = self._settings.get("scanner.auto_execute", True)
        if auto_start:
            self._scanner.start()
            logger.info("Scanner auto-started (QTimer-based timers active)")
            # v3: asyncio-based scan loop as safety net.
            # The Qt shim's QTimer runs in daemon threads which can silently die.
            # This asyncio task ensures scans fire on candle boundaries regardless.
            self._scan_loop_task = asyncio.create_task(self._scan_loop())
            logger.info("Engine: asyncio scan loop started as safety net")
        else:
            logger.info("Scanner imported (auto_execute=false, idle)")

    async def _scan_loop(self):
        """
        Asyncio-based scan loop that fires scans on candle boundaries.
        Acts as a safety net for the QTimer-based scanner timers which
        may fail silently in the headless (non-Qt) environment.
        """
        from core.scanning.scanner import TF_POLL_SECONDS, _seconds_to_next_candle
        tf = self._scanner._timeframe if self._scanner else "30m"
        interval_s = TF_POLL_SECONDS.get(tf, 1800)

        # Wait for the first candle boundary
        first_delay = _seconds_to_next_candle(tf)
        logger.info("Engine scan loop: first aligned scan in %ds (TF=%s)", first_delay, tf)
        await asyncio.sleep(first_delay)

        while self._running:
            try:
                if self._scanner and self._scanner._running:
                    if not self._scanner._any_scan_active:
                        logger.info("Engine scan loop: triggering aligned scan")
                        self._scanner._trigger_scan()
                    else:
                        logger.info("Engine scan loop: scan already active, skipping")
                        # Safety: if scan has been active for > 120s, force-reset
                        if (self._scanner._worker_started_at and
                                time.time() - self._scanner._worker_started_at > 120):
                            logger.warning("Engine scan loop: scan stuck for >120s, force-resetting _any_scan_active")
                            self._scanner._any_scan_active = False
                            self._scanner._worker = None
                            self._scanner._worker_started_at = None
            except Exception as e:
                logger.error("Engine scan loop error: %s", e, exc_info=True)

            # Sleep until next candle boundary
            next_delay = _seconds_to_next_candle(tf)
            await asyncio.sleep(next_delay)

    def _on_scan_all_results(self, results: list):
        """Store per-symbol scan results (all symbols incl. rejected/filtered)."""
        from datetime import datetime
        self._last_pipeline_results = results or []
        self._last_pipeline_ts = datetime.utcnow().isoformat()
        logger.debug("Engine: stored %d pipeline results", len(self._last_pipeline_results))

        # ── Capture regime snapshot for Market Regime page ──────────
        # Extract regime per symbol from this scan cycle and store
        # in a ring buffer so the frontend can show history across
        # page navigations and refreshes.
        regimes: dict[str, str] = {}
        for r in self._last_pipeline_results:
            if isinstance(r, dict) and r.get("regime"):
                regimes[r["symbol"]] = r["regime"]
        if regimes:
            snap = {
                "timestamp": self._last_pipeline_ts,
                "regimes": regimes,
            }
            # Dedup: skip if timestamp matches the most recent snapshot
            if (not self._regime_snapshots
                    or self._regime_snapshots[0]["timestamp"] != snap["timestamp"]):
                self._regime_snapshots.insert(0, snap)
                self._regime_snapshots = self._regime_snapshots[:self._MAX_REGIME_SNAPSHOTS]
                logger.debug("Engine: regime snapshot captured (%d total)", len(self._regime_snapshots))

    def _on_scan_metrics_updated(self, metrics_dict: dict):
        """Store scan cycle metrics for phase timing display."""
        self._last_scan_metrics = metrics_dict or {}
        logger.debug("Engine: stored scan metrics (total=%.0fms)", metrics_dict.get("total_cycle_ms", 0))

    # ── Auto-Execute (confirmed_ready) ─────────────────────

    def _on_confirmed_ready(self, confirmed_candidates: list):
        """Handle LTF-confirmed candidates — execution pathway.

        Mirrors the Qt GUI's IDSSScannerTab._on_confirmed_ready flow:
          1. Read portfolio state from PaperExecutor
          2. Run auto_execute_guard.run_batch() for safeguard checks
          3. Build OrderCandidate and submit via order_router
        """
        if not confirmed_candidates:
            return
        if self._trading_paused:
            logger.info("Engine: auto-execute skipped — trading paused")
            return

        logger.info(
            "Engine: %d LTF-confirmed candidate(s) received for execution",
            len(confirmed_candidates),
        )

        from core.scanning.auto_execute_guard import run_batch as _run_batch
        from config.settings import settings as _s

        self._ae_state.reset_if_new_day()

        if not self._pe:
            logger.warning("Engine: auto-execute skipped — PaperExecutor not initialised")
            return

        try:
            open_positions = self._pe.get_open_positions()
            drawdown_pct = self._pe.drawdown_pct
            max_dd = float(_s.get("risk.max_portfolio_drawdown_pct", 15.0))
            max_pos = int(_s.get("risk.max_concurrent_positions", 3))
        except Exception as exc:
            logger.error("Engine: auto-execute could not read portfolio state: %s", exc)
            return

        tf = self._scanner._timeframe if self._scanner else "30m"

        to_execute = _run_batch(
            candidates=confirmed_candidates,
            timeframe=tf,
            open_positions=open_positions,
            drawdown_pct=drawdown_pct,
            max_dd_pct=max_dd,
            max_pos=max_pos,
            state=self._ae_state,
        )

        for c in to_execute:
            self._do_auto_execute_one(c)

    def _do_auto_execute_one(self, c: dict) -> bool:
        """Build OrderCandidate from candidate dict and submit via order_router."""
        sym = c.get("symbol", "?")
        try:
            from core.meta_decision.order_candidate import OrderCandidate
            from core.execution.order_router import order_router
            from core.market_data.exchange_manager import exchange_manager
            from datetime import datetime, timedelta

            model_entry = c.get("entry_price") or 0.0
            stop = c.get("stop_loss_price", 0.0)
            tp = c.get("take_profit_price", 0.0)
            size = c.get("position_size_usdt", 40.0)

            # Fetch current market price for market-order fill
            market_price = 0.0
            try:
                ticker = exchange_manager.fetch_ticker(sym)
                if ticker:
                    market_price = float(ticker.get("last") or 0.0)
            except Exception as exc:
                logger.debug("Engine auto-execute: ticker fetch failed for %s: %s", sym, exc)

            entry = market_price if market_price > 0 else model_entry
            if market_price > 0 and model_entry > 0:
                diff_pct = abs(entry - model_entry) / model_entry * 100
                logger.info(
                    "Engine auto-execute: %s market price %.4f vs model entry %.4f (Δ%.2f%%)",
                    sym, market_price, model_entry, diff_pct,
                )

            candidate = OrderCandidate(
                symbol=sym,
                side=c.get("side", "buy"),
                entry_type="market",
                entry_price=entry if entry else None,
                stop_loss_price=stop,
                take_profit_price=tp,
                position_size_usdt=size,
                score=c.get("score", 0.6),
                models_fired=c.get("models_fired", []),
                regime=c.get("regime", "unknown"),
                rationale=c.get("rationale", "Auto-executed by web engine"),
                timeframe=c.get("timeframe", "30m"),
                atr_value=c.get("atr_value", 0.0),
                approved=True,
                expiry=datetime.utcnow() + timedelta(hours=4),
            )
            candidate.symbol_weight = float(c.get("symbol_weight", 1.0) or 1.0)
            candidate.adjusted_score = float(c.get("adjusted_score", c.get("score", 0.6)) or 0.6)

            ok = order_router.submit(candidate)
            if ok:
                price_str = f"{entry:,.4f}" if entry else "market"
                logger.info("Engine auto-execute: submitted %s %s @ %s", sym, c.get("side"), price_str)

                # Mark candidate as EXECUTED in the CandidateStore
                staged_id = c.get("staged_candidate_id")
                if staged_id:
                    try:
                        from core.scanning.candidate_store import get_candidate_store
                        get_candidate_store().mark_executed(staged_id)
                    except Exception as exc:
                        logger.warning("Engine auto-execute: could not mark %s as EXECUTED: %s", staged_id, exc)
            else:
                logger.warning("Engine auto-execute: order_router rejected %s", sym)
            return ok
        except Exception as exc:
            logger.error("Engine auto-execute: exception for %s: %s", sym, exc, exc_info=True)
            return False

    # ── Command Loop ────────────────────────────────────────

    async def _command_loop(self):
        """
        Listen for commands on Redis queue nexus:engine:commands.
        Process each command and reply on nexus:engine:replies:{command_id}.

        Non-fatal: if Redis is unavailable (local dev), logs warning and exits.
        The HTTP API serves as the primary command interface in local dev mode.
        """
        try:
            import redis.asyncio as aioredis
        except ImportError:
            logger.warning("redis.asyncio not available — command loop disabled (HTTP API active)")
            return

        try:
            r = aioredis.from_url(REDIS_URL, decode_responses=True)
            # Test connection
            await r.ping()
            logger.info("Command listener started on nexus:engine:commands")
        except Exception as e:
            logger.warning("Redis unavailable — command loop disabled (HTTP API active): %s", e)
            return

        try:
            while self._running:
                try:
                    result = await r.blpop("nexus:engine:commands", timeout=5)
                    if result is None:
                        continue

                    _, raw = result
                    command = json.loads(raw)
                    command_id = command.get("command_id", "unknown")
                    action = command.get("action", "")
                    params = command.get("params", {})

                    logger.info("Command received: %s (id=%s)", action, command_id)
                    reply = await self._handle_command(action, params)
                    reply["command_id"] = command_id

                    reply_key = f"nexus:engine:replies:{command_id}"
                    await r.rpush(reply_key, json.dumps(reply))
                    await r.expire(reply_key, 60)  # TTL: 1 minute

                except json.JSONDecodeError as e:
                    logger.error("Malformed command: %s", e)
                except Exception as e:
                    logger.error("Command processing error: %s", e)
        except asyncio.CancelledError:
            pass
        finally:
            await r.aclose()

    async def _handle_command(self, action: str, params: dict) -> dict:
        """Route and execute a command. Returns response dict."""
        handlers = {
            "get_positions": self._cmd_get_positions,
            "get_portfolio": self._cmd_get_portfolio,
            "get_config": self._cmd_get_config,
            "start_scanner": self._cmd_start_scanner,
            "stop_scanner": self._cmd_stop_scanner,
            "pause_trading": self._cmd_pause_trading,
            "resume_trading": self._cmd_resume_trading,
            "close_position": self._cmd_close_position,
            "close_all_positions": self._cmd_close_all,
            "refresh_data": self._cmd_refresh_data,
            "get_dashboard": self._cmd_get_dashboard,
            "get_crash_defense": self._cmd_get_crash_defense,
            "get_scanner_results": self._cmd_get_scanner_results,
            "get_pipeline_status": self._cmd_get_pipeline_status,
            "get_watchlist": self._cmd_get_watchlist,
            "get_agent_status": self._cmd_get_agent_status,
            "get_signals": self._cmd_get_signals,
            "get_risk_status": self._cmd_get_risk_status,
            "get_trade_history": self._cmd_get_trade_history,
            "update_config": self._cmd_update_config,
            "get_system_health": self._cmd_get_system_health,
            "trigger_scan": self._cmd_trigger_scan,
            "kill_switch": self._cmd_kill_switch,
            "get_performance_by_regime": self._cmd_get_performance_by_regime,
            "get_regime_transitions": self._cmd_get_regime_transitions,
            "get_drawdown_curve": self._cmd_get_drawdown_curve,
            "get_rolling_metrics": self._cmd_get_rolling_metrics,
            "get_r_distribution": self._cmd_get_r_distribution,
            "get_duration_analysis": self._cmd_get_duration_analysis,
            "get_current_regime": self._cmd_get_current_regime,
            "get_regime_history": self._cmd_get_regime_history,
            # Demo Monitor (Phase 8H)
            "get_active_positions": self._cmd_get_active_positions,
            "get_portfolio_state": self._cmd_get_portfolio_state,
            "get_live_pnl": self._cmd_get_live_pnl,
            "get_risk_state": self._cmd_get_risk_state,
            "get_recent_trades_monitor": self._cmd_get_recent_trades_monitor,
            # Exchange Management (Phase 8B)
            "exchange.test_connection": self._cmd_exchange_test_connection,
            "exchange.load_active": self._cmd_exchange_load_active,
            "exchange.sync_assets": self._cmd_exchange_sync_assets,
            "exchange.disconnect": self._cmd_exchange_disconnect,
            "exchange.status": self._cmd_exchange_status,
            "exchange.fetch_balance": self._cmd_exchange_fetch_balance,
        }

        handler = handlers.get(action)
        if handler is None:
            return {"status": "error", "detail": f"Unknown action: {action}"}

        try:
            return await handler(params)
        except Exception as e:
            logger.error("Command %s failed: %s", action, e, exc_info=True)
            return {"status": "error", "detail": str(e)}

    # ── Command Handlers (wired to real core/ components) ───

    async def _cmd_get_positions(self, params: dict) -> dict:
        """Return all open positions from the active exchange."""
        positions = await self._fetch_exchange_positions_cached()
        return {"status": "ok", "positions": positions, "count": len(positions)}

    async def _cmd_get_portfolio(self, params: dict) -> dict:
        """Return portfolio snapshot: capital, stats, production status."""
        if not self._pe:
            return {"status": "error", "detail": "PaperExecutor not initialized"}
        stats = self._pe.get_stats()
        prod_status = self._pe.get_production_status()
        return {
            "status": "ok",
            "portfolio": {
                "capital_usdt": prod_status.get("capital_usdt", 0.0),
                "peak_capital_usdt": prod_status.get("peak_capital_usdt", 0.0),
                "total_return_pct": prod_status.get("total_return_pct", 0.0),
                "drawdown_pct": prod_status.get("drawdown_pct", 0.0),
                "open_positions": prod_status.get("open_positions", 0),
                "total_trades": stats.get("total_trades", 0),
                "win_rate": stats.get("win_rate", 0.0),
                "profit_factor": stats.get("profit_factor", 0.0),
                "total_pnl_usdt": stats.get("total_pnl_usdt", 0.0),
                "trading_paused": self._trading_paused,
            },
        }

    async def _cmd_get_config(self, params: dict) -> dict:
        """Return the live runtime config dict.

        Optional ``section`` param returns only that top-level key.
        """
        if not self._settings:
            return {"status": "error", "detail": "Settings not loaded"}
        section = params.get("section")
        if section:
            data = self._settings.get_section(section)
            return {"status": "ok", "config": {section: data}}
        # Full config (safe — contains no secrets; API keys are in vault)
        return {"status": "ok", "config": dict(self._settings._config)}

    async def _cmd_start_scanner(self, params: dict) -> dict:
        """Start the IDSS asset scanner."""
        if not self._scanner:
            return {"status": "error", "detail": "Scanner not initialized"}
        if self._scanner._running:
            return {"status": "ok", "message": "Scanner already running"}
        self._scanner.start()
        logger.info("Scanner started via command")
        self._event_bus.publish(Topics.SYSTEM_ALERT, data={
            "type": "scanner_started",
            "message": "Scanner started via web command",
            "timestamp": time.time(),
        })
        return {"status": "ok", "message": "Scanner started"}

    async def _cmd_stop_scanner(self, params: dict) -> dict:
        """Stop the IDSS asset scanner."""
        if not self._scanner:
            return {"status": "error", "detail": "Scanner not initialized"}
        if not self._scanner._running:
            return {"status": "ok", "message": "Scanner already stopped"}
        self._scanner.stop()
        logger.info("Scanner stopped via command")
        self._event_bus.publish(Topics.SYSTEM_ALERT, data={
            "type": "scanner_stopped",
            "message": "Scanner stopped via web command",
            "timestamp": time.time(),
        })
        return {"status": "ok", "message": "Scanner stopped"}

    async def _cmd_pause_trading(self, params: dict) -> dict:
        """Pause new trade submissions.

        Sets ``_trading_paused`` flag.  Existing open positions are
        unaffected — only new entry submissions are blocked.  The
        flag is persisted in Redis engine state so the API can read it.
        """
        if self._trading_paused:
            return {"status": "ok", "message": "Trading already paused"}
        self._trading_paused = True
        logger.info("Trading PAUSED via command")
        await self._update_state("running", trading_paused=True)
        self._event_bus.publish(Topics.SYSTEM_ALERT, data={
            "type": "trading_paused",
            "message": "Trading paused via web command",
            "timestamp": time.time(),
        })
        return {"status": "ok", "message": "Trading paused"}

    async def _cmd_resume_trading(self, params: dict) -> dict:
        """Resume trade submissions after a pause."""
        if not self._trading_paused:
            return {"status": "ok", "message": "Trading already active"}
        self._trading_paused = False
        logger.info("Trading RESUMED via command")
        await self._update_state("running", trading_paused=False)
        self._event_bus.publish(Topics.SYSTEM_ALERT, data={
            "type": "trading_resumed",
            "message": "Trading resumed via web command",
            "timestamp": time.time(),
        })
        return {"status": "ok", "message": "Trading resumed"}

    async def _cmd_close_position(self, params: dict) -> dict:
        """Close a specific position by symbol."""
        if not self._pe:
            return {"status": "error", "detail": "PaperExecutor not initialized"}
        symbol = params.get("symbol")
        if not symbol:
            return {"status": "error", "detail": "symbol required"}
        price = params.get("price")  # Optional override price
        closed = self._pe.close_position(symbol, price=price)
        if closed:
            logger.info("Position closed via command: %s", symbol)
            return {"status": "ok", "message": f"Position {symbol} closed"}
        return {
            "status": "error",
            "detail": f"No open position for {symbol}",
        }

    async def _cmd_close_all(self, params: dict) -> dict:
        """Close all open positions."""
        if not self._pe:
            return {"status": "error", "detail": "PaperExecutor not initialized"}
        count = self._pe.close_all()
        logger.info("Close-all via command: %d positions closed", count)
        return {"status": "ok", "message": f"{count} positions closed", "count": count}

    async def _cmd_refresh_data(self, params: dict) -> dict:
        """Trigger a data refresh via ExchangeManager.fetch_tickers().

        Uses the watchlist from config or defaults (currently 20 symbols).
        """
        if not self._exchange_manager:
            return {"status": "error", "detail": "ExchangeManager not initialized"}
        symbols = [
            "BTC/USDT", "ETH/USDT", "BNB/USDT", "XRP/USDT", "SOL/USDT",
            "TRX/USDT", "DOGE/USDT", "ADA/USDT", "BCH/USDT", "HYPE/USDT",
            "LINK/USDT", "XLM/USDT", "AVAX/USDT", "HBAR/USDT", "SUI/USDT",
            "NEAR/USDT", "ICP/USDT", "ONDO/USDT", "ALGO/USDT", "RENDER/USDT",
        ]
        if self._settings:
            cfg_symbols = self._settings.get("scanner.watchlist", None)
            if cfg_symbols and isinstance(cfg_symbols, list):
                symbols = cfg_symbols
        tickers = self._exchange_manager.fetch_tickers(symbols)
        logger.info("Data refresh via command: %d tickers fetched", len(tickers))
        # Publish refreshed tickers to event bus for WebSocket clients
        self._event_bus.publish("ticker.update", data={
            "tickers": {
                sym: {
                    "last": t.get("last"),
                    "bid": t.get("bid"),
                    "ask": t.get("ask"),
                    "volume": t.get("quoteVolume"),
                }
                for sym, t in tickers.items()
            },
            "timestamp": time.time(),
        })
        return {
            "status": "ok",
            "message": f"{len(tickers)} tickers refreshed",
            "symbols": list(tickers.keys()),
        }

    async def _cmd_get_dashboard(self, params: dict) -> dict:
        """Aggregated dashboard snapshot: portfolio + crash defense + recent trades."""
        result = {"status": "ok"}

        # Fetch exchange balance (cached 30s) for capital display
        exchange_balance = await self._fetch_exchange_balance_cached()

        # Portfolio
        if self._pe:
            prod = self._pe.get_production_status()
            stats = self._pe.get_stats()
            # Use exchange balance when available, fall back to PaperExecutor capital
            capital = exchange_balance if exchange_balance > 0 else prod.get("capital_usdt", 0.0)
            result["portfolio"] = {
                "capital_usdt": capital,
                "peak_capital_usdt": prod.get("peak_capital_usdt", 0.0),
                "total_return_pct": prod.get("total_return_pct", 0.0),
                "drawdown_pct": prod.get("drawdown_pct", 0.0),
                "session_pnl_usdt": prod.get("session_pnl_usdt", 0.0),
                "open_positions": prod.get("open_positions", 0),
                "open_symbols": prod.get("open_symbols", []),
                "total_trades": stats.get("total_trades", 0),
                "win_rate": stats.get("win_rate", 0.0),
                "profit_factor": stats.get("profit_factor", 0.0),
                "total_pnl_usdt": stats.get("total_pnl_usdt", 0.0),
                "avg_r": stats.get("avg_r", 0.0),
                "avg_rr": stats.get("avg_rr", 0.0),
                "last_10_outcomes": prod.get("last_10_outcomes", []),
                "current_losing_streak": prod.get("current_losing_streak", 0),
            }
        else:
            result["portfolio"] = {}

        # Crash defense
        try:
            from core.risk.crash_defense_controller import get_crash_defense_controller
            cdc = get_crash_defense_controller()
            result["crash_defense"] = {
                "tier": cdc.current_tier,
                "is_defensive": cdc.is_defensive,
                "is_safe_mode": getattr(cdc, "is_safe_mode", False),
            }
        except Exception:
            result["crash_defense"] = {"tier": "UNKNOWN", "is_defensive": False}

        # Engine state
        result["engine"] = {
            "state": "running" if self._running else "stopped",
            "trading_paused": self._trading_paused,
            "scanner_running": bool(self._scanner and self._scanner._running),
        }

        return result

    async def _cmd_get_crash_defense(self, params: dict) -> dict:
        """Detailed crash defense state including actions log."""
        try:
            from core.risk.crash_defense_controller import get_crash_defense_controller
            cdc = get_crash_defense_controller()
            return {
                "status": "ok",
                "crash_defense": {
                    "tier": cdc.current_tier,
                    "is_defensive": cdc.is_defensive,
                    "is_safe_mode": getattr(cdc, "is_safe_mode", False),
                    "actions_log": cdc.get_actions_log() if hasattr(cdc, "get_actions_log") else [],
                },
            }
        except Exception as e:
            return {"status": "error", "detail": f"CrashDefense unavailable: {e}"}

    async def _cmd_get_scanner_results(self, params: dict) -> dict:
        """Return results from the most recent scan cycle."""
        if not self._scanner:
            return {"status": "error", "detail": "Scanner not initialized"}

        # Scanner stores last results via event bus; read from cached results
        results = []
        if hasattr(self._scanner, '_last_scan_results'):
            results = self._scanner._last_scan_results or []
        elif hasattr(self._scanner, '_last_candidates'):
            results = self._scanner._last_candidates or []

        return {
            "status": "ok",
            "results": results,
            "count": len(results),
            "scanner_running": self._scanner._running,
        }

    async def _cmd_get_pipeline_status(self, params: dict) -> dict:
        """Return full per-asset pipeline status for all tradable assets.

        Phase 3B: merges DB tradable universe with scanner results so the
        Scan page shows every tradable asset and where it stands in the
        scan -> regime -> strategy -> confluence -> risk -> trade pipeline.

        Assets that have not yet been scanned show status 'Waiting'.
        """
        # ── 1. Get tradable universe ──────────────────────────
        # Primary: async DB (PostgreSQL).  Fallback: core SQLite DB.
        # Final fallback: scanner watchlist (config.yaml).
        tradable_assets: list[dict] = []
        try:
            from app.database import get_async_session_factory
            from app.models.trading import Asset, Exchange
            from sqlalchemy import select

            factory = get_async_session_factory()
            async with factory() as session:
                ex_result = await session.execute(
                    select(Exchange).where(Exchange.is_active.is_(True))
                )
                active_ex = ex_result.scalar_one_or_none()
                if active_ex:
                    result = await session.execute(
                        select(Asset)
                        .where(
                            Asset.exchange_id == active_ex.id,
                            Asset.is_tradable.is_(True),
                        )
                        .order_by(Asset.symbol)
                    )
                    for a in result.scalars().all():
                        tradable_assets.append({
                            "asset_id": a.id,
                            "symbol": a.symbol,
                            "allocation_weight": a.allocation_weight,
                        })
        except Exception:
            pass

        # Fallback: core SQLite DB (always available locally)
        if not tradable_assets:
            try:
                from core.database.engine import get_session
                from core.database.models import Exchange as ExModel, Asset as AsModel
                with get_session() as session:
                    active_ex = session.query(ExModel).filter_by(is_active=True).first()
                    if active_ex:
                        assets = (session.query(AsModel)
                                  .filter_by(exchange_id=active_ex.id, is_tradable=True)
                                  .order_by(AsModel.symbol).all())
                        for a in assets:
                            tradable_assets.append({
                                "asset_id": a.id,
                                "symbol": a.symbol,
                                "allocation_weight": getattr(a, "allocation_weight", 1.0) or 1.0,
                            })
            except Exception as e:
                logger.warning("_cmd_get_pipeline_status: SQLite fallback failed: %s", e)

        # Final fallback: scanner watchlist symbols
        if not tradable_assets and self._scanner:
            try:
                symbols = self._scanner._watchlist_mgr.get_active_symbols()
                for i, sym in enumerate(symbols):
                    tradable_assets.append({
                        "asset_id": i + 1,
                        "symbol": sym,
                        "allocation_weight": 1.0,
                    })
            except Exception as e:
                logger.warning("_cmd_get_pipeline_status: watchlist fallback failed: %s", e)

        # ── 2. Build scanner results lookup ───────────────────
        scan_by_symbol: dict[str, dict] = {}
        for r in getattr(self, "_last_pipeline_results", []):
            sym = r.get("symbol", "")
            if sym:
                scan_by_symbol[sym] = r

        # ── 3. Merge: every tradable asset gets a pipeline row ─
        pipeline_rows: list[dict] = []
        for asset in tradable_assets:
            sym = asset["symbol"]
            scan = scan_by_symbol.get(sym)

            if scan:
                # Normalize status into pipeline stage
                raw_status = scan.get("status", "")
                pipeline_status = self._normalize_pipeline_status(raw_status, scan)
                diag = scan.get("diagnostics", {})

                # ── MIL diagnostics (promoted to top-level + inside diagnostics) ──
                mil_diag = self._get_mil_diagnostics(sym)

                # ── Decision explainability ───────────────────────
                decision_explanation, block_reasons = self._build_decision_explanation(
                    pipeline_status, raw_status, scan, diag, mil_diag,
                )

                # ── Extract top-level MIL fields ──────────────────
                _mil_breakdown = mil_diag.get("mil_breakdown", {})

                pipeline_rows.append({
                    "asset_id": asset["asset_id"],
                    "symbol": sym,
                    "allocation_weight": asset["allocation_weight"],
                    "price": scan.get("entry_price"),
                    "regime": scan.get("regime", ""),
                    "regime_confidence": diag.get("regime_confidence", 0.0),
                    "models_fired": scan.get("models_fired", []),
                    "models_no_signal": diag.get("models_no_signal", []),
                    "score": scan.get("score", 0.0),
                    "direction": scan.get("side", ""),
                    "status": pipeline_status,
                    "reason": self._pipeline_reason(raw_status, scan),
                    "is_approved": scan.get("is_approved", False),
                    "entry_price": scan.get("entry_price"),
                    "stop_loss": scan.get("stop_loss_price", 0.0),
                    "take_profit": scan.get("take_profit_price", 0.0),
                    "rr_ratio": scan.get("risk_reward_ratio", 0.0),
                    "position_size_usdt": scan.get("position_size_usdt", 0.0),
                    "scanned_at": scan.get("generated_at", ""),
                    # ── Phase S1: promoted MIL fields (top-level) ──
                    "technical_score": diag.get("mil_technical_baseline", 0.0),
                    "final_score": scan.get("score", 0.0),
                    "mil_active": mil_diag.get("mil_active", False),
                    "mil_total_delta": mil_diag.get("mil_total_delta", 0.0),
                    "mil_influence_pct": mil_diag.get("mil_influence_pct", 0.0),
                    "mil_capped": mil_diag.get("mil_capped", False),
                    "mil_dominant_source": mil_diag.get("mil_dominant_source", "none"),
                    "mil_breakdown": _mil_breakdown,
                    # ── Phase S1: decision explainability ──────────
                    "decision_explanation": decision_explanation,
                    "block_reasons": block_reasons,
                    # ── diagnostics (unchanged + MIL pass-through) ─
                    "diagnostics": {
                        "candle_count": diag.get("candle_count", 0),
                        "candle_age_s": diag.get("candle_age_s", 0),
                        "candle_ts_str": diag.get("candle_ts_str", ""),
                        "regime_confidence": diag.get("regime_confidence", 0.0),
                        "regime_probs": diag.get("regime_probs", {}),
                        "all_model_names": diag.get("all_model_names", []),
                        "models_disabled": diag.get("models_disabled", []),
                        "models_fired": diag.get("models_fired", []),
                        "models_no_signal": diag.get("models_no_signal", []),
                        "signal_details": diag.get("signal_details", {}),
                        "indicator_cols_missing": diag.get("indicator_cols_missing", []),
                        "pre_filter_reason": raw_status if raw_status not in ("approved", "pending", "") else "",
                        "rejection_reason": scan.get("rejection_reason", ""),
                        # ── MIL Phase 4A diagnostic keys ─────────
                        **mil_diag,
                    },
                })
            else:
                # Asset is tradable but has not been scanned yet
                pipeline_rows.append({
                    "asset_id": asset["asset_id"],
                    "symbol": sym,
                    "allocation_weight": asset["allocation_weight"],
                    "price": None,
                    "regime": "",
                    "regime_confidence": 0.0,
                    "models_fired": [],
                    "models_no_signal": [],
                    "score": 0.0,
                    "direction": "",
                    "status": "Waiting",
                    "reason": "Not yet scanned",
                    "is_approved": False,
                    "entry_price": None,
                    "stop_loss": 0.0,
                    "take_profit": 0.0,
                    "rr_ratio": 0.0,
                    "position_size_usdt": 0.0,
                    "scanned_at": "",
                    # ── Phase S1: promoted MIL fields (defaults) ──
                    "technical_score": 0.0,
                    "final_score": 0.0,
                    "mil_active": False,
                    "mil_total_delta": 0.0,
                    "mil_influence_pct": 0.0,
                    "mil_capped": False,
                    "mil_dominant_source": "none",
                    "mil_breakdown": {},
                    "decision_explanation": "Not yet scanned",
                    "block_reasons": [],
                    "diagnostics": {},
                })

        # ── 4. Summary stats ──────────────────────────────────
        n_total = len(pipeline_rows)
        n_eligible = sum(1 for r in pipeline_rows if r["status"] == "Eligible")
        n_signals = sum(1 for r in pipeline_rows if r["score"] > 0)
        n_blocked = sum(1 for r in pipeline_rows if r["status"] == "Risk Blocked")

        return {
            "status": "ok",
            "pipeline": pipeline_rows,
            "summary": {
                "total": n_total,
                "eligible": n_eligible,
                "active_signals": n_signals,
                "blocked": n_blocked,
            },
            "scanner_running": bool(self._scanner and self._scanner._running),
            "last_scan_at": getattr(self, "_last_pipeline_ts", ""),
            "source": "db",
            "phase_timing": getattr(self, "_last_scan_metrics", {}),
            "regime_snapshots": getattr(self, "_regime_snapshots", []),
        }

    @staticmethod
    def _normalize_pipeline_status(raw_status: str, scan: dict) -> str:
        """Map scanner raw status to pipeline display status."""
        if scan.get("is_approved"):
            return "Eligible"
        s = raw_status.lower().strip()
        if s in ("approved",):
            return "Eligible"
        if s in ("filtered",):
            return "Pre-Filter"
        if s in ("no signal",):
            return "No Signal"
        if s in ("below threshold",):
            return "No Signal"
        if s in ("no data", "stale data"):
            return "Error"
        if s in ("indicators missing",):
            return "Error"
        if s in ("scan error",):
            return "Error"
        if s in ("pending",):
            # Had a signal but risk gate not yet run (shouldn't happen in batch)
            return "No Signal"
        # Any rejection reason from risk gate
        if scan.get("rejection_reason"):
            return "Risk Blocked"
        if not s or s == "":
            return "Waiting"
        # Regime-related rejections
        if "regime" in s.lower():
            return "Regime Filtered"
        return "Risk Blocked"

    def _get_mil_diagnostics(self, symbol: str) -> dict:
        """
        Collect MIL Phase 4A diagnostic data for a pipeline row.

        Queries the FundingRateAgent cache, CoinglassAgent cache, and
        ConfluenceScorer diagnostics for MIL-enhanced metadata.
        Returns empty dict if MIL is disabled or agents are unavailable.
        Fail-open: never raises.

        Pipeline visibility keys (Section 2):
          mil_active, mil_influence_pct, mil_capped, mil_dominant_source
        """
        result: dict = {}
        try:
            from config.settings import settings
            if not settings.get("mil.global_enabled", False):
                result["mil_active"] = False
                return result

            result["mil_active"] = True

            # ── Funding Rate MIL diagnostics ────────────────
            if settings.get("agents.funding_rate_enhanced", False):
                try:
                    from core.agents.funding_rate_agent import funding_rate_agent
                    if funding_rate_agent is not None:
                        cached = funding_rate_agent.get_symbol_signal(symbol)
                        if cached.get("mil_enhanced"):
                            result["mil_funding_signal"] = cached.get("signal", 0.0)
                            result["mil_funding_percentile"] = cached.get("mil_percentile_24h", 0.0)
                            result["mil_funding_divergence"] = cached.get("mil_divergence_detected", False)
                            result["mil_funding_weighted_rate"] = cached.get("mil_weighted_rate", 0.0)
                except Exception:
                    pass

            # ── OI MIL diagnostics ──────────────────────────
            if settings.get("agents.oi_enhanced", False):
                try:
                    from core.agents.mil.oi_enhanced import get_oi_enhancer
                    enhancer = get_oi_enhancer()
                    diag = enhancer.get_diagnostics()
                    if symbol in (diag.get("history_sizes") or {}):
                        from core.agents.coinglass_agent import coinglass_agent
                        if coinglass_agent is not None:
                            oi_data = coinglass_agent.get_oi_data(symbol)
                            if oi_data and oi_data.get("mil_enhanced"):
                                result["mil_oi_delta"] = oi_data.get("mil_oi_delta_1h", 0.0)
                                result["mil_oi_delta_4h"] = oi_data.get("mil_oi_delta_4h", 0.0)
                                result["mil_liquidation_proximity"] = oi_data.get("mil_liquidation_proximity", 0.0)
                                result["mil_oi_volume_ratio"] = oi_data.get("mil_oi_volume_ratio", 0.0)
                except Exception:
                    pass

            # ── Orchestrator meta-signal ─────────────────────
            try:
                from core.orchestrator.orchestrator_engine import get_orchestrator
                orch = get_orchestrator()
                sig = orch.get_signal()
                if sig:
                    result["mil_orchestrator_meta"] = round(sig.meta_signal, 4)
                    result["mil_veto_active"] = sig.macro_veto
            except Exception:
                pass

            # ── ConfluenceScorer MIL diagnostics — PURE PASS-THROUGH ──
            # All MIL computation (breakdown, dominant source, invariants)
            # is done in ConfluenceScorer.score(). This block ONLY reads
            # stored diagnostics. Zero computation, zero derivation.
            try:
                scorer_diag = self._scorer._last_diagnostics if hasattr(self, "_scorer") else {}
                if scorer_diag.get("mil_technical_baseline"):
                    result["mil_influence_pct"] = scorer_diag.get("mil_delta_pct", 0.0)
                    result["mil_total_delta"] = scorer_diag.get("mil_total_delta", 0.0)
                    result["mil_capped"] = scorer_diag.get("mil_capped", False)
                    result["mil_breakdown"] = scorer_diag.get("mil_breakdown", {})
                    result["mil_dominant_source"] = scorer_diag.get("mil_dominant_source", "none")
                else:
                    result["mil_influence_pct"] = 0.0
                    result["mil_total_delta"] = 0.0
                    result["mil_capped"] = False
                    result["mil_dominant_source"] = "none"
                    result["mil_breakdown"] = {}
            except Exception:
                result["mil_influence_pct"] = 0.0
                result["mil_total_delta"] = 0.0
                result["mil_capped"] = False
                result["mil_dominant_source"] = "none"
                result["mil_breakdown"] = {}

        except Exception:
            pass  # fail-open: return whatever we have

        return result

    def _build_decision_explanation(
        self,
        pipeline_status: str,
        raw_status: str,
        scan: dict,
        diag: dict,
        mil_diag: dict,
    ) -> tuple[str, list[str]]:
        """Build a human-readable decision explanation and block reasons.

        Phase S1: every pipeline row gets a clear textual explanation of
        WHY it reached its current status, plus a list of specific block
        reasons for anything that prevented trade eligibility.

        Returns:
            (decision_explanation, block_reasons)
        """
        block_reasons: list[str] = []
        parts: list[str] = []

        try:
            # ── Data stage ─────────────────────────────────────
            candle_count = diag.get("candle_count", 0)
            if candle_count == 0:
                block_reasons.append("No market data fetched")
                return "No market data available for analysis", block_reasons

            # ── Indicator stage ────────────────────────────────
            missing_cols = diag.get("indicator_cols_missing", [])
            if missing_cols:
                block_reasons.append(f"Missing indicators: {', '.join(missing_cols)}")
                return f"Indicator computation failed ({', '.join(missing_cols)} missing)", block_reasons

            # ── Pre-filter stage ───────────────────────────────
            pre_filter = diag.get("pre_filter_reason", "")
            if pre_filter and raw_status not in ("approved", "pending", ""):
                if pipeline_status == "Pre-Filter":
                    block_reasons.append(f"Pre-filter: {pre_filter}")
                    return f"Rejected by pre-filter: {pre_filter}", block_reasons

            # ── Regime stage ───────────────────────────────────
            regime = scan.get("regime", "")
            regime_conf = diag.get("regime_confidence", 0.0)
            if regime:
                parts.append(f"Regime: {regime} ({regime_conf:.0%} confidence)")
            else:
                parts.append("Regime: unclassified")

            # ── Signal stage ───────────────────────────────────
            models_fired = scan.get("models_fired", [])
            models_no_signal = diag.get("models_no_signal", [])
            if not models_fired:
                block_reasons.append(f"No model generated a signal ({len(models_no_signal)} checked)")
                parts.append(f"No signal from {len(models_no_signal)} models")
                return " → ".join(parts), block_reasons

            parts.append(f"Signals: {', '.join(models_fired)}")

            # ── Confluence stage ───────────────────────────────
            score = scan.get("score", 0.0)
            threshold = diag.get("effective_threshold", 0.0)
            if score <= 0 and threshold > 0:
                block_reasons.append(f"Score {score:.3f} below threshold {threshold:.3f}")
                parts.append(f"Score {score:.3f} < threshold {threshold:.3f}")
                return " → ".join(parts), block_reasons
            elif score > 0:
                parts.append(f"Score: {score:.3f}")

            # ── MIL influence ──────────────────────────────────
            if mil_diag.get("mil_active"):
                pct = mil_diag.get("mil_influence_pct", 0.0)
                capped = mil_diag.get("mil_capped", False)
                if abs(pct) > 0.001:
                    mil_str = f"MIL influence: {pct:+.1%}"
                    if capped:
                        mil_str += " (capped)"
                    parts.append(mil_str)

            # ── Risk gate stage ────────────────────────────────
            rejection = scan.get("rejection_reason", "")
            if rejection:
                block_reasons.append(f"Risk gate: {rejection}")
                parts.append(f"Blocked: {rejection}")
                return " → ".join(parts), block_reasons

            # ── Approved ───────────────────────────────────────
            if scan.get("is_approved"):
                rr = scan.get("risk_reward_ratio", 0.0)
                size = scan.get("position_size_usdt", 0.0)
                parts.append(f"Approved (R:R {rr:.1f}, ${size:.0f})")

            return " → ".join(parts), block_reasons

        except Exception:
            return pipeline_status, block_reasons

    @staticmethod
    def _pipeline_reason(raw_status: str, scan: dict) -> str:
        """Build a human-readable reason string for the pipeline stage."""
        if scan.get("is_approved"):
            return "Trade eligible"
        if scan.get("rejection_reason"):
            return str(scan["rejection_reason"])
        if raw_status and raw_status not in ("approved", "pending", ""):
            return raw_status
        return ""

    async def _cmd_get_watchlist(self, params: dict) -> dict:
        """Return the current scanner watchlist with symbol weights.

        Phase 3A: reads from PostgreSQL Asset.is_tradable as single source
        of truth. Falls back to config.yaml only if PostgreSQL is unavailable
        (desktop-only mode without web backend).
        """
        # ── Primary: PostgreSQL tradable assets ──────────────
        try:
            from app.database import get_async_session_factory
            from app.models.trading import Asset, Exchange
            from sqlalchemy import select

            factory = get_async_session_factory()
            async with factory() as session:
                # Find active exchange
                ex_result = await session.execute(
                    select(Exchange).where(Exchange.is_active.is_(True))
                )
                active_ex = ex_result.scalar_one_or_none()
                if active_ex:
                    result = await session.execute(
                        select(Asset)
                        .where(
                            Asset.exchange_id == active_ex.id,
                            Asset.is_tradable.is_(True),
                        )
                        .order_by(Asset.symbol)
                    )
                    assets = result.scalars().all()
                    if assets:
                        symbols = [a.symbol for a in assets]
                        weights = {a.symbol: a.allocation_weight for a in assets}
                        return {"status": "ok", "symbols": symbols, "weights": weights, "source": "db"}
                    # DB has zero tradable assets — return empty (not fallback)
                    return {"status": "ok", "symbols": [], "weights": {}, "source": "db"}
        except Exception as e:
            logger.warning("_cmd_get_watchlist: PostgreSQL unavailable, falling back to config: %s", e)

        # ── Fallback: config.yaml (desktop-only compatibility) ─
        symbols = [
            "BTC/USDT", "ETH/USDT", "BNB/USDT", "XRP/USDT", "SOL/USDT",
            "TRX/USDT", "DOGE/USDT", "ADA/USDT", "BCH/USDT", "HYPE/USDT",
            "LINK/USDT", "XLM/USDT", "AVAX/USDT", "HBAR/USDT", "SUI/USDT",
            "NEAR/USDT", "ICP/USDT", "ONDO/USDT", "ALGO/USDT", "RENDER/USDT",
        ]
        weights = {
            "BTC/USDT": 1.0, "ETH/USDT": 1.2, "SOL/USDT": 1.3, "BNB/USDT": 0.8, "XRP/USDT": 0.8,
            "TRX/USDT": 0.7, "DOGE/USDT": 0.7, "ADA/USDT": 0.7, "BCH/USDT": 0.7, "HYPE/USDT": 0.7,
            "LINK/USDT": 0.7, "XLM/USDT": 0.7, "AVAX/USDT": 0.7, "HBAR/USDT": 0.7, "SUI/USDT": 0.7,
            "NEAR/USDT": 0.7, "ICP/USDT": 0.7, "ONDO/USDT": 0.7, "ALGO/USDT": 0.7, "RENDER/USDT": 0.7,
        }

        if self._settings:
            cfg_symbols = self._settings.get("scanner.watchlist", None)
            if cfg_symbols and isinstance(cfg_symbols, list):
                symbols = cfg_symbols
            cfg_weights = self._settings.get("scanner.symbol_weights", None)
            if cfg_weights and isinstance(cfg_weights, dict):
                weights = cfg_weights

        return {"status": "ok", "symbols": symbols, "weights": weights, "source": "config"}

    async def _cmd_get_agent_status(self, params: dict) -> dict:
        """Return status of all intelligence agents.

        First tries the real AgentCoordinator; if empty, synthesizes
        per-model agent cards from the latest pipeline scan results so
        the Intelligence page always has useful data.
        """
        agents: dict = {}

        # 1. Try real coordinator
        try:
            from core.agents.agent_coordinator import get_coordinator
            coordinator = get_coordinator()
            if hasattr(coordinator, 'get_status'):
                agents = coordinator.get_status() or {}
        except Exception:
            pass

        # 2. If coordinator returned nothing, synthesize from pipeline
        if not agents:
            pipeline = getattr(self, '_last_pipeline_results', None)
            if not pipeline and self._scanner:
                pipeline = getattr(self._scanner, '_last_scan_results', None)
            if pipeline:
                import datetime
                now = datetime.datetime.utcnow().isoformat() + "Z"
                # Collect all unique models across all assets
                all_models: dict[str, dict] = {}
                for asset in (pipeline if isinstance(pipeline, list) else []):
                    if not isinstance(asset, dict):
                        continue
                    for m in asset.get("models_fired", []):
                        if m not in all_models:
                            all_models[m] = {
                                "running": True, "stale": False,
                                "signal": asset.get("score", 0.0),
                                "confidence": asset.get("regime_confidence", 0.0),
                                "updated_at": asset.get("scanned_at", now),
                                "errors": 0,
                            }
                    for m in asset.get("models_no_signal", []):
                        if m not in all_models:
                            all_models[m] = {
                                "running": True, "stale": False,
                                "signal": 0.0,
                                "confidence": 0.0,
                                "updated_at": asset.get("scanned_at", now),
                                "errors": 0,
                            }
                agents = all_models

        return {"status": "ok", "agents": agents, "count": len(agents)}

    async def _cmd_get_signals(self, params: dict) -> dict:
        """Return recent signal data from the signal pipeline."""
        signals = []
        if True:
            # Try pipeline results first (richer), fall back to scan results
            raw = getattr(self, '_last_pipeline_results', None)
            if not raw and self._scanner:
                raw = getattr(self._scanner, '_last_scan_results', None)
            for r in (raw or []):
                if not isinstance(r, dict):
                    continue
                if r.get("score", 0) > 0 or r.get("models_fired"):
                    signals.append({
                        "symbol": r.get("symbol", ""),
                        "direction": r.get("direction", r.get("side", "")),
                        "score": r.get("score", 0.0),
                        "models": r.get("models_fired", []),
                        "regime": r.get("regime", ""),
                        "entry_price": r.get("entry_price"),
                        "stop_loss": r.get("stop_loss", r.get("stop_loss_price")),
                        "take_profit": r.get("take_profit", r.get("take_profit_price")),
                        "approved": r.get("is_approved", False),
                        "rejection_reason": r.get("reason", ""),
                    })
        return {"status": "ok", "signals": signals, "count": len(signals)}

    async def _cmd_get_risk_status(self, params: dict) -> dict:
        """Return current portfolio risk metrics."""
        result = {"status": "ok", "risk": {}}

        if self._pe:
            positions = self._pe.get_open_positions()
            prod = self._pe.get_production_status()
            result["risk"] = {
                "portfolio_heat_pct": prod.get("portfolio_heat_pct", 0.0),
                "drawdown_pct": prod.get("drawdown_pct", 0.0),
                "open_positions": len(positions),
                "circuit_breaker_on": prod.get("circuit_breaker_on", False),
                "daily_loss_pct": 0.0,
            }

        # Crash defense tier
        try:
            from core.risk.crash_defense_controller import get_crash_defense_controller
            cdc = get_crash_defense_controller()
            result["risk"]["crash_tier"] = cdc.current_tier
            result["risk"]["is_defensive"] = cdc.is_defensive
        except Exception:
            result["risk"]["crash_tier"] = "UNKNOWN"
            result["risk"]["is_defensive"] = False

        return result

    async def _cmd_get_trade_history(self, params: dict) -> dict:
        """Return paginated trade history from the active exchange."""
        page = params.get("page", 1)
        per_page = min(params.get("per_page", 50), 200)  # cap at 200

        # Fetch from exchange (cached 30s)
        closed = await self._fetch_exchange_trades_cached()

        start = (page - 1) * per_page
        end = start + per_page
        page_trades = closed[start:end]

        # Summary stats
        total_wins = sum(1 for t in closed if (t.get("pnl_usdt") or 0) > 0)
        total_losses = sum(1 for t in closed if (t.get("pnl_usdt") or 0) <= 0)
        total_pnl_usdt = round(sum(t.get("pnl_usdt") or 0 for t in closed), 2)
        entry_sum = sum(abs(t.get("size_usdt") or 0) for t in closed)
        total_pnl_pct = round((total_pnl_usdt / entry_sum * 100) if entry_sum > 0 else 0.0, 2)

        return {
            "status": "ok",
            "trades": page_trades,
            "total": len(closed),
            "page": page,
            "per_page": per_page,
            "pages": (len(closed) + per_page - 1) // per_page if per_page > 0 else 0,
            "summary": {
                "wins": total_wins,
                "losses": total_losses,
                "total_pnl_usdt": total_pnl_usdt,
                "total_pnl_pct": total_pnl_pct,
            },
            "data_source": "exchange",
        }

    async def _cmd_update_config(self, params: dict) -> dict:
        """Update runtime configuration values."""
        if not self._settings:
            return {"status": "error", "detail": "Settings not loaded"}

        updates = params.get("updates", {})
        if not updates:
            return {"status": "error", "detail": "No updates provided"}

        updated_keys = []
        for key, value in updates.items():
            self._settings.set(key, value)
            updated_keys.append(key)
            logger.info("Config updated via command: %s = %s", key, value)

        # Persist to disk
        self._settings.save()

        self._event_bus.publish("config.changed", data={
            "updated_keys": updated_keys,
            "timestamp": time.time(),
        })

        return {"status": "ok", "updated_keys": updated_keys}

    async def _cmd_get_system_health(self, params: dict) -> dict:
        """Comprehensive system health check."""
        import threading

        health = {"status": "ok", "components": {}}

        # Thread count
        thread_count = threading.active_count()
        health["components"]["threads"] = {
            "count": thread_count,
            "warning": thread_count > 75,
        }

        # Scanner
        health["components"]["scanner"] = {
            "running": bool(self._scanner and self._scanner._running),
        }

        # PaperExecutor
        health["components"]["executor"] = {
            "initialized": self._pe is not None,
            "open_positions": len(self._pe.get_open_positions()) if self._pe else 0,
        }

        # Exchange
        health["components"]["exchange"] = {
            "initialized": self._exchange_manager is not None,
        }

        # Engine state
        health["components"]["engine"] = {
            "running": self._running,
            "trading_paused": self._trading_paused,
            "uptime_s": time.time() - getattr(self, '_start_time', time.time()),
        }

        return health

    async def _cmd_trigger_scan(self, params: dict) -> dict:
        """Trigger an immediate scan cycle."""
        if not self._scanner:
            return {"status": "error", "detail": "Scanner not initialized"}
        if not self._scanner._running:
            return {"status": "error", "detail": "Scanner not running — start it first"}

        self._scanner.scan_now()
        logger.info("Manual scan triggered via command")
        return {"status": "ok", "message": "Scan cycle triggered"}

    async def _cmd_kill_switch(self, params: dict) -> dict:
        """EMERGENCY: Close all positions, pause trading, stop scanner."""
        result = {"status": "ok", "actions": {}}

        # 1. Close all positions
        if self._pe:
            count = self._pe.close_all()
            result["actions"]["positions_closed"] = count
            logger.critical("KILL SWITCH: %d positions closed", count)

        # 2. Pause trading
        self._trading_paused = True
        result["actions"]["trading_paused"] = True
        await self._update_state("running", trading_paused=True)

        # 3. Stop scanner
        if self._scanner and self._scanner._running:
            self._scanner.stop()
            result["actions"]["scanner_stopped"] = True
            logger.critical("KILL SWITCH: Scanner stopped")

        # 4. Publish emergency event
        self._event_bus.publish(Topics.SYSTEM_ALERT, data={
            "type": "kill_switch_activated",
            "message": "Emergency kill switch activated via web command",
            "timestamp": time.time(),
            "actions": result["actions"],
        })

        logger.critical("KILL SWITCH COMPLETE: %s", result["actions"])
        return result

    # ── Regime Analytics Handlers ───────────────────────────

    async def _cmd_get_performance_by_regime(self, params: dict) -> dict:
        """Return performance metrics grouped by market regime."""
        if not self._pe:
            return {"status": "error", "detail": "PaperExecutor not initialized"}

        try:
            closed_trades = getattr(self._pe, '_closed_trades', [])
            regime_stats = {}

            # Group trades by regime
            for trade in closed_trades:
                regime = trade.get("regime", "uncertain")
                if regime not in regime_stats:
                    regime_stats[regime] = {
                        "trades": [],
                        "count": 0,
                        "wins": 0,
                        "losses": 0,
                        "total_pnl": 0.0,
                        "total_r": 0.0,
                        "total_duration_s": 0.0,
                    }

                stats = regime_stats[regime]
                stats["trades"].append(trade)
                stats["count"] += 1
                pnl = trade.get("pnl_usdt", 0.0)
                stats["total_pnl"] += pnl
                if pnl > 0:
                    stats["wins"] += 1
                elif pnl < 0:
                    stats["losses"] += 1
                stats["total_r"] += trade.get("r_multiple", 0.0)
                duration_s = trade.get("duration_s", 0.0)
                stats["total_duration_s"] += duration_s

            # Calculate final metrics
            total_trades = sum(s["count"] for s in regime_stats.values())
            regimes = []

            for regime_name, stats in regime_stats.items():
                n = stats["count"]
                if n == 0:
                    continue

                win_rate = (stats["wins"] / n) * 100.0 if n > 0 else 0.0
                # Profit factor: sum(wins) / abs(sum(losses))
                wins_sum = sum(
                    t.get("pnl_usdt", 0.0)
                    for t in stats["trades"]
                    if t.get("pnl_usdt", 0.0) > 0
                )
                losses_sum = sum(
                    abs(t.get("pnl_usdt", 0.0))
                    for t in stats["trades"]
                    if t.get("pnl_usdt", 0.0) < 0
                )
                pf = wins_sum / losses_sum if losses_sum > 0 else (999.0 if wins_sum > 0 else 0.0)
                avg_r = (stats["total_r"] / n) if n > 0 else 0.0
                avg_duration_s = (stats["total_duration_s"] / n) if n > 0 else 0.0
                pct_of_total = (n / total_trades * 100.0) if total_trades > 0 else 0.0

                regimes.append({
                    "name": regime_name,
                    "trades": n,
                    "win_rate": win_rate,
                    "pf": pf,
                    "avg_r": avg_r,
                    "avg_duration_s": avg_duration_s,
                    "pct_of_total": pct_of_total,
                })

            logger.info("Performance by regime: %d regimes, %d total trades", len(regimes), total_trades)
            return {"status": "ok", "regimes": regimes}

        except Exception as e:
            logger.error("Error in get_performance_by_regime: %s", e, exc_info=True)
            return {"status": "error", "detail": str(e)}

    async def _cmd_get_regime_transitions(self, params: dict) -> dict:
        """Return regime transition matrix."""
        if not self._pe:
            return {"status": "error", "detail": "PaperExecutor not initialized"}

        try:
            closed_trades = getattr(self._pe, '_closed_trades', [])
            # Sort by close time to track transitions
            sorted_trades = sorted(
                closed_trades,
                key=lambda t: t.get("close_time", 0.0)
            )

            transitions_map = {}

            # Build transition pairs from consecutive trades
            for i in range(len(sorted_trades) - 1):
                current_regime = sorted_trades[i].get("regime", "uncertain")
                next_regime = sorted_trades[i + 1].get("regime", "uncertain")
                key = f"{current_regime}->{next_regime}"

                if key not in transitions_map:
                    transitions_map[key] = {
                        "from": current_regime,
                        "to": next_regime,
                        "count": 0,
                        "pnls": [],
                    }

                transitions_map[key]["count"] += 1
                # Collect PnL during the transition (next trade's PnL)
                next_pnl = sorted_trades[i + 1].get("pnl_usdt", 0.0)
                transitions_map[key]["pnls"].append(next_pnl)

            # Build response with average PnL
            transitions = []
            for trans in transitions_map.values():
                avg_pnl = (
                    sum(trans["pnls"]) / len(trans["pnls"])
                    if trans["pnls"]
                    else 0.0
                )
                transitions.append({
                    "from": trans["from"],
                    "to": trans["to"],
                    "count": trans["count"],
                    "avg_pnl_during_transition": avg_pnl,
                })

            logger.info("Regime transitions: %d unique transitions", len(transitions))
            return {"status": "ok", "transitions": transitions}

        except Exception as e:
            logger.error("Error in get_regime_transitions: %s", e, exc_info=True)
            return {"status": "error", "detail": str(e)}

    async def _cmd_get_drawdown_curve(self, params: dict) -> dict:
        """Return drawdown time-series from equity curve."""
        if not self._pe:
            return {"status": "error", "detail": "PaperExecutor not initialized"}

        try:
            closed_trades = getattr(self._pe, '_closed_trades', [])
            sorted_trades = sorted(
                closed_trades,
                key=lambda t: t.get("close_time", 0.0)
            )

            # Build cumulative capital curve
            initial_capital = getattr(self._pe, '_initial_capital_usdt', 0.0) or (
                self._pe.get_production_status().get("initial_capital_usdt", 0.0)
                if hasattr(self._pe, 'get_production_status') else 0.0
            )
            capital = initial_capital
            peak_capital = initial_capital
            drawdown_points = []

            for trade in sorted_trades:
                capital += trade.get("pnl_usdt", 0.0)
                peak_capital = max(peak_capital, capital)
                drawdown_pct = (
                    ((peak_capital - capital) / peak_capital * 100.0)
                    if peak_capital > 0
                    else 0.0
                )
                drawdown_points.append({
                    "time": trade.get("close_time", 0.0),
                    "drawdown_pct": drawdown_pct,
                    "peak_capital": peak_capital,
                })

            logger.info("Drawdown curve: %d points", len(drawdown_points))
            return {"status": "ok", "points": drawdown_points}

        except Exception as e:
            logger.error("Error in get_drawdown_curve: %s", e, exc_info=True)
            return {"status": "error", "detail": str(e)}

    async def _cmd_get_rolling_metrics(self, params: dict) -> dict:
        """Return rolling WR/PF over a configurable window."""
        if not self._pe:
            return {"status": "error", "detail": "PaperExecutor not initialized"}

        try:
            window = params.get("window", 20)
            closed_trades = getattr(self._pe, '_closed_trades', [])
            sorted_trades = sorted(
                closed_trades,
                key=lambda t: t.get("close_time", 0.0)
            )

            rolling_points = []

            # Calculate rolling metrics for each trade (window of last N trades)
            for i in range(len(sorted_trades)):
                start_idx = max(0, i - window + 1)
                window_trades = sorted_trades[start_idx:i + 1]

                if len(window_trades) == 0:
                    continue

                # Win rate
                wins = sum(1 for t in window_trades if t.get("pnl_usdt", 0.0) > 0)
                win_rate = (wins / len(window_trades)) * 100.0

                # Profit factor
                wins_sum = sum(
                    t.get("pnl_usdt", 0.0)
                    for t in window_trades
                    if t.get("pnl_usdt", 0.0) > 0
                )
                losses_sum = sum(
                    abs(t.get("pnl_usdt", 0.0))
                    for t in window_trades
                    if t.get("pnl_usdt", 0.0) < 0
                )
                pf = wins_sum / losses_sum if losses_sum > 0 else (999.0 if wins_sum > 0 else 0.0)

                # Average R
                avg_r = sum(t.get("r_multiple", 0.0) for t in window_trades) / len(window_trades)

                rolling_points.append({
                    "time": window_trades[-1].get("close_time", 0.0),
                    "rolling_wr": win_rate,
                    "rolling_pf": pf,
                    "rolling_avg_r": avg_r,
                })

            logger.info("Rolling metrics: %d points with window=%d", len(rolling_points), window)
            return {"status": "ok", "points": rolling_points, "window": window}

        except Exception as e:
            logger.error("Error in get_rolling_metrics: %s", e, exc_info=True)
            return {"status": "error", "detail": str(e)}

    async def _cmd_get_r_distribution(self, params: dict) -> dict:
        """Return R-multiple distribution histogram."""
        if not self._pe:
            return {"status": "error", "detail": "PaperExecutor not initialized"}

        try:
            closed_trades = getattr(self._pe, '_closed_trades', [])
            r_values = [t.get("r_multiple", 0.0) for t in closed_trades]

            if not r_values:
                return {"status": "ok", "buckets": [], "expectancy": 0.0, "median_r": 0.0, "max_win_r": 0.0, "max_loss_r": 0.0}

            # Create buckets: -5 to +5 in 0.5R increments
            buckets_map = {}
            bucket_width = 0.5
            for r in r_values:
                bucket_key = int(r / bucket_width) * bucket_width
                if bucket_key not in buckets_map:
                    buckets_map[bucket_key] = 0
                buckets_map[bucket_key] += 1

            # Sort and convert to list
            buckets = []
            for bucket_key in sorted(buckets_map.keys()):
                r_min = bucket_key
                r_max = bucket_key + bucket_width
                buckets.append({
                    "r_min": round(r_min, 2),
                    "r_max": round(r_max, 2),
                    "count": buckets_map[bucket_key],
                })

            # Calculate stats
            expectancy = sum(r_values) / len(r_values) if r_values else 0.0
            sorted_r = sorted(r_values)
            median_r = sorted_r[len(sorted_r) // 2] if sorted_r else 0.0
            max_win_r = max(r_values) if r_values else 0.0
            max_loss_r = min(r_values) if r_values else 0.0

            logger.info("R distribution: %d buckets, expectancy=%.3fR", len(buckets), expectancy)
            return {
                "status": "ok",
                "buckets": buckets,
                "expectancy": expectancy,
                "median_r": median_r,
                "max_win_r": max_win_r,
                "max_loss_r": max_loss_r,
            }

        except Exception as e:
            logger.error("Error in get_r_distribution: %s", e, exc_info=True)
            return {"status": "error", "detail": str(e)}

    async def _cmd_get_duration_analysis(self, params: dict) -> dict:
        """Return trade duration vs outcome analysis."""
        if not self._pe:
            return {"status": "error", "detail": "PaperExecutor not initialized"}

        try:
            closed_trades = getattr(self._pe, '_closed_trades', [])

            if not closed_trades:
                return {"status": "ok", "buckets": []}

            # Create duration buckets: 0-5m, 5-15m, 15-1h, 1-4h, 4h+
            buckets = {
                (0, 300): {"label": "0-5m", "trades": [], "durations": []},
                (300, 900): {"label": "5-15m", "trades": [], "durations": []},
                (900, 3600): {"label": "15m-1h", "trades": [], "durations": []},
                (3600, 14400): {"label": "1-4h", "trades": [], "durations": []},
                (14400, float('inf')): {"label": "4h+", "trades": [], "durations": []},
            }

            for trade in closed_trades:
                duration_s = trade.get("duration_s", 0.0)
                pnl = trade.get("pnl_usdt", 0.0)

                for (min_s, max_s), bucket_data in buckets.items():
                    if min_s <= duration_s < max_s:
                        bucket_data["trades"].append(trade)
                        bucket_data["durations"].append(duration_s)
                        break

            # Calculate stats per bucket
            result_buckets = []
            for (min_s, max_s), bucket_data in sorted(buckets.items()):
                trades = bucket_data["trades"]
                n = len(trades)
                if n == 0:
                    continue

                wins = sum(1 for t in trades if t.get("pnl_usdt", 0.0) > 0)
                win_rate = (wins / n) * 100.0 if n > 0 else 0.0
                avg_r = sum(t.get("r_multiple", 0.0) for t in trades) / n if n > 0 else 0.0

                result_buckets.append({
                    "duration_min_s": min_s,
                    "duration_max_s": max_s if max_s != float('inf') else 86400,  # cap at 24h
                    "count": n,
                    "avg_r": avg_r,
                    "win_rate": win_rate,
                })

            logger.info("Duration analysis: %d buckets", len(result_buckets))
            return {"status": "ok", "buckets": result_buckets}

        except Exception as e:
            logger.error("Error in get_duration_analysis: %s", e, exc_info=True)
            return {"status": "error", "detail": str(e)}

    async def _cmd_get_current_regime(self, params: dict) -> dict:
        """Return current market regime classification."""
        try:
            # Try to get regime from orchestrator if available
            if self._orchestrator and hasattr(self._orchestrator, 'get_current_regime'):
                try:
                    regime_info = self._orchestrator.get_current_regime()
                    if regime_info:
                        return {
                            "status": "ok",
                            "regime": regime_info.get("name", "uncertain"),
                            "confidence": regime_info.get("confidence", 0.0),
                            "classifier": regime_info.get("classifier_type", "hmm"),
                            "hmm_fitted": regime_info.get("hmm_fitted", False),
                            "probabilities": regime_info.get("probabilities", {}),
                            "description": regime_info.get("description", ""),
                            "strategies": regime_info.get("suggested_strategies", []),
                            "risk_adjustment": regime_info.get("risk_adjustment", "standard"),
                            "source": "live",
                        }
                except Exception as e:
                    logger.debug("Could not fetch from orchestrator: %s", e)

            # Fallback: return sensible default regime state
            logger.info("No current regime available, returning fallback state")
            return {
                "status": "ok",
                "regime": "uncertain",
                "confidence": 0.0,
                "classifier": "ensemble",
                "hmm_fitted": False,
                "probabilities": {
                    "bull_trend": 0.0,
                    "bear_trend": 0.0,
                    "ranging": 0.0,
                    "vol_expansion": 0.0,
                    "uncertain": 1.0,
                },
                "description": "Insufficient data or regime classifier not ready",
                "strategies": [],
                "risk_adjustment": "conservative",
                "source": "fallback",
            }

        except Exception as e:
            logger.error("Error in get_current_regime: %s", e, exc_info=True)
            return {"status": "error", "detail": str(e)}

    async def _cmd_get_regime_history(self, params: dict) -> dict:
        """Return recent regime classification history."""
        try:
            # Try to get regime history from orchestrator if available
            if self._orchestrator and hasattr(self._orchestrator, 'get_regime_history'):
                try:
                    history = self._orchestrator.get_regime_history()
                    if history:
                        return {
                            "status": "ok",
                            "history": history,
                            "source": "live",
                        }
                except Exception as e:
                    logger.debug("Could not fetch history from orchestrator: %s", e)

            # Fallback: infer regime changes from closed trades
            closed_trades = getattr(self._pe, '_closed_trades', []) if self._pe else []
            sorted_trades = sorted(
                closed_trades,
                key=lambda t: t.get("close_time", 0.0)
            )

            history = []
            last_regime = None

            for trade in sorted_trades:
                regime = trade.get("regime", "uncertain")
                close_time = trade.get("close_time", 0.0)

                # Only add if regime changed
                if regime != last_regime:
                    history.append({
                        "timestamp": close_time,
                        "regime": regime,
                        "confidence": trade.get("regime_confidence", 0.5),
                        "classifier": "inferred_from_trades",
                    })
                    last_regime = regime

            logger.info("Regime history: %d changes detected from trades", len(history))
            return {
                "status": "ok",
                "history": history,
                "source": "inferred_from_trades" if history else "none",
            }

        except Exception as e:
            logger.error("Error in get_regime_history: %s", e, exc_info=True)
            return {"status": "error", "detail": str(e)}

    # ── Demo Monitor Handlers (Phase 8H) ────────────────────────

    async def _cmd_get_active_positions(self, params: dict) -> dict:
        """All open positions from the active exchange."""
        try:
            from datetime import datetime, timezone
            positions = await self._fetch_exchange_positions_cached()
            return {
                "status": "ok",
                "positions": positions,
                "count": len(positions),
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "data_source": "exchange",
            }
        except Exception as e:
            logger.error(f"get_active_positions failed: {e}")
            return {"status": "error", "detail": str(e)}

    async def _cmd_get_portfolio_state(self, params: dict) -> dict:
        """Portfolio state with heat and margin."""
        try:
            from datetime import datetime, timezone
            portfolio = {}
            if self._pe:
                status = self._pe.get_production_status() if hasattr(self._pe, 'get_production_status') else {}
                stats = self._pe.get_stats() if hasattr(self._pe, 'get_stats') else {}
                pe_capital = status.get("capital_usdt", 0) or status.get("available_capital", 0)
                # Use exchange balance when available, fall back to PaperExecutor capital
                exchange_bal = await self._fetch_exchange_balance_cached()
                capital = exchange_bal if exchange_bal > 0 else pe_capital
                peak = status.get("peak_capital_usdt", capital)

                # Calculate portfolio heat
                open_positions = self._pe.get_open_positions() if hasattr(self._pe, 'get_open_positions') else []
                total_exposure = sum(
                    (p.get("size_usdt", 0) if isinstance(p, dict) else getattr(p, 'size_usdt', 0))
                    for p in open_positions
                )
                heat_pct = (total_exposure / capital * 100) if capital > 0 else 0.0

                portfolio = {
                    "equity": capital,
                    "balance": capital,
                    "used_margin": total_exposure,
                    "free_margin": max(0, capital - total_exposure),
                    "portfolio_heat_pct": round(heat_pct, 2),
                    "max_heat_limit": 6.0,
                    "drawdown_pct": status.get("drawdown_pct", 0),
                    "total_return_pct": status.get("total_return_pct", 0),
                    "open_positions": len(open_positions),
                    "total_trades": stats.get("total_trades", 0) or status.get("total_trades", 0),
                    "win_rate": stats.get("win_rate", 0) or status.get("win_rate", 0),
                    "profit_factor": stats.get("profit_factor", 0),
                    "trading_paused": status.get("circuit_breaker_on", False) or getattr(self._pe, '_trading_paused', False),
                }
            return {
                "status": "ok",
                "portfolio": portfolio,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "data_source": type(self._pe).__name__ if self._pe else "none"
            }
        except Exception as e:
            logger.error(f"get_portfolio_state failed: {e}")
            return {"status": "error", "detail": str(e)}

    async def _cmd_get_live_pnl(self, params: dict) -> dict:
        """Real-time PnL breakdown from the active exchange."""
        try:
            from datetime import datetime, timezone
            total_unrealized = 0.0
            total_realized = 0.0
            fees_paid = 0.0

            # Unrealized from exchange open positions
            positions = await self._fetch_exchange_positions_cached()
            for p in positions:
                total_unrealized += p.get("pnl_unrealized", 0) or 0

            # Realized from exchange closed trades
            closed = await self._fetch_exchange_trades_cached()
            for t in closed:
                total_realized += t.get("pnl_usdt", 0) or 0
                fees_paid += t.get("fees", 0) or 0

            return {
                "status": "ok",
                "pnl": {
                    "total_unrealized": round(total_unrealized, 2),
                    "total_realized": round(total_realized, 2),
                    "daily_pnl": round(total_realized, 2),
                    "fees_paid": round(fees_paid, 4),
                    "net_pnl": round(total_realized + total_unrealized - fees_paid, 2),
                },
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "data_source": "exchange",
            }
        except Exception as e:
            logger.error(f"get_live_pnl failed: {e}")
            return {"status": "error", "detail": str(e)}

    async def _cmd_get_risk_state(self, params: dict) -> dict:
        """Risk state including crash defense and circuit breakers."""
        try:
            from datetime import datetime, timezone
            risk = {
                "drawdown_pct": 0.0,
                "daily_loss_pct": 0.0,
                "circuit_breaker_triggered": False,
                "trading_enabled": True,
                "crash_defense_tier": "NORMAL",
                "is_defensive": False,
                "is_safe_mode": False,
                "reason": "",
            }

            if self._pe:
                status = self._pe.get_production_status() if hasattr(self._pe, 'get_production_status') else {}
                capital = status.get("capital_usdt", 0) or status.get("available_capital", 0)
                peak = status.get("peak_capital_usdt", capital)

                risk["drawdown_pct"] = status.get("drawdown_pct", 0)
                risk["circuit_breaker_triggered"] = status.get("circuit_breaker_on", False)
                risk["trading_enabled"] = not status.get("circuit_breaker_on", False) and not getattr(self._pe, '_trading_paused', False)

                if not risk["trading_enabled"]:
                    if status.get("circuit_breaker_on", False):
                        risk["reason"] = "Circuit breaker triggered"
                    elif getattr(self._pe, '_trading_paused', False):
                        risk["reason"] = "Trading manually paused"

                # Session/daily loss
                session_pnl = status.get("session_pnl_usdt", 0)
                if capital > 0 and session_pnl < 0:
                    risk["daily_loss_pct"] = round(abs(session_pnl) / capital * 100, 2)

            # Crash defense from orchestrator
            if self._orch:
                try:
                    cdc = getattr(self._orch, '_crash_defense', None) or getattr(self._orch, 'crash_defense_controller', None)
                    if cdc:
                        risk["crash_defense_tier"] = getattr(cdc, 'current_tier', 'NORMAL') or 'NORMAL'
                        risk["is_defensive"] = risk["crash_defense_tier"] not in ("NORMAL", "normal")
                        risk["is_safe_mode"] = risk["crash_defense_tier"] in ("EMERGENCY", "SYSTEMIC")
                except Exception:
                    pass

            return {
                "status": "ok",
                "risk": risk,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "data_source": type(self._pe).__name__ if self._pe else "none"
            }
        except Exception as e:
            logger.error(f"get_risk_state failed: {e}")
            return {"status": "error", "detail": str(e)}

    async def _cmd_get_recent_trades_monitor(self, params: dict) -> dict:
        """Last 50 closed trades from the active exchange."""
        try:
            from datetime import datetime, timezone

            # Fetch from exchange (cached 30s)
            closed = await self._fetch_exchange_trades_cached()
            recent = closed[:50]  # already sorted newest first

            trades = []
            for t in recent:
                entry = t.get("entry_price", 0) or 0
                exit_p = t.get("exit_price", 0) or 0
                pnl = t.get("pnl_usdt", 0) or 0
                size = t.get("size_usdt", 0) or 0
                fees = t.get("fees", 0) or 0

                trades.append({
                    "symbol": t.get("symbol", ""),
                    "side": t.get("side", ""),
                    "entry_price": entry,
                    "exit_price": exit_p,
                    "pnl_usdt": round(pnl, 2),
                    "pnl_pct": round(pnl / size * 100, 2) if size else 0,
                    "r_multiple": 0,
                    "duration_s": 0,
                    "regime": "",
                    "exit_reason": t.get("exit_reason", ""),
                    "models_fired": [],
                    "fees_estimated": round(fees, 4),
                    "slippage": 0,
                    "closed_at": t.get("closed_at", ""),
                    "score": 0,
                })

            return {
                "status": "ok",
                "trades": trades,
                "count": len(trades),
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "data_source": "exchange",
            }
        except Exception as e:
            logger.error(f"get_recent_trades_monitor failed: {e}")
            return {"status": "error", "detail": str(e)}

    # ── Exchange Management Handlers (Phase 8B) ──────────────

    async def _cmd_exchange_test_connection(self, params: dict) -> dict:
        """Test connection is handled directly by the API layer (CCXT).
        This stub exists so the engine doesn't return 'Unknown action'
        if called via the generic engine command route."""
        return {
            "status": "error",
            "detail": "test_connection is handled by the API layer, not the engine. "
                      "Use POST /exchanges/test-connection directly.",
        }

    async def _cmd_exchange_load_active(self, params: dict) -> dict:
        """Reload exchange connection after activation.

        Syncs the DB from web_assets.json (mode flags), then rebuilds
        the CCXT instance so the engine connects to the correct endpoint.
        Also invalidates cached balance/trades/positions.
        """
        try:
            if not self._exchange_manager:
                return {"status": "ok", "detail": "No exchange manager"}

            logger.info("Reloading exchange connection (exchange_id=%s)", params.get("exchange_id"))

            # 1. Sync DB mode flags from web_assets.json
            self._sync_exchange_db_from_assets()

            # 2. Rebuild CCXT instance with updated config
            ok = self._exchange_manager.load_active_exchange()
            if not ok:
                return {"status": "error", "detail": "load_active_exchange failed — check logs"}

            # 3. Invalidate caches so next dashboard poll fetches fresh data
            self._cached_balance_ts = 0.0
            self._cached_exchange_trades_ts = 0.0
            self._cached_exchange_positions_ts = 0.0

            logger.info("Exchange reloaded successfully")
            return {"status": "ok", "detail": "Exchange reloaded"}
        except Exception as e:
            logger.error("exchange.load_active failed: %s", e)
            return {"status": "error", "detail": str(e)}

    async def _cmd_exchange_sync_assets(self, params: dict) -> dict:
        """Sync assets from the active exchange. Fetches markets via CCXT."""
        try:
            if not self._exchange_manager:
                return {"status": "error", "detail": "ExchangeManager not initialized"}

            exchange = self._exchange_manager.get_exchange()
            if not exchange:
                return {"status": "error", "detail": "No active exchange connection"}

            import asyncio
            loop = asyncio.get_event_loop()
            markets = await loop.run_in_executor(None, exchange.load_markets)

            # Return market data for the API to upsert into DB
            assets = []
            for symbol, market in markets.items():
                if market.get("active") and market.get("quote") in ("USDT", "BTC", "ETH", "BNB"):
                    assets.append({
                        "symbol": symbol,
                        "base_currency": market.get("base", ""),
                        "quote_currency": market.get("quote", ""),
                        "price_precision": market.get("precision", {}).get("price"),
                        "amount_precision": market.get("precision", {}).get("amount"),
                        "min_amount": market.get("limits", {}).get("amount", {}).get("min"),
                        "min_cost": market.get("limits", {}).get("cost", {}).get("min"),
                    })

            return {"status": "ok", "assets": assets, "count": len(assets), "new_count": len(assets)}
        except Exception as e:
            logger.error("exchange.sync_assets failed: %s", e)
            return {"status": "error", "detail": str(e)}

    async def _cmd_exchange_disconnect(self, params: dict) -> dict:
        """Disconnect the active exchange."""
        return {"status": "ok", "detail": "Exchange disconnect acknowledged"}

    async def _cmd_exchange_status(self, params: dict) -> dict:
        """Get current exchange connection status."""
        try:
            connected = self._exchange_manager is not None
            return {
                "status": "ok",
                "connected": connected,
                "exchange_manager_initialized": connected,
            }
        except Exception as e:
            return {"status": "error", "detail": str(e)}

    async def _cmd_exchange_fetch_balance(self, params: dict) -> dict:
        """Fetch balance from the active exchange."""
        try:
            if not self._exchange_manager:
                return {"status": "error", "detail": "ExchangeManager not initialized"}

            exchange = self._exchange_manager.get_exchange()
            if not exchange:
                return {"status": "error", "detail": "No active exchange connection"}

            import asyncio
            loop = asyncio.get_event_loop()
            balance = await loop.run_in_executor(None, exchange.fetch_balance)

            usdt_free = 0.0
            if "USDT" in balance:
                usdt_free = float(balance["USDT"].get("free", 0) or 0)
            elif "free" in balance and "USDT" in balance["free"]:
                usdt_free = float(balance["free"]["USDT"] or 0)

            return {
                "status": "ok",
                "balance_usdt": round(usdt_free, 2),
            }
        except Exception as e:
            logger.error("exchange.fetch_balance failed: %s", e)
            return {"status": "error", "detail": str(e)}

    # ── State Management ────────────────────────────────────

    async def _update_state(self, state: str, **extra):
        """Update engine state in Redis hash."""
        if self._redis_bridge:
            data = {"state": state, "updated_at": time.time(), **extra}
            self._redis_bridge.set_state("nexus:engine:state", data)


# ── Entry Point ─────────────────────────────────────────────

async def main():
    engine = TradingEngineService()

    loop = asyncio.get_event_loop()
    # add_signal_handler is Unix-only; skip on Windows
    if hasattr(loop, "add_signal_handler") and os.name != "nt":
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, lambda: asyncio.create_task(engine.stop()))

    await engine.start()

    # Keep running until stopped
    try:
        while engine._running:
            await asyncio.sleep(1)
    except (asyncio.CancelledError, KeyboardInterrupt):
        pass
    finally:
        await engine.stop()


if __name__ == "__main__":
    asyncio.run(main())
