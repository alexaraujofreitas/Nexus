# ============================================================
# NEXUS TRADER — Core Patch Module
#
# Call install_qt_shim() BEFORE importing any core/ modules.
# This injects pure-Python replacements for PySide6 so that
# core/ modules can run headlessly without Qt installed.
# ============================================================
from __future__ import annotations

import sys
import types
import logging

logger = logging.getLogger(__name__)

_INSTALLED = False


def install_qt_shim() -> None:
    """
    Inject the Qt shim into sys.modules so that:
        from PySide6.QtCore import QObject, QThread, Signal, QTimer, Slot, Qt, QMetaObject
    resolves to our pure-Python replacements.

    Must be called once, before any core/ imports.
    """
    global _INSTALLED
    if _INSTALLED:
        return

    from core_patch import qt_shim

    # Create fake PySide6 package module
    pyside6_mod = types.ModuleType("PySide6")
    pyside6_mod.__path__ = []  # mark as package

    # Create fake PySide6.QtCore module with our shim classes
    qtcore_mod = types.ModuleType("PySide6.QtCore")
    qtcore_mod.QObject = qt_shim.QObject
    qtcore_mod.QThread = qt_shim.QThread
    qtcore_mod.Signal = qt_shim.Signal
    qtcore_mod.QTimer = qt_shim.QTimer
    qtcore_mod.Slot = qt_shim.Slot
    qtcore_mod.Qt = qt_shim.Qt
    qtcore_mod.QMetaObject = qt_shim.QMetaObject

    # Inject into sys.modules
    sys.modules["PySide6"] = pyside6_mod
    sys.modules["PySide6.QtCore"] = qtcore_mod

    # Also shim PySide6.QtWidgets and PySide6.QtGui as empty
    # (some imports may reference these; they won't be used in headless mode)
    for submod in ("QtWidgets", "QtGui", "QtCharts", "QtSvg"):
        mod = types.ModuleType(f"PySide6.{submod}")
        sys.modules[f"PySide6.{submod}"] = mod

    _INSTALLED = True
    logger.info("Qt shim installed — PySide6 imports redirected to pure-Python replacements")
