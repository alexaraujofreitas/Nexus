# ============================================================
# Phase 6A Tests — Global Exception Handling & Request ID
#
# Validates:
#   - Global 500 handler returns sanitized JSON
#   - Debug mode includes traceback
#   - Validation errors return normalized 422
#   - X-Request-ID header present on all responses
#   - Healthcheck returns 200
# ============================================================
from __future__ import annotations

import pytest
from unittest.mock import patch

pytestmark = pytest.mark.unit


class TestGlobalExceptionHandler:
    """Test the global @app.exception_handler(Exception) in main.py."""

    @pytest.fixture
    def client(self):
        """Create test client with a route that raises."""
        import os
        os.environ.setdefault("NEXUS_DATABASE_URL", "sqlite+aiosqlite:///")
        os.environ.setdefault("NEXUS_REDIS_URL", "redis://localhost:6379/0")
        os.environ.setdefault("NEXUS_JWT_SECRET", "test-secret-key-32chars-long!!")

        from app.config import clear_settings
        clear_settings()

        from main import app
        from fastapi import APIRouter
        from fastapi.testclient import TestClient

        # Add a test route that raises
        _test_router = APIRouter()

        @_test_router.get("/test/raise-500")
        async def _raise():
            raise RuntimeError("deliberate test error")

        app.include_router(_test_router)
        return TestClient(app, raise_server_exceptions=False)

    def test_500_returns_json_with_request_id(self, client):
        """Unhandled exception → 500 JSON with request_id, no traceback in prod."""
        resp = client.get("/test/raise-500")
        assert resp.status_code == 500
        body = resp.json()
        assert body["detail"] == "Internal server error"
        assert "request_id" in body
        # In non-debug mode, no traceback
        assert "traceback" not in body

    def test_500_debug_includes_traceback(self, client):
        """In debug mode, 500 response includes traceback."""
        import os
        os.environ["NEXUS_DEBUG"] = "true"
        try:
            resp = client.get("/test/raise-500")
            assert resp.status_code == 500
            body = resp.json()
            assert "traceback" in body
            assert any("deliberate test error" in line for line in body["traceback"])
        finally:
            os.environ["NEXUS_DEBUG"] = "false"

    def test_request_id_header_on_normal_response(self, client):
        """Normal responses include X-Request-ID header."""
        resp = client.get("/")
        assert resp.status_code == 200
        assert "X-Request-ID" in resp.headers

    def test_request_id_header_on_error_response(self, client):
        """Error responses include X-Request-ID header."""
        resp = client.get("/test/raise-500")
        assert resp.status_code == 500
        assert "X-Request-ID" in resp.headers
        assert resp.json()["request_id"] == resp.headers["X-Request-ID"]


class TestValidationErrorHandler:
    """Test the @app.exception_handler(RequestValidationError)."""

    @pytest.fixture
    def client(self):
        import os
        os.environ.setdefault("NEXUS_DATABASE_URL", "sqlite+aiosqlite:///")
        os.environ.setdefault("NEXUS_REDIS_URL", "redis://localhost:6379/0")
        os.environ.setdefault("NEXUS_JWT_SECRET", "test-secret-key-32chars-long!!")

        from app.config import clear_settings
        clear_settings()

        from main import app
        from fastapi import APIRouter, Query
        from fastapi.testclient import TestClient

        _test_router2 = APIRouter()

        @_test_router2.get("/test/validate")
        async def _validate(count: int = Query(...)):
            return {"count": count}

        app.include_router(_test_router2)
        return TestClient(app, raise_server_exceptions=False)

    def test_422_returns_field_level_errors(self, client):
        """Missing required query param → 422 with field errors."""
        resp = client.get("/test/validate")
        assert resp.status_code == 422
        body = resp.json()
        assert body["detail"] == "Validation error"
        assert "errors" in body
        assert len(body["errors"]) >= 1
        assert "request_id" in body

    def test_422_includes_request_id_header(self, client):
        resp = client.get("/test/validate")
        assert "X-Request-ID" in resp.headers


class TestHealthcheck:
    """Healthcheck endpoints should still work."""

    @pytest.fixture
    def client(self):
        import os
        os.environ.setdefault("NEXUS_DATABASE_URL", "sqlite+aiosqlite:///")
        os.environ.setdefault("NEXUS_REDIS_URL", "redis://localhost:6379/0")
        os.environ.setdefault("NEXUS_JWT_SECRET", "test-secret-key-32chars-long!!")

        from app.config import clear_settings
        clear_settings()

        from main import app
        from fastapi.testclient import TestClient
        return TestClient(app)

    def test_health_returns_200(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200
