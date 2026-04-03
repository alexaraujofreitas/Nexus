# ============================================================
# NEXUS TRADER Web — Validation Router
#
# GET /validation/health         — comprehensive health report
# GET /validation/readiness      — system readiness assessment
# GET /validation/data-integrity — data consistency checks
# ============================================================
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends

from app.auth.dependencies import get_current_user
from app.api.engine import _send_engine_command

logger = logging.getLogger(__name__)
router = APIRouter(
    prefix="/validation",
    tags=["validation"],
    dependencies=[Depends(get_current_user)],
)


@router.get("/health")
async def validation_health():
    """Comprehensive health report: components, threads, error rates."""
    return await _send_engine_command("get_validation_health", {})


@router.get("/readiness")
async def validation_readiness():
    """System readiness assessment (STILL_LEARNING / IMPROVING / READY)."""
    return await _send_engine_command("get_readiness", {})


@router.get("/data-integrity")
async def data_integrity():
    """Data consistency checks: positions, trades, capital reconciliation."""
    return await _send_engine_command("get_data_integrity", {})
