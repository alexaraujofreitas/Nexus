# ============================================================
# NEXUS TRADER Web — Dashboard Router
#
# GET /dashboard/summary     — aggregated portfolio + crash + engine state
# GET /dashboard/crash-defense — detailed crash defense status
# ============================================================
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends

from app.auth.dependencies import get_current_user
from app.api.engine import _send_engine_command

logger = logging.getLogger(__name__)
router = APIRouter(
    prefix="/dashboard",
    tags=["dashboard"],
    dependencies=[Depends(get_current_user)],
)


@router.get("/summary")
async def dashboard_summary():
    """Aggregated dashboard snapshot: portfolio, crash defense, engine state."""
    return await _send_engine_command("get_dashboard", {})


@router.get("/crash-defense")
async def crash_defense_status():
    """Detailed crash defense tier, score, and actions log."""
    return await _send_engine_command("get_crash_defense", {})
