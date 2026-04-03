# ============================================================
# Rate Limiting Middleware (slowapi)
#
# Enforces per-IP rate limits:
#   - Global: 100 req/min (configurable via NEXUS_RATE_LIMIT_GLOBAL)
#   - Auth endpoints: 5 req/min (configurable via NEXUS_RATE_LIMIT_AUTH)
#   - Engine commands: 10 req/min (configurable via NEXUS_RATE_LIMIT_COMMANDS)
#
# Uses in-memory storage by default. For distributed deployments,
# configure Redis backend via NEXUS_REDIS_URL.
# ============================================================
from __future__ import annotations

import logging
import os

from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
from starlette.requests import Request
from starlette.responses import JSONResponse

from app.config import get_settings

logger = logging.getLogger(__name__)

# Create limiter — key function extracts client IP
# Default to in-memory storage. Production Redis is configured at startup
# via main.py lifespan when NEXUS_REDIS_URL is set.
_storage_uri = "memory://"
_redis_url = os.environ.get("NEXUS_REDIS_URL", "")
if _redis_url:
    _storage_uri = _redis_url

limiter = Limiter(key_func=get_remote_address, storage_uri=_storage_uri)


def rate_limit_exceeded_handler(request: Request, exc: RateLimitExceeded):
    """Return 429 Too Many Requests with retry-after header."""
    logger.warning(
        "Rate limit exceeded: %s %s from %s",
        request.method,
        request.url.path,
        get_remote_address(request),
    )
    return JSONResponse(
        status_code=429,
        content={
            "detail": "Rate limit exceeded. Please try again later.",
            "retry_after": str(exc.detail),
        },
    )


def get_global_limit() -> str:
    """Return global rate limit string for slowapi."""
    settings = get_settings()
    return f"{settings.rate_limit_global}/minute"


def get_auth_limit() -> str:
    """Return auth endpoint rate limit string."""
    settings = get_settings()
    return f"{settings.rate_limit_auth}/minute"


def get_commands_limit() -> str:
    """Return engine commands rate limit string."""
    settings = get_settings()
    return f"{settings.rate_limit_commands}/minute"
