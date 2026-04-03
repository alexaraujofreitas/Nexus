# Phase 5 — Advanced Analytics, Browser E2E Infrastructure & Stress Testing

## Scope

3 workstreams executed in parallel:

**Workstream A — Advanced Analytics** (4 enhanced pages):
1. Equity & Drawdown Analysis (enhanced equity curve + drawdown curve + rolling metrics)
2. Trade Performance Breakdown (win/loss distribution, R-multiple, expectancy, duration vs outcome)
3. Strategy/Model Performance (enhanced per-model, sortable, filterable)
4. Regime Analysis (performance by market regime, returns and PF per regime)

**Workstream B — Playwright Browser Infrastructure**:
1. Dockerfile for Playwright runner with Chromium
2. docker-compose.yml integrating app stack + Playwright
3. GitHub Actions CI workflow
4. All 10+ Playwright spec tests executing in Docker

**Workstream C — Backtest Stress Testing**:
1. Controlled concurrency (3-5 simultaneous jobs)
2. Long-running stability (multi-year dataset)
3. Cancellation handling (clean termination)
4. Failure injection (invalid params, missing data)
5. Result integrity (deterministic comparison)

**Explicitly deferred**: Full 9-tab analytics, edge analysis, quant dashboard, risk exposure analysis (optional stretch goal).

---

## Workstream A — Advanced Analytics

### Current State (Phase 4)

Analytics.tsx has: MetricCards (5), equity curve (LineSeries), PnL distribution (HistogramSeries), model breakdown table. Single page, no sub-navigation.

### Target Architecture

Replace single Analytics page with tabbed sub-pages. URL: `/analytics` with tab query param `?tab=equity|trades|models|regime`.

### 5A-1: Equity & Drawdown Analysis (`?tab=equity`)

**Backend endpoints (new):**
- `GET /analytics/drawdown-curve` → engine action `get_drawdown_curve`
  - Returns `{points: [{time, drawdown_pct, peak_capital}]}`
- `GET /analytics/rolling-metrics?window=20` → engine action `get_rolling_metrics`
  - Returns `{points: [{time, rolling_wr, rolling_pf, rolling_avg_r}], window}`

**Frontend components:**
- Enhanced equity curve with area fill (green above start, red below)
- Drawdown curve chart (inverted, red area, below equity)
- Rolling metrics chart (WR, PF on dual Y-axis, configurable window: 10/20/50)
- Underwater chart (time spent in drawdown)
- Statistics panel: peak capital, current DD, max DD date, recovery time

### 5A-2: Trade Performance Breakdown (`?tab=trades`)

**Backend endpoints (new):**
- `GET /analytics/r-distribution` → engine action `get_r_distribution`
  - Returns `{buckets: [{r_min, r_max, count}], expectancy, median_r, max_win_r, max_loss_r}`
- `GET /analytics/duration-analysis` → engine action `get_duration_analysis`
  - Returns `{buckets: [{duration_min_s, duration_max_s, count, avg_r, win_rate}]}`

**Frontend components:**
- Win/Loss distribution (enhanced from Phase 4 with separate series)
- R-multiple distribution histogram (centered on 0)
- Expectancy card with calculation breakdown (WR * AvgWin - LR * AvgLoss)
- Duration vs outcome scatter/bucket chart
- Statistics: consecutive wins/losses, best/worst trade, longest/shortest duration

### 5A-3: Strategy/Model Performance (`?tab=models`)

**Backend endpoints (enhanced):**
- `GET /analytics/by-model?sort=pf&order=desc&regime=bull_trend` → existing `get_performance_by_model` enhanced with sort/filter
  - Returns `{models: [{name, trades, win_rate, pf, avg_r, max_dd, expectancy, best_trade, worst_trade, active}]}`

**Frontend components:**
- Enhanced sortable table (click column headers to sort)
- Filter chips: regime, asset, date range
- Sparkline mini-charts per model (last 20 trades)
- Model comparison mode (select 2-3, side-by-side charts)
- Color coding: green (PF>1.2), yellow (1.0-1.2), red (<1.0)

### 5A-4: Regime Analysis (`?tab=regime`)

**Backend endpoints (new):**
- `GET /analytics/by-regime` → engine action `get_performance_by_regime`
  - Returns `{regimes: [{name, trades, win_rate, pf, avg_r, avg_duration_s, pct_of_total}]}`
- `GET /analytics/regime-transitions` → engine action `get_regime_transitions`
  - Returns `{transitions: [{from, to, count, avg_pnl_during_transition}]}`

**Frontend components:**
- Regime performance table (sortable)
- Regime distribution pie/donut chart (% of time in each regime)
- Performance bar chart per regime (PF comparison)
- Regime transition matrix (heatmap-style)
- Current regime indicator with historical context

---

## Workstream B — Playwright Docker + CI

### 5B-1: Dockerfile (`web/playwright/Dockerfile`)

```dockerfile
FROM mcr.microsoft.com/playwright:v1.58.0-noble
WORKDIR /app
COPY web/frontend/package.json web/frontend/package-lock.json ./
RUN npm ci
COPY web/frontend/e2e ./e2e
COPY web/frontend/playwright.config.ts ./
CMD ["npx", "playwright", "test", "--reporter=list,html"]
```

### 5B-2: docker-compose.yml (`web/docker-compose.e2e.yml`)

Services:
- `postgres`: PostgreSQL 15, port 5433
- `redis`: Redis 7 alpine, port 6379
- `api`: FastAPI backend, depends on postgres+redis
- `frontend`: Vite preview (production build), port 5173
- `playwright`: Playwright runner, depends on api+frontend, runs tests

### 5B-3: GitHub Actions CI (`web/.github/workflows/e2e.yml`)

Triggers: push to `main`, `web-*` branches, PRs touching `web/`.
Steps: build frontend, start services, run Playwright, upload artifacts (screenshots, traces, HTML report).

### 5B-4: Test Coverage

All 10 existing Playwright specs must execute and pass:
1. Login flow (auth + redirect)
2. Dashboard load
3. Scanner page + results
4. Trading page + positions
5. Settings tabs + save
6. Logs filter interaction
7. Analytics rendering (charts visible)
8. Backtest workflow (start + progress)
9. Validation page (3 sections)
10. Mobile viewport (375px) + hamburger nav

### 5B-5: WebSocket Behavioral Tests (new)

- `ws-connect.spec.ts`: Connect to WS, verify handshake
- `ws-subscribe.spec.ts`: Subscribe to channels, receive messages
- `ws-reconnect.spec.ts`: Disconnect → auto-reconnect behavior

---

## Workstream C — Backtest Stress Testing

### 5C-1: Controlled Concurrency

Test file: `web/backend/tests/test_backtest_stress.py`

- Launch 3 concurrent backtest jobs via API
- Verify each returns unique job_id
- Verify results are isolated (no cross-contamination)
- Verify all 3 complete or timeout gracefully

### 5C-2: Long-Running Stability

- Submit backtest with 2-year date range
- Monitor memory via `tracemalloc`
- Verify progress updates are monotonically increasing
- Verify completion within timeout (10 min)

### 5C-3: Cancellation Handling

- Start a backtest
- Cancel via `POST /backtest/cancel/{job_id}` (new endpoint)
- Verify status transitions to `cancelled`
- Verify no zombie threads/processes
- Verify subsequent jobs work normally

### 5C-4: Failure Injection

- Invalid symbols: verify 400/422 error
- Invalid date range (end < start): verify error
- Missing data scenario: verify graceful error
- Malformed parameters: verify validation catches

### 5C-5: Result Integrity

- Run identical backtest twice
- Compare results for determinism
- Verify PF, WR, trade count match within tolerance

---

## API Contract Additions (Phase 5)

### New Engine Actions

| Action | Method | Endpoint | Returns |
|--------|--------|----------|---------|
| `get_drawdown_curve` | GET | /analytics/drawdown-curve | `{points: [{time, drawdown_pct, peak_capital}]}` |
| `get_rolling_metrics` | GET | /analytics/rolling-metrics?window=N | `{points: [{time, rolling_wr, rolling_pf}], window}` |
| `get_r_distribution` | GET | /analytics/r-distribution | `{buckets, expectancy, median_r}` |
| `get_duration_analysis` | GET | /analytics/duration-analysis | `{buckets: [{duration_min_s, count, avg_r}]}` |
| `get_performance_by_regime` | GET | /analytics/by-regime | `{regimes: [{name, trades, wr, pf}]}` |
| `get_regime_transitions` | GET | /analytics/regime-transitions | `{transitions: [{from, to, count}]}` |
| `cancel_backtest` | POST | /backtest/cancel/{job_id} | `{status: "cancelled"}` |

---

## Acceptance Criteria

| ID | Criterion | Validation |
|----|-----------|------------|
| 5A-1a | Equity curve renders with area fill | Chart visible with green/red area, data from API |
| 5A-1b | Drawdown curve renders below equity | Inverted red area chart, peak-to-trough visualized |
| 5A-1c | Rolling metrics chart with configurable window | Dual Y-axis, window 10/20/50 selector changes data |
| 5A-2a | R-multiple distribution renders | Histogram centered on 0, correct bucket counts |
| 5A-2b | Expectancy displayed with breakdown | Formula shown, value matches manual calculation |
| 5A-2c | Duration vs outcome analysis renders | Bucket chart showing duration → avg R relationship |
| 5A-3a | Model table is sortable | Click column → sort asc/desc, visual indicator |
| 5A-3b | Model table has filter capability | Regime/asset filter changes displayed rows |
| 5A-4a | Regime performance table renders | All regimes with WR/PF/trades, data from API |
| 5A-4b | Regime distribution chart renders | Pie/donut showing % time in each regime |
| 5B-1 | Dockerfile builds and runs Playwright | `docker build` succeeds, `npx playwright test` runs |
| 5B-2 | docker-compose starts full stack | All services healthy, frontend accessible |
| 5B-3 | GitHub Actions CI config present | Valid workflow YAML, triggers on web/ changes |
| 5B-4 | All Playwright specs pass in Docker | 10+ tests pass with screenshot artifacts |
| 5B-5 | WebSocket behavioral tests present | 3 WS test files, cover connect/subscribe/reconnect |
| 5C-1 | 3 concurrent backtests complete correctly | Isolated results, no crashes, all finish |
| 5C-2 | Long-running backtest stable | Memory stable, progress monotonic, completes |
| 5C-3 | Cancellation cleans up properly | Status=cancelled, no zombies, next job works |
| 5C-4 | Invalid params return proper errors | 400/422 for bad input, graceful error messages |
| 5C-5 | Identical backtests produce same results | PF/WR/trade count match within 0.1% tolerance |
| 5-M1 | All new analytics pages mobile-responsive | No overflow at 375px, touch targets >= 44px |
| 5-M2 | All new analytics pages desktop-rendered | Proper multi-column layouts at 1280px |
| 5-REG | Full regression passes | All Phase 1-5 tests pass, 0 failures, 0 skips |
| 5-E2E | HTTP E2E tests cover new endpoints | New analytics APIs included in E2E suite |
| 5-BUILD | TypeScript clean + Vite build clean | tsc --noEmit + vite build succeed |

**Total: 25 acceptance criteria**

---

## Risk Items

- Drawdown curve computation may be expensive for large trade histories — consider caching
- Regime analysis requires access to HMM regime labels which may not be in trade records — need to verify data model
- Docker Playwright image is large (~2GB) — may need multi-stage build for faster CI
- WebSocket tests in Playwright require server to be running with WS enabled
- Backtest stress tests need a mock/lightweight engine implementation since real engine won't be available in CI

---

## File Plan

### Backend
- `web/backend/app/api/analytics.py` — 6 new endpoints added
- `web/backend/app/api/backtest.py` — cancel endpoint added
- `web/backend/app/api/engine.py` — 7 new actions added to ALLOWED_ACTIONS
- `web/backend/tests/test_phase5_api.py` — API tests for new endpoints
- `web/backend/tests/test_backtest_stress.py` — stress test suite

### Frontend
- `web/frontend/src/pages/Analytics.tsx` — refactored to tabbed layout
- `web/frontend/src/pages/analytics/EquityDrawdown.tsx` — equity + DD charts
- `web/frontend/src/pages/analytics/TradePerformance.tsx` — R-dist + duration
- `web/frontend/src/pages/analytics/ModelPerformance.tsx` — sortable table
- `web/frontend/src/pages/analytics/RegimeAnalysis.tsx` — regime charts
- `web/frontend/src/api/analytics.ts` — new API functions

### Infrastructure
- `web/playwright/Dockerfile`
- `web/docker-compose.e2e.yml`
- `web/.github/workflows/e2e.yml`
- `web/frontend/e2e/ws-connect.spec.ts`
- `web/frontend/e2e/ws-subscribe.spec.ts`
- `web/frontend/e2e/ws-reconnect.spec.ts`
