# ============================================================
# NEXUS TRADER Web — Scanner Router
#
# GET  /scanner/results          — last scan cycle results (approved only)
# GET  /scanner/watchlist        — current watchlist + weights
# GET  /scanner/pipeline-status  — Phase 3B: full per-asset pipeline dashboard
# POST /scanner/trigger          — trigger immediate scan
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


@router.get("/pipeline-status")
async def pipeline_status():
    """Return full per-asset pipeline status for all tradable assets.

    Phase 3B: Shows every tradable asset from Asset Management and where
    it stands in the scan → regime → strategy → confluence → risk pipeline.
    Assets that have not been scanned yet appear with status 'Waiting'.

    Response schema:
        {
            "status": "ok",
            "pipeline": [
                {
                    "asset_id": int,
                    "symbol": str,
                    "allocation_weight": float,
                    "price": float | null,
                    "regime": str,
                    "regime_confidence": float,
                    "models_fired": list[str],
                    "models_no_signal": list[str],
                    "score": float,
                    "direction": str,
                    "status": str,  // Eligible | Risk Blocked | No Signal | Regime Filtered | Pre-Filter | Waiting | Error
                    "reason": str,
                    "is_approved": bool,
                    "entry_price": float | null,
                    "stop_loss": float,
                    "take_profit": float,
                    "rr_ratio": float,
                    "position_size_usdt": float,
                    "scanned_at": str,
                    "diagnostics": dict,
                }
            ],
            "summary": {
                "total": int,
                "eligible": int,
                "active_signals": int,
                "blocked": int,
            },
            "scanner_running": bool,
            "last_scan_at": str,
            "source": str,
        }
    """
    return await _send_engine_command("get_pipeline_status", {})


@router.get("/watchlist")
async def watchlist():
    """Return current scanner watchlist with symbol weights."""
    return await _send_engine_command("get_watchlist", {})


@router.post("/trigger")
async def trigger_scan():
    """Trigger an immediate scan cycle."""
    return await _send_engine_command("trigger_scan", {})
