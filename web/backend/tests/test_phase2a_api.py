# ============================================================
# Phase 2A — API Expansion Tests
#
# Tests all new Phase 2A API endpoints and engine command handlers.
# Uses mock Redis + mock engine responses to verify:
#   - Route registration and auth requirement
#   - Request parameter validation
#   - Response schema correctness
#   - Engine command dispatch (correct action + params)
# ============================================================
from __future__ import annotations

import json
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ── Path setup ──────────────────────────────────────────────
_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_WEB_DIR = os.path.dirname(_BACKEND)
_PROJECT_ROOT = os.path.dirname(_WEB_DIR)

# Backend MUST be first so `from main import app` finds web/backend/main.py
# not the desktop main.py at project root.
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)
for p in [_WEB_DIR, _PROJECT_ROOT]:
    if p not in sys.path:
        sys.path.append(p)

from core_patch import install_qt_shim
install_qt_shim()

# Force-remove any cached desktop 'main' module and ensure backend
# main.py is resolved first by temporarily pushing backend to sys.path[0].
if "main" in sys.modules:
    _cached = sys.modules.pop("main")
    # Only re-add if it was actually the backend main
    if hasattr(_cached, "app"):
        sys.modules["main"] = _cached

# Ensure backend dir is before project root in sys.path
_backend_idx = sys.path.index(_BACKEND) if _BACKEND in sys.path else -1
if _backend_idx != 0:
    if _BACKEND in sys.path:
        sys.path.remove(_BACKEND)
    sys.path.insert(0, _BACKEND)


# ── Shared fixtures ─────────────────────────────────────────

# JWT token for auth
_TEST_SECRET = "test-secret-32-chars-long-enough!!"


def _make_token():
    """Create a valid JWT access token for testing."""
    from app.auth.jwt import create_access_token
    return create_access_token({"sub": "1", "email": "test@nexustest.com"})


def _auth_headers():
    return {"Authorization": f"Bearer {_make_token()}"}


# ── Mock send_engine_command ────────────────────────────────

_mock_response = {}


async def _mock_send_cmd(action: str, params: dict, timeout: int = 10) -> dict:
    """Capture the command and return a canned response."""
    _mock_send_cmd.last_action = action
    _mock_send_cmd.last_params = params
    return _mock_response.copy()


_mock_send_cmd.last_action = None
_mock_send_cmd.last_params = None


# ── Test setup ──────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _setup_env():
    """Set up environment for all tests."""
    global _mock_response
    _mock_response = {"status": "ok"}
    _mock_send_cmd.last_action = None
    _mock_send_cmd.last_params = None

    env = {
        "NEXUS_JWT_SECRET": _TEST_SECRET,
        "NEXUS_DATABASE_URL": "postgresql://test:test@localhost/test",
        "NEXUS_REDIS_URL": "redis://localhost:6379/0",
    }
    with patch.dict(os.environ, env):
        from app.config import clear_settings
        clear_settings()
        yield


_app_instance = None


def _get_app():
    """Return the FastAPI app singleton — always from web/backend/main.py."""
    global _app_instance
    if _app_instance is None:
        from main import app as _a
        _app_instance = _a
    return _app_instance


def _get_client():
    """Create a test client with mocked engine command sender."""
    from httpx import AsyncClient, ASGITransport
    app = _get_app()
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")


import asyncio


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ============================================================
# Dashboard Endpoints
# ============================================================

class TestDashboardAPI:

    def test_dashboard_summary_requires_auth(self):
        async def _test():
            async with _get_client() as client:
                resp = await client.get("/api/v1/dashboard/summary")
                assert resp.status_code == 401
        _run(_test())

    @patch("app.api.dashboard._send_engine_command", _mock_send_cmd)
    def test_dashboard_summary_dispatches_command(self):
        global _mock_response
        _mock_response = {
            "status": "ok",
            "portfolio": {"capital_usdt": 100000.0, "drawdown_pct": 2.5},
            "crash_defense": {"tier": "NORMAL"},
            "engine": {"state": "running", "trading_paused": False},
        }
        async def _test():
            async with _get_client() as client:
                resp = await client.get("/api/v1/dashboard/summary",
                                        headers=_auth_headers())
                assert resp.status_code == 200
                body = resp.json()
                assert body["status"] == "ok"
                assert body["portfolio"]["capital_usdt"] == 100000.0
                assert _mock_send_cmd.last_action == "get_dashboard"
        _run(_test())

    @patch("app.api.dashboard._send_engine_command", _mock_send_cmd)
    def test_crash_defense_endpoint(self):
        global _mock_response
        _mock_response = {
            "status": "ok",
            "crash_defense": {"tier": "DEFENSIVE", "is_defensive": True},
        }
        async def _test():
            async with _get_client() as client:
                resp = await client.get("/api/v1/dashboard/crash-defense",
                                        headers=_auth_headers())
                assert resp.status_code == 200
                assert resp.json()["crash_defense"]["tier"] == "DEFENSIVE"
                assert _mock_send_cmd.last_action == "get_crash_defense"
        _run(_test())


# ============================================================
# Scanner Endpoints
# ============================================================

class TestScannerAPI:

    def test_scanner_results_requires_auth(self):
        async def _test():
            async with _get_client() as client:
                resp = await client.get("/api/v1/scanner/results")
                assert resp.status_code == 401
        _run(_test())

    @patch("app.api.scanner._send_engine_command", _mock_send_cmd)
    def test_scanner_results(self):
        global _mock_response
        _mock_response = {
            "status": "ok",
            "results": [{"symbol": "BTC/USDT", "score": 0.72}],
            "count": 1,
        }
        async def _test():
            async with _get_client() as client:
                resp = await client.get("/api/v1/scanner/results",
                                        headers=_auth_headers())
                assert resp.status_code == 200
                assert resp.json()["count"] == 1
                assert _mock_send_cmd.last_action == "get_scanner_results"
        _run(_test())

    @patch("app.api.scanner._send_engine_command", _mock_send_cmd)
    def test_watchlist(self):
        global _mock_response
        _mock_response = {
            "status": "ok",
            "symbols": ["BTC/USDT", "ETH/USDT"],
            "weights": {"BTC/USDT": 1.0, "ETH/USDT": 1.2},
        }
        async def _test():
            async with _get_client() as client:
                resp = await client.get("/api/v1/scanner/watchlist",
                                        headers=_auth_headers())
                assert resp.status_code == 200
                assert len(resp.json()["symbols"]) == 2
                assert _mock_send_cmd.last_action == "get_watchlist"
        _run(_test())

    @patch("app.api.scanner._send_engine_command", _mock_send_cmd)
    def test_trigger_scan(self):
        global _mock_response
        _mock_response = {"status": "ok", "message": "Scan cycle triggered"}
        async def _test():
            async with _get_client() as client:
                resp = await client.post("/api/v1/scanner/trigger",
                                         headers=_auth_headers())
                assert resp.status_code == 200
                assert _mock_send_cmd.last_action == "trigger_scan"
        _run(_test())


# ============================================================
# Signals Endpoints
# ============================================================

class TestSignalsAPI:

    @patch("app.api.signals._send_engine_command", _mock_send_cmd)
    def test_agent_status(self):
        global _mock_response
        _mock_response = {
            "status": "ok",
            "agents": {"CrashDetectionAgent": {"running": True, "signal": 0.3}},
            "count": 1,
        }
        async def _test():
            async with _get_client() as client:
                resp = await client.get("/api/v1/signals/agents",
                                        headers=_auth_headers())
                assert resp.status_code == 200
                assert "CrashDetectionAgent" in resp.json()["agents"]
                assert _mock_send_cmd.last_action == "get_agent_status"
        _run(_test())

    @patch("app.api.signals._send_engine_command", _mock_send_cmd)
    def test_confluence_signals(self):
        global _mock_response
        _mock_response = {
            "status": "ok",
            "signals": [{"symbol": "SOL/USDT", "score": 0.65, "side": "buy"}],
            "count": 1,
        }
        async def _test():
            async with _get_client() as client:
                resp = await client.get("/api/v1/signals/confluence",
                                        headers=_auth_headers())
                assert resp.status_code == 200
                assert resp.json()["signals"][0]["score"] == 0.65
                assert _mock_send_cmd.last_action == "get_signals"
        _run(_test())


# ============================================================
# Risk Endpoints
# ============================================================

class TestRiskAPI:

    @patch("app.api.risk._send_engine_command", _mock_send_cmd)
    def test_risk_status(self):
        global _mock_response
        _mock_response = {
            "status": "ok",
            "risk": {
                "portfolio_heat_pct": 3.2,
                "drawdown_pct": 1.8,
                "crash_tier": "NORMAL",
                "circuit_breaker_on": False,
            },
        }
        async def _test():
            async with _get_client() as client:
                resp = await client.get("/api/v1/risk/status",
                                        headers=_auth_headers())
                assert resp.status_code == 200
                risk = resp.json()["risk"]
                assert risk["portfolio_heat_pct"] == 3.2
                assert risk["crash_tier"] == "NORMAL"
                assert _mock_send_cmd.last_action == "get_risk_status"
        _run(_test())


# ============================================================
# Trade History Endpoints
# ============================================================

class TestTradesAPI:

    @patch("app.api.trades._send_engine_command", _mock_send_cmd)
    def test_trade_history_default_pagination(self):
        global _mock_response
        _mock_response = {
            "status": "ok",
            "trades": [],
            "total": 0,
            "page": 1,
            "per_page": 50,
            "pages": 0,
        }
        async def _test():
            async with _get_client() as client:
                resp = await client.get("/api/v1/trades/history",
                                        headers=_auth_headers())
                assert resp.status_code == 200
                assert resp.json()["per_page"] == 50
                assert _mock_send_cmd.last_action == "get_trade_history"
                assert _mock_send_cmd.last_params["page"] == 1
                assert _mock_send_cmd.last_params["per_page"] == 50
        _run(_test())

    @patch("app.api.trades._send_engine_command", _mock_send_cmd)
    def test_trade_history_custom_pagination(self):
        global _mock_response
        _mock_response = {"status": "ok", "trades": [], "total": 0,
                          "page": 3, "per_page": 25, "pages": 0}
        async def _test():
            async with _get_client() as client:
                resp = await client.get("/api/v1/trades/history?page=3&per_page=25",
                                        headers=_auth_headers())
                assert resp.status_code == 200
                assert _mock_send_cmd.last_params["page"] == 3
                assert _mock_send_cmd.last_params["per_page"] == 25
        _run(_test())

    def test_trade_history_per_page_cap(self):
        """per_page > 200 should be rejected."""
        async def _test():
            async with _get_client() as client:
                resp = await client.get("/api/v1/trades/history?per_page=500",
                                        headers=_auth_headers())
                assert resp.status_code == 422  # validation error
        _run(_test())


# ============================================================
# System Endpoints
# ============================================================

class TestSystemAPI:

    @patch("app.api.system._send_engine_command", _mock_send_cmd)
    def test_system_health(self):
        global _mock_response
        _mock_response = {
            "status": "ok",
            "components": {
                "threads": {"count": 51, "warning": False},
                "scanner": {"running": True},
                "executor": {"initialized": True, "open_positions": 2},
            },
        }
        async def _test():
            async with _get_client() as client:
                resp = await client.get("/api/v1/system/health",
                                        headers=_auth_headers())
                assert resp.status_code == 200
                assert resp.json()["components"]["threads"]["count"] == 51
                assert _mock_send_cmd.last_action == "get_system_health"
        _run(_test())

    @patch("app.api.system._send_engine_command", _mock_send_cmd)
    def test_kill_switch(self):
        global _mock_response
        _mock_response = {
            "status": "ok",
            "actions": {"positions_closed": 3, "trading_paused": True,
                        "scanner_stopped": True},
        }
        async def _test():
            async with _get_client() as client:
                resp = await client.post("/api/v1/system/kill-switch",
                                         headers=_auth_headers())
                assert resp.status_code == 200
                assert resp.json()["actions"]["positions_closed"] == 3
                assert _mock_send_cmd.last_action == "kill_switch"
        _run(_test())


# ============================================================
# Settings Endpoints
# ============================================================

class TestSettingsAPI:

    @patch("app.api.settings_api._send_engine_command", _mock_send_cmd)
    def test_get_config_full(self):
        global _mock_response
        _mock_response = {"status": "ok", "config": {"rl": {"enabled": True}}}
        async def _test():
            async with _get_client() as client:
                resp = await client.get("/api/v1/settings/",
                                        headers=_auth_headers())
                assert resp.status_code == 200
                assert "config" in resp.json()
                assert _mock_send_cmd.last_action == "get_config"
        _run(_test())

    @patch("app.api.settings_api._send_engine_command", _mock_send_cmd)
    def test_get_config_section(self):
        global _mock_response
        _mock_response = {"status": "ok", "config": {"rl": {"enabled": True}}}
        async def _test():
            async with _get_client() as client:
                resp = await client.get("/api/v1/settings/?section=rl",
                                        headers=_auth_headers())
                assert resp.status_code == 200
                assert _mock_send_cmd.last_params == {"section": "rl"}
        _run(_test())

    @patch("app.api.settings_api._send_engine_command", _mock_send_cmd)
    def test_update_config(self):
        global _mock_response
        _mock_response = {"status": "ok", "updated_keys": ["risk_engine.risk_pct_per_trade"]}
        async def _test():
            async with _get_client() as client:
                resp = await client.patch("/api/v1/settings/",
                                          json={"updates": {"risk_engine.risk_pct_per_trade": 0.5}},
                                          headers=_auth_headers())
                assert resp.status_code == 200
                assert "risk_engine.risk_pct_per_trade" in resp.json()["updated_keys"]
                assert _mock_send_cmd.last_action == "update_config"
        _run(_test())


# ============================================================
# Engine Command Expansion
# ============================================================

class TestEngineCommandAllowlist:
    """Verify the engine /command endpoint accepts all new actions."""

    @patch("app.api.engine._send_engine_command", _mock_send_cmd)
    def test_new_actions_allowed(self):
        new_actions = [
            "get_dashboard", "get_crash_defense", "get_scanner_results",
            "get_watchlist", "get_agent_status", "get_signals",
            "get_risk_status", "get_trade_history", "update_config",
            "get_system_health", "trigger_scan", "kill_switch",
        ]
        async def _test():
            async with _get_client() as client:
                for action in new_actions:
                    resp = await client.post(
                        "/api/v1/engine/command",
                        json={"action": action, "params": {}},
                        headers=_auth_headers(),
                    )
                    assert resp.status_code == 200, \
                        f"Action '{action}' rejected: {resp.status_code} {resp.text}"

        _run(_test())

    def test_unknown_action_rejected(self):
        async def _test():
            async with _get_client() as client:
                resp = await client.post(
                    "/api/v1/engine/command",
                    json={"action": "delete_everything", "params": {}},
                    headers=_auth_headers(),
                )
                assert resp.status_code == 400
        _run(_test())


# ============================================================
# Engine Command Handler Tests (unit-level, mock core components)
# ============================================================

class TestEngineHandlers:
    """Test engine command handler logic directly."""

    def _make_engine(self):
        """Create a TradingEngineService with mock components."""
        sys.path.insert(0, os.path.join(_WEB_DIR, "engine"))
        from engine.main import TradingEngineService
        engine = TradingEngineService()
        engine._running = True
        engine._start_time = 1000000.0
        return engine

    def test_get_dashboard_no_executor(self):
        engine = self._make_engine()
        result = _run(engine._cmd_get_dashboard({}))
        assert result["status"] == "ok"
        assert result["portfolio"] == {}

    def test_get_dashboard_with_executor(self):
        engine = self._make_engine()
        mock_pe = MagicMock()
        mock_pe.get_production_status.return_value = {
            "capital_usdt": 100000.0, "drawdown_pct": 2.0,
            "open_positions": 1, "open_symbols": ["BTC/USDT"],
            "session_pnl_usdt": 500.0, "peak_capital_usdt": 102000.0,
            "total_return_pct": 5.0, "last_10_outcomes": ["W", "L", "W"],
            "current_losing_streak": 0,
        }
        mock_pe.get_stats.return_value = {
            "total_trades": 25, "win_rate": 55.0,
            "profit_factor": 1.8, "total_pnl_usdt": 5000.0,
            "avg_rr": 1.2,
        }
        engine._pe = mock_pe
        result = _run(engine._cmd_get_dashboard({}))
        assert result["status"] == "ok"
        assert result["portfolio"]["capital_usdt"] == 100000.0
        assert result["portfolio"]["total_trades"] == 25

    def test_get_trade_history_empty(self):
        engine = self._make_engine()
        mock_pe = MagicMock()
        mock_pe._closed_trades = []
        engine._pe = mock_pe
        result = _run(engine._cmd_get_trade_history({"page": 1, "per_page": 10}))
        assert result["status"] == "ok"
        assert result["total"] == 0
        assert result["trades"] == []

    def test_get_trade_history_pagination(self):
        engine = self._make_engine()
        mock_pe = MagicMock()
        mock_pe._closed_trades = [{"id": i} for i in range(25)]
        engine._pe = mock_pe
        result = _run(engine._cmd_get_trade_history({"page": 2, "per_page": 10}))
        assert result["total"] == 25
        assert len(result["trades"]) == 10
        assert result["page"] == 2
        assert result["pages"] == 3

    def test_kill_switch(self):
        engine = self._make_engine()
        mock_pe = MagicMock()
        mock_pe.close_all.return_value = 3
        engine._pe = mock_pe
        mock_scanner = MagicMock()
        mock_scanner._running = True
        engine._scanner = mock_scanner
        engine._redis_bridge = MagicMock()
        engine._redis_bridge.set_state = MagicMock()

        result = _run(engine._cmd_kill_switch({}))
        assert result["status"] == "ok"
        assert result["actions"]["positions_closed"] == 3
        assert result["actions"]["trading_paused"] is True
        assert result["actions"]["scanner_stopped"] is True
        mock_pe.close_all.assert_called_once()
        mock_scanner.stop.assert_called_once()
        assert engine._trading_paused is True

    def test_trigger_scan_not_running(self):
        engine = self._make_engine()
        mock_scanner = MagicMock()
        mock_scanner._running = False
        engine._scanner = mock_scanner
        result = _run(engine._cmd_trigger_scan({}))
        assert result["status"] == "error"
        assert "not running" in result["detail"]

    def test_update_config_no_settings(self):
        engine = self._make_engine()
        result = _run(engine._cmd_update_config({"updates": {"a": 1}}))
        assert result["status"] == "error"

    def test_get_watchlist_defaults(self):
        engine = self._make_engine()
        result = _run(engine._cmd_get_watchlist({}))
        assert result["status"] == "ok"
        assert "BTC/USDT" in result["symbols"]
        assert result["weights"]["SOL/USDT"] == 1.3

    def test_get_system_health(self):
        engine = self._make_engine()
        engine._pe = MagicMock()
        engine._pe.get_open_positions.return_value = [{"symbol": "BTC/USDT"}]
        result = _run(engine._cmd_get_system_health({}))
        assert result["status"] == "ok"
        assert result["components"]["executor"]["open_positions"] == 1
        assert result["components"]["engine"]["running"] is True


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
