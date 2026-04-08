# ============================================================
# Security Headers Middleware
#
# Adds standard security headers to all HTTP responses:
#   - X-Content-Type-Options: nosniff
#   - X-Frame-Options: DENY
#   - X-XSS-Protection: 1; mode=block
#   - Strict-Transport-Security (HSTS)
#   - Content-Security-Policy
#   - Referrer-Policy
# ============================================================
from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Add security headers to every HTTP response."""

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        response.headers["Strict-Transport-Security"] = (
            "max-age=31536000; includeSubDomains"
        )
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "style-src 'self' 'unsafe-inline'; "
            "script-src 'self' 'unsafe-inline'; "
            "img-src 'self' data: blob:; "
            "font-src 'self' data:; "
            "connect-src 'self' ws: wss:; "
        )
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        return response
