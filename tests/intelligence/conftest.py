"""
tests/intelligence/conftest.py — Ensure real EventBus and Topics for intelligence tests.

Intelligence tests need the REAL Topics enum and EventBus, not mocks.
This conftest runs BEFORE test collection for this directory and ensures the real
modules are available.
"""
from __future__ import annotations

import sys
from types import ModuleType
from unittest.mock import MagicMock

import pytest

# Pre-import the real event_bus module to capture the real Topics enum
# and bus singleton, ensuring they're not replaced by MagicMocks from
# contaminating test files in tests/unit/test_zzz_*.py
_REAL_MODULES: dict[str, ModuleType] = {}

def _preload(mod_name: str) -> None:
    """Import mod_name if not already imported and stash the real module."""
    if mod_name not in sys.modules:
        __import__(mod_name)
    module = sys.modules[mod_name]
    if not isinstance(module, MagicMock):
        _REAL_MODULES[mod_name] = module

for _m in ("core.event_bus",):
    try:
        _preload(_m)
    except Exception:
        pass


def _is_mock(module) -> bool:
    """Return True if the module is a MagicMock (i.e. was patched)."""
    return isinstance(module, MagicMock)


@pytest.fixture(autouse=True)
def _restore_real_event_bus():
    """
    Per-test fixture: before each intelligence test, restore the real core.event_bus
    if it was replaced with a MagicMock by contaminating test files in tests/unit/.

    This mirrors the logic in tests/unit/conftest.py but applies to intelligence tests.
    """
    if "core.event_bus" in sys.modules and _is_mock(sys.modules["core.event_bus"]):
        if "core.event_bus" in _REAL_MODULES:
            # Restore the saved real module — preserves object identity.
            sys.modules["core.event_bus"] = _REAL_MODULES["core.event_bus"]
        else:
            del sys.modules["core.event_bus"]

    yield
