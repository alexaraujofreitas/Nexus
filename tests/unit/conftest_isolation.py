"""
Contamination isolation helper — prevents test_breakeven_sl_after_partial.py
from poisoning sys.modules for subsequent test files.

Usage: pytest --import-mode=importlib tests/unit/
OR add conftest.py entry that clears injected mocks between files.
"""
import sys
import pytest

# Modules that test_breakeven_sl patches at module level
_PATCHED_MODULES = [
    "core.meta_decision.confluence_scorer",
    "core.database.engine",
    "core.analytics.filter_stats",
    "core.monitoring.trade_monitor",
    "core.learning.trade_outcome_store",
    "core.learning.level2_tracker",
]

@pytest.fixture(autouse=True, scope="session")
def clear_test_module_patches():
    """
    At session start, clear any module-level patches that were applied
    during collection of test_breakeven_sl_after_partial.py.
    This allows tests that import these modules to get real implementations.
    """
    yield
    # Cleanup: remove mock entries so next file imports real modules
    for mod in _PATCHED_MODULES:
        if mod in sys.modules:
            # Only remove if it is a MagicMock (i.e., was patched)
            m = sys.modules[mod]
            if type(m).__name__ == "MagicMock":
                del sys.modules[mod]
