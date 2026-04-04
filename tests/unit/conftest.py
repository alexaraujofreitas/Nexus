"""
tests/unit/conftest.py — Test isolation for sys.modules contamination.

Root cause
----------
test_zzz_breakeven_sl_after_partial.py (and similarly-structured files) force-
assign MagicMocks into sys.modules at MODULE LEVEL:

    sys.modules["core.event_bus"] = MagicMock(...)

This runs at COLLECTION time.  Even though we renamed them to ``test_zzz_*`` so
they are collected LAST (after all clean test files have been imported), they
still contaminate sys.modules BEFORE any test executes.

When the contaminated module is later deleted from sys.modules (old approach),
Python creates a *new* module instance on re-import.  Any already-imported
module that held a reference to the original module-level singleton (e.g.
``crash_defense_controller.bus``) now points to a different object than the one
the test fixture obtains.  Subscriptions made via the fixture's bus reference
never trigger on the controller's bus reference.

Fix
---
1. Pre-import critical modules in conftest.py module-level code.  conftest.py
   is processed BEFORE any test file, so these imports capture real singletons.
2. Save those real module objects in ``_REAL_MODULES``.
3. In ``_restore_contaminated_modules``, RESTORE the saved real module back into
   sys.modules instead of deleting it.  This guarantees that all parties
   (controller, fixture, test) share the same bus/Topics/etc. objects.
4. For core.database.engine (and its dependents) where the real module wasn't
   pre-loaded, fall back to deletion so the ORM layer is rebuilt cleanly.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock

import pytest

# ── Pre-import real modules BEFORE any test file can contaminate them ─────────
# These imports run at conftest.py import time — the very first thing pytest
# does before collecting any test files.

_REAL_MODULES: dict[str, ModuleType] = {}

def _preload(mod_name: str) -> None:
    """Import mod_name if not already imported and stash the real module."""
    if mod_name not in sys.modules:
        __import__(mod_name)
    module = sys.modules[mod_name]
    if not isinstance(module, MagicMock):
        _REAL_MODULES[mod_name] = module

# Order matters: import parents before children.
for _m in (
    "core.event_bus",
    "core.risk.crash_defense_controller",
):
    try:
        _preload(_m)
    except Exception:
        pass  # optional — best-effort

# ── Files that patch sys.modules at collection time ───────────────────────────
# Prefixed test_zzz_ so they are collected (imported) LAST alphabetically,
# preventing their module-level patches from poisoning earlier test files.
_CONTAMINATING_FILES = {
    "test_zzz_breakeven_sl_after_partial.py",
    "test_auto_execute.py",
    "test_section5_rolling_pf_guardrails.py",
    "test_section6_failure_modes.py",
}

# Modules those files override with MagicMock
_RESTORABLE = [
    "config",
    "config.settings",
    "core.event_bus",
    "core.database.engine",
    "core.analytics.filter_stats",
    "core.monitoring.trade_monitor",
    "core.learning.trade_outcome_store",
    "core.meta_decision.confluence_scorer",
    "core.learning.level2_tracker",
]


def pytest_collection_modifyitems(session, config, items):
    """
    Re-order test items so that files in _CONTAMINATING_FILES run after all
    other unit tests.  This prevents their module-level sys.modules patches
    from poisoning earlier tests in the same collection run.
    """
    normal = []
    last = []
    for item in items:
        fname = Path(item.fspath).name
        if fname in _CONTAMINATING_FILES:
            last.append(item)
        else:
            normal.append(item)
    items[:] = normal + last


def _is_mock(module) -> bool:
    """Return True if the module is a MagicMock (i.e. was patched)."""
    return isinstance(module, MagicMock)


@pytest.fixture(autouse=True)
def _restore_contaminated_modules():
    """
    Per-test fixture: before each test, restore any sys.modules entries that
    were replaced with MagicMocks by contaminating test files.

    Strategy:
    - If we pre-loaded the real module (it's in _REAL_MODULES), PUT IT BACK
      directly so all existing references (e.g. crash_defense_controller.bus)
      remain valid — no new module object is created.
    - Otherwise fall back to deletion so the next import reloads from disk.

    When core.database.engine is restored, also evict core.database.models and
    core.database so that ORM model classes re-register against a fresh real
    Base.metadata (otherwise create_all() creates no tables because the old
    models were registered against the mocked Base).
    """
    engine_was_mocked = (
        "core.database.engine" in sys.modules
        and _is_mock(sys.modules["core.database.engine"])
    )

    for mod_name in _RESTORABLE:
        if mod_name in sys.modules and _is_mock(sys.modules[mod_name]):
            if mod_name in _REAL_MODULES:
                # Restore the saved real module — preserves object identity.
                sys.modules[mod_name] = _REAL_MODULES[mod_name]
            else:
                del sys.modules[mod_name]

    # If the DB engine was mocked, force-evict the ORM layer too so models
    # re-import cleanly against the fresh real Base on next access.
    if engine_was_mocked:
        for dep in ("core.database.models", "core.database"):
            sys.modules.pop(dep, None)

    yield
