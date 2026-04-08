# ============================================================
# NEXUS TRADER — Headless Engine  (Phase 2 — Intraday Redesign)
#
# Pure-Python core that can run WITHOUT PySide6/Qt.
#
# Lifecycle:
#   engine = NexusEngine()
#   engine.start()       # DB, exchange, agents, crash defense, notifications
#   ...
#   engine.stop()        # graceful teardown
#
# main.py becomes:
#   engine = NexusEngine()
#   engine.start()
#   if not headless:
#       QtBridge.attach(bus)
#       start_qt_gui(engine)
# ============================================================
from __future__ import annotations

import logging
import threading
from typing import Optional

logger = logging.getLogger(__name__)


class IntradayEngineStartupError(RuntimeError):
    """Raised when the intraday strategy engine fails to start.

    This is a FATAL error for a trading system — the engine MUST NOT
    continue in a misleadingly-healthy state without its strategy pipeline.
    """
    pass


class NexusEngine:
    """
    Headless core engine for NexusTrader.

    Initializes all non-GUI subsystems:
      - Database (SQLite + schema migration)
      - ExchangeManager (Bybit REST + optional WS)
      - AgentCoordinator (4 retained agents)
      - OrchestratorEngine (advisory meta-signal)
      - CrashDefenseController
      - NotificationManager
      - Thread baseline measurement

    Can run entirely without PySide6.
    """

    def __init__(self):
        self._running = False
        self._coordinator = None
        self._orchestrator = None
        self._crash_defense = None
        self._strategy_bus = None
        self._baseline_thread_count: Optional[int] = None

    def start(self) -> None:
        """
        Initialize all subsystems and start agents.

        Safe to call multiple times — guards against double-start.
        """
        if self._running:
            logger.warning("NexusEngine: already running — ignoring start()")
            return

        logger.info("=" * 60)
        logger.info("  NEXUS TRADER — Headless Engine Starting")
        logger.info("=" * 60)

        # ── Measure thread baseline BEFORE anything starts ────────
        self._baseline_thread_count = threading.active_count()
        logger.info(
            "STARTUP: baseline thread count = %d (before agents)",
            self._baseline_thread_count,
        )

        # ── Initialize Database ───────────────────────────────────
        try:
            from core.database.engine import init_database
            init_database()
            logger.info("Database ready")
        except Exception as e:
            logger.critical("Database initialization failed: %s", e, exc_info=True)
            raise

        # ── Initialize Orchestrator (subscribes to agent events) ──
        try:
            from core.orchestrator.orchestrator_engine import get_orchestrator
            self._orchestrator = get_orchestrator()
            logger.info("OrchestratorEngine ready")
        except Exception as e:
            logger.warning("OrchestratorEngine init failed (non-fatal): %s", e)

        # ── Initialize AgentCoordinator ───────────────────────────
        try:
            from core.agents.agent_coordinator import get_coordinator
            self._coordinator = get_coordinator()
            logger.info("AgentCoordinator ready")
        except Exception as e:
            logger.warning("AgentCoordinator init failed (non-fatal): %s", e)

        # ── Initialize CrashDefenseController ─────────────────────
        try:
            from core.risk.crash_defense_controller import get_crash_defense_controller
            self._crash_defense = get_crash_defense_controller()
            logger.info("CrashDefenseController initialized")
        except Exception as e:
            logger.debug("CrashDefenseController init failed (non-fatal): %s", e)

        # ── Initialize Notification System ────────────────────────
        try:
            self._init_notifications()
        except Exception as e:
            logger.warning("Notification system init failed (non-fatal): %s", e)

        # ── Auto-start agents ─────────────────────────────────────
        try:
            from config.settings import settings as _app_settings
            if _app_settings.get("agents", {}).get("auto_start", True):
                if self._coordinator:
                    self._coordinator.start_all()
                    logger.info("AgentCoordinator: agents auto-started at launch")
        except Exception as e:
            logger.warning("Agent auto-start failed: %s", e)

        # ── Start Intraday Strategy Engine (Phase 4) ─────────────
        # FAIL-FAST: This is a trading-critical subsystem.
        # If it fails, the engine MUST NOT continue in a misleading
        # healthy state — it raises and aborts startup entirely.
        try:
            from core.intraday.engine_integration import start_intraday_engine
            self._strategy_bus = start_intraday_engine()
            if self._strategy_bus is None:
                raise IntradayEngineStartupError(
                    "start_intraday_engine() returned None — "
                    "StrategyBus failed to initialize"
                )
            logger.info(
                "Intraday StrategyBus started: %d strategies loaded",
                len(self._strategy_bus._strategies),
            )
        except IntradayEngineStartupError:
            logger.critical(
                "FATAL: Intraday strategy engine failed to start. "
                "NexusEngine startup ABORTED — trading cannot proceed "
                "without an active strategy pipeline."
            )
            # Clean up anything already started before re-raising
            self._teardown_on_failed_start()
            raise
        except Exception as e:
            logger.critical(
                "FATAL: Intraday engine startup exception: %s", e,
                exc_info=True,
            )
            self._teardown_on_failed_start()
            raise IntradayEngineStartupError(
                f"Intraday engine startup failed: {e}"
            ) from e

        self._running = True

        # ── Log final thread count ────────────────────────────────
        final_count = threading.active_count()
        logger.info(
            "STARTUP: final thread count = %d (delta +%d from baseline %d)",
            final_count,
            final_count - self._baseline_thread_count,
            self._baseline_thread_count,
        )

        logger.info("NexusEngine started successfully")

    def stop(self) -> None:
        """Gracefully stop all subsystems."""
        if not self._running:
            return

        logger.info("NexusEngine: shutting down...")

        # Stop intraday strategy engine (Phase 4)
        try:
            from core.intraday.engine_integration import stop_intraday_engine
            stop_intraday_engine()
            self._strategy_bus = None
            logger.info("Intraday StrategyBus stopped")
        except Exception as e:
            logger.warning("Intraday engine stop failed: %s", e)

        # Stop agents
        if self._coordinator:
            try:
                self._coordinator.stop_all()
            except Exception as e:
                logger.warning("AgentCoordinator stop failed: %s", e)

        # Stop notifications
        try:
            from core.notifications.notification_manager import notification_manager
            notification_manager.stop()
        except Exception:
            pass

        self._running = False
        logger.info("NexusEngine stopped")

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def baseline_thread_count(self) -> Optional[int]:
        return self._baseline_thread_count

    @property
    def strategy_bus(self):
        """Phase 4: runtime StrategyBus instance (None if not started)."""
        return self._strategy_bus

    # ── Private helpers ──────────────────────────────────────────

    def _teardown_on_failed_start(self) -> None:
        """Clean up subsystems that were already started when startup fails.

        Called when the intraday engine (or any future critical subsystem)
        fails during start(), ensuring no orphaned threads/subscriptions.
        """
        logger.info("NexusEngine: tearing down after failed startup...")

        # Stop agents if they were started
        if self._coordinator:
            try:
                self._coordinator.stop_all()
            except Exception:
                pass

        # Stop notifications if started
        try:
            from core.notifications.notification_manager import notification_manager
            notification_manager.stop()
        except Exception:
            pass

        # Stop intraday engine if partially started
        try:
            from core.intraday.engine_integration import stop_intraday_engine
            stop_intraday_engine()
        except Exception:
            pass

        self._strategy_bus = None
        self._running = False

    def _init_notifications(self) -> None:
        """Initialize the notification system with secrets from vault."""
        from core.notifications.notification_manager import notification_manager
        from config.settings import settings
        from core.security.key_vault import key_vault

        import copy
        notif_cfg: dict = copy.deepcopy(settings.get_section("notifications"))

        # Inject secrets from vault into channel configs
        twilio_sid   = key_vault.load("notifications.twilio_sid")
        twilio_token = key_vault.load("notifications.twilio_token")
        tg_token     = key_vault.load("notifications.telegram_token")
        email_pass   = key_vault.load("notifications.email_password")

        if twilio_sid and twilio_token:
            wa_cfg = notif_cfg.get("whatsapp", {})
            wa_cfg.update({"account_sid": twilio_sid, "auth_token": twilio_token})
            notif_cfg["whatsapp"] = wa_cfg

            sms_cfg = notif_cfg.get("sms", {})
            sms_cfg.update({"account_sid": twilio_sid, "auth_token": twilio_token})
            notif_cfg["sms"] = sms_cfg

        if tg_token:
            tg_cfg = notif_cfg.get("telegram", {})
            tg_cfg.update({"bot_token": tg_token})
            notif_cfg["telegram"] = tg_cfg

        if email_pass:
            em_cfg = notif_cfg.get("email", {})
            em_cfg.update({"password": email_pass})
            notif_cfg["email"] = em_cfg

        gemini_pass = key_vault.load("notifications.gemini_password")
        if gemini_pass:
            gm_cfg = notif_cfg.get("gemini", {})
            gm_cfg.update({"password": gemini_pass})
            notif_cfg["gemini"] = gm_cfg

        notification_manager.configure(notif_cfg)
        notification_manager.start()
        logger.info(
            "NotificationManager ready (%d channels)",
            notification_manager.get_channel_count(),
        )
