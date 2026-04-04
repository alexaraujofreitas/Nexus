# ============================================================
# NEXUS TRADER Web — Market Data Service (Phase 2)
#
# Singleton async service owning the full pipeline:
#   CCXT fetch → Redis hot cache (OHLCV sorted sets + HASH
#   snapshots) → PostgreSQL persistence → Redis pub/sub WS
#   delivery.
#
# Architecture: Direct CCXT — MDS creates its own read-only
# CCXT instance. No engine modifications required.
#
# Race-condition safety: Single-writer snapshot assembler.
# All snapshot mutations go through _assemble_and_flush().
# Lock scope is limited to in-memory merge only — no I/O
# under lock.
# ============================================================
from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, Optional

import ccxt.async_support as ccxt_async
from sqlalchemy import select, delete, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.trading import Asset, Exchange, OHLCV
from app.services.vault import get_vault

logger = logging.getLogger("nexus.market_data")

# ── Configuration Constants (Appendix B) ────────────────────
TIER0_INTERVAL_S: int = 15
TIER1_INTERVAL_S: int = 60
TIER2_INTERVAL_S: int = 300
TRADABLE_CACHE_TTL_S: int = 60
OHLCV_1H_MAX_BARS: int = 168
OHLCV_1D_MAX_BARS: int = 30
OHLCV_1H_FETCH_LIMIT: int = 4
OHLCV_1D_FETCH_LIMIT: int = 2
WARMUP_1H_BARS: int = 168
WARMUP_1D_BARS: int = 30
SNAPSHOT_TTL_S: int = 600
OHLCV_1H_TTL_S: int = 172800
OHLCV_1D_TTL_S: int = 604800
DB_PRUNE_INTERVAL_S: int = 21600
DB_RETENTION_1H_HOURS: int = 168
DB_RETENTION_1D_DAYS: int = 30
CCXT_CONCURRENCY: int = 3
CCXT_RATE_LIMIT_MS: int = 350

# Snapshot field names grouped by owner tier
_TIER0_FIELDS = frozenset({
    "price", "bid", "ask", "spread_pct", "last_trade_ts", "source",
})
_TIER1_FIELDS = frozenset({
    "change_1h", "change_4h", "volume_1h", "volume_4h",
    "high_24h", "low_24h", "source_candle_ts",
})
_TIER2_FIELDS = frozenset({
    "change_12h", "change_24h", "change_7d", "volume_24h",
})
ALL_SNAPSHOT_FIELDS = frozenset(
    _TIER0_FIELDS | _TIER1_FIELDS | _TIER2_FIELDS | {"computed_at"}
)


def _floor_to_hour_ms(dt: datetime) -> int:
    """Return epoch milliseconds of the start of the given hour (UTC)."""
    truncated = dt.replace(minute=0, second=0, microsecond=0)
    return int(truncated.timestamp() * 1000)


def _utcnow() -> datetime:
    """Current UTC time (timezone-aware)."""
    return datetime.now(timezone.utc)


def _ts_ms_to_iso(ts_ms: int) -> str:
    """Convert epoch milliseconds to ISO 8601 UTC string."""
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def _parse_bar(raw: str) -> list:
    """Parse a JSON-encoded OHLCV bar from Redis."""
    return json.loads(raw)


def compute_spread_pct(bid: Optional[float], ask: Optional[float]) -> Optional[float]:
    """Compute spread percentage from bid/ask. Returns None on invalid input."""
    if not bid or not ask or bid <= 0 or ask <= 0:
        return None
    mid = (ask + bid) / 2
    if mid == 0:
        return None
    return round(((ask - bid) / mid) * 100, 4)


def compute_change_pct(
    current_price: Optional[float], ref_close: Optional[float]
) -> Optional[float]:
    """Compute percentage change. Returns None if either input is invalid."""
    if current_price is None or ref_close is None or ref_close == 0:
        return None
    return round(((current_price - ref_close) / ref_close) * 100, 2)


class MarketDataService:
    """
    Singleton async service for market data fetching, caching, and delivery.

    Lifecycle:
        1. Instantiated by main.py lifespan with db_session_factory + redis + settings.
        2. start() queries DB for active exchange, inits CCXT, spawns tier tasks.
        3. stop() cancels all tasks, closes CCXT.
    """

    def __init__(
        self,
        db_session_factory: async_sessionmaker[AsyncSession],
        redis: Any,  # redis.asyncio.Redis or fakeredis equivalent
        settings: Any,
    ) -> None:
        self._db_session_factory = db_session_factory
        self._redis = redis
        self._settings = settings

        # CCXT exchange (lazy-init in start())
        self._ccxt_exchange: Optional[Any] = None

        # Runtime state
        self._running: bool = False
        self._tasks: dict[str, asyncio.Task] = {}

        # In-memory canonical snapshots — single source of truth at runtime
        self._snapshots: dict[int, dict[str, Any]] = {}
        self._snapshot_lock: asyncio.Lock = asyncio.Lock()

        # Single-writer flush queue (Option A race-condition prevention).
        # _pending_flushes holds only the LATEST snapshot per asset_id.
        # _flush_event signals the writer coroutine that work is available.
        # Because a single coroutine performs all Redis I/O, writes are
        # strictly ordered and stale overwrites are impossible.
        self._pending_flushes: dict[int, tuple[int, dict[str, Any]]] = {}
        self._flush_lock: asyncio.Lock = asyncio.Lock()
        self._flush_event: asyncio.Event = asyncio.Event()

        # Known asset tracking (for staged warm-up detection)
        self._known_asset_ids: set[int] = set()

        # Warm-up tracking: separate from _known_asset_ids so startup
        # assets get a full backfill on first tier cycle.
        self._warmed_1h_assets: set[int] = set()
        self._warmed_1d_assets: set[int] = set()

        # Tradable asset cache
        self._tradable_assets: list[Asset] = []
        self._tradable_refresh_ts: float = 0.0

        # Symbol lookup (asset_id → symbol string)
        self._symbols: dict[int, str] = {}

        # Exchange DB record
        self._exchange_db_id: Optional[int] = None
        self._exchange_db: Optional[Exchange] = None

        # Concurrency control for CCXT fetch_ohlcv
        self._ohlcv_semaphore: asyncio.Semaphore = asyncio.Semaphore(CCXT_CONCURRENCY)

        # DB prune tracking
        self._last_prune_ts: float = 0.0

        # Tier overrun tracking
        self._tier1_overrun_count: int = 0
        self._tier2_overrun_count: int = 0
        self._tier1_interval: float = float(TIER1_INTERVAL_S)
        self._tier2_interval: float = float(TIER2_INTERVAL_S)

    # ── Lifecycle ───────────────────────────────────────────────

    async def start(self) -> None:
        """Initialize CCXT, load tradable assets, spawn tier loops."""
        logger.info("MarketDataService starting")

        # Load active exchange from DB
        async with self._db_session_factory() as session:
            result = await session.execute(
                select(Exchange).where(Exchange.is_active.is_(True)).limit(1)
            )
            exchange = result.scalar_one_or_none()

        if exchange is None:
            logger.warning("No active exchange found — MDS entering idle mode")
            self._running = True
            self._tasks["idle"] = asyncio.create_task(self._idle_loop())
            return

        self._exchange_db = exchange
        self._exchange_db_id = exchange.id

        # Initialize CCXT instance
        await self._init_ccxt(exchange)

        # Load initial tradable assets
        self._tradable_assets = await self._refresh_tradable_assets(force=True)
        for asset in self._tradable_assets:
            self._known_asset_ids.add(asset.id)
            self._snapshots[asset.id] = {}
            self._symbols[asset.id] = asset.symbol

        logger.info(
            "MDS initialized: exchange=%s, tradable_assets=%d",
            exchange.name, len(self._tradable_assets),
        )

        # Cold-start backfill: populate Redis cache from DB/CCXT before
        # tier loops begin (ensures first tier cycle has data to compute on).
        for asset in self._tradable_assets:
            try:
                await self._backfill_ohlcv_if_needed(asset.id, asset.symbol)
            except Exception:
                logger.error(
                    "Cold-start backfill failed for asset %d (%s)",
                    asset.id, asset.symbol, exc_info=True,
                )

        self._running = True
        self._tasks["flush_writer"] = asyncio.create_task(self._flush_writer())
        self._tasks["tier0"] = asyncio.create_task(self._tier0_loop())
        self._tasks["tier1"] = asyncio.create_task(self._tier1_loop())
        self._tasks["tier2"] = asyncio.create_task(self._tier2_loop())

    async def stop(self) -> None:
        """Cancel all tasks and close CCXT."""
        logger.info("MarketDataService stopping")
        self._running = False

        # Drain any pending Redis flushes before cancelling the writer
        try:
            await self._drain_flush_queue()
        except Exception:
            logger.error("Error draining flush queue on stop", exc_info=True)

        for name, task in self._tasks.items():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.error("Error cancelling task %s", name, exc_info=True)

        self._tasks.clear()

        if self._ccxt_exchange is not None:
            try:
                await self._ccxt_exchange.close()
            except Exception:
                logger.error("Error closing CCXT exchange", exc_info=True)
            self._ccxt_exchange = None

        logger.info("MarketDataService stopped")

    # ── CCXT Initialization ────────────────────────────────────

    async def _init_ccxt(self, exchange: Exchange) -> None:
        """Create and configure the read-only CCXT instance."""
        vault = get_vault()

        api_key = ""
        api_secret = ""
        if exchange.api_key_encrypted:
            try:
                api_key = vault.decrypt(exchange.api_key_encrypted)
            except Exception:
                logger.warning("Failed to decrypt API key for exchange %s", exchange.name)
        if exchange.api_secret_encrypted:
            try:
                api_secret = vault.decrypt(exchange.api_secret_encrypted)
            except Exception:
                logger.warning("Failed to decrypt API secret for exchange %s", exchange.name)

        ccxt_config: dict[str, Any] = {
            "apiKey": api_key,
            "secret": api_secret,
            "enableRateLimit": True,
            "timeout": 15000,
            "rateLimit": CCXT_RATE_LIMIT_MS,
        }

        if exchange.demo_mode:
            ccxt_config["options"] = {
                "defaultType": "swap",
            }
            # Bybit demo URL overrides
            ccxt_config["urls"] = {
                "api": {
                    "public": "https://api-demo.bybit.com",
                    "private": "https://api-demo.bybit.com",
                },
            }

        exchange_class = getattr(ccxt_async, exchange.exchange_id, None)
        if exchange_class is None:
            logger.error("CCXT exchange class not found: %s", exchange.exchange_id)
            return

        self._ccxt_exchange = exchange_class(ccxt_config)

        try:
            await self._ccxt_exchange.load_markets()
            logger.info("CCXT markets loaded for %s", exchange.exchange_id)
        except Exception:
            logger.error("Failed to load CCXT markets", exc_info=True)

    # ── Idle Loop (no active exchange) ─────────────────────────

    async def _idle_loop(self) -> None:
        """Check every 60s for an active exchange."""
        while self._running:
            try:
                await asyncio.sleep(60)
                async with self._db_session_factory() as session:
                    result = await session.execute(
                        select(Exchange).where(Exchange.is_active.is_(True)).limit(1)
                    )
                    exchange = result.scalar_one_or_none()

                if exchange is not None:
                    logger.info("Active exchange found: %s — restarting MDS", exchange.name)
                    self._exchange_db = exchange
                    self._exchange_db_id = exchange.id
                    await self._init_ccxt(exchange)
                    self._tradable_assets = await self._refresh_tradable_assets(force=True)
                    for asset in self._tradable_assets:
                        self._known_asset_ids.add(asset.id)
                        self._snapshots[asset.id] = {}
                        self._symbols[asset.id] = asset.symbol

                    # Cold-start backfill (consistent with start())
                    for asset in self._tradable_assets:
                        try:
                            await self._backfill_ohlcv_if_needed(asset.id, asset.symbol)
                        except Exception:
                            logger.error(
                                "Cold-start backfill failed for asset %d (%s)",
                                asset.id, asset.symbol, exc_info=True,
                            )

                    # flush_writer MUST be started before tier loops
                    self._tasks["flush_writer"] = asyncio.create_task(self._flush_writer())
                    self._tasks["tier0"] = asyncio.create_task(self._tier0_loop())
                    self._tasks["tier1"] = asyncio.create_task(self._tier1_loop())
                    self._tasks["tier2"] = asyncio.create_task(self._tier2_loop())
                    return  # exit idle loop
            except asyncio.CancelledError:
                return
            except Exception:
                logger.error("Error in idle loop", exc_info=True)

    # ── Tradable Asset Cache ───────────────────────────────────

    async def _refresh_tradable_assets(self, force: bool = False) -> list[Asset]:
        """
        Query DB for is_tradable=true assets. Cached for TRADABLE_CACHE_TTL_S.

        When force=True, bypasses the cache TTL (used at startup).
        """
        now = time.monotonic()
        if not force and (now - self._tradable_refresh_ts) < TRADABLE_CACHE_TTL_S:
            return self._tradable_assets

        async with self._db_session_factory() as session:
            result = await session.execute(
                select(Asset).where(
                    Asset.exchange_id == self._exchange_db_id,
                    Asset.is_tradable.is_(True),
                )
            )
            self._tradable_assets = list(result.scalars().all())

        self._tradable_refresh_ts = now
        return self._tradable_assets

    # ── Tier 0: Price-Only Loop (15s) ──────────────────────────

    async def _tier0_loop(self) -> None:
        """Fetch tickers every 15s, update price fields via assembler."""
        while self._running:
            t0 = time.monotonic()
            try:
                assets = await self._refresh_tradable_assets()
                self._detect_new_assets(assets)

                if not assets or self._ccxt_exchange is None:
                    await asyncio.sleep(TIER0_INTERVAL_S)
                    continue

                symbols = [a.symbol for a in assets]
                tickers = await self._fetch_tickers(symbols)

                for asset in assets:
                    ticker = tickers.get(asset.symbol)
                    if not ticker:
                        continue

                    price = ticker.get("last")
                    bid = ticker.get("bid")
                    ask = ticker.get("ask")
                    last_trade_ts = ticker.get("datetime")

                    fields: dict[str, Any] = {
                        "price": price,
                        "bid": bid,
                        "ask": ask,
                        "spread_pct": compute_spread_pct(bid, ask),
                        "last_trade_ts": last_trade_ts,
                        "source": "ticker",
                    }
                    await self._assemble_and_flush(asset.id, tier=0, fields=fields)

            except asyncio.CancelledError:
                return
            except Exception:
                logger.error("Tier 0 cycle error", exc_info=True)

            elapsed = time.monotonic() - t0
            if elapsed > TIER0_INTERVAL_S:
                logger.warning("Tier 0 overrun: %.1fs > %ds", elapsed, TIER0_INTERVAL_S)
            await asyncio.sleep(max(0, TIER0_INTERVAL_S - elapsed))

    # ── Tier 1: Short-Horizon Metrics Loop (60s) ───────────────

    async def _tier1_loop(self) -> None:
        """Fetch 1H OHLCV, compute short-horizon metrics every 60s."""
        while self._running:
            t0 = time.monotonic()
            try:
                assets = await self._refresh_tradable_assets()

                if not assets or self._ccxt_exchange is None:
                    await asyncio.sleep(self._tier1_interval)
                    continue

                tasks = []
                for asset in assets:
                    tasks.append(self._tier1_process_asset(asset))
                await asyncio.gather(*tasks, return_exceptions=True)

            except asyncio.CancelledError:
                return
            except Exception:
                logger.error("Tier 1 cycle error", exc_info=True)

            elapsed = time.monotonic() - t0
            if elapsed > self._tier1_interval:
                self._tier1_overrun_count += 1
                logger.warning(
                    "Tier 1 overrun: %.1fs > %.0fs (consecutive: %d)",
                    elapsed, self._tier1_interval, self._tier1_overrun_count,
                )
                if self._tier1_overrun_count >= 3:
                    self._tier1_interval *= 1.5
                    logger.warning(
                        "Tier 1 interval increased to %.0fs due to consecutive overruns",
                        self._tier1_interval,
                    )
                    self._tier1_overrun_count = 0
            else:
                self._tier1_overrun_count = 0

            await asyncio.sleep(max(0, self._tier1_interval - elapsed))

    async def _tier1_process_asset(self, asset: Asset) -> None:
        """Fetch 1H OHLCV for a single asset, compute Tier 1 metrics."""
        async with self._ohlcv_semaphore:
            needs_warmup = asset.id not in self._warmed_1h_assets
            limit = WARMUP_1H_BARS if needs_warmup else OHLCV_1H_FETCH_LIMIT

            bars = await self._fetch_ohlcv(asset.symbol, "1h", limit=limit)
            if not bars:
                return

            await self._write_redis_ohlcv(asset.id, "1h", bars)
            await self._trim_redis_ohlcv(asset.id, "1h", OHLCV_1H_MAX_BARS)
            self._warmed_1h_assets.add(asset.id)

            fields = await self._compute_tier1_metrics(asset.id)
            await self._assemble_and_flush(asset.id, tier=1, fields=fields)

    # ── Tier 2: Deep-Horizon Metrics Loop (5min) ───────────────

    async def _tier2_loop(self) -> None:
        """Fetch 1D OHLCV, compute deep metrics, persist to DB every 5min."""
        while self._running:
            t0 = time.monotonic()
            try:
                assets = await self._refresh_tradable_assets()

                if not assets or self._ccxt_exchange is None:
                    await asyncio.sleep(self._tier2_interval)
                    continue

                tasks = []
                for asset in assets:
                    tasks.append(self._tier2_process_asset(asset))
                await asyncio.gather(*tasks, return_exceptions=True)

                # Tier 3: Persist to DB
                await self._tier3_persist()

                # DB prune check
                now = time.monotonic()
                if now - self._last_prune_ts > DB_PRUNE_INTERVAL_S:
                    pruned = await self._prune_db_ohlcv()
                    self._last_prune_ts = now
                    if pruned > 0:
                        logger.info("DB OHLCV prune: removed %d rows", pruned)

            except asyncio.CancelledError:
                return
            except Exception:
                logger.error("Tier 2 cycle error", exc_info=True)

            elapsed = time.monotonic() - t0
            if elapsed > self._tier2_interval:
                self._tier2_overrun_count += 1
                logger.warning(
                    "Tier 2 overrun: %.1fs > %.0fs (consecutive: %d)",
                    elapsed, self._tier2_interval, self._tier2_overrun_count,
                )
                if self._tier2_overrun_count >= 3:
                    self._tier2_interval *= 1.5
                    logger.warning(
                        "Tier 2 interval increased to %.0fs due to consecutive overruns",
                        self._tier2_interval,
                    )
                    self._tier2_overrun_count = 0
            else:
                self._tier2_overrun_count = 0

            await asyncio.sleep(max(0, self._tier2_interval - elapsed))

    async def _tier2_process_asset(self, asset: Asset) -> None:
        """Fetch 1D OHLCV for a single asset, compute Tier 2 metrics."""
        async with self._ohlcv_semaphore:
            needs_warmup = asset.id not in self._warmed_1d_assets
            limit = WARMUP_1D_BARS if needs_warmup else OHLCV_1D_FETCH_LIMIT

            bars = await self._fetch_ohlcv(asset.symbol, "1d", limit=limit)
            if not bars:
                return

            await self._write_redis_ohlcv(asset.id, "1d", bars)
            await self._trim_redis_ohlcv(asset.id, "1d", OHLCV_1D_MAX_BARS)
            self._warmed_1d_assets.add(asset.id)

            fields = await self._compute_tier2_metrics(asset.id)
            await self._assemble_and_flush(asset.id, tier=2, fields=fields)

    # ── New Asset Detection ────────────────────────────────────

    def _detect_new_assets(self, assets: list[Asset]) -> None:
        """Detect newly-tradable assets and init their snapshot state."""
        current_ids = {a.id for a in assets}

        # New assets
        for asset in assets:
            if asset.id not in self._known_asset_ids:
                logger.info(
                    "New tradable asset detected: %s (id=%d) — staging warm-up",
                    asset.symbol, asset.id,
                )
                self._known_asset_ids.add(asset.id)
                self._snapshots[asset.id] = {}
                self._symbols[asset.id] = asset.symbol

        # Removed assets
        removed = self._known_asset_ids - current_ids
        for aid in removed:
            logger.info("Asset removed from tradable set: id=%d", aid)
            self._known_asset_ids.discard(aid)
            self._snapshots.pop(aid, None)
            self._symbols.pop(aid, None)

    # ── Single-Writer Snapshot Assembler ───────────────────────

    async def _assemble_and_flush(
        self, asset_id: int, tier: int, fields: dict[str, Any]
    ) -> None:
        """
        Merge fields into in-memory canonical snapshot, then enqueue for
        Redis flush via the single-writer coroutine.

        Race-condition safety (Option A — single-writer queue):
        - In-memory merge is serialised by _snapshot_lock (no I/O under lock).
        - After merge, the snapshot copy is placed into _pending_flushes[asset_id],
          overwriting any older pending entry for the same asset.
        - A dedicated _flush_writer coroutine is the ONLY code path that performs
          Redis HSET/PUBLISH. Because it is a single sequential coroutine, writes
          for any given asset are strictly ordered — stale overwrites are impossible.
        """
        # 1. In-memory merge (lock scope: microseconds, no I/O)
        async with self._snapshot_lock:
            snap = self._snapshots.setdefault(asset_id, {})
            snap.update(fields)
            snap["computed_at"] = _utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
            snapshot_copy = dict(snap)  # shallow copy for flush

        # 2. Enqueue for single-writer flush (coalesced per asset_id).
        #    Only the newest snapshot_copy survives — older pending entries
        #    for the same asset_id are silently replaced.
        async with self._flush_lock:
            self._pending_flushes[asset_id] = (tier, snapshot_copy)
        self._flush_event.set()

    async def _flush_writer(self) -> None:
        """
        Dedicated single-writer coroutine for Redis HSET + PUBLISH.

        Drains _pending_flushes on each wake-up. Because this is the ONLY
        coroutine that writes snapshots to Redis, and it processes assets
        sequentially, write ordering is guaranteed — a newer snapshot for
        asset X can never be overwritten by an older one.
        """
        while self._running:
            try:
                # Wait for work
                await self._flush_event.wait()
                self._flush_event.clear()

                # Atomically grab all pending work
                async with self._flush_lock:
                    batch = dict(self._pending_flushes)
                    self._pending_flushes.clear()

                if not batch:
                    continue

                for asset_id, (tier, snapshot_copy) in batch.items():
                    key = f"nexus:snapshot:{asset_id}"

                    # Redis HSET (non-fatal)
                    try:
                        mapping = {k: json.dumps(v) for k, v in snapshot_copy.items()}
                        await self._redis.hset(key, mapping=mapping)
                        await self._redis.expire(key, SNAPSHOT_TTL_S)
                    except Exception:
                        logger.error(
                            "Redis HSET failed for asset %d", asset_id, exc_info=True
                        )

                    # Redis PUBLISH (non-fatal)
                    try:
                        symbol = self._symbols.get(asset_id, "")
                        payload = json.dumps({
                            "type": "snapshot_update",
                            "tier": tier,
                            "exchange_id": self._exchange_db_id,
                            "assets": [{
                                "asset_id": asset_id,
                                "symbol": symbol,
                                "snapshot": snapshot_copy,
                            }],
                            "timestamp": snapshot_copy.get("computed_at"),
                        })
                        await self._redis.publish("nexus:events:ticker", payload)
                    except Exception:
                        logger.error(
                            "Redis PUBLISH failed for asset %d", asset_id,
                            exc_info=True,
                        )

            except asyncio.CancelledError:
                return
            except Exception:
                logger.error("Flush writer error", exc_info=True)

    async def _drain_flush_queue(self) -> None:
        """
        Force-drain all pending flushes synchronously (used by tests and stop()).

        This ensures that after calling _assemble_and_flush() in a test, the
        Redis state is immediately consistent without needing to await the
        background _flush_writer coroutine.
        """
        async with self._flush_lock:
            batch = dict(self._pending_flushes)
            self._pending_flushes.clear()
        self._flush_event.clear()

        for asset_id, (tier, snapshot_copy) in batch.items():
            key = f"nexus:snapshot:{asset_id}"
            try:
                mapping = {k: json.dumps(v) for k, v in snapshot_copy.items()}
                await self._redis.hset(key, mapping=mapping)
                await self._redis.expire(key, SNAPSHOT_TTL_S)
            except Exception:
                logger.error(
                    "Redis HSET failed for asset %d (drain)", asset_id, exc_info=True
                )
            try:
                symbol = self._symbols.get(asset_id, "")
                payload = json.dumps({
                    "type": "snapshot_update",
                    "tier": tier,
                    "exchange_id": self._exchange_db_id,
                    "assets": [{
                        "asset_id": asset_id,
                        "symbol": symbol,
                        "snapshot": snapshot_copy,
                    }],
                    "timestamp": snapshot_copy.get("computed_at"),
                })
                await self._redis.publish("nexus:events:ticker", payload)
            except Exception:
                logger.error(
                    "Redis PUBLISH failed for asset %d (drain)", asset_id,
                    exc_info=True,
                )

    # ── CCXT Fetch Methods ─────────────────────────────────────

    async def _fetch_tickers(self, symbols: list[str]) -> dict[str, dict]:
        """
        Batch fetch tickers via CCXT.

        Returns {symbol: ticker_dict}. Only reads 'last', 'bid', 'ask', 'datetime'
        from each ticker. ticker['percentage'] and ticker['baseVolume'] are NEVER used.
        """
        if not self._ccxt_exchange or not symbols:
            return {}
        try:
            raw = await self._ccxt_exchange.fetch_tickers(symbols)
            return raw
        except ccxt_async.NetworkError as e:
            logger.warning("CCXT NetworkError in fetch_tickers: %s", e)
            return {}
        except ccxt_async.ExchangeNotAvailable as e:
            logger.warning("CCXT ExchangeNotAvailable in fetch_tickers: %s", e)
            return {}
        except ccxt_async.RateLimitExceeded:
            logger.warning("CCXT rate limit exceeded — backing off Tier 0")
            await asyncio.sleep(TIER0_INTERVAL_S * 2)
            return {}
        except Exception:
            logger.error("Unexpected error in fetch_tickers", exc_info=True)
            return {}

    async def _fetch_ohlcv(
        self, symbol: str, timeframe: str, limit: int
    ) -> list[list]:
        """
        Fetch OHLCV bars from CCXT.

        Returns list of [ts_ms, open, high, low, close, volume].
        """
        if not self._ccxt_exchange:
            return []
        try:
            bars = await self._ccxt_exchange.fetch_ohlcv(
                symbol, timeframe, limit=limit
            )
            return bars
        except ccxt_async.NetworkError as e:
            logger.warning("CCXT NetworkError fetching %s %s: %s", symbol, timeframe, e)
            return []
        except ccxt_async.ExchangeNotAvailable as e:
            logger.warning("CCXT ExchangeNotAvailable fetching %s %s: %s", symbol, timeframe, e)
            return []
        except ccxt_async.RateLimitExceeded:
            logger.warning("CCXT rate limit exceeded fetching %s %s", symbol, timeframe)
            return []
        except Exception:
            logger.error("Unexpected error fetching OHLCV %s %s", symbol, timeframe, exc_info=True)
            return []

    # ── Redis OHLCV Cache ──────────────────────────────────────

    async def _write_redis_ohlcv(
        self, asset_id: int, timeframe: str, bars: list[list]
    ) -> None:
        """Write OHLCV bars to Redis sorted set. Score = ts_ms, value = JSON bar."""
        key = f"nexus:ohlcv:{timeframe}:{asset_id}"
        ttl = OHLCV_1H_TTL_S if timeframe == "1h" else OHLCV_1D_TTL_S

        try:
            pipe = self._redis.pipeline()
            for bar in bars:
                ts_ms = bar[0]
                pipe.zadd(key, {json.dumps(bar): ts_ms})
            pipe.expire(key, ttl)
            await pipe.execute()
        except Exception:
            logger.error(
                "Redis OHLCV write failed for asset %d %s", asset_id, timeframe,
                exc_info=True,
            )

    async def _read_redis_ohlcv(
        self, asset_id: int, timeframe: str, limit: int = 0
    ) -> list[list]:
        """Read OHLCV bars from Redis sorted set, ordered by timestamp ascending."""
        key = f"nexus:ohlcv:{timeframe}:{asset_id}"
        try:
            if limit > 0:
                # Get the latest `limit` bars
                raw = await self._redis.zrange(key, -limit, -1)
            else:
                raw = await self._redis.zrange(key, 0, -1)

            return [_parse_bar(r) for r in raw]
        except Exception:
            logger.error(
                "Redis OHLCV read failed for asset %d %s", asset_id, timeframe,
                exc_info=True,
            )
            return []

    async def _trim_redis_ohlcv(
        self, asset_id: int, timeframe: str, max_bars: int
    ) -> None:
        """Trim Redis sorted set to max_bars entries (remove oldest)."""
        key = f"nexus:ohlcv:{timeframe}:{asset_id}"
        try:
            await self._redis.zremrangebyrank(key, 0, -(max_bars + 1))
        except Exception:
            logger.error(
                "Redis OHLCV trim failed for asset %d %s", asset_id, timeframe,
                exc_info=True,
            )

    async def _read_redis_ohlcv_by_score(
        self, asset_id: int, timeframe: str, min_ms: int, max_ms: int
    ) -> list[list]:
        """Read bars from Redis sorted set within a score (timestamp) range."""
        key = f"nexus:ohlcv:{timeframe}:{asset_id}"
        try:
            raw = await self._redis.zrangebyscore(key, min_ms, max_ms)
            return [_parse_bar(r) for r in raw]
        except Exception:
            logger.error(
                "Redis OHLCV range read failed for asset %d %s",
                asset_id, timeframe, exc_info=True,
            )
            return []

    async def _read_redis_ohlcv_latest_before(
        self, asset_id: int, timeframe: str, max_ms_exclusive: int
    ) -> Optional[list]:
        """
        Get the most recent bar with score strictly before max_ms_exclusive.

        Uses ZREVRANGEBYSCORE with exclusive upper bound.
        """
        key = f"nexus:ohlcv:{timeframe}:{asset_id}"
        try:
            # "(" prefix means exclusive in Redis
            raw = await self._redis.zrevrangebyscore(
                key, f"({max_ms_exclusive}", "-inf", start=0, num=1
            )
            if raw:
                return _parse_bar(raw[0])
            return None
        except Exception:
            logger.error(
                "Redis OHLCV latest-before read failed for asset %d %s",
                asset_id, timeframe, exc_info=True,
            )
            return None

    # ── source_candle_ts Computation ───────────────────────────

    async def _compute_source_candle_ts(self, asset_id: int) -> Optional[str]:
        """
        Compute source_candle_ts using the hour-boundary rule.

        Returns the ISO timestamp of the most recent fully closed 1H candle,
        determined by: ts < floor_to_hour(utc_now()). Never returns the open candle.
        """
        current_hour_start_ms = _floor_to_hour_ms(_utcnow())
        bar = await self._read_redis_ohlcv_latest_before(
            asset_id, "1h", current_hour_start_ms
        )
        if bar is None:
            return None
        return _ts_ms_to_iso(bar[0])

    async def _compute_source_candle_ts_ms(self, asset_id: int) -> Optional[int]:
        """Return source_candle_ts as epoch ms, or None."""
        current_hour_start_ms = _floor_to_hour_ms(_utcnow())
        bar = await self._read_redis_ohlcv_latest_before(
            asset_id, "1h", current_hour_start_ms
        )
        if bar is None:
            return None
        return int(bar[0])

    # ── Metric Computation ─────────────────────────────────────

    async def _compute_tier1_metrics(self, asset_id: int) -> dict[str, Any]:
        """Compute Tier 1 metrics: change_1h, change_4h, volume_1h, volume_4h, high/low_24h, source_candle_ts."""
        source_ts = await self._compute_source_candle_ts(asset_id)
        source_ts_ms = await self._compute_source_candle_ts_ms(asset_id)

        if source_ts is None or source_ts_ms is None:
            return {
                "change_1h": None, "change_4h": None,
                "volume_1h": None, "volume_4h": None,
                "high_24h": None, "low_24h": None,
                "source_candle_ts": None,
            }

        # Get current price from in-memory snapshot
        current_price = self._snapshots.get(asset_id, {}).get("price")

        # change_1h: close of bar 1h before anchor
        change_1h = await self._compute_change_n(asset_id, current_price, source_ts_ms, 1, "1h")
        # change_4h: close of bar 4h before anchor
        change_4h = await self._compute_change_n(asset_id, current_price, source_ts_ms, 4, "1h")

        # volume_1h: single bar at anchor
        volume_1h = await self._compute_volume_n(asset_id, source_ts_ms, 1)
        # volume_4h: 4 bars ending at anchor
        volume_4h = await self._compute_volume_n(asset_id, source_ts_ms, 4)

        # high/low 24h
        high_24h, low_24h = await self._compute_high_low_24h(asset_id, source_ts_ms)

        return {
            "change_1h": change_1h,
            "change_4h": change_4h,
            "volume_1h": volume_1h,
            "volume_4h": volume_4h,
            "high_24h": high_24h,
            "low_24h": low_24h,
            "source_candle_ts": source_ts,
        }

    async def _compute_tier2_metrics(self, asset_id: int) -> dict[str, Any]:
        """Compute Tier 2 metrics: change_12h, change_24h, change_7d, volume_24h."""
        source_ts_ms = await self._compute_source_candle_ts_ms(asset_id)

        if source_ts_ms is None:
            return {
                "change_12h": None, "change_24h": None,
                "change_7d": None, "volume_24h": None,
            }

        current_price = self._snapshots.get(asset_id, {}).get("price")

        change_12h = await self._compute_change_n(asset_id, current_price, source_ts_ms, 12, "1h")
        change_24h = await self._compute_change_n(asset_id, current_price, source_ts_ms, 24, "1h")
        change_7d = await self._compute_change_7d(asset_id, current_price, source_ts_ms)
        volume_24h = await self._compute_volume_n(asset_id, source_ts_ms, 24)

        return {
            "change_12h": change_12h,
            "change_24h": change_24h,
            "change_7d": change_7d,
            "volume_24h": volume_24h,
        }

    async def _compute_change_n(
        self,
        asset_id: int,
        current_price: Optional[float],
        source_ts_ms: int,
        n_hours: int,
        timeframe: str,
    ) -> Optional[float]:
        """Compute change_Nh: percentage change vs close N hours before anchor."""
        if current_price is None:
            return None

        target_ms = source_ts_ms - (n_hours * 3600 * 1000)
        bar = await self._read_redis_ohlcv_latest_before(
            asset_id, timeframe, target_ms + 1  # inclusive: bar with ts <= target_ms
        )
        # Use zrangebyscore to get bar at exactly target_ms or nearest before
        bars = await self._read_redis_ohlcv_by_score(asset_id, timeframe, target_ms, target_ms)
        if bars:
            ref_close = bars[0][4]  # close is index 4
        elif bar is not None:
            ref_close = bar[4]
        else:
            return None

        return compute_change_pct(current_price, ref_close)

    async def _compute_change_7d(
        self,
        asset_id: int,
        current_price: Optional[float],
        source_ts_ms: int,
    ) -> Optional[float]:
        """Compute change_7d using 1D sorted set."""
        if current_price is None:
            return None

        target_ms = source_ts_ms - (7 * 86400 * 1000)
        # Try exact match first
        bars = await self._read_redis_ohlcv_by_score(asset_id, "1d", target_ms, target_ms)
        if bars:
            ref_close = bars[0][4]
        else:
            # Nearest bar before target
            bar = await self._read_redis_ohlcv_latest_before(asset_id, "1d", target_ms + 1)
            if bar is None:
                return None
            ref_close = bar[4]

        return compute_change_pct(current_price, ref_close)

    async def _compute_volume_n(
        self, asset_id: int, source_ts_ms: int, n_hours: int
    ) -> Optional[float]:
        """Sum volume over the N most recent closed 1H candles ending at anchor."""
        min_ms = source_ts_ms - ((n_hours - 1) * 3600 * 1000)
        bars = await self._read_redis_ohlcv_by_score(asset_id, "1h", min_ms, source_ts_ms)
        if not bars:
            return None
        total = sum(b[5] for b in bars)  # volume is index 5
        return round(total, 0)

    async def _compute_high_low_24h(
        self, asset_id: int, source_ts_ms: int
    ) -> tuple[Optional[float], Optional[float]]:
        """Compute 24h high and low from 1H bars, including current open candle."""
        min_ms = source_ts_ms - (24 * 3600 * 1000)

        # Closed bars in 24h window
        bars = await self._read_redis_ohlcv_by_score(asset_id, "1h", min_ms, source_ts_ms)

        # Include current open candle
        current_hour_start_ms = _floor_to_hour_ms(_utcnow())
        open_bars = await self._read_redis_ohlcv_by_score(
            asset_id, "1h", current_hour_start_ms, "+inf"
        )
        all_bars = bars + open_bars

        if not all_bars:
            return None, None

        high = max(b[2] for b in all_bars)  # high is index 2
        low = min(b[3] for b in all_bars)   # low is index 3
        return high, low

    # ── Tier 3: DB Persistence ─────────────────────────────────

    async def _tier3_persist(self) -> None:
        """Persist current snapshots and OHLCV to PostgreSQL."""
        try:
            async with self._db_session_factory() as session:
                for asset_id, snapshot in self._snapshots.items():
                    if not snapshot:
                        continue

                    # Update Asset.market_snapshot and snapshot_updated_at
                    await session.execute(
                        update(Asset)
                        .where(Asset.id == asset_id)
                        .values(
                            market_snapshot=snapshot,
                            snapshot_updated_at=_utcnow(),
                        )
                    )

                # Persist OHLCV bars from Redis to DB
                for asset_id in list(self._known_asset_ids):
                    await self._persist_ohlcv_for_asset(session, asset_id)

                await session.commit()

        except Exception:
            logger.error("Tier 3 DB persist failed", exc_info=True)

    async def _persist_ohlcv_for_asset(
        self, session: AsyncSession, asset_id: int
    ) -> None:
        """Persist latest OHLCV bars from Redis to PostgreSQL for one asset."""
        for tf, limit in [("1h", OHLCV_1H_FETCH_LIMIT), ("1d", OHLCV_1D_FETCH_LIMIT)]:
            bars = await self._read_redis_ohlcv(asset_id, tf, limit=limit + 1)
            if not bars:
                continue

            for bar in bars:
                ts_ms, o, h, l, c, v = bar[0], bar[1], bar[2], bar[3], bar[4], bar[5]
                ts = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)

                stmt = pg_insert(OHLCV).values(
                    asset_id=asset_id,
                    timeframe=tf,
                    timestamp=ts,
                    open=o,
                    high=h,
                    low=l,
                    close=c,
                    volume=v,
                ).on_conflict_do_update(
                    constraint="uq_ohlcv",
                    set_={
                        "open": o, "high": h, "low": l, "close": c, "volume": v,
                    },
                )
                await session.execute(stmt)

    async def _prune_db_ohlcv(self) -> int:
        """Delete expired OHLCV rows from PostgreSQL."""
        now = _utcnow()
        try:
            async with self._db_session_factory() as session:
                # 1H: older than 7 days
                cutoff_1h = now.replace(tzinfo=None) - __import__("datetime").timedelta(hours=DB_RETENTION_1H_HOURS)
                result_1h = await session.execute(
                    delete(OHLCV).where(
                        OHLCV.timeframe == "1h",
                        OHLCV.timestamp < cutoff_1h,
                    )
                )

                # 1D: older than 30 days
                cutoff_1d = now.replace(tzinfo=None) - __import__("datetime").timedelta(days=DB_RETENTION_1D_DAYS)
                result_1d = await session.execute(
                    delete(OHLCV).where(
                        OHLCV.timeframe == "1d",
                        OHLCV.timestamp < cutoff_1d,
                    )
                )

                await session.commit()
                return (result_1h.rowcount or 0) + (result_1d.rowcount or 0)
        except Exception:
            logger.error("DB OHLCV prune failed", exc_info=True)
            return 0

    # ── Cold-Start Backfill ────────────────────────────────────

    async def _backfill_ohlcv_if_needed(self, asset_id: int, symbol: str) -> None:
        """Check Redis cache depth; backfill from DB or CCXT if needed."""
        key_1h = f"nexus:ohlcv:1h:{asset_id}"
        try:
            count = await self._redis.zcard(key_1h)
        except Exception:
            count = 0

        if count >= 24:
            return  # sufficient data

        logger.info("Cold-start backfill for asset %d (%s): Redis has %d 1H bars", asset_id, symbol, count)

        # Try PostgreSQL first
        async with self._db_session_factory() as session:
            result = await session.execute(
                select(OHLCV)
                .where(OHLCV.asset_id == asset_id, OHLCV.timeframe == "1h")
                .order_by(OHLCV.timestamp.desc())
                .limit(WARMUP_1H_BARS)
            )
            db_bars = list(result.scalars().all())

        if len(db_bars) >= 24:
            bars = [
                [int(b.timestamp.replace(tzinfo=timezone.utc).timestamp() * 1000),
                 b.open, b.high, b.low, b.close, b.volume]
                for b in reversed(db_bars)
            ]
            await self._write_redis_ohlcv(asset_id, "1h", bars)
            logger.info("Backfilled %d 1H bars from DB for asset %d", len(bars), asset_id)
            return

        # Fallback: CCXT
        bars = await self._fetch_ohlcv(symbol, "1h", limit=WARMUP_1H_BARS)
        if bars:
            await self._write_redis_ohlcv(asset_id, "1h", bars)
            logger.info("Backfilled %d 1H bars from CCXT for asset %d", len(bars), asset_id)

        # Also backfill 1D
        bars_1d = await self._fetch_ohlcv(symbol, "1d", limit=WARMUP_1D_BARS)
        if bars_1d:
            await self._write_redis_ohlcv(asset_id, "1d", bars_1d)
            logger.info("Backfilled %d 1D bars from CCXT for asset %d", len(bars_1d), asset_id)

    # ── Public Read Methods (for REST router) ──────────────────

    async def get_snapshot_from_redis(self, asset_id: int) -> Optional[dict[str, Any]]:
        """Read a single asset snapshot from Redis HASH."""
        key = f"nexus:snapshot:{asset_id}"
        try:
            raw = await self._redis.hgetall(key)
            if not raw:
                return None
            return {k: json.loads(v) for k, v in raw.items()}
        except Exception:
            logger.error("Redis HGETALL failed for asset %d", asset_id, exc_info=True)
            return None

    async def get_snapshots_from_redis(
        self, asset_ids: list[int]
    ) -> dict[int, Optional[dict[str, Any]]]:
        """Read multiple snapshots via pipelined HGETALL. Single Redis round-trip."""
        if not asset_ids:
            return {}

        try:
            pipe = self._redis.pipeline()
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

    async def get_ohlcv_from_redis(
        self, asset_id: int, timeframe: str, limit: int
    ) -> list[dict[str, Any]]:
        """Read OHLCV bars from Redis, formatted for API response."""
        bars = await self._read_redis_ohlcv(asset_id, timeframe, limit=limit)
        return [
            {
                "timestamp": _ts_ms_to_iso(b[0]),
                "open": b[1],
                "high": b[2],
                "low": b[3],
                "close": b[4],
                "volume": b[5],
            }
            for b in bars
        ]
