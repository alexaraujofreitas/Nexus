#!/usr/bin/env python3
"""
NexusTrader — Autonomous UI Validation Script
=============================================

Run this script after any UI-impacting change to validate that:
  • All pages load without errors
  • Navigation updates the correct page + sidebar state
  • Data displayed on key pages matches source data
  • Status bar is live and updating
  • Screenshots of every page are captured for visual review

Usage
-----
    python scripts/run_ui_checks.py              # full run (screenshots + checks)
    python scripts/run_ui_checks.py --no-screenshots   # checks only, faster
    python scripts/run_ui_checks.py --pages market_scanner paper_trading  # subset

Exit codes
----------
  0 — all checks passed
  1 — one or more checks failed
  2 — fatal startup error (DB / window failed to build)

Output
------
  artifacts/ui/<YYYYMMDD_HHMMSS>/
    ├── report.json       machine-readable results
    ├── report.txt        human-readable summary
    ├── dashboard_Dashboard.png
    ├── market_scanner_Market_Scanner.png
    ├── ... (one PNG per page)
    └── ...

Self-usage by Claude
--------------------
Run this script after any change that touches GUI files.  Read report.json
to identify failures, investigate the relevant page source, fix, and re-run.
Never ask the user for screenshots — use this script instead.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

# ── Path setup ───────────────────────────────────────────────
ROOT = Path(__file__).parent.parent
SCRIPTS_DIR = Path(__file__).parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# ── EGL stub auto-bootstrap (Linux VM / CI — no Mesa EGL) ───
# Qt6/PySide6 requires libEGL.so.1 even for offscreen rendering.
# LD_LIBRARY_PATH must be set BEFORE the interpreter starts, so we
# self-re-exec once with the updated environment if the stub is needed.
_EGL_STUB_SRC = r"""
#include <stddef.h>
typedef void* EGLDisplay; typedef void* EGLConfig; typedef void* EGLContext;
typedef void* EGLSurface; typedef int EGLint; typedef unsigned int EGLBoolean;
typedef unsigned int EGLenum; typedef void* EGLNativeDisplayType;
typedef void* EGLNativeWindowType;
typedef void (*__eglMustCastToProperFunctionPointerType)(void);
EGLint eglGetError(void){return 0x3000;}
EGLDisplay eglGetDisplay(EGLNativeDisplayType d){return(void*)1;}
EGLBoolean eglInitialize(EGLDisplay d,EGLint*m,EGLint*n){if(m)*m=1;if(n)*n=5;return 1;}
EGLBoolean eglBindAPI(EGLenum a){return 1;}
EGLBoolean eglChooseConfig(EGLDisplay d,const EGLint*al,EGLConfig*c,EGLint cs,EGLint*nc){if(nc)*nc=0;return 1;}
EGLBoolean eglGetConfigs(EGLDisplay d,EGLConfig*c,EGLint cs,EGLint*nc){if(nc)*nc=0;return 1;}
EGLBoolean eglGetConfigAttrib(EGLDisplay d,EGLConfig c,EGLint a,EGLint*v){if(v)*v=0;return 1;}
EGLContext eglCreateContext(EGLDisplay d,EGLConfig c,EGLContext sc,const EGLint*al){return NULL;}
EGLSurface eglCreateWindowSurface(EGLDisplay d,EGLConfig c,EGLNativeWindowType w,const EGLint*al){return NULL;}
EGLSurface eglCreatePbufferSurface(EGLDisplay d,EGLConfig c,const EGLint*al){return NULL;}
EGLBoolean eglMakeCurrent(EGLDisplay d,EGLSurface draw,EGLSurface read,EGLContext c){return 0;}
EGLBoolean eglSwapBuffers(EGLDisplay d,EGLSurface s){return 0;}
EGLBoolean eglDestroyContext(EGLDisplay d,EGLContext c){return 1;}
EGLBoolean eglDestroySurface(EGLDisplay d,EGLSurface s){return 1;}
EGLBoolean eglTerminate(EGLDisplay d){return 1;}
EGLBoolean eglReleaseThread(void){return 1;}
EGLBoolean eglSwapInterval(EGLDisplay d,EGLint i){return 1;}
EGLDisplay eglGetCurrentDisplay(void){return NULL;}
EGLContext eglGetCurrentContext(void){return NULL;}
EGLSurface eglGetCurrentSurface(EGLenum w){return NULL;}
EGLBoolean eglQueryContext(EGLDisplay d,EGLContext c,EGLint a,EGLint*v){if(v)*v=0;return 1;}
const char* eglQueryString(EGLDisplay d,EGLint n){return "";}
__eglMustCastToProperFunctionPointerType eglGetProcAddress(const char*n){return NULL;}
"""

def _bootstrap_egl() -> None:
    """
    If libEGL.so.1 is missing from the system, build a headless stub and
    self-re-exec this process with LD_LIBRARY_PATH pointing to it.
    The guard env-var _NEXUS_EGL_READY prevents infinite re-exec.
    """
    import ctypes.util, subprocess
    if os.environ.get("_NEXUS_EGL_READY"):
        return  # already bootstrapped
    if ctypes.util.find_library("EGL"):
        return  # system EGL found

    stub_dir  = SCRIPTS_DIR / "lib"
    stub_path = stub_dir / "libEGL.so.1"

    if not stub_path.exists():
        stub_dir.mkdir(parents=True, exist_ok=True)
        src = stub_dir / "_egl_stub.c"
        src.write_text(_EGL_STUB_SRC)
        result = subprocess.run(
            ["gcc", "-shared", "-fPIC", "-o", str(stub_path), str(src), "-lc"],
            capture_output=True, text=True,
        )
        src.unlink(missing_ok=True)
        if result.returncode != 0:
            print(f"WARNING: EGL stub compile failed:\n{result.stderr}",
                  file=sys.stderr)
            return

    # Re-exec this process with LD_LIBRARY_PATH set.
    # The new process will load libEGL.so.1 from the stub dir at startup.
    new_env = dict(os.environ)
    existing = new_env.get("LD_LIBRARY_PATH", "")
    new_env["LD_LIBRARY_PATH"] = str(stub_dir) + (":" + existing if existing else "")
    new_env["_NEXUS_EGL_READY"] = "1"
    new_env["QT_QPA_PLATFORM"] = "offscreen"
    os.execve(sys.executable, [sys.executable] + sys.argv, new_env)

_bootstrap_egl()  # Must run before any PySide6 import

# ── Offscreen rendering (MUST be set before Qt imports) ──────
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# ── Logging ──────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("run_ui_checks")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="run_ui_checks",
        description="NexusTrader autonomous UI validation",
    )
    p.add_argument(
        "--no-screenshots",
        action="store_true",
        default=False,
        help="Skip screenshot capture (faster — checks only)",
    )
    p.add_argument(
        "--pages",
        nargs="+",
        metavar="PAGE_KEY",
        default=None,
        help=(
            "Validate only these page keys.  "
            "Example: --pages dashboard paper_trading market_scanner. "
            "If omitted, all pages are checked."
        ),
    )
    p.add_argument(
        "--quiet",
        action="store_true",
        default=False,
        help="Only print the summary, suppress per-check lines",
    )
    return p.parse_args()


def main() -> int:
    args = parse_args()

    # ── Qt Application ────────────────────────────────────────
    logger.info("Starting Qt application (offscreen mode)")
    from PySide6.QtWidgets import QApplication
    from PySide6.QtCore import Qt

    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("Nexus Trader UI Checks")

    try:
        app.setAttribute(Qt.AA_UseHighDpiPixmaps, True)
    except AttributeError:
        pass

    # Apply dark theme so widgets render with correct styling
    try:
        from gui.theme.theme_manager import ThemeManager
        ThemeManager.apply_dark_theme(app)
        logger.info("Dark theme applied")
    except Exception as exc:
        logger.warning("Could not apply theme (non-fatal): %s", exc)

    # ── Minimal subsystem init ────────────────────────────────
    # Database init is OPTIONAL for UI checks.  The DB may be locked by a
    # running NexusTrader process on the host, or may not be reachable via
    # the VM mount. Page-load / navigation / structural checks do not need
    # the database. Only data cross-checks that explicitly query the DB will
    # be skipped (marked as SKIPPED in the report).
    logger.info("Initialising database (best-effort)...")
    try:
        from core.database.engine import init_database
        init_database()
        logger.info("Database ready")
    except Exception as exc:
        logger.warning(
            "Database init failed (non-fatal — DB data checks will be skipped): %s",
            str(exc)[:120],
        )

    # ── Build MainWindow ──────────────────────────────────────
    logger.info("Building MainWindow...")
    try:
        from gui.main_window import MainWindow
        window = MainWindow()
        # Do NOT call window.show() — we stay offscreen throughout
        logger.info("MainWindow ready (%d pages registered)", len(window._pages))
    except Exception as exc:
        logger.critical("MainWindow build failed: %s", exc, exc_info=True)
        return 2

    # ── Build controller & run checks ────────────────────────
    logger.info("Running UI checks...")
    try:
        from gui.ui_test_controller import UITestController
        ctrl = UITestController(window)

        # If --pages was specified, restrict ALL_PAGES
        if args.pages:
            valid_keys = set(ctrl.get_all_page_keys())
            unknown = [k for k in args.pages if k not in valid_keys]
            if unknown:
                logger.warning("Unknown page keys (will be skipped): %s", unknown)
            ctrl.ALL_PAGES = [
                (k, lbl) for k, lbl in ctrl.ALL_PAGES
                if k in args.pages
            ]
            logger.info("Restricted to %d pages: %s", len(ctrl.ALL_PAGES), args.pages)

        capture = not args.no_screenshots
        report = ctrl.run_all_checks(capture_screenshots=capture)

    except Exception as exc:
        logger.critical("UITestController error: %s", exc, exc_info=True)
        return 2

    # ── Print results ─────────────────────────────────────────
    if not args.quiet:
        # Print individual check results
        print()
        print(f"{'ID':<30} {'PAGE':<25} {'STATUS'}")
        print("-" * 75)
        for r in report.checks:
            status = "✓ PASS" if r.passed else "✗ FAIL"
            line = f"{r.check_id:<30} {r.page:<25} {status}"
            if not r.passed and r.details:
                line += f"\n{'':>30}  {r.details}"
            print(line)
        print()

    # Always print the summary
    for line in report.summary_lines():
        print(line)

    # ── Return exit code ──────────────────────────────────────
    return 0 if report.failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
