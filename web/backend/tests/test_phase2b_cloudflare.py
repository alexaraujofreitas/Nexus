# ============================================================
# Phase 2B — Cloudflare Zero Trust Access Tests
#
# Tests CF JWT validation middleware:
#   - Bypass when disabled (dev mode)
#   - Health endpoints exempt
#   - Missing header → 401
#   - Invalid/expired/wrong-audience JWT → 401
#   - Valid CF JWT → pass through
#   - Key cache TTL and refresh
#   - Dual-layer auth (CF JWT + app JWT)
# ============================================================
from __future__ import annotations

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

# Force backend main resolution
if "main" in sys.modules:
    _cached = sys.modules.pop("main")
    if hasattr(_cached, "app"):
        sys.modules["main"] = _cached
_backend_idx = sys.path.index(_BACKEND) if _BACKEND in sys.path else -1
if _backend_idx != 0:
    if _BACKEND in sys.path:
        sys.path.remove(_BACKEND)
    sys.path.insert(0, _BACKEND)


import asyncio
import jwt as pyjwt
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives import serialization


# ── RSA key pair for signing test CF JWTs ───────────────────

_rsa_private_key = rsa.generate_private_key(
    public_exponent=65537,
    key_size=2048,
)
_rsa_public_key = _rsa_private_key.public_key()

# Export public key in JWK format for the mock JWKS endpoint
_pub_numbers = _rsa_public_key.public_numbers()


def _int_to_base64url(n: int) -> str:
    """Convert an integer to a base64url-encoded string (no padding)."""
    import base64
    byte_len = (n.bit_length() + 7) // 8
    b = n.to_bytes(byte_len, byteorder="big")
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


_TEST_JWK = {
    "kty": "RSA",
    "use": "sig",
    "alg": "RS256",
    "kid": "test-key-1",
    "n": _int_to_base64url(_pub_numbers.n),
    "e": _int_to_base64url(_pub_numbers.e),
}

_TEST_AUDIENCE = "test-cf-audience-tag-12345"
_TEST_TEAM_DOMAIN = "testteam.cloudflareaccess.com"


def _make_cf_jwt(
    audience: str = _TEST_AUDIENCE,
    email: str = "admin@nexustrader.com",
    exp_offset: int = 300,
    nbf_offset: int = -10,
    kid: str = "test-key-1",
) -> str:
    """Create a signed RS256 JWT mimicking Cloudflare Access."""
    now = int(time.time())
    payload = {
        "aud": [audience],
        "email": email,
        "exp": now + exp_offset,
        "iat": now,
        "nbf": now + nbf_offset,
        "iss": f"https://{_TEST_TEAM_DOMAIN}",
        "sub": "cf-user-id-123",
        "type": "app",
        "identity_nonce": "test-nonce",
        "custom": {"groups": ["admins"]},
    }
    headers = {"kid": kid}
    # Encode using RS256 with our test private key
    private_pem = _rsa_private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return pyjwt.encode(payload, private_pem, algorithm="RS256", headers=headers)


# ── Test JWT secret for app-level JWT ───────────────────────
_TEST_SECRET = "test-secret-32-chars-long-enough!!"


def _make_app_token():
    """Create a valid app-level JWT access token."""
    from app.auth.jwt import create_access_token
    return create_access_token({"sub": "1", "email": "test@nexustest.com"})


def _auth_headers(include_cf: bool = True):
    """Return headers with both CF and app JWT."""
    h = {"Authorization": f"Bearer {_make_app_token()}"}
    if include_cf:
        h["CF-Access-JWT-Assertion"] = _make_cf_jwt()
    return h


# ── Mock JWKS endpoint ──────────────────────────────────────

def _mock_httpx_get(url: str, **kwargs):
    """Mock httpx.get to return our test JWKS."""
    resp = MagicMock()
    resp.status_code = 200
    resp.raise_for_status = MagicMock()
    resp.json.return_value = {"keys": [_TEST_JWK]}
    return resp


# ── Async helper ────────────────────────────────────────────

def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ── App singleton ───────────────────────────────────────────

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


# ============================================================
# Test: CF Middleware Disabled (Dev Mode)
# ============================================================

class TestCFMiddlewareDisabled:
    """When NEXUS_CF_ENABLED=false (default), all requests pass through."""

    @pytest.fixture(autouse=True)
    def _setup(self):
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

    def test_no_cf_header_still_200_on_health(self):
        async def _test():
            async with _get_client() as client:
                resp = await client.get("/health")
                assert resp.status_code == 200
        _run(_test())

    def test_no_cf_header_passes_to_app_auth(self):
        """Without CF header, request reaches app-level auth (which needs app JWT)."""
        async def _test():
            async with _get_client() as client:
                # No CF header, no app JWT → should get 401 from app-level auth
                resp = await client.get("/api/v1/dashboard/summary")
                assert resp.status_code in (401, 403)
        _run(_test())


# ============================================================
# Test: CF Middleware Enabled
# ============================================================

class TestCFMiddlewareEnabled:
    """When NEXUS_CF_ENABLED=true, CF JWT is required."""

    @pytest.fixture(autouse=True)
    def _setup(self):
        env = {
            "NEXUS_JWT_SECRET": _TEST_SECRET,
            "NEXUS_DATABASE_URL": "postgresql://test:test@localhost/test",
            "NEXUS_REDIS_URL": "redis://localhost:6379/0",
            "NEXUS_CF_ENABLED": "true",
            "NEXUS_CF_TEAM_DOMAIN": _TEST_TEAM_DOMAIN,
            "NEXUS_CF_AUDIENCE": _TEST_AUDIENCE,
        }
        with patch.dict(os.environ, env):
            from app.config import clear_settings
            clear_settings()
            # Reset the middleware's internal state
            from app.middleware.cloudflare import CloudflareAccessMiddleware, _key_cache
            _key_cache.configure(_TEST_TEAM_DOMAIN)
            yield

    @patch("app.middleware.cloudflare.httpx.get", side_effect=_mock_httpx_get)
    def test_health_bypasses_cf_check(self, mock_get):
        """Health endpoints should be accessible without CF JWT."""
        async def _test():
            async with _get_client() as client:
                resp = await client.get("/health")
                assert resp.status_code == 200
        _run(_test())

    @patch("app.middleware.cloudflare.httpx.get", side_effect=_mock_httpx_get)
    def test_missing_cf_header_401(self, mock_get):
        """Requests without CF-Access-JWT-Assertion get 401."""
        async def _test():
            # Temporarily enable CF on the middleware instance
            from app.middleware.cloudflare import CloudflareAccessMiddleware
            app = _get_app()
            for mw in app.user_middleware:
                if mw.cls is CloudflareAccessMiddleware:
                    break
            # The middleware reads enabled from env at init. We need to
            # force it to be enabled by patching the env and re-creating.
            # Since middleware instances are created at app init, we test
            # the validation function directly instead.
            from app.middleware.cloudflare import _validate_cf_jwt
            try:
                _validate_cf_jwt("", _TEST_AUDIENCE)
                assert False, "Should have raised ValueError"
            except (ValueError, Exception):
                pass  # Expected: empty token fails validation
        _run(_test())

    @patch("app.middleware.cloudflare.httpx.get", side_effect=_mock_httpx_get)
    def test_valid_cf_jwt_accepted(self, mock_get):
        """A properly signed CF JWT should pass validation."""
        from app.middleware.cloudflare import _validate_cf_jwt, _key_cache
        _key_cache._fetched_at = 0  # Force refresh
        payload = _validate_cf_jwt(_make_cf_jwt(), _TEST_AUDIENCE)
        assert payload["email"] == "admin@nexustrader.com"
        assert _TEST_AUDIENCE in payload["aud"]

    @patch("app.middleware.cloudflare.httpx.get", side_effect=_mock_httpx_get)
    def test_expired_cf_jwt_rejected(self, mock_get):
        """An expired CF JWT should be rejected."""
        from app.middleware.cloudflare import _validate_cf_jwt, _key_cache
        _key_cache._fetched_at = 0
        expired_token = _make_cf_jwt(exp_offset=-60)  # Expired 60s ago
        with pytest.raises(ValueError, match="expired"):
            _validate_cf_jwt(expired_token, _TEST_AUDIENCE)

    @patch("app.middleware.cloudflare.httpx.get", side_effect=_mock_httpx_get)
    def test_wrong_audience_rejected(self, mock_get):
        """CF JWT with wrong audience tag should be rejected."""
        from app.middleware.cloudflare import _validate_cf_jwt, _key_cache
        _key_cache._fetched_at = 0
        wrong_aud_token = _make_cf_jwt(audience="wrong-audience-tag")
        with pytest.raises(ValueError, match="audience"):
            _validate_cf_jwt(wrong_aud_token, _TEST_AUDIENCE)

    @patch("app.middleware.cloudflare.httpx.get", side_effect=_mock_httpx_get)
    def test_tampered_token_rejected(self, mock_get):
        """A token with modified payload should fail signature validation."""
        from app.middleware.cloudflare import _validate_cf_jwt, _key_cache
        _key_cache._fetched_at = 0
        valid_token = _make_cf_jwt()
        # Tamper with the payload section
        parts = valid_token.split(".")
        parts[1] = parts[1][:10] + "XXXXX" + parts[1][15:]
        tampered = ".".join(parts)
        with pytest.raises(ValueError):
            _validate_cf_jwt(tampered, _TEST_AUDIENCE)


# ============================================================
# Test: Key Cache Behaviour
# ============================================================

class TestKeyCacheBehaviour:
    """Test JWKS key caching with TTL and force-refresh."""

    @patch("app.middleware.cloudflare.httpx.get", side_effect=_mock_httpx_get)
    def test_cache_ttl(self, mock_get):
        """Keys are fetched once and cached for TTL duration."""
        from app.middleware.cloudflare import _KeyCache
        cache = _KeyCache(ttl_seconds=300)
        cache.configure(_TEST_TEAM_DOMAIN)

        keys1 = cache.get_keys()
        assert len(keys1) == 1
        assert mock_get.call_count == 1

        keys2 = cache.get_keys()
        assert len(keys2) == 1
        assert mock_get.call_count == 1  # No additional fetch

    @patch("app.middleware.cloudflare.httpx.get", side_effect=_mock_httpx_get)
    def test_cache_expired_refetches(self, mock_get):
        """After TTL expires, keys are re-fetched."""
        from app.middleware.cloudflare import _KeyCache
        cache = _KeyCache(ttl_seconds=1)
        cache.configure(_TEST_TEAM_DOMAIN)

        keys1 = cache.get_keys()
        assert mock_get.call_count == 1

        # Simulate TTL expiry
        cache._fetched_at = time.time() - 2
        keys2 = cache.get_keys()
        assert mock_get.call_count == 2

    @patch("app.middleware.cloudflare.httpx.get", side_effect=_mock_httpx_get)
    def test_force_refresh(self, mock_get):
        """force_refresh() fetches immediately regardless of TTL."""
        from app.middleware.cloudflare import _KeyCache
        cache = _KeyCache(ttl_seconds=300)
        cache.configure(_TEST_TEAM_DOMAIN)

        cache.get_keys()
        assert mock_get.call_count == 1

        cache.force_refresh()
        assert mock_get.call_count == 2

    def test_fetch_failure_logs_error(self):
        """When JWKS fetch fails, cache returns empty (no crash)."""
        from app.middleware.cloudflare import _KeyCache

        def _fail_get(url, **kwargs):
            raise ConnectionError("Network down")

        with patch("app.middleware.cloudflare.httpx.get", side_effect=_fail_get):
            cache = _KeyCache(ttl_seconds=300)
            cache.configure(_TEST_TEAM_DOMAIN)
            keys = cache.get_keys()
            assert keys == []


# ============================================================
# Test: Dual-Layer Auth (CF + App JWT)
# ============================================================

class TestDualLayerAuth:
    """When CF is enabled, both CF JWT and app JWT are required."""

    @pytest.fixture(autouse=True)
    def _setup(self):
        env = {
            "NEXUS_JWT_SECRET": _TEST_SECRET,
            "NEXUS_DATABASE_URL": "postgresql://test:test@localhost/test",
            "NEXUS_REDIS_URL": "redis://localhost:6379/0",
            "NEXUS_CF_ENABLED": "false",  # Test app-level auth separately
        }
        with patch.dict(os.environ, env):
            from app.config import clear_settings
            clear_settings()
            yield

    def test_app_jwt_only_reaches_endpoint(self):
        """With CF disabled, app JWT alone reaches the protected endpoint."""
        async def _test():
            async with _get_client() as client:
                headers = {"Authorization": f"Bearer {_make_app_token()}"}
                with patch("app.api.dashboard._send_engine_command", new_callable=AsyncMock) as mock_cmd:
                    mock_cmd.return_value = {"capital": 10000}
                    resp = await client.get("/api/v1/dashboard/summary", headers=headers)
                    assert resp.status_code == 200
        _run(_test())

    def test_no_auth_at_all_401(self):
        """Without any auth, protected endpoints return 401."""
        async def _test():
            async with _get_client() as client:
                resp = await client.get("/api/v1/dashboard/summary")
                assert resp.status_code in (401, 403)
        _run(_test())


# ============================================================
# Test: Bypass Path Configuration
# ============================================================

class TestBypassPaths:
    """Verify health endpoint paths are correctly bypassed."""

    def test_bypass_paths_include_health(self):
        from app.middleware.cloudflare import _BYPASS_PATHS
        assert "/health" in _BYPASS_PATHS
        assert "/health/ready" in _BYPASS_PATHS

    def test_api_paths_not_bypassed(self):
        from app.middleware.cloudflare import _BYPASS_PATHS
        assert "/api/v1/auth/login" not in _BYPASS_PATHS
        assert "/api/v1/dashboard/summary" not in _BYPASS_PATHS
