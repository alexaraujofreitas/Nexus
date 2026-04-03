# Phase 4 — Operational Maturity & Decision Insight

## Scope

5 pages (3 Tier 1, 2 Tier 2), E2E testing infrastructure, live integration tests, bundle optimization.

**Tier 1** (must complete fully): Settings, Logs, Performance Analytics
**Tier 2** (controlled scope): Backtesting, Validation

**Explicitly deferred**: Full analytics suite (9 tabs), Edge Analysis, Quant Dashboard, advanced research tools.

---

## Infrastructure Work

### 4-INF-1: PostgreSQL + Redis in Sandbox

Stand up real PostgreSQL and Redis for automated integration testing.

- PostgreSQL via `pgserver` (already available — used in Phase 2 migration)
- Redis via `redis-server` (install if needed, or use fakeredis for unit tests + real Redis for integration)
- Each test run starts from clean, isolated database
- Seed data fixture for deterministic tests

### 4-INF-2: Eliminate Skipped Integration Tests

All 4 currently-failing integration tests must pass:
1. `test_alembic_upgrade_head` — Alembic migration against real PostgreSQL
2. `test_api_starts_and_health_endpoints` — Full API boot with PG + Redis
3. `test_full_auth_flow` — Register → login → refresh → protected endpoint
4. `test_engine_command_via_api` — API → Redis → engine reply roundtrip

### 4-INF-3: Playwright E2E Infrastructure

Install Playwright with Chromium. 10 initial E2E tests:

| # | Test | Viewport |
|---|------|----------|
| 1 | Login flow (form → auth → redirect) | 1280×720 |
| 2 | Dashboard load + data visible | 1280×720 |
| 3 | Scanner page load + results/watchlist | 1280×720 |
| 4 | Charts page load + symbol/TF switch | 1280×720 |
| 5 | Trading page load + position visibility | 1280×720 |
| 6 | Risk page + kill-switch dialog flow (open, type, cancel — no trigger) | 1280×720 |
| 7 | Settings page load + save flow | 1280×720 |
| 8 | Logs page load + filter interaction | 1280×720 |
| 9 | Mobile rendering: core pages at 375×812 | 375×812 |
| 10 | No horizontal overflow check at 375px for all pages | 375×812 |

Each test captures screenshot evidence. All tests run headless in Chromium.

### 4-INF-4: Bundle Optimization

- Lazy-load Charts page (React.lazy + Suspense) — lightweight-charts is ~300KB
- Lazy-load Backtesting page
- Code-split heavy dependencies
- Target: main chunk < 300KB gzipped

---

## Section 4A — Settings (`/settings`)

### Purpose
Configure risk parameters, strategy toggles, execution behavior, and API keys.

### Backend Dependencies (existing)
- `GET /api/v1/settings/` → full config or `?section=risk`
- `PATCH /api/v1/settings/` → `{updates: {key: value}}`
- WS channel: `engine` (config.changed events)

### UI Components

**SettingsPage** (`/settings`)
- Tab bar: "Risk" | "Strategy" | "Execution" | "API Keys"

**RiskTab**
- Portfolio heat limit slider (0–100%, current value from `risk.max_portfolio_heat`)
- Max drawdown threshold input (`risk.max_drawdown`)
- Max positions input (`risk.max_open_positions`)
- Risk per trade % input (`risk_engine.risk_pct_per_trade`)
- Max capital % per trade input (`risk_engine.max_capital_pct`)
- Save button → PATCH /settings/

**StrategyTab**
- Model toggles: list of all models with enable/disable switches
  - Reads `disabled_models` array from config
  - Toggle adds/removes model name from array
- Min confluence score slider (`idss.min_confluence_score`, range 0.20–0.80)
- Multi-timeframe confirmation toggle (`multi_tf.confirmation_required`)
- Save button → PATCH /settings/

**ExecutionTab**
- Auto-execute toggle (`scanner.auto_execute`)
- Paper trading mode indicator (read-only — Phase 1 safety)
- Default timeframe selector (`data.default_timeframe`: 15m, 30m, 1h, 4h)
- Save button → PATCH /settings/

**APIKeysTab**
- Masked display of configured API keys (CryptoPanic, Coinglass, Reddit)
- Key present / missing indicator per service
- Input fields for updating keys (masked, reveal toggle)
- Save button → PATCH /settings/ with key values
- Note: actual secret storage is config.yaml — API key values shown as masked (`****...last4`)

**Mobile layout**: tabs stack vertically, full-width inputs
**Desktop layout**: sidebar tabs left, content right

### WebSocket Integration
- Subscribe to `engine` channel
- On `config.changed` event: refetch config, show "Config updated externally" toast

---

## Section 4B — Logs (`/logs`)

### Purpose
Real-time structured log viewer with filtering, level highlighting, and component isolation.

### New Backend Work

**Engine: Log Collector + Publisher**
In `web/engine/main.py`, add a custom Python logging handler that captures log records from key loggers (engine, scanner, signals, risk, executor) and publishes to Redis:
```
nexus:events:logs → {timestamp, level, component, message, extra}
```

**New API Route: `app/api/logs.py`**
- `GET /api/v1/logs/recent?limit=200&level=WARNING&component=scanner` — returns last N log entries from in-memory ring buffer (engine keeps last 2000 entries)
- No persistent log storage required — in-memory ring buffer in engine process

**New Engine Command: `get_logs`**
- Added to ALLOWED_ACTIONS
- Returns from ring buffer with optional level/component filters

### UI Components

**LogsPage** (`/logs`)

**FilterBar**
- Level dropdown: ALL | DEBUG | INFO | WARNING | ERROR | CRITICAL
- Component dropdown: ALL | engine | scanner | signals | risk | executor | exchange
- Search input (text filter on message content)
- Clear button
- Auto-scroll toggle (on by default)

**LogStream**
- Virtualized list of log entries (render only visible rows for performance)
- Each entry: timestamp | level badge (color-coded) | component | message
- Level colors: DEBUG=gray, INFO=blue, WARNING=amber, ERROR=red, CRITICAL=red-bold
- Click to expand: full message + extra fields (JSON)
- Auto-scroll to bottom on new entries (when enabled)

**Mobile layout**: full-width, compact log rows (timestamp abbreviated, component abbreviated)
**Desktop layout**: full-width with generous column spacing

### WebSocket Integration
- Subscribe to `logs` channel on mount
- Each WS message is a new log entry → prepend to virtual list
- Rate limit display: batch WS messages at 100ms intervals to prevent UI thrashing

---

## Section 4C — Performance Analytics (`/analytics`)

### Purpose
Equity curve, trade statistics, drawdown tracking, and performance breakdown.

### New Backend Work

**New API Route: `app/api/analytics.py`**
- `GET /api/v1/analytics/equity-curve` — time-series of capital snapshots
  - Engine command: `get_equity_curve`
  - Source: closed trades → cumulative PnL over time
  - Returns: `{points: [{time, capital, pnl}], initial_capital}`

- `GET /api/v1/analytics/metrics` — aggregate performance metrics
  - Engine command: `get_performance_metrics`
  - Returns: `{total_trades, win_rate, profit_factor, avg_r, max_drawdown_pct, sharpe_ratio, calmar_ratio, recovery_factor, best_trade, worst_trade, avg_win, avg_loss, win_streak, loss_streak}`

- `GET /api/v1/analytics/trade-distribution` — trade PnL distribution histogram
  - Engine command: `get_trade_distribution`
  - Returns: `{buckets: [{range_min, range_max, count}], mean, median, std}`

- `GET /api/v1/analytics/by-model` — per-model breakdown
  - Engine command: `get_performance_by_model`
  - Returns: `{models: [{name, trades, win_rate, pf, avg_r}]}`

**New Engine Commands (4):**
- `get_equity_curve` — compute from `_closed_trades` list
- `get_performance_metrics` — aggregate stats from `_closed_trades`
- `get_trade_distribution` — histogram buckets from trade PnL
- `get_performance_by_model` — group by `models_fired` field

### UI Components

**AnalyticsPage** (`/analytics`)

**SummaryCards** (top row)
- Total trades, win rate, profit factor, avg R, max drawdown
- Each as a compact card with value + label
- Color-coded: green if above baseline, red if below

**EquityCurveChart**
- Line chart (lightweight-charts or recharts) showing capital over time
- Overlay: horizontal line at initial capital
- Drawdown shading below equity curve
- Tooltip with capital value + PnL at each point

**TradeDistributionChart**
- Histogram of trade PnL buckets
- Green bars for positive, red for negative
- Mean + median lines

**ModelBreakdownTable**
- Table: Model | Trades | Win Rate | PF | Avg R
- Color-coded cells (green ≥ baseline, red < baseline)
- Sort by PF or win rate

**Mobile layout**: single column, cards → equity chart → distribution → table (scroll)
**Desktop layout**: 2-col summary cards, full-width charts below, table full-width

### WebSocket Integration
- Subscribe to `trades` channel
- On new trade close: refetch metrics and equity curve
- Debounce refetch to 5s to avoid thrashing

---

## Section 4D — Backtesting (`/backtest`)

### Purpose
Minimal UI: launch a backtest with basic parameters, view summary results. No advanced optimization.

### New Backend Work

**New API Route: `app/api/backtest.py`**
- `POST /api/v1/backtest/start` — `{symbols: string[], start_date, end_date, timeframe, fee_pct}`
  - Launches backtest as asyncio.create_task in engine
  - Stores job in Redis hash: `nexus:backtest:jobs:{job_id}`
  - Returns: `{job_id, status: "running"}`

- `GET /api/v1/backtest/status/{job_id}` — poll progress
  - Returns: `{job_id, status, progress_pct, elapsed_s}`

- `GET /api/v1/backtest/results/{job_id}` — completed results
  - Returns: `{job_id, status: "complete", metrics: {pf, wr, max_dd, cagr, n_trades, sharpe}, trades: [...top 20...]}`

**New Engine Commands (3):**
- `start_backtest` — validates params, spawns BacktestRunner in thread pool, stores results in Redis
- `get_backtest_status` — reads job hash from Redis
- `get_backtest_results` — reads completed results from Redis

### UI Components

**BacktestPage** (`/backtest`)

**ConfigPanel**
- Symbol multiselect: checkboxes for BTC/USDT, ETH/USDT, SOL/USDT, BNB/USDT, XRP/USDT
- Date range picker: start date + end date
- Timeframe selector: 15m | 30m | 1h | 4h
- Fee %: input (default 0.04)
- "Run Backtest" button

**ProgressBar**
- Shown while backtest running
- Progress % bar + elapsed time + ETA

**ResultsSummary**
- Metrics cards: PF, WR, Max DD, CAGR, Sharpe, n trades
- Color-coded vs baseline thresholds

**RecentRuns**
- List of recent backtest runs: date, params, PF, WR
- Click to view full results

**Mobile layout**: config panel stacked, full-width progress/results
**Desktop layout**: config panel left sidebar, results right

### WebSocket Integration
- Not required for MVP — poll status endpoint every 2s during active run

---

## Section 4E — Validation (`/validation`)

### Purpose
System integrity checks, data consistency verification, and readiness assessment.

### New Backend Work

**New API Route: `app/api/validation.py`**
- `GET /api/v1/validation/health` — comprehensive health report
  - Engine command: `get_validation_health`
  - Returns component status, thread counts, error rates, last errors, data freshness

- `GET /api/v1/validation/readiness` — system readiness assessment
  - Engine command: `get_readiness`
  - Uses `SystemReadinessEvaluator` + `DemoPerformanceEvaluator`
  - Returns: `{verdict, score, checks: [{name, passed, value, threshold, note}]}`

- `GET /api/v1/validation/data-integrity` — data consistency checks
  - Engine command: `get_data_integrity`
  - Checks: position JSON ↔ DB sync, trade count consistency, capital reconciliation
  - Returns: `{passed, checks: [{name, status, detail}]}`

**New Engine Commands (3):**
- `get_validation_health` — collect component health data
- `get_readiness` — run readiness evaluator
- `get_data_integrity` — cross-check positions JSON, DB, and in-memory state

### UI Components

**ValidationPage** (`/validation`)

**HealthOverview**
- Component status cards: Exchange, Database, Redis, Scanner, Executor, Agents
- Each card: status dot (green/yellow/red) + details
- Thread count gauge + warning threshold

**ReadinessPanel**
- Verdict badge: STILL_LEARNING | IMPROVING | READY_FOR_CAUTIOUS_LIVE
- Score bar (0–100)
- Checklist: expand each check to see value vs threshold

**DataIntegrityPanel**
- Check list with PASS/FAIL badges
- Details expandable per check
- "Run Checks" button to refresh

**Mobile layout**: single column, overview → readiness → integrity
**Desktop layout**: 2-col, health left + readiness right, integrity full-width

### WebSocket Integration
- Subscribe to `engine` channel for state change notifications
- Auto-refresh health on engine state change

---

## Routing Updates

```tsx
// App.tsx lazy-loaded additions
const Charts = lazy(() => import('./pages/Charts'));
const Backtest = lazy(() => import('./pages/Backtest'));

// New routes
<Route path="settings" element={<Settings />} />
<Route path="logs" element={<Logs />} />
<Route path="analytics" element={<Analytics />} />
<Route path="backtest" element={<Suspense fallback={<PageLoader />}><Backtest /></Suspense>} />
<Route path="validation" element={<Validation />} />
```

Sidebar: Dashboard, Scanner, Charts, Trading, Intelligence, Risk, Analytics, Backtest, Settings, Logs, Validation (11 items)

---

## Test Strategy

### Unit Tests (per section)
- 4A: Settings read/write, section filtering, API key masking
- 4B: Log ring buffer, level filtering, component filtering, WS log publish
- 4C: Equity curve computation, metrics aggregation, trade distribution buckets, by-model grouping
- 4D: Backtest start/poll/results lifecycle, Redis job storage, parameter validation
- 4E: Health report assembly, readiness evaluator integration, data integrity checks

### Integration Tests (all must pass — zero skipped)
- PostgreSQL: Alembic migration, CRUD operations
- Redis: command roundtrip, pub/sub events
- API: full startup, auth flow, engine command dispatch
- WebSocket: subscription, event relay

### E2E Tests (Playwright — 10 tests)
- Desktop + mobile viewport coverage
- Screenshot evidence captured per test

### Regression
- All Phase 1+2+3 tests must still pass
- Plus all new Phase 4 tests
- Zero failures, zero skips

---

## Acceptance Criteria

| ID | Criterion | Validation |
|----|-----------|------------|
| 4A-1 | Settings page loads real config | GET /settings/ returns live data, rendered in form fields |
| 4A-2 | Risk params save correctly | PATCH /settings/ with risk values, verify persisted |
| 4A-3 | Model enable/disable toggles work | Toggle updates disabled_models array, verified on reload |
| 4A-4 | API key display masked | Keys shown as `****...last4`, input reveals on toggle |
| 4B-1 | Log stream renders real-time entries | WS logs channel → entries appear within 500ms |
| 4B-2 | Level filtering works | Select WARNING → only WARNING+ entries shown |
| 4B-3 | Component filtering works | Select scanner → only scanner logs shown |
| 4B-4 | Error entries highlighted | ERROR/CRITICAL entries visually distinct (red) |
| 4C-1 | Equity curve renders from real data | GET /analytics/equity-curve → chart with capital time-series |
| 4C-2 | Metrics match trade data | WR, PF, DD computed from closed trades — verified manually |
| 4C-3 | Trade distribution renders | Histogram with correct bucket counts |
| 4C-4 | Model breakdown shows per-model stats | Table with correct per-model WR, PF, n |
| 4D-1 | Backtest can be launched | POST /backtest/start returns job_id, status=running |
| 4D-2 | Backtest results display | GET /backtest/results returns PF, WR, DD, n |
| 4D-3 | Progress indicator works | Status endpoint shows progress_pct during run |
| 4E-1 | Health report shows component status | All components (exchange, DB, scanner, etc.) with status indicators |
| 4E-2 | Readiness verdict displays | STILL_LEARNING/IMPROVING/READY badge with score |
| 4E-3 | Data integrity checks run | Check list with PASS/FAIL per check |
| 4-INT | All integration tests pass (0 skipped) | PostgreSQL + Redis live, 4 previously-skipped tests pass |
| 4-E2E | 10 Playwright E2E tests pass | Desktop + mobile viewport, screenshots captured |
| 4-OPT | Charts lazy-loaded, main chunk < 300KB gzip | Bundle analysis confirms code-splitting |
| 4-M1 | All 5 new pages mobile-responsive at 375px | No overflow, touch targets ≥ 44px, font ≥ 12px |
| 4-M2 | All 5 new pages desktop-rendered at 1280px | Proper multi-column layouts |
| 4-REG | Full regression passes | All Phase 1+2+3+4 tests pass, 0 failures, 0 skips |

**Total: 25 acceptance criteria**

---

## Risk Items

- Backtest execution in engine process may block event loop — use thread pool with timeout
- Log ring buffer size (2000 entries) may be insufficient for debugging — make configurable
- Equity curve requires trade history — empty if no trades yet (handle gracefully)
- Playwright in sandbox may need Xvfb or headless mode — verify setup
- Bundle splitting may require Vite config changes for vendor chunking
