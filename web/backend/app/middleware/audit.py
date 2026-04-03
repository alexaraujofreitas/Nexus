# ============================================================
# NEXUS TRADER Web — Request Audit Middleware
#
# Phase 6A: Logs every HTTP request with method, path, status,
# latency, user identity, and request ID. Generates a UUID4
# request_id per request and attaches it to both the request
# state and the response headers (X-Request-ID).
#
# Excludes /health and /health/ready from audit logs.
# ============================================================
from __future__ import annotations

import logging
import time
import uuid

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

logger = logging.getLogger("nexus.audit")

# Paths excluded from audit logging (high-frequency healthchecks)
_AUDIT_EXCLUDE = {"/health", "/health/ready"}


class AuditMiddleware(BaseHTTPMiddleware):
    """
    Attach X-Request-ID to every request/response and log an audit
    line with method, path, status, latency, and user identity.

    Also acts as a global exception safety net: any unhandled exception
    that escapes the router layer is caught here, logged, and returned
    as a sanitized 500 JSON response. In debug mode (NEXUS_DEBUG=true),
    the traceback is included.
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        import os
        import traceback as tb_module
        from starlette.responses import JSONResponse as StarletteJSONResponse

        # Generate or reuse forwarded request ID
        request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
        request.state.request_id = request_id

        start = time.monotonic()
        status_code = 500

        try:
            response: Response = await call_next(request)
            status_code = response.status_code
        except Exception as exc:
            # Global catch-all: log and return sanitized 500
            logger.error(
                "Unhandled exception on %s %s: %s",
                request.method, request.url.path, exc,
                exc_info=True,
            )
            content: dict = {
                "detail": "Internal server error",
                "request_id": request_id,
            }
            is_debug = os.getenv("NEXUS_DEBUG", "false").lower() == "true"
            if is_debug:
                content["traceback"] = tb_module.format_exception(
                    type(exc), exc, exc.__traceback__,
                )
            response = StarletteJSONResponse(
                status_code=500,
                content=content,
                headers={"X-Request-ID": request_id},
            )
            status_code = 500

        # Attach request ID to response
        response.headers["X-Request-ID"] = request_id

        latency_ms = round((time.monotonic() - start) * 1000, 1)

        # Audit log (skip healthcheck noise)
        path = request.url.path
        if path not in _AUDIT_EXCLUDE:
            user_id = getattr(request.state, "user_id", None)
            client_ip = request.client.host if request.client else "unknown"
            logger.info(
                "request",
                extra={
                    "method": request.method,
                    "path": path,
                    "status": status_code,
                    "latency_ms": latency_ms,
                    "user_id": user_id,
                    "request_id": request_id,
                    "client_ip": client_ip,
                },
            )

        return response
