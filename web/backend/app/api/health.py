# ============================================================
# NEXUS TRADER Web — Health Check Router
#
# /health — liveness probe (always 200)
# /health/ready — readiness probe (checks DB + Redis)
# ============================================================
from __future__ import annotations

import logging
import time

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import get_db

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/health", tags=["health"])


@router.get("")
async def liveness():
    """Liveness probe — always returns 200."""
    return {"status": "alive", "service": "nexus-api"}


@router.get("/ready")
async def readiness(db: AsyncSession = Depends(get_db)):
    """Readiness probe — checks PostgreSQL and Redis connectivity."""
    settings = get_settings()
    checks = {}

    # PostgreSQL
    try:
        t0 = time.monotonic()
        await db.execute(text("SELECT 1"))
        checks["postgresql"] = {
            "status": "ok",
            "latency_ms": round((time.monotonic() - t0) * 1000, 1),
        }
    except Exception as e:
        checks["postgresql"] = {"status": "error", "detail": str(e)}

    # Redis
    try:
        t0 = time.monotonic()
        r = aioredis.from_url(settings.redis_url, decode_responses=True)
        await r.ping()
        await r.aclose()
        checks["redis"] = {
            "status": "ok",
            "latency_ms": round((time.monotonic() - t0) * 1000, 1),
        }
    except Exception as e:
        checks["redis"] = {"status": "error", "detail": str(e)}

    all_ok = all(c["status"] == "ok" for c in checks.values())
    return {
        "status": "ready" if all_ok else "degraded",
        "checks": checks,
    }
