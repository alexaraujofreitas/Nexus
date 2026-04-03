# ============================================================
# conftest.py — Shared test fixtures and test taxonomy hooks
#
# Test Taxonomy (see pytest.ini for marker definitions):
#   unit                 Pure logic tests, no external services
#   sandbox_integration  Uses in-process fakes (fakeredis)
#   docker_integration   Requires live PostgreSQL + Redis
#
# Ensures rate limiter uses in-memory storage (no Redis required)
# and resets app dependency_overrides between tests to prevent
# cross-module test pollution.
# ============================================================
import os
import socket

import pytest

# ── Force in-memory storage BEFORE any app imports ──────────
# Remove Redis URL so rate_limit.py defaults to memory://
os.environ.pop("NEXUS_REDIS_URL", None)
os.environ.setdefault("NEXUS_JWT_SECRET", "test-secret-32-chars-long-enough!!")
os.environ.setdefault("NEXUS_DATABASE_URL", "postgresql://test:test@localhost/test")


# ── Infrastructure detection ────────────────────────────────

def _pg_available(host: str = "127.0.0.1", port: int = 5433, timeout: float = 0.5) -> bool:
    """Check if PostgreSQL is accepting connections."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except (ConnectionRefusedError, OSError, TimeoutError):
        return False


# ── Auto-skip docker_integration when infra is absent ───────

def pytest_collection_modifyitems(config, items):
    """
    Auto-skip tests marked @pytest.mark.docker_integration when the
    required infrastructure (PostgreSQL on port 5433) is not available.

    This ensures 'pytest tests/' always produces a clean result:
      - Without Docker: docker_integration tests are SKIPPED (not FAILED)
      - With Docker E2E stack: all tests run normally
    """
    pg_up = _pg_available()
    if pg_up:
        return  # Infrastructure present — run everything

    skip_docker = pytest.mark.skip(
        reason="docker_integration: PostgreSQL not available on port 5433 "
               "(run inside Docker E2E stack or start local PostgreSQL)"
    )
    for item in items:
        if "docker_integration" in item.keywords:
            item.add_marker(skip_docker)
