#!/usr/bin/env python3
# ============================================================
# NEXUS TRADER — Application Entry Point
# Institutional-Grade AI Trading Platform v1.1
#
# Launch modes:
#   python main.py              — normal interactive mode
#   python main.py --test-ui    — headless UI validation mode
#     Renders offscreen, navigates all pages, captures screenshots,
#     validates displayed data, writes report to artifacts/ui/.
#     Exit code: 0 = all checks passed, 1 = failures found.
# ============================================================

import os
import sys
import logging
import argparse
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


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="nexustrader",
        description="NexusTrader — Institutional AI Trading Platform",
    )
    parser.add_argument(
        "--test-ui",
        action="store_true",
        default=False,
        help=(
            "Run headless UI validation: navigate all pages, capture screenshots, "
            "validate displayed data, write report to artifacts/ui/. "
            "Exit 0 = all checks passed, exit 1 = failures found."
        ),
    )
    # Parse only known args so Qt's own args pass through untouched
    args, _ = parser.parse_known_args()
    return args


def _run_ui_tests(app: QApplication) -> int:
    """
    Headless UI validation mode.

    Initialises the minimum required subsystems (DB only), builds the
    MainWindow in offscreen mode, runs the UITestController validation
    suite, prints the report, and returns an exit code.
    """
    logger.info("=" * 60)
    logger.info("  NEXUS TRADER — UI TEST MODE")
    logger.info("=" * 60)

    # Minimal init: database only (no exchange, no agents, no notifs)
    try:
        init_database()
        logger.info("[test-ui] Database ready")
    except Exception as exc:
        logger.critical("[test-ui] Database init failed: %s", exc)
        return 2

    # Build main window (pages are loaded eagerly in __init__)
    try:
        from gui.main_window import MainWindow
        window = MainWindow()
        # Keep window off-screen — never call window.show() in test mode
        logger.info("[test-ui] MainWindow built (%d pages registered)",
                    len(window._pages))
    except Exception as exc:
        logger.critical("[test-ui] MainWindow build failed: %s", exc, exc_info=True)
        return 2

    # Run validation
    try:
        from gui.ui_test_controller import UITestController
        ctrl = UITestController(window)
        report = ctrl.run_all_checks(capture_screenshots=True)
    except Exception as exc:
        logger.critical("[test-ui] UITestController failed: %s", exc, exc_info=True)
        return 2

    # Print report to stdout
    for line in report.summary_lines():
        print(line)

    return 0 if report.failed == 0 else 1


def main():
    args = _parse_args()

    # ── Offscreen mode for --test-ui ──────────────────────────
    if args.test_ui:
        # Must be set BEFORE QApplication is created.
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        # Bootstrap EGL stub if libEGL is missing (Linux VM / CI).
        # Self-re-exec with LD_LIBRARY_PATH pointing to the stub so the
        # dynamic linker can resolve libEGL.so.1 before Qt is loaded.
        if not os.environ.get("_NEXUS_EGL_READY"):
            import ctypes.util, subprocess
            if not ctypes.util.find_library("EGL"):
                stub_dir  = ROOT / "scripts" / "lib"
                stub_path = stub_dir / "libEGL.so.1"
                if not stub_path.exists():
                    stub_dir.mkdir(parents=True, exist_ok=True)
                    # Stub source is embedded in scripts/run_ui_checks.py;
                    # fall back to a copy here.
                    src = stub_dir / "_egl_stub_main.c"
                    src.write_text(
                        "#include<stddef.h>\n"
                        "typedef void* EGLDisplay;typedef void* EGLConfig;"
                        "typedef void* EGLContext;typedef void* EGLSurface;"
                        "typedef int EGLint;typedef unsigned int EGLBoolean;"
                        "typedef unsigned int EGLenum;"
                        "typedef void* EGLNativeDisplayType;"
                        "typedef void* EGLNativeWindowType;"
                        "typedef void (*__eglMustCastToProperFunctionPointerType)(void);\n"
                        "EGLint eglGetError(void){return 0x3000;}\n"
                        "EGLDisplay eglGetDisplay(EGLNativeDisplayType d){return(void*)1;}\n"
                        "EGLBoolean eglInitialize(EGLDisplay d,EGLint*m,EGLint*n){if(m)*m=1;if(n)*n=5;return 1;}\n"
                        "EGLBoolean eglBindAPI(EGLenum a){return 1;}\n"
                        "EGLBoolean eglChooseConfig(EGLDisplay d,const EGLint*al,EGLConfig*c,EGLint cs,EGLint*nc){if(nc)*nc=0;return 1;}\n"
                        "EGLBoolean eglGetConfigs(EGLDisplay d,EGLConfig*c,EGLint cs,EGLint*nc){if(nc)*nc=0;return 1;}\n"
                        "EGLBoolean eglGetConfigAttrib(EGLDisplay d,EGLConfig c,EGLint a,EGLint*v){if(v)*v=0;return 1;}\n"
                        "EGLContext eglCreateContext(EGLDisplay d,EGLConfig c,EGLContext sc,const EGLint*al){return NULL;}\n"
                        "EGLSurface eglCreateWindowSurface(EGLDisplay d,EGLConfig c,EGLNativeWindowType w,const EGLint*al){return NULL;}\n"
                        "EGLSurface eglCreatePbufferSurface(EGLDisplay d,EGLConfig c,const EGLint*al){return NULL;}\n"
                        "EGLBoolean eglMakeCurrent(EGLDisplay d,EGLSurface dr,EGLSurface rd,EGLContext c){return 0;}\n"
                        "EGLBoolean eglSwapBuffers(EGLDisplay d,EGLSurface s){return 0;}\n"
                        "EGLBoolean eglDestroyContext(EGLDisplay d,EGLContext c){return 1;}\n"
                        "EGLBoolean eglDestroySurface(EGLDisplay d,EGLSurface s){return 1;}\n"
                        "EGLBoolean eglTerminate(EGLDisplay d){return 1;}\n"
                        "EGLBoolean eglReleaseThread(void){return 1;}\n"
                        "EGLBoolean eglSwapInterval(EGLDisplay d,EGLint i){return 1;}\n"
                        "EGLDisplay eglGetCurrentDisplay(void){return NULL;}\n"
                        "EGLContext eglGetCurrentContext(void){return NULL;}\n"
                        "EGLSurface eglGetCurrentSurface(EGLenum w){return NULL;}\n"
                        "EGLBoolean eglQueryContext(EGLDisplay d,EGLContext c,EGLint a,EGLint*v){if(v)*v=0;return 1;}\n"
                        "const char* eglQueryString(EGLDisplay d,EGLint n){return \"\";}\n"
                        "__eglMustCastToProperFunctionPointerType eglGetProcAddress(const char*n){return NULL;}\n"
                    )
                    subprocess.run(
                        ["gcc", "-shared", "-fPIC", "-o", str(stub_path), str(src), "-lc"],
                        check=False,
                    )
                    src.unlink(missing_ok=True)
                if stub_path.exists():
                    new_env = dict(os.environ)
                    existing = new_env.get("LD_LIBRARY_PATH", "")
                    new_env["LD_LIBRARY_PATH"] = (
                        str(stub_dir) + (":" + existing if existing else "")
                    )
                    new_env["_NEXUS_EGL_READY"] = "1"
                    os.execve(sys.executable, [sys.executable] + sys.argv, new_env)

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

    # Apply theme (needed in both modes)
    ThemeManager.apply_dark_theme(app)

    # ── UI test mode — skip splash / agents / exchange ────────
    if args.test_ui:
        return _run_ui_tests(app)

    # ── Normal mode: Splash screen ────────────────────────────
    splash = create_splash(app)
    splash.show()
    app.processEvents()

    logger.info("=" * 60)
    logger.info("  NEXUS TRADER v1.1 — Starting Up")
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
