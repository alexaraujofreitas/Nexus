# ============================================================
# NEXUS TRADER Web — System Router
#
# GET  /system/health     — detailed system health (auth required)
# POST /system/kill-switch — emergency stop
# ============================================================
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends

from app.auth.dependencies import get_current_user
from app.api.engine import _send_engine_command

logger = logging.getLogger(__name__)
router = APIRouter(
    prefix="/system",
    tags=["system"],
    dependencies=[Depends(get_current_user)],
)


@router.get("/health")
async def system_health():
    """Detailed system health: threads, scanner, executor, exchange, engine."""
    return await _send_engine_command("get_system_health", {})


@router.post("/kill-switch")
async def kill_switch():
    """EMERGENCY: Close all positions, pause trading, stop scanner."""
    return await _send_engine_command("kill_switch", {})
