# ============================================================
# NEXUS TRADER Web — Logs Router
#
# GET /logs/recent — recent log entries from engine ring buffer
# ============================================================
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, Query

from app.auth.dependencies import get_current_user
from app.api.engine import _send_engine_command

logger = logging.getLogger(__name__)
router = APIRouter(
    prefix="/logs",
    tags=["logs"],
    dependencies=[Depends(get_current_user)],
)


@router.get("/recent")
async def get_recent_logs(
    limit: int = Query(200, ge=1, le=2000, description="Max entries to return"),
    level: Optional[str] = Query(None, description="Filter by log level (DEBUG, INFO, WARNING, ERROR, CRITICAL)"),
    component: Optional[str] = Query(None, description="Filter by component (engine, scanner, signals, risk, executor)"),
    search: Optional[str] = Query(None, description="Text search in message content"),
):
    """Return recent log entries from the engine's in-memory ring buffer."""
    params: dict = {"limit": limit}
    if level:
        params["level"] = level.upper()
    if component:
        params["component"] = component
    if search:
        params["search"] = search
    return await _send_engine_command("get_logs", params)
