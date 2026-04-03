# ============================================================
# Phase 7C — Security Verification Tests
#
# Validates: security headers, auth security, rate limiting,
# data leakage prevention, Cloudflare middleware, CORS, request IDs
# ============================================================
import os
import json
import pytest
from unittest.mock import patch, MagicMock


class TestSecurityHeaders:
    @pytest.mark.asyncio
    async def test_nosniff_header(self):
        from app.middleware.security_headers import SecurityHeadersMiddleware

        class FR:
            headers = {}

        async def cn(r):
            return FR()

        resp = await SecurityHeadersMiddleware(app=None).dispatch(MagicMock(), cn)
        assert resp.headers["X-Content-Type-Options"] == "nosniff"

    @pytest.mark.asyncio
    async def test_frame_deny(self):
        from app.middleware.security_headers import SecurityHeadersMiddleware

        class FR:
            headers = {}

        async def cn(r):
            return FR()

        resp = await SecurityHeadersMiddleware(app=None).dispatch(MagicMock(), cn)
        assert resp.headers["X-Frame-Options"] == "DENY"

    @pytest.mark.asyncio
    async def test_hsts_enabled(self):
        from app.middleware.security_headers import SecurityHeadersMiddleware

        class FR:
            headers = {}

        async def cn(r):
            return FR()

        resp = await SecurityHeadersMiddleware(app=None).dispatch(MagicMock(), cn)
        hsts = resp.headers.get("Strict-Transport-Security", "")
        assert "max-age=31536000" in hsts
        assert "includeSubDomains" in hsts

    @pytest.mark.asyncio
    async def test_csp_default_src_self(self):
        from app.middleware.security_headers import SecurityHeadersMiddleware

        class FR:
            headers = {}

        async def cn(r):
            return FR()

        resp = await SecurityHeadersMiddleware(app=None).dispatch(MagicMock(), cn)
        assert "'self'" in resp.headers.get("Content-Security-Policy", "")

    @pytest.mark.asyncio
    async def test_referrer_policy(self):
        from app.middleware.security_headers import SecurityHeadersMiddleware

        class FR:
            headers = {}

        async def cn(r):
            return FR()

        resp = await SecurityHeadersMiddleware(app=None).dispatch(MagicMock(), cn)
        assert resp.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"


class TestAuthSecurity:
    def test_login_error_does_not_reveal_user_existence(self):
        import inspect
        from app.api.auth import login
        source = inspect.getsource(login)
        assert "Invalid email or password" in source
        assert "User not found" not in source
        assert "Wrong password" not in source

    def test_lockout_message_mentions_time(self):
        import inspect
        from app.api.auth import login
        source = inspect.getsource(login)
        assert "minute(s)" in source

    def test_jwt_secret_minimum_length(self):
        from app.config import validate_settings, Settings
        s = Settings(
            jwt_secret="a" * 31,
            database_url="postgresql+asyncpg://x:x@localhost/db",
            redis_url="redis://localhost:6379/0",
        )
        errors = validate_settings(s)
        assert any("32" in e for e in errors)


class TestCloudflareMiddleware:
    def test_disabled_by_default(self):
        with patch.dict(os.environ, {"NEXUS_CF_ENABLED": "false"}, clear=False):
            from app.middleware.cloudflare import CloudflareAccessMiddleware
            mw = CloudflareAccessMiddleware(app=MagicMock())
            assert mw._enabled is False

    def test_health_bypasses_cf(self):
        from app.middleware.cloudflare import _BYPASS_PATHS
        assert "/health" in _BYPASS_PATHS
        assert "/health/ready" in _BYPASS_PATHS


class TestRateLimiting:
    def test_rate_limits_configured(self):
        from app.middleware.rate_limit import get_global_limit, get_auth_limit, get_commands_limit
        assert "/minute" in get_global_limit()
        assert "/minute" in get_auth_limit()
        assert "/minute" in get_commands_limit()

    def test_auth_limit_is_restrictive(self):
        from app.config import get_settings
        s = get_settings()
        assert s.rate_limit_auth < s.rate_limit_global
        assert s.rate_limit_auth <= 10


class TestDataLeakagePrevention:
    @pytest.mark.asyncio
    async def test_500_no_traceback_in_production(self):
        from app.middleware.audit import AuditMiddleware

        async def fail(r):
            raise RuntimeError("postgresql://user:pass@host/db")

        mw = AuditMiddleware(app=None)
        req = MagicMock()
        req.method = "GET"
        req.url.path = "/test"
        req.headers = {}
        req.state = MagicMock()

        with patch.dict(os.environ, {"NEXUS_DEBUG": "false"}):
            resp = await mw.dispatch(req, fail)

        assert resp.status_code == 500
        body = json.loads(resp.body.decode())
        assert "traceback" not in body
        assert "postgresql://" not in json.dumps(body)

    @pytest.mark.asyncio
    async def test_500_includes_request_id(self):
        from app.middleware.audit import AuditMiddleware

        async def fail(r):
            raise RuntimeError("test")

        mw = AuditMiddleware(app=None)
        req = MagicMock()
        req.method = "GET"
        req.url.path = "/test"
        req.headers = {}
        req.state = MagicMock()
        resp = await mw.dispatch(req, fail)
        assert "X-Request-ID" in resp.headers

    def test_logging_masks_jwt_tokens(self):
        from app.logging_config import _mask_sensitive
        jwt = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4gRG9lIn0.Gfx6VO9tcxwk6xqx9yYzSfebfeakZp5JYIgP_edcw_A"
        result = _mask_sensitive(f"Authorization: Bearer {jwt}")
        assert "eyJ" not in result

    def test_logging_masks_passwords(self):
        from app.logging_config import _mask_sensitive
        result = _mask_sensitive('Login attempt password="hunter2" for user@test.com')
        assert "hunter2" not in result
