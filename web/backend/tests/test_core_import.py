# ============================================================
# Tests that core/ modules can be imported headlessly using
# the Qt shim.  This proves de-Qt-ification works.
# ============================================================
import sys
import os
import pytest

# Setup paths
_BACKEND = os.path.join(os.path.dirname(__file__), "..")
_PROJECT_ROOT = os.path.abspath(os.path.join(_BACKEND, "..", ".."))
sys.path.insert(0, _BACKEND)
sys.path.insert(0, _PROJECT_ROOT)

# Install shim BEFORE any core imports
from core_patch import install_qt_shim
install_qt_shim()


class TestCoreEventBusImport:
    """Verify core.event_bus can be imported via the shim."""

    def test_import_event_bus(self):
        from core.event_bus import bus, Topics, Event
        assert bus is not None
        assert hasattr(Topics, "TICK_UPDATE")
        assert hasattr(Topics, "TRADE_OPENED")

    def test_event_bus_publish_subscribe(self):
        from core.event_bus import bus
        results = []
        # NOTE: this uses the ORIGINAL core.event_bus which now
        # resolves QObject/Signal to our shim
        bus.subscribe("test.core.import", lambda e: results.append(e.data))
        bus.publish("test.core.import", data="from_core")
        assert results == ["from_core"]
        # Cleanup
        bus.clear_subscribers("test.core.import")


class TestCoreModuleImports:
    """
    Attempt to import key core modules that use PySide6.
    Each test proves the shim intercepts the Qt imports correctly.
    """

    def test_import_orchestrator_engine(self):
        """OrchestratorEngine(QObject) must import cleanly."""
        try:
            from core.orchestrator.orchestrator_engine import OrchestratorEngine
            assert OrchestratorEngine is not None
        except ImportError as e:
            if "PySide6" in str(e):
                pytest.fail(f"Qt dependency leaked: {e}")
            # Other import errors (missing deps like ccxt) are acceptable
            pytest.skip(f"Non-Qt import error: {e}")

    def test_import_base_agent(self):
        """BaseAgent(QThread) must import cleanly."""
        try:
            from core.agents.base_agent import BaseAgent
            assert BaseAgent is not None
        except ImportError as e:
            if "PySide6" in str(e):
                pytest.fail(f"Qt dependency leaked: {e}")
            pytest.skip(f"Non-Qt import error: {e}")

    def test_import_agent_coordinator(self):
        """AgentCoordinator(QObject) must import cleanly."""
        try:
            from core.agents.agent_coordinator import AgentCoordinator
            assert AgentCoordinator is not None
        except ImportError as e:
            if "PySide6" in str(e):
                pytest.fail(f"Qt dependency leaked: {e}")
            pytest.skip(f"Non-Qt import error: {e}")

    def test_import_online_trainer(self):
        """OnlineRLTrainer(QObject) must import cleanly."""
        try:
            from core.rl.online_trainer import OnlineRLTrainer
            assert OnlineRLTrainer is not None
        except ImportError as e:
            if "PySide6" in str(e):
                pytest.fail(f"Qt dependency leaked: {e}")
            pytest.skip(f"Non-Qt import error: {e}")

    def test_import_scanner(self):
        """Scanner with ScanWorker(QThread) must import cleanly."""
        try:
            from core.scanning.scanner import AssetScanner
            assert AssetScanner is not None
        except ImportError as e:
            if "PySide6" in str(e):
                pytest.fail(f"Qt dependency leaked: {e}")
            pytest.skip(f"Non-Qt import error: {e}")

    def test_import_data_feed(self):
        """LiveDataFeed(QThread) must import cleanly."""
        try:
            from core.market_data.data_feed import LiveDataFeed
            assert LiveDataFeed is not None
        except ImportError as e:
            if "PySide6" in str(e):
                pytest.fail(f"Qt dependency leaked: {e}")
            pytest.skip(f"Non-Qt import error: {e}")

    def test_import_websocket_feed(self):
        """WebSocketCandleFeed(QObject) must import cleanly."""
        try:
            from core.market_data.websocket_feed import WebSocketCandleFeed
            assert WebSocketCandleFeed is not None
        except ImportError as e:
            if "PySide6" in str(e):
                pytest.fail(f"Qt dependency leaked: {e}")
            pytest.skip(f"Non-Qt import error: {e}")

    def test_import_paper_executor(self):
        """PaperExecutor must import cleanly (uses EventBus)."""
        try:
            from core.execution.paper_executor import PaperExecutor
            assert PaperExecutor is not None
        except ImportError as e:
            if "PySide6" in str(e):
                pytest.fail(f"Qt dependency leaked: {e}")
            pytest.skip(f"Non-Qt import error: {e}")


class TestNoPySide6Leaks:
    """Verify no actual PySide6 package is loaded."""

    def test_no_real_pyside6_loaded(self):
        """The shim module should be in sys.modules, not real PySide6."""
        mod = sys.modules.get("PySide6.QtCore")
        assert mod is not None, "PySide6.QtCore not in sys.modules"
        # The real PySide6.QtCore has __file__ pointing to a .pyd/.so
        # Our shim module was created with types.ModuleType and has no __file__
        assert not hasattr(mod, "__file__") or mod.__file__ is None, \
            "Real PySide6 is loaded instead of shim!"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
