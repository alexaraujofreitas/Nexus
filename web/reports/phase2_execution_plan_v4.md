# Phase 2 Execution Plan v4 — Market Data Service + Snapshot Pipeline

**Status:** PENDING APPROVAL — do NOT implement until explicitly authorized.
**Date:** 2026-04-03
**Revision:** v4 — fix-only revision of v3. Removes bulk snapshot key, narrows lock scope, adds Redis error handling to pseudocode, documents change-vs-volume semantics, caches tradable assets, corrects field count to 18. No architectural changes.
**Depends on:** Phase 1 (Asset Management) — ACCEPTED
**Constraints:** No scanner changes, no engine changes, no config.yaml changes, no frontend computation, no SQLite authority, no fake data. Phase 1 contracts preserved.

---

## Section 1 — Exact Files to Create or Modify

### CREATE

| File | Purpose |
|------|---------|
| `web/backend/app/services/market_data.py` | MarketDataService class — singleton async service. Owns the full pipeline: CCXT fetch → Redis hot cache (OHLCV sorted sets + HASH snapshots) → PostgreSQL persistence → Redis pub/sub WS delivery. Contains the single-writer snapshot assembler that prevents tier race conditions. |
| `web/backend/app/api/market_data.py` | REST router: `GET /market-data/snapshots`, `GET /market-data/snapshots/{asset_id}`, `GET /market-data/ohlcv/{asset_id}`. Reads from Redis hot cache first (pipelined HGETALL per asset), falls back to PostgreSQL. |
| `web/backend/alembic/versions/<hash>_add_ohlcv_indexes.py` | Migration: adds `idx_ohlcv_timestamp` (retention pruning) and `idx_ohlcv_asset_tf_latest` (descending timestamp, "latest N bars" queries). No new tables, no new columns. |
| `web/backend/tests/test_phase2_market_data.py` | Full test suite: unit tests (metric formulas, ticker field exclusion proofs, source_candle_ts hour-boundary), integration tests (pgserver + fakeredis for cache/DB/WS/REST paths, staged warm-up, TTL, atomicity, race conditions). |

### MODIFY

| File | Exact change |
|------|-------------|
| `web/backend/main.py` | Extend `lifespan` context manager: import `MarketDataService`, instantiate with `db_session_factory` + `redis` + `settings`, call `mds.start()` before yield, `mds.stop()` after yield. Add `from app.api import market_data` and `app.include_router(market_data.router, prefix="/api/v1")`. |
| `web/backend/app/api/__init__.py` | Add `from . import market_data` to the router registration block. |

### NOT TOUCHED

| File | Reason |
|------|--------|
| `web/backend/app/models/trading.py` | No schema changes. `Asset.market_snapshot` (JSONB), `Asset.snapshot_updated_at`, OHLCV table all exist from Phase 0/1. |
| `web/backend/app/ws/manager.py` | `"ticker"` already in CHANNELS. Redis bridge (`psubscribe("nexus:events:*")`) already routes `nexus:events:ticker` to WS subscribers. No modification needed. |
| `web/backend/app/api/engine.py` | MDS uses direct CCXT — no engine command wrappers needed. |
| `web/engine/main.py` | No engine changes — constraint. |
| `core/scanning/scanner.py` | No scanner changes — constraint. |
| `core/market_data/exchange_manager.py` | Desktop-only singleton. MDS has its own CCXT instance. |
| `config.yaml` | No runtime config changes — constraint. |
| Any frontend file | Phase 2 is backend-only. Frontend consumes via existing WS `ticker` channel + new REST endpoints. |
| `web/backend/app/api/exchanges.py` | Phase 1 endpoints unchanged. sync-assets ON CONFLICT already preserves `market_snapshot`/`snapshot_updated_at`. |

**Consistency check:** Every file in CREATE or MODIFY appears in the implementation. No file is listed in MODIFY that later says "no changes." No engine files are touched.

---

## Section 2 — MarketDataService Architecture

### Architecture Decision: Direct CCXT (CHOSEN)

MDS creates its own read-only CCXT instance using exchange credentials decrypted from PostgreSQL via the vault service. This is the only architecture consistent with the "no engine changes" constraint.

**Why not engine-delegated:** The engine command handler registry (`web/engine/main.py`) has no `market_data.fetch_tickers` command. Adding one would require modifying `web/engine/main.py`, which is out of scope. Direct CCXT avoids this entirely — MDS is self-contained.

**Trade-off accepted:** Two CCXT instances exist (engine + MDS). MDS is read-only (`fetch_tickers`, `fetch_ohlcv` only — no orders, no balance). Both use `enableRateLimit: True`. Bybit's public API budget (120 req/5s) is large enough to accommodate both without contention.

### Class Design

```
MarketDataService (singleton)
├── __init__(db_session_factory, redis: aioredis.Redis, settings)
├── _ccxt_exchange: ccxt.Exchange | None       (lazy-init)
├── _redis: aioredis.Redis                     (injected)
├── _running: bool
├── _tasks: dict[str, asyncio.Task]            (one per tier loop)
├── _known_asset_ids: set[int]                 (warm-up tracking)
├── _snapshots: dict[int, dict]                (in-memory canonical snapshots, keyed by asset_id)
├── _snapshot_lock: asyncio.Lock               (serializes in-memory snapshot merge ONLY)
├── _tradable_assets: list[Asset]              (cached tradable asset set)
├── _tradable_refresh_ts: float                (epoch of last tradable set refresh)
│
├── start() → spawns tier tasks
├── stop() → cancels all tasks, closes CCXT
│
├── _tier0_loop()           # price-only: fetch_tickers → assemble → flush
├── _tier1_loop()           # 1H metrics: fetch 1h OHLCV → Redis OHLCV cache → assemble → flush
├── _tier2_loop()           # deep metrics: fetch 1d OHLCV → Redis OHLCV cache → assemble → flush → DB persist
│
├── _assemble_and_flush(asset_id, tier, fields: dict) → None
│   # THE SINGLE WRITER: lock scope limited to in-memory merge only.
│   # Redis I/O and DB persist happen OUTSIDE the lock.
│
├── _refresh_tradable_assets() → list[Asset]
│   # Queries DB for is_tradable=true. Cached for 60s.
│
├── _fetch_tickers(symbols) → dict[str, TickerData]
├── _fetch_ohlcv(symbol, tf, limit) → list[list]
├── _compute_source_candle_ts(asset_id) → str | None
├── _compute_tier1_metrics(asset_id) → dict
├── _compute_tier2_metrics(asset_id) → dict
├── _warm_up_asset(asset_id, symbol) → None   (staged: price → partial → full)
│
├── _write_redis_ohlcv(asset_id, tf, bars) → None
├── _read_redis_ohlcv(asset_id, tf, limit) → list[list]
├── _trim_redis_ohlcv(asset_id, tf, max_bars) → None
│
├── _persist_snapshot_to_db(asset_id, snapshot) → None
├── _persist_ohlcv_to_db(asset_id, tf, bars) → None
├── _prune_db_ohlcv() → int
│
└── get_exchange() → ccxt.Exchange  (lazy init)
```

### Single-Writer Snapshot Assembler (Race Condition Fix)

**Problem:** If tiers independently read-merge-write snapshots, interleaving causes one tier to overwrite another tier's fields.

**Solution: In-memory canonical snapshot with single-writer flush.**

Every tier computes its field values, then calls `_assemble_and_flush(asset_id, tier, fields)`. This method:

1. Acquires `_snapshot_lock` — **lock scope is limited to in-memory merge only.**
2. Merges `fields` into `_snapshots[asset_id]`. Only the keys present in `fields` are overwritten — other keys are preserved.
3. Sets `computed_at` to `utcnow()`.
4. Takes a shallow copy of the merged snapshot (`snapshot_copy = dict(snap)`).
5. Releases `_snapshot_lock`.
6. **Outside the lock:** writes `snapshot_copy` to Redis HASH (`HSET` with all 18 fields), wrapped in try/except.
7. **Outside the lock:** publishes `snapshot_copy` to `nexus:events:ticker` via Redis pub/sub, wrapped in try/except.
8. **Outside the lock, Tier 2 only:** persists `snapshot_copy` to PostgreSQL.

**Why lock scope is narrow:** The lock protects only the in-memory dict merge (microseconds, no I/O). Redis HSET and PUBLISH happen outside the lock. If two tiers call `_assemble_and_flush` near-simultaneously, they serialize only for the in-memory merge. The second tier's Redis write arrives after the first — this is correct because it has the later `computed_at`. Each `HSET` writes all 18 fields atomically (single Redis command), so no partial snapshot is ever visible in Redis.

**Why this prevents all race conditions:**
- Only `_assemble_and_flush` writes to `_snapshots[asset_id]`. No other code path writes snapshots.
- The lock serializes all in-memory merges. Each tier's fields are merged into the latest state.
- No GET-before-SET pattern exists — the canonical state is in memory, not read from Redis.
- Stale data regression is impossible because each tier only overwrites its own fields.

**Why Option C over Option A (HSET-only) or B (WATCH/MULTI):**
- Option A (HSET field-scoped, no lock): Each tier could HSET its own fields independently. However, the WS publish must send the complete snapshot, not just the changed fields. Without a single canonical copy, we'd need a HGETALL after every HSET to build the publish payload — doubling Redis round-trips and still risking inconsistency between the HSET and HGETALL.
- Option B (WATCH/MULTI): Adds retry loops (MULTI aborts if watched key changes). In a 15s Tier 0 cadence, retries introduce unpredictable latency.
- Option C: Zero Redis round-trips for read-merge. One HSET + one PUBLISH per flush. Deterministic timing. The in-memory dict is the single source of truth; Redis is a durable projection of it.

### Lifecycle

1. `main.py` `lifespan` creates `MarketDataService(session_factory, redis, settings)`.
2. `start()` queries DB for active exchange, initializes CCXT instance (demo mode, `enableRateLimit: True`, 15s timeout, `rateLimit: 350`), calls `load_markets()`, loads initial tradable assets into `_tradable_assets`, initializes `_snapshots` dict for all tradable assets (empty dicts), then spawns three `asyncio.Task`s: `_tier0_loop`, `_tier1_loop`, `_tier2_loop`.
3. On shutdown, `stop()` cancels all tasks, awaits them, calls `exchange.close()`.

### CCXT Instance Initialization

```
1. SELECT * FROM exchanges WHERE is_active = true LIMIT 1
2. If exchange.demo_mode: set Bybit demo URL overrides
3. Config: enableRateLimit=True, timeout=15000, rateLimit=350
4. Decrypt api_key/api_secret via vault service
5. load_markets() once
```

### Error Handling

| Error | Behaviour |
|-------|-----------|
| CCXT `NetworkError` / `ExchangeNotAvailable` | Log warning, skip cycle, retry next interval. No crash. |
| CCXT `RateLimitExceeded` | Back off 2× for one cycle, then restore. |
| Redis HSET/EXPIRE failure | Log error, skip Redis write. In-memory `_snapshots` dict remains valid. DB persist still attempted (Tier 2). |
| Redis PUBLISH failure | Log error, skip WS delivery. Redis HASH may or may not be current. DB persist still attempted (Tier 2). |
| DB write failure | Log error, skip DB persist. Redis hot cache and in-memory dict remain valid. Next Tier 2 cycle retries. |
| No active exchange in DB | MDS enters idle mode. Checks every 60s for an active exchange. |

---

## Section 3 — OHLCV Cache Design

### Dual-Layer Cache

**Redis is the operational cache.** All metric computation reads from Redis OHLCV sorted sets. PostgreSQL is the persistence/history layer — written to on Tier 2 cycles for crash recovery and REST fallback.

### Redis OHLCV Keys

| Key pattern | Content | Max entries | TTL |
|------------|---------|-------------|-----|
| `nexus:ohlcv:1h:{asset_id}` | Sorted set of 1H bars, score=`ts_ms`, value=JSON `[ts_ms, o, h, l, c, v]` per entry | 168 (7 days) | 48h (auto-renewed on every write; if MDS is down >48h, cold-start required) |
| `nexus:ohlcv:1d:{asset_id}` | Sorted set of 1D bars, score=`ts_ms`, value=JSON `[ts_ms, o, h, l, c, v]` per entry | 30 (30 days) | 7d (auto-renewed on every write) |

**Data structure:** Redis Sorted Set (`ZADD`) keyed by `ts_ms` as score. This gives O(log N) insert and O(log N + M) range queries. `ZRANGEBYSCORE` retrieves bars in a time window; `ZREMRANGEBYSCORE` trims old bars.

**Why 30 1D bars (not 14):** `change_7d` requires a bar from at least 7 days ago. 14 bars would work, but 30 bars provides a buffer for gap handling (exchange downtime, missing daily candles) and allows future extension to `change_30d` without a cache redesign.

### Append-Only Incremental Updates

Each Tier 1/2 cycle fetches only the latest N bars from CCXT (not the full history):

| Timeframe | Per-cycle fetch | Bars fetched |
|-----------|----------------|-------------|
| 1H | `fetch_ohlcv(symbol, "1h", limit=4)` | Latest 4 bars (3 closed + 1 open) |
| 1D | `fetch_ohlcv(symbol, "1d", limit=2)` | Latest 2 bars (1 closed + 1 open) |

Fetched bars are `ZADD`'d into the sorted set (idempotent — same score overwrites). The current open candle is updated in-place on every cycle (CCXT returns the latest incomplete bar).

### Trimming

After each `ZADD`, trim to max entries:

```
ZREMRANGEBYRANK nexus:ohlcv:1h:{asset_id} 0 -(max_bars+1)
```

This removes the oldest entries if the sorted set exceeds 168 (1H) or 30 (1D) entries.

### Cold-Start Backfill

On MDS startup (or when Redis keys are missing/expired):

1. Check `EXISTS nexus:ohlcv:1h:{asset_id}`.
2. If missing or `ZCARD < 24`: **backfill from PostgreSQL first** — read the most recent 168 1H / 30 1D bars from `ohlcv` table, `ZADD` all into Redis.
3. If PostgreSQL also empty (brand-new asset): **backfill from CCXT** — `fetch_ohlcv(symbol, "1h", limit=168)` and `fetch_ohlcv(symbol, "1d", limit=30)`, write to both Redis and PostgreSQL.

This three-layer fallback ensures MDS always recovers: Redis → PostgreSQL → CCXT.

### PostgreSQL Persistence (Tier 3)

The existing `ohlcv` table (`id PK, asset_id FK, timeframe, timestamp, open, high, low, close, volume, UNIQUE(asset_id, timeframe, timestamp)`) is used as the persistence layer.

**Write cadence:** Every Tier 2 cycle (5 minutes), the latest bars already in Redis are flushed to PostgreSQL via `INSERT ... ON CONFLICT DO UPDATE`.

**Retention pruning:** Every 6 hours, MDS executes:

```sql
DELETE FROM ohlcv
WHERE (timeframe = '1h' AND timestamp < now() - interval '7 days')
   OR (timeframe = '1d' AND timestamp < now() - interval '30 days');
```

### New Indexes (Migration)

1. `idx_ohlcv_timestamp`: `(timestamp)` — retention pruning.
2. `idx_ohlcv_asset_tf_latest`: `(asset_id, timeframe, timestamp DESC)` — "latest N bars" cold-start reads.

---

## Section 4 — Metric Computation Contract

All metrics are computed **server-side** from **OHLCV close prices in the Redis cache**. The frontend receives pre-computed values and does NO computation.

### Snapshot Schema (18 fields)

```json
{
  "price": 84231.50,
  "source": "ticker",
  "source_candle_ts": "2026-04-03T13:00:00Z",
  "change_1h":  -0.42,
  "change_4h":  1.15,
  "change_12h": -2.33,
  "change_24h": 3.72,
  "change_7d":  -5.10,
  "volume_1h":  12450000.0,
  "volume_4h":  48900000.0,
  "volume_24h": 285000000.0,
  "high_24h":   85100.00,
  "low_24h":    83200.00,
  "bid":        84230.10,
  "ask":        84232.90,
  "spread_pct": 0.0033,
  "last_trade_ts": "2026-04-03T14:30:00Z",
  "computed_at": "2026-04-03T14:30:05Z"
}
```

**Total keys: 18.** All values are scalars (float, string, or null). No nested objects.

### `source_candle_ts` — Exact Semantics (Deterministic, Hour-Boundary Rule)

`source_candle_ts` is the **open timestamp of the most recent fully closed 1H candle**, determined by the current UTC hour boundary — never by index position or exchange-dependent bar ordering.

**Definition:**

```
source_candle_ts = timestamp of the most recent 1H candle where:
    candle_ts < current_hour_start
```

**Derivation (deterministic, exchange-agnostic):**

```python
# Step 1: compute the current hour boundary in UTC
current_hour_start_ms = floor_to_hour(utc_now())  # e.g. 14:00:00 → 14:00:00

# Step 2: query Redis for the most recent bar STRICTLY BEFORE the current hour
# This guarantees the bar is fully closed regardless of exchange candle timing
anchor_bar = ZREVRANGEBYSCORE nexus:ohlcv:1h:{asset_id}
    (current_hour_start_ms - 1)     # exclusive upper bound: anything < current hour
    -inf                            # no lower bound
    LIMIT 0 1

# Step 3: extract the timestamp
source_candle_ts = datetime.utcfromtimestamp(anchor_bar.score / 1000).isoformat() + "Z"
```

**Why this is deterministic and exchange-agnostic:**
- `floor_to_hour(utc_now())` is a pure UTC computation — no exchange dependency.
- The `ZREVRANGEBYSCORE` with `(current_hour_start_ms - 1)` as the exclusive upper bound guarantees we never select the current open candle, regardless of whether the exchange includes it, delays it, or labels it differently.
- If the most recent closed bar is from `13:00:00Z` and current time is `14:25:00Z`, `source_candle_ts = "2026-04-03T13:00:00Z"`. If it's `14:00:01Z`, the answer is still `13:00:00Z` because the 14:00 candle just opened and is excluded.

**Open candles are NEVER used as the anchor.** The `< current_hour_start` strict inequality guarantees this. Even if CCXT returns an open candle with `ts_ms == current_hour_start_ms`, it is excluded from the anchor query.

**Consistency guarantee:** Every `change_Nh` and `volume_Nh` metric in the snapshot is computed relative to this same `source_candle_ts` anchor. If `source_candle_ts` is `"2026-04-03T13:00:00Z"`:
- `change_1h` compares current price to the close of the candle at `12:00:00Z`
- `change_4h` compares current price to the close of the candle at `09:00:00Z`
- `volume_1h` is the volume of the single candle at `13:00:00Z` (the closed bar at the anchor)
- All metrics share the same temporal reference frame

If the anchor bar is stale (e.g., a gap — latest closed bar is from `11:00:00Z` instead of `13:00:00Z`), `source_candle_ts` reveals this immediately. The frontend can compare it to its local clock.

**Fallback:** If no bar exists with `ts < current_hour_start_ms` (brand-new asset, Redis empty), `source_candle_ts` is `null` and all OHLCV-derived metrics are `null`.

### Price Source vs OHLCV Source — Explicit Distinction

| Field | Source | Origin |
|-------|--------|--------|
| `price` | CCXT `ticker['last']` | Tier 0 fetch |
| `bid`, `ask` | CCXT `ticker['bid']`, `ticker['ask']` | Tier 0 fetch |
| `last_trade_ts` | CCXT `ticker['datetime']` | Tier 0 fetch |
| `source` | `"ticker"` if from live ticker, `"ohlcv"` if fell back to candle close | Tier 0 |
| `change_*`, `volume_*`, `high_24h`, `low_24h` | Redis OHLCV cache | Tier 1 / Tier 2 |
| `spread_pct` | Computed from `bid`/`ask` | Tier 0 |
| `source_candle_ts` | Hour-boundary query on Redis 1H sorted set | Tier 1 / Tier 2 |
| `computed_at` | `datetime.utcnow()` at snapshot assembly | All tiers |

### Exact Formulas

#### Price Changes (percentage, 2 decimal places)

```
change_Nh = round(((current_price - close_N_hours_ago) / close_N_hours_ago) * 100, 2)
```

Where `close_N_hours_ago` is obtained from the Redis 1H sorted set using `source_candle_ts_ms` as the anchor:

```
ZREVRANGEBYSCORE nexus:ohlcv:1h:{asset_id}
    (source_candle_ts_ms - N*3600*1000)
    -inf
    LIMIT 0 1
```

This returns the bar with the highest timestamp that is ≤ `source_candle_ts - N hours`. Parse its close value (index 4).

| Metric | N (hours) | Source TF | Fallback |
|--------|-----------|-----------|----------|
| `change_1h` | 1 | 1H Redis cache | `null` if <2 closed bars |
| `change_4h` | 4 | 1H Redis cache | `null` if <5 closed bars |
| `change_12h` | 12 | 1H Redis cache | `null` if <13 closed bars |
| `change_24h` | 24 | 1H Redis cache | `null` if <25 closed bars |
| `change_7d` | 168 (7d) | 1D Redis cache | `null` if <8 daily bars |

For `change_7d`, the lookup is against the 1D sorted set:

```
ZREVRANGEBYSCORE nexus:ohlcv:1d:{asset_id}
    (source_candle_ts_ms - 7*86400*1000)
    -inf
    LIMIT 0 1
```

**Division by zero guard:** If denominator is 0, None, or bar not found → metric is `null`.

#### Volume Aggregations (absolute USD, 0 decimal places)

Volume is summed over the N most recent **closed** 1H candles from the Redis sorted set. The range is bounded by the hour-boundary anchor, which inherently excludes the current open candle.

```
ZRANGEBYSCORE nexus:ohlcv:1h:{asset_id}
    (source_candle_ts_ms - (N-1)*3600*1000)
    source_candle_ts_ms
```

Sum the `volume` field (index 5) of all returned bars.

| Metric | N (hours) |
|--------|-----------|
| `volume_1h` | 1 (single bar at `source_candle_ts`) |
| `volume_4h` | 4 |
| `volume_24h` | 24 |

**Note on change vs volume semantics:** `change_1h` measures how much price moved relative to the candle one hour BEFORE the anchor (e.g., anchor at 13:00 → `change_1h` denominator is the 12:00 bar's close). `volume_1h` measures how much volume traded in the anchor candle itself (the 13:00 bar). This difference is intentional: `change_Nh` answers "how much has price changed over the last N hours?" while `volume_Nh` answers "how much volume occurred in the last N completed hours?" Both are anchored to `source_candle_ts` but measure different temporal windows by design.

#### 24h High/Low

From the 1H Redis cache, query bars in the 24h window ending at the anchor:

```
ZRANGEBYSCORE nexus:ohlcv:1h:{asset_id}
    (source_candle_ts_ms - 24*3600*1000)
    source_candle_ts_ms
```

Also include the current open candle (it contributes to intra-hour high/low):

```
ZRANGEBYSCORE nexus:ohlcv:1h:{asset_id}
    (current_hour_start_ms)
    +inf
```

`high_24h` = max of all `high` values (index 2) across both queries. `low_24h` = min of all `low` values (index 3).

#### Spread

```
spread_pct = round(((ask - bid) / ((ask + bid) / 2)) * 100, 4)
```

If `bid` or `ask` is null/zero → `spread_pct` is `null`.

### Forbidden Fields — `ticker.percentage` and `ticker.baseVolume`

**`ticker['percentage']` is NEVER used.** All percentage changes are derived from OHLCV close prices via the formulas above. CCXT's `ticker.percentage` is exchange-reported, inconsistent across exchanges, and may use different time windows.

**`ticker['baseVolume']` is NEVER used.** All volume metrics are derived from OHLCV candle volumes. `ticker.baseVolume` represents a different aggregation window and unit (base currency vs quote currency) than what the snapshot requires.

The only fields read from the CCXT ticker response are: `last`, `bid`, `ask`, `datetime`. All other ticker fields are discarded.

**Enforcement:** See Section 9 tests U17–U19, G1–G3, and acceptance criteria F11–F13.

### Null Handling

Any metric that cannot be computed (insufficient OHLCV data, missing ticker fields, division by zero) is `null` in the JSONB. The frontend renders `null` as "—" or equivalent.

---

## Section 5 — Update Frequency and Rate-Limit Budget

### Tier Model

| Tier | Cadence | What it fetches | What it computes | What it writes (via assembler) |
|------|---------|----------------|-----------------|-------------------------------|
| **Tier 0** (price) | **15s** | `fetch_tickers(symbols)` — 1 batch call | `price`, `bid`, `ask`, `spread_pct`, `last_trade_ts`, `source`, `computed_at` | Redis HASH (price fields only) → pub/sub |
| **Tier 1** (short-horizon) | **60s** | `fetch_ohlcv(symbol, "1h", limit=4)` per tradable symbol | `change_1h`, `change_4h`, `volume_1h`, `volume_4h`, `high_24h`, `low_24h`, `source_candle_ts`, `computed_at` | Redis OHLCV 1H ZADD + trim → Redis HASH (Tier 1 fields) → pub/sub |
| **Tier 2** (deep-horizon) | **5min** | `fetch_ohlcv(symbol, "1d", limit=2)` per tradable symbol | `change_12h`, `change_24h`, `change_7d`, `volume_24h`, `computed_at` | Redis OHLCV 1D ZADD + trim → Redis HASH (Tier 2 fields) → pub/sub → PostgreSQL persist |
| **Tier 3** (persistence) | **5min** (subroutine of Tier 2) | Nothing — reads from `_snapshots` in-memory | Nothing new | PostgreSQL `ohlcv` table flush, PostgreSQL `Asset.market_snapshot` + `snapshot_updated_at` |

### Why These Cadences

- **Tier 0 at 15s:** Price is the most time-sensitive metric. 15s provides near-real-time feel without overwhelming the exchange. `fetch_tickers` is a single batch call regardless of symbol count.
- **Tier 1 at 60s:** 1H candles close once per hour. Fetching every 60s catches the latest closed bar within 60s of close. More frequent is wasteful — the candle data doesn't change until the next close.
- **Tier 2 at 5min:** Daily candles close once per 24h. 12h/24h/7d metrics change slowly. 5-minute cadence is sufficient. Tier 3 DB persistence piggybacks here to avoid separate scheduling.

### Tradable Asset Cache

Tier 0 does NOT query PostgreSQL for tradable assets every 15s. Instead, MDS maintains an in-memory cache:

```
_tradable_assets: list[Asset]       # cached result
_tradable_refresh_ts: float         # epoch of last DB query
TRADABLE_CACHE_TTL_S = 60           # refresh every 60s (Tier 1 cadence)
```

```python
async def _refresh_tradable_assets(self) -> list[Asset]:
    now = time.monotonic()
    if now - self._tradable_refresh_ts < TRADABLE_CACHE_TTL_S:
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
```

All tier loops call `_refresh_tradable_assets()` at the start of each cycle. Tier 0 typically gets the cached result (TTL=60s > Tier 0 interval=15s). Tier 1 triggers the actual DB refresh every 60s. New asset detection (Section 8) happens at the refresh boundary.

### Service Loop Design

Three separate `asyncio.Task`s, one per tier loop. All snapshot writes go through `_assemble_and_flush` (single writer):

```
_tier0_loop:
    while running:
        t0 = time.monotonic()
        assets = await _refresh_tradable_assets()   # cached, no DB hit
        symbols = [a.symbol for a in assets]
        tickers = await _fetch_tickers(symbols)
        for asset in assets:
            ticker = tickers.get(asset.symbol)
            if not ticker:
                continue
            fields = {
                "price": ticker["last"],
                "bid": ticker["bid"],
                "ask": ticker["ask"],
                "spread_pct": _compute_spread(ticker),
                "last_trade_ts": ticker["datetime"],
                "source": "ticker",
            }
            await _assemble_and_flush(asset.id, tier=0, fields=fields)
        elapsed = time.monotonic() - t0
        await asyncio.sleep(max(0, TIER0_INTERVAL - elapsed))

_tier1_loop:
    while running:
        t0 = time.monotonic()
        assets = await _refresh_tradable_assets()   # may trigger DB refresh
        for asset in assets (semaphore-bounded):
            bars = await _fetch_ohlcv(asset.symbol, "1h", limit=4)
            await _write_redis_ohlcv(asset.id, "1h", bars)
            _trim_redis_ohlcv(asset.id, "1h", OHLCV_1H_MAX_BARS)
            fields = await _compute_tier1_metrics(asset.id)
            await _assemble_and_flush(asset.id, tier=1, fields=fields)
        elapsed = time.monotonic() - t0
        await asyncio.sleep(max(0, TIER1_INTERVAL - elapsed))

_tier2_loop:
    while running:
        t0 = time.monotonic()
        assets = await _refresh_tradable_assets()
        for asset in assets (semaphore-bounded):
            bars = await _fetch_ohlcv(asset.symbol, "1d", limit=2)
            await _write_redis_ohlcv(asset.id, "1d", bars)
            _trim_redis_ohlcv(asset.id, "1d", OHLCV_1D_MAX_BARS)
            fields = await _compute_tier2_metrics(asset.id)
            await _assemble_and_flush(asset.id, tier=2, fields=fields)
        _tier3_persist()
        elapsed = time.monotonic() - t0
        await asyncio.sleep(max(0, TIER2_INTERVAL - elapsed))
```

### `_assemble_and_flush` — Single-Writer Sequence

For **every** snapshot update, regardless of tier:

```python
async def _assemble_and_flush(self, asset_id: int, tier: int, fields: dict) -> None:
    # 1. COMPUTE: merge fields into in-memory canonical snapshot (LOCK HELD)
    async with self._snapshot_lock:
        snap = self._snapshots.setdefault(asset_id, {})
        snap.update(fields)
        snap["computed_at"] = datetime.utcnow().isoformat() + "Z"
        snapshot_copy = dict(snap)  # shallow copy for I/O outside lock
    # Lock released here — all Redis/DB I/O is outside the lock.

    key = f"nexus:snapshot:{asset_id}"

    # 2. WRITE REDIS (non-fatal)
    try:
        await self._redis.hset(key, mapping={k: json.dumps(v) for k, v in snapshot_copy.items()})
        await self._redis.expire(key, SNAPSHOT_TTL_S)
    except Exception:
        logger.error("Redis HSET failed for asset %s", asset_id, exc_info=True)

    # 3. PUBLISH (non-fatal)
    try:
        payload = json.dumps({
            "type": "snapshot_update",
            "tier": tier,
            "exchange_id": self._exchange_db_id,
            "assets": [{"asset_id": asset_id, "symbol": self._symbols[asset_id], "snapshot": snapshot_copy}],
            "timestamp": snapshot_copy["computed_at"],
        })
        await self._redis.publish("nexus:events:ticker", payload)
    except Exception:
        logger.error("Redis PUBLISH failed for asset %s", asset_id, exc_info=True)

    # 4. PERSIST DB (Tier 2 only) — always executes even if Redis failed
    #    (actual batch DB write happens in _tier3_persist after all assets)
```

**Write order is always: compute (under lock) → Redis HSET (outside lock, non-fatal) → Redis PUBLISH (outside lock, non-fatal) → DB (Tier 2 only, always attempted).** This is explicit and invariant across all tiers.

### Overrun Handling Per Tier

If a tier's cycle takes longer than its interval:

- **Tier 0:** Log warning `"Tier 0 overrun: {elapsed:.1f}s > {TIER0_INTERVAL}s"`. Skip the sleep — immediately start the next cycle. Do NOT double up.
- **Tier 1:** Log warning. If overrun >2× interval for 3 consecutive cycles, increase `TIER1_INTERVAL` by 50% (adaptive). Emit `SYSTEM_ALERT` via Redis.
- **Tier 2:** Same as Tier 1 but with `TIER2_INTERVAL`.
- **Cross-tier isolation:** Tier 0 never waits for Tier 1/2. Tier 1 never waits for Tier 2. Each task is independent. The only serialization point is the `_snapshot_lock`, held only for in-memory merge (microseconds, no I/O).

### Per-Cycle API Call Budget

| Call | Per symbol | Count | Notes |
|------|-----------|-------|-------|
| `fetch_tickers(symbols)` | 1 batch | 1 | Single call, all symbols. |
| `fetch_ohlcv(symbol, "1h", limit=4)` | 1 per symbol | S | Tier 1. |
| `fetch_ohlcv(symbol, "1d", limit=2)` | 1 per symbol | S | Tier 2. |

### Scaling Table (per minute)

| Symbols (S) | Tier 0 calls/min | Tier 1 calls/min | Tier 2 calls/min | Total calls/min | Bybit budget% (1440/min) |
|-------------|-----------------|-----------------|-----------------|----------------|--------------------------|
| 5 | 4 | 5 | 1 | 10 | 0.7% |
| 10 | 4 | 10 | 2 | 16 | 1.1% |
| 25 | 4 | 25 | 5 | 34 | 2.4% |
| 50 | 4 | 50 | 10 | 64 | 4.4% |
| 100 | 4 | 100 | 20 | 124 | 8.6% |

### Concurrency

- `fetch_tickers`: single batch call, no concurrency needed.
- `fetch_ohlcv` (Tier 1 and Tier 2): parallelized with `asyncio.Semaphore(3)`. CCXT's internal rate limiter serializes HTTP calls, but the semaphore bounds task creation.

---

## Section 6 — Snapshot Authority and Delivery Model

### Authority Hierarchy

```
CCXT (exchange) → MDS (compute) → _snapshots (in-memory canonical)
                                       ↓
                                  Redis HASH (hot authority, per-asset HSET projection)
                                       ↓
                                  PostgreSQL (persisted fallback, Tier 2 cadence)
                                       ↓
                                  WebSocket (primary delivery via Redis pub/sub)
                                       ↓
                                  REST (fallback delivery, reads Redis HASH first then DB)
```

**In-memory `_snapshots` dict is the single source of truth** during MDS runtime. Redis HASH is a durable projection of it. PostgreSQL is the crash-recovery fallback.

**Redis is the hot authority for external consumers.** REST endpoints and WS subscribers read from Redis. If Redis is empty (MDS restart, key expiry), REST falls back to PostgreSQL.

### Redis Snapshot Keys

| Key pattern | Type | Content | TTL |
|------------|------|---------|-----|
| `nexus:snapshot:{asset_id}` | HASH | 18 snapshot fields, each a JSON-encoded value | **600s** (renewed on every write by any tier) |

**Why TTL = 600s (not 300s):** The longest write interval is Tier 2 at 300s. A 300s TTL risks expiry during a slow Tier 2 cycle (e.g., 290s cycle + 15s network hiccup = 305s). A 600s TTL provides 2× headroom. Note: Tier 0 renews the TTL every 15s during normal operation, so expiry only occurs if MDS is down for >10 minutes. At that point, TTL expiry correctly signals "no fresh data" to REST consumers, who then fall back to DB.

**No bulk key.** There is no `nexus:snapshot:all:*` key. The REST `/snapshots` endpoint reads per-asset HASH keys via a Redis pipeline (see Section 7). This eliminates the consistency gap where a bulk key could become stale between Tier 0 and Tier 1 writes.

**Redis HASH structure for `nexus:snapshot:{asset_id}`:**

| Hash field | Value (JSON-encoded) |
|-----------|---------------------|
| `price` | `84231.50` |
| `source` | `"ticker"` |
| `source_candle_ts` | `"2026-04-03T13:00:00Z"` |
| `change_1h` | `-0.42` |
| `change_4h` | `1.15` |
| `change_12h` | `-2.33` |
| `change_24h` | `3.72` |
| `change_7d` | `-5.10` |
| `volume_1h` | `12450000.0` |
| `volume_4h` | `48900000.0` |
| `volume_24h` | `285000000.0` |
| `high_24h` | `85100.00` |
| `low_24h` | `83200.00` |
| `bid` | `84230.10` |
| `ask` | `84232.90` |
| `spread_pct` | `0.0033` |
| `last_trade_ts` | `"2026-04-03T14:30:00Z"` |
| `computed_at` | `"2026-04-03T14:30:05Z"` |

Each `_assemble_and_flush` call writes all 18 fields via `hset(key, mapping=...)` to keep the HASH and in-memory dict identical.

### Write Order (Explicit, Invariant)

For every update, the write order is:

```
1. COMPUTE  — merge tier-specific fields into _snapshots[asset_id] in memory (under lock)
2. REDIS    — HSET nexus:snapshot:{asset_id} (all 18 fields) + EXPIRE 600 (outside lock, non-fatal)
3. PUBLISH  — PUBLISH nexus:events:ticker (full snapshot payload) (outside lock, non-fatal)
4. DB       — (Tier 2 only) UPDATE assets SET market_snapshot = ..., snapshot_updated_at = now()
              + INSERT INTO ohlcv ... ON CONFLICT DO UPDATE (always attempted even if Redis failed)
```

This order ensures:
- In-memory dict is updated first (the canonical source for the next tier's merge).
- Redis is updated before pub/sub (WS subscribers never see a notification without the Redis key being current).
- DB is written last (slowest operation, failure does not affect hot path).
- Redis failures do not block DB persistence.

### Read Paths

| Consumer | Primary path | Fallback path | Trust rule |
|----------|-------------|---------------|------------|
| WS subscriber (`ticker` channel) | Redis pub/sub (pushed by MDS) | — (fire-and-forget) | Always receives the freshest snapshot |
| `GET /market-data/snapshots` | Redis pipeline: `HGETALL nexus:snapshot:{aid}` for each tradable asset (single round-trip) | PostgreSQL `SELECT market_snapshot FROM assets WHERE is_tradable` | Redis first; if empty/expired, fall back to DB |
| `GET /market-data/snapshots/{id}` | Redis `HGETALL nexus:snapshot:{asset_id}` | PostgreSQL `SELECT market_snapshot FROM assets WHERE id = ?` | Redis first; if empty/expired, fall back to DB |
| `GET /market-data/ohlcv/{id}` | Redis `ZRANGEBYSCORE nexus:ohlcv:{tf}:{asset_id}` | PostgreSQL `SELECT * FROM ohlcv WHERE asset_id = ? AND timeframe = ?` | Redis first; if empty, fall back to DB |

### Frontend Trust Hierarchy

The frontend does not choose between sources — it receives a single response from the REST endpoint or WS channel. The backend resolves the trust hierarchy:

1. **WS delivery:** Most trusted. Pushed every 15s (Tier 0). Frontend treats WS snapshots as authoritative.
2. **REST response:** On initial page load or reconnection, frontend calls `GET /market-data/snapshots`. Handler pipelines HGETALL for each tradable asset; if Redis is empty or expired, reads PostgreSQL. Response includes `computed_at` — frontend displays "stale" indicator if `computed_at` is older than 5 minutes.

### Temporary Redis/DB Divergence

Between Tier 0/1 writes (Redis only) and Tier 2 writes (Redis + DB), Redis is ahead of PostgreSQL by up to 5 minutes. This is expected:

- WS subscribers always get the freshest data (from Redis pub/sub).
- REST reads Redis first. Only if Redis is empty (MDS down, key expired) does it fall back to DB.
- `computed_at` in both Redis and DB lets any consumer detect staleness.
- No conflict resolution needed — Redis is always ≥ DB freshness.

### Phase 1 Contract Preservation

- `Asset.market_snapshot` and `Asset.snapshot_updated_at` are written ONLY by MDS Tier 3. sync-assets `ON CONFLICT DO UPDATE` already excludes these columns.
- `Asset.is_tradable` and `Asset.allocation_weight` are NEVER written by MDS.
- Phase 1 PATCH endpoints do not touch `market_snapshot` or `snapshot_updated_at`.

---

## Section 7 — API and Schema Changes

### New REST Endpoints

All endpoints require `Depends(get_current_user)`.

#### `GET /api/v1/market-data/snapshots`

Returns latest snapshots for all tradable assets. Reads per-asset Redis HASH keys via pipeline, falls back to PostgreSQL.

**Query params:** `exchange_id` (optional, defaults to active exchange)

**Read logic:**

```python
# 1. Get tradable asset IDs from DB (or cached)
asset_ids = [a.id for a in tradable_assets]

# 2. Pipeline HGETALL for each asset — single Redis round-trip
pipe = redis.pipeline()
for aid in asset_ids:
    pipe.hgetall(f"nexus:snapshot:{aid}")
results = await pipe.execute()

# 3. For any asset where HGETALL returned empty (expired/missing),
#    fall back to DB: SELECT market_snapshot FROM assets WHERE id = ?
```

**Response (200):**

```json
{
  "snapshots": [
    {
      "asset_id": 1,
      "symbol": "BTC/USDT:USDT",
      "base_currency": "BTC",
      "is_tradable": true,
      "allocation_weight": 1.0,
      "snapshot": {
        "price": 84231.50,
        "source": "ticker",
        "source_candle_ts": "2026-04-03T13:00:00Z",
        "change_1h": -0.42,
        "change_4h": 1.15,
        "change_12h": -2.33,
        "change_24h": 3.72,
        "change_7d": -5.10,
        "volume_1h": 12450000,
        "volume_4h": 48900000,
        "volume_24h": 285000000,
        "high_24h": 85100.00,
        "low_24h": 83200.00,
        "bid": 84230.10,
        "ask": 84232.90,
        "spread_pct": 0.0033,
        "last_trade_ts": "2026-04-03T14:30:00Z",
        "computed_at": "2026-04-03T14:30:05Z"
      },
      "snapshot_updated_at": "2026-04-03T14:30:05Z",
      "data_source": "redis"
    }
  ],
  "count": 5,
  "exchange_id": 1
}
```

`"data_source": "redis"` or `"data_source": "db"` indicates which layer served each individual snapshot. Observability only — frontend ignores it.

#### `GET /api/v1/market-data/snapshots/{asset_id}`

Returns single asset snapshot. Redis HASH first, DB fallback. 404 if asset not found.

**Response (200):** Single object from `snapshots` array above (unwrapped).

#### `GET /api/v1/market-data/ohlcv/{asset_id}`

Returns OHLCV bars. Redis sorted set first, DB fallback.

**Query params:**
- `timeframe`: `"1h"` or `"1d"` (default `"1h"`, 422 on other values)
- `limit`: 10–500 (default 168 for 1h, 30 for 1d)

**Response (200):**

```json
{
  "asset_id": 1,
  "symbol": "BTC/USDT:USDT",
  "timeframe": "1h",
  "bars": [
    {"timestamp": "2026-04-03T13:00:00Z", "open": 84100.0, "high": 84350.0, "low": 84050.0, "close": 84231.5, "volume": 12450000.0}
  ],
  "count": 168,
  "data_source": "redis"
}
```

### WebSocket Payload Shape

**Channel:** `ticker` (already in `CHANNELS` set — no change to `ws/manager.py`)

**Redis publication key:** `nexus:events:ticker`

**Payload:**

```json
{
  "type": "snapshot_update",
  "tier": 0,
  "exchange_id": 1,
  "assets": [
    {
      "asset_id": 1,
      "symbol": "BTC/USDT:USDT",
      "snapshot": { "...full 18-field snapshot..." }
    }
  ],
  "timestamp": "2026-04-03T14:30:05Z"
}
```

`"tier"` indicates which tier produced this update (0, 1, or 2). The `snapshot` object always contains all 18 fields (some may be `null` during staged warm-up). This lets the frontend always overwrite its local state with the latest server-side snapshot — no field-level merge logic on the client.

### Existing WS Manager — Reused Unchanged

The `ConnectionManager` in `ws/manager.py` is reused without modification:
- `"ticker"` is already in `CHANNELS` (line 34).
- The Redis bridge (`psubscribe("nexus:events:*")`) routes `nexus:events:ticker` to WS subscribers.
- Rate limiting (20 msgs/sec per client) applies. At Tier 0 every 15s, well under limit.

### Schema Changes

**No new tables. No new columns.** The only migration adds two indexes:

```python
def upgrade():
    op.create_index("idx_ohlcv_timestamp", "ohlcv", ["timestamp"])
    op.create_index("idx_ohlcv_asset_tf_latest", "ohlcv",
                    ["asset_id", "timeframe", sa.text("timestamp DESC")])

def downgrade():
    op.drop_index("idx_ohlcv_asset_tf_latest")
    op.drop_index("idx_ohlcv_timestamp")
```

---

## Section 8 — Newly-Enabled Asset Flow (Staged Warm-Up)

When a user marks an asset as tradable via Phase 1's `PATCH /{id}/assets/{asset_id}` with `is_tradable: true`, MDS detects the new asset on its next tradable asset cache refresh and initiates staged warm-up (no Phase 1 modifications required).

### Detection Mechanism

MDS maintains a cached tradable asset set (`_tradable_assets`) refreshed every 60s via `_refresh_tradable_assets()` (see Section 5). When the cache refreshes and a new `asset_id` appears that is not in `_known_asset_ids`, it triggers staged warm-up. Detection latency is at most 60s (the cache TTL).

### Staged Warm-Up Timeline

| Stage | Trigger | Latency from toggle | What happens | Metrics available after |
|-------|---------|--------------------|--------------|-----------------------|
| **Stage 0** (detection + price) | Next Tier 0 cycle after cache refresh | 0–75s (up to 60s cache TTL + 15s Tier 0) | New asset_id detected. Added to `_known_asset_ids` and `_snapshots`. `fetch_tickers` already includes it. `_assemble_and_flush(asset_id, tier=0, price_fields)`. | `price`, `bid`, `ask`, `spread_pct`, `last_trade_ts`, `source`, `computed_at`. All change/volume/high/low fields = `null`. `source_candle_ts` = `null`. |
| **Stage 1** (1H backfill) | Next Tier 1 cycle | 75s–135s | `fetch_ohlcv(symbol, "1h", limit=168)` — full 7-day backfill. ZADD into Redis 1H sorted set. Compute Tier 1 metrics. `_assemble_and_flush(asset_id, tier=1, tier1_fields)`. | `change_1h`, `change_4h`, `volume_1h`, `volume_4h`, `high_24h`, `low_24h`, `source_candle_ts`. `change_12h`/`change_24h`/`change_7d`/`volume_24h` still `null`. |
| **Stage 2** (1D backfill + DB persist) | Next Tier 2 cycle | 135s–435s | `fetch_ohlcv(symbol, "1d", limit=30)` — full 30-day backfill. ZADD into Redis 1D sorted set. Compute Tier 2 metrics. `_assemble_and_flush(asset_id, tier=2, tier2_fields)`. Tier 3 persists to DB. | ALL 18 fields populated. Full snapshot. |

### UI-Visible Timeline

- **Within ~75 seconds:** User sees the asset appear with a live price, bid/ask, and spread. Change and volume columns show "—".
- **Within ~135 seconds:** 1H metrics populate. User sees change_1h, change_4h, hourly volumes, 24h high/low.
- **Within ~7 minutes:** All metrics populated including change_7d and volume_24h. Full snapshot complete.

### Backfill vs Incremental

Only Stage 1 and Stage 2 perform backfill fetches (168 bars + 30 bars). After initial warm-up, all subsequent cycles use incremental fetches (4 bars + 2 bars). The backfill is a one-time cost per newly-enabled asset.

### Backfill API Cost

Per newly-enabled asset: 2 CCXT calls (168 1H bars + 30 1D bars). At 350ms spacing: ~0.7s. This runs within the normal Tier 1/2 cycle.

### Disabled Asset Cleanup

When `is_tradable` flips to `false`:
1. Asset removed from `_known_asset_ids` and `_snapshots` on next cache refresh.
2. Redis OHLCV keys expire naturally (48h/7d TTL).
3. Redis snapshot HASH expires (600s TTL).
4. PostgreSQL OHLCV rows pruned by normal retention (7d/30d).
5. `Asset.market_snapshot` is NOT cleared — retains last-known values. `snapshot_updated_at` shows staleness.

---

## Section 9 — Test Plan

### Test File

`web/backend/tests/test_phase2_market_data.py`

### Fixtures and Infrastructure

- `pgserver` for PostgreSQL (same `_PGClientContext` pattern as Phase 1).
- `fakeredis.aioredis.FakeRedis` for Redis (supports sorted sets, HASH, pub/sub, TTL, EXPIRE, pipeline).
- CCXT mocked via `unittest.mock.AsyncMock` patching `ccxt.bybit`.
- `httpx.AsyncClient` + `ASGITransport` for REST endpoint tests.

### Unit Tests — Metric Computation (no DB, no Redis, pure functions)

| # | Test | Validates |
|---|------|-----------|
| U1 | `test_change_1h_positive` | 1h-ago close=100, current=105 → `change_1h=5.0` |
| U2 | `test_change_1h_negative` | 1h-ago close=100, current=95 → `change_1h=-5.0` |
| U3 | `test_change_4h_computation` | 4h-ago close=100, current=103 → `change_4h=3.0` |
| U4 | `test_change_24h_computation` | 24h-ago close from bar list → correct % |
| U5 | `test_change_7d_from_daily_candles` | 7d-ago daily close → correct % |
| U6 | `test_volume_1h_single_candle` | Single closed candle volume returned |
| U7 | `test_volume_4h_sum` | Sum of 4 closed candle volumes |
| U8 | `test_volume_24h_excludes_open_candle` | Current incomplete candle excluded |
| U9 | `test_spread_pct_normal` | bid=100, ask=100.05 → `0.05%` |
| U10 | `test_spread_pct_null_on_zero_bid` | bid=0 → null |
| U11 | `test_change_null_insufficient_bars` | Fewer bars than window → null |
| U12 | `test_change_null_zero_denominator` | close=0 → null |
| U13 | `test_high_low_24h` | Correct max(high)/min(low) over 24 bars |
| U14 | `test_snapshot_schema_18_keys` | All 18 keys present including `source_candle_ts` |

### Unit Tests — `source_candle_ts` Hour-Boundary Determinism

| # | Test | Validates |
|---|------|-----------|
| U15 | `test_source_candle_ts_uses_hour_boundary` | At 14:25 UTC with bars at 12:00, 13:00, 14:00 → `source_candle_ts = "13:00:00Z"` (14:00 is open candle, excluded by `< floor_to_hour(now)` rule). |
| U16 | `test_source_candle_ts_at_exact_hour` | At exactly 14:00:00 UTC with bars at 12:00, 13:00, 14:00 → `source_candle_ts = "13:00:00Z"` (14:00 bar just opened, its candle_ts == current_hour_start, excluded by strict `<`). |
| U16b | `test_source_candle_ts_with_gap` | Bars at 10:00, 11:00, 14:00 (12:00 and 13:00 missing). At 14:25 → `source_candle_ts = "11:00:00Z"` (latest bar with ts < 14:00:00). Gap is correctly reflected. |
| U16c | `test_source_candle_ts_all_metrics_use_same_anchor` | Compute change_1h, change_4h, volume_1h with known bar data. Verify each used the same `source_candle_ts` as the reference anchor (not independently computed). |
| U16d | `test_source_candle_ts_null_when_no_closed_bars` | Empty OHLCV cache → `source_candle_ts = null`, all change/volume metrics = null. |

### Unit Tests — Forbidden Field Exclusion Proofs

| # | Test | Validates |
|---|------|-----------|
| U17 | `test_ticker_percentage_never_read` | Mock ticker with `percentage=99.9`. Computed `change_24h` uses OHLCV → result differs from `99.9`. |
| U18 | `test_ticker_baseVolume_never_read` | Mock ticker with `baseVolume=999999`. Computed `volume_24h` uses OHLCV sums → result differs from `999999`. |
| U19 | `test_only_allowed_ticker_fields_accessed` | Ticker is `MagicMock(spec=dict)` with `__getitem__` tracking. Only `last`, `bid`, `ask`, `datetime` accessed. Any other key access fails. |

### Integration Tests — Redis Hot Cache (fakeredis)

| # | Test | Validates |
|---|------|-----------|
| R1 | `test_ohlcv_zadd_and_retrieve` | ZADD 168 bars → ZRANGEBYSCORE returns correct bars |
| R2 | `test_ohlcv_trim_to_max` | ZADD 200 bars → trim → ZCARD == 168, oldest removed |
| R3 | `test_snapshot_hset_and_hgetall` | HSET 18 fields → HGETALL returns identical dict |
| R4 | `test_snapshot_ttl_expires` | HSET with EXPIRE 600 → time_travel(601) → HGETALL returns empty |
| R5 | `test_snapshot_ttl_renewed_on_write` | HSET → wait 300s → HSET again → TTL reset to 600 → still alive at 800s |
| R6 | `test_ohlcv_cold_start_empty_redis` | Redis empty → MDS falls back to DB → backfills Redis |
| R7 | `test_pipeline_hgetall_multiple_assets` | Pipeline HGETALL for 5 assets → returns all 5 snapshots in single round-trip |

### Integration Tests — Snapshot Atomicity and Race Conditions

| # | Test | Validates |
|---|------|-----------|
| A1 | `test_assembler_serializes_concurrent_updates` | Simulate Tier 0 and Tier 1 calling `_assemble_and_flush` near-simultaneously. Verify final snapshot contains BOTH tier's fields — neither is lost. |
| A2 | `test_tier0_does_not_overwrite_tier2_fields` | Tier 2 writes `change_24h=3.5`. Tier 0 writes `price=85000`. After both, `change_24h` still `3.5`. |
| A3 | `test_tier2_does_not_regress_tier0_price` | Tier 0 writes `price=85000`. Tier 2 writes `change_7d=-2.1` (does NOT include `price` in its fields dict). After both, `price` still `85000`. |
| A4 | `test_published_snapshot_matches_redis_hash` | After `_assemble_and_flush`, capture the pub/sub message and HGETALL the HASH. Both snapshots are identical. |
| A5 | `test_snapshot_integrity_under_rapid_tier0` | Fire 10 Tier 0 updates in tight loop. After all complete, HGETALL returns a consistent 18-field snapshot with monotonically increasing `computed_at`. |
| A6 | `test_redis_failure_does_not_block_db_persist` | Mock Redis HSET to raise. Verify DB write still executes in Tier 2 path. |

### Integration Tests — DB Fallback (pgserver + fakeredis)

| # | Test | Validates |
|---|------|-----------|
| D1 | `test_ohlcv_upsert_insert` | Fresh bars inserted into PostgreSQL |
| D2 | `test_ohlcv_upsert_update_on_conflict` | Duplicate (asset_id, tf, ts) updates values |
| D3 | `test_snapshot_persisted_to_asset` | `Asset.market_snapshot` JSONB populated, `snapshot_updated_at` set |
| D4 | `test_snapshot_preserves_phase1_fields` | After MDS write, `is_tradable` and `allocation_weight` unchanged |
| D5 | `test_retention_prune_1h` | 1H candles older than 7 days deleted, recent preserved |
| D6 | `test_retention_prune_1d` | 1D candles older than 30 days deleted, recent preserved |
| D7 | `test_rest_snapshots_reads_redis_first` | Seed Redis HASH + DB with different `computed_at`. GET returns Redis version with `data_source: "redis"`. |
| D8 | `test_rest_snapshots_falls_back_to_db` | Redis empty. GET returns DB version with `data_source: "db"`. |
| D9 | `test_rest_ohlcv_reads_redis_first` | Redis has bars, DB has bars. GET returns Redis bars. |
| D10 | `test_rest_ohlcv_falls_back_to_db` | Redis empty. GET returns DB bars. |
| D11 | `test_rest_single_snapshot_404` | Non-existent asset_id → 404 |
| D12 | `test_rest_ohlcv_invalid_timeframe` | `timeframe=15m` → 422 |
| D13 | `test_rest_snapshots_mixed_redis_db` | 3 assets: 2 in Redis, 1 expired. Response has 2 with `data_source: "redis"`, 1 with `data_source: "db"`. |

### Integration Tests — WS Delivery Consistency

| # | Test | Validates |
|---|------|-----------|
| W1 | `test_redis_publish_on_tier0` | Tier 0 cycle → `nexus:events:ticker` receives message with price fields |
| W2 | `test_redis_publish_on_tier1` | Tier 1 cycle → message includes change_1h/4h and source_candle_ts |
| W3 | `test_ws_payload_shape` | Published payload has `type`, `tier`, `exchange_id`, `assets[]`, `timestamp` |
| W4 | `test_ws_and_rest_consistency` | After Tier 1, WS payload snapshot matches GET /snapshots response snapshot for same asset |

### Integration Tests — Staged Warm-Up

| # | Test | Validates |
|---|------|-----------|
| S1 | `test_stage0_price_only` | New asset after Tier 0: price/bid/ask populated, change_* all null, source_candle_ts null |
| S2 | `test_stage1_1h_metrics` | After Tier 1: change_1h/4h, volume_1h/4h, source_candle_ts populated. change_12h/24h/7d still null. |
| S3 | `test_stage2_full_metrics` | After Tier 2: all 18 fields populated, no nulls |
| S4 | `test_backfill_fetches_168_1h_bars` | CCXT mock called with `limit=168` for 1H on new asset (not 4) |
| S5 | `test_backfill_fetches_30_1d_bars` | CCXT mock called with `limit=30` for 1D on new asset (not 2) |
| S6 | `test_disabled_asset_excluded` | `is_tradable=false` → not in Tier 0 fetch_tickers, removed from _snapshots |
| S7 | `test_tradable_cache_refresh_detects_new_asset` | Set `is_tradable=true` in DB. Within 60s cache TTL, new asset appears in `_tradable_assets`. |

### Integration Tests — Error Handling

| # | Test | Validates |
|---|------|-----------|
| E1 | `test_ccxt_network_error_no_crash` | CCXT raises `NetworkError` → MDS logs warning, cycle completes, no snapshot written |
| E2 | `test_no_active_exchange_idle` | No active exchange in DB → MDS idles without error |
| E3 | `test_redis_hset_failure_db_still_persists` | Mock Redis HSET to raise exception. Verify Tier 2 DB write still executes and succeeds. |
| E4 | `test_redis_publish_failure_no_crash` | Mock Redis PUBLISH to raise. Verify cycle completes, no crash. |

### Static Analysis / Grep Proofs

| # | Test | Method |
|---|------|--------|
| G1 | `test_grep_no_ticker_percentage_in_mds` | `subprocess.run(["grep", "-rn", "percentage", "app/services/market_data.py"])` returns empty |
| G2 | `test_grep_no_ticker_baseVolume_in_mds` | `subprocess.run(["grep", "-rn", "baseVolume", "app/services/market_data.py"])` returns empty |
| G3 | `test_grep_no_frontend_metric_computation` | `subprocess.run(["grep", "-rn", "change_1h\|change_4h\|volume_1h", "web/frontend/src/"])` returns empty |

### No-Mock-Data Enforcement

All integration tests use either pgserver (real PostgreSQL 16), fakeredis (faithful Redis with sorted sets + HASH + pub/sub + TTL + pipeline), or CCXT mock returning realistic deterministic data (fixed timestamps, known prices). No hardcoded fake data injected into DB/Redis that bypasses MDS computation. Tests seed raw OHLCV bars and verify MDS computes correct derived metrics.

### Manual API Proof (post-implementation)

8-step proof script against real pgserver + fakeredis + mocked CCXT:

1. Seed exchange + 3 tradable assets (BTC, ETH, SOL).
2. Seed 168 1H + 30 1D OHLCV bars per asset into Redis sorted sets (simulating MDS warm-up).
3. Trigger MDS snapshot computation via `_assemble_and_flush`.
4. `GET /market-data/snapshots` → 3 snapshots, all 18 fields non-null, `data_source: "redis"`.
5. Verify BTC `change_24h` matches hand-calculated formula from seeded bar closes.
6. Verify ETH `volume_24h` matches sum of 24 seeded 1H candle volumes.
7. Verify `source_candle_ts` equals the most recent bar with `ts < floor_to_hour(now)` — NOT the open candle.
8. Clear Redis entirely → `GET /market-data/snapshots` → 3 snapshots with `data_source: "db"`.

---

## Section 10 — Acceptance Criteria

### Pre-existing Baseline

The test suite baseline from Phase 1 is: **811 passed, 1 failed (`test_phase8g` — pre-existing), 5 skipped.** Phase 2 acceptance is measured relative to this baseline.

### Functional Criteria

| # | Criterion | Evidence |
|---|-----------|----------|
| F1 | MDS starts with FastAPI lifespan, stops on shutdown | Startup/shutdown logs in manual proof |
| F2 | OHLCV bars fetched from CCXT and stored in Redis sorted sets | Tests R1, R2 passing |
| F3 | OHLCV bars persisted to PostgreSQL on Tier 2 cycles | Tests D1, D2 passing |
| F4 | All 18 snapshot fields computed correctly | Tests U1–U14 all passing |
| F5 | Snapshot written to Redis HASH with 600s TTL | Tests R3, R4, R5 passing |
| F6 | Snapshot persisted to `Asset.market_snapshot` JSONB on Tier 2 | Test D3 passing |
| F7 | Snapshot published to `nexus:events:ticker` on every tier | Tests W1, W2 passing |
| F8 | REST endpoints read Redis (pipelined HGETALL) first, fall back to DB | Tests D7, D8, D13 passing |
| F9 | Staged warm-up: price within 75s, 1H metrics within 135s, full within 7min | Tests S1, S2, S3 passing |
| F10 | OHLCV retention pruning works for both 1H (7d) and 1D (30d) | Tests D5, D6 passing |
| F11 | `ticker.percentage` is NEVER used in metric computation | Tests U17, G1 passing |
| F12 | `ticker.baseVolume` is NEVER used in metric computation | Tests U18, G2 passing |
| F13 | Frontend does NOT compute any metrics | Test G3 passing (grep proof) |
| F14 | `source_candle_ts` uses hour-boundary rule, not index | Tests U15, U16, U16b, U16c, U16d passing + manual proof step 7 |
| F15 | CCXT failure does not crash MDS | Test E1 passing |
| F16 | WS payload matches REST snapshot for same asset at same time | Test W4 passing |
| F17 | Snapshot writes are atomic via single-writer assembler — no tier race conditions | Tests A1, A2, A3, A4, A5 passing |
| F18 | No field regression across tiers (Tier 0 preserves Tier 2 fields, Tier 2 preserves Tier 0 fields) | Tests A2, A3 passing |
| F19 | Redis failure does not block DB persistence | Tests A6, E3 passing |
| F20 | Lock scope is limited to in-memory merge only — no I/O under lock | Code review + test A5 (rapid Tier 0 throughput) |

### Non-Regression Criteria

| # | Criterion | Evidence |
|---|-----------|----------|
| NR1 | Phase 1 tests pass unchanged | `pytest tests/test_phase1_asset_management.py` — 21 passed, 0 failed |
| NR2 | Phase 6 logging canary passes | `pytest tests/test_phase6_logging.py::TestAuditMiddleware::test_normal_request_produces_audit_log` — PASS |
| NR3 | Full suite: no NEW failures relative to baseline | `pytest tests/ -m "not docker_integration"` — pre-existing `test_phase8g` failure only; 0 new failures |
| NR4 | Phase 1 contracts preserved | `is_tradable`, `allocation_weight` not modified by MDS (test D4) |
| NR5 | sync-assets preserves `market_snapshot` | Verified by existing Phase 1 test suite |

### Scoped Diff Criteria

| # | Criterion |
|---|-----------|
| SD1 | No changes to `core/scanning/scanner.py` |
| SD2 | No changes to `web/engine/main.py` |
| SD3 | No changes to `config.yaml` |
| SD4 | No changes to frontend files |
| SD5 | No modifications to Phase 1 endpoints or models (except router registration in main.py) |
| SD6 | No changes to `web/backend/app/ws/manager.py` |
| SD7 | No changes to `web/backend/app/api/engine.py` |
| SD8 | `git diff --stat` shows only the files listed in Section 1 CREATE + MODIFY |

### Manual Proof Criteria

| # | Criterion |
|---|-----------|
| MP1 | 8-step manual API proof script runs with all steps PASS |
| MP2 | Proof output included in Phase 2 remediation report |

---

## Appendix A — Redis Key Reference

| Key | Type | Content | TTL | Written by | Read by |
|-----|------|---------|-----|-----------|---------|
| `nexus:ohlcv:1h:{asset_id}` | Sorted Set | 1H bars, score=ts_ms, value=JSON `[ts,o,h,l,c,v]` | 48h (renewed on write) | MDS Tier 1 | MDS metric computation, REST /ohlcv |
| `nexus:ohlcv:1d:{asset_id}` | Sorted Set | 1D bars, score=ts_ms, value=JSON `[ts,o,h,l,c,v]` | 7d (renewed on write) | MDS Tier 2 | MDS metric computation, REST /ohlcv |
| `nexus:snapshot:{asset_id}` | HASH | 18 snapshot fields, each JSON-encoded | 600s (renewed on write) | MDS assembler (all tiers) | REST /snapshots, REST /snapshots/{id} |
| `nexus:events:ticker` | Pub/Sub | Snapshot update payloads (ephemeral) | N/A | MDS assembler | WS ConnectionManager bridge |

## Appendix B — Configuration Constants

| Constant | Value | Location | Notes |
|----------|-------|----------|-------|
| `TIER0_INTERVAL_S` | 15 | `market_data.py` | Price-only cycle |
| `TIER1_INTERVAL_S` | 60 | `market_data.py` | 1H OHLCV + short-horizon metrics |
| `TIER2_INTERVAL_S` | 300 | `market_data.py` | 1D OHLCV + deep metrics + DB persist |
| `TRADABLE_CACHE_TTL_S` | 60 | `market_data.py` | In-memory tradable asset cache refresh |
| `OHLCV_1H_MAX_BARS` | 168 | `market_data.py` | 7 days of 1H in Redis |
| `OHLCV_1D_MAX_BARS` | 30 | `market_data.py` | 30 days of 1D in Redis |
| `OHLCV_1H_FETCH_LIMIT` | 4 | `market_data.py` | Incremental: 3 closed + 1 open |
| `OHLCV_1D_FETCH_LIMIT` | 2 | `market_data.py` | Incremental: 1 closed + 1 open |
| `WARMUP_1H_BARS` | 168 | `market_data.py` | Full backfill for new assets |
| `WARMUP_1D_BARS` | 30 | `market_data.py` | Full backfill for new assets |
| `SNAPSHOT_TTL_S` | 600 | `market_data.py` | 10 minutes. 2× headroom over Tier 2 (300s). |
| `OHLCV_1H_TTL_S` | 172800 | `market_data.py` | 48 hours |
| `OHLCV_1D_TTL_S` | 604800 | `market_data.py` | 7 days |
| `DB_PRUNE_INTERVAL_S` | 21600 | `market_data.py` | 6 hours between DB prune runs |
| `DB_RETENTION_1H_HOURS` | 168 | `market_data.py` | 7 days in PostgreSQL |
| `DB_RETENTION_1D_DAYS` | 30 | `market_data.py` | 30 days in PostgreSQL |
| `CCXT_CONCURRENCY` | 3 | `market_data.py` | Max concurrent fetch_ohlcv tasks |
| `CCXT_RATE_LIMIT_MS` | 350 | `market_data.py` | Passed to CCXT config |

## Appendix C — Dependency Graph

```
Phase 0 (Initial Schema) ← ohlcv table, OHLCV model
    └── Phase 1 (Asset Management) ← ACCEPTED
         ├── Asset.market_snapshot (JSONB), Asset.snapshot_updated_at
         ├── Asset.is_tradable, Asset.allocation_weight
         └── Phase 2 (Market Data Service) ← THIS PLAN
              ├── reads: Asset.is_tradable (Phase 1, read-only)
              ├── reads: Asset.exchange_id, symbol (Phase 0)
              ├── writes: Redis hot cache (nexus:ohlcv:*, nexus:snapshot:*)
              ├── writes: Asset.market_snapshot, Asset.snapshot_updated_at (Tier 3 only)
              ├── writes: ohlcv table (Tier 3 only)
              ├── publishes: nexus:events:ticker (existing WS channel)
              └── adds: REST endpoints, MDS service, Alembic index migration
```
