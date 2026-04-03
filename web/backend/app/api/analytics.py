# ============================================================
# NEXUS TRADER Web — Analytics Router
#
# GET /analytics/equity-curve         — capital over time
# GET /analytics/metrics              — aggregate performance metrics
# GET /analytics/trade-distribution   — PnL histogram buckets
# GET /analytics/by-model             — per-model breakdown
# GET /analytics/drawdown-curve       — drawdown time-series (Phase 5)
# GET /analytics/rolling-metrics      — rolling WR/PF/AvgR (Phase 5)
# GET /analytics/r-distribution       — R-multiple histogram (Phase 5)
# GET /analytics/duration-analysis    — duration vs outcome (Phase 5)
# GET /analytics/by-regime            — per-regime performance (Phase 5)
# GET /analytics/regime-transitions   — regime transition matrix (Phase 5)
# ============================================================
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Query

from app.auth.dependencies import get_current_user
from app.api.engine import _send_engine_command

logger = logging.getLogger(__name__)
router = APIRouter(
    prefix="/analytics",
    tags=["analytics"],
    dependencies=[Depends(get_current_user)],
)


# ── Phase 4 endpoints ───────────────────────────────────────

@router.get("/equity-curve")
async def equity_curve():
    """Capital time-series from closed trades (cumulative PnL)."""
    return await _send_engine_command("get_equity_curve", {})


@router.get("/metrics")
async def performance_metrics():
    """Aggregate performance metrics: WR, PF, DD, Sharpe, etc."""
    return await _send_engine_command("get_performance_metrics", {})


@router.get("/trade-distribution")
async def trade_distribution():
    """PnL distribution histogram with bucket counts."""
    return await _send_engine_command("get_trade_distribution", {})


@router.get("/by-model")
async def performance_by_model(
    sort: str = Query("pf", description="Sort field: name, trades, win_rate, pf, avg_r"),
    order: str = Query("desc", description="Sort order: asc or desc"),
    regime: str | None = Query(None, description="Filter by regime name"),
    asset: str | None = Query(None, description="Filter by asset symbol"),
):
    """Per-model breakdown with optional sort and filter."""
    params = {"sort": sort, "order": order}
    if regime:
        params["regime"] = regime
    if asset:
        params["asset"] = asset
    return await _send_engine_command("get_performance_by_model", params)


# ── Phase 5 endpoints ───────────────────────────────────────

@router.get("/drawdown-curve")
async def drawdown_curve():
    """Drawdown time-series: peak-to-trough percentage over time."""
    return await _send_engine_command("get_drawdown_curve", {})


@router.get("/rolling-metrics")
async def rolling_metrics(
    window: int = Query(20, ge=5, le=200, description="Rolling window size (trade count)"),
):
    """Rolling performance metrics over a configurable window."""
    return await _send_engine_command("get_rolling_metrics", {"window": window})


@router.get("/r-distribution")
async def r_distribution():
    """R-multiple distribution histogram with expectancy stats."""
    return await _send_engine_command("get_r_distribution", {})


@router.get("/duration-analysis")
async def duration_analysis():
    """Trade duration vs outcome analysis (bucketed)."""
    return await _send_engine_command("get_duration_analysis", {})


@router.get("/by-regime")
async def performance_by_regime():
    """Performance breakdown by market regime."""
    return await _send_engine_command("get_performance_by_regime", {})


@router.get("/regime-transitions")
async def regime_transitions():
    """Regime transition matrix: from → to counts and avg PnL."""
    return await _send_engine_command("get_regime_transitions", {})
