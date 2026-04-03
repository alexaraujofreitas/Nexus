# ============================================================
# Phase 3 — New API Endpoint Tests
#
# Tests the Phase 3 API additions:
#   - GET  /charts/ohlcv         — OHLCV candlestick data
#   - GET  /trading/positions    — open positions
#   - POST /trading/close        — close single position
#   - POST /trading/close-all    — close all positions
#
# Verifies:
#   - Route registration and auth requirement
#   - Request parameter validation
#   - Engine command dispatch (correct action + params)
#   - Response pass-through from engine
# ============================================================
from __future__ import annotations

import asyncio
import json
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ── Path setup ──────────────────────────────────────────────
_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_WEB_DIR = os.path.dirname(_BACKEND)
_PROJECT_ROOT = os.path.dirname(_WEB_DIR)

if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)
for p in [_WEB_DIR, _PROJECT_ROOT]:
    if p not in sys.path:
        sys.path.append(p)

from core_patch import install_qt_shim
install_qt_shim()

# Ensure backend main.py is resolved (not desktop main.py)
if "main" in sys.modules:
    _cached = sys.modules.pop("main")
    if hasattr(_cached, "app"):
        sys.modules["main"] = _cached

_backend_idx = sys.path.index(_BACKEND) if _BACKEND in sys.path else -1
if _backend_idx != 0:
    if _BACKEND in sys.path:
        sys.path.remove(_BACKEND)
    sys.path.insert(0, _BACKEND)


# ── Shared fixtures ─────────────────────────────────────────

_TEST_SECRET = "test-secret-32-chars-long-enough!!"


def _make_token():
    from app.auth.jwt import create_access_token
    return create_access_token({"sub": "1", "email": "test@nexustest.com"})


def _auth_headers():
    return {"Authorization": f"Bearer {_make_token()}"}


# ── Mock engine command ─────────────────────────────────────

_mock_response = {}


async def _mock_send_cmd(action: str, params: dict, timeout: int = 10) -> dict:
    _mock_send_cmd.last_action = action
    _mock_send_cmd.last_params = params
    return _mock_response.copy()


_mock_send_cmd.last_action = None
_mock_send_cmd.last_params = None


@pytest.fixture(autouse=True)
def _setup_env():
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
    global _app_instance
    if _app_instance is None:
        from main import app as _a
        _app_instance = _a
    return _app_instance


def _get_client():
    from httpx import AsyncClient, ASGITransport
    app = _get_app()
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ============================================================
# Charts Endpoints
# ============================================================

class TestChartsAPI:
    """Tests for GET /api/v1/charts/ohlcv."""

    def test_ohlcv_requires_auth(self):
        async def _test():
            async with _get_client() as client:
                resp = await client.get("/api/v1/charts/ohlcv")
                assert resp.status_code == 401
        _run(_test())

    @patch("app.api.charts._send_engine_command", _mock_send_cmd)
    def test_ohlcv_default_params(self):
        """GET /charts/ohlcv with no params uses defaults."""
        global _mock_response
        _mock_response = {
            "status": "ok",
            "bars": [{"time": 1700000000, "open": 50000, "high": 50100, "low": 49900, "close": 50050, "volume": 100}],
            "symbol": "BTC/USDT",
            "timeframe": "30m",
        }
        async def _test():
            async with _get_client() as client:
                resp = await client.get("/api/v1/charts/ohlcv", headers=_auth_headers())
                assert resp.status_code == 200
                assert _mock_send_cmd.last_action == "get_ohlcv"
                assert _mock_send_cmd.last_params["symbol"] == "BTC/USDT"
                assert _mock_send_cmd.last_params["timeframe"] == "30m"
                assert _mock_send_cmd.last_params["limit"] == 300
        _run(_test())

    @patch("app.api.charts._send_engine_command", _mock_send_cmd)
    def test_ohlcv_custom_params(self):
        """GET /charts/ohlcv with custom symbol, timeframe, limit."""
        global _mock_response
        _mock_response = {"status": "ok", "bars": [], "symbol": "SOL/USDT", "timeframe": "4h"}
        async def _test():
            async with _get_client() as client:
                resp = await client.get(
                    "/api/v1/charts/ohlcv?symbol=SOL/USDT&timeframe=4h&limit=100",
                    headers=_auth_headers(),
                )
                assert resp.status_code == 200
                assert _mock_send_cmd.last_params["symbol"] == "SOL/USDT"
                assert _mock_send_cmd.last_params["timeframe"] == "4h"
                assert _mock_send_cmd.last_params["limit"] == 100
        _run(_test())

    @patch("app.api.charts._send_engine_command", _mock_send_cmd)
    def test_ohlcv_limit_validation(self):
        """Limit below 10 should fail validation."""
        async def _test():
            async with _get_client() as client:
                resp = await client.get(
                    "/api/v1/charts/ohlcv?limit=5",
                    headers=_auth_headers(),
                )
                assert resp.status_code == 422  # validation error
        _run(_test())

    @patch("app.api.charts._send_engine_command", _mock_send_cmd)
    def test_ohlcv_limit_max_validation(self):
        """Limit above 1000 should fail validation."""
        async def _test():
            async with _get_client() as client:
                resp = await client.get(
                    "/api/v1/charts/ohlcv?limit=2000",
                    headers=_auth_headers(),
                )
                assert resp.status_code == 422
        _run(_test())

    @patch("app.api.charts._send_engine_command", _mock_send_cmd)
    def test_ohlcv_response_passthrough(self):
        """Response from engine is passed through to client."""
        global _mock_response
        bars = [
            {"time": 1700000000 + i * 1800, "open": 50000 + i, "high": 50100, "low": 49900, "close": 50050, "volume": 100}
            for i in range(5)
        ]
        _mock_response = {"status": "ok", "bars": bars, "symbol": "BTC/USDT", "timeframe": "30m"}
        async def _test():
            async with _get_client() as client:
                resp = await client.get("/api/v1/charts/ohlcv", headers=_auth_headers())
                data = resp.json()
                assert len(data["bars"]) == 5
                assert data["symbol"] == "BTC/USDT"
        _run(_test())


# ============================================================
# Trading — Positions Endpoint
# ============================================================

class TestTradingPositionsAPI:
    """Tests for GET /api/v1/trading/positions."""

    def test_positions_requires_auth(self):
        async def _test():
            async with _get_client() as client:
                resp = await client.get("/api/v1/trading/positions")
                assert resp.status_code == 401
        _run(_test())

    @patch("app.api.trading._send_engine_command", _mock_send_cmd)
    def test_positions_dispatches_command(self):
        global _mock_response
        _mock_response = {
            "status": "ok",
            "positions": [
                {"symbol": "BTC/USDT", "side": "buy", "entry_price": 50000, "size_usdt": 500, "unrealized_pnl": 25.5},
            ],
            "count": 1,
        }
        async def _test():
            async with _get_client() as client:
                resp = await client.get("/api/v1/trading/positions", headers=_auth_headers())
                assert resp.status_code == 200
                assert _mock_send_cmd.last_action == "get_positions"
                assert _mock_send_cmd.last_params == {}
                data = resp.json()
                assert data["count"] == 1
                assert data["positions"][0]["symbol"] == "BTC/USDT"
        _run(_test())

    @patch("app.api.trading._send_engine_command", _mock_send_cmd)
    def test_positions_empty(self):
        global _mock_response
        _mock_response = {"status": "ok", "positions": [], "count": 0}
        async def _test():
            async with _get_client() as client:
                resp = await client.get("/api/v1/trading/positions", headers=_auth_headers())
                assert resp.status_code == 200
                data = resp.json()
                assert data["count"] == 0
                assert data["positions"] == []
        _run(_test())


# ============================================================
# Trading — Close Position Endpoint
# ============================================================

class TestTradingCloseAPI:
    """Tests for POST /api/v1/trading/close."""

    def test_close_requires_auth(self):
        async def _test():
            async with _get_client() as client:
                resp = await client.post("/api/v1/trading/close", json={"symbol": "BTC/USDT"})
                assert resp.status_code == 401
        _run(_test())

    @patch("app.api.trading._send_engine_command", _mock_send_cmd)
    def test_close_dispatches_command(self):
        global _mock_response
        _mock_response = {"status": "ok", "symbol": "BTC/USDT", "message": "Position closed"}
        async def _test():
            async with _get_client() as client:
                resp = await client.post(
                    "/api/v1/trading/close",
                    json={"symbol": "BTC/USDT"},
                    headers=_auth_headers(),
                )
                assert resp.status_code == 200
                assert _mock_send_cmd.last_action == "close_position"
                assert _mock_send_cmd.last_params == {"symbol": "BTC/USDT"}
        _run(_test())

    @patch("app.api.trading._send_engine_command", _mock_send_cmd)
    def test_close_missing_symbol(self):
        """POST /trading/close without symbol should fail validation."""
        async def _test():
            async with _get_client() as client:
                resp = await client.post(
                    "/api/v1/trading/close",
                    json={},
                    headers=_auth_headers(),
                )
                assert resp.status_code == 422
        _run(_test())


# ============================================================
# Trading — Close All Endpoint
# ============================================================

class TestTradingCloseAllAPI:
    """Tests for POST /api/v1/trading/close-all."""

    def test_close_all_requires_auth(self):
        async def _test():
            async with _get_client() as client:
                resp = await client.post("/api/v1/trading/close-all")
                assert resp.status_code == 401
        _run(_test())

    @patch("app.api.trading._send_engine_command", _mock_send_cmd)
    def test_close_all_dispatches_command(self):
        global _mock_response
        _mock_response = {"status": "ok", "closed_count": 3, "message": "All positions closed"}
        async def _test():
            async with _get_client() as client:
                resp = await client.post(
                    "/api/v1/trading/close-all",
                    headers=_auth_headers(),
                )
                assert resp.status_code == 200
                assert _mock_send_cmd.last_action == "close_all_positions"
                assert _mock_send_cmd.last_params == {}
                data = resp.json()
                assert data["closed_count"] == 3
        _run(_test())


# ============================================================
# Engine ALLOWED_ACTIONS includes Phase 3
# ============================================================

class TestEngineAllowedActions:
    """Verify get_ohlcv is in ALLOWED_ACTIONS."""

    @patch("app.api.engine._send_engine_command", _mock_send_cmd)
    def test_get_ohlcv_allowed(self):
        global _mock_response
        _mock_response = {"status": "ok", "bars": []}
        async def _test():
            async with _get_client() as client:
                resp = await client.post(
                    "/api/v1/engine/command",
                    json={"action": "get_ohlcv", "params": {"symbol": "BTC/USDT"}},
                    headers=_auth_headers(),
                )
                assert resp.status_code == 200
                assert _mock_send_cmd.last_action == "get_ohlcv"
        _run(_test())

    def test_invalid_action_rejected(self):
        async def _test():
            async with _get_client() as client:
                resp = await client.post(
                    "/api/v1/engine/command",
                    json={"action": "drop_database", "params": {}},
                    headers=_auth_headers(),
                )
                assert resp.status_code == 400
        _run(_test())


# ============================================================
# Route Registration
# ============================================================

class TestRouteRegistration:
    """Verify Phase 3 routes are registered in the app."""

    def test_charts_route_registered(self):
        app = _get_app()
        routes = [r.path for r in app.routes if hasattr(r, "path")]
        assert "/api/v1/charts/ohlcv" in routes

    def test_trading_positions_route_registered(self):
        app = _get_app()
        routes = [r.path for r in app.routes if hasattr(r, "path")]
        assert "/api/v1/trading/positions" in routes

    def test_trading_close_route_registered(self):
        app = _get_app()
        routes = [r.path for r in app.routes if hasattr(r, "path")]
        assert "/api/v1/trading/close" in routes

    def test_trading_close_all_route_registered(self):
        app = _get_app()
        routes = [r.path for r in app.routes if hasattr(r, "path")]
        assert "/api/v1/trading/close-all" in routes
