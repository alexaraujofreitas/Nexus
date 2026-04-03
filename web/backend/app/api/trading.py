# ============================================================
# NEXUS TRADER Web — Trading Router
#
# GET  /trading/positions — open positions with real-time data
# POST /trading/close     — close a single position by symbol
# POST /trading/close-all — close all open positions
# ============================================================
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.auth.dependencies import get_current_user
from app.api.engine import _send_engine_command

logger = logging.getLogger(__name__)
router = APIRouter(
    prefix="/trading",
    tags=["trading"],
    dependencies=[Depends(get_current_user)],
)


class ClosePositionRequest(BaseModel):
    symbol: str


@router.get("/positions")
async def get_positions():
    """Return all open positions with current PnL data."""
    return await _send_engine_command("get_positions", {})


@router.post("/close")
async def close_position(req: ClosePositionRequest):
    """Close a single open position by symbol."""
    return await _send_engine_command("close_position", {"symbol": req.symbol})


@router.post("/close-all")
async def close_all_positions():
    """Close all open positions."""
    return await _send_engine_command("close_all_positions", {})
