#!/usr/bin/env python3
# ============================================================
# NEXUS TRADER — Headless Entry Point  (Phase 2 Addendum)
#
# Pure-Python entry point with ZERO PySide6 imports.
# Starts the core trading engine without any GUI dependencies.
#
# Usage:
#   python main_headless.py              — run headless
#   python main_headless.py --log-level DEBUG
#
# This file guarantees:
#   - No PySide6 / Qt imports at any level
#   - Signal-based graceful shutdown (SIGINT, SIGTERM)
#   - Identical engine lifecycle to main.py --headless
# ============================================================
from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import threading
from pathlib import Path

# ── Ensure project root is on sys.path ──────────────────────
ROOT = Path(__file__).parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="nexustrader-headless",
        description="NexusTrader — Headless Core Engine (no GUI)",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="Set logging verbosity (default: INFO)",
    )
    args, _ = parser.parse_known_args()
    return args


def _setup_logging(level: str) -> None:
    """Configure logging without any GUI dependency."""
    log_dir = ROOT / "logs"
    log_dir.mkdir(exist_ok=True)

    fmt = "%(asctime)s  %(levelname)-8s  %(name)-40s  %(message)s"
    handlers: list[logging.Handler] = [
        logging.StreamHandler(sys.stdout),
    ]

    # Rotating file handler
    try:
        from logging.handlers import RotatingFileHandler
        fh = RotatingFileHandler(
            str(log_dir / "nexustrader_headless.log"),
            maxBytes=10 * 1024 * 1024,  # 10 MB
            backupCount=5,
        )
        fh.setFormatter(logging.Formatter(fmt))
        handlers.append(fh)
    except Exception:
        pass  # File logging optional

    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format=fmt,
        handlers=handlers,
        force=True,
    )


def main() -> int:
    args = _parse_args()
    _setup_logging(args.log_level)
    logger = logging.getLogger("nexustrader.headless")

    # ── Verify no PySide6 leakage ────────────────────────────
    pyside_mods = [m for m in sys.modules if m.startswith("PySide6")]
    if pyside_mods:
        logger.error(
            "FATAL: PySide6 modules leaked into headless path: %s", pyside_mods
        )
        return 1

    logger.info("=" * 60)
    logger.info("  NEXUS TRADER — Headless Mode")
    logger.info("  PID: %d  |  Python: %s", os.getpid(), sys.version.split()[0])
    logger.info("=" * 60)

    # ── Import and start engine (deferred to avoid import-time side effects) ──
    from core.engine import NexusEngine

    engine = NexusEngine()
    engine.start()

    # ── Block on signal ──────────────────────────────────────
    stop_event = threading.Event()

    def _shutdown(signum, _frame):
        logger.info("Received signal %d — initiating graceful shutdown", signum)
        stop_event.set()

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    logger.info("NexusTrader running in headless mode. Ctrl+C to stop.")
    stop_event.wait()

    engine.stop()
    logger.info("Headless engine stopped. Goodbye.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
