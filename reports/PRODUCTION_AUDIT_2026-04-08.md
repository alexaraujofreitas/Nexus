# NexusTrader Production Readiness Audit

**Date:** 2026-04-08
**Scope:** 614 Python files (250 in core/), 6 audit domains
**Branch:** `claude/audit-nexustrader-production-MLfFD`

---

## Executive Summary

| Severity | Count |
|----------|-------|
| CRITICAL | 5 |
| HIGH     | 16 |
| MEDIUM   | 24 |
| LOW      | 10 |
| **Total** | **55** |

The system has a well-architected signal pipeline (disabled_models gate, adjusted_score isolation, entry_size_usdt immutability, _auto_partial_applied persistence all PASS). However, **the live execution path has multiple CRITICAL defects** that can cause real monetary loss: orphaned exchange positions, missing timeouts, duplicate position risk, and state corruption on restart. These must be resolved before any live trading.

---

## CRITICAL Findings (5)

### C-1: LiveExecutor removes position from tracking BEFORE close order is sent
**Files:** `core/execution/live_executor.py:640-726`
**Domain:** Error Handling / Exchange

```python
with self._lock:
    pos = self._positions.pop(symbol, None)  # Position REMOVED
# ... later ...
try:
    order = ex.create_market_order(symbol, close_side, amount)
except Exception as exc:
    logger.error("close order failed: %s", exc)
    # Falls through to P&L calc and DB write despite failure
```

The position is popped at line 641 BEFORE the close order at line 656. If the close order fails (network error, exchange timeout), the position disappears from internal tracking but **remains open on the exchange**. The code then records a phantom closed trade with incorrect P&L. Result: orphaned live position with real money, no stop-loss monitoring.

**Fix:** Move `_positions.pop()` AFTER confirmed order fill. Add order-state verification on failure.

---

### C-2: `exchange_call()` timeout wrapper exists but is NEVER used — all live CCXT calls have NO timeout
**Files:** `core/execution/exchange_call.py:49-102`, `core/execution/live_executor.py:192,367,578,656`
**Domain:** Error Handling / Exchange

The `exchange_call()` module header says "Every CCXT call goes through exchange_call()" but it is imported ONLY by test files. All production order placement (`create_market_order`) and balance fetches (`fetch_balance`) call CCXT directly with no timeout. A hung exchange API blocks the executor thread indefinitely, preventing all SL/TP checks, new orders, and position updates.

**Fix:** Route all LiveExecutor CCXT calls through `exchange_call()`. Add per-call timeout enforcement.

---

### C-3: SmartOrderExecutor cancel+re-place race condition creates duplicate positions
**Files:** `core/execution/smart_order_executor.py:154-177`
**Domain:** Error Handling

```python
try:
    exchange.cancel_order(order_id, symbol)
except Exception as exc:
    logger.warning("cancel failed (may be filled): %s", exc)
# Immediately places NEW market order without checking if original filled
try:
    market_order = exchange.create_market_order(symbol, side.lower(), amount)
```

If the limit order fills during the cancel attempt, the cancel fails, and the code immediately places a market order — creating 2x intended position size with real money.

**Fix:** After cancel failure, query order status. Only place market order if original is confirmed unfilled/cancelled.

---

### C-4: Hardcoded JWT secret fallback in production Docker Compose
**Files:** `web/docker-compose.prod.yml:78`, `web/backend/app/config.py:67`
**Domain:** Security

```yaml
NEXUS_JWT_SECRET: ${NEXUS_JWT_SECRET:-CHANGE-ME-IN-PRODUCTION-32chars!}
```

The production compose file has a publicly-known fallback JWT secret. If the env var is unset, attackers reading this repo can forge arbitrary JWT tokens. The `validate_settings` check exists but only fires when `debug=False`.

**Fix:** Remove the default value entirely. Fail hard on startup if `NEXUS_JWT_SECRET` is not set.

---

### C-5: `_initial_risk` not serialized — corrupted to 0.0 after restart with breakeven-moved positions
**Files:** `core/execution/paper_executor.py:80,168-188,278-305`
**Domain:** Config / State Management

`PaperPosition._initial_risk` is computed as `abs(entry_price - stop_loss)` in `__init__()` but is **not included in `to_dict()`** and **not restored from JSON**. After restart, if a breakeven move has set SL = entry_price, `_initial_risk` reconstructs as **0.0**. This breaks:
- Auto-partial trigger guard (`pos._initial_risk > 0` — always False)
- R-multiple calculations (division by 0, saved by guard)
- Breakeven logic guard (silently skipped forever)

**Fix:** Add `_initial_risk` to `to_dict()` and restore it in `_load_open_positions()`.

---

## HIGH Findings (16)

### H-1: PaperExecutor has NO threading lock despite shared mutable state
**File:** `core/execution/paper_executor.py` (entire class)
**Domain:** Config / State / Threading

`_positions`, `_closed_trades`, `_capital`, `_peak_capital` are accessed from scanner threads, event bus callbacks, and the Qt UI thread. `LiveExecutor` has `self._lock = RLock()` but PaperExecutor has zero locks. Concurrent dict iteration/modification can cause `RuntimeError` or lost updates.

### H-2: CrashDefenseController accesses executor._positions without lock
**File:** `core/risk/crash_defense_controller.py:208-214`
**Domain:** Threading

Iterates `_positions` without acquiring the executor's lock while `partial_close()` modifies the same dict. Race condition: `RuntimeError: dictionary changed size during iteration`.

### H-3: GUI widget mutations from background threads (3 pages)
**Files:** `gui/pages/intelligence/intelligence_page.py:495-506`, `gui/pages/regime/regime_page.py:577-773`, `gui/widgets/signal_confirmation_widget.py:203-209`
**Domain:** Threading

EventBus callbacks from background threads directly mutate QLabels and create QWidgets. This causes sporadic segfaults, corrupted widget state, and unreproducible crashes.

**Fix:** Emit intermediate Qt Signal with QueuedConnection, or use `QTimer.singleShot(0, ...)`.

### H-4: `settings.set("risk", risk_cfg)` clobbers entire risk section
**File:** `gui/pages/risk_management/risk_page.py:924-932`
**Domain:** Config / State

Replaces the entire `risk` config section with only 5 keys from the UI spinners. The other 3+ keys in DEFAULT_CONFIG.risk are silently erased.

**Fix:** Use `settings.set("risk.max_concurrent_positions", val)` for each key individually.

### H-5: 8 paper_trades migration columns have no ORM model attributes
**Files:** `core/database/engine.py:170-178`, `core/database/models.py:454-495`
**Domain:** Config / State

`_migrate_schema()` adds `strategy_class`, `tqs_score`, `capital_weight`, `signal_age_ms`, `setup_bar_ts`, `trigger_bar_ts`, `gtf_passed`, `execution_quality_score` to the DB, but `PaperTrade` ORM model has none of them. SQLAlchemy cannot read/write these columns.

### H-6: Hardcoded default PostgreSQL credentials
**Files:** `web/docker-compose.prod.yml:35-36`, `web/backend/alembic.ini:90`, `web/backend/app/config.py:62`
**Domain:** Security

User/password both default to `nexus`. Production compose, alembic.ini, and Python config all carry these defaults.

### H-7: SQL injection risk — dynamic table/column names via f-strings
**File:** `core/database/engine.py:182-186`
**Domain:** Security

```python
conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {definition}"))
```

Values come from a hardcoded list (not user input), so not immediately exploitable, but the pattern is dangerous.

### H-8: Unsafe `pickle.load` on disk files
**Files:** `core/learning/probability_calibrator.py:92`, `core/rl/online_trainer.py:421`
**Domain:** Security

`pickle.load` can execute arbitrary code. If an attacker modifies the pickle files, they achieve RCE.

### H-9: PaperExecutor DB write failures silently swallowed on trade close
**File:** `core/execution/paper_executor.py:1416-1445`
**Domain:** Error Handling

Failed DB writes cause in-memory state to diverge from database. On restart, capital/equity calculations are wrong.

### H-10: LiveExecutor DB save failures silently swallowed
**Files:** `core/execution/live_executor.py:730-806`
**Domain:** Error Handling

For a live trading system, losing trade records with only a `logger.warning` is a severe accounting issue.

### H-11: `get_db()` returns unclosed sessions — connection leak risk
**File:** `core/database/engine.py:248-250`
**Domain:** Error Handling

Returns raw `SessionLocal()` with no cleanup mechanism. `get_session()` context manager exists but `get_db()` is also exported.

### H-12: Broad except on position monitor path swallows critical failures
**File:** `core/execution/live_executor.py:114-115`
**Domain:** Error Handling

Failed stop adjustments and position management actions are silently ignored with only a warning log.

### H-13: PaperExecutor _load_open_positions all-or-nothing failure
**File:** `core/execution/paper_executor.py:256-316`
**Domain:** Error Handling

A single corrupted field in `open_positions.json` causes ALL positions to be lost. No per-position error isolation.

### H-14: WebSocketCandleFeed creates unauthenticated ccxt.pro instance
**File:** `core/market_data/websocket_feed.py:188-221`
**Domain:** Exchange

Creates a new ccxt.pro instance with NO credentials, NO demo mode, NO rate limits — instead of using `get_ws_exchange()`.

### H-15: Division by `pos.entry_price` without zero guard in _close_position
**File:** `core/execution/paper_executor.py:1692`
**Domain:** Signal Pipeline

Positions restored from corrupted JSON with `entry_price: 0` would cause `ZeroDivisionError` in the tick processing loop.

### H-16: `notify_telegram.py` requests.post has no timeout
**File:** `notify_telegram.py:7`
**Domain:** Exchange

Blocks indefinitely if Telegram is unreachable.

---

## MEDIUM Findings (24)

| # | Finding | File(s) | Domain |
|---|---------|---------|--------|
| M-1 | bus.publish() from ScanWorker background thread reaches GUI subscribers | `core/scanning/scanner.py:836` | Threading |
| M-2 | bus.publish() from BaseAgent daemon threads reaches GUI | `core/agents/base_agent.py:106,122,169,177` | Threading |
| M-3 | bus.publish(FEED_STATUS) from ConnectivityManager on WS/REST threads | `core/market_data/connectivity_manager.py:214-260` | Threading |
| M-4 | Non-atomic file writes — crash mid-write corrupts data | `paper_executor.py:252`, `trade_monitor.py:260`, `level2_tracker.py:663`, `confluence_scorer.py:182`, `filter_stats.py:83` | Error Handling |
| M-5 | Multiple `except Exception: pass` blocks hiding errors | 12 locations across `paper_executor.py`, `llm_provider.py`, `audit_logger.py`, `engine.py`, `probability_calibrator.py` | Error Handling |
| M-6 | Bare `except:` (catches SystemExit/KeyboardInterrupt) in scripts | `scripts/nexus_full_backtest.py:644-679` | Error Handling |
| M-7 | No `finally` for state persistence in `_close_position` | `core/execution/paper_executor.py:~1041-1058` | Error Handling |
| M-8 | `available_capital` property returns 0.0 on any exception (not logged) | `core/execution/live_executor.py:209-212` | Error Handling |
| M-9 | Notification retry worker lacks graceful shutdown check | `core/notifications/notification_manager.py:615-661` | Error Handling |
| M-10 | `exchange_call.py` config load falls back silently | `core/execution/exchange_call.py:37-46` | Error Handling |
| M-11 | `exec()` on hardcoded code strings | `Check.py:14`, `check_deps.py:14` | Security |
| M-12 | Debug mode exposes full tracebacks to clients | `web/backend/main.py:209-216` | Security |
| M-13 | Debug mode defaults to `true` in dev Docker Compose | `web/docker-compose.yml:71` | Security |
| M-14 | PostgreSQL port exposed to host in dev compose | `web/docker-compose.yml:27-28` | Security |
| M-15 | Hardcoded credentials in `alembic.ini` | `web/backend/alembic.ini:90` | Security |
| M-16 | Redis deployed without authentication | `web/docker-compose.yml:43-44` | Security |
| M-17 | WebSocket JWT token passed as query parameter (logged in access logs) | `web/backend/app/ws/routes.py:27` | Security |
| M-18 | `trading_rules` table in migration but no ORM model exists | `core/database/engine.py:84-85` | Config/State |
| M-19 | Stale `settings.yaml` references in comments/logs; stale file exists | `gui/pages/risk_management/risk_page.py:898,923,935` | Config/State |
| M-20 | `AppSettings.set()`/`save()` has no thread lock for concurrent access | `config/settings.py:750-800` | Config/State |
| M-21 | Position tracking fields (`highest_price`, `bars_held`, etc.) not serialized | `core/execution/paper_executor.py:74-78,168-188` | Config/State |
| M-22 | No HTTP session reuse — every `requests.get()` creates new TCP+TLS | All agent files using `import requests` | Exchange |
| M-23 | No DNS/TLS-specific error handling in any core execution path | All `core/execution/`, `core/agents/` | Exchange |
| M-24 | No application-level rate limiter for `scalp_agent.py` (4 Binance calls/cycle) | `core/agents/scalp_agent.py:349-391` | Exchange |

---

## LOW Findings (10)

| # | Finding | File(s) |
|---|---------|---------|
| L-1 | Telegram bot token placeholder in committed script | `notify_telegram.py:3` |
| L-2 | Hardcoded test passwords in test files | `web/frontend/e2e/helpers.ts:6` |
| L-3 | ILIKE pattern with user input allows wildcard injection | `web/backend/app/api/exchanges.py:529` |
| L-4 | MetricsService returns empty dict on any exception | `core/execution/metrics_service.py:43-47` |
| L-5 | Shadow tracker logs data loss at DEBUG level | `core/scanning/shadow_tracker.py:86-90` |
| L-6 | `_parse_ts` returns 0.0 (epoch) on parse failure | `core/scanning/shadow_tracker.py:384-390` |
| L-7 | Audit logger `clear()` fails silently | `core/strategies/audit_logger.py:202-208` |
| L-8 | Stale "25% of capital" in position_sizer docstring (actual: 4%) | `core/meta_decision/position_sizer.py:8` |
| L-9 | Float `== 0.0` comparisons on accumulated values | `position_sizer.py:173`, `confluence_scorer.py:520` |
| L-10 | ExchangeManager defaults ticker fields to 0 for missing data | `core/market_data/exchange_manager.py:316-356` |

---

## Items PASSING Audit (Signal Pipeline Integrity)

These critical invariants were verified and are correctly implemented:

| Check | Status | Location |
|-------|--------|----------|
| `disabled_models` checked before `model.evaluate()` | PASS | `signal_generator.py:169` |
| `adjusted_score` never used in sizing/SL/TP | PASS | `symbol_allocator.py`, `paper_executor.py` |
| `partial_close()` creates `_closed_trades` + DB row + `TRADE_CLOSED` | PASS | `paper_executor.py:1296-1327` |
| `entry_size_usdt` immutable after position open | PASS | `paper_executor.py:64` |
| `_auto_partial_applied` checked in `on_tick()` and serialized | PASS | `paper_executor.py:1010,186,301` |
| EV gate, MTF conflict rejection, R:R floor implemented | PASS | `risk_gate.py:224-313` |
| Quarter-Kelly and 4% cap enforced | PASS | `position_sizer.py:69,74,208` |
| Candle-boundary timer alignment correct | PASS | `scanner.py:1518-1537` |
| `_any_scan_active` cleared on all completion/error paths | PASS | `scanner.py:2043,2107,2019,2031` |
| `max_size_usdt = 0.0` in ConfluenceScorer | PASS | `confluence_scorer.py:252` |
| `max_capital_pct` uses `self.max_capital_pct` (not hardcoded) | PASS | `position_sizer.py:190,349` |
| Capital load order: JSON first, then DB overwrites | PASS | `paper_executor.py:224-229` |
| CryptoPanic URL is `/api/developer/v2/posts/` | PASS | `sentiment/news_fetcher.py:29` |
| CCXT `enableRateLimit: True` configured | PASS | `exchange_manager.py:150` |
| WebSocket reconnection has exponential backoff | PASS | `ws_client.py:33-36,203,246` |
| BaseAgent staleness detection with confidence decay | PASS | `base_agent.py:78-81,217-222` |
| Scanner OHLCV freshness checks (3x timeframe) | PASS | `scanner.py:902-919` |
| REST poller gap detection with backfill | PASS | `rest_poller.py:242-257` |
| Notification retry with exponential backoff | PASS | `notification_manager.py:68-69,609-654` |
| ExchangeManager separates REST and WS instances | PASS | `exchange_manager.py:177-198,242-253` |
| All `urllib.request.urlopen()` calls have explicit timeouts | PASS | Multiple agent files |
| `.gitignore` excludes vault, env, credential files | PASS | `.gitignore` |
| Key vault encrypts with Fernet AES-256, 0o600 perms | PASS | `core/security/key_vault.py` |
| Logging auto-masks JWT, API keys, passwords | PASS | `logging_config.py` |
| Auth: bcrypt, 12-char min, lockout after 5 attempts | PASS | Web backend auth system |
| ThreadPoolExecutor patterns: all clean (no `with` pattern) | PASS | Multiple files |
| `on_tick()` dispatch via QueuedConnection | PASS | `data_feed.py:334,356` |
| Coinglass agent uses RLock (not Lock) | PASS | `coinglass_agent.py:65` |
| ScanWorker cleanup: `_worker = None` in both complete and error | PASS | `scanner.py:2041,2105,2017,2029` |

---

## Prioritized Remediation Roadmap

### Phase 1: CRITICAL (Block live trading)
1. **C-1:** Move `_positions.pop()` after confirmed close order fill; add order-state verification
2. **C-2:** Route all LiveExecutor CCXT calls through `exchange_call()` with timeout
3. **C-3:** Query order status after failed cancel before placing market order
4. **C-4:** Remove JWT secret default; fail hard if env var unset
5. **C-5:** Serialize `_initial_risk` in `to_dict()` and restore in `_load_open_positions()`

### Phase 2: HIGH (Before production confidence)
6. **H-1:** Add `threading.RLock()` to PaperExecutor for shared state access
7. **H-2:** Acquire executor lock in CrashDefenseController before iterating positions
8. **H-3:** Route GUI EventBus callbacks through Qt Signal with QueuedConnection
9. **H-4:** Use per-key `settings.set("risk.key", val)` instead of section replacement
10. **H-5:** Add 8 missing columns to PaperTrade ORM model
11. **H-6/H-7:** Remove hardcoded DB credentials; parameterize SQL migration
12. **H-8:** Replace `pickle.load` with `torch.load(weights_only=True)` or JSON
13. **H-9/H-10:** Add retry + fallback persistence for DB write failures
14. **H-13:** Per-position try/except in `_load_open_positions()`
15. **H-15:** Add `entry_price > 0` guard in `_close_position`

### Phase 3: MEDIUM (Production hardening)
16. Implement atomic writes (write-to-temp-then-rename) for all JSON persistence
17. Add thread lock to `AppSettings.set()`/`save()`
18. Serialize `highest_price`, `bars_held`, `trailing_stop_pct` in position JSON
19. Add `requests.Session()` for HTTP connection reuse in agents
20. Add ccxt error type differentiation in LiveExecutor (NetworkError vs InvalidOrder)
21. Add Redis authentication in Docker Compose
22. Remove stale `settings.yaml` references

---

## Methodology

Six parallel audit agents examined:
1. **Threading & async safety** — cross-thread Qt calls, ThreadPoolExecutor, locks, asyncio
2. **Error handling & resilience** — exception swallowing, missing finally, retry logic, timeouts
3. **Security** — secrets, SQL injection, deserialization, input validation, Docker config
4. **Configuration & state management** — config load/save, schema migration, serialization
5. **Exchange & network layer** — REST/WS handling, rate limits, connection pooling, retries
6. **Signal pipeline & execution** — model gating, scoring isolation, position management, risk gates

Each agent examined all files under `core/`, `gui/`, `web/`, `scripts/`, and `config/` with grep, read, and cross-reference verification.
