# ============================================================
# NEXUS TRADER Web — Market Data REST Router (Phase 2)
#
# Endpoints:
#   GET /api/v1/market-data/snapshots         — All tradable asset snapshots
#   GET /api/v1/market-data/snapshots/{id}    — Single asset snapshot
#   GET /api/v1/market-data/ohlcv/{id}        — OHLCV bars for an asset
#
# Read path: Redis hot cache first (pipelined HGETALL), PostgreSQL fallback.
# No computation — all values pre-computed by MarketDataService.
# ============================================================
from __future__ import annotations

import json
import logging
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from datetime import datetime, timezone

from app.auth.dependencies import get_current_user
from app.database import get_db, get_async_session_factory
from app.models.trading import Asset, OHLCV

logger = logging.getLogger("nexus.api.market_data")

def _format_utc_z(dt: datetime | None) -> str | None:
    """Format a datetime as ISO 8601 UTC with trailing Z.

    Handles both tz-aware and naive datetimes correctly:
    - tz-aware: converts to UTC, strips tzinfo, formats with Z
    - naive: assumed UTC, formats with Z
    - None: returns None
    """
    if dt is None:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


router = APIRouter(
    prefix="/market-data",
    tags=["market-data"],
    dependencies=[Depends(get_current_user)],
)


def _get_redis():
    """
    Get the Redis instance from the WS manager's connection pattern.
    This is resolved at request time to support test patching.
    """
    import redis.asyncio as aioredis
    from app.config import get_settings
    settings = get_settings()
    return aioredis.from_url(settings.redis_url, decode_responses=True)


async def _snapshot_from_redis(redis: Any, asset_id: int) -> Optional[dict[str, Any]]:
    """Read a single snapshot HASH from Redis."""
    key = f"nexus:snapshot:{asset_id}"
    try:
        raw = await redis.hgetall(key)
        if not raw:
            return None
        return {k: json.loads(v) for k, v in raw.items()}
    except Exception:
        logger.error("Redis HGETALL failed for asset %d", asset_id, exc_info=True)
        return None


async def _snapshots_from_redis_pipeline(
    redis: Any, asset_ids: list[int]
) -> dict[int, Optional[dict[str, Any]]]:
    """Pipelined HGETALL for multiple assets — single Redis round-trip."""
    if not asset_ids:
        return {}
    try:
        pipe = redis.pipeline()
        for aid in asset_ids:
            pipe.hgetall(f"nexus:snapshot:{aid}")
        results = await pipe.execute()

        out: dict[int, Optional[dict[str, Any]]] = {}
        for aid, raw in zip(asset_ids, results):
            if raw:
                out[aid] = {k: json.loads(v) for k, v in raw.items()}
            else:
                out[aid] = None
        return out
    except Exception:
        logger.error("Redis pipeline HGETALL failed", exc_info=True)
        return {aid: None for aid in asset_ids}


async def _ohlcv_from_redis(
    redis: Any, asset_id: int, timeframe: str, limit: int
) -> Optional[list[dict[str, Any]]]:
    """Read OHLCV bars from Redis sorted set."""
    key = f"nexus:ohlcv:{timeframe}:{asset_id}"
    try:
        if limit > 0:
            raw = await redis.zrange(key, -limit, -1)
        else:
            raw = await redis.zrange(key, 0, -1)

        if not raw:
            return None

        bars = []
        for r in raw:
            b = json.loads(r)
            ts_iso = datetime.fromtimestamp(b[0] / 1000, tz=timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )
            bars.append({
                "timestamp": ts_iso,
                "open": b[1],
                "high": b[2],
                "low": b[3],
                "close": b[4],
                "volume": b[5],
            })
        return bars
    except Exception:
        logger.error("Redis OHLCV read failed for asset %d %s", asset_id, timeframe, exc_info=True)
        return None


# ── Endpoints ──────────────────────────────────────────────


@router.get("/snapshots")
async def get_snapshots(
    exchange_id: Optional[int] = Query(None, description="Exchange ID (defaults to active)"),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """
    Return latest snapshots for all tradable assets.

    Read path: Redis pipelined HGETALL first, PostgreSQL fallback per asset.
    """
    # Get tradable assets
    query = select(Asset).where(Asset.is_tradable.is_(True))
    if exchange_id is not None:
        query = query.where(Asset.exchange_id == exchange_id)
    result = await db.execute(query)
    assets = list(result.scalars().all())

    if not assets:
        return {"snapshots": [], "count": 0, "exchange_id": exchange_id}

    # Determine exchange_id from first asset if not provided
    effective_exchange_id = exchange_id or (assets[0].exchange_id if assets else None)

    # Pipeline HGETALL for all assets
    redis = _get_redis()
    try:
        asset_ids = [a.id for a in assets]
        redis_snapshots = await _snapshots_from_redis_pipeline(redis, asset_ids)

        snapshots = []
        for asset in assets:
            redis_snap = redis_snapshots.get(asset.id)
            if redis_snap:
                data_source = "redis"
                snapshot = redis_snap
            elif asset.market_snapshot:
                data_source = "db"
                snapshot = asset.market_snapshot
            else:
                data_source = "db"
                snapshot = {}

            snapshots.append({
                "asset_id": asset.id,
                "symbol": asset.symbol,
                "base_currency": asset.base_currency,
                "is_tradable": asset.is_tradable,
                "allocation_weight": asset.allocation_weight,
                "snapshot": snapshot,
                "snapshot_updated_at": _format_utc_z(asset.snapshot_updated_at),
                "data_source": data_source,
            })

        return {
            "snapshots": snapshots,
            "count": len(snapshots),
            "exchange_id": effective_exchange_id,
        }
    finally:
        await redis.aclose()


@router.get("/snapshots/{asset_id}")
async def get_snapshot(
    asset_id: int,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """
    Return snapshot for a single asset.

    Read path: Redis HGETALL first, PostgreSQL fallback.
    """
    # Verify asset exists
    result = await db.execute(select(Asset).where(Asset.id == asset_id))
    asset = result.scalar_one_or_none()
    if asset is None:
        raise HTTPException(status_code=404, detail="Asset not found")

    redis = _get_redis()
    try:
        redis_snap = await _snapshot_from_redis(redis, asset_id)

        if redis_snap:
            data_source = "redis"
            snapshot = redis_snap
        elif asset.market_snapshot:
            data_source = "db"
            snapshot = asset.market_snapshot
        else:
            data_source = "db"
            snapshot = {}

        return {
            "asset_id": asset.id,
            "symbol": asset.symbol,
            "base_currency": asset.base_currency,
            "is_tradable": asset.is_tradable,
            "allocation_weight": asset.allocation_weight,
            "snapshot": snapshot,
            "snapshot_updated_at": _format_utc_z(asset.snapshot_updated_at),
            "data_source": data_source,
        }
    finally:
        await redis.aclose()


@router.get("/ohlcv/{asset_id}")
async def get_ohlcv(
    asset_id: int,
    timeframe: str = Query("1h", description="Timeframe: '1h' or '1d'"),
    limit: int = Query(None, ge=10, le=500, description="Number of bars (10-500)"),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """
    Return OHLCV bars for an asset.

    Read path: Redis sorted set first, PostgreSQL fallback.
    """
    # Validate timeframe
    if timeframe not in ("1h", "1d"):
        raise HTTPException(
            status_code=422,
            detail=f"Invalid timeframe '{timeframe}'. Allowed: '1h', '1d'",
        )

    # Set default limit based on timeframe
    if limit is None:
        limit = 168 if timeframe == "1h" else 30

    # Verify asset exists
    result = await db.execute(select(Asset).where(Asset.id == asset_id))
    asset = result.scalar_one_or_none()
    if asset is None:
        raise HTTPException(status_code=404, detail="Asset not found")

    redis = _get_redis()
    try:
        redis_bars = await _ohlcv_from_redis(redis, asset_id, timeframe, limit)

        if redis_bars:
            return {
                "asset_id": asset.id,
                "symbol": asset.symbol,
                "timeframe": timeframe,
                "bars": redis_bars,
                "count": len(redis_bars),
                "data_source": "redis",
            }
    finally:
        await redis.aclose()

    # Fallback: PostgreSQL
    result = await db.execute(
        select(OHLCV)
        .where(OHLCV.asset_id == asset_id, OHLCV.timeframe == timeframe)
        .order_by(OHLCV.timestamp.desc())
        .limit(limit)
    )
    db_bars = list(result.scalars().all())

    bars = [
        {
            "timestamp": (
                b.timestamp.strftime("%Y-%m-%dT%H:%M:%SZ")
                if b.timestamp.tzinfo
                else b.timestamp.strftime("%Y-%m-%dT%H:%M:%SZ")
            ),
            "open": b.open,
            "high": b.high,
            "low": b.low,
            "close": b.close,
            "volume": b.volume,
        }
        for b in reversed(db_bars)  # oldest first
    ]

    return {
        "asset_id": asset.id,
        "symbol": asset.symbol,
        "timeframe": timeframe,
        "bars": bars,
        "count": len(bars),
        "data_source": "db",
    }
