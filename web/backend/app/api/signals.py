# ============================================================
# NEXUS TRADER Web — Signals Router
#
# GET /signals/agents      — all 23 agent statuses
# GET /signals/confluence   — recent confluence signals
# ============================================================
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends

from app.auth.dependencies import get_current_user
from app.api.engine import _send_engine_command

logger = logging.getLogger(__name__)
router = APIRouter(
    prefix="/signals",
    tags=["signals"],
    dependencies=[Depends(get_current_user)],
)


@router.get("/agents")
async def agent_status():
    """Return status of all 23 intelligence agents."""
    return await _send_engine_command("get_agent_status", {})


@router.get("/confluence")
async def confluence_signals():
    """Return recent confluence signals from the signal pipeline."""
    return await _send_engine_command("get_signals", {})
