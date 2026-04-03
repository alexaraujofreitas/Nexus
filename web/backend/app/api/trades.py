# ============================================================
# NEXUS TRADER Web — Trades Router
#
# GET /trades/history — paginated trade history
# ============================================================
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, Query

from app.auth.dependencies import get_current_user
from app.api.engine import _send_engine_command

logger = logging.getLogger(__name__)
router = APIRouter(
    prefix="/trades",
    tags=["trades"],
    dependencies=[Depends(get_current_user)],
)


@router.get("/history")
async def trade_history(
    page: int = Query(1, ge=1, description="Page number"),
    per_page: int = Query(50, ge=1, le=200, description="Items per page"),
):
    """Paginated trade history with optional filters."""
    return await _send_engine_command("get_trade_history", {
        "page": page,
        "per_page": per_page,
    })
