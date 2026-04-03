# Phase 3 — Trading Operations Frontend

## Objective

Turn NexusTrader into a usable trading system in the browser. Phase 3 delivers 5 operational pages with real-time data, WebSocket integration, and mobile-first design. No placeholder data. No compressed desktop UI.

## Prerequisites

- Phase 2 fully approved (202 tests, all validations passed)
- Backend API endpoints for all 5 pages already exist (Phase 2A)
- WebSocket infrastructure ready (11 channels, heartbeat, token refresh)
- Auth flow complete (JWT + refresh tokens)

---

## Architecture Decisions

### Frontend Stack (unchanged from Phase 2E)
- React 19 + TypeScript + Vite
- Tailwind CSS v4 (utility-first, mobile-first breakpoints)
- Zustand for client state (auth, WebSocket)
- TanStack React Query for server state + polling fallback
- React Router v7 for routing

### New Dependencies
- `lightweight-charts` (TradingView) — candlestick charts, indicators, markers
- `lucide-react` — icon set (consistent, tree-shakeable)

### Data Flow Pattern (established in Phase 2E)
```
API polling (TanStack Query, 10–30s) ← primary data source
WebSocket push (wsStore) ← real-time overlay when connected
Component: uses WS data if fresh, falls back to API data
```

### Mobile-First Design Rules
1. All layouts start at `grid-cols-1`, expand at `md:` (768px) and `lg:` (1024px)
2. Touch targets ≥ 44px
3. Font sizes ≥ 12px
4. Tables use `overflow-x-auto` wrapper
5. Charts responsive via `width: 100%` + ResizeObserver
6. No hover-only interactions — all actions via tap/click

---

## Section 3A — Market Scanner (`/scanner`)

### Purpose
Display real-time scan results: symbol cards with regime, score, direction, models fired, timing.

### Backend Dependencies
- `GET /api/v1/scanner/results` → `{results: OrderCandidate[], count, scanner_running}`
- `GET /api/v1/scanner/watchlist` → `{symbols, weights}`
- `POST /api/v1/scanner/trigger` → triggers manual scan
- WS channel: `scanner` — pushes after each `SCAN_CYCLE_COMPLETE`

### UI Components

**ScannerPage** (`/scanner`)
- Header row: "Market Scanner" title + scanner status badge (Running/Stopped/Scanning) + "Trigger Scan" button
- Cycle timer: countdown to next scan cycle (candle-boundary aligned)
- Watchlist bar: horizontal chip row showing tracked symbols with weights
- Results grid: card per symbol result

**SymbolCard** (one per scan result)
- Symbol name + regime badge (color-coded: bull=green, bear=red, ranging=yellow, uncertain=gray)
- Score bar (0–1.0 visual progress bar with numeric label)
- Direction arrow (▲ BUY green, ▼ SELL red)
- Models fired: chip list (e.g., "MomentumBreakout", "SLC")
- Entry/SL/TP prices with R:R ratio
- Approval status: ✓ Approved / ✗ Rejected (with reason tooltip)
- Position size in USDT
- Generated timestamp (relative: "2m ago")

**Mobile layout**: single column, cards stack vertically
**Desktop layout**: 2-col grid (md:) or 3-col (lg:)

### WebSocket Integration
- Subscribe to `scanner` channel on mount
- On message: replace query cache with fresh data (instant update)
- Flash animation on new results (brief green pulse on updated cards)

---

## Section 3B — Chart Workspace (`/charts`)

### Purpose
Interactive candlestick charts with indicators and signal markers. Primary analysis tool.

### Backend Dependencies
- New endpoint needed: `GET /api/v1/charts/ohlcv?symbol=BTC/USDT&timeframe=30m&limit=300`
  - Returns: `{bars: [{time, open, high, low, close, volume}], symbol, timeframe}`
  - Engine command: `get_ohlcv` (new — reads from exchange manager cache)
- `GET /api/v1/scanner/results` — for signal markers overlay
- WS channel: `ticker` — live price updates for active symbol

### New Engine Command
Add `get_ohlcv` to command handler:
```python
def _handle_get_ohlcv(self, params):
    symbol = params.get("symbol", "BTC/USDT")
    timeframe = params.get("timeframe", "30m")
    limit = params.get("limit", 300)
    df = self._exchange_manager.get_ohlcv(symbol, timeframe, limit)
    return {"bars": df_to_bars(df), "symbol": symbol, "timeframe": timeframe}
```

### New API Endpoint
```python
# app/api/charts.py
@router.get("/ohlcv")
async def get_ohlcv(symbol: str = "BTC/USDT", timeframe: str = "30m", limit: int = 300):
    return await _send_engine_command("get_ohlcv", {"symbol": symbol, "timeframe": timeframe, "limit": limit})
```

### UI Components

**ChartPage** (`/charts`)
- Symbol selector: dropdown/chips for watchlist symbols
- Timeframe selector: 15m / 30m / 1h / 4h buttons (highlight active)
- Chart area: `lightweight-charts` CandlestickSeries with volume histogram
- Indicator toggles: EMA 9/20/50, RSI 14, MACD (12,26,9)
- Signal markers: triangles on chart at signal generation timestamps (green ▲ buy, red ▼ sell)

**Indicators** (client-side computed from OHLCV data)
- EMA: simple exponential moving average as LineSeries overlay
- RSI: separate pane below chart (0–100 scale, overbought/oversold lines at 70/30)
- MACD: separate pane with MACD line, signal line, histogram

**Mobile layout**: full-width chart, controls in horizontal scroll row above
**Desktop layout**: chart takes ~75% width, symbol list sidebar on left

### WebSocket Integration
- Subscribe to `ticker` channel for active symbol
- On tick: append to chart's candlestick data (update last bar's close/high/low)
- Debounce to 1 update/second to avoid chart jank

---

## Section 3C — Paper Trading (`/trading`)

### Purpose
Manage open positions with real-time PnL, close/close-all, and view recent closed trades.

### Backend Dependencies
- `GET /api/v1/trading/positions` → `{positions: PaperPosition[], count: int}` (new dedicated endpoint)
- `GET /api/v1/trades/history?page=1&per_page=20` → `{trades: ClosedTrade[], total, page, per_page, pages}`
- `POST /api/v1/trading/close` with `{symbol: str}` → close single position
- `POST /api/v1/trading/close-all` → close all positions
- WS channels: `positions` (open position updates), `trades` (trade close events)

### UI Components

**TradingPage** (`/trading`)
- Tab bar: "Open Positions" | "Trade History"
- Position count badge on Open tab
- "Close All" button (with confirmation dialog)

**OpenPositionsTab**
- Card per position (not a table — better for mobile):
  - Symbol + side badge (LONG green / SHORT red)
  - Entry price → current price (with arrow direction)
  - Unrealized PnL (USDT + %, color-coded)
  - Size (USDT) + regime badge
  - SL / TP levels with visual distance indicator
  - Models fired chips
  - Duration ("Opened 2h 15m ago")
  - "Close" button per position (with confirmation)
  - Auto-partial status indicator (if applied)
  - Breakeven SL indicator (if applied)

**TradeHistoryTab**
- Table: Symbol, Side, Entry→Exit, PnL, Duration, Exit Reason, When
- Pagination controls (prev/next, page indicator)
- Summary row: total trades, win rate, total PnL
- Color-coded PnL cells (green/red)

**Mobile layout**: position cards full-width stacked; history table horizontal-scroll
**Desktop layout**: position cards in 2-col grid; history table full-width

### WebSocket Integration
- Subscribe to `positions` on mount → instant PnL updates
- Subscribe to `trades` → append new closes to history, update position count
- Optimistic UI: on close click, dim the position card immediately, remove on WS confirmation

---

## Section 3D — Intelligence (`/intelligence`)

### Purpose
Display agent signals, confluence scoring, and meta-signal breakdown.

### Backend Dependencies
- `GET /api/v1/signals/agents` → `{agent_name: {running, stale, signal, confidence, updated_at, errors}}`
- `GET /api/v1/signals/confluence` → signal list with scores
- WS channel: `signals` — agent signal updates

### UI Components

**IntelligencePage** (`/intelligence`)
- Header: "Intelligence" + agent count badge (e.g., "12/23 active")
- Meta-signal summary card: overall market read (bullish/bearish/neutral with score)

**AgentGrid** (card per agent)
- Agent name + status indicator (green=running, yellow=stale, red=error)
- Signal value (0–1.0 with color gradient bar)
- Confidence value (0–1.0)
- Last updated (relative timestamp)
- Error count (if > 0, show red badge)

**SignalsList** (from confluence scorer)
- Per-symbol signal cards:
  - Symbol + direction + score bar
  - Models contributing (chips)
  - Regime context
  - Entry/SL/TP if approved
  - Approval status with rejection reason if applicable

**Mobile layout**: single column, agents stacked
**Desktop layout**: agent grid 3–4 columns; signals list below

### WebSocket Integration
- Subscribe to `signals` channel
- On update: refresh agent statuses in real-time
- Stale indicators auto-update based on timestamp age

---

## Section 3E — Risk (`/risk`)

### Purpose
Portfolio risk overview, crash defense status, and emergency controls.

### Backend Dependencies
- `GET /api/v1/risk/status` → `{portfolio_heat_pct, drawdown_pct, open_positions, circuit_breaker_on, daily_loss_pct, crash_tier, is_defensive}`
- `GET /api/v1/dashboard/crash-defense` → `{tier, score, is_defensive, actions_log}`
- `POST /api/v1/system/kill-switch` → emergency stop
- WS channels: `risk`, `crash_defense`

### UI Components

**RiskPage** (`/risk`)
- Header: "Risk Management" + crash tier badge (color-coded)

**PortfolioHeatCard**
- Heat gauge: visual bar 0–100% with zones (green < 30%, yellow 30–60%, red > 60%)
- Numeric: portfolio heat %, drawdown %, daily loss %
- Circuit breaker status (on/off indicator)

**CrashDefenseCard**
- Tier display: large badge with color (NORMAL=green, DEFENSIVE=yellow, HIGH_ALERT=orange, EMERGENCY=red, SYSTEMIC=dark-red)
- Score value
- Defensive mode status
- Recent actions log: scrollable list of timestamped defensive actions

**ControlsCard**
- Kill Switch button: large, red, opens typed-confirmation modal
  - Modal step 1: Warning text — "This will close all positions, pause trading, and stop the scanner."
  - Modal step 2: Text input — user must type "KILL" (case-sensitive) to enable the confirm button
  - Confirm button: disabled + grayed until input matches "KILL" exactly
  - On confirm: POST kill-switch, show success/failure toast, clear input
  - On cancel: close modal, clear input, no action
- Pause/Resume trading toggle

**OpenExposureCard**
- Per-symbol breakdown: position count, total size, risk amount
- Total open positions count

**Mobile layout**: single column, heat card → crash defense → controls → exposure
**Desktop layout**: 2-col grid, heat + crash defense left, controls + exposure right

### WebSocket Integration
- Subscribe to `risk` + `crash_defense` channels
- Crash tier changes: animate badge transition + optional browser notification
- Circuit breaker activation: prominent alert banner

---

## Routing Updates

```tsx
// App.tsx additions
<Route path="scanner" element={<Scanner />} />
<Route path="charts" element={<Charts />} />
<Route path="trading" element={<Trading />} />
<Route path="intelligence" element={<Intelligence />} />
<Route path="risk" element={<Risk />} />
```

Sidebar navigation updated with all 6 pages (Dashboard + 5 new).

---

## New Backend Work

### New Engine Command: `get_ohlcv`
- Added to `web/engine/main.py` `_handle_command()` switch
- Added to `web/backend/app/api/engine.py` `ALLOWED_ACTIONS`
- Reads from exchange manager's cached OHLCV data

### New API Route File: `app/api/charts.py`
- `GET /charts/ohlcv` — symbol, timeframe, limit params

### New API Route File: `app/api/trading.py`
- `GET /trading/positions` — open positions with count (engine command: `get_positions`)
- `POST /trading/close` — close single position by symbol (engine command: `close_position`)
- `POST /trading/close-all` — close all positions (engine command: `close_all_positions`)

### WebSocket Event Expansion
- Engine service publishes to Redis channels on:
  - Position open/close → `nexus:events:positions` + `nexus:events:trades`
  - Scan complete → `nexus:events:scanner`
  - Agent update → `nexus:events:signals`
  - Risk change → `nexus:events:risk`
  - Crash tier change → `nexus:events:crash_defense`
  - Ticker update → `nexus:events:ticker`

---

## API Contract Reference (Frozen)

All endpoints below are finalized. Frontend code MUST use these exact paths and methods.

### Convention
- **GET** for all read operations (data retrieval, status checks)
- **POST** for all write operations (commands, triggers, mutations)
- Query params for GET filters; JSON body for POST payloads

### Phase 2 Endpoints (existing)

| Method | Path | Purpose | Response Shape |
|--------|------|---------|---------------|
| GET | `/api/v1/dashboard/summary` | Dashboard stats | `{capital, pnl, drawdown, positions, win_rate, profit_factor, recent_trades}` |
| GET | `/api/v1/dashboard/crash-defense` | Crash defense status | `{tier, score, is_defensive, actions_log}` |
| GET | `/api/v1/scanner/results` | Scan results | `{results: OrderCandidate[], count, scanner_running}` |
| GET | `/api/v1/scanner/watchlist` | Tracked symbols | `{symbols, weights}` |
| POST | `/api/v1/scanner/trigger` | Trigger manual scan | `{status, message}` |
| GET | `/api/v1/signals/agents` | Agent statuses | `{agent_name: {running, stale, signal, confidence, updated_at, errors}}` |
| GET | `/api/v1/signals/confluence` | Confluence signals | `{signals: Signal[]}` |
| GET | `/api/v1/risk/status` | Risk metrics | `{portfolio_heat_pct, drawdown_pct, open_positions, circuit_breaker_on, daily_loss_pct, crash_tier, is_defensive}` |
| GET | `/api/v1/trades/history` | Closed trades (paginated) | `{trades[], total, page, per_page, pages}` |
| GET | `/api/v1/system/health` | System health | `{exchange, database, threads, scanner, uptime}` |
| POST | `/api/v1/system/kill-switch` | Emergency stop | `{status, message}` |
| GET | `/api/v1/settings/` | Read config | `{settings: dict}` |
| PATCH | `/api/v1/settings/` | Update config | `{status, updated_keys}` |
| POST | `/api/v1/engine/command` | Generic engine cmd | `{status, data}` |
| GET | `/api/v1/engine/status` | Engine Redis state | `{state_hash}` |

### Phase 3 New Endpoints

| Method | Path | Purpose | Response Shape |
|--------|------|---------|---------------|
| GET | `/api/v1/charts/ohlcv` | OHLCV candle data | `{bars: [{time, open, high, low, close, volume}], symbol, timeframe}` |
| GET | `/api/v1/trading/positions` | Open positions | `{positions: PaperPosition[], count}` |
| POST | `/api/v1/trading/close` | Close single position | `{status, symbol, message}` |
| POST | `/api/v1/trading/close-all` | Close all positions | `{status, closed_count, message}` |

### Engine Commands (via Redis request/reply)

| Command | Used By | Method |
|---------|---------|--------|
| `get_positions` | `GET /trading/positions` | Read |
| `close_position` | `POST /trading/close` | Write |
| `close_all_positions` | `POST /trading/close-all` | Write |
| `get_ohlcv` | `GET /charts/ohlcv` | Read |
| `trigger_scan` | `POST /scanner/trigger` | Write |
| `kill_switch` | `POST /system/kill-switch` | Write |

### WebSocket Channels

| Channel | Events | Used By |
|---------|--------|---------|
| `dashboard` | Summary updates | Dashboard |
| `scanner` | Scan cycle results | Scanner page |
| `ticker` | Live price ticks | Charts page |
| `positions` | Position PnL updates | Trading page |
| `trades` | Trade close events | Trading page |
| `signals` | Agent signal updates | Intelligence page |
| `risk` | Risk metric changes | Risk page |
| `crash_defense` | Tier changes | Risk page, Dashboard |
| `engine` | Engine state changes | System-wide |
| `alerts` | System alerts | Notification layer |
| `logs` | Log stream | Debug/monitoring |

---

## Test Strategy

### Unit Tests (per section)
- 3A: Scanner page API calls, WS subscription, result rendering logic
- 3B: OHLCV endpoint, chart data transform, indicator computation
- 3C: Position management commands, trade history pagination, close confirmation
- 3D: Agent status parsing, signal list rendering
- 3E: Risk status parsing, kill switch command, crash defense display

### Integration Tests
- Full API→Redis→Engine roundtrip for `get_ohlcv`
- WebSocket scanner event → frontend cache invalidation
- Kill switch end-to-end

### Frontend Rendering Tests
- Each page: 375px mobile screenshot + 1280px desktop screenshot
- No horizontal overflow at 375px
- Typography ≥ 12px
- Touch targets ≥ 44px

### Regression
- Full Phase 1+2 suite must still pass
- Plus all new Phase 3 tests
- Zero failures, zero skips

---

## Acceptance Criteria

| ID | Criterion | Evidence Required |
|----|-----------|-------------------|
| 3A-1 | Scanner shows real scan results | Screenshot with symbol cards, scores, regimes |
| 3A-2 | Scanner updates via WebSocket | WS message triggers card refresh without page reload |
| 3A-3 | Trigger scan button works | POST trigger_scan → scan starts → results appear |
| 3A-4 | Watchlist displayed | Horizontal chips with symbol weights |
| 3B-1 | Candlestick chart renders real OHLCV | Chart with ≥100 bars for BTC/USDT 30m |
| 3B-2 | Timeframe switching works | Switch 30m→4h, chart reloads with correct data |
| 3B-3 | At least 2 indicators render | EMA + RSI visible on chart |
| 3B-4 | Signal markers on chart | Buy/sell triangles at signal timestamps |
| 3C-1 | Open positions show real data | Position cards with entry, current price, PnL |
| 3C-2 | Close position works | Click close → confirmation → position removed |
| 3C-3 | Positions update via WebSocket | PnL changes in real-time without page reload |
| 3C-4 | Trade history with pagination | Paginated table of closed trades |
| 3D-1 | Agent grid shows all agents | Cards for 23 agents with status/signal/confidence |
| 3D-2 | Signal list shows confluence data | Per-symbol signals with scores and models |
| 3D-3 | Stale/error indicators visible | Visual distinction for stale vs running agents |
| 3E-1 | Risk metrics display real data | Heat, drawdown, daily loss, crash tier |
| 3E-2 | Kill switch works with typed confirmation | Button → modal → type "KILL" → confirm enabled → POST → success feedback |
| 3E-3 | Crash defense actions log visible | Scrollable list of recent defensive actions |
| 3E-4 | Crash tier updates via WebSocket | Tier change reflected in real-time |
| 3-M1 | All 5 pages mobile-responsive at 375px | Screenshots proving no overflow, readable text |
| 3-M2 | All 5 pages desktop-rendered at 1280px | Screenshots proving proper layout expansion |
| 3-REG | Full regression passes | N tests passed, 0 failed, 0 skipped |

---

## Risk Register

| Risk | Mitigation |
|------|------------|
| lightweight-charts bundle too large | Tree-shake; lazy-load Chart page |
| OHLCV data stale if exchange disconnected | Show "Data as of" timestamp; gray overlay on stale |
| Kill switch accidental trigger | Two-step confirmation dialog with typed confirmation |
| WS reconnection causes duplicate events | Deduplicate by candidate_id / trade_id |
| Chart indicator computation too slow | Web Worker for indicator math if >500 bars |
| Mobile chart interaction conflicts with scroll | Touch event handling in chart container |

---

## Execution Order

1. Backend: Add `get_ohlcv` command + `/charts/ohlcv` endpoint + tests
2. Frontend: Install `lightweight-charts` + `lucide-react`
3. Build pages in order: Scanner → Charts → Trading → Intelligence → Risk
4. After each page: mobile + desktop rendering validation
5. Final: full regression + Gate Report
