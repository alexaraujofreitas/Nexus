# Phase 2 — Design Document

## Overview

Phase 2 delivers a production-grade web trading platform with real API coverage, dual-layer security, data migration, and a professional React frontend. All work is strictly gated — no stubs, no placeholders, no partial APIs.

**Execution order (by priority):**
- 2A: API expansion (core operational endpoints)
- 2B: Cloudflare Zero Trust Access + security controls
- 2C: WebSocket/auth hardening
- 2D: SQLite → PostgreSQL migration tool + schema parity validation
- 2E: React frontend (Tailwind + shadcn/ui, auth UI, dashboard)

---

## Phase 2A — API Expansion

### Architecture: Engine Command Pattern

All data flows through the Engine process via Redis request/reply:

```
React UI → API endpoint → Redis RPUSH nexus:engine:commands
                            → Engine BLPOP → handler queries core/ components
                            → Redis RPUSH nexus:engine:replies:{id}
                            → API BLPOP reply → HTTP response
```

Real-time updates flow via Redis pub/sub → WebSocket:

```
Engine event → Redis PUBLISH nexus:events:{channel}
            → API WebSocket manager psubscribe → broadcast to clients
```

### New Engine Commands (12 additions to existing 10)

| # | Command | Handler Source | Return Shape |
|---|---------|---------------|-------------|
| 11 | `get_dashboard` | `_pe.get_production_status()` + `_pe.get_stats()` + crash_defense + trade_monitor | `{capital, pnl, drawdown, positions, crash_tier, recent_trades, win_rate, profit_factor}` |
| 12 | `get_crash_defense` | `CrashDefenseController` | `{tier, score, is_defensive, actions_log}` |
| 13 | `get_scanner_results` | Scanner last_scan_results cache | `{results: [{symbol, regime, score, side, ...}], cycle_metadata}` |
| 14 | `get_watchlist` | Scanner watchlist | `{symbols: [...], weights: {...}}` |
| 15 | `get_agent_status` | `AgentCoordinator.get_status()` | `{agents: {name: {running, signal, confidence, ...}}}` |
| 16 | `get_signals` | ConfluenceScorer last signals | `{signals: [{symbol, score, models_fired, ...}]}` |
| 17 | `get_risk_status` | RiskGate + PortfolioGuard state | `{portfolio_heat, drawdown, correlation_groups, position_limits}` |
| 18 | `get_trade_history` | Database query (paper_trades) | `{trades: [...], total, page, per_page}` |
| 19 | `update_config` | Settings write + persist | `{status: "ok", updated_keys: [...]}` |
| 20 | `get_system_health` | Exchange ping + DB check + thread count | `{exchange, database, threads, scanner, agents, uptime}` |
| 21 | `trigger_scan` | `scanner.scan_now()` | `{status: "ok", message: "Scan triggered"}` |
| 22 | `kill_switch` | `close_all()` + `pause_trading()` + `stop_scanner()` | `{positions_closed, trading_paused, scanner_stopped}` |

### New API Endpoints

**Dashboard Router** (`/api/v1/dashboard`):
- `GET /summary` → get_dashboard command
- `GET /crash-defense` → get_crash_defense command

**Scanner Router** (`/api/v1/scanner`):
- `GET /results` → get_scanner_results command
- `GET /watchlist` → get_watchlist command
- `POST /trigger` → trigger_scan command

**Signals Router** (`/api/v1/signals`):
- `GET /agents` → get_agent_status command
- `GET /confluence` → get_signals command

**Risk Router** (`/api/v1/risk`):
- `GET /status` → get_risk_status command

**Trades Router** (`/api/v1/trades`):
- `GET /history?page=1&per_page=50&symbol=&status=` → get_trade_history command

**System Router** (`/api/v1/system`):
- `GET /health` → get_system_health command (detailed, auth-required)
- `POST /kill-switch` → kill_switch command (emergency, auth-required)

**Settings Router** (`/api/v1/settings`):
- `GET /` → get_config command
- `PATCH /` → update_config command (body: `{key: value, ...}`)

### WebSocket Event Channels (additions)

| Channel | Trigger | Payload |
|---------|---------|---------|
| `dashboard` | Every 5s heartbeat + on trade/position change | `{capital, pnl, drawdown, open_count, crash_tier}` |
| `scanner` | On SCAN_CYCLE_COMPLETE | `{results: [...], timestamp}` |
| `signals` | On new signal generation | `{symbol, score, side, models_fired}` |
| `crash_defense` | On tier change | `{tier, score, actions}` |
| `risk` | On position open/close | `{portfolio_heat, drawdown, position_count}` |

### Engine-Side Event Publishing

The engine will publish to Redis pub/sub on these event bus topics:
- `TRADE_OPENED` → `nexus:events:positions` + `nexus:events:dashboard`
- `TRADE_CLOSED` → `nexus:events:trades` + `nexus:events:dashboard`
- `SCAN_CYCLE_COMPLETE` → `nexus:events:scanner`
- `DEFENSIVE_MODE_ACTIVATED` → `nexus:events:crash_defense`
- Dashboard heartbeat (5s timer) → `nexus:events:dashboard`

---

## Phase 2B — Cloudflare Zero Trust Access

### Dual-Layer Auth Architecture

```
Internet → Cloudflare Access (outer gate, CF JWT)
         → Cloudflare Tunnel (encrypted)
         → API Server (validates CF JWT + app JWT)
```

### Implementation

1. **CF JWT Validation Middleware** (`app/auth/cloudflare.py`):
   - Reads `CF-Access-JWT-Assertion` header
   - Fetches CF signing keys from `https://<team>.cloudflareaccess.com/cdn-cgi/access/certs`
   - Validates JWT (iss, aud, exp, nbf)
   - Caches signing keys (5min TTL)
   - Configurable: `NEXUS_CF_TEAM_DOMAIN`, `NEXUS_CF_AUDIENCE` env vars
   - Bypassable in dev mode: `NEXUS_CF_ENABLED=false`

2. **Middleware Stack** (order matters):
   ```python
   app.add_middleware(CloudflareAccessMiddleware)  # outer: CF JWT check
   # ... then CORS, rate limiting, etc.
   # ... then per-route app JWT check via Depends(get_current_user)
   ```

3. **Cloudflare Tunnel Config** (`cloudflared.yml`):
   ```yaml
   tunnel: <tunnel-id>
   credentials-file: /etc/cloudflared/<tunnel-id>.json
   ingress:
     - hostname: nexus.yourdomain.com
       service: http://localhost:8000
     - service: http_status:404
   ```

4. **Docker Compose Addition**: `cloudflared` service running `cloudflared tunnel run`.

5. **Health endpoint bypass**: `/health` and `/health/ready` exempt from CF JWT check (for uptime monitoring).

### Configuration

```env
NEXUS_CF_ENABLED=true
NEXUS_CF_TEAM_DOMAIN=yourteam.cloudflareaccess.com
NEXUS_CF_AUDIENCE=<application-audience-tag>
```

---

## Phase 2C — WebSocket/Auth Hardening

### Rate Limiting

Enforce rate limits already configured in Settings:
- Global: 100 req/min per IP
- Auth endpoints: 5 req/min per IP
- Engine commands: 10 req/min per user

Implementation: `slowapi` library with Redis backend for distributed rate limiting.

### Security Headers

Add via middleware:
- `X-Content-Type-Options: nosniff`
- `X-Frame-Options: DENY`
- `X-XSS-Protection: 1; mode=block`
- `Strict-Transport-Security: max-age=31536000; includeSubDomains`
- `Content-Security-Policy: default-src 'self'`
- `Referrer-Policy: strict-origin-when-cross-origin`

### WebSocket Auth Hardening

- Token expiry check on every message (not just connect)
- Connection timeout: 30min idle → disconnect
- Max connections per user: 5
- Heartbeat: server sends ping every 30s, client must pong within 10s

### Password Policy

- Minimum 8 characters
- Reject common passwords (top 10k list)
- bcrypt cost factor: 12 (current)

### Audit Logging

- Log all auth events (login, logout, token refresh, failed attempts)
- Log all engine commands (who, what, when)
- Log all config changes
- Store in `system_logs` table with structured JSON payload

---

## Phase 2D — SQLite → PostgreSQL Migration

### Schema Gap Fix (Pre-Migration)

Before building the migration tool, fix schema parity:

1. **Add missing PostgreSQL models** for 6 SQLite-only tables:
   - `signal_logs` (1,382 rows)
   - `trade_feedback` (7 rows)
   - `strategy_tuning_proposals` (0 rows)
   - `applied_strategy_changes` (2 rows)
   - `tuning_proposal_outcomes` (0 rows)
   - `trading_rules` (0 rows)

   Skip: `ai_conversations`, `ai_messages` (0 rows, not used in web)

2. **Add missing columns to `live_trades`**:
   - `trailing_stop_pct`, `max_hold_bars`, `bars_held`
   - `highest_price`, `lowest_price`
   - `_breakeven_applied`, `_auto_partial_applied`, `_initial_risk`

3. **Generate new Alembic migration** for schema additions.

### Migration Tool (`scripts/migrate_sqlite_to_pg.py`)

```
Usage: python scripts/migrate_sqlite_to_pg.py \
  --sqlite-path data/nexus_trader.db \
  --pg-url postgresql://nexus:nexus@localhost:5432/nexustrader \
  --dry-run | --execute
```

**Pipeline:**
1. Connect to SQLite (read-only)
2. Connect to PostgreSQL
3. For each table (topological order for FK satisfaction):
   a. Read all rows from SQLite
   b. Transform: JSON→JSONB, naive datetime→UTC-aware, NULL handling
   c. Batch INSERT into PostgreSQL (1000 rows/batch)
   d. Reset sequences (auto-increment) to max(id)+1
4. Produce validation report

**Type transformations:**
- `JSON` text → `JSONB` (parse + re-serialize)
- `datetime` string → `datetime(timezone=True)` (assume UTC if naive)
- `BOOLEAN` integer (0/1) → Python bool
- `REAL` → `float` (handle NaN → None)

**Safety:**
- `--dry-run` mode: reads SQLite, validates transforms, reports counts, writes nothing
- Original SQLite untouched (opened read-only)
- PostgreSQL transaction: entire migration in single transaction, rollback on any error
- Idempotent: TRUNCATE target tables before INSERT (within transaction)

### Validation Script (`scripts/validate_migration.py`)

Post-migration checks:
1. Row count comparison (every table)
2. Aggregate validation: SUM(pnl_usdt), COUNT(DISTINCT symbol), total trades
3. FK integrity: all foreign keys resolve
4. NULL constraint check: no unexpected NULLs in NOT NULL columns
5. JSONB validity: all JSONB columns parse successfully
6. Produce structured report (JSON + human-readable)

---

## Phase 2E — React Frontend

### Tech Stack

- **Vite** — build tool
- **React 18** — UI library
- **TypeScript** — type safety
- **Tailwind CSS** — utility-first styling
- **shadcn/ui** — component library
- **TanStack Query** — server state management
- **Zustand** — client state (auth, WebSocket)
- **React Router** — routing

### Project Structure

```
web/frontend/
├── package.json
├── vite.config.ts
├── tailwind.config.ts
├── tsconfig.json
├── index.html
├── src/
│   ├── main.tsx
│   ├── App.tsx
│   ├── api/                    # API client + hooks
│   │   ├── client.ts           # Axios instance with JWT interceptor
│   │   ├── auth.ts             # Auth API calls
│   │   ├── dashboard.ts        # Dashboard API calls
│   │   └── engine.ts           # Engine command API calls
│   ├── hooks/
│   │   ├── useWebSocket.ts     # WebSocket connection + reconnect
│   │   └── useAuth.ts          # Auth state + token refresh
│   ├── stores/
│   │   ├── authStore.ts        # Zustand: tokens, user
│   │   └── wsStore.ts          # Zustand: WS state, subscriptions
│   ├── components/
│   │   ├── ui/                 # shadcn/ui components
│   │   ├── layout/
│   │   │   ├── AppShell.tsx    # Main layout (sidebar + content)
│   │   │   ├── Sidebar.tsx     # Navigation
│   │   │   └── Header.tsx      # Top bar with status
│   │   └── shared/             # Reusable domain components
│   ├── pages/
│   │   ├── Login.tsx
│   │   ├── Setup.tsx
│   │   └── Dashboard.tsx
│   └── lib/
│       └── utils.ts
```

### Phase 2E Scope (Minimal Viable)

1. **Auth pages**: Login, Setup (first-run)
2. **Dashboard page**: Portfolio summary, crash defense indicator, recent trades, system status
3. **App shell**: Sidebar nav, header with engine status
4. **WebSocket integration**: Real-time dashboard updates

Defer to Phase 3: Scanner page, Trade History page, Settings page, Signals page, Risk page.

### Mobile-First Design

- Tailwind breakpoints: `sm:`, `md:`, `lg:`, `xl:`
- Dashboard cards stack vertically on mobile, grid on desktop
- Touch-friendly: min 44px tap targets
- Font sizes: 14px base, 12px dense data tables

---

## Test Strategy

### Unit Tests (per section)
- 2A: Test every new engine command handler (mock core components)
- 2A: Test every new API endpoint (mock Redis)
- 2B: Test CF JWT validation (valid, expired, wrong audience, missing header)
- 2C: Test rate limiting (exceed limit → 429)
- 2D: Test migration transforms (JSON→JSONB, datetime, booleans)
- 2E: Not in scope (frontend testing deferred)

### Integration Tests
- 2A: Full API→Redis→Engine roundtrip for new commands
- 2B: CF middleware + app JWT dual validation
- 2D: Run migration against test SQLite → live PG, validate

### Regression
- Full Phase 1 suite (104 tests) must still pass
- Plus all new Phase 2 tests

---

## Acceptance Criteria

| ID | Criterion | Evidence Required |
|----|-----------|-------------------|
| 2A-1 | Dashboard API returns real portfolio data | HTTP response with live capital, PnL, positions |
| 2A-2 | Scanner API returns real scan results | HTTP response with symbol scores and regimes |
| 2A-3 | Engine control commands all work | Kill switch test: positions closed + paused + scanner stopped |
| 2A-4 | Agent status API returns real agent data | HTTP response with 23 agent statuses |
| 2A-5 | Risk status API returns real risk data | HTTP response with portfolio heat, drawdown |
| 2A-6 | Trade history with pagination | HTTP response with trades, total, page metadata |
| 2A-7 | WebSocket real-time events fire | WS message received on dashboard/scanner channels |
| 2B-1 | CF JWT validated on all protected endpoints | 401 without valid CF header, 200 with it |
| 2B-2 | Health endpoints bypass CF check | 200 without CF header on /health |
| 2B-3 | Dual-layer auth works | Request needs both CF JWT + app JWT |
| 2C-1 | Rate limiting enforced | 429 response after exceeding limit |
| 2C-2 | Security headers present | Response headers include all 6 security headers |
| 2C-3 | WS idle timeout works | Connection dropped after 30min idle |
| 2D-1 | Schema parity validated | All SQLite columns mapped to PostgreSQL |
| 2D-2 | Migration runs successfully | All rows migrated, counts match |
| 2D-3 | Validation report passes | Aggregates match, FK integrity, no NULLs |
| 2E-1 | Login page functional | Can log in with valid credentials |
| 2E-2 | Dashboard shows live data | Portfolio summary, crash score, recent trades |
| 2E-3 | Mobile responsive | Dashboard readable on 375px viewport |
| 2E-4 | WebSocket connected | Dashboard updates in real-time |

---

## Risk Register

| Risk | Mitigation |
|------|------------|
| Engine commands too slow (>5s) | Add timeout handling, return partial data if available |
| CF signing key rotation | Cache with 5min TTL + force-refresh on validation failure |
| SQLite 567MB migration too slow | Batch inserts (1000 rows), skip system_logs (2M rows) initially |
| Frontend bundle too large | Code-split pages, lazy-load non-critical components |
| WebSocket reconnection storms | Exponential backoff with jitter (1s→2s→4s→8s→30s max) |
