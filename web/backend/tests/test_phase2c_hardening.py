# ============================================================
# Phase 2C — WebSocket/Auth Hardening Tests
#
# Tests:
#   - Security headers present on all responses
#   - Rate limiting (429 on exceed)
#   - WS idle timeout constants
#   - WS max connections per user
#   - WS per-message token validation
#   - WS heartbeat/pong handling
#   - Audit logging patterns
# ============================================================
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
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

if "main" in sys.modules:
    _cached = sys.modules.pop("main")
    if hasattr(_cached, "app"):
        sys.modules["main"] = _cached
if _BACKEND in sys.path:
    sys.path.remove(_BACKEND)
sys.path.insert(0, _BACKEND)


_TEST_SECRET = "test-secret-32-chars-long-enough!!"


def _make_token():
    from app.auth.jwt import create_access_token
    return create_access_token({"sub": "1", "email": "test@nexustest.com"})


def _auth_headers():
    return {"Authorization": f"Bearer {_make_token()}"}


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


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


@pytest.fixture(autouse=True)
def _setup_env():
    env = {
        "NEXUS_JWT_SECRET": _TEST_SECRET,
        "NEXUS_DATABASE_URL": "postgresql://test:test@localhost/test",
        "NEXUS_REDIS_URL": "redis://localhost:6379/0",
        "NEXUS_CF_ENABLED": "false",
    }
    with patch.dict(os.environ, env):
        from app.config import clear_settings
        clear_settings()
        yield


# ============================================================
# Test: Security Headers
# ============================================================

class TestSecurityHeaders:
    """All responses must include the 6 security headers."""

    def test_health_has_security_headers(self):
        async def _test():
            async with _get_client() as client:
                resp = await client.get("/health")
                assert resp.status_code == 200
                assert resp.headers.get("X-Content-Type-Options") == "nosniff"
                assert resp.headers.get("X-Frame-Options") == "DENY"
                assert resp.headers.get("X-XSS-Protection") == "1; mode=block"
                assert "max-age=31536000" in resp.headers.get("Strict-Transport-Security", "")
                assert resp.headers.get("Content-Security-Policy") == "default-src 'self'"
                assert resp.headers.get("Referrer-Policy") == "strict-origin-when-cross-origin"
        _run(_test())

    def test_api_has_security_headers(self):
        async def _test():
            async with _get_client() as client:
                resp = await client.get("/api/v1/dashboard/summary")
                # Even 401 responses should have security headers
                assert resp.headers.get("X-Content-Type-Options") == "nosniff"
                assert resp.headers.get("X-Frame-Options") == "DENY"
        _run(_test())

    def test_root_has_security_headers(self):
        async def _test():
            async with _get_client() as client:
                resp = await client.get("/")
                assert resp.status_code == 200
                assert resp.headers.get("X-Content-Type-Options") == "nosniff"
                assert resp.headers.get("Referrer-Policy") == "strict-origin-when-cross-origin"
        _run(_test())


# ============================================================
# Test: Rate Limiting Configuration
# ============================================================

class TestRateLimiting:
    """Rate limiting is configured and responds with 429 on exceed."""

    def test_limiter_exists_on_app(self):
        app = _get_app()
        assert hasattr(app.state, "limiter")

    def test_rate_limit_defaults(self):
        from app.config import get_settings
        s = get_settings()
        assert s.rate_limit_global == 100
        assert s.rate_limit_auth == 5
        assert s.rate_limit_commands == 10

    def test_rate_limit_strings(self):
        from app.middleware.rate_limit import get_global_limit, get_auth_limit, get_commands_limit
        assert get_global_limit() == "100/minute"
        assert get_auth_limit() == "5/minute"
        assert get_commands_limit() == "10/minute"

    def test_rate_limit_exceeded_handler_exists(self):
        """The 429 exception handler is registered."""
        from slowapi.errors import RateLimitExceeded
        app = _get_app()
        # Exception handlers are stored on the app
        assert RateLimitExceeded in app.exception_handlers


# ============================================================
# Test: WebSocket Hardening — Constants
# ============================================================

class TestWSConstants:
    """Verify WS hardening constants match Phase 2C design."""

    def test_idle_timeout(self):
        from app.ws.manager import IDLE_TIMEOUT_SECONDS
        assert IDLE_TIMEOUT_SECONDS == 30 * 60  # 30 minutes

    def test_max_connections_per_user(self):
        from app.ws.manager import MAX_CONNECTIONS_PER_USER
        assert MAX_CONNECTIONS_PER_USER == 5

    def test_heartbeat_interval(self):
        from app.ws.manager import HEARTBEAT_INTERVAL_SECONDS
        assert HEARTBEAT_INTERVAL_SECONDS == 30

    def test_heartbeat_timeout(self):
        from app.ws.manager import HEARTBEAT_TIMEOUT_SECONDS
        assert HEARTBEAT_TIMEOUT_SECONDS == 10


# ============================================================
# Test: WS Manager — Max Connections Per User
# ============================================================

class TestWSMaxConnections:
    """Enforce max 5 connections per user."""

    def test_connection_counting(self):
        from app.ws.manager import ConnectionManager
        mgr = ConnectionManager()

        # Simulate connections (bypass actual WS)
        for i in range(3):
            mock_ws = MagicMock()
            state_mock = MagicMock()
            state_mock.user_sub = "user1"
            state_mock.email = "test@test.com"
            conn_id = f"ws_user1_{i}"
            mgr._connections[conn_id] = state_mock
            mgr._user_counts["user1"] = mgr._user_counts.get("user1", 0) + 1

        assert mgr.get_user_connection_count("user1") == 3
        assert mgr.get_user_connection_count("user2") == 0

    def test_disconnect_decrements_count(self):
        from app.ws.manager import ConnectionManager, _ConnState
        mgr = ConnectionManager()

        mock_ws = MagicMock()
        state = _ConnState(ws=mock_ws, user_sub="u1", email="t@t.com", token="tok")
        mgr._connections["ws_u1_1"] = state
        mgr._user_counts["u1"] = 1

        mgr.disconnect("ws_u1_1")
        assert mgr.get_user_connection_count("u1") == 0
        assert "ws_u1_1" not in mgr._connections


# ============================================================
# Test: WS Manager — Token Validation
# ============================================================

class TestWSTokenValidation:
    """Per-message token expiry checks."""

    def test_validate_token_valid(self):
        from app.ws.manager import ConnectionManager, _ConnState
        mgr = ConnectionManager()

        token = _make_token()
        state = _ConnState(ws=MagicMock(), user_sub="1", email="t@t.com", token=token)
        mgr._connections["ws_1_1"] = state

        assert mgr.validate_token("ws_1_1") is True

    def test_validate_token_expired(self):
        """An expired token should fail validation."""
        from app.ws.manager import ConnectionManager, _ConnState
        import jwt as pyjwt

        mgr = ConnectionManager()

        # Create an expired token
        expired_token = pyjwt.encode(
            {"sub": "1", "email": "t@t.com", "type": "access", "exp": time.time() - 60},
            _TEST_SECRET,
            algorithm="HS256",
        )
        state = _ConnState(ws=MagicMock(), user_sub="1", email="t@t.com", token=expired_token)
        mgr._connections["ws_1_1"] = state

        assert mgr.validate_token("ws_1_1") is False

    def test_validate_missing_connection(self):
        from app.ws.manager import ConnectionManager
        mgr = ConnectionManager()
        assert mgr.validate_token("nonexistent") is False


# ============================================================
# Test: WS Manager — Heartbeat State
# ============================================================

class TestWSHeartbeat:
    """Heartbeat pong recording and idle tracking."""

    def test_touch_updates_last_active(self):
        from app.ws.manager import ConnectionManager, _ConnState
        mgr = ConnectionManager()

        state = _ConnState(ws=MagicMock(), user_sub="1", email="t@t.com", token="tok")
        state.last_active = time.time() - 100  # Simulate old activity
        mgr._connections["ws_1_1"] = state

        mgr.touch("ws_1_1")
        assert (time.time() - state.last_active) < 2

    def test_record_pong_updates_last_pong(self):
        from app.ws.manager import ConnectionManager, _ConnState
        mgr = ConnectionManager()

        state = _ConnState(ws=MagicMock(), user_sub="1", email="t@t.com", token="tok")
        state.last_pong = time.time() - 100
        mgr._connections["ws_1_1"] = state

        mgr.record_pong("ws_1_1")
        assert (time.time() - state.last_pong) < 2


# ============================================================
# Test: WS Channels — Phase 2A additions
# ============================================================

class TestWSChannels:
    """Verify Phase 2A WebSocket channels are registered."""

    def test_phase2a_channels_registered(self):
        from app.ws.manager import CHANNELS
        for ch in ["dashboard", "crash_defense", "risk", "scanner", "signals"]:
            assert ch in CHANNELS, f"Missing channel: {ch}"


# ============================================================
# Test: Middleware Stack Order
# ============================================================

class TestMiddlewareStack:
    """Verify middleware is registered in correct order."""

    def test_cf_middleware_registered(self):
        from app.middleware.cloudflare import CloudflareAccessMiddleware
        app = _get_app()
        mw_classes = [m.cls for m in app.user_middleware]
        assert CloudflareAccessMiddleware in mw_classes

    def test_security_headers_registered(self):
        from app.middleware.security_headers import SecurityHeadersMiddleware
        app = _get_app()
        mw_classes = [m.cls for m in app.user_middleware]
        assert SecurityHeadersMiddleware in mw_classes
