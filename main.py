#!/usr/bin/env python3
# ============================================================
# NEXUS TRADER — Application Entry Point
# Institutional-Grade AI Trading Platform v1.0
# ============================================================

import sys
import logging
from pathlib import Path

# Ensure project root is on sys.path
ROOT = Path(__file__).parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from PySide6.QtWidgets import QApplication, QSplashScreen, QLabel
from PySide6.QtCore import Qt, QTimer, QThread
from PySide6.QtGui import QFont, QColor, QPalette

# Setup logging FIRST — before any other imports
from gui.pages.logs.logs_page import setup_logging
setup_logging()
logger = logging.getLogger(__name__)


class _StartupConnectThread(QThread):
    """
    Runs once at startup: connects the active exchange in the background
    so the status bar and live feed update automatically without blocking
    the main window from appearing.
    """
    def run(self):
        try:
            from core.market_data.exchange_manager import exchange_manager
            exchange_manager.load_active_exchange()
            logger.info("Startup auto-connect completed")
        except Exception as e:
            logger.warning("Startup auto-connect failed: %s", e)

from core.database.engine import init_database
from gui.theme.theme_manager import ThemeManager
from core.agents.agent_coordinator import get_coordinator
from core.orchestrator.orchestrator_engine import get_orchestrator
from core.notifications.notification_manager import notification_manager


def create_splash(app: QApplication) -> QSplashScreen:
    """Create a simple startup splash screen."""
    from PySide6.QtGui import QPixmap, QPainter, QBrush
    from PySide6.QtCore import QRect

    pixmap = QPixmap(600, 300)
    pixmap.fill(QColor("#0A0E1A"))

    painter = QPainter(pixmap)
    painter.setPen(QColor("#FF6B00"))

    font_big = QFont("Segoe UI", 36, QFont.Bold)
    painter.setFont(font_big)
    painter.drawText(QRect(0, 80, 600, 80), Qt.AlignCenter, "NEXUS TRADER")

    painter.setPen(QColor("#4A5568"))
    font_small = QFont("Segoe UI", 11)
    painter.setFont(font_small)
    painter.drawText(QRect(0, 155, 600, 40), Qt.AlignCenter, "INSTITUTIONAL  ·  ALGORITHMIC  ·  AI-DRIVEN")

    painter.setPen(QColor("#1E2D40"))
    painter.drawLine(80, 220, 520, 220)

    painter.setPen(QColor("#8899AA"))
    font_ver = QFont("Segoe UI", 9)
    painter.setFont(font_ver)
    painter.drawText(QRect(0, 240, 600, 30), Qt.AlignCenter, "Initializing platform...")
    painter.end()

    splash = QSplashScreen(pixmap)
    splash.setWindowFlag(Qt.WindowStaysOnTopHint)
    return splash


def main():
    # ── Qt Application ────────────────────────────────────────
    app = QApplication(sys.argv)
    app.setApplicationName("Nexus Trader")
    app.setApplicationVersion("1.0.0")
    app.setOrganizationName("NexusTrader")

    # High-DPI support
    try:
        app.setAttribute(Qt.AA_UseHighDpiPixmaps, True)
    except AttributeError:
        pass  # Qt6 handles this automatically

    # Apply theme before splash
    ThemeManager.apply_dark_theme(app)

    # Splash screen
    splash = create_splash(app)
    splash.show()
    app.processEvents()

    logger.info("=" * 60)
    logger.info("  NEXUS TRADER v1.0 — Starting Up")
    logger.info("=" * 60)

    # ── Initialize Database ───────────────────────────────────
    try:
        splash.showMessage("  Initializing database...",
                           Qt.AlignBottom | Qt.AlignLeft, QColor("#8899AA"))
        app.processEvents()
        init_database()
        logger.info("Database ready")
    except Exception as e:
        logger.critical("Database initialization failed: %s", e, exc_info=True)
        sys.exit(1)

    # ── Initialize Intelligence Layer ─────────────────────────
    try:
        splash.showMessage("  Starting intelligence agents...",
                           Qt.AlignBottom | Qt.AlignLeft, QColor("#8899AA"))
        app.processEvents()

        # Orchestrator engine listens to agent events via EventBus — init first
        _orchestrator = get_orchestrator()
        logger.info("OrchestratorEngine ready")

        # AgentCoordinator auto-starts agents when exchange connects
        # (via EXCHANGE_CONNECTED event subscription in AgentCoordinator)
        _coordinator = get_coordinator()
        logger.info("AgentCoordinator ready")

        # Most intelligence agents (macro, social sentiment, news, geopolitical,
        # sector rotation) use public APIs and don't need an exchange connection.
        # Auto-start them immediately so the Intelligence Dashboard is live from
        # the moment the app opens. agents.auto_start=true (default) enables this.
        # Agents that need exchange data (funding_rate, order_book, options_flow)
        # will still start here — they'll gracefully handle no-connection by
        # returning stale/empty data until the exchange connects.
        from config.settings import settings as _app_settings
        if _app_settings.get("agents", {}).get("auto_start", True):
            _coordinator.start_all()
            logger.info("AgentCoordinator: agents auto-started at launch")

        # Initialize auto-execution settings
        from config.settings import settings
        from core.execution.order_router import order_router
        auto_cfg = settings.get("execution", {})
        if auto_cfg.get("auto_execute_enabled", False):
            order_router.set_auto_execute(
                enabled=True,
                min_confidence=auto_cfg.get("auto_execute_min_confidence", 0.72),
                min_signal_strength=auto_cfg.get("auto_execute_min_signal", 0.55),
                regime_whitelist=auto_cfg.get("auto_execute_regime_whitelist", []),
            )
            logger.info("Auto-execution enabled")
    except Exception as e:
        logger.warning("Intelligence layer init failed (non-fatal): %s", e)

    # Start HMM regime retrainer (monthly background scheduler)
    try:
        from core.regime.regime_retrainer import get_regime_retrainer
        _retrainer = get_regime_retrainer()
        _retrainer.start()
        logger.info("RegimeRetrainer scheduler started")
    except Exception as e:
        logger.debug("RegimeRetrainer start failed (non-fatal): %s", e)

    # Initialize CrashDefenseController singleton
    try:
        from core.risk.crash_defense_controller import get_crash_defense_controller
        _crash_defense = get_crash_defense_controller()
        logger.info("CrashDefenseController initialized")
    except Exception as e:
        logger.debug("CrashDefenseController init failed (non-fatal): %s", e)

    # ── Initialize Notification System ────────────────────────
    try:
        splash.showMessage("  Starting notification system...",
                           Qt.AlignBottom | Qt.AlignLeft, QColor("#8899AA"))
        app.processEvents()

        # Load notification config from settings / key vault
        from config.settings import settings
        from core.security.key_vault import key_vault

        notif_cfg: dict = settings.get("notifications", {})

        # Inject secrets from vault into channel configs
        # key_vault.load() returns "" when key is absent — no default kwarg needed
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
        logger.info("NotificationManager ready (%d channels)",
                    notification_manager.get_channel_count())
    except Exception as e:
        logger.warning("Notification system init failed (non-fatal): %s", e)

    # ── Launch Main Window ────────────────────────────────────
    try:
        splash.showMessage("  Loading interface...",
                           Qt.AlignBottom | Qt.AlignLeft, QColor("#8899AA"))
        app.processEvents()

        from gui.main_window import MainWindow
        window = MainWindow()

        # Show main window FIRST, then close splash.
        # PySide6's QSplashScreen.finish() enters a busy loop waiting for the
        # window to be "exposed".  If called BEFORE window.show(), the window
        # is never exposed and the loop spins forever — an intermittent hang
        # observed on 2026-03-16 22:40, 22:48, and 2026-03-17 08:27.
        # Fix: show the window first so it begins exposing, then let
        # splash.close() clean up the splash immediately.
        window.show()
        app.processEvents()    # let Qt begin painting the window
        splash.close()
        logger.info("Main window launched successfully")

        # Auto-connect the active exchange in a background thread so the
        # status bar and live feed update without blocking the UI.
        # 800 ms delay lets Qt finish painting the window first.
        _conn_thread = _StartupConnectThread(window)
        QTimer.singleShot(800, _conn_thread.start)
        window._startup_connect_thread = _conn_thread   # keep reference

    except Exception as e:
        logger.critical("Failed to launch main window: %s", e, exc_info=True)
        splash.close()
        sys.exit(1)

    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
