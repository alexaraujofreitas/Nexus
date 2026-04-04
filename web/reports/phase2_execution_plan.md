# Phase 2 Execution Plan — Market Data Service + Snapshot Pipeline

**Status:** PENDING APPROVAL — do NOT implement until explicitly authorized.
**Date:** 2026-04-03
**Depends on:** Phase 1 (Asset Management) — ACCEPTED
**Constraint:** No scanner changes, no engine changes, no config.yaml changes, no frontend computation, no SQLite authority, no fake data. Phase 1 contracts preserved.

---

## Section 1 — Exact Files to Create or Modify

### New files

| File | Purpose |
|------|---------|
| `web/backend/app/services/market_data.py` | MarketDataService class — singleton async service that fetches tickers + OHLCV from the engine, computes metrics, writes snapshots to PostgreSQL, and publishes to Redis for WebSocket delivery |
| `web/backend/app/api/market_data.py` | REST router: `GET /api/v1/market-data/snapshots`, `GET /api/v1/market-data/snapshots/{asset_id}`, `GET /api/v1/market-data/ohlcv/{asset_id}` |
| `web/backend/alembic/versions/<hash>_add_ohlcv_retention_index.py` | Migration: adds `idx_ohlcv_timestamp` (for retention pruning) and `idx_ohlcv_asset_tf_latest` (descending timestamp, for "latest N bars" queries) |
| `web/backend/tests/test_phase2_market_data.py` | Full test suite: unit tests for metric computation, pgserver integration tests for snapshot persistence, OHLCV upsert, retention pruning, and REST endpoint responses |
| `web/backend/app/services/__init__.py` | Already exists (39 bytes); no modification needed |

### Modified files

| File | Change |
|------|--------|
| `web/backend/main.py` | Add `lifespan` context manager (or extend existing one) to start/stop MDS background task; register `market_data` router |
| `web/backend/app/api/__init__.py` | Import and include `market_data.router` |
| `web/backend/app/models/trading.py` | No schema changes — Asset.market_snapshot (JSONB) and Asset.snapshot_updated_at already exist from Phase 1. OHLCV table already exists in initial schema. No new columns. |
| `web/backend/app/ws/manager.py` | No changes — `"ticker"` channel already in CHANNELS set. MDS publishes to Redis `nexus:events:ticker`; the existing bridge picks it up. |
| `web/backend/app/api/engine.py` | Add two new engine command wrappers: `fetch_tickers_command(symbols)` and `fetch_ohlcv_command(symbol, timeframe, limit)` — thin wrappers around existing `_send_engine_command`. |
| `web/engine/main.py` | **NOT MODIFIED** — but depends on existing `exchange.sync_assets` and `get_ohlcv` handlers. If ticker command doesn't exist yet, MDS will use a new engine command `market_data.fetch_tickers` that must be added to engine. **Decision point: if we must not touch engine, MDS calls CCXT directly via the exchange credentials in DB. See Section 2 for the chosen architecture.** |

### Files NOT touched (scope boundary)

- `core/scanning/scanner.py` — no scanner changes
- `core/market_data/exchange_manager.py` — desktop-only singleton; web MDS uses its own CCXT instance
- `config.yaml` — no runtime config changes
- Any frontend files — Phase 2 is backend-only; frontend consumes via existing WS `ticker` channel + new REST endpoints
- `web/backend/app/models/trading.py` schema — no new columns; existing OHLCV + Asset.market_snapshot suffice

---

## Section 2 — MarketDataService Architecture

### Design Decision: Engine-Delegated vs Direct CCXT

**Option A — Engine-delegated:** MDS sends Redis commands to engine, engine calls CCXT, replies with data. Pros: single CCXT instance, rate-limit parity with desktop. Cons: engine must have command handlers for tickers (currently missing for batch tickers); adds Redis round-trip latency; engine becomes a bottleneck.

**Option B — Direct CCXT (CHOSEN):** MDS creates its own read-only CCXT instance using exchange credentials from PostgreSQL. Pros: no engine changes, no Redis command latency, independent rate-limit budget, MDS can run even if engine is down. Cons: second CCXT instance (acceptable — read-only, `enableRateLimit: True`).

**Rationale:** The user's constraint is "no engine changes." The engine's command handler registry (`web/engine/main.py` lines 311–355) has no `market_data.fetch_tickers` command. Adding one violates the constraint. Direct CCXT is the only option that stays in scope.

### Class Design

```
MarketDataService (singleton)
├── __init__(db_session_factory, redis, settings)
├── _ccxt_exchange: ccxt.Exchange  (lazy-init from DB exchange credentials)
├── _running: bool
├── _task: asyncio.Task
│
├── start() → creates asyncio.Task(_run_loop)
├── stop() → cancels task, awaits cleanup
│
├── _run_loop()
│   └── while _running:
│       ├── _fetch_and_store_tickers()   # every 60s (configurable)
│       ├── _fetch_and_store_ohlcv()     # every 60s, staggered by 30s
│       ├── _compute_and_write_snapshots()
│       ├── _publish_to_redis()
│       ├── _prune_old_ohlcv()           # every 6h
│       └── await asyncio.sleep(remaining_interval)
│
├── _fetch_and_store_tickers(symbols: list[str]) → dict[str, TickerData]
├── _fetch_and_store_ohlcv(assets: list[Asset]) → None
├── _compute_snapshot(asset_id, ticker, ohlcv_rows) → dict  # JSONB payload
├── _publish_to_redis(snapshots: dict) → None
├── _prune_old_ohlcv(retention_hours: int = 168) → int  # returns rows deleted
│
└── get_exchange() → ccxt.Exchange  # lazy init
```

### Lifecycle

1. `main.py` `lifespan` context manager creates `MarketDataService` instance.
2. On `yield` (startup complete), calls `mds.start()`.
3. `start()` queries DB for active exchange, initializes CCXT instance (demo mode, `enableRateLimit: True`, 15s timeout), then spawns `_run_loop` as `asyncio.Task`.
4. On shutdown, `lifespan` calls `mds.stop()`, which cancels the task and closes the CCXT exchange.

### CCXT Instance Initialization

```
1. Query: SELECT * FROM exchanges WHERE is_active = true LIMIT 1
2. If exchange.demo_mode: set 'urls.api' overrides for Bybit demo
3. Config: enableRateLimit=True, timeout=15000, rateLimit=350 (ms between calls)
4. API key: decrypt from exchange.api_key_encrypted / api_secret_encrypted using vault service
5. Call load_markets() once at init
```

The MDS CCXT instance is **read-only** — it calls `fetch_tickers` and `fetch_ohlcv` only. No order placement, no balance queries.

### Error Handling

- CCXT `NetworkError` / `ExchangeNotAvailable`: log warning, skip cycle, retry next interval. No crash.
- CCXT `RateLimitExceeded`: back off 2× for one cycle, then restore.
- DB write failure: log error, skip snapshot write, do NOT cache stale data.
- Redis publish failure: log warning, skip WS delivery, DB snapshot is still written (DB is authority).
- Exchange not configured / no active exchange: MDS enters idle mode, checks every 60s for an active exchange.

---

## Section 3 — OHLCV Cache Design

### Storage

The OHLCV table already exists (Phase 0 initial schema):

```
ohlcv(id PK, asset_id FK→assets, timeframe VARCHAR(5), timestamp DATETIME,
      open FLOAT, high FLOAT, low FLOAT, close FLOAT, volume FLOAT)
UNIQUE(asset_id, timeframe, timestamp)
INDEX(asset_id, timeframe, timestamp)
```

### Timeframes Stored

| Timeframe | Bars fetched per cycle | Bars retained | Retention period | Purpose |
|-----------|----------------------|---------------|-----------------|---------|
| `1h` | 25 (latest) | 168 (7 days) | 7 days | change_1h, change_4h, change_12h, change_24h, volume_1h, volume_4h, volume_24h |
| `1d` | 8 (latest) | 30 (30 days) | 30 days | change_7d |

Two timeframes cover all required metric windows. No 15m/30m/4h needed — 1h granularity is sufficient for hourly/4h/12h/24h metrics via bar math; 1d covers weekly.

### Upsert Strategy

`INSERT ... ON CONFLICT (asset_id, timeframe, timestamp) DO UPDATE SET open=EXCLUDED.open, high=EXCLUDED.high, low=EXCLUDED.low, close=EXCLUDED.close, volume=EXCLUDED.volume`

This handles late-arriving candle updates (exchange may revise the current incomplete candle) and is idempotent on replay.

### New Indexes (Migration)

1. `idx_ohlcv_asset_tf_latest`: `(asset_id, timeframe, timestamp DESC)` — optimizes "latest N bars" queries used by metric computation. The existing ascending index `idx_ohlcv_asset_tf_ts` is retained for range scans.
2. `idx_ohlcv_timestamp`: `(timestamp)` — used by retention pruning (`DELETE WHERE timestamp < now() - interval`).

### Retention Pruning

Every 6 hours, MDS executes:

```sql
DELETE FROM ohlcv
WHERE (timeframe = '1h' AND timestamp < now() - interval '7 days')
   OR (timeframe = '1d' AND timestamp < now() - interval '30 days');
```

Returns row count for logging. Runs inside a single transaction. Expected to delete ~5–25 rows per cycle (5 assets × 1 bar/hour × ~6 hours between prunes for 1h TF).

### Fetch Sequencing

Per cycle (every 60 seconds):

1. Query DB for all tradable assets (`is_tradable = true`) on the active exchange.
2. For each asset, `fetch_ohlcv(symbol, "1h", limit=25)` — 25 bars covers 24h of closed candles + 1 open candle.
3. For each asset, `fetch_ohlcv(symbol, "1d", limit=8)` — 8 bars covers 7 full days + today's open candle.
4. Batch upsert all fetched bars.
5. Proceed to metric computation.

CCXT calls are sequential per-symbol (CCXT `enableRateLimit` enforces inter-call delay). Cross-symbol calls are parallelized with `asyncio.gather` up to a concurrency limit of 3.

---

## Section 4 — Metric Computation Contract

All metrics are computed **server-side** from OHLCV rows stored in PostgreSQL. The frontend receives pre-computed values in the `market_snapshot` JSONB column and via the `ticker` WebSocket channel. The frontend does NO computation — it renders numbers as-is.

### Snapshot JSONB Schema

```json
{
  "price": 84231.50,
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

### Exact Formulas

All percentage changes are computed from OHLCV **close** prices. "Current price" is the `last` price from the ticker fetch, falling back to the most recent 1h candle close.

#### Price Changes (percentage, 2 decimal places)

```
change_Nh = ((current_price - close_N_hours_ago) / close_N_hours_ago) * 100
```

Where `close_N_hours_ago` is the close of the 1h candle whose timestamp is closest to `now - N hours`, looked up by:

```sql
SELECT close FROM ohlcv
WHERE asset_id = ? AND timeframe = '1h'
  AND timestamp <= now() - interval 'N hours'
ORDER BY timestamp DESC
LIMIT 1;
```

| Metric | N | Source TF | Fallback if insufficient bars |
|--------|---|-----------|-------------------------------|
| `change_1h` | 1 | 1h | `null` |
| `change_4h` | 4 | 1h | `null` |
| `change_12h` | 12 | 1h | `null` |
| `change_24h` | 24 | 1h | `null` |
| `change_7d` | 7d | 1d | `null` |

For `change_7d`:

```sql
SELECT close FROM ohlcv
WHERE asset_id = ? AND timeframe = '1d'
  AND timestamp <= now() - interval '7 days'
ORDER BY timestamp DESC
LIMIT 1;
```

**Division by zero guard:** If `close_N_hours_ago` is 0 or NULL, the metric is `null`.

#### Volume Aggregations (absolute USD, 0 decimal places)

Volume is summed over the N most recent **closed** 1h candles (excluding the current incomplete candle).

```
volume_Nh = SUM(volume) of the N most recent closed 1h candles
```

```sql
SELECT COALESCE(SUM(volume), 0) FROM ohlcv
WHERE asset_id = ? AND timeframe = '1h'
  AND timestamp >= now() - interval 'N hours'
  AND timestamp < date_trunc('hour', now());  -- exclude current incomplete candle
```

| Metric | N | Notes |
|--------|---|-------|
| `volume_1h` | 1 | Last fully closed 1h candle's volume |
| `volume_4h` | 4 | Sum of last 4 closed 1h candles |
| `volume_24h` | 24 | Sum of last 24 closed 1h candles |

**Edge case:** `volume_1h` is technically the single last closed candle's volume, not a sum. The SQL still works (SUM of 1 row = that row's value).

#### 24h High/Low

```sql
SELECT MAX(high), MIN(low) FROM ohlcv
WHERE asset_id = ? AND timeframe = '1h'
  AND timestamp >= now() - interval '24 hours';
```

Includes the current incomplete candle (it contributes to the day's high/low).

#### Spread

```
spread_pct = ((ask - bid) / ((ask + bid) / 2)) * 100
```

`bid` and `ask` come from the CCXT ticker response (`ticker['bid']`, `ticker['ask']`). If either is null or zero, `spread_pct` is `null`.

#### Timestamps

- `last_trade_ts`: From CCXT ticker `ticker['datetime']` (ISO 8601). Null if unavailable.
- `computed_at`: `datetime.utcnow().isoformat() + "Z"` at the moment the snapshot dict is assembled.

### Null Handling

Any metric that cannot be computed (insufficient OHLCV data, missing ticker fields) is set to `null` in the JSONB. The frontend must handle null gracefully (display "—" or equivalent). This is critical for newly-enabled assets that have no historical data yet (see Section 8).

---

## Section 5 — Update Frequency and Rate-Limit Budget

### CCXT Rate Limits (Bybit)

Bybit public API rate limit: 120 requests per 5 seconds = 24 req/s. With `enableRateLimit: True` and `rateLimit: 350` (ms), CCXT enforces a minimum 350ms gap between calls.

### Per-Cycle API Call Budget

Each MDS cycle makes these CCXT calls:

| Call | Per symbol | Count | Notes |
|------|-----------|-------|-------|
| `fetch_tickers(symbols)` | 1 batch call | 1 | Single call fetches all symbols at once |
| `fetch_ohlcv(symbol, "1h", limit=25)` | 1 per symbol | S | Sequential with rate limit |
| `fetch_ohlcv(symbol, "1d", limit=8)` | 1 per symbol | S | Sequential with rate limit |

**Total calls per cycle = 1 + 2S** where S = number of tradable symbols.

### Scaling Table

| Tradable symbols (S) | API calls/cycle | Time per cycle (at 350ms spacing) | Cycle interval | Calls/minute | Bybit budget used |
|----------------------|----------------|-----------------------------------|----------------|-------------|-------------------|
| 5 (current watchlist) | 11 | ~3.9s | 60s | 11 | 0.8% |
| 10 | 21 | ~7.4s | 60s | 21 | 1.5% |
| 25 | 51 | ~17.9s | 60s | 51 | 3.5% |
| 50 | 101 | ~35.4s | 60s | 101 | 7.0% |
| 100 | 201 | ~70.4s | 90s (auto-adjusted) | 134 | 9.3% |

### Adaptive Interval

The MDS monitors cycle duration. If a cycle takes longer than 80% of the target interval (48s at 60s target), the interval is automatically increased to `cycle_duration * 1.25` (rounded to nearest 15s). This prevents overlapping cycles. A log warning is emitted when this triggers.

For the current 5-symbol watchlist, the cycle completes in ~4 seconds — well within the 60s budget.

### Concurrency

- `fetch_tickers` is a single batch call (Bybit returns all requested symbols in one response).
- `fetch_ohlcv` calls are parallelized with `asyncio.Semaphore(3)` — max 3 concurrent OHLCV fetches. CCXT's internal rate limiter serializes the actual HTTP calls, but the semaphore controls asyncio task fan-out to avoid creating hundreds of pending tasks at scale.

### Engine Interaction Budget

MDS does NOT use Redis engine commands for data fetching (see Section 2 — Direct CCXT). Zero engine command budget consumed. The desktop engine's own CCXT rate-limit budget is unaffected.

---

## Section 6 — Snapshot Authority and Delivery Model

### Authority Hierarchy

```
CCXT (exchange) → MDS (compute) → PostgreSQL (authority) → Redis (delivery) → WebSocket (frontend)
```

1. **CCXT** is the raw data source. MDS trusts CCXT ticker and OHLCV responses.
2. **MDS** computes all metrics. No other component computes price changes or volume aggregations.
3. **PostgreSQL** is the single authority for snapshot state. `Asset.market_snapshot` (JSONB) and `Asset.snapshot_updated_at` are the canonical snapshot. If Redis fails, the DB snapshot is still correct and queryable via REST.
4. **Redis** is the delivery bus, not an authority. It carries ephemeral snapshots for real-time WS delivery. Redis data is never read back as authoritative — it's fire-and-forget pub/sub.
5. **WebSocket** is the delivery channel to the frontend. Subscribers on the `ticker` channel receive snapshots as they're published. No computation on the frontend.

### Write Path

```
MDS._compute_and_write_snapshots():
  1. Compute snapshot dict for each tradable asset (Section 4 formulas)
  2. UPDATE assets SET market_snapshot = ?, snapshot_updated_at = now() WHERE id = ?
     (one UPDATE per asset, batched in a single transaction)
  3. PUBLISH nexus:events:ticker {asset_id, symbol, snapshot}
     (one Redis PUBLISH per asset, fire-and-forget)
```

### Read Paths

| Consumer | Path | Freshness |
|----------|------|-----------|
| Frontend (real-time) | WS `ticker` channel ← Redis `nexus:events:ticker` | ~60s (MDS cycle interval) |
| Frontend (on-load) | `GET /api/v1/market-data/snapshots` ← PostgreSQL `Asset.market_snapshot` | Last successful MDS write |
| Frontend (single asset) | `GET /api/v1/market-data/snapshots/{asset_id}` ← PostgreSQL | Last successful MDS write |
| Frontend (chart) | `GET /api/v1/market-data/ohlcv/{asset_id}?timeframe=1h&limit=300` ← PostgreSQL `ohlcv` table | Last successful OHLCV fetch |

### Staleness Detection

Every snapshot includes `computed_at` (ISO 8601). The frontend can compare `computed_at` to its local clock to detect staleness. If `computed_at` is older than 5 minutes, the frontend should display a "data may be stale" indicator. This is a frontend concern — the backend provides the timestamp; the frontend decides how to render it.

### Phase 1 Contract Preservation

- `Asset.market_snapshot` is written by MDS only. The sync-assets endpoint (`POST /{id}/sync-assets`) already preserves this column on upsert — it is NOT in the `on_conflict_do_update.set_` dict.
- `Asset.is_tradable` and `Asset.allocation_weight` are Phase 1-owned. MDS reads `is_tradable` to determine which assets to fetch data for, but never writes to it.
- The `PATCH /{id}/assets/{asset_id}` and `PATCH /{id}/assets/bulk` endpoints do not touch `market_snapshot` or `snapshot_updated_at`.

---

## Section 7 — API and Schema Changes

### New REST Endpoints

All endpoints require authentication (`Depends(get_current_user)`).

#### `GET /api/v1/market-data/snapshots`

Returns the latest snapshot for all tradable assets on the active exchange.

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
      "snapshot_updated_at": "2026-04-03T14:30:05Z"
    }
  ],
  "count": 5,
  "exchange_id": 1
}
```

**SQL:**
```sql
SELECT id, symbol, base_currency, is_tradable, allocation_weight,
       market_snapshot, snapshot_updated_at
FROM assets
WHERE exchange_id = ? AND is_tradable = true
ORDER BY symbol;
```

#### `GET /api/v1/market-data/snapshots/{asset_id}`

Returns the snapshot for a single asset. Returns 404 if asset not found or not on the active exchange.

**Response (200):** Single object from the `snapshots` array above (not wrapped in array).

#### `GET /api/v1/market-data/ohlcv/{asset_id}`

Returns OHLCV bars from PostgreSQL cache.

**Query params:**
- `timeframe`: `"1h"` or `"1d"` (default `"1h"`)
- `limit`: 10–500 (default 168 for 1h = 7 days, default 30 for 1d)

**Response (200):**

```json
{
  "asset_id": 1,
  "symbol": "BTC/USDT:USDT",
  "timeframe": "1h",
  "bars": [
    {"timestamp": "2026-04-03T13:00:00Z", "open": 84100.0, "high": 84350.0, "low": 84050.0, "close": 84231.5, "volume": 12450000.0}
  ],
  "count": 168
}
```

**SQL:**
```sql
SELECT timestamp, open, high, low, close, volume
FROM ohlcv
WHERE asset_id = ? AND timeframe = ?
ORDER BY timestamp DESC
LIMIT ?;
```

Results returned in descending order (most recent first) for efficient "latest N" access.

### WebSocket Channel

**Channel:** `ticker` (already registered in `CHANNELS` set in `ws/manager.py`)

**Redis publication:** `PUBLISH nexus:events:ticker <json_payload>`

**Payload shape:**

```json
{
  "type": "snapshot_update",
  "exchange_id": 1,
  "assets": [
    {
      "asset_id": 1,
      "symbol": "BTC/USDT:USDT",
      "snapshot": { ... }
    }
  ],
  "timestamp": "2026-04-03T14:30:05Z"
}
```

The existing Redis bridge in `ConnectionManager` (`psubscribe("nexus:events:*")`) already routes `nexus:events:ticker` to WS subscribers on the `ticker` channel. No changes to `manager.py` needed.

### Schema Changes

**No new tables. No new columns.** The OHLCV table and Asset.market_snapshot/snapshot_updated_at already exist. The only migration adds two indexes:

```python
# New migration: add_ohlcv_retention_index
def upgrade():
    op.create_index("idx_ohlcv_timestamp", "ohlcv", ["timestamp"])
    op.create_index("idx_ohlcv_asset_tf_latest", "ohlcv",
                    ["asset_id", "timeframe", sa.text("timestamp DESC")])

def downgrade():
    op.drop_index("idx_ohlcv_asset_tf_latest")
    op.drop_index("idx_ohlcv_timestamp")
```

---

## Section 8 — Newly-Enabled Asset Warm-Up Flow

When a user marks an asset as tradable via Phase 1's `PATCH /{id}/assets/{asset_id}` with `is_tradable: true`, MDS must populate its OHLCV cache and snapshot before the next regular cycle. Otherwise the asset appears in the tradable list with `market_snapshot: null`.

### Trigger Mechanism

**Option A — Polling (CHOSEN):** Each MDS cycle queries the current tradable asset set. If a new asset_id appears that has no OHLCV rows, MDS executes immediate warm-up for that asset within the current cycle, before computing snapshots.

**Why not event-driven:** An event-driven approach (Phase 1 PATCH publishes to Redis, MDS subscribes) would require modifying the Phase 1 endpoint to publish an event — violating the "do not modify Phase 1" constraint. Polling is simpler, has at-most 60s latency, and requires zero Phase 1 changes.

### Warm-Up Sequence

```
1. MDS cycle starts
2. Query: SELECT id, symbol FROM assets WHERE exchange_id = ? AND is_tradable = true
3. Compare against MDS._known_asset_ids (in-memory set)
4. For each new asset_id not in _known_asset_ids:
   a. fetch_ohlcv(symbol, "1h", limit=168)   # 7 days of hourly data
   b. fetch_ohlcv(symbol, "1d", limit=30)     # 30 days of daily data
   c. Batch upsert all bars into ohlcv table
   d. Add asset_id to _known_asset_ids
5. Proceed to normal ticker fetch + metric computation for ALL tradable assets
```

### Warm-Up Data Volume

Per newly-enabled asset: 168 + 30 = 198 bars upserted. Two CCXT calls. At 350ms spacing: ~0.7s per asset. Negligible impact on cycle time.

### Metric Availability After Warm-Up

After warm-up, the asset has 7 days of 1h data and 30 days of 1d data. All metrics are computable immediately:

| Metric | Available after warm-up? |
|--------|------------------------|
| `change_1h` through `change_24h` | Yes — 168 hourly bars covers 7 days |
| `change_7d` | Yes — 30 daily bars covers 30 days |
| `volume_1h/4h/24h` | Yes — 168 hourly bars |
| `high_24h`, `low_24h` | Yes — 24 hourly bars |
| `bid`, `ask`, `spread_pct` | Yes — from ticker (fetched in same cycle) |

No null metrics for warm-up assets, assuming the exchange has trading history for the symbol.

### Disabled Asset Cleanup

When an asset is set to `is_tradable: false`, MDS removes it from `_known_asset_ids` on the next cycle. Its OHLCV data is NOT deleted — retention pruning handles cleanup on the normal 7d/30d schedule. The `market_snapshot` JSONB is NOT cleared — it retains the last-known values, which is useful if the user re-enables the asset. `snapshot_updated_at` naturally shows the staleness.

---

## Section 9 — Test Plan

### Test File Structure

`web/backend/tests/test_phase2_market_data.py`

### Marker Taxonomy

- `@pytest.mark.unit` — Pure computation tests, no DB, no CCXT. Mocked inputs.
- `@pytest.mark.skipif(SKIP_PG)` — pgserver integration tests (same pattern as Phase 1). Real PostgreSQL, real Alembic migrations, fakeredis.

### Unit Tests (no DB, mocked OHLCV rows)

| # | Test | Validates |
|---|------|-----------|
| U1 | `test_change_1h_positive` | `change_1h` formula: 1h-ago close=100, current=105 → `change_1h=5.0` |
| U2 | `test_change_1h_negative` | `change_1h` formula: 1h-ago close=100, current=95 → `change_1h=-5.0` |
| U3 | `test_change_24h_computation` | `change_24h` from 24 1h candles |
| U4 | `test_change_7d_computation` | `change_7d` from daily candles |
| U5 | `test_volume_1h_single_candle` | `volume_1h` = single closed candle volume |
| U6 | `test_volume_4h_sum` | `volume_4h` = sum of 4 closed candles |
| U7 | `test_volume_24h_excludes_open_candle` | Current incomplete candle excluded from sum |
| U8 | `test_spread_pct_computation` | `spread_pct` formula with bid=100, ask=100.05 → `0.05%` |
| U9 | `test_spread_pct_null_on_zero_bid` | `spread_pct` is null when bid=0 |
| U10 | `test_change_null_on_insufficient_data` | Returns null when fewer bars than needed |
| U11 | `test_change_null_on_zero_denominator` | Division by zero → null |
| U12 | `test_high_low_24h` | `high_24h`/`low_24h` correct over 24 candles |
| U13 | `test_snapshot_schema_completeness` | All 15 keys present in computed snapshot |
| U14 | `test_computed_at_is_iso8601` | `computed_at` is valid ISO 8601 with Z suffix |

### Integration Tests (pgserver + fakeredis)

| # | Test | Validates |
|---|------|-----------|
| I1 | `test_ohlcv_upsert_insert` | Fresh bars inserted correctly |
| I2 | `test_ohlcv_upsert_update_on_conflict` | Duplicate (asset_id, tf, ts) updates OHLCV values |
| I3 | `test_snapshot_written_to_asset` | `Asset.market_snapshot` JSONB populated, `snapshot_updated_at` set |
| I4 | `test_snapshot_preserves_phase1_fields` | After snapshot write, `is_tradable` and `allocation_weight` unchanged |
| I5 | `test_retention_prune_deletes_old_1h` | 1h candles older than 7 days deleted |
| I6 | `test_retention_prune_preserves_recent_1h` | 1h candles within 7 days preserved |
| I7 | `test_retention_prune_deletes_old_1d` | 1d candles older than 30 days deleted |
| I8 | `test_warm_up_fetches_168_bars` | Newly-tradable asset triggers 168 1h + 30 1d bar fetch (CCXT mocked) |
| I9 | `test_warm_up_snapshot_non_null` | After warm-up, `market_snapshot` is not null, all metrics present |
| I10 | `test_disabled_asset_not_fetched` | `is_tradable=false` asset excluded from CCXT fetch cycle |
| I11 | `test_rest_snapshots_endpoint` | `GET /api/v1/market-data/snapshots` returns correct shape and data |
| I12 | `test_rest_single_snapshot_endpoint` | `GET /api/v1/market-data/snapshots/{id}` returns single asset |
| I13 | `test_rest_single_snapshot_404` | Non-existent asset_id returns 404 |
| I14 | `test_rest_ohlcv_endpoint` | `GET /api/v1/market-data/ohlcv/{id}?timeframe=1h&limit=24` returns correct bars |
| I15 | `test_rest_ohlcv_invalid_timeframe` | Unsupported timeframe returns 422 |
| I16 | `test_redis_publish_on_snapshot` | After snapshot write, Redis `nexus:events:ticker` receives published message |
| I17 | `test_ccxt_failure_no_crash` | Mocked CCXT raising `NetworkError` → MDS logs warning, cycle completes, no snapshot written |
| I18 | `test_no_active_exchange_idle` | No active exchange in DB → MDS idles without error |

### Manual API Proof (post-implementation)

Same pattern as Phase 1: standalone script with pgserver + mocked CCXT. Steps:

1. Seed exchange + 3 assets (BTC, ETH, SOL tradable)
2. Seed 168 1h OHLCV bars + 30 1d bars per asset
3. Trigger MDS snapshot computation
4. `GET /api/v1/market-data/snapshots` → 3 snapshots, all metrics non-null
5. Verify `change_24h` for BTC matches expected formula from seeded data
6. Verify `volume_24h` for ETH matches sum of 24 seeded candles
7. Mark SOL `is_tradable=false` via Phase 1 PATCH
8. Re-run MDS cycle → `GET /snapshots` returns 2 (BTC, ETH only)

### Regression Scope

After Phase 2 implementation, run:

```bash
# Full regression — all phases
pytest tests/ -m "not docker_integration" -v

# Phase 1 targeted
pytest tests/test_phase1_asset_management.py -v

# Phase 6 logging (canary for contamination)
pytest tests/test_phase6_logging.py -v
```

All existing tests must pass with zero regressions. The Phase 1 canary (`test_normal_request_produces_audit_log`) is the contamination sentinel — if it fails, the same `fileConfig(disable_existing_loggers=False)` pattern has been violated.

---

## Section 10 — Acceptance Criteria

Phase 2 is accepted when ALL of the following are demonstrated:

### Functional Criteria

| # | Criterion | Evidence required |
|---|-----------|-------------------|
| F1 | MDS starts automatically with the FastAPI app and stops on shutdown | Startup/shutdown logs in manual proof output |
| F2 | OHLCV bars are fetched from CCXT and stored in PostgreSQL | pgserver integration test I1 + I2 passing |
| F3 | All 15 snapshot metrics are computed correctly | Unit tests U1–U14 all passing |
| F4 | Snapshots are written to `Asset.market_snapshot` JSONB | Integration test I3 passing |
| F5 | Snapshots are published to Redis `nexus:events:ticker` | Integration test I16 passing |
| F6 | `GET /api/v1/market-data/snapshots` returns correct data | Integration test I11 passing + manual proof step 4 |
| F7 | `GET /api/v1/market-data/ohlcv/{id}` returns cached bars | Integration test I14 passing |
| F8 | Newly-enabled assets warm up within one MDS cycle | Integration test I8 + I9 passing |
| F9 | OHLCV retention pruning deletes bars beyond 7d/30d | Integration tests I5–I7 passing |
| F10 | CCXT failure does not crash MDS | Integration test I17 passing |

### Non-Regression Criteria

| # | Criterion | Evidence required |
|---|-----------|-------------------|
| NR1 | Phase 1 asset management tests pass | `pytest tests/test_phase1_asset_management.py` — 21 passed, 0 failed |
| NR2 | Phase 6 logging canary passes | `pytest tests/test_phase6_logging.py::TestAuditMiddleware::test_normal_request_produces_audit_log` — PASS |
| NR3 | Full test suite green | `pytest tests/ -m "not docker_integration"` — 0 failures |
| NR4 | Phase 1 contracts preserved | `is_tradable`, `allocation_weight` not modified by MDS (integration test I4) |
| NR5 | sync-assets still preserves `market_snapshot` | Verified by Phase 1 test suite (already tests ON CONFLICT exclusion) |

### Scoped Diff Criteria

| # | Criterion |
|---|-----------|
| SD1 | No changes to `core/scanning/scanner.py` |
| SD2 | No changes to `web/engine/main.py` |
| SD3 | No changes to `config.yaml` |
| SD4 | No changes to frontend files |
| SD5 | No modifications to Phase 1 endpoints or models (except adding router to main.py) |
| SD6 | `git diff --stat` shows only the files listed in Section 1 |

### Manual Proof Criteria

| # | Criterion |
|---|-----------|
| MP1 | 8-step manual API proof script runs against pgserver with all steps PASS |
| MP2 | Proof output included in remediation report |

---

## Appendix A — Dependency Graph

```
Phase 1 (Asset Management) ← ACCEPTED
    └── Phase 2 (Market Data Service + Snapshot Pipeline) ← THIS PLAN
         ├── reads: Asset.is_tradable (Phase 1)
         ├── reads: Asset.exchange_id, symbol, base_currency (Phase 0)
         ├── writes: Asset.market_snapshot, Asset.snapshot_updated_at (Phase 1 columns, Phase 2 owned)
         ├── writes: ohlcv table (Phase 0 schema)
         ├── publishes: nexus:events:ticker (Phase 0 WS channel)
         └── adds: REST endpoints, MDS service, Alembic index migration
```

## Appendix B — Configuration Constants (hardcoded, not config.yaml)

| Constant | Value | Location | Notes |
|----------|-------|----------|-------|
| `MDS_CYCLE_INTERVAL_S` | 60 | `market_data.py` | Adaptive: increases if cycle takes >48s |
| `MDS_OHLCV_1H_LIMIT` | 25 | `market_data.py` | Per-cycle fetch: 24 closed + 1 open |
| `MDS_OHLCV_1D_LIMIT` | 8 | `market_data.py` | Per-cycle fetch: 7 closed + 1 open |
| `MDS_WARMUP_1H_BARS` | 168 | `market_data.py` | 7 days of 1h data for new assets |
| `MDS_WARMUP_1D_BARS` | 30 | `market_data.py` | 30 days of 1d data for new assets |
| `MDS_RETENTION_1H_HOURS` | 168 | `market_data.py` | 7 days |
| `MDS_RETENTION_1D_DAYS` | 30 | `market_data.py` | 30 days |
| `MDS_PRUNE_INTERVAL_S` | 21600 | `market_data.py` | 6 hours between prune runs |
| `MDS_CCXT_CONCURRENCY` | 3 | `market_data.py` | Max concurrent OHLCV fetch tasks |
| `MDS_CCXT_RATE_LIMIT_MS` | 350 | `market_data.py` | Passed to CCXT `rateLimit` config |
