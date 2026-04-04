# ============================================================
# NEXUS TRADER Web — Exchange Management Router
#
# Full CRUD for exchange connections + credential vault integration.
# Desktop parity: exchange_page.py (PySide6)
#
# GET    /exchanges/                         — list all configured exchanges
# GET    /exchanges/{id}                     — get single exchange (masked creds)
# POST   /exchanges/                         — add new exchange
# PUT    /exchanges/{id}                     — update exchange
# DELETE /exchanges/{id}                     — remove exchange
# POST   /exchanges/{id}/activate            — set as active exchange
# POST   /exchanges/{id}/deactivate          — deactivate exchange
# POST   /exchanges/test-connection          — test exchange connection
# GET    /exchanges/supported                — list supported exchanges
# GET    /exchanges/{id}/assets              — list assets (paginated, filterable)
# POST   /exchanges/{id}/sync-assets         — sync assets from exchange (DB upsert)
# PATCH  /exchanges/{id}/assets/{asset_id}   — update single asset (tradable/weight)
# PATCH  /exchanges/{id}/assets/bulk         — bulk update assets (tradable/weight)
# GET    /exchanges/{id}/assets/tradable     — list tradable assets only
# ============================================================
from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select, update as sa_update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.auth.dependencies import get_current_user
from app.database import get_db
from app.models.trading import Exchange, Asset
from app.services.vault import get_vault
from app.api.engine import _send_engine_command

logger = logging.getLogger(__name__)
router = APIRouter(
    prefix="/exchanges",
    tags=["exchanges"],
    dependencies=[Depends(get_current_user)],
)


# ── Supported Exchanges (matches desktop SUPPORTED_EXCHANGES) ──
SUPPORTED_EXCHANGES = {
    "kucoin":   {"name": "KuCoin",    "has_sandbox": False, "has_demo": False, "needs_passphrase": True},
    "binance":  {"name": "Binance",   "has_sandbox": True,  "has_demo": False, "needs_passphrase": False},
    "bybit":    {"name": "Bybit",     "has_sandbox": True,  "has_demo": True,  "needs_passphrase": False},
    "coinbase": {"name": "Coinbase",  "has_sandbox": False, "has_demo": False, "needs_passphrase": False},
    "kraken":   {"name": "Kraken",    "has_sandbox": False, "has_demo": False, "needs_passphrase": False},
    "okx":      {"name": "OKX",       "has_sandbox": True,  "has_demo": False, "needs_passphrase": True},
}

MODES = {"live", "sandbox", "demo"}


# ── Request / Response Schemas ──────────────────────────────
class ExchangeCreate(BaseModel):
    name: str
    exchange_id: str
    api_key: Optional[str] = None
    api_secret: Optional[str] = None
    passphrase: Optional[str] = None
    mode: str = "live"  # live | sandbox | demo


class ExchangeUpdate(BaseModel):
    name: Optional[str] = None
    api_key: Optional[str] = None
    api_secret: Optional[str] = None
    passphrase: Optional[str] = None
    mode: Optional[str] = None


class ConnectionTest(BaseModel):
    exchange_id: str
    api_key: Optional[str] = None
    api_secret: Optional[str] = None
    passphrase: Optional[str] = None
    mode: str = "live"


# ── Phase 1: Asset Management Schemas ──────────────────────

class AssetUpdate(BaseModel):
    """Update a single asset's tradable status or allocation weight."""
    is_tradable: Optional[bool] = None
    allocation_weight: Optional[float] = Field(None, ge=0.0, le=10.0)


class BulkAssetUpdate(BaseModel):
    """Bulk update assets — same value applied to all listed IDs."""
    asset_ids: list[int] = Field(..., min_length=1, max_length=100)
    is_tradable: Optional[bool] = None
    allocation_weight: Optional[float] = Field(None, ge=0.0, le=10.0)


# ── Helpers ─────────────────────────────────────────────────

def _asset_to_dict(a: Asset) -> dict:
    """Serialize Asset ORM model to API response dict."""
    return {
        "id": a.id,
        "symbol": a.symbol,
        "base_currency": a.base_currency,
        "quote_currency": a.quote_currency,
        "price_precision": a.price_precision,
        "amount_precision": a.amount_precision,
        "min_amount": a.min_amount,
        "min_cost": a.min_cost,
        "is_active": a.is_active,
        "is_tradable": a.is_tradable,
        "allocation_weight": a.allocation_weight,
        "market_snapshot": a.market_snapshot,
        "snapshot_updated_at": (
            a.snapshot_updated_at.isoformat() if a.snapshot_updated_at else None
        ),
        "last_updated": a.last_updated.isoformat() if a.last_updated else None,
    }


def _mask_credential(value: Optional[str]) -> str:
    """Mask a credential for API response. Never expose plaintext."""
    if not value:
        return ""
    vault = get_vault()
    if vault.is_encrypted(value):
        try:
            plain = vault.decrypt(value)
            return vault.mask(plain)
        except Exception:
            return "****[error]"
    return vault.mask(value)


def _exchange_to_dict(ex: Exchange) -> dict:
    """Serialize Exchange ORM model to API response dict with masked credentials."""
    return {
        "id": ex.id,
        "name": ex.name,
        "exchange_id": ex.exchange_id,
        "api_key_masked": _mask_credential(ex.api_key_encrypted),
        "api_secret_masked": _mask_credential(ex.api_secret_encrypted),
        "passphrase_masked": _mask_credential(ex.api_passphrase_encrypted),
        "has_api_key": bool(ex.api_key_encrypted),
        "has_api_secret": bool(ex.api_secret_encrypted),
        "has_passphrase": bool(ex.api_passphrase_encrypted),
        "sandbox_mode": ex.sandbox_mode,
        "demo_mode": ex.demo_mode,
        "mode": ex.mode,
        "is_active": ex.is_active,
        "testnet_url": ex.testnet_url,
        "created_at": ex.created_at.isoformat() if ex.created_at else None,
        "updated_at": ex.updated_at.isoformat() if ex.updated_at else None,
    }


def _encrypt_if_present(value: Optional[str]) -> Optional[str]:
    """Encrypt a credential value if non-empty and not already encrypted."""
    if not value:
        return None
    vault = get_vault()
    if vault.is_encrypted(value):
        return value
    return vault.encrypt(value)


# ── Endpoints ───────────────────────────────────────────────

@router.get("/supported")
async def list_supported():
    """List all supported exchanges with their capabilities."""
    return {
        "exchanges": [
            {"exchange_id": eid, **info}
            for eid, info in SUPPORTED_EXCHANGES.items()
        ]
    }


@router.get("/")
async def list_exchanges(db: AsyncSession = Depends(get_db)):
    """List all configured exchanges with masked credentials."""
    result = await db.execute(select(Exchange).order_by(Exchange.created_at.desc()))
    exchanges = result.scalars().all()
    return {"exchanges": [_exchange_to_dict(ex) for ex in exchanges]}


@router.get("/{exchange_db_id}")
async def get_exchange(exchange_db_id: int, db: AsyncSession = Depends(get_db)):
    """Get a single exchange by database ID."""
    result = await db.execute(select(Exchange).where(Exchange.id == exchange_db_id))
    ex = result.scalar_one_or_none()
    if not ex:
        raise HTTPException(status_code=404, detail="Exchange not found")
    return _exchange_to_dict(ex)


@router.post("/")
async def create_exchange(body: ExchangeCreate, db: AsyncSession = Depends(get_db)):
    """Add a new exchange connection."""
    if body.exchange_id not in SUPPORTED_EXCHANGES:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported exchange: {body.exchange_id}. "
                   f"Supported: {', '.join(sorted(SUPPORTED_EXCHANGES))}",
        )
    if body.mode not in MODES:
        raise HTTPException(status_code=400, detail=f"Invalid mode: {body.mode}. Must be: {', '.join(sorted(MODES))}")

    # Validate mode compatibility
    info = SUPPORTED_EXCHANGES[body.exchange_id]
    if body.mode == "sandbox" and not info["has_sandbox"]:
        raise HTTPException(status_code=400, detail=f"{info['name']} does not support sandbox/testnet mode")
    if body.mode == "demo" and not info["has_demo"]:
        raise HTTPException(status_code=400, detail=f"Demo trading is only available on Bybit")

    ex = Exchange(
        name=body.name,
        exchange_id=body.exchange_id,
        api_key_encrypted=_encrypt_if_present(body.api_key),
        api_secret_encrypted=_encrypt_if_present(body.api_secret),
        api_passphrase_encrypted=_encrypt_if_present(body.passphrase),
        sandbox_mode=(body.mode == "sandbox"),
        demo_mode=(body.mode == "demo"),
        is_active=False,
    )
    db.add(ex)
    await db.flush()
    await db.refresh(ex)

    logger.info("Exchange created: %s (%s, mode=%s)", ex.name, ex.exchange_id, body.mode)
    return _exchange_to_dict(ex)


@router.put("/{exchange_db_id}")
async def update_exchange(exchange_db_id: int, body: ExchangeUpdate, db: AsyncSession = Depends(get_db)):
    """Update an existing exchange configuration."""
    result = await db.execute(select(Exchange).where(Exchange.id == exchange_db_id))
    ex = result.scalar_one_or_none()
    if not ex:
        raise HTTPException(status_code=404, detail="Exchange not found")

    if body.name is not None:
        ex.name = body.name

    if body.mode is not None:
        if body.mode not in MODES:
            raise HTTPException(status_code=400, detail=f"Invalid mode: {body.mode}")
        info = SUPPORTED_EXCHANGES.get(ex.exchange_id, {})
        if body.mode == "sandbox" and not info.get("has_sandbox"):
            raise HTTPException(status_code=400, detail=f"{ex.name} does not support sandbox mode")
        if body.mode == "demo" and not info.get("has_demo"):
            raise HTTPException(status_code=400, detail="Demo trading is only available on Bybit")
        ex.sandbox_mode = (body.mode == "sandbox")
        ex.demo_mode = (body.mode == "demo")

    # Only update credentials if provided and not masked placeholder
    if body.api_key and "\u2022" not in body.api_key:
        ex.api_key_encrypted = _encrypt_if_present(body.api_key)
    if body.api_secret and "\u2022" not in body.api_secret:
        ex.api_secret_encrypted = _encrypt_if_present(body.api_secret)
    if body.passphrase and "\u2022" not in body.passphrase:
        ex.api_passphrase_encrypted = _encrypt_if_present(body.passphrase)

    ex.updated_at = datetime.utcnow()
    await db.flush()
    await db.refresh(ex)

    logger.info("Exchange updated: %s (id=%d)", ex.name, ex.id)
    return _exchange_to_dict(ex)


@router.delete("/{exchange_db_id}")
async def delete_exchange(exchange_db_id: int, db: AsyncSession = Depends(get_db)):
    """Remove an exchange and its assets."""
    result = await db.execute(select(Exchange).where(Exchange.id == exchange_db_id))
    ex = result.scalar_one_or_none()
    if not ex:
        raise HTTPException(status_code=404, detail="Exchange not found")
    if ex.is_active:
        raise HTTPException(status_code=400, detail="Cannot delete the active exchange. Deactivate first.")

    name = ex.name
    await db.delete(ex)
    logger.info("Exchange deleted: %s (id=%d)", name, exchange_db_id)
    return {"status": "deleted", "name": name}


@router.post("/{exchange_db_id}/activate")
async def activate_exchange(exchange_db_id: int, db: AsyncSession = Depends(get_db)):
    """Set an exchange as the active exchange. Deactivates all others."""
    result = await db.execute(select(Exchange).where(Exchange.id == exchange_db_id))
    ex = result.scalar_one_or_none()
    if not ex:
        raise HTTPException(status_code=404, detail="Exchange not found")

    # Deactivate all exchanges first
    await db.execute(sa_update(Exchange).values(is_active=False))
    # Activate this one
    ex.is_active = True
    ex.updated_at = datetime.utcnow()
    await db.flush()

    # Notify engine to reload exchange connection
    try:
        await _send_engine_command("exchange.load_active", {"exchange_id": ex.id}, timeout=15)
    except Exception as e:
        logger.warning("Engine reload after activation failed (non-blocking): %s", e)

    logger.info("Exchange activated: %s (id=%d, mode=%s)", ex.name, ex.id, ex.mode)
    return {"status": "activated", "name": ex.name, "mode": ex.mode}


@router.post("/{exchange_db_id}/deactivate")
async def deactivate_exchange(exchange_db_id: int, db: AsyncSession = Depends(get_db)):
    """Deactivate an exchange."""
    result = await db.execute(select(Exchange).where(Exchange.id == exchange_db_id))
    ex = result.scalar_one_or_none()
    if not ex:
        raise HTTPException(status_code=404, detail="Exchange not found")

    ex.is_active = False
    ex.updated_at = datetime.utcnow()
    await db.flush()

    logger.info("Exchange deactivated: %s (id=%d)", ex.name, ex.id)
    return {"status": "deactivated", "name": ex.name}


@router.post("/test-connection")
async def test_connection(body: ConnectionTest):
    """
    Test exchange connection directly via CCXT.

    Creates a temporary CCXT exchange instance, loads markets, and
    optionally tests API credentials by fetching balance.
    Returns market count and USDT balance on success.

    Runs in the API process (not engine) because:
    - Credentials are plaintext here (before encryption)
    - Engine process does not have the vault encryption key
    - Avoids Redis round-trip for a stateless operation
    """
    if body.exchange_id not in SUPPORTED_EXCHANGES:
        raise HTTPException(status_code=400, detail=f"Unsupported exchange: {body.exchange_id}")

    import asyncio
    import ccxt

    info = SUPPORTED_EXCHANGES[body.exchange_id]
    mode_label = body.mode.upper()

    # Build CCXT exchange config
    exchange_class = getattr(ccxt, body.exchange_id, None)
    if exchange_class is None:
        return {"status": "error", "error": f"CCXT does not support '{body.exchange_id}'"}

    ccxt_config: dict = {"enableRateLimit": True, "timeout": 15000}

    # Credentials
    if body.api_key:
        ccxt_config["apiKey"] = body.api_key
    if body.api_secret:
        ccxt_config["secret"] = body.api_secret
    if body.passphrase:
        ccxt_config["password"] = body.passphrase

    # Mode-specific configuration
    if body.mode == "sandbox":
        ccxt_config["sandbox"] = True
        mode_label = "TESTNET"

    exchange = exchange_class(ccxt_config)

    # Bybit Demo: apply demo trading URLs AFTER construction
    # CCXT stores URL templates — must use enable_demo_trading() or
    # swap the 'api' URL set with the 'demotrading' URL set.
    if body.mode == "demo" and body.exchange_id == "bybit":
        mode_label = "DEMO"
        if hasattr(exchange, "enable_demo_trading"):
            exchange.enable_demo_trading(True)
            logger.info("Bybit demo trading enabled via enable_demo_trading()")
        else:
            demo_urls = exchange.urls.get("demotrading")
            if demo_urls:
                exchange.urls["api"] = demo_urls
                logger.info("Bybit demo trading URLs applied via demotrading URL map")
            else:
                logger.warning("Bybit demotrading URL map not found — falling back to manual override")
                exchange.urls["api"] = {
                    "public": "https://api-demo.bybit.com",
                    "private": "https://api-demo.bybit.com",
                }

    try:
        # Step 1: Load markets (public — no credentials needed)
        loop = asyncio.get_event_loop()
        markets = await loop.run_in_executor(None, exchange.load_markets)
        market_count = len(markets)

        result = {
            "status": "ok",
            "markets": market_count,
            "mode_label": mode_label,
        }

        # Step 2: Test credentials by fetching balance (if keys provided)
        if body.api_key and body.api_secret:
            try:
                balance = await loop.run_in_executor(None, exchange.fetch_balance)
                usdt_free = 0.0
                if "USDT" in balance:
                    usdt_free = float(balance["USDT"].get("free", 0) or 0)
                elif "free" in balance and "USDT" in balance["free"]:
                    usdt_free = float(balance["free"]["USDT"] or 0)
                result["balance_usdt"] = round(usdt_free, 2)
            except ccxt.AuthenticationError as auth_err:
                return {
                    "status": "error",
                    "error": f"Authentication failed: {str(auth_err)[:200]}",
                    "markets": market_count,
                    "mode_label": mode_label,
                }
            except ccxt.PermissionDenied as perm_err:
                return {
                    "status": "error",
                    "error": f"Permission denied: {str(perm_err)[:200]}. Check API key permissions.",
                    "markets": market_count,
                    "mode_label": mode_label,
                }
            except Exception as bal_err:
                # Markets loaded but balance failed — partial success
                result["balance_error"] = str(bal_err)[:200]

        return result

    except ccxt.NetworkError as net_err:
        return {
            "status": "error",
            "error": f"Network error: {str(net_err)[:200]}. Check VPN/firewall.",
        }
    except ccxt.ExchangeNotAvailable as na_err:
        return {
            "status": "error",
            "error": f"Exchange unavailable: {str(na_err)[:200]}",
        }
    except ccxt.ExchangeError as ex_err:
        return {
            "status": "error",
            "error": f"Exchange error: {str(ex_err)[:200]}",
        }
    except Exception as e:
        logger.error("test_connection failed for %s: %s", body.exchange_id, e)
        return {
            "status": "error",
            "error": f"Connection failed: {str(e)[:200]}",
        }
    finally:
        try:
            exchange.close()
        except Exception:
            pass


@router.get("/{exchange_db_id}/assets/tradable")
async def list_tradable_assets(
    exchange_db_id: int,
    db: AsyncSession = Depends(get_db),
):
    """
    List only tradable assets for an exchange.

    Uses the ix_assets_tradable partial index. This endpoint will be consumed
    by the engine in Phase 3 to get the tradable universe.
    """
    result = await db.execute(select(Exchange).where(Exchange.id == exchange_db_id))
    if not result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Exchange not found")

    stmt = (
        select(Asset)
        .where(Asset.exchange_id == exchange_db_id, Asset.is_tradable.is_(True))
        .order_by(Asset.symbol)
    )
    result = await db.execute(stmt)
    assets = result.scalars().all()

    return {
        "symbols": [a.symbol for a in assets],
        "assets": [
            {
                "id": a.id,
                "symbol": a.symbol,
                "allocation_weight": a.allocation_weight,
                "exchange_id": a.exchange_id,
            }
            for a in assets
        ],
        "count": len(assets),
    }


@router.get("/{exchange_db_id}/assets")
async def list_assets(
    exchange_db_id: int,
    quote: Optional[str] = None,
    search: Optional[str] = None,
    is_tradable: Optional[bool] = None,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
):
    """List assets for an exchange with pagination, filtering, and new Phase 1 fields."""
    # Verify exchange exists
    result = await db.execute(select(Exchange).where(Exchange.id == exchange_db_id))
    if not result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Exchange not found")

    # Build base WHERE clause (shared between count and data queries)
    where_clauses = [Asset.exchange_id == exchange_db_id]
    if quote:
        where_clauses.append(Asset.quote_currency == quote.upper())
    if search:
        where_clauses.append(Asset.symbol.ilike(f"%{search}%"))
    if is_tradable is not None:
        where_clauses.append(Asset.is_tradable.is_(is_tradable))

    # Total count (before pagination)
    count_stmt = select(func.count(Asset.id)).where(*where_clauses)
    total = (await db.execute(count_stmt)).scalar_one()

    # Paginated data
    data_stmt = (
        select(Asset)
        .where(*where_clauses)
        .order_by(Asset.symbol)
        .offset(offset)
        .limit(limit)
    )
    result = await db.execute(data_stmt)
    assets = result.scalars().all()

    return {
        "assets": [_asset_to_dict(a) for a in assets],
        "count": len(assets),
        "total": total,
        "offset": offset,
        "limit": limit,
    }


@router.post("/{exchange_db_id}/sync-assets")
async def sync_assets(exchange_db_id: int, db: AsyncSession = Depends(get_db)):
    """
    Sync assets from the active exchange via engine command.

    Fetches tradable markets from the exchange and upserts them into the
    assets table. Uses INSERT ... ON CONFLICT to preserve user-managed
    fields (is_tradable, allocation_weight) and Phase 2-owned fields
    (market_snapshot, snapshot_updated_at). Only exchange-sourced fields
    are updated on conflict.
    """
    result = await db.execute(select(Exchange).where(Exchange.id == exchange_db_id))
    ex = result.scalar_one_or_none()
    if not ex:
        raise HTTPException(status_code=404, detail="Exchange not found")
    if not ex.is_active:
        raise HTTPException(status_code=400, detail="Can only sync assets from the active exchange")

    engine_result = await _send_engine_command(
        "exchange.sync_assets",
        {"exchange_db_id": exchange_db_id},
        timeout=30,
    )

    if isinstance(engine_result, dict) and engine_result.get("status") == "timeout":
        raise HTTPException(status_code=504, detail="Asset sync timed out")

    if not isinstance(engine_result, dict) or engine_result.get("status") != "ok":
        raise HTTPException(
            status_code=502,
            detail=f"Engine sync failed: {engine_result}",
        )

    raw_assets = engine_result.get("assets", [])
    if not raw_assets:
        return {"status": "ok", "count": 0, "upserted": 0, "preserved_tradable": 0}

    # Count tradable assets BEFORE upsert so we can report preservation
    pre_tradable_result = await db.execute(
        select(func.count(Asset.id)).where(
            Asset.exchange_id == exchange_db_id,
            Asset.is_tradable.is_(True),
        )
    )
    pre_tradable_count = pre_tradable_result.scalar_one()

    # Batch upsert — preserve is_tradable, allocation_weight,
    # market_snapshot, snapshot_updated_at on conflict.
    now = datetime.utcnow()
    upserted = 0

    for raw in raw_assets:
        # Engine returns varied key names; normalise
        symbol = raw.get("symbol", "")
        if not symbol:
            continue

        values = {
            "exchange_id": exchange_db_id,
            "symbol": symbol,
            "base_currency": raw.get("base_currency", raw.get("base", "")),
            "quote_currency": raw.get("quote_currency", raw.get("quote", "")),
            "min_amount": raw.get("min_amount"),
            "min_cost": raw.get("min_cost"),
            "price_precision": raw.get("price_precision", 8),
            "amount_precision": raw.get("amount_precision", 8),
            "is_active": raw.get("is_active", True),
            "last_updated": now,
        }

        stmt = pg_insert(Asset).values(**values)
        stmt = stmt.on_conflict_do_update(
            constraint="uq_asset_exchange_symbol",
            set_={
                # Only exchange-sourced fields — NEVER overwrite user/Phase 2 fields
                "base_currency": stmt.excluded.base_currency,
                "quote_currency": stmt.excluded.quote_currency,
                "min_amount": stmt.excluded.min_amount,
                "min_cost": stmt.excluded.min_cost,
                "price_precision": stmt.excluded.price_precision,
                "amount_precision": stmt.excluded.amount_precision,
                "is_active": stmt.excluded.is_active,
                "last_updated": stmt.excluded.last_updated,
            },
        )
        await db.execute(stmt)
        upserted += 1

    await db.flush()

    # Count tradable assets AFTER upsert
    post_tradable_result = await db.execute(
        select(func.count(Asset.id)).where(
            Asset.exchange_id == exchange_db_id,
            Asset.is_tradable.is_(True),
        )
    )
    post_tradable_count = post_tradable_result.scalar_one()

    return {
        "status": "ok",
        "count": len(raw_assets),
        "upserted": upserted,
        "preserved_tradable": post_tradable_count,
    }


@router.patch("/{exchange_db_id}/assets/bulk")
async def bulk_update_assets(
    exchange_db_id: int,
    body: BulkAssetUpdate,
    db: AsyncSession = Depends(get_db),
):
    """
    Bulk update assets — same value applied to all listed IDs.

    All-or-nothing transaction: rejects entire request if any asset_id
    is not found or does not belong to the specified exchange.
    """
    # Verify exchange exists
    result = await db.execute(select(Exchange).where(Exchange.id == exchange_db_id))
    if not result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Exchange not found")

    # Validate at least one field is being updated
    if body.is_tradable is None and body.allocation_weight is None:
        raise HTTPException(
            status_code=422,
            detail="At least one of is_tradable or allocation_weight must be provided",
        )

    # Fetch all requested assets and validate ownership
    stmt = select(Asset).where(
        Asset.id.in_(body.asset_ids),
        Asset.exchange_id == exchange_db_id,
    )
    result = await db.execute(stmt)
    found_assets = result.scalars().all()
    found_ids = {a.id for a in found_assets}

    missing_ids = [aid for aid in body.asset_ids if aid not in found_ids]
    if missing_ids:
        raise HTTPException(
            status_code=404,
            detail={
                "message": "Asset IDs not found or not owned by this exchange",
                "missing": missing_ids,
            },
        )

    # Build update values
    update_values: dict = {}
    if body.is_tradable is not None:
        update_values["is_tradable"] = body.is_tradable
    if body.allocation_weight is not None:
        update_values["allocation_weight"] = body.allocation_weight

    # Single UPDATE for all matching IDs
    await db.execute(
        sa_update(Asset)
        .where(Asset.id.in_(body.asset_ids), Asset.exchange_id == exchange_db_id)
        .values(**update_values)
    )
    await db.flush()

    return {
        "updated": len(body.asset_ids),
        "asset_ids": body.asset_ids,
    }


@router.patch("/{exchange_db_id}/assets/{asset_id}")
async def update_asset(
    exchange_db_id: int,
    asset_id: int,
    body: AssetUpdate,
    db: AsyncSession = Depends(get_db),
):
    """
    Update a single asset's tradable status or allocation weight.

    Validates exchange ownership — rejects if the asset belongs to a
    different exchange than the route parameter.
    """
    # Verify exchange exists
    result = await db.execute(select(Exchange).where(Exchange.id == exchange_db_id))
    if not result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Exchange not found")

    # Validate at least one field is being updated
    if body.is_tradable is None and body.allocation_weight is None:
        raise HTTPException(
            status_code=422,
            detail="At least one of is_tradable or allocation_weight must be provided",
        )

    # Fetch asset and validate exchange ownership
    result = await db.execute(select(Asset).where(Asset.id == asset_id))
    asset = result.scalar_one_or_none()
    if not asset:
        raise HTTPException(status_code=404, detail="Asset not found")
    if asset.exchange_id != exchange_db_id:
        raise HTTPException(
            status_code=404,
            detail="Asset not found on this exchange",
        )

    # Apply updates
    if body.is_tradable is not None:
        asset.is_tradable = body.is_tradable
    if body.allocation_weight is not None:
        asset.allocation_weight = body.allocation_weight

    await db.flush()
    await db.refresh(asset)

    return _asset_to_dict(asset)
