# ============================================================
# NEXUS TRADER Web — Charts Router
#
# GET /charts/ohlcv — OHLCV candlestick data for charting
# ============================================================
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Query

from app.auth.dependencies import get_current_user
from app.api.engine import _send_engine_command

logger = logging.getLogger(__name__)
router = APIRouter(
    prefix="/charts",
    tags=["charts"],
    dependencies=[Depends(get_current_user)],
)


@router.get("/ohlcv")
async def get_ohlcv(
    symbol: str = Query("BTC/USDT", description="Trading pair symbol"),
    timeframe: str = Query("30m", description="Candle timeframe (15m, 30m, 1h, 4h)"),
    limit: int = Query(300, ge=10, le=1000, description="Number of bars to return"),
):
    """Return OHLCV candlestick data for the requested symbol and timeframe."""
    return await _send_engine_command("get_ohlcv", {
        "symbol": symbol,
        "timeframe": timeframe,
        "limit": limit,
    })
