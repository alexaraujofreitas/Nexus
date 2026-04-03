# ============================================================
# NEXUS TRADER Web — Backtest Router
#
# POST /backtest/start            — launch a backtest job
# GET  /backtest/status/{job_id}  — poll progress
# GET  /backtest/results/{job_id} — completed results
# POST /backtest/cancel/{job_id}  — cancel a running job (Phase 5)
# ============================================================
from __future__ import annotations

import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, Path, Query
from pydantic import BaseModel

from app.auth.dependencies import get_current_user
from app.api.engine import _send_engine_command

logger = logging.getLogger(__name__)
router = APIRouter(
    prefix="/backtest",
    tags=["backtest"],
    dependencies=[Depends(get_current_user)],
)


class BacktestRequest(BaseModel):
    symbols: List[str] = ["BTC/USDT", "ETH/USDT", "SOL/USDT"]
    start_date: str = "2024-01-01"
    end_date: str = "2026-03-01"
    timeframe: str = "30m"
    fee_pct: float = 0.04


@router.post("/start")
async def start_backtest(req: BacktestRequest):
    """Launch a backtest job asynchronously."""
    return await _send_engine_command("start_backtest", {
        "symbols": req.symbols,
        "start_date": req.start_date,
        "end_date": req.end_date,
        "timeframe": req.timeframe,
        "fee_pct": req.fee_pct,
    }, timeout=15)


@router.get("/status/{job_id}")
async def backtest_status(job_id: str = Path(description="Backtest job ID")):
    """Poll backtest progress."""
    return await _send_engine_command("get_backtest_status", {"job_id": job_id})


@router.get("/results/{job_id}")
async def backtest_results(job_id: str = Path(description="Backtest job ID")):
    """Get completed backtest results."""
    return await _send_engine_command("get_backtest_results", {"job_id": job_id})


@router.post("/cancel/{job_id}")
async def cancel_backtest(job_id: str = Path(description="Backtest job ID to cancel")):
    """Cancel a running backtest job."""
    return await _send_engine_command("cancel_backtest", {"job_id": job_id})
