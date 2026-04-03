# ============================================================
# Phase 5 API Tests — Advanced Analytics + Backtest Cancel
# ============================================================
import pytest
from unittest.mock import AsyncMock, patch
from httpx import ASGITransport, AsyncClient

# Patch Qt shim before importing app
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from core_patch import install_qt_shim
install_qt_shim()

from main import app  # noqa: E402
from app.auth.dependencies import get_current_user  # noqa: E402


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def _set_auth_override():
    """Set auth override before each test, clean up after."""
    app.dependency_overrides[get_current_user] = lambda: {"id": 1, "email": "test@test.com"}
    yield
    app.dependency_overrides.pop(get_current_user, None)


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


# ── Phase 5 Analytics Endpoints ────────────────────────────

class TestDrawdownCurve:
    @pytest.mark.anyio
    async def test_drawdown_requires_auth(self, client):
        app.dependency_overrides.pop(get_current_user, None)
        try:
            r = await client.get("/api/v1/analytics/drawdown-curve")
            assert r.status_code in (401, 403)
        finally:
            app.dependency_overrides[get_current_user] = lambda: {"id": 1, "email": "test@test.com"}

    @pytest.mark.anyio
    @patch("app.api.analytics._send_engine_command", new_callable=AsyncMock)
    async def test_drawdown_dispatches(self, mock_cmd, client):
        mock_cmd.return_value = {"points": []}
        r = await client.get("/api/v1/analytics/drawdown-curve")
        assert r.status_code == 200
        mock_cmd.assert_called_once_with("get_drawdown_curve", {})


class TestRollingMetrics:
    @pytest.mark.anyio
    @patch("app.api.analytics._send_engine_command", new_callable=AsyncMock)
    async def test_rolling_default_window(self, mock_cmd, client):
        mock_cmd.return_value = {"points": [], "window": 20}
        r = await client.get("/api/v1/analytics/rolling-metrics")
        assert r.status_code == 200
        mock_cmd.assert_called_once_with("get_rolling_metrics", {"window": 20})

    @pytest.mark.anyio
    @patch("app.api.analytics._send_engine_command", new_callable=AsyncMock)
    async def test_rolling_custom_window(self, mock_cmd, client):
        mock_cmd.return_value = {"points": [], "window": 50}
        r = await client.get("/api/v1/analytics/rolling-metrics?window=50")
        assert r.status_code == 200
        mock_cmd.assert_called_once_with("get_rolling_metrics", {"window": 50})

    @pytest.mark.anyio
    async def test_rolling_window_validation(self, client):
        r = await client.get("/api/v1/analytics/rolling-metrics?window=1")
        assert r.status_code == 422  # Below ge=5

        r = await client.get("/api/v1/analytics/rolling-metrics?window=999")
        assert r.status_code == 422  # Above le=200


class TestRDistribution:
    @pytest.mark.anyio
    @patch("app.api.analytics._send_engine_command", new_callable=AsyncMock)
    async def test_r_distribution_dispatches(self, mock_cmd, client):
        mock_cmd.return_value = {"buckets": [], "expectancy": 0.3}
        r = await client.get("/api/v1/analytics/r-distribution")
        assert r.status_code == 200
        mock_cmd.assert_called_once_with("get_r_distribution", {})


class TestDurationAnalysis:
    @pytest.mark.anyio
    @patch("app.api.analytics._send_engine_command", new_callable=AsyncMock)
    async def test_duration_analysis_dispatches(self, mock_cmd, client):
        mock_cmd.return_value = {"buckets": []}
        r = await client.get("/api/v1/analytics/duration-analysis")
        assert r.status_code == 200
        mock_cmd.assert_called_once_with("get_duration_analysis", {})


class TestPerformanceByRegime:
    @pytest.mark.anyio
    @patch("app.api.analytics._send_engine_command", new_callable=AsyncMock)
    async def test_by_regime_dispatches(self, mock_cmd, client):
        mock_cmd.return_value = {"regimes": []}
        r = await client.get("/api/v1/analytics/by-regime")
        assert r.status_code == 200
        mock_cmd.assert_called_once_with("get_performance_by_regime", {})


class TestRegimeTransitions:
    @pytest.mark.anyio
    @patch("app.api.analytics._send_engine_command", new_callable=AsyncMock)
    async def test_regime_transitions_dispatches(self, mock_cmd, client):
        mock_cmd.return_value = {"transitions": []}
        r = await client.get("/api/v1/analytics/regime-transitions")
        assert r.status_code == 200
        mock_cmd.assert_called_once_with("get_regime_transitions", {})


class TestByModelEnhanced:
    @pytest.mark.anyio
    @patch("app.api.analytics._send_engine_command", new_callable=AsyncMock)
    async def test_by_model_with_sort_params(self, mock_cmd, client):
        mock_cmd.return_value = {"models": []}
        r = await client.get("/api/v1/analytics/by-model?sort=win_rate&order=asc")
        assert r.status_code == 200
        call_params = mock_cmd.call_args[0][1]
        assert call_params["sort"] == "win_rate"
        assert call_params["order"] == "asc"

    @pytest.mark.anyio
    @patch("app.api.analytics._send_engine_command", new_callable=AsyncMock)
    async def test_by_model_with_regime_filter(self, mock_cmd, client):
        mock_cmd.return_value = {"models": []}
        r = await client.get("/api/v1/analytics/by-model?regime=bull_trend")
        assert r.status_code == 200
        call_params = mock_cmd.call_args[0][1]
        assert call_params["regime"] == "bull_trend"


# ── Backtest Cancel Endpoint ───────────────────────────────

class TestBacktestCancel:
    @pytest.mark.anyio
    @patch("app.api.backtest._send_engine_command", new_callable=AsyncMock)
    async def test_cancel_dispatches(self, mock_cmd, client):
        mock_cmd.return_value = {"status": "cancelled"}
        r = await client.post("/api/v1/backtest/cancel/job-123")
        assert r.status_code == 200
        mock_cmd.assert_called_once_with("cancel_backtest", {"job_id": "job-123"})


# ── Phase 5 Engine Allowed Actions ──────────────────────────

class TestPhase5AllowedActions:
    @pytest.mark.anyio
    @patch("app.api.engine._send_engine_command", new_callable=AsyncMock)
    async def test_phase5_actions_allowed(self, mock_cmd, client):
        mock_cmd.return_value = {"status": "ok"}
        new_actions = [
            "get_drawdown_curve", "get_rolling_metrics", "get_r_distribution",
            "get_duration_analysis", "get_performance_by_regime",
            "get_regime_transitions", "cancel_backtest",
        ]
        for action in new_actions:
            r = await client.post("/api/v1/engine/command", json={"action": action})
            assert r.status_code == 200, f"{action} should be allowed, got {r.status_code}"


# ── Phase 5 Route Registration ──────────────────────────────

class TestPhase5Routes:
    @pytest.mark.anyio
    @patch("app.api.analytics._send_engine_command", new_callable=AsyncMock)
    async def test_all_new_analytics_routes(self, mock_cmd, client):
        mock_cmd.return_value = {}
        routes = [
            "/api/v1/analytics/drawdown-curve",
            "/api/v1/analytics/rolling-metrics",
            "/api/v1/analytics/r-distribution",
            "/api/v1/analytics/duration-analysis",
            "/api/v1/analytics/by-regime",
            "/api/v1/analytics/regime-transitions",
        ]
        for route in routes:
            r = await client.get(route)
            assert r.status_code == 200, f"Route {route} failed with {r.status_code}"
