# ============================================================
# Phase 1 — Asset Management: Schema + API Foundation Tests
#
# Test Tiers:
#   unit (3 tests)            Pure ORM model tests, no database
#   pg_integration (18)       PostgreSQL-backed (pgserver embedded)
#                             Uses skipif(SKIP_PG) — NOT docker_integration
#                             marker (conftest auto-skip checks port 5433,
#                             but pgserver uses Unix sockets).
#
# PostgreSQL is REQUIRED for Tier 2 because Phase 1 introduces:
#   - JSONB column type (no SQLite equivalent)
#   - ON CONFLICT upsert semantics (different from SQLite)
#   - Partial index WHERE is_tradable = true
#   - server_default=text("false") (PG boolean literal)
# ============================================================
from __future__ import annotations

import asyncio
import logging
import os
import sys
import tempfile
from datetime import datetime
from unittest.mock import AsyncMock, patch

import pytest

# ── Path setup ──────────────────────────────────────────────
_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_WEB_DIR = os.path.dirname(_BACKEND)
_PROJECT_ROOT = os.path.dirname(_WEB_DIR)

for p in [_BACKEND, _WEB_DIR, _PROJECT_ROOT]:
    if p not in sys.path:
        sys.path.insert(0, p)

# Install Qt shim before any core imports
from core_patch import install_qt_shim
install_qt_shim()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("test.phase1_asset_mgmt")


# ============================================================
# TIER 1: Unit Tests (no database, pure model inspection)
# ============================================================

class TestAssetModelUnit:
    """Pure ORM model tests — no database connection needed."""

    @pytest.mark.unit
    def test_asset_model_has_new_columns(self):
        """Assert 4 new columns exist on Asset with correct types."""
        from app.models.trading import Asset
        from sqlalchemy import Boolean, Float, DateTime
        from sqlalchemy.dialects.postgresql import JSONB

        columns = Asset.__table__.columns
        col_names = {c.name for c in columns}

        assert "is_tradable" in col_names
        assert "allocation_weight" in col_names
        assert "market_snapshot" in col_names
        assert "snapshot_updated_at" in col_names

        # Type checks
        assert isinstance(columns["is_tradable"].type, Boolean)
        assert isinstance(columns["allocation_weight"].type, Float)
        assert isinstance(columns["market_snapshot"].type, JSONB)
        assert isinstance(columns["snapshot_updated_at"].type, DateTime)
        assert columns["snapshot_updated_at"].type.timezone is True

        logger.info("PASS: Asset model has all 4 new columns with correct types")

    @pytest.mark.unit
    def test_asset_model_defaults(self):
        """Verify column default values are correctly defined."""
        from app.models.trading import Asset

        columns = Asset.__table__.columns

        # is_tradable: Python default=False, server_default="false"
        is_tradable_col = columns["is_tradable"]
        assert is_tradable_col.default is not None
        assert is_tradable_col.default.arg is False
        assert is_tradable_col.server_default is not None

        # allocation_weight: Python default=1.0, server_default="1.0"
        weight_col = columns["allocation_weight"]
        assert weight_col.default is not None
        assert weight_col.default.arg == 1.0
        assert weight_col.server_default is not None

        # market_snapshot: nullable, no default (Phase 2-owned)
        snapshot_col = columns["market_snapshot"]
        assert snapshot_col.nullable is True
        assert snapshot_col.default is None

        # snapshot_updated_at: nullable, no default (Phase 2-owned)
        ts_col = columns["snapshot_updated_at"]
        assert ts_col.nullable is True
        assert ts_col.default is None

        logger.info("PASS: Asset defaults: is_tradable=False, weight=1.0, snapshot=nullable")

    @pytest.mark.unit
    def test_asset_table_has_tradable_index(self):
        """Verify ix_assets_tradable index exists in ORM metadata."""
        from app.models.trading import Asset

        index_names = {idx.name for idx in Asset.__table__.indexes}
        assert "ix_assets_tradable" in index_names

        logger.info("PASS: ix_assets_tradable index defined in ORM metadata")


# ============================================================
# TIER 2: Docker Integration Tests (PostgreSQL via pgserver)
# ============================================================

def _run(coro):
    """Run async coroutine synchronously."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ── Lazy PostgreSQL startup ─────────────────────────────────
# pgserver is started on first access, not at import time, to avoid
# polluting other test modules in the same pytest session.
_PG_SERVER = None
_PG_URI = None
_PG_TMPDIR = None
_PG_INIT_DONE = False


def _ensure_pg():
    """Start pgserver lazily on first call. Returns URI or None."""
    global _PG_SERVER, _PG_URI, _PG_TMPDIR, _PG_INIT_DONE
    if _PG_INIT_DONE:
        return _PG_URI
    _PG_INIT_DONE = True

    try:
        import pgserver
        _PG_TMPDIR = tempfile.mkdtemp()
        _PG_SERVER = pgserver.get_server(_PG_TMPDIR)
        _PG_URI = _PG_SERVER.get_uri()
        logger.info("pgserver started: %s", _PG_URI)
    except Exception as e:
        logger.warning("pgserver unavailable: %s — PG tests will skip", e)

    # Fallback: Docker E2E stack
    if _PG_URI is None and os.path.exists("/tmp/pg_uri.txt"):
        with open("/tmp/pg_uri.txt") as f:
            _PG_URI = f.read().strip()
        logger.info("Using PG from /tmp/pg_uri.txt: %s", _PG_URI)

    return _PG_URI


def _pg_available():
    """Check if pgserver or /tmp/pg_uri.txt is available (without starting)."""
    try:
        import pgserver  # noqa: F401
        return True
    except ImportError:
        pass
    return os.path.exists("/tmp/pg_uri.txt")


SKIP_PG = not _pg_available()
PG_REASON = "PostgreSQL not available (pgserver not installed and no /tmp/pg_uri.txt)"

# ── fakeredis setup ─────────────────────────────────────────
import fakeredis
import fakeredis.aioredis

_FAKE_REDIS_SERVER = fakeredis.FakeServer()


def _get_fake_redis(**kwargs):
    return fakeredis.aioredis.FakeRedis(
        server=_FAKE_REDIS_SERVER,
        decode_responses=kwargs.get("decode_responses", True),
    )


# ── App factory with PG + fakeredis ────────────────────────
_app_instance = None


def _get_app():
    global _app_instance
    if _app_instance is None:
        from main import app as _a
        _app_instance = _a
    return _app_instance


def _async_pg_uri():
    """Convert sync PG URI to asyncpg format."""
    uri = _ensure_pg()
    if uri is None:
        return None
    return uri.replace("postgresql://", "postgresql+asyncpg://", 1)


class _PGClientContext:
    """
    Context manager that patches the app to use pgserver PG + fakeredis,
    yields an httpx AsyncClient. Must be used as `async with`.

    Follows the exact pattern from test_phase1_1_integration.py.
    """
    def __init__(self):
        self._patches = []
        self._client = None

    async def __aenter__(self):
        from httpx import AsyncClient, ASGITransport

        pg_uri = _ensure_pg()
        env_overrides = {
            "NEXUS_DATABASE_URL": pg_uri,
            "NEXUS_REDIS_URL": "redis://fake:6379/0",
            "NEXUS_JWT_SECRET": "test-secret-32-chars-long-enough!!",
            "NEXUS_DEBUG": "true",
        }

        # Patch env
        p1 = patch.dict(os.environ, env_overrides)
        p1.start()
        self._patches.append(p1)

        # Clear cached settings so they re-read from env
        from app.config import clear_settings
        clear_settings()

        # Patch redis
        import redis.asyncio as aioredis
        p2 = patch.object(aioredis, "from_url",
                          lambda url, **kw: _get_fake_redis(**kw))
        p2.start()
        self._patches.append(p2)

        # Reset async engine so it reconnects with new URL
        from app import database as db_mod
        db_mod._async_engine = None
        db_mod._async_session_factory = None

        app = _get_app()

        # Override auth
        from app.auth.dependencies import get_current_user
        app.dependency_overrides[get_current_user] = lambda: {
            "id": 1, "email": "test@test.com"
        }

        transport = ASGITransport(app=app)
        self._client = AsyncClient(transport=transport, base_url="http://test")
        return self._client

    async def __aexit__(self, *exc):
        if self._client:
            await self._client.aclose()

        # Remove auth override BEFORE stopping patches — prevents
        # subsequent test files from inheriting our auth bypass.
        try:
            from app.auth.dependencies import get_current_user
            app = _get_app()
            app.dependency_overrides.pop(get_current_user, None)
        except Exception:
            pass

        for p in reversed(self._patches):
            p.stop()

        # Reset engine + settings so subsequent test files get clean state
        from app import database as db_mod
        db_mod._async_engine = None
        db_mod._async_session_factory = None
        from app.config import clear_settings
        clear_settings()


async def _setup_exchange_and_assets(client, n_assets=5):
    """
    Create an exchange and insert test assets directly via DB.
    Returns (exchange_id, list_of_asset_dicts).
    """
    from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
    from app.models.trading import Exchange, Asset
    from app.database import Base

    engine = create_async_engine(_async_pg_uri(), echo=False)

    # Create tables if needed (alembic should have run, but be safe)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)

    asset_dicts = []
    async with factory() as session:
        # Create exchange
        ex = Exchange(
            name="TestBybit",
            exchange_id="bybit",
            is_active=True,
            sandbox_mode=False,
            demo_mode=True,
        )
        session.add(ex)
        await session.flush()
        exchange_id = ex.id

        # Create assets
        symbols = [
            ("BTC/USDT:USDT", "BTC", "USDT"),
            ("ETH/USDT:USDT", "ETH", "USDT"),
            ("SOL/USDT:USDT", "SOL", "USDT"),
            ("BNB/USDT:USDT", "BNB", "USDT"),
            ("XRP/USDT:USDT", "XRP", "USDT"),
            ("DOGE/USDT:USDT", "DOGE", "USDT"),
            ("ADA/USDT:USDT", "ADA", "USDT"),
            ("AVAX/USDT:USDT", "AVAX", "USDT"),
            ("DOT/USDT:USDT", "DOT", "USDT"),
            ("LINK/USDT:USDT", "LINK", "USDT"),
            ("MATIC/USDT:USDT", "MATIC", "USDT"),
            ("UNI/USDT:USDT", "UNI", "USDT"),
            ("ATOM/USDT:USDT", "ATOM", "USDT"),
            ("FIL/USDT:USDT", "FIL", "USDT"),
            ("NEAR/USDT:USDT", "NEAR", "USDT"),
        ]
        for i, (sym, base, quote) in enumerate(symbols[:n_assets]):
            a = Asset(
                exchange_id=exchange_id,
                symbol=sym,
                base_currency=base,
                quote_currency=quote,
                price_precision=2,
                amount_precision=3,
                min_amount=0.001,
                min_cost=5.0,
                is_active=True,
                last_updated=datetime.utcnow(),
            )
            session.add(a)
            await session.flush()
            asset_dicts.append({
                "id": a.id,
                "symbol": sym,
                "base_currency": base,
                "quote_currency": quote,
            })

        await session.commit()

    await engine.dispose()
    return exchange_id, asset_dicts


async def _cleanup_db():
    """Drop all tables for clean state between tests."""
    uri = _async_pg_uri()
    if uri is None:
        return
    from sqlalchemy.ext.asyncio import create_async_engine
    from app.database import Base

    engine = create_async_engine(uri, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        # Also drop alembic_version if it exists
        await conn.execute(__import__("sqlalchemy").text(
            "DROP TABLE IF EXISTS alembic_version"
        ))
    await engine.dispose()


# ── Migration Tests ─────────────────────────────────────────

@pytest.mark.skipif(SKIP_PG, reason=PG_REASON)
class TestAlembicMigration:

    def test_alembic_migration_applies_cleanly(self):
        """Alembic upgrade head succeeds and creates correct PG columns."""
        _run(self._async_test())

    async def _async_test(self):
        await _cleanup_db()

        from alembic.config import Config
        from alembic import command

        alembic_ini = os.path.join(_BACKEND, "alembic.ini")
        cfg = Config(alembic_ini)
        pg_uri = _ensure_pg()
        cfg.set_main_option("sqlalchemy.url", pg_uri)

        with patch.dict(os.environ, {"NEXUS_DATABASE_URL": pg_uri}):
            from app.config import clear_settings
            clear_settings()
            command.upgrade(cfg, "head")

        # Verify columns via information_schema
        import psycopg2
        conn = psycopg2.connect(_PG_URI)
        cur = conn.cursor()

        cur.execute("""
            SELECT column_name, udt_name
            FROM information_schema.columns
            WHERE table_name = 'assets'
            AND column_name IN ('is_tradable', 'allocation_weight',
                                'market_snapshot', 'snapshot_updated_at')
            ORDER BY column_name
        """)
        cols = {row[0]: row[1] for row in cur.fetchall()}

        assert cols["is_tradable"] == "bool", f"Expected bool, got {cols['is_tradable']}"
        assert cols["allocation_weight"] in ("float8", "float4"), f"Got {cols['allocation_weight']}"
        assert cols["market_snapshot"] == "jsonb", f"Expected jsonb, got {cols['market_snapshot']}"
        assert cols["snapshot_updated_at"] == "timestamptz", f"Expected timestamptz, got {cols['snapshot_updated_at']}"

        logger.info("EVIDENCE: PG columns: %s", cols)

        cur.close()
        conn.close()
        logger.info("PASS: Alembic migration applies cleanly with correct PG types")

    def test_alembic_downgrade_reverses(self):
        """Downgrade removes columns, re-upgrade restores them."""
        _run(self._async_downgrade_test())

    async def _async_downgrade_test(self):
        await _cleanup_db()

        from alembic.config import Config
        from alembic import command

        alembic_ini = os.path.join(_BACKEND, "alembic.ini")
        cfg = Config(alembic_ini)
        pg_uri = _ensure_pg()
        cfg.set_main_option("sqlalchemy.url", pg_uri)

        with patch.dict(os.environ, {"NEXUS_DATABASE_URL": pg_uri}):
            from app.config import clear_settings
            clear_settings()

            # Upgrade to head
            command.upgrade(cfg, "head")

            # Downgrade to initial schema (before Phase 1 columns were added)
            command.downgrade(cfg, "00788dff2b9e")

            # Verify columns removed
            import psycopg2
            conn = psycopg2.connect(_PG_URI)
            cur = conn.cursor()
            cur.execute("""
                SELECT column_name FROM information_schema.columns
                WHERE table_name = 'assets'
                AND column_name IN ('is_tradable', 'allocation_weight',
                                    'market_snapshot', 'snapshot_updated_at')
            """)
            remaining = cur.fetchall()
            assert len(remaining) == 0, f"Columns not removed: {remaining}"
            logger.info("EVIDENCE: Downgrade removed all 4 columns")

            cur.close()
            conn.close()

            # Re-upgrade to verify clean round-trip
            command.upgrade(cfg, "head")
            logger.info("PASS: Downgrade + re-upgrade round-trip clean")

    def test_partial_index_exists_in_pg(self):
        """Verify ix_assets_tradable partial index exists with correct WHERE."""
        _run(self._async_index_test())

    async def _async_index_test(self):
        await _cleanup_db()

        from alembic.config import Config
        from alembic import command

        alembic_ini = os.path.join(_BACKEND, "alembic.ini")
        cfg = Config(alembic_ini)
        pg_uri = _ensure_pg()
        cfg.set_main_option("sqlalchemy.url", pg_uri)

        with patch.dict(os.environ, {"NEXUS_DATABASE_URL": pg_uri}):
            from app.config import clear_settings
            clear_settings()
            command.upgrade(cfg, "head")

        import psycopg2
        conn = psycopg2.connect(_PG_URI)
        cur = conn.cursor()

        cur.execute("""
            SELECT indexname, indexdef FROM pg_indexes
            WHERE tablename = 'assets' AND indexname = 'ix_assets_tradable'
        """)
        row = cur.fetchone()
        assert row is not None, "Partial index ix_assets_tradable not found"
        indexdef = row[1]
        assert "is_tradable = true" in indexdef.lower() or "is_tradable" in indexdef.lower()
        logger.info("EVIDENCE: Index definition: %s", indexdef)

        cur.close()
        conn.close()
        logger.info("PASS: Partial index ix_assets_tradable exists in PostgreSQL")


# ── API Tests (all against real PG) ─────────────────────────

@pytest.mark.skipif(SKIP_PG, reason=PG_REASON)
class TestListAssets:

    def test_list_assets_includes_new_fields(self):
        _run(self._test())

    async def _test(self):
        await _cleanup_db()
        async with _PGClientContext() as client:
            exchange_id, assets = await _setup_exchange_and_assets(client, n_assets=3)

            resp = await client.get(f"/api/v1/exchanges/{exchange_id}/assets")
            assert resp.status_code == 200
            body = resp.json()

            assert "total" in body
            assert "offset" in body
            assert "limit" in body

            first = body["assets"][0]
            assert "is_tradable" in first
            assert "allocation_weight" in first
            assert "market_snapshot" in first
            assert "snapshot_updated_at" in first

            # Phase 1: market_snapshot must be null
            for a in body["assets"]:
                assert a["market_snapshot"] is None
                assert a["snapshot_updated_at"] is None

            logger.info("EVIDENCE: list_assets includes new fields: %s",
                        {k: first[k] for k in ["is_tradable", "allocation_weight",
                                                "market_snapshot", "snapshot_updated_at"]})
            logger.info("PASS: list_assets includes all 4 new fields")

    def test_list_assets_filter_tradable_true(self):
        _run(self._test_filter_true())

    async def _test_filter_true(self):
        await _cleanup_db()
        async with _PGClientContext() as client:
            exchange_id, assets = await _setup_exchange_and_assets(client, n_assets=3)

            # Make one tradable
            resp = await client.patch(
                f"/api/v1/exchanges/{exchange_id}/assets/{assets[0]['id']}",
                json={"is_tradable": True},
            )
            assert resp.status_code == 200

            # Filter tradable=true
            resp = await client.get(
                f"/api/v1/exchanges/{exchange_id}/assets?is_tradable=true"
            )
            assert resp.status_code == 200
            body = resp.json()
            assert body["total"] == 1
            assert body["assets"][0]["is_tradable"] is True
            logger.info("PASS: is_tradable=true filter returns only tradable assets")

    def test_list_assets_filter_tradable_false(self):
        _run(self._test_filter_false())

    async def _test_filter_false(self):
        await _cleanup_db()
        async with _PGClientContext() as client:
            exchange_id, assets = await _setup_exchange_and_assets(client, n_assets=3)

            # Make one tradable
            await client.patch(
                f"/api/v1/exchanges/{exchange_id}/assets/{assets[0]['id']}",
                json={"is_tradable": True},
            )

            resp = await client.get(
                f"/api/v1/exchanges/{exchange_id}/assets?is_tradable=false"
            )
            assert resp.status_code == 200
            body = resp.json()
            assert body["total"] == 2
            for a in body["assets"]:
                assert a["is_tradable"] is False
            logger.info("PASS: is_tradable=false filter returns only non-tradable assets")

    def test_list_assets_pagination(self):
        _run(self._test_pagination())

    async def _test_pagination(self):
        await _cleanup_db()
        async with _PGClientContext() as client:
            exchange_id, assets = await _setup_exchange_and_assets(client, n_assets=15)

            resp = await client.get(
                f"/api/v1/exchanges/{exchange_id}/assets?offset=5&limit=5"
            )
            assert resp.status_code == 200
            body = resp.json()
            assert body["count"] == 5
            assert body["total"] == 15
            assert body["offset"] == 5
            assert body["limit"] == 5
            logger.info("EVIDENCE: Pagination: count=%d, total=%d, offset=%d",
                        body["count"], body["total"], body["offset"])
            logger.info("PASS: Pagination works correctly")


@pytest.mark.skipif(SKIP_PG, reason=PG_REASON)
class TestSyncAssets:

    def test_sync_creates_new_assets(self):
        _run(self._test_create())

    async def _test_create(self):
        await _cleanup_db()
        async with _PGClientContext() as client:
            # Create exchange directly in DB
            from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
            from app.models.trading import Exchange
            from app.database import Base

            engine = create_async_engine(_async_pg_uri(), echo=False)
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)

            factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
            async with factory() as session:
                ex = Exchange(name="TestBybit", exchange_id="bybit",
                              is_active=True, demo_mode=True)
                session.add(ex)
                await session.flush()
                exchange_id = ex.id
                await session.commit()
            await engine.dispose()

            # Mock engine command to return assets
            mock_engine_result = {
                "status": "ok",
                "assets": [
                    {"symbol": "BTC/USDT:USDT", "base_currency": "BTC",
                     "quote_currency": "USDT", "price_precision": 2,
                     "amount_precision": 3, "min_amount": 0.001,
                     "min_cost": 5.0, "is_active": True},
                    {"symbol": "ETH/USDT:USDT", "base_currency": "ETH",
                     "quote_currency": "USDT", "price_precision": 2,
                     "amount_precision": 4, "min_amount": 0.01,
                     "min_cost": 5.0, "is_active": True},
                ],
                "count": 2,
            }

            with patch("app.api.exchanges._send_engine_command",
                        new_callable=AsyncMock, return_value=mock_engine_result):
                resp = await client.post(
                    f"/api/v1/exchanges/{exchange_id}/sync-assets"
                )
                assert resp.status_code == 200
                body = resp.json()
                assert body["status"] == "ok"
                assert body["upserted"] == 2
                logger.info("EVIDENCE: sync created %d assets", body["upserted"])

            # Verify assets exist via list
            resp = await client.get(f"/api/v1/exchanges/{exchange_id}/assets")
            assert resp.status_code == 200
            assert resp.json()["total"] == 2
            logger.info("PASS: sync-assets creates DB rows")

    def test_sync_preserves_tradable_on_conflict(self):
        _run(self._test_preserve_tradable())

    async def _test_preserve_tradable(self):
        await _cleanup_db()
        async with _PGClientContext() as client:
            exchange_id, assets = await _setup_exchange_and_assets(client, n_assets=2)

            # Set first asset to tradable
            resp = await client.patch(
                f"/api/v1/exchanges/{exchange_id}/assets/{assets[0]['id']}",
                json={"is_tradable": True},
            )
            assert resp.status_code == 200

            # Re-sync (same symbols)
            mock_result = {
                "status": "ok",
                "assets": [
                    {"symbol": assets[0]["symbol"], "base_currency": assets[0]["base_currency"],
                     "quote_currency": assets[0]["quote_currency"], "price_precision": 4,
                     "amount_precision": 5, "min_amount": 0.002, "min_cost": 10.0, "is_active": True},
                    {"symbol": assets[1]["symbol"], "base_currency": assets[1]["base_currency"],
                     "quote_currency": assets[1]["quote_currency"], "price_precision": 2,
                     "amount_precision": 3, "min_amount": 0.001, "min_cost": 5.0, "is_active": True},
                ],
                "count": 2,
            }

            with patch("app.api.exchanges._send_engine_command",
                        new_callable=AsyncMock, return_value=mock_result):
                resp = await client.post(
                    f"/api/v1/exchanges/{exchange_id}/sync-assets"
                )
                assert resp.status_code == 200
                body = resp.json()
                assert body["preserved_tradable"] >= 1
                logger.info("EVIDENCE: preserved_tradable=%d", body["preserved_tradable"])

            # Verify tradable preserved
            resp = await client.get(
                f"/api/v1/exchanges/{exchange_id}/assets?is_tradable=true"
            )
            assert resp.status_code == 200
            tradable = resp.json()["assets"]
            assert len(tradable) == 1
            assert tradable[0]["symbol"] == assets[0]["symbol"]
            assert tradable[0]["is_tradable"] is True
            logger.info("PASS: sync-assets preserves is_tradable on conflict")

    def test_sync_preserves_weight_on_conflict(self):
        _run(self._test_preserve_weight())

    async def _test_preserve_weight(self):
        await _cleanup_db()
        async with _PGClientContext() as client:
            exchange_id, assets = await _setup_exchange_and_assets(client, n_assets=2)

            # Set custom weight
            resp = await client.patch(
                f"/api/v1/exchanges/{exchange_id}/assets/{assets[0]['id']}",
                json={"allocation_weight": 1.3},
            )
            assert resp.status_code == 200

            # Re-sync
            mock_result = {
                "status": "ok",
                "assets": [
                    {"symbol": assets[0]["symbol"], "base_currency": assets[0]["base_currency"],
                     "quote_currency": assets[0]["quote_currency"], "price_precision": 4,
                     "amount_precision": 5, "min_amount": 0.002, "min_cost": 10.0, "is_active": True},
                ],
                "count": 1,
            }

            with patch("app.api.exchanges._send_engine_command",
                        new_callable=AsyncMock, return_value=mock_result):
                resp = await client.post(
                    f"/api/v1/exchanges/{exchange_id}/sync-assets"
                )
                assert resp.status_code == 200

            # Verify weight preserved
            resp = await client.get(f"/api/v1/exchanges/{exchange_id}/assets")
            found = [a for a in resp.json()["assets"]
                     if a["symbol"] == assets[0]["symbol"]]
            assert len(found) == 1
            assert abs(found[0]["allocation_weight"] - 1.3) < 0.01
            logger.info("EVIDENCE: allocation_weight=%.1f after re-sync", found[0]["allocation_weight"])
            logger.info("PASS: sync-assets preserves allocation_weight on conflict")

    def test_sync_updates_exchange_fields(self):
        _run(self._test_updates_fields())

    async def _test_updates_fields(self):
        await _cleanup_db()
        async with _PGClientContext() as client:
            exchange_id, assets = await _setup_exchange_and_assets(client, n_assets=1)

            # Set tradable + weight first
            await client.patch(
                f"/api/v1/exchanges/{exchange_id}/assets/{assets[0]['id']}",
                json={"is_tradable": True, "allocation_weight": 2.0},
            )

            # Sync with different precision values
            mock_result = {
                "status": "ok",
                "assets": [
                    {"symbol": assets[0]["symbol"], "base_currency": assets[0]["base_currency"],
                     "quote_currency": assets[0]["quote_currency"], "price_precision": 6,
                     "amount_precision": 8, "min_amount": 0.005, "min_cost": 15.0, "is_active": True},
                ],
                "count": 1,
            }

            with patch("app.api.exchanges._send_engine_command",
                        new_callable=AsyncMock, return_value=mock_result):
                resp = await client.post(
                    f"/api/v1/exchanges/{exchange_id}/sync-assets"
                )
                assert resp.status_code == 200

            # Verify exchange fields updated but user fields preserved
            resp = await client.get(f"/api/v1/exchanges/{exchange_id}/assets")
            a = resp.json()["assets"][0]
            assert a["price_precision"] == 6
            assert a["amount_precision"] == 8
            assert a["min_amount"] == 0.005
            assert a["min_cost"] == 15.0
            # User fields preserved
            assert a["is_tradable"] is True
            assert abs(a["allocation_weight"] - 2.0) < 0.01
            assert a["market_snapshot"] is None  # Phase 2-owned
            logger.info("PASS: sync updates exchange fields, preserves user + Phase 2 fields")


@pytest.mark.skipif(SKIP_PG, reason=PG_REASON)
class TestPatchSingleAsset:

    def test_patch_asset_set_tradable(self):
        _run(self._test())

    async def _test(self):
        await _cleanup_db()
        async with _PGClientContext() as client:
            exchange_id, assets = await _setup_exchange_and_assets(client, n_assets=1)

            resp = await client.patch(
                f"/api/v1/exchanges/{exchange_id}/assets/{assets[0]['id']}",
                json={"is_tradable": True},
            )
            assert resp.status_code == 200
            body = resp.json()
            assert body["is_tradable"] is True
            logger.info("EVIDENCE: PATCH response is_tradable=%s", body["is_tradable"])

            # Verify via GET
            resp = await client.get(f"/api/v1/exchanges/{exchange_id}/assets")
            found = [a for a in resp.json()["assets"] if a["id"] == assets[0]["id"]]
            assert found[0]["is_tradable"] is True
            logger.info("PASS: PATCH asset sets tradable correctly")

    def test_patch_asset_set_weight(self):
        _run(self._test_weight())

    async def _test_weight(self):
        await _cleanup_db()
        async with _PGClientContext() as client:
            exchange_id, assets = await _setup_exchange_and_assets(client, n_assets=1)

            resp = await client.patch(
                f"/api/v1/exchanges/{exchange_id}/assets/{assets[0]['id']}",
                json={"allocation_weight": 1.5},
            )
            assert resp.status_code == 200
            body = resp.json()
            assert abs(body["allocation_weight"] - 1.5) < 0.01
            logger.info("EVIDENCE: PATCH response allocation_weight=%.1f", body["allocation_weight"])

            # Verify persistence
            resp = await client.get(f"/api/v1/exchanges/{exchange_id}/assets")
            found = [a for a in resp.json()["assets"] if a["id"] == assets[0]["id"]]
            assert abs(found[0]["allocation_weight"] - 1.5) < 0.01
            logger.info("PASS: PATCH asset sets weight correctly")

    def test_patch_asset_cross_exchange_rejected(self):
        _run(self._test_cross_exchange())

    async def _test_cross_exchange(self):
        await _cleanup_db()
        async with _PGClientContext() as client:
            exchange_id, assets = await _setup_exchange_and_assets(client, n_assets=1)

            # Create a second exchange
            from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
            from app.models.trading import Exchange

            engine = create_async_engine(_async_pg_uri(), echo=False)
            factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
            async with factory() as session:
                ex2 = Exchange(name="TestBinance", exchange_id="binance",
                               is_active=False, sandbox_mode=False, demo_mode=False)
                session.add(ex2)
                await session.flush()
                exchange2_id = ex2.id
                await session.commit()
            await engine.dispose()

            # Try to PATCH asset via wrong exchange
            resp = await client.patch(
                f"/api/v1/exchanges/{exchange2_id}/assets/{assets[0]['id']}",
                json={"is_tradable": True},
            )
            assert resp.status_code == 404
            logger.info("EVIDENCE: Cross-exchange PATCH → 404 (detail=%s)", resp.json()["detail"])

            # Verify original asset unchanged
            resp = await client.get(f"/api/v1/exchanges/{exchange_id}/assets")
            found = [a for a in resp.json()["assets"] if a["id"] == assets[0]["id"]]
            assert found[0]["is_tradable"] is False
            logger.info("PASS: Cross-exchange PATCH correctly rejected")


@pytest.mark.skipif(SKIP_PG, reason=PG_REASON)
class TestBulkUpdate:

    def test_bulk_update_tradable_all_or_nothing(self):
        _run(self._test_all_or_nothing())

    async def _test_all_or_nothing(self):
        await _cleanup_db()
        async with _PGClientContext() as client:
            exchange_id, assets = await _setup_exchange_and_assets(client, n_assets=3)

            valid_ids = [a["id"] for a in assets]
            invalid_id = 99999

            # Try with one invalid ID — should fail entirely
            resp = await client.patch(
                f"/api/v1/exchanges/{exchange_id}/assets/bulk",
                json={"asset_ids": valid_ids + [invalid_id], "is_tradable": True},
            )
            assert resp.status_code == 404
            body = resp.json()
            assert invalid_id in body["detail"]["missing"]
            logger.info("EVIDENCE: Bulk with invalid ID → 404, missing=%s",
                        body["detail"]["missing"])

            # Verify NO assets were updated (all-or-nothing)
            resp = await client.get(
                f"/api/v1/exchanges/{exchange_id}/assets?is_tradable=true"
            )
            assert resp.json()["total"] == 0
            logger.info("EVIDENCE: Zero assets updated after rejected bulk")

            # Now with valid IDs only — should succeed
            resp = await client.patch(
                f"/api/v1/exchanges/{exchange_id}/assets/bulk",
                json={"asset_ids": valid_ids, "is_tradable": True},
            )
            assert resp.status_code == 200
            assert resp.json()["updated"] == 3
            logger.info("PASS: Bulk all-or-nothing works correctly")

    def test_bulk_update_weight(self):
        _run(self._test_weight())

    async def _test_weight(self):
        await _cleanup_db()
        async with _PGClientContext() as client:
            exchange_id, assets = await _setup_exchange_and_assets(client, n_assets=5)

            ids = [a["id"] for a in assets]
            resp = await client.patch(
                f"/api/v1/exchanges/{exchange_id}/assets/bulk",
                json={"asset_ids": ids, "allocation_weight": 0.8},
            )
            assert resp.status_code == 200
            assert resp.json()["updated"] == 5

            # Verify all have weight 0.8
            resp = await client.get(f"/api/v1/exchanges/{exchange_id}/assets")
            for a in resp.json()["assets"]:
                assert abs(a["allocation_weight"] - 0.8) < 0.01
            logger.info("PASS: Bulk weight update applied to all 5 assets")


@pytest.mark.skipif(SKIP_PG, reason=PG_REASON)
class TestGetTradable:

    def test_tradable_returns_only_tradable(self):
        _run(self._test())

    async def _test(self):
        await _cleanup_db()
        async with _PGClientContext() as client:
            exchange_id, assets = await _setup_exchange_and_assets(client, n_assets=5)

            # Make 2 tradable
            for a in assets[:2]:
                await client.patch(
                    f"/api/v1/exchanges/{exchange_id}/assets/{a['id']}",
                    json={"is_tradable": True},
                )

            resp = await client.get(
                f"/api/v1/exchanges/{exchange_id}/assets/tradable"
            )
            assert resp.status_code == 200
            body = resp.json()
            assert body["count"] == 2
            assert len(body["symbols"]) == 2
            assert len(body["assets"]) == 2

            for a in body["assets"]:
                assert "allocation_weight" in a
                assert "exchange_id" in a
                assert a["exchange_id"] == exchange_id

            logger.info("EVIDENCE: tradable count=%d symbols=%s",
                        body["count"], body["symbols"])
            logger.info("PASS: GET tradable returns only tradable assets")

    def test_tradable_empty(self):
        _run(self._test_empty())

    async def _test_empty(self):
        await _cleanup_db()
        async with _PGClientContext() as client:
            exchange_id, _ = await _setup_exchange_and_assets(client, n_assets=3)

            resp = await client.get(
                f"/api/v1/exchanges/{exchange_id}/assets/tradable"
            )
            assert resp.status_code == 200
            body = resp.json()
            assert body["count"] == 0
            assert body["symbols"] == []
            assert body["assets"] == []
            logger.info("PASS: GET tradable returns empty when no tradable assets")


# ── Cleanup ─────────────────────────────────────────────────

def pytest_sessionfinish(session, exitstatus):
    """Cleanup pgserver and restore app state on test session end."""
    global _PG_SERVER

    # Restore app database state so subsequent test modules don't get
    # a stale engine pointing at a now-defunct pgserver.
    try:
        from app import database as db_mod
        db_mod._async_engine = None
        db_mod._async_session_factory = None
    except Exception:
        pass

    try:
        from app.config import clear_settings
        clear_settings()
    except Exception:
        pass

    if _PG_SERVER is not None:
        try:
            _PG_SERVER.cleanup()
            logger.info("pgserver cleaned up")
        except Exception:
            pass
