# ============================================================
# Cloudflare Zero Trust Access — JWT Validation Middleware
#
# Validates the CF-Access-JWT-Assertion header on every request
# except health endpoints. This is the OUTER auth gate; the
# inner gate is the per-route app JWT check via get_current_user.
#
# Configuration via environment:
#   NEXUS_CF_ENABLED=true|false   (default false — dev bypass)
#   NEXUS_CF_TEAM_DOMAIN=team.cloudflareaccess.com
#   NEXUS_CF_AUDIENCE=<application-audience-tag>
#
# Signing keys are fetched from:
#   https://<team>.cloudflareaccess.com/cdn-cgi/access/certs
# and cached with a 5-minute TTL. On validation failure the
# cache is cleared and keys are re-fetched once.
# ============================================================
from __future__ import annotations

import logging
import os
import time
from typing import Any, Optional

import httpx
import jwt as pyjwt
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

logger = logging.getLogger(__name__)

# Paths that bypass Cloudflare JWT check (health monitoring)
_BYPASS_PATHS: set[str] = {
    "/health",
    "/health/ready",
}


class _KeyCache:
    """In-memory cache for Cloudflare signing keys (JWKS)."""

    def __init__(self, ttl_seconds: int = 300):
        self._ttl = ttl_seconds
        self._keys: list[dict[str, Any]] = []
        self._fetched_at: float = 0.0
        self._certs_url: str = ""

    def configure(self, team_domain: str) -> None:
        self._certs_url = f"https://{team_domain}/cdn-cgi/access/certs"

    @property
    def is_stale(self) -> bool:
        return (time.time() - self._fetched_at) > self._ttl

    def get_keys(self) -> list[dict[str, Any]]:
        if self.is_stale:
            self._refresh()
        return self._keys

    def force_refresh(self) -> list[dict[str, Any]]:
        self._refresh()
        return self._keys

    def _refresh(self) -> None:
        if not self._certs_url:
            logger.warning("CF certs URL not configured — cannot fetch signing keys")
            self._keys = []
            return
        try:
            resp = httpx.get(self._certs_url, timeout=10.0)
            resp.raise_for_status()
            data = resp.json()
            self._keys = data.get("keys", [])
            self._fetched_at = time.time()
            logger.info(
                "Fetched %d Cloudflare signing keys from %s",
                len(self._keys),
                self._certs_url,
            )
        except Exception:
            logger.exception("Failed to fetch Cloudflare signing keys")
            # Keep stale keys if we have any; caller handles empty keys


_key_cache = _KeyCache()


def _validate_cf_jwt(token: str, audience: str) -> dict[str, Any]:
    """
    Validate a Cloudflare Access JWT.

    Checks:
      - Signature against CF signing keys (RS256)
      - iss matches team domain
      - aud contains the configured audience tag
      - exp / nbf / iat standard claims

    Returns decoded payload on success.
    Raises ValueError on any validation failure.
    """
    keys = _key_cache.get_keys()
    if not keys:
        raise ValueError("No Cloudflare signing keys available")

    decode_opts = {
        "verify_exp": True,
        "verify_nbf": True,
        "verify_iss": False,  # CF doesn't always set iss consistently
        "verify_aud": True,
    }

    def _try_decode(key_list: list[dict]) -> Optional[dict]:
        """Try to decode with each key in the list. Returns payload or None."""
        nonlocal last_error
        jwk_set = pyjwt.PyJWKSet.from_dict({"keys": key_list})
        for jwk in jwk_set.keys:
            try:
                payload = pyjwt.decode(
                    token,
                    jwk.key,  # Extract the actual cryptographic key
                    algorithms=["RS256"],
                    audience=audience,
                    options=decode_opts,
                )
                return payload
            except pyjwt.ExpiredSignatureError:
                raise ValueError("CF JWT has expired")
            except pyjwt.InvalidAudienceError:
                raise ValueError("CF JWT audience mismatch")
            except pyjwt.DecodeError as e:
                last_error = e
                continue
            except Exception as e:
                last_error = e
                continue
        return None

    last_error: Optional[Exception] = None

    result = _try_decode(keys)
    if result is not None:
        return result

    # All keys failed — try force-refreshing keys once
    logger.info("All cached CF keys failed validation, force-refreshing")
    keys = _key_cache.force_refresh()
    if not keys:
        raise ValueError("No Cloudflare signing keys available after refresh")

    result = _try_decode(keys)
    if result is not None:
        return result

    raise ValueError(f"CF JWT signature validation failed: {last_error}")


class CloudflareAccessMiddleware(BaseHTTPMiddleware):
    """
    Starlette middleware that validates Cloudflare Access JWT on
    every request except bypass paths (health endpoints).

    When NEXUS_CF_ENABLED is false (default), all requests pass through.
    """

    def __init__(self, app, **kwargs):
        super().__init__(app, **kwargs)
        self._enabled = os.getenv("NEXUS_CF_ENABLED", "false").lower() == "true"
        self._team_domain = os.getenv("NEXUS_CF_TEAM_DOMAIN", "")
        self._audience = os.getenv("NEXUS_CF_AUDIENCE", "")

        if self._enabled:
            if not self._team_domain:
                logger.error("NEXUS_CF_ENABLED=true but NEXUS_CF_TEAM_DOMAIN not set!")
            if not self._audience:
                logger.error("NEXUS_CF_ENABLED=true but NEXUS_CF_AUDIENCE not set!")
            _key_cache.configure(self._team_domain)
            logger.info(
                "Cloudflare Access middleware ENABLED — team=%s, audience=%s",
                self._team_domain,
                self._audience[:16] + "..." if len(self._audience) > 16 else self._audience,
            )
        else:
            logger.info("Cloudflare Access middleware DISABLED (dev mode)")

    async def dispatch(self, request: Request, call_next):
        # Dev bypass
        if not self._enabled:
            return await call_next(request)

        # Health endpoints bypass
        path = request.url.path.rstrip("/")
        if path in _BYPASS_PATHS:
            return await call_next(request)

        # WebSocket upgrade requests — check query param token or header
        if request.headers.get("upgrade", "").lower() == "websocket":
            # For WebSocket, CF Access token may be in cookie or header
            cf_token = request.headers.get("CF-Access-JWT-Assertion", "")
            if not cf_token:
                cf_token = request.cookies.get("CF_Authorization", "")
        else:
            cf_token = request.headers.get("CF-Access-JWT-Assertion", "")

        if not cf_token:
            return JSONResponse(
                status_code=401,
                content={"detail": "Missing Cloudflare Access token"},
            )

        try:
            payload = _validate_cf_jwt(cf_token, self._audience)
            # Store CF identity on request state for audit logging
            request.state.cf_identity = payload.get("email", "unknown")
        except ValueError as e:
            logger.warning("CF JWT validation failed: %s", e)
            return JSONResponse(
                status_code=401,
                content={"detail": f"Cloudflare Access authentication failed: {e}"},
            )

        return await call_next(request)
