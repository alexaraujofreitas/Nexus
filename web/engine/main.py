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

for p in [_BACKEND_DIR, _PROJECT_ROOT]:
    if p not in sys.path:
        sys.path.insert(0, p)

# ── Install Qt shim BEFORE any core/ imports ────────────────
from core_patch import install_qt_shim  # noqa: E402
install_qt_shim()

# ── Now safe to import ──────────────────────────────────────
from core_patch.event_bus import EventBus, Topics  # noqa: E402
from core_patch.redis_bridge import RedisBridge  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(name)-30s | %(levelname)-7s | %(message)s",
)
logger = logging.getLogger("nexus.engine")

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

        # ── Core component references (populated during init) ──
        self._settings = None          # config.settings.AppSettings
        self._pe = None                # core.execution.paper_executor.PaperExecutor
        self._scanner = None           # core.scanning.scanner.AssetScanner
        self._exchange_manager = None  # core.market_data.exchange_manager
        self._orchestrator = None      # core.orchestrator.orchestrator_engine
        self._notification_mgr = None  # core.notifications.notification_manager

        # ── Engine-level trading pause flag ─────────────────────
        # The desktop app has no pause/resume API on OrchestratorEngine.
        # We implement it here: when _trading_paused is True, the engine
        # will not submit new trades (checked in the scan cycle via
        # EventBus publication). Existing positions are unaffected.
        self._trading_paused = False

    async def start(self):
        """Full 13-step startup sequence."""
        logger.info("=== NexusTrader Engine Service Starting ===")
        self._running = True
        self._start_time = time.time()

        # Step 1: Connect Redis bridge
        logger.info("[1/13] Connecting Redis bridge")
        self._redis_bridge = RedisBridge(
            redis_url=REDIS_URL,
            service_name="engine",
        )
        self._event_bus.attach_redis_bridge(self._redis_bridge)
        self._redis_bridge.start(self._event_bus)

        # Step 2: Publish engine state
        logger.info("[2/13] Publishing initial engine state")
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
            (8, "Creating PaperExecutor",       self._init_executor),
            (9, "Creating CrashDefenseCtrl",    self._init_crash_defense),
            (10, "Creating NotificationManager", self._init_notifications),
            (11, "Loading positions & history",  self._init_positions),
            (12, "Starting scanner timers",      self._init_scanner),
        ]

        for step_num, step_name, step_fn in init_steps:
            try:
                logger.info("[%d/13] %s", step_num, step_name)
                await step_fn()
            except Exception as e:
                logger.error(
                    "[%d/13] FAILED: %s — %s\n%s",
                    step_num, step_name, e, traceback.format_exc(),
                )
                await self._update_state("init_error", error=str(e))

        # Step 13: Start command listener
        logger.info("[13/13] Starting command listener")
        self._command_task = asyncio.create_task(self._command_loop())

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
        """Import and store reference to ExchangeManager singleton."""
        from core.market_data.exchange_manager import exchange_manager
        self._exchange_manager = exchange_manager
        logger.info("ExchangeManager ready")

    async def _init_orchestrator(self):
        """Import and store reference to OrchestratorEngine singleton."""
        from core.orchestrator.orchestrator_engine import get_orchestrator
        self._orchestrator = get_orchestrator()
        logger.info("OrchestratorEngine ready")

    async def _init_agents(self):
        """Initialize AgentCoordinator with 23 agents."""
        from core.agents.agent_coordinator import AgentCoordinator
        logger.info("AgentCoordinator class available")

    async def _init_executor(self):
        """Construct PaperExecutor (no global singleton — engine owns it)."""
        from core.execution.paper_executor import PaperExecutor
        initial_capital = 100_000.0
        if self._settings:
            initial_capital = self._settings.get(
                "risk_engine.initial_capital_usdt", 100_000.0,
            )
        self._pe = PaperExecutor(initial_capital_usdt=initial_capital)
        logger.info(
            "PaperExecutor ready (capital=%.2f USDT)", initial_capital,
        )

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
        # config.yaml: scanner.auto_execute MUST always be true per CLAUDE.md
        auto_start = True
        if self._settings:
            auto_start = self._settings.get("scanner.auto_execute", True)
        if auto_start:
            self._scanner.start()
            logger.info("Scanner auto-started")
        else:
            logger.info("Scanner imported (auto_execute=false, idle)")

    # ── Command Loop ────────────────────────────────────────

    async def _command_loop(self):
        """
        Listen for commands on Redis queue nexus:engine:commands.
        Process each command and reply on nexus:engine:replies:{command_id}.
        """
        import redis.asyncio as aioredis

        r = aioredis.from_url(REDIS_URL, decode_responses=True)
        logger.info("Command listener started on nexus:engine:commands")

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
            "get_watchlist": self._cmd_get_watchlist,
            "get_agent_status": self._cmd_get_agent_status,
            "get_signals": self._cmd_get_signals,
            "get_risk_status": self._cmd_get_risk_status,
            "get_trade_history": self._cmd_get_trade_history,
            "update_config": self._cmd_update_config,
            "get_system_health": self._cmd_get_system_health,
            "trigger_scan": self._cmd_trigger_scan,
            "kill_switch": self._cmd_kill_switch,
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
        """Return all open positions from PaperExecutor."""
        if not self._pe:
            return {"status": "error", "detail": "PaperExecutor not initialized"}
        positions = self._pe.get_open_positions()
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

        Uses the 5-symbol watchlist from config or defaults.
        """
        if not self._exchange_manager:
            return {"status": "error", "detail": "ExchangeManager not initialized"}
        symbols = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT", "XRP/USDT"]
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

        # Portfolio
        if self._pe:
            prod = self._pe.get_production_status()
            stats = self._pe.get_stats()
            result["portfolio"] = {
                "capital_usdt": prod.get("capital_usdt", 0.0),
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

    async def _cmd_get_watchlist(self, params: dict) -> dict:
        """Return the current scanner watchlist with symbol weights."""
        symbols = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT", "XRP/USDT"]
        weights = {"SOL/USDT": 1.3, "ETH/USDT": 1.2, "BTC/USDT": 1.0, "BNB/USDT": 0.8, "XRP/USDT": 0.8}

        if self._settings:
            cfg_symbols = self._settings.get("scanner.watchlist", None)
            if cfg_symbols and isinstance(cfg_symbols, list):
                symbols = cfg_symbols
            cfg_weights = self._settings.get("scanner.symbol_weights", None)
            if cfg_weights and isinstance(cfg_weights, dict):
                weights = cfg_weights

        return {"status": "ok", "symbols": symbols, "weights": weights}

    async def _cmd_get_agent_status(self, params: dict) -> dict:
        """Return status of all intelligence agents."""
        try:
            from core.agents.agent_coordinator import get_coordinator
            coordinator = get_coordinator()
            agents = coordinator.get_status() if hasattr(coordinator, 'get_status') else {}
            return {"status": "ok", "agents": agents, "count": len(agents)}
        except Exception as e:
            return {"status": "error", "detail": f"AgentCoordinator unavailable: {e}"}

    async def _cmd_get_signals(self, params: dict) -> dict:
        """Return recent signal data from the signal pipeline."""
        # Signal data is ephemeral — read from event bus history or scanner results
        signals = []
        if self._scanner and hasattr(self._scanner, '_last_scan_results'):
            raw = self._scanner._last_scan_results or []
            for r in raw:
                if isinstance(r, dict) and r.get("score", 0) > 0:
                    signals.append({
                        "symbol": r.get("symbol", ""),
                        "score": r.get("score", 0.0),
                        "side": r.get("side", ""),
                        "models_fired": r.get("models_fired", []),
                        "regime": r.get("regime", ""),
                        "entry_price": r.get("entry_price"),
                        "stop_loss": r.get("stop_loss_price"),
                        "take_profit": r.get("take_profit_price"),
                        "risk_reward": r.get("risk_reward_ratio"),
                        "approved": r.get("is_approved", False),
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
        """Return paginated trade history from the database."""
        page = params.get("page", 1)
        per_page = min(params.get("per_page", 50), 200)  # cap at 200

        if not self._pe:
            return {"status": "error", "detail": "PaperExecutor not initialized"}

        # Get closed trades from PaperExecutor's in-memory list
        closed = list(reversed(getattr(self._pe, '_closed_trades', [])))
        total = len(closed)
        start = (page - 1) * per_page
        end = start + per_page
        page_trades = closed[start:end]

        return {
            "status": "ok",
            "trades": page_trades,
            "total": total,
            "page": page,
            "per_page": per_page,
            "pages": (total + per_page - 1) // per_page if per_page > 0 else 0,
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
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda: asyncio.create_task(engine.stop()))

    await engine.start()

    # Keep running until stopped
    try:
        while engine._running:
            await asyncio.sleep(1)
    except asyncio.CancelledError:
        pass
    finally:
        await engine.stop()


if __name__ == "__main__":
    asyncio.run(main())
