# ============================================================
# Phase 1 Tests — Trading Engine Service
#
# Tests that the engine module can be imported, basic service
# logic works, and command handlers are wired to real component
# APIs (without requiring Redis/DB).
# ============================================================
import sys
import os
import pytest
import asyncio

# Path setup
_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_WEB_DIR = os.path.dirname(_BACKEND)  # web/
_ENGINE_DIR = os.path.join(_WEB_DIR, "engine")
_PROJECT_ROOT = os.path.dirname(_WEB_DIR)  # NexusTrader/

for p in [_BACKEND, _WEB_DIR, _PROJECT_ROOT]:
    if p not in sys.path:
        sys.path.insert(0, p)

# Install shim
from core_patch import install_qt_shim
install_qt_shim()


class TestRedisBridge:
    """Test Redis bridge module imports and basic logic."""

    def test_import_redis_bridge(self):
        from core_patch.redis_bridge import RedisBridge
        assert RedisBridge is not None

    def test_safe_json_serialization(self):
        from core_patch.redis_bridge import _safe_json
        import datetime
        result = _safe_json({
            "key": "value",
            "nested": {"a": 1},
            "dt": datetime.datetime(2026, 1, 1),
            "set_val": {1, 2, 3},
        })
        assert result["key"] == "value"
        assert result["nested"]["a"] == 1
        assert isinstance(result["dt"], str)

    def test_event_serialization_roundtrip(self):
        from core_patch.event_bus import Event
        e = Event(topic="test.topic", data={"price": 50000.0}, source="engine")
        d = e.to_dict()
        e2 = Event.from_dict(d)
        assert e2.topic == "test.topic"
        assert e2.data["price"] == 50000.0
        assert e2.source == "engine"


class TestEngineServiceImport:
    """Test engine module is importable."""

    def test_engine_service_class(self):
        from engine.main import TradingEngineService
        assert TradingEngineService is not None

    def test_engine_service_init(self):
        from engine.main import TradingEngineService
        svc = TradingEngineService()
        assert svc._running is False
        assert svc._event_bus is not None
        assert svc._redis_bridge is None

    def test_engine_service_has_component_slots(self):
        """Verify all component references are initialized to None."""
        from engine.main import TradingEngineService
        svc = TradingEngineService()
        assert svc._settings is None
        assert svc._pe is None
        assert svc._scanner is None
        assert svc._exchange_manager is None
        assert svc._orchestrator is None
        assert svc._notification_mgr is None

    def test_engine_service_trading_pause_default(self):
        from engine.main import TradingEngineService
        svc = TradingEngineService()
        assert svc._trading_paused is False


class TestEventBusBridge:
    """Test EventBus ↔ Redis bridge attachment."""

    def test_attach_bridge(self):
        from core_patch.event_bus import EventBus

        bus = EventBus()
        # Can't test with real Redis, but verify the attach method exists
        assert hasattr(bus, "attach_redis_bridge")
        # Attach None should not crash
        bus.attach_redis_bridge(None)

    def test_event_bus_publish_without_bridge(self):
        from core_patch.event_bus import EventBus

        bus = EventBus()
        results = []
        bus.subscribe("test.no_bridge", lambda e: results.append(e.data))
        bus.publish("test.no_bridge", data="hello")
        assert results == ["hello"]


class TestDockerComposeStructure:
    """Validate Docker Compose file exists and has correct structure."""

    def test_docker_compose_exists(self):
        compose_path = os.path.join(_WEB_DIR, "docker-compose.yml")
        assert os.path.exists(compose_path), f"docker-compose.yml not found at {compose_path}"

    def test_env_example_exists(self):
        env_path = os.path.join(_WEB_DIR, ".env.example")
        assert os.path.exists(env_path), f".env.example not found at {env_path}"

    def test_api_dockerfile_exists(self):
        path = os.path.join(_BACKEND, "Dockerfile")
        assert os.path.exists(path)

    def test_engine_dockerfile_exists(self):
        path = os.path.join(_ENGINE_DIR, "Dockerfile")
        assert os.path.exists(path)


class TestAlembicStructure:
    """Verify Alembic migration environment is set up."""

    def test_alembic_ini_exists(self):
        path = os.path.join(_BACKEND, "alembic.ini")
        assert os.path.exists(path)

    def test_alembic_env_exists(self):
        path = os.path.join(_BACKEND, "alembic", "env.py")
        assert os.path.exists(path)

    def test_alembic_versions_dir_exists(self):
        path = os.path.join(_BACKEND, "alembic", "versions")
        assert os.path.isdir(path)

    def test_initial_migration_exists(self):
        """Verify the initial schema migration file was generated."""
        versions_dir = os.path.join(_BACKEND, "alembic", "versions")
        py_files = [f for f in os.listdir(versions_dir)
                     if f.endswith(".py") and "initial_schema" in f]
        assert len(py_files) == 1, (
            f"Expected 1 initial_schema migration, found: {py_files}"
        )

    def test_initial_migration_importable(self):
        """Verify the migration file can be imported and has correct structure."""
        import importlib.util
        versions_dir = os.path.join(_BACKEND, "alembic", "versions")
        py_files = [f for f in os.listdir(versions_dir)
                     if f.endswith(".py") and "initial_schema" in f]
        path = os.path.join(versions_dir, py_files[0])
        spec = importlib.util.spec_from_file_location("migration", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        assert hasattr(mod, "revision")
        assert hasattr(mod, "down_revision")
        assert mod.down_revision is None  # First migration
        assert hasattr(mod, "upgrade")
        assert hasattr(mod, "downgrade")
        assert callable(mod.upgrade)
        assert callable(mod.downgrade)


# ── Command Handler Tests (using mock components) ───────────

class MockPaperExecutor:
    """Minimal mock matching the real PaperExecutor interface."""

    def __init__(self, initial_capital_usdt=100_000.0):
        self._capital = initial_capital_usdt
        self._initial_capital = initial_capital_usdt
        self._peak_capital = initial_capital_usdt
        self._positions = {}
        self._closed_trades = []

    def get_open_positions(self):
        return [
            {"symbol": "BTC/USDT", "side": "buy", "entry_price": 50000.0,
             "size_usdt": 1000.0, "unrealized_pnl": 50.0},
        ]

    def get_stats(self):
        return {
            "total_trades": 10, "win_rate": 55.0, "profit_factor": 1.5,
            "total_pnl_usdt": 500.0, "open_positions": 1,
        }

    def get_production_status(self):
        return {
            "capital_usdt": self._capital,
            "peak_capital_usdt": self._peak_capital,
            "total_return_pct": 0.5,
            "drawdown_pct": 0.1,
            "open_positions": 1,
        }

    def close_position(self, symbol, price=None):
        if symbol == "BTC/USDT":
            return True
        return False

    def close_all(self):
        return 2

    def get_closed_trades(self):
        return list(self._closed_trades)


class MockSettings:
    """Minimal mock matching AppSettings interface."""

    def __init__(self):
        self._config = {
            "risk_engine": {"risk_pct_per_trade": 0.5},
            "scanner": {"auto_execute": True, "watchlist": ["BTC/USDT", "ETH/USDT"]},
        }

    def get(self, key_path, default=None):
        parts = key_path.split(".")
        d = self._config
        for p in parts:
            if isinstance(d, dict) and p in d:
                d = d[p]
            else:
                return default
        return d

    def get_section(self, section):
        return self._config.get(section, {})


class MockScanner:
    """Minimal mock matching AssetScanner interface."""

    def __init__(self):
        self._running = False

    def start(self):
        self._running = True

    def stop(self):
        self._running = False


class MockExchangeManager:
    """Minimal mock matching ExchangeManager interface."""

    def fetch_tickers(self, symbols):
        return {
            s: {"last": 50000.0, "bid": 49999.0, "ask": 50001.0, "quoteVolume": 1e8}
            for s in symbols
        }


def _make_engine_svc():
    """Create a TradingEngineService with mocked components."""
    from engine.main import TradingEngineService
    svc = TradingEngineService()
    svc._pe = MockPaperExecutor()
    svc._settings = MockSettings()
    svc._scanner = MockScanner()
    svc._exchange_manager = MockExchangeManager()
    return svc


class TestCommandHandlerGetPositions:
    """Test _cmd_get_positions wired to PaperExecutor.get_open_positions()."""

    def test_returns_positions(self):
        svc = _make_engine_svc()
        result = asyncio.run(
            svc._cmd_get_positions({})
        )
        assert result["status"] == "ok"
        assert len(result["positions"]) == 1
        assert result["positions"][0]["symbol"] == "BTC/USDT"
        assert result["count"] == 1

    def test_error_when_no_executor(self):
        from engine.main import TradingEngineService
        svc = TradingEngineService()
        result = asyncio.run(
            svc._cmd_get_positions({})
        )
        assert result["status"] == "error"
        assert "PaperExecutor" in result["detail"]


class TestCommandHandlerGetPortfolio:
    """Test _cmd_get_portfolio wired to PaperExecutor stats."""

    def test_returns_portfolio(self):
        svc = _make_engine_svc()
        result = asyncio.run(
            svc._cmd_get_portfolio({})
        )
        assert result["status"] == "ok"
        p = result["portfolio"]
        assert p["capital_usdt"] == 100_000.0
        assert p["total_trades"] == 10
        assert p["win_rate"] == 55.0
        assert p["profit_factor"] == 1.5
        assert p["trading_paused"] is False

    def test_error_when_no_executor(self):
        from engine.main import TradingEngineService
        svc = TradingEngineService()
        result = asyncio.run(
            svc._cmd_get_portfolio({})
        )
        assert result["status"] == "error"


class TestCommandHandlerGetConfig:
    """Test _cmd_get_config wired to AppSettings._config."""

    def test_returns_full_config(self):
        svc = _make_engine_svc()
        result = asyncio.run(
            svc._cmd_get_config({})
        )
        assert result["status"] == "ok"
        assert "risk_engine" in result["config"]
        assert "scanner" in result["config"]

    def test_returns_section(self):
        svc = _make_engine_svc()
        result = asyncio.run(
            svc._cmd_get_config({"section": "risk_engine"})
        )
        assert result["status"] == "ok"
        assert "risk_engine" in result["config"]
        assert result["config"]["risk_engine"]["risk_pct_per_trade"] == 0.5

    def test_error_when_no_settings(self):
        from engine.main import TradingEngineService
        svc = TradingEngineService()
        result = asyncio.run(
            svc._cmd_get_config({})
        )
        assert result["status"] == "error"


class TestCommandHandlerScanner:
    """Test start/stop scanner commands wired to AssetScanner."""

    def test_start_scanner(self):
        svc = _make_engine_svc()
        assert svc._scanner._running is False
        result = asyncio.run(
            svc._cmd_start_scanner({})
        )
        assert result["status"] == "ok"
        assert svc._scanner._running is True

    def test_start_scanner_already_running(self):
        svc = _make_engine_svc()
        svc._scanner._running = True
        result = asyncio.run(
            svc._cmd_start_scanner({})
        )
        assert result["status"] == "ok"
        assert "already running" in result["message"]

    def test_stop_scanner(self):
        svc = _make_engine_svc()
        svc._scanner._running = True
        result = asyncio.run(
            svc._cmd_stop_scanner({})
        )
        assert result["status"] == "ok"
        assert svc._scanner._running is False

    def test_stop_scanner_already_stopped(self):
        svc = _make_engine_svc()
        result = asyncio.run(
            svc._cmd_stop_scanner({})
        )
        assert result["status"] == "ok"
        assert "already stopped" in result["message"]

    def test_scanner_not_initialized(self):
        from engine.main import TradingEngineService
        svc = TradingEngineService()
        result = asyncio.run(
            svc._cmd_start_scanner({})
        )
        assert result["status"] == "error"


class TestCommandHandlerPauseResume:
    """Test pause/resume trading commands."""

    def test_pause_trading(self):
        svc = _make_engine_svc()
        assert svc._trading_paused is False
        result = asyncio.run(
            svc._cmd_pause_trading({})
        )
        assert result["status"] == "ok"
        assert svc._trading_paused is True

    def test_pause_already_paused(self):
        svc = _make_engine_svc()
        svc._trading_paused = True
        result = asyncio.run(
            svc._cmd_pause_trading({})
        )
        assert result["status"] == "ok"
        assert "already paused" in result["message"]

    def test_resume_trading(self):
        svc = _make_engine_svc()
        svc._trading_paused = True
        result = asyncio.run(
            svc._cmd_resume_trading({})
        )
        assert result["status"] == "ok"
        assert svc._trading_paused is False

    def test_resume_already_active(self):
        svc = _make_engine_svc()
        result = asyncio.run(
            svc._cmd_resume_trading({})
        )
        assert result["status"] == "ok"
        assert "already active" in result["message"]


class TestCommandHandlerClosePosition:
    """Test close_position command wired to PaperExecutor.close_position()."""

    def test_close_existing_position(self):
        svc = _make_engine_svc()
        result = asyncio.run(
            svc._cmd_close_position({"symbol": "BTC/USDT"})
        )
        assert result["status"] == "ok"
        assert "closed" in result["message"]

    def test_close_nonexistent_position(self):
        svc = _make_engine_svc()
        result = asyncio.run(
            svc._cmd_close_position({"symbol": "DOGE/USDT"})
        )
        assert result["status"] == "error"
        assert "No open position" in result["detail"]

    def test_close_missing_symbol(self):
        svc = _make_engine_svc()
        result = asyncio.run(
            svc._cmd_close_position({})
        )
        assert result["status"] == "error"
        assert "symbol required" in result["detail"]


class TestCommandHandlerCloseAll:
    """Test close_all command wired to PaperExecutor.close_all()."""

    def test_close_all(self):
        svc = _make_engine_svc()
        result = asyncio.run(
            svc._cmd_close_all({})
        )
        assert result["status"] == "ok"
        assert result["count"] == 2


class TestCommandHandlerRefreshData:
    """Test refresh_data command wired to ExchangeManager.fetch_tickers()."""

    def test_refresh_data(self):
        svc = _make_engine_svc()
        result = asyncio.run(
            svc._cmd_refresh_data({})
        )
        assert result["status"] == "ok"
        assert len(result["symbols"]) == 2  # From mock settings watchlist
        assert "BTC/USDT" in result["symbols"]

    def test_refresh_data_no_exchange(self):
        from engine.main import TradingEngineService
        svc = TradingEngineService()
        result = asyncio.run(
            svc._cmd_refresh_data({})
        )
        assert result["status"] == "error"


class TestCommandHandlerRouting:
    """Test the _handle_command routing method."""

    def test_unknown_action(self):
        svc = _make_engine_svc()
        result = asyncio.run(
            svc._handle_command("nonexistent_action", {})
        )
        assert result["status"] == "error"
        assert "Unknown action" in result["detail"]

    def test_all_actions_registered(self):
        """Verify all 10 expected actions have handlers."""
        svc = _make_engine_svc()
        expected_actions = [
            "get_positions", "get_portfolio", "get_config",
            "start_scanner", "stop_scanner",
            "pause_trading", "resume_trading",
            "close_position", "close_all_positions", "refresh_data",
        ]
        for action in expected_actions:
            # All should route without "Unknown action" error
            result = asyncio.run(
                svc._handle_command(action, {})
            )
            assert result["status"] != "error" or "Unknown action" not in result.get("detail", ""), \
                f"Action '{action}' not registered in handler routing"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
