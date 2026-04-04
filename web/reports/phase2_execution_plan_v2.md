# Phase 2 Execution Plan v2 — Market Data Service + Snapshot Pipeline

**Status:** PENDING APPROVAL — do NOT implement until explicitly authorized.
**Date:** 2026-04-03
**Revision:** v2 — corrects authority model, cache location, tier separation, warm-up staging, file scope, and test coverage from v1.
**Depends on:** Phase 1 (Asset Management) — ACCEPTED
**Constraints:** No scanner changes, no engine changes, no config.yaml changes, no frontend computation, no SQLite authority, no fake data. Phase 1 contracts preserved.

---

## Section 1 — Exact Files to Create or Modify

### CREATE

| File | Purpose |
|------|---------|
| `web/backend/app/services/market_data.py` | MarketDataService class — singleton async service. Owns the full pipeline: CCXT fetch → Redis hot cache (OHLCV + snapshots) → PostgreSQL persistence → Redis pub/sub WS delivery. |
| `web/backend/app/api/market_data.py` | REST router: `GET /market-data/snapshots`, `GET /market-data/snapshots/{asset_id}`, `GET /market-data/ohlcv/{asset_id}`. Reads from Redis hot cache first, falls back to PostgreSQL. |
| `web/backend/alembic/versions/<hash>_add_ohlcv_indexes.py` | Migration: adds `idx_ohlcv_timestamp` (retention pruning) and `idx_ohlcv_asset_tf_latest` (descending timestamp, "latest N bars" queries). No new tables, no new columns. |
| `web/backend/tests/test_phase2_market_data.py` | Full test suite: unit tests (metric formulas, ticker field exclusion proofs), integration tests (pgserver + fakeredis for cache/DB/WS/REST paths, staged warm-up, TTL behaviour). |

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
| `web/backend/app/api/engine.py` | MDS uses direct CCXT — no engine command wrappers needed for Phase 2. Existing `_send_engine_command` is untouched. |
| `web/engine/main.py` | No engine changes — constraint. MDS is independent. |
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
├── _tasks: dict[str, asyncio.Task]            (one per tier)
├── _known_asset_ids: set[int]                 (warm-up tracking)
│
├── start() → spawns tier tasks
├── stop() → cancels all tasks, closes CCXT
│
├── _tier0_loop()           # price-only: fetch_tickers → Redis snapshot → WS publish
├── _tier1_loop()           # 1H metrics: fetch 1h OHLCV → Redis OHLCV cache → recompute 1h/4h
├── _tier2_loop()           # deep metrics: fetch 1d OHLCV → Redis OHLCV cache → recompute 12h/24h/7d → DB persist
├── _tier3_persist()        # called by Tier 2: flush snapshot + OHLCV to PostgreSQL
│
├── _fetch_tickers(symbols) → dict[str, TickerData]
├── _fetch_ohlcv(symbol, tf, limit) → list[list]
├── _compute_snapshot(asset_id, ticker, ohlcv_1h, ohlcv_1d) → dict
├── _warm_up_asset(asset_id, symbol) → None   (staged: price → partial → full)
│
├── _write_redis_snapshot(asset_id, snapshot) → None
├── _read_redis_snapshot(asset_id) → dict | None
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

### Lifecycle

1. `main.py` `lifespan` creates `MarketDataService(session_factory, redis, settings)`.
2. `start()` queries DB for active exchange, initializes CCXT instance (demo mode, `enableRateLimit: True`, 15s timeout, `rateLimit: 350`), calls `load_markets()`, then spawns three `asyncio.Task`s: `_tier0_loop`, `_tier1_loop`, `_tier2_loop`.
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
| Redis write failure | Log error, skip WS delivery. DB persist still attempted. Snapshot is stale but not lost. |
| DB write failure | Log error, skip DB persist. Redis hot cache is still valid. Next Tier 2 cycle retries. |
| No active exchange in DB | MDS enters idle mode. Checks every 60s for an active exchange. |

---

## Section 3 — OHLCV Cache Design

### Dual-Layer Cache

**Redis is the operational cache.** All metric computation reads from Redis OHLCV lists. PostgreSQL is the persistence/history layer — written to on Tier 2 cycles for crash recovery and REST fallback.

### Redis OHLCV Keys

| Key pattern | Content | Max entries | TTL |
|------------|---------|-------------|-----|
| `nexus:ohlcv:1h:{asset_id}` | Sorted list of 1H bars, JSON-encoded `[ts_ms, o, h, l, c, v]` per entry | 168 (7 days) | 48h (auto-renewed on every write; if MDS is down >48h, cold-start required) |
| `nexus:ohlcv:1d:{asset_id}` | Sorted list of 1D bars, JSON-encoded `[ts_ms, o, h, l, c, v]` per entry | 30 (30 days) | 7d (auto-renewed on every write) |

**Data structure:** Redis Sorted Set (`ZADD`) keyed by `ts_ms` as score. This gives O(log N) insert and O(log N + M) range queries. `ZRANGEBYSCORE` retrieves bars in a time window; `ZREMRANGEBYSCORE` trims old bars.

**Why 30 1D bars (not 14):** `change_7d` requires a bar from at least 7 days ago. 14 bars would work for that, but 30 bars provides a comfortable buffer for gap handling (exchange downtime, missing daily candles) and allows future extension to `change_30d` without a cache redesign.

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

### Snapshot JSONB Schema

```json
{
  "price": 84231.50,
  "source": "ticker",
  "source_candle_ts": "2026-04-03T14:00:00Z",
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

**Total keys: 16.** All values are scalars (float, string, or null). No nested objects.

### `source_candle_ts` — Exact Semantics

`source_candle_ts` is the **timestamp (open time) of the most recent fully closed 1H candle used as the reference point for all OHLCV-derived metrics in this snapshot.** It is NOT the current time, NOT the ticker timestamp, and NOT the open candle's timestamp.

**Derivation:**

```python
# Redis: get the two most recent 1H bars (sorted by score descending)
bars = ZREVRANGE nexus:ohlcv:1h:{asset_id} 0 1 WITHSCORES
# bars[0] = current open candle (ts_ms > current_hour_start)
# bars[1] = most recent closed candle
source_candle_ts = datetime.utcfromtimestamp(bars[1].score / 1000).isoformat() + "Z"
```

**Consistency guarantee:** Every `change_Nh` and `volume_Nh` metric in the snapshot is computed relative to this same `source_candle_ts` anchor. If `source_candle_ts` is `2026-04-03T14:00:00Z`, then:
- `change_1h` compares current price to the close of the candle at `13:00:00Z`
- `change_4h` compares current price to the close of the candle at `10:00:00Z`
- `volume_1h` is the volume of the single candle at `14:00:00Z` (the closed bar)
- All metrics share the same temporal reference frame

If the latest closed candle is stale (older than expected), `source_candle_ts` reveals it. The frontend can compare `source_candle_ts` to its local clock.

### Price Source vs OHLCV Source — Explicit Distinction

| Field | Source | Origin |
|-------|--------|--------|
| `price` | CCXT `ticker['last']` | Tier 0 fetch |
| `bid`, `ask` | CCXT `ticker['bid']`, `ticker['ask']` | Tier 0 fetch |
| `last_trade_ts` | CCXT `ticker['datetime']` | Tier 0 fetch |
| `source` | `"ticker"` if from live ticker, `"ohlcv"` if fell back to candle close | Tier 0 |
| `change_*`, `volume_*`, `high_24h`, `low_24h` | Redis OHLCV cache | Tier 1 / Tier 2 |
| `spread_pct` | Computed from `bid`/`ask` | Tier 0 |
| `source_candle_ts` | Most recent closed 1H bar timestamp in Redis | Tier 1 / Tier 2 |
| `computed_at` | `datetime.utcnow()` at snapshot assembly | All tiers |

### Exact Formulas

#### Price Changes (percentage, 2 decimal places)

```
change_Nh = round(((current_price - close_N_hours_ago) / close_N_hours_ago) * 100, 2)
```

Where `close_N_hours_ago` is obtained from the Redis 1H sorted set:

```
ZREVRANGEBYSCORE nexus:ohlcv:1h:{asset_id}
    (source_candle_ts_ms - N*3600*1000)
    -inf
    LIMIT 0 1
```

This returns the bar with the highest timestamp that is ≤ `source_candle_ts - N hours`. Parse its close value.

| Metric | N (hours) | Source TF | Fallback |
|--------|-----------|-----------|----------|
| `change_1h` | 1 | 1H Redis cache | `null` if <2 bars |
| `change_4h` | 4 | 1H Redis cache | `null` if <5 bars |
| `change_12h` | 12 | 1H Redis cache | `null` if <13 bars |
| `change_24h` | 24 | 1H Redis cache | `null` if <25 bars |
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

Volume is summed over the N most recent **closed** 1H candles from the Redis sorted set, excluding the current open candle.

```
ZRANGEBYSCORE nexus:ohlcv:1h:{asset_id}
    (source_candle_ts_ms - N*3600*1000)
    source_candle_ts_ms
```

Sum the `volume` field (index 5) of all returned bars.

| Metric | N (hours) |
|--------|-----------|
| `volume_1h` | 1 (single bar at `source_candle_ts`) |
| `volume_4h` | 4 |
| `volume_24h` | 24 |

#### 24h High/Low

From the 1H Redis cache, scan the 25 most recent bars (including current open):

```
ZREVRANGE nexus:ohlcv:1h:{asset_id} 0 24
```

`high_24h` = max of all `high` values (index 2). `low_24h` = min of all `low` values (index 3).

#### Spread

```
spread_pct = round(((ask - bid) / ((ask + bid) / 2)) * 100, 4)
```

If `bid` or `ask` is null/zero → `spread_pct` is `null`.

### Forbidden Fields — `ticker.percentage` and `ticker.baseVolume`

**`ticker['percentage']` is NEVER used.** All percentage changes are derived from OHLCV close prices via the formulas above. CCXT's `ticker.percentage` is exchange-reported, inconsistent across exchanges, and may use different time windows.

**`ticker['baseVolume']` is NEVER used.** All volume metrics are derived from OHLCV candle volumes. `ticker.baseVolume` represents a different aggregation window and unit (base currency vs quote currency) than what the snapshot requires.

The only fields read from the CCXT ticker response are: `last`, `bid`, `ask`, `datetime`. All other ticker fields are discarded.

**Enforcement:** See Section 9 tests U15–U17 and acceptance criteria F11–F13.

### Null Handling

Any metric that cannot be computed (insufficient OHLCV data, missing ticker fields, division by zero) is `null` in the JSONB. The frontend renders `null` as "—" or equivalent.

---

## Section 5 — Update Frequency and Rate-Limit Budget

### Tier Model

| Tier | Cadence | What it fetches | What it computes | What it writes |
|------|---------|----------------|-----------------|----------------|
| **Tier 0** (price) | **15s** | `fetch_tickers(symbols)` — 1 batch call | `price`, `bid`, `ask`, `spread_pct`, `last_trade_ts` | Redis snapshot (price fields only), Redis pub/sub `nexus:events:ticker` |
| **Tier 1** (short-horizon) | **60s** | `fetch_ohlcv(symbol, "1h", limit=4)` per tradable symbol | `change_1h`, `change_4h`, `volume_1h`, `volume_4h`, `high_24h`, `low_24h`, `source_candle_ts` | Redis OHLCV 1H cache (ZADD + trim), Redis snapshot (merge Tier 1 fields), Redis pub/sub |
| **Tier 2** (deep-horizon) | **5min** | `fetch_ohlcv(symbol, "1d", limit=2)` per tradable symbol | `change_12h`, `change_24h`, `change_7d`, `volume_24h` | Redis OHLCV 1D cache (ZADD + trim), Redis snapshot (merge Tier 2 fields), Redis pub/sub |
| **Tier 3** (persistence) | **5min** (piggybacks on Tier 2) | Nothing — reads from Redis | Nothing new | PostgreSQL `ohlcv` table flush, PostgreSQL `Asset.market_snapshot` + `snapshot_updated_at` |

### Why These Cadences

- **Tier 0 at 15s:** Price is the most time-sensitive metric. 15s provides near-real-time feel without overwhelming the exchange. `fetch_tickers` is a single batch call regardless of symbol count.
- **Tier 1 at 60s:** 1H candles close once per hour. Fetching every 60s catches the latest closed bar within 60s of close. More frequent is wasteful — the candle data doesn't change until the next close.
- **Tier 2 at 5min:** Daily candles close once per 24h. 12h/24h/7d metrics change slowly. 5-minute cadence is sufficient. Tier 3 DB persistence piggybacks here to avoid separate scheduling.

### Service Loop Design

Three separate `asyncio.Task`s, one per tier (Tier 3 is a subroutine of Tier 2, not a separate task):

```
_tier0_loop:
    while running:
        t0 = time.monotonic()
        await _fetch_tickers(tradable_symbols)
        _update_redis_snapshot_price_fields(...)
        _publish_ws(...)
        elapsed = time.monotonic() - t0
        await asyncio.sleep(max(0, TIER0_INTERVAL - elapsed))

_tier1_loop:
    while running:
        t0 = time.monotonic()
        for symbol in tradable_symbols (semaphore-bounded):
            await _fetch_ohlcv(symbol, "1h", limit=4)
            _zadd_redis_ohlcv_1h(...)
            _trim_redis_ohlcv_1h(...)
        _recompute_tier1_metrics(...)
        _merge_into_redis_snapshot(...)
        _publish_ws(...)
        elapsed = time.monotonic() - t0
        await asyncio.sleep(max(0, TIER1_INTERVAL - elapsed))

_tier2_loop:
    while running:
        t0 = time.monotonic()
        for symbol in tradable_symbols (semaphore-bounded):
            await _fetch_ohlcv(symbol, "1d", limit=2)
            _zadd_redis_ohlcv_1d(...)
            _trim_redis_ohlcv_1d(...)
        _recompute_tier2_metrics(...)
        _merge_into_redis_snapshot(...)
        _publish_ws(...)
        _tier3_persist()  # flush to PostgreSQL
        elapsed = time.monotonic() - t0
        await asyncio.sleep(max(0, TIER2_INTERVAL - elapsed))
```

### Overrun Handling Per Tier

If a tier's cycle takes longer than its interval:

- **Tier 0:** Log warning `"Tier 0 overrun: {elapsed:.1f}s > {TIER0_INTERVAL}s"`. Skip the sleep — immediately start the next cycle. Do NOT double up — the next cycle replaces the stale data.
- **Tier 1:** Log warning. If overrun >2× interval for 3 consecutive cycles, increase `TIER1_INTERVAL` by 50% (adaptive). Emit `SYSTEM_ALERT` via Redis.
- **Tier 2:** Same as Tier 1 but with `TIER2_INTERVAL`.
- **Cross-tier isolation:** Tier 0 never waits for Tier 1/2. Tier 1 never waits for Tier 2. Each task is independent.

### Per-Cycle API Call Budget

| Call | Per symbol | Count | Notes |
|------|-----------|-------|-------|
| `fetch_tickers(symbols)` | 1 batch | 1 | Single call, all symbols. Returns all tickers. |
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

At 100 symbols, Tier 1 cycle time at 350ms spacing = 35s (within 60s interval). Tier 2 = 7s (within 300s interval). Tier 0 is always 1 call regardless of symbol count.

### Concurrency

- `fetch_tickers`: single batch call, no concurrency needed.
- `fetch_ohlcv` (Tier 1 and Tier 2): parallelized with `asyncio.Semaphore(3)`. CCXT's internal rate limiter serializes HTTP calls, but the semaphore bounds task creation.

---

## Section 6 — Snapshot Authority and Delivery Model

### Authority Hierarchy

```
CCXT (exchange) → MDS (compute) → Redis (hot authority) → PostgreSQL (persisted fallback)
                                       ↓
                                  WebSocket (primary delivery)
                                       ↓
                                  REST (fallback delivery, reads Redis first then DB)
```

**Redis is the hot authority.** It holds the most recent snapshot and is the first source read by both WS delivery and REST endpoints.

**PostgreSQL is the persisted fallback.** It is written to every Tier 2 cycle (5 minutes) and serves as the recovery source after Redis expiry or cold start.

### Redis Snapshot Keys

| Key pattern | Content | TTL |
|------------|---------|-----|
| `nexus:snapshot:{asset_id}` | Full 16-field snapshot JSON (Section 4 schema) | 5 minutes (renewed on every Tier 0/1/2 write) |
| `nexus:snapshot:all:{exchange_id}` | JSON dict mapping `asset_id → snapshot` for all tradable assets | 5 minutes (renewed on every write) |

**Write sequence:** Redis is written **first**, before PostgreSQL. This ensures the hot path is always fresh.

### Write Path Per Tier

**Tier 0 (every 15s):**
```
1. fetch_tickers → extract price/bid/ask/spread/last_trade_ts
2. GET nexus:snapshot:{asset_id} → current snapshot
3. Merge price fields into snapshot, update computed_at
4. SET nexus:snapshot:{asset_id} (EX 300) → Redis hot cache
5. PUBLISH nexus:events:ticker {asset_id, symbol, snapshot}
6. (No DB write — Tier 0 is too frequent for DB)
```

**Tier 1 (every 60s):**
```
1. fetch_ohlcv 1H → ZADD nexus:ohlcv:1h:{asset_id} + trim
2. Recompute change_1h, change_4h, volume_1h, volume_4h, high_24h, low_24h, source_candle_ts
3. GET nexus:snapshot:{asset_id} → current snapshot
4. Merge Tier 1 fields, update computed_at
5. SET nexus:snapshot:{asset_id} (EX 300) → Redis hot cache
6. SET nexus:snapshot:all:{exchange_id} (EX 300)
7. PUBLISH nexus:events:ticker {type: "snapshot_update", ...}
8. (No DB write — piggybacked on Tier 2)
```

**Tier 2 (every 5min):**
```
1. fetch_ohlcv 1D → ZADD nexus:ohlcv:1d:{asset_id} + trim
2. Recompute change_12h, change_24h, change_7d, volume_24h
3. GET nexus:snapshot:{asset_id} → current snapshot
4. Merge Tier 2 fields, update computed_at
5. SET nexus:snapshot:{asset_id} (EX 300) → Redis hot cache
6. SET nexus:snapshot:all:{exchange_id} (EX 300)
7. PUBLISH nexus:events:ticker {type: "snapshot_update", ...}
8. _tier3_persist():
   a. UPDATE assets SET market_snapshot = ?, snapshot_updated_at = now() WHERE id = ?
   b. INSERT INTO ohlcv ... ON CONFLICT DO UPDATE (batch, latest bars from Redis)
```

### Read Paths

| Consumer | Primary path | Fallback path | Trust rule |
|----------|-------------|---------------|------------|
| WS subscriber (`ticker` channel) | Redis pub/sub (pushed by MDS) | — (no fallback; WS is fire-and-forget) | Always receives the freshest snapshot |
| `GET /market-data/snapshots` | Redis `GET nexus:snapshot:all:{exchange_id}` | PostgreSQL `SELECT market_snapshot FROM assets WHERE is_tradable` | Return whichever has the more recent `computed_at` |
| `GET /market-data/snapshots/{id}` | Redis `GET nexus:snapshot:{asset_id}` | PostgreSQL `SELECT market_snapshot FROM assets WHERE id = ?` | Return whichever has the more recent `computed_at` |
| `GET /market-data/ohlcv/{id}` | Redis `ZRANGEBYSCORE nexus:ohlcv:{tf}:{asset_id}` | PostgreSQL `SELECT * FROM ohlcv WHERE asset_id = ? AND timeframe = ?` | Redis first; if empty, fall back to DB |

### Frontend Trust Hierarchy

The frontend does not choose between sources — it receives a single response from the REST endpoint or WS channel. The backend (MDS + REST handler) resolves the trust hierarchy:

1. **WS delivery:** Most trusted. Pushed every 15s (Tier 0). Frontend treats WS snapshots as authoritative.
2. **REST response:** On initial page load or reconnection, frontend calls `GET /market-data/snapshots`. The handler reads Redis first; if Redis is empty or expired, it reads PostgreSQL. The response includes `computed_at` — the frontend can display a "stale" indicator if `computed_at` is older than 5 minutes.

### Temporary Redis/DB Divergence

Between Tier 0/1 writes (Redis only) and Tier 2 writes (Redis + DB), Redis is ahead of PostgreSQL by up to 5 minutes. This is expected and acceptable:

- WS subscribers always get the freshest data (from Redis pub/sub).
- REST reads Redis first. Only if Redis is empty (MDS down, key expired) does it fall back to DB.
- The `computed_at` field in both Redis and DB snapshots lets any consumer detect staleness.
- There is no conflict resolution needed — Redis is always ≥ DB freshness. If Redis is empty, DB is the best available.

### Phase 1 Contract Preservation

- `Asset.market_snapshot` and `Asset.snapshot_updated_at` are written ONLY by MDS Tier 3. sync-assets `ON CONFLICT DO UPDATE` already excludes these columns.
- `Asset.is_tradable` and `Asset.allocation_weight` are NEVER written by MDS. MDS reads `is_tradable` to determine the tradable set.
- Phase 1 PATCH endpoints do not touch `market_snapshot` or `snapshot_updated_at`.

---

## Section 7 — API and Schema Changes

### New REST Endpoints

All endpoints require `Depends(get_current_user)`.

#### `GET /api/v1/market-data/snapshots`

Returns latest snapshots for all tradable assets. Reads Redis first (`nexus:snapshot:all:{exchange_id}`), falls back to PostgreSQL.

**Query params:** `exchange_id` (optional, defaults to active exchange)

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
        "source_candle_ts": "2026-04-03T14:00:00Z",
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
      "source": "redis"
    }
  ],
  "count": 5,
  "exchange_id": 1
}
```

Note: `"source": "redis"` or `"source": "db"` at the item level indicates which layer served this snapshot. This is for debugging/observability only — the frontend ignores it.

#### `GET /api/v1/market-data/snapshots/{asset_id}`

Returns single asset snapshot. Redis first, DB fallback. 404 if asset not found.

**Response (200):** Single object from `snapshots` array above (unwrapped).

#### `GET /api/v1/market-data/ohlcv/{asset_id}`

Returns OHLCV bars. Redis first, DB fallback.

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
    {"timestamp": "2026-04-03T14:00:00Z", "open": 84100.0, "high": 84350.0, "low": 84050.0, "close": 84231.5, "volume": 12450000.0}
  ],
  "count": 168,
  "source": "redis"
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
      "snapshot": { "...full 16-field snapshot..." }
    }
  ],
  "timestamp": "2026-04-03T14:30:05Z"
}
```

`"tier"` indicates which tier produced this update (0, 1, or 2). Tier 0 payloads only have price/bid/ask/spread populated in the snapshot (other fields are carried forward from the last Tier 1/2 run). This lets the frontend know the scope of the update.

### Existing WS Manager — Reused Unchanged

The `ConnectionManager` in `ws/manager.py` is reused without modification:
- `"ticker"` is already in `CHANNELS` (line 34).
- The Redis bridge (`psubscribe("nexus:events:*")`) routes `nexus:events:ticker` to WS subscribers on the `ticker` channel.
- Rate limiting (20 msgs/sec per client) applies. At Tier 0 every 15s, this is well under limit.

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

When a user marks an asset as tradable via Phase 1's `PATCH /{id}/assets/{asset_id}` with `is_tradable: true`, MDS detects the new asset on its next cycle via polling (no Phase 1 modifications required).

### Detection Mechanism

Each Tier 0 cycle, MDS queries the current tradable asset set from PostgreSQL:

```sql
SELECT id, symbol FROM assets WHERE exchange_id = ? AND is_tradable = true
```

Compares against `_known_asset_ids` (in-memory set). Any new `asset_id` triggers the staged warm-up.

### Staged Warm-Up Timeline

| Stage | Trigger | Latency from toggle | What happens | Metrics available after |
|-------|---------|--------------------|--------------|-----------------------|
| **Stage 0** (detection) | Next Tier 0 cycle | 0–15s | New asset_id detected. Added to `_known_asset_ids`. `fetch_tickers` already includes it (batch call). | `price`, `bid`, `ask`, `spread_pct`, `last_trade_ts`. All change/volume fields = `null`. |
| **Stage 1** (1H backfill) | Next Tier 1 cycle | 15s–75s | `fetch_ohlcv(symbol, "1h", limit=168)` — full 7-day backfill. ZADD into Redis. | `change_1h`, `change_4h`, `volume_1h`, `volume_4h`, `high_24h`, `low_24h`, `source_candle_ts`. `change_12h`/`change_24h`/`change_7d`/`volume_24h` still `null`. |
| **Stage 2** (1D backfill) | Next Tier 2 cycle | 75s–375s | `fetch_ohlcv(symbol, "1d", limit=30)` — full 30-day backfill. ZADD into Redis. Tier 3 persist to DB. | ALL 16 fields populated. Full snapshot. |

### UI-Visible Timeline

- **Within 15 seconds:** User sees the asset appear in the market data list with a live price, bid/ask, and spread. Change and volume columns show "—".
- **Within ~75 seconds:** 1H metrics populate. User sees change_1h, change_4h, and hourly volumes.
- **Within ~6 minutes:** All metrics populated including change_7d and volume_24h. Full snapshot complete.

### Backfill vs Incremental

Only Stage 1 and Stage 2 perform backfill fetches (168 bars + 30 bars). After initial warm-up, all subsequent cycles use incremental fetches (4 bars + 2 bars). The backfill is a one-time cost per newly-enabled asset.

### Backfill API Cost

Per newly-enabled asset: 2 CCXT calls (168 1H bars + 30 1D bars). At 350ms spacing: ~0.7s. This runs within the normal Tier 1/2 cycle and does not require a separate task.

### Disabled Asset Cleanup

When `is_tradable` flips to `false`:
1. Asset removed from `_known_asset_ids` on next Tier 0 cycle.
2. Redis OHLCV keys expire naturally (48h/7d TTL).
3. Redis snapshot key expires (5min TTL).
4. PostgreSQL OHLCV rows pruned by normal retention (7d/30d).
5. `Asset.market_snapshot` is NOT cleared — retains last-known values. `snapshot_updated_at` naturally shows staleness.

---

## Section 9 — Test Plan

### Test File

`web/backend/tests/test_phase2_market_data.py`

### Fixtures and Infrastructure

- `pgserver` for PostgreSQL (same `_PGClientContext` pattern as Phase 1).
- `fakeredis.aioredis.FakeRedis` for Redis (supports sorted sets, pub/sub, TTL).
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
| U14 | `test_snapshot_schema_16_keys` | All 16 keys present, including `source_candle_ts` |
| U15 | `test_source_candle_ts_is_closed_bar` | `source_candle_ts` equals the most recent fully closed 1H bar, NOT the open candle |
| U16 | `test_source_candle_ts_consistency` | All change_Nh metrics reference the same anchor bar |

### Unit Tests — Forbidden Field Exclusion Proofs

| # | Test | Validates |
|---|------|-----------|
| U17 | `test_ticker_percentage_never_read` | Mock ticker with `percentage=99.9`. Computed `change_24h` uses OHLCV, not 99.9. Proof: result differs from `ticker.percentage`. |
| U18 | `test_ticker_baseVolume_never_read` | Mock ticker with `baseVolume=999999`. Computed `volume_24h` uses OHLCV sums, not 999999. Proof: result differs from `ticker.baseVolume`. |
| U19 | `test_no_ticker_fields_beyond_allowed` | Assert that MDS only accesses `ticker['last']`, `ticker['bid']`, `ticker['ask']`, `ticker['datetime']`. Mock ticker as `MagicMock(spec=dict)` with `__getitem__` tracking — any access to other keys fails the test. |

### Integration Tests — Redis Hot Cache (fakeredis)

| # | Test | Validates |
|---|------|-----------|
| R1 | `test_ohlcv_zadd_and_retrieve` | ZADD 168 bars → ZRANGEBYSCORE returns correct bars |
| R2 | `test_ohlcv_trim_to_max` | ZADD 200 bars → trim to 168 → ZCARD == 168, oldest removed |
| R3 | `test_snapshot_set_and_get` | SET snapshot JSON → GET returns identical dict |
| R4 | `test_snapshot_ttl_expires` | SET with EX 300 → fakeredis `time_travel(301)` → GET returns None |
| R5 | `test_snapshot_ttl_renewed` | SET → write again before expiry → TTL reset to 300 |
| R6 | `test_ohlcv_cold_start_empty_redis` | Redis empty → MDS falls back to DB → backfills Redis |
| R7 | `test_snapshot_all_key_consistency` | `nexus:snapshot:all:{eid}` contains same data as individual `nexus:snapshot:{aid}` keys |

### Integration Tests — DB Fallback (pgserver + fakeredis)

| # | Test | Validates |
|---|------|-----------|
| D1 | `test_ohlcv_upsert_insert` | Fresh bars inserted into PostgreSQL |
| D2 | `test_ohlcv_upsert_update_on_conflict` | Duplicate (asset_id, tf, ts) updates values |
| D3 | `test_snapshot_persisted_to_asset` | `Asset.market_snapshot` JSONB populated, `snapshot_updated_at` set |
| D4 | `test_snapshot_preserves_phase1_fields` | After MDS write, `is_tradable` and `allocation_weight` unchanged |
| D5 | `test_retention_prune_1h` | 1H candles older than 7 days deleted, recent preserved |
| D6 | `test_retention_prune_1d` | 1D candles older than 30 days deleted, recent preserved |
| D7 | `test_rest_snapshots_reads_redis_first` | Seed Redis + DB with different `computed_at`. GET returns Redis version. |
| D8 | `test_rest_snapshots_falls_back_to_db` | Redis empty. GET returns DB version with `"source": "db"`. |
| D9 | `test_rest_ohlcv_reads_redis_first` | Redis has bars, DB has bars. GET returns Redis bars. |
| D10 | `test_rest_ohlcv_falls_back_to_db` | Redis empty. GET returns DB bars. |
| D11 | `test_rest_single_snapshot_404` | Non-existent asset_id → 404 |
| D12 | `test_rest_ohlcv_invalid_timeframe` | `timeframe=15m` → 422 |

### Integration Tests — WS Delivery Consistency

| # | Test | Validates |
|---|------|-----------|
| W1 | `test_redis_publish_on_tier0` | Tier 0 cycle → `nexus:events:ticker` receives message with price fields |
| W2 | `test_redis_publish_on_tier1` | Tier 1 cycle → `nexus:events:ticker` message includes change_1h/4h |
| W3 | `test_ws_payload_shape` | Published payload has `type`, `tier`, `exchange_id`, `assets[]`, `timestamp` |
| W4 | `test_ws_and_rest_consistency` | After Tier 1 cycle, WS payload snapshot matches GET /snapshots response snapshot |

### Integration Tests — Staged Warm-Up

| # | Test | Validates |
|---|------|-----------|
| S1 | `test_stage0_price_only` | New asset after Tier 0: price/bid/ask populated, change_* all null |
| S2 | `test_stage1_1h_metrics` | After Tier 1: change_1h/4h, volume_1h/4h populated. change_12h/24h/7d still null. |
| S3 | `test_stage2_full_metrics` | After Tier 2: all 16 fields populated, no nulls |
| S4 | `test_backfill_fetches_168_1h_bars` | CCXT mock called with `limit=168` for 1H on new asset (not 4) |
| S5 | `test_backfill_fetches_30_1d_bars` | CCXT mock called with `limit=30` for 1D on new asset (not 2) |
| S6 | `test_disabled_asset_excluded` | `is_tradable=false` → not in Tier 0 fetch_tickers call |

### Integration Tests — Error Handling

| # | Test | Validates |
|---|------|-----------|
| E1 | `test_ccxt_network_error_no_crash` | CCXT raises `NetworkError` → MDS logs warning, cycle completes, no snapshot written |
| E2 | `test_no_active_exchange_idle` | No active exchange in DB → MDS idles without error |
| E3 | `test_redis_write_failure_db_still_persists` | Redis SET raises → DB write still attempted and succeeds |

### Static Analysis / Grep Proofs

| # | Test | Method |
|---|------|--------|
| G1 | `test_grep_no_ticker_percentage_in_mds` | `subprocess.run(["grep", "-rn", "percentage", "app/services/market_data.py"])` returns empty |
| G2 | `test_grep_no_ticker_baseVolume_in_mds` | `subprocess.run(["grep", "-rn", "baseVolume", "app/services/market_data.py"])` returns empty |
| G3 | `test_grep_no_frontend_metric_computation` | `subprocess.run(["grep", "-rn", "change_1h\|change_4h\|volume_1h", "web/frontend/src/"])` returns empty (no frontend files compute these) |

### No-Mock-Data Enforcement

All integration tests use either:
- pgserver (real PostgreSQL 16 via Unix socket)
- fakeredis (faithful Redis implementation with sorted set + pub/sub + TTL support)
- CCXT mock returning realistic but deterministic data (fixed timestamps, known prices)

No hardcoded fake data is injected into the DB or Redis that would bypass MDS computation. Tests seed raw OHLCV bars and verify that MDS computes correct derived metrics.

### Manual API Proof (post-implementation)

8-step proof script against real pgserver + fakeredis + mocked CCXT:

1. Seed exchange + 3 tradable assets (BTC, ETH, SOL).
2. Seed 168 1H + 30 1D OHLCV bars per asset into Redis (simulating MDS warm-up).
3. Trigger MDS snapshot computation.
4. `GET /market-data/snapshots` → 3 snapshots, all 16 fields non-null, `source: "redis"`.
5. Verify BTC `change_24h` matches hand-calculated formula from seeded bar closes.
6. Verify ETH `volume_24h` matches sum of 24 seeded 1H candle volumes.
7. Verify `source_candle_ts` is the most recent closed 1H bar (not current open).
8. Clear Redis entirely → `GET /market-data/snapshots` → 3 snapshots with `source: "db"`.

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
| F4 | All 16 snapshot fields computed correctly | Tests U1–U16 all passing |
| F5 | Snapshot written to Redis hot cache with 5min TTL | Tests R3, R4, R5 passing |
| F6 | Snapshot persisted to `Asset.market_snapshot` JSONB on Tier 2 | Test D3 passing |
| F7 | Snapshot published to `nexus:events:ticker` on every tier | Tests W1, W2 passing |
| F8 | REST endpoints read Redis first, fall back to DB | Tests D7, D8, D9, D10 passing |
| F9 | Staged warm-up: price within 15s, 1H metrics within 75s, full within 6min | Tests S1, S2, S3 passing |
| F10 | OHLCV retention pruning works for both 1H (7d) and 1D (30d) | Tests D5, D6 passing |
| F11 | `ticker.percentage` is NEVER used in metric computation | Tests U17, G1 passing |
| F12 | `ticker.baseVolume` is NEVER used in metric computation | Tests U18, G2 passing |
| F13 | Frontend does NOT compute any metrics | Test G3 passing (grep proof) |
| F14 | `source_candle_ts` correctly identifies the anchor bar | Tests U15, U16, manual proof step 7 passing |
| F15 | CCXT failure does not crash MDS | Test E1 passing |
| F16 | WS payload matches REST snapshot for same asset at same time | Test W4 passing |

### Non-Regression Criteria

| # | Criterion | Evidence |
|---|-----------|----------|
| NR1 | Phase 1 tests pass unchanged | `pytest tests/test_phase1_asset_management.py` — 21 passed, 0 failed |
| NR2 | Phase 6 logging canary passes | `pytest tests/test_phase6_logging.py::TestAuditMiddleware::test_normal_request_produces_audit_log` — PASS |
| NR3 | Full suite: no NEW failures | `pytest tests/ -m "not docker_integration"` — ≤1 failure (pre-existing `test_phase8g` only) |
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
| `nexus:ohlcv:1h:{asset_id}` | Sorted Set | 1H bars, score=ts_ms, value=JSON `[ts,o,h,l,c,v]` | 48h (renewed on write) | MDS Tier 1 | MDS metric computation, REST /ohlcv endpoint |
| `nexus:ohlcv:1d:{asset_id}` | Sorted Set | 1D bars, score=ts_ms, value=JSON `[ts,o,h,l,c,v]` | 7d (renewed on write) | MDS Tier 2 | MDS metric computation, REST /ohlcv endpoint |
| `nexus:snapshot:{asset_id}` | String | Full 16-field snapshot JSON | 5min (renewed on write) | MDS Tier 0/1/2 | REST /snapshots/{id} endpoint |
| `nexus:snapshot:all:{exchange_id}` | String | JSON dict `{asset_id: snapshot}` | 5min (renewed on write) | MDS Tier 1/2 | REST /snapshots endpoint |
| `nexus:events:ticker` | Pub/Sub | Snapshot update payloads (not stored) | N/A (pub/sub) | MDS Tier 0/1/2 | WS ConnectionManager bridge |

## Appendix B — Configuration Constants

| Constant | Value | Location | Notes |
|----------|-------|----------|-------|
| `TIER0_INTERVAL_S` | 15 | `market_data.py` | Price-only cycle |
| `TIER1_INTERVAL_S` | 60 | `market_data.py` | 1H OHLCV + short-horizon metrics |
| `TIER2_INTERVAL_S` | 300 | `market_data.py` | 1D OHLCV + deep metrics + DB persist |
| `OHLCV_1H_MAX_BARS` | 168 | `market_data.py` | 7 days of 1H in Redis |
| `OHLCV_1D_MAX_BARS` | 30 | `market_data.py` | 30 days of 1D in Redis |
| `OHLCV_1H_FETCH_LIMIT` | 4 | `market_data.py` | Incremental: 3 closed + 1 open |
| `OHLCV_1D_FETCH_LIMIT` | 2 | `market_data.py` | Incremental: 1 closed + 1 open |
| `WARMUP_1H_BARS` | 168 | `market_data.py` | Full backfill for new assets |
| `WARMUP_1D_BARS` | 30 | `market_data.py` | Full backfill for new assets |
| `SNAPSHOT_TTL_S` | 300 | `market_data.py` | 5 minutes |
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
              ├── writes: Redis hot cache (new — nexus:ohlcv:*, nexus:snapshot:*)
              ├── writes: Asset.market_snapshot, Asset.snapshot_updated_at (Phase 1 columns, Phase 2 owned)
              ├── writes: ohlcv table (Phase 0 schema)
              ├── publishes: nexus:events:ticker (Phase 0 WS channel)
              └── adds: REST endpoints, MDS service, Alembic index migration
```
