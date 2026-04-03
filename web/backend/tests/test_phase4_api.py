# ============================================================
# Phase 4 — New API Endpoint Tests
#
# Tests Phase 4 API additions:
#   - GET  /logs/recent              — log entries
#   - GET  /analytics/equity-curve   — capital time-series
#   - GET  /analytics/metrics        — aggregate metrics
#   - GET  /analytics/trade-distribution — PnL histogram
#   - GET  /analytics/by-model       — per-model breakdown
#   - POST /backtest/start           — launch backtest
#   - GET  /backtest/status/{id}     — poll progress
#   - GET  /backtest/results/{id}    — get results
#   - GET  /validation/health        — health report
#   - GET  /validation/readiness     — readiness assessment
#   - GET  /validation/data-integrity — data integrity checks
# ============================================================
from __future__ import annotations

import asyncio
import os
import sys
from unittest.mock import patch

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

if "main" in sys.modules:
    _cached = sys.modules.pop("main")
    if hasattr(_cached, "app"):
        sys.modules["main"] = _cached

_backend_idx = sys.path.index(_BACKEND) if _BACKEND in sys.path else -1
if _backend_idx != 0:
    if _BACKEND in sys.path:
        sys.path.remove(_BACKEND)
    sys.path.insert(0, _BACKEND)


_TEST_SECRET = "test-secret-32-chars-long-enough!!"


def _make_token():
    from app.auth.jwt import create_access_token
    return create_access_token({"sub": "1", "email": "test@nexustest.com"})


def _auth_headers():
    return {"Authorization": f"Bearer {_make_token()}"}


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
# Logs Endpoints
# ============================================================

class TestLogsAPI:

    def test_logs_requires_auth(self):
        async def _test():
            async with _get_client() as client:
                resp = await client.get("/api/v1/logs/recent")
                assert resp.status_code == 401
        _run(_test())

    @patch("app.api.logs._send_engine_command", _mock_send_cmd)
    def test_logs_default_params(self):
        global _mock_response
        _mock_response = {"status": "ok", "entries": [], "count": 0}
        async def _test():
            async with _get_client() as client:
                resp = await client.get("/api/v1/logs/recent", headers=_auth_headers())
                assert resp.status_code == 200
                assert _mock_send_cmd.last_action == "get_logs"
                assert _mock_send_cmd.last_params["limit"] == 200
        _run(_test())

    @patch("app.api.logs._send_engine_command", _mock_send_cmd)
    def test_logs_with_filters(self):
        global _mock_response
        _mock_response = {"status": "ok", "entries": [], "count": 0}
        async def _test():
            async with _get_client() as client:
                resp = await client.get(
                    "/api/v1/logs/recent?limit=50&level=ERROR&component=scanner&search=failed",
                    headers=_auth_headers(),
                )
                assert resp.status_code == 200
                assert _mock_send_cmd.last_params["level"] == "ERROR"
                assert _mock_send_cmd.last_params["component"] == "scanner"
                assert _mock_send_cmd.last_params["search"] == "failed"
                assert _mock_send_cmd.last_params["limit"] == 50
        _run(_test())


# ============================================================
# Analytics Endpoints
# ============================================================

class TestAnalyticsAPI:

    def test_equity_curve_requires_auth(self):
        async def _test():
            async with _get_client() as client:
                resp = await client.get("/api/v1/analytics/equity-curve")
                assert resp.status_code == 401
        _run(_test())

    @patch("app.api.analytics._send_engine_command", _mock_send_cmd)
    def test_equity_curve_dispatches(self):
        global _mock_response
        _mock_response = {"status": "ok", "points": [{"time": 1700000000, "capital": 100000}], "initial_capital": 100000}
        async def _test():
            async with _get_client() as client:
                resp = await client.get("/api/v1/analytics/equity-curve", headers=_auth_headers())
                assert resp.status_code == 200
                assert _mock_send_cmd.last_action == "get_equity_curve"
        _run(_test())

    @patch("app.api.analytics._send_engine_command", _mock_send_cmd)
    def test_metrics_dispatches(self):
        global _mock_response
        _mock_response = {"status": "ok", "total_trades": 50, "win_rate": 55.0, "profit_factor": 1.45}
        async def _test():
            async with _get_client() as client:
                resp = await client.get("/api/v1/analytics/metrics", headers=_auth_headers())
                assert resp.status_code == 200
                assert _mock_send_cmd.last_action == "get_performance_metrics"
        _run(_test())

    @patch("app.api.analytics._send_engine_command", _mock_send_cmd)
    def test_trade_distribution_dispatches(self):
        global _mock_response
        _mock_response = {"status": "ok", "buckets": [], "mean": 0, "median": 0, "std": 0}
        async def _test():
            async with _get_client() as client:
                resp = await client.get("/api/v1/analytics/trade-distribution", headers=_auth_headers())
                assert resp.status_code == 200
                assert _mock_send_cmd.last_action == "get_trade_distribution"
        _run(_test())

    @patch("app.api.analytics._send_engine_command", _mock_send_cmd)
    def test_by_model_dispatches(self):
        global _mock_response
        _mock_response = {"status": "ok", "models": []}
        async def _test():
            async with _get_client() as client:
                resp = await client.get("/api/v1/analytics/by-model", headers=_auth_headers())
                assert resp.status_code == 200
                assert _mock_send_cmd.last_action == "get_performance_by_model"
        _run(_test())


# ============================================================
# Backtest Endpoints
# ============================================================

class TestBacktestAPI:

    def test_start_requires_auth(self):
        async def _test():
            async with _get_client() as client:
                resp = await client.post("/api/v1/backtest/start", json={})
                assert resp.status_code == 401
        _run(_test())

    @patch("app.api.backtest._send_engine_command", _mock_send_cmd)
    def test_start_dispatches(self):
        global _mock_response
        _mock_response = {"status": "ok", "job_id": "abc123", "status": "running"}
        async def _test():
            async with _get_client() as client:
                resp = await client.post(
                    "/api/v1/backtest/start",
                    json={"symbols": ["BTC/USDT"], "start_date": "2024-01-01", "end_date": "2025-01-01", "timeframe": "1h", "fee_pct": 0.04},
                    headers=_auth_headers(),
                )
                assert resp.status_code == 200
                assert _mock_send_cmd.last_action == "start_backtest"
                assert _mock_send_cmd.last_params["symbols"] == ["BTC/USDT"]
                assert _mock_send_cmd.last_params["fee_pct"] == 0.04
        _run(_test())

    @patch("app.api.backtest._send_engine_command", _mock_send_cmd)
    def test_status_dispatches(self):
        global _mock_response
        _mock_response = {"status": "ok", "job_id": "abc123", "progress_pct": 50}
        async def _test():
            async with _get_client() as client:
                resp = await client.get("/api/v1/backtest/status/abc123", headers=_auth_headers())
                assert resp.status_code == 200
                assert _mock_send_cmd.last_action == "get_backtest_status"
                assert _mock_send_cmd.last_params["job_id"] == "abc123"
        _run(_test())

    @patch("app.api.backtest._send_engine_command", _mock_send_cmd)
    def test_results_dispatches(self):
        global _mock_response
        _mock_response = {"status": "ok", "job_id": "abc123", "metrics": {"pf": 1.5, "wr": 55}}
        async def _test():
            async with _get_client() as client:
                resp = await client.get("/api/v1/backtest/results/abc123", headers=_auth_headers())
                assert resp.status_code == 200
                assert _mock_send_cmd.last_action == "get_backtest_results"
        _run(_test())


# ============================================================
# Validation Endpoints
# ============================================================

class TestValidationAPI:

    def test_health_requires_auth(self):
        async def _test():
            async with _get_client() as client:
                resp = await client.get("/api/v1/validation/health")
                assert resp.status_code == 401
        _run(_test())

    @patch("app.api.validation._send_engine_command", _mock_send_cmd)
    def test_health_dispatches(self):
        global _mock_response
        _mock_response = {"status": "ok", "components": {"exchange": "ok", "database": "ok"}}
        async def _test():
            async with _get_client() as client:
                resp = await client.get("/api/v1/validation/health", headers=_auth_headers())
                assert resp.status_code == 200
                assert _mock_send_cmd.last_action == "get_validation_health"
        _run(_test())

    @patch("app.api.validation._send_engine_command", _mock_send_cmd)
    def test_readiness_dispatches(self):
        global _mock_response
        _mock_response = {"status": "ok", "verdict": "STILL_LEARNING", "score": 25, "checks": []}
        async def _test():
            async with _get_client() as client:
                resp = await client.get("/api/v1/validation/readiness", headers=_auth_headers())
                assert resp.status_code == 200
                assert _mock_send_cmd.last_action == "get_readiness"
        _run(_test())

    @patch("app.api.validation._send_engine_command", _mock_send_cmd)
    def test_data_integrity_dispatches(self):
        global _mock_response
        _mock_response = {"status": "ok", "passed": True, "checks": []}
        async def _test():
            async with _get_client() as client:
                resp = await client.get("/api/v1/validation/data-integrity", headers=_auth_headers())
                assert resp.status_code == 200
                assert _mock_send_cmd.last_action == "get_data_integrity"
        _run(_test())


# ============================================================
# Route Registration
# ============================================================

class TestPhase4RouteRegistration:

    def test_all_phase4_routes_registered(self):
        app = _get_app()
        routes = [r.path for r in app.routes if hasattr(r, "path")]
        expected = [
            "/api/v1/logs/recent",
            "/api/v1/analytics/equity-curve",
            "/api/v1/analytics/metrics",
            "/api/v1/analytics/trade-distribution",
            "/api/v1/analytics/by-model",
            "/api/v1/backtest/start",
            "/api/v1/backtest/status/{job_id}",
            "/api/v1/backtest/results/{job_id}",
            "/api/v1/validation/health",
            "/api/v1/validation/readiness",
            "/api/v1/validation/data-integrity",
        ]
        for path in expected:
            assert path in routes, f"Route {path} not registered"


# ============================================================
# Engine ALLOWED_ACTIONS includes Phase 4
# ============================================================

class TestPhase4AllowedActions:

    @patch("app.api.engine._send_engine_command", _mock_send_cmd)
    def test_phase4_actions_allowed(self):
        global _mock_response
        _mock_response = {"status": "ok"}
        phase4_actions = [
            "get_logs", "get_equity_curve", "get_performance_metrics",
            "get_trade_distribution", "get_performance_by_model",
            "start_backtest", "get_backtest_status", "get_backtest_results",
            "get_validation_health", "get_readiness", "get_data_integrity",
        ]
        async def _test():
            async with _get_client() as client:
                for action in phase4_actions:
                    resp = await client.post(
                        "/api/v1/engine/command",
                        json={"action": action, "params": {}},
                        headers=_auth_headers(),
                    )
                    assert resp.status_code == 200, f"Action {action} rejected"
        _run(_test())
