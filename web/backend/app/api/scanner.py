# ============================================================
# NEXUS TRADER Web — Scanner Router
#
# GET  /scanner/results   — last scan cycle results
# GET  /scanner/watchlist  — current watchlist + weights
# POST /scanner/trigger    — trigger immediate scan
# ============================================================
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends

from app.auth.dependencies import get_current_user
from app.api.engine import _send_engine_command

logger = logging.getLogger(__name__)
router = APIRouter(
    prefix="/scanner",
    tags=["scanner"],
    dependencies=[Depends(get_current_user)],
)


@router.get("/results")
async def scanner_results():
    """Return results from the most recent scan cycle."""
    return await _send_engine_command("get_scanner_results", {})


@router.get("/watchlist")
async def watchlist():
    """Return current scanner watchlist with symbol weights."""
    return await _send_engine_command("get_watchlist", {})


@router.post("/trigger")
async def trigger_scan():
    """Trigger an immediate scan cycle."""
    return await _send_engine_command("trigger_scan", {})
