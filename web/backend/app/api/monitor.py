"""
NexusTrader Web — Demo Monitor API (Phase 8H)

Real-time demo trading monitor endpoints.
All data sourced from PaperExecutor via engine commands.
"""
import logging
from fastapi import APIRouter, Depends

from app.api.auth import get_current_user
from app.api.engine import _send_engine_command

logger = logging.getLogger("nexus.api.monitor")
router = APIRouter(prefix="/monitor", tags=["monitor"])


@router.get("/positions")
async def get_monitor_positions(user=Depends(get_current_user)):
    """Active positions with enriched monitor fields."""
    return await _send_engine_command("get_active_positions", {})


@router.get("/portfolio")
async def get_monitor_portfolio(user=Depends(get_current_user)):
    """Portfolio state with heat and margin."""
    return await _send_engine_command("get_portfolio_state", {})


@router.get("/pnl")
async def get_monitor_pnl(user=Depends(get_current_user)):
    """Real-time PnL breakdown."""
    return await _send_engine_command("get_live_pnl", {})


@router.get("/risk")
async def get_monitor_risk(user=Depends(get_current_user)):
    """Risk state including crash defense and circuit breakers."""
    return await _send_engine_command("get_risk_state", {})


@router.get("/trades")
async def get_monitor_trades(user=Depends(get_current_user)):
    """Recent closed trades with R-multiple and regime."""
    return await _send_engine_command("get_recent_trades_monitor", {})
