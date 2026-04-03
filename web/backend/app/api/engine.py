# ============================================================
# NEXUS TRADER Web — Engine Control Router
#
# Sends commands to the Trading Engine via Redis request/reply.
# All endpoints require authentication.
# ============================================================
from __future__ import annotations

import json
import logging
import uuid

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.auth.dependencies import get_current_user
from app.config import get_settings

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/engine", tags=["engine"], dependencies=[Depends(get_current_user)])


# ── Request Schemas ──────────────────────────────────────────
class EngineCommand(BaseModel):
    action: str   # start_scanner, stop_scanner, close_position, etc.
    params: dict = {}


# ── Redis command helper ─────────────────────────────────────
async def _send_engine_command(action: str, params: dict, timeout: int = 10) -> dict:
    """
    Send a command to the Trading Engine via Redis request/reply pattern.
    RPUSH command onto nexus:engine:commands queue.
    BLPOP reply from nexus:engine:replies:{command_id}.
    """
    settings = get_settings()
    command_id = str(uuid.uuid4())

    r = aioredis.from_url(settings.redis_url, decode_responses=True)
    try:
        # Idempotency: SET NX with 1h TTL
        idem_key = f"nexus:cmd:idem:{command_id}"
        was_set = await r.set(idem_key, "1", nx=True, ex=3600)
        if not was_set:
            return {"status": "duplicate", "command_id": command_id}

        command = {
            "command_id": command_id,
            "action": action,
            "params": params,
        }
        await r.rpush("nexus:engine:commands", json.dumps(command))

        # Wait for reply
        reply_key = f"nexus:engine:replies:{command_id}"
        result = await r.blpop(reply_key, timeout=timeout)

        if result is None:
            return {"status": "timeout", "command_id": command_id}

        return json.loads(result[1])
    finally:
        await r.aclose()


# ── Endpoints ────────────────────────────────────────────────

@router.get("/status")
async def engine_status():
    """Get current engine state from Redis."""
    settings = get_settings()
    r = aioredis.from_url(settings.redis_url, decode_responses=True)
    try:
        state = await r.hgetall("nexus:engine:state")
        return {"status": "ok", "engine": state or {"state": "unknown"}}
    finally:
        await r.aclose()


@router.post("/command")
async def send_command(cmd: EngineCommand):
    """Send a command to the Trading Engine."""
    allowed_actions = {
        "start_scanner", "stop_scanner", "pause_trading", "resume_trading",
        "close_position", "close_all_positions", "refresh_data",
        "get_positions", "get_portfolio", "get_config",
        # Phase 2A additions
        "get_dashboard", "get_crash_defense", "get_scanner_results",
        "get_watchlist", "get_agent_status", "get_signals",
        "get_risk_status", "get_trade_history", "update_config",
        "get_system_health", "trigger_scan", "kill_switch",
        # Phase 3 additions
        "get_ohlcv",
        # Phase 4 additions
        "get_logs", "get_equity_curve", "get_performance_metrics",
        "get_trade_distribution", "get_performance_by_model",
        "start_backtest", "get_backtest_status", "get_backtest_results",
        "get_validation_health", "get_readiness", "get_data_integrity",
        # Phase 5 additions
        "get_drawdown_curve", "get_rolling_metrics", "get_r_distribution",
        "get_duration_analysis", "get_performance_by_regime",
        "get_regime_transitions", "cancel_backtest",
        "get_settings", "update_settings",
    }
    if cmd.action not in allowed_actions:
        raise HTTPException(status_code=400, detail=f"Unknown action: {cmd.action}")

    result = await _send_engine_command(cmd.action, cmd.params)
    return result
