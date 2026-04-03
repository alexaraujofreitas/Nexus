# ============================================================
# NEXUS TRADER Web — Risk Router
#
# GET /risk/status — portfolio risk snapshot
# ============================================================
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends

from app.auth.dependencies import get_current_user
from app.api.engine import _send_engine_command

logger = logging.getLogger(__name__)
router = APIRouter(
    prefix="/risk",
    tags=["risk"],
    dependencies=[Depends(get_current_user)],
)


@router.get("/status")
async def risk_status():
    """Current portfolio risk metrics: heat, drawdown, circuit breaker, crash tier."""
    return await _send_engine_command("get_risk_status", {})
