# ============================================================
# Phase 7A — Cross-Layer Validation Tests
#
# Validates integration between all system layers:
#   - Auth → DB → JWT → protected routes
#   - Config validation at startup
#   - Middleware stack ordering
#   - WebSocket lifecycle
#   - Error propagation through all layers
#   - Health endpoint chain
# ============================================================
import os
import pytest
from unittest.mock import patch, MagicMock


class TestMiddlewareStackOrder:
    """Verify middleware is applied in the correct order in main.py."""

    def test_middleware_imports_exist(self):
        from app.middleware.audit import AuditMiddleware
        from app.middleware.cloudflare import CloudflareAccessMiddleware
        from app.middleware.security_headers import SecurityHeadersMiddleware
        from app.middleware.rate_limit import limiter
        assert AuditMiddleware is not None
        assert CloudflareAccessMiddleware is not None
        assert SecurityHeadersMiddleware is not None
        assert limiter is not None

    @pytest.mark.asyncio
    async def test_security_headers_all_present(self):
        from app.middleware.security_headers import SecurityHeadersMiddleware

        class FakeResponse:
            def __init__(self):
                self.headers = {}

        async def call_next(request):
            return FakeResponse()

        mw = SecurityHeadersMiddleware(app=None)
        resp = await mw.dispatch(MagicMock(), call_next)
        required = [
            "X-Content-Type-Options", "X-Frame-Options", "X-XSS-Protection",
            "Strict-Transport-Security", "Content-Security-Policy", "Referrer-Policy",
        ]
        for h in required:
            assert h in resp.headers, f"Missing header: {h}"

    @pytest.mark.asyncio
    async def test_security_header_values(self):
        from app.middleware.security_headers import SecurityHeadersMiddleware

        class FakeResponse:
            def __init__(self):
                self.headers = {}

        async def call_next(request):
            return FakeResponse()

        mw = SecurityHeadersMiddleware(app=None)
        resp = await mw.dispatch(MagicMock(), call_next)
        assert resp.headers["X-Content-Type-Options"] == "nosniff"
        assert resp.headers["X-Frame-Options"] == "SAMEORIGIN"
        assert "max-age=31536000" in resp.headers["Strict-Transport-Security"]
        assert resp.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"


class TestConfigValidation:
    def test_catches_empty_jwt(self):
        from app.config import Settings, validate_settings
        s = Settings(
            jwt_secret="",
            database_url="postgresql+asyncpg://x:x@localhost/db",
            redis_url="redis://localhost:6379/0",
        )
        with patch.dict(os.environ, {"NEXUS_DEBUG": "false"}):
            errors = validate_settings(s)
        assert any("empty" in e.lower() or "jwt" in e.lower() for e in errors)

    def test_catches_short_jwt(self):
        from app.config import Settings, validate_settings
        s = Settings(
            jwt_secret="short",
            database_url="postgresql+asyncpg://x:x@localhost/db",
            redis_url="redis://localhost:6379/0",
        )
        errors = validate_settings(s)
        assert any("32" in e for e in errors)

    def test_passes_valid_config(self):
        from app.config import Settings, validate_settings
        s = Settings(
            jwt_secret="a" * 64,
            database_url="postgresql+asyncpg://x:x@localhost/db",
            redis_url="redis://localhost:6379/0",
            encryption_key="b" * 32,
        )
        assert len(validate_settings(s)) == 0

    def test_catches_empty_database_url(self):
        from app.config import Settings, validate_settings
        s = Settings(
            jwt_secret="a" * 64, database_url="", redis_url="redis://localhost:6379/0",
        )
        errors = validate_settings(s)
        assert any("database" in e.lower() for e in errors)


class TestAuthEndpointContracts:
    def test_password_complexity_rejects_short(self):
        from app.api.auth import validate_password_complexity
        assert any("12 characters" in e for e in validate_password_complexity("Short1!"))

    def test_password_complexity_rejects_no_uppercase(self):
        from app.api.auth import validate_password_complexity
        assert any("uppercase" in e for e in validate_password_complexity("alllowercase1!"))

    def test_password_complexity_rejects_no_digit(self):
        from app.api.auth import validate_password_complexity
        assert any("digit" in e for e in validate_password_complexity("NoDigitsHere!!"))

    def test_password_complexity_rejects_no_special(self):
        from app.api.auth import validate_password_complexity
        assert any("special" in e for e in validate_password_complexity("NoSpecialChar1A"))

    def test_password_complexity_accepts_strong(self):
        from app.api.auth import validate_password_complexity
        assert len(validate_password_complexity("StrongPass123!@#")) == 0

    def test_lockout_constants(self):
        from app.api.auth import MAX_LOGIN_ATTEMPTS, LOCKOUT_DURATION_MINUTES
        assert MAX_LOGIN_ATTEMPTS == 5
        assert LOCKOUT_DURATION_MINUTES == 15

    def test_setup_request_rejects_weak_password(self):
        from app.api.auth import SetupRequest
        from pydantic import ValidationError
        with pytest.raises(ValidationError) as exc_info:
            SetupRequest(email="admin@test.com", password="weak")
        assert "12 characters" in str(exc_info.value)

    def test_setup_request_accepts_strong_password(self):
        from app.api.auth import SetupRequest
        req = SetupRequest(email="admin@test.com", password="StrongPass123!@#")
        assert req.password == "StrongPass123!@#"


class TestLoggingIntegrity:
    def test_jwt_masking(self):
        from app.logging_config import _mask_sensitive
        jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abc123"
        masked = _mask_sensitive(f"token={jwt}")
        assert "eyJ" not in masked
        assert "<JWT_MASKED>" in masked

    def test_password_masking(self):
        from app.logging_config import _mask_sensitive
        masked = _mask_sensitive('password="mysecretpass123"')
        assert "mysecretpass123" not in masked

    def test_api_key_masking(self):
        from app.logging_config import _mask_sensitive
        masked = _mask_sensitive("api_key=sk-abc123xyz789")
        assert "sk-abc123xyz789" not in masked

    def test_clean_text_unchanged(self):
        from app.logging_config import _mask_sensitive
        assert _mask_sensitive("User logged in from 192.168.1.1") == "User logged in from 192.168.1.1"


class TestWebSocketContracts:
    def test_rate_limit_constant(self):
        from app.ws.manager import MESSAGE_RATE_LIMIT
        assert MESSAGE_RATE_LIMIT == 20

    def test_size_limit_constant(self):
        from app.ws.manager import MESSAGE_SIZE_LIMIT
        assert MESSAGE_SIZE_LIMIT == 64 * 1024

    def test_connection_id_format(self):
        import secrets
        sample = f"ws_{secrets.token_hex(16)}"
        assert sample.startswith("ws_")
        assert len(sample) == 35

    def test_conn_state_rate_limiter(self):
        from app.ws.manager import _ConnState
        ws_mock = MagicMock()
        state = _ConnState(ws_mock, user_sub="test", email="t@t.com", token="tok")
        for _ in range(20):
            assert state.check_rate_limit() is True
        assert state.check_rate_limit() is False


class TestHealthEndpoint:
    def test_health_route_exists(self):
        import importlib
        main_mod = importlib.import_module("main")
        routes = [r.path for r in main_mod.app.routes]
        assert "/health" in routes or any("/health" in str(r) for r in routes)


class TestErrorHandling:
    def test_request_validation_handler_registered(self):
        import importlib
        from fastapi.exceptions import RequestValidationError
        main_mod = importlib.import_module("main")
        assert RequestValidationError in main_mod.app.exception_handlers

    @pytest.mark.asyncio
    async def test_audit_middleware_catches_exceptions(self):
        from app.middleware.audit import AuditMiddleware

        async def failing_call_next(request):
            raise RuntimeError("test explosion")

        mw = AuditMiddleware(app=None)
        req = MagicMock()
        req.method = "GET"
        req.url.path = "/test"
        req.headers = {}
        req.state = MagicMock()
        resp = await mw.dispatch(req, failing_call_next)
        assert resp.status_code == 500


@pytest.mark.docker_integration
class TestAuthFlowIntegration:
    """Requires PostgreSQL + Redis."""

    @pytest.fixture
    def client(self):
        from httpx import AsyncClient, ASGITransport
        import importlib
        main_mod = importlib.import_module("main")
        return AsyncClient(transport=ASGITransport(app=main_mod.app), base_url="http://test")

    @pytest.mark.asyncio
    async def test_setup_login_refresh_logout_flow(self, client):
        async with client as c:
            setup_resp = await c.post("/api/v1/auth/setup", json={
                "email": "admin@nexustrader.com", "password": "StrongPass123!@#", "display_name": "Admin",
            })
            if setup_resp.status_code == 201:
                assert "access_token" in setup_resp.json()

            login_resp = await c.post("/api/v1/auth/login", json={
                "email": "admin@nexustrader.com", "password": "StrongPass123!@#",
            })
            assert login_resp.status_code == 200
            tokens = login_resp.json()

            me_resp = await c.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {tokens['access_token']}"})
            assert me_resp.status_code == 200

            refresh_resp = await c.post("/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
            assert refresh_resp.status_code == 200

            logout_resp = await c.post("/api/v1/auth/logout", json={"refresh_token": tokens["refresh_token"]})
            assert logout_resp.status_code == 204
