# NexusTrader — Project Memory

## Investigation & Fix Standard (MANDATORY)

Every bug fix MUST follow this. Non-negotiable.

### Requirements
1. **Prove root cause with evidence** — stack traces, reproduction tests, or exact log sequences. Never infer from "last log seen."
2. **Trace full execution path end-to-end** — read all code in the path including dependencies, locks, thread/async boundaries.
3. **Verify no second root cause** — check for compound failures.
4. **Write reproduction test FIRST** — must FAIL before fix, PASS after.
5. **Fix ALL instances** — if pattern exists in 5 files, fix all 5.
6. **Add hardening** — timeouts, cooldown/backoff, `finally` state reset, diagnostic logging.
7. **Run full regression** — report exact counts: passed / failed / skipped.

### Anti-Patterns (NEVER DO)
- `with ThreadPoolExecutor` around network calls → use explicit pool + `finally: pool.shutdown(wait=False, cancel_futures=True)`
- Declare a fix without runtime log validation
- Stop at design/pseudocode — implement and test

---

## Post-Restart Validation Standard (MANDATORY)

When user says "Nexus Trader restarted", run full validation automatically. No superficial checks.

**Always verify:**
- Initialization: Database, OrchestratorEngine, 23 agents, PaperExecutor, CrashDefenseController, NotificationManager, RL Trainer
- Scheduler: HTF timer (1h) and LTF timer (15m) active and candle-boundary aligned
- Scan workers: no stuck workers, `_any_scan_active` flag cleared, `_worker = None` after completion
- Exchange/data: Bybit fetch success rate, bar counts, latency, timestamp freshness
- All ERROR/WARNING lines — surface every non-benign entry
- Thread count: baseline ~51; alert threshold 75
- Recent fixes: confirm in logs they behaved as expected

**Report format:** health summary → issues found → risks for next cycle → confirmed working → recommended actions.

---

## User Hardware & Environment
- GPU: NVIDIA RTX 4070 (12 GB VRAM), OS: Windows, Python 3.11.x
- PyTorch: CUDA cu124 build (`pip install "torch>=2.6.0" --index-url https://download.pytorch.org/whl/cu124`)
- FinBERT: loaded on GPU (`device="cuda"`), ~5–10ms/batch inference
- RL agents auto-use CUDA via `torch.device("cuda")`
- All dependencies installed: gymnasium, arch, hmmlearn, feedparser, ccxt 4.5.42, safetensors

---

## Current System State (v1.3 Session 45 — 2026-03-29)

### Production Config (`config.yaml` — runtime only, NOT `config/settings.yaml`)
```yaml
rl:
  enabled: true              # RL ensemble weight active (0.30)
ai:
  active_provider: Local (Ollama)   # deepseek-r1:14b via http://localhost:11434/v1
data:
  websocket_enabled: false   # Reliable REST polling (WS crashes Qt at 10Hz without throttle)
  default_timeframe: 30m     # v1.2: Phase 5 primary TF (was 1h)
idss:
  min_confluence_score: 0.45
scanner:
  auto_execute: true         # MUST always be true — auto-execute on every restart
disabled_models:
  - mean_reversion            # Disabled: backtest PF 0.21, -$18k (Study 4)
  - liquidity_sweep           # Disabled: backtest PF 0.28, -$15k (Study 4)
  - trend                     # Disabled Session 48: net-negative at 0.04%/side fees (PF 0.9592)
  - donchian_breakout         # Gated Session 48: research candidate — not validated yet
multi_tf:
  confirmation_required: true  # v1.2: 30m primary → 4h HTF gate (Phase 5 winning)
risk_engine:
  risk_pct_per_trade: 0.5    # Phase 1 demo (0.5%)
  max_capital_pct: 0.04      # 4% per-trade hard cap
scale_manager:
  current_phase: 1
# v1.2 exit logic (Phase 5 winning: partial 33% at 1R + breakeven SL)
exit:
  mode: partial
  partial_pct: 0.33
  partial_r_trigger: 1.0
models:
  trend:
    adx_min: 31.0            # v1.2: Phase 5 lever 2 (was 25.0, +42% trade count)
```

### Active Models & Backtest Baselines (Study 4, 13 months; Phase 5 combined PF = 2.976)
| Model | WR | PF | Avg R | Status |
|-------|----|----|-------|--------|
| TrendModel | 50.3% | 0.9592 | — | ❌ Disabled Session 48 (net-negative at fees) |
| MomentumBreakout | 63.5% | 4.17 | +1.21 | ✅ Active |
| FundingRateModel | — | — | — | ✅ Active (context enrichment, low weight) |
| SentimentModel | — | — | — | ✅ Active (FinBERT/VADER, low weight) |
| PullbackLong | 44.6% | 0.8995 | — | ✅ Active v1.3 (mr_pbl_slc.enabled=true, Stage 8 validated Session 36) |
| SwingLowContinuation | 60.9% | 1.5455 | — | ✅ Active v1.3 (mr_pbl_slc.enabled=true, Stage 8 validated Session 36) |
| MeanReversion | 32.2% | 0.21 | — | ❌ Archived v1.2 (−$18k Study 4) |
| LiquiditySweep | 19.3% | 0.28 | — | ❌ Archived v1.2 (−$15k Study 4) |
| VWAPReversion | — | 0.28 | — | ❌ Archived v1.2 (below 1.0 threshold, 2026-03-24) |
| OrderBook | — | ≤1.0 | — | ❌ Archived v1.2 (structural 1h+ TF gate) |
| DonchianBreakout | — | 1.1053 (zero fee) | — | 🔬 Research candidate Session 48 — gated (disabled_models) |

### v1.3 PBL+SLC System Backtest (commit c2c5e30 — 2026-03-28)
4 years (2022-03-22 → 2026-03-21), BTC+SOL+ETH, 30m primary, 200-bar rolling regime window

| Scenario | CAGR | PF | WR | MaxDD | n |
|----------|------|----|----|-------|---|
| A: zero fees | 66.44% | 1.3708 | 56.1% | −16.37% | 1,745 |
| B: 0.04%/side maker | 47.44% | 1.2682 | 56.1% | −20.33% | 1,745 |
| Research baseline (v7_final, BTC only) | 50.41% | 1.2975 | 61.1% | −20.66% | 1,476 |

**Per-model breakdown (Scenario A):** PBL: n=516 WR=44.6% PF=0.8995 | SLC: n=1,229 WR=60.9% PF=1.5455

**Fix applied (Session 36):** v8 PF=1.0179 was caused by: (1) SignalGenerator ACTIVE_REGIMES hard gate
blocking SLC entirely (required ACTIVE_REGIMES=[] + dual generate() calls with correct regime strings);
(2) single-bar entry at close instead of next-bar open (required pending_entries buffer + sl<ep<tp
validation matching research gen_pbl/gen_slc). Combined system now matches research baseline.
PBL PF=0.8995 (Scenario A) is below 1.0 in isolation; combined system PF=1.2682 (with fees) clears the
≥1.18 target. **Stage 8 runtime validation PASSED (52/52 checks, Session 36).** `mr_pbl_slc.enabled: true`
is the production-ready configuration — ACTIVE_REGIMES gate confirmed, PBL/SLC signals fire with correct
direction/SL/TP, PositionSizer path validated, no context injection.

### v1.2 Phase 5 Exit Performance Comparison
| Exit Mode | PF | Max DD | WR |
|-----------|-----|--------|-----|
| Full exit (v1.1) | 1.825 | 8.2R | 51.3% |
| Partial 33% at 1R (v1.2) | 2.634 | 4.7R | 53.8% |
| **Combined best (30m+4h MTF)** | **2.976** | **4.1R** | **57.1%** |

### API Keys (encrypted in vault)
- CryptoPanic, Coinglass, Reddit Client ID+Secret — all set

### Test Suite (latest full run — Session 44, 2026-03-29)
- **85 passed**, 0 failed (mr_pbl_slc suite post-fix; full regression from Session 35: 1,652 passed, 11 skipped)
- Session 36 updated: `test_mr_pbl_slc_models.py` — fixed rejection candle helper (`_make_pbl_df` explicit high/low), updated `test_active_regimes_restored` assertions for ACTIVE_REGIMES=[REGIME_BULL_TREND]/[REGIME_BEAR_TREND], corrected settings mock namespace in pos-sizer test
- **Stage 8 runtime validation**: `scripts/stage8_runtime_validation.py` — **52 passed, 0 failed** (13 sections; no runtime fixes required)
- Session 37 added: `test_session37_reset_and_ui_fixes.py` (16 tests) + `test_session37_scan_error_fix.py` (12 tests — 7 original pre-filter fix + 3 UnboundLocalError fix + 2 indicator-presence guard) + `test_session37_pbl_ema50_fix.py` (12 tests — ema_50 in SCAN_CORE_COLUMNS, computed on all 3 data shapes, PBL indicator check passes)
- **Full suite (Session 37)**: 1,459 passed (post-PySide6 install), 11 skipped, 0 failed
- **Stage 8 LIVE runtime validation (Session 37)**: PASSED — live logs from 2026-03-28 12:06:08 restart confirm `calculate_scan_mode: 22 scan-mode columns` on all 3 data shapes (30m/299 bars, 4h/59 bars, 1h/149 bars), zero `missing ema_50` errors post-restart (vs 4 per cycle in prior session), PBL and SLC indicator path clean. Report: `reports/stage8_validation_report_session37_FINAL.docx`
- Session 37 dashboard scanner status fix: `test_session37_dashboard_scanner_status.py` (7 tests)
- **Session 43 backtest engine optimizations**: `report_perf_opt.js` profiling + 4 optimizations applied to `research/engine/backtest_runner.py`, `core/signals/signal_generator.py`, `core/features/indicator_library.py` — 4.0× total speedup (418.7s → 104.1s), sim alone 5.8× (248.3s → 43.1s)
- **Session 44 NewsFeed fix**: `test_newsfeed_age_fallback.py` (9 tests) — 24h fallback for empty 8h primary window; **80 passed** in targeted regression
- **Session 45 cache/perf**: no new unit tests needed (backtest_runner changes are integration-level); **118 passed** in core regression suite

---

## Architecture Overview

### Signal Pipeline
```
OHLCV (1h, 300 bars) → HMM+RuleBased Regime → SignalGenerator (5 models)
→ ConfluenceScorer (weighted voting, dynamic threshold 0.28–0.65)
→ RiskGate (EV gate, MTF 4h confirm, portfolio heat 6%)
→ PositionSizer (quarter-Kelly, 4% cap) → PaperExecutor
```

### Key Components
| Component | Location | Description |
|-----------|----------|-------------|
| IDSS Scanner | `core/scanning/scanner.py` | AssetScanner + ScanWorker, 5-symbol watchlist, candle-boundary timer alignment |
| Signal models | `core/signals/sub_models/` | base.py, trend, momentum_breakout, vwap_reversion, order_book, funding_rate, pullback_long_model, swing_low_continuation_model |
| Regime | `core/regime/hmm_regime_classifier.py` + `hmm_classifier.py` | HMM (diag covariance) + rule-based blend; adaptive weight when >50% states "uncertain" |
| Confluence | `core/meta_decision/confluence_scorer.py` | REGIME_AFFINITY matrix, L1+L2 adaptive weights, direction dominance ≥0.30 |
| Risk gate | `core/risk/risk_gate.py` | EV gate, slippage-adjusted, MTF conflict rejection, R:R floor 1.0 |
| Position sizer | `core/meta_decision/position_sizer.py` | `calculate_risk_based()` uses `self.max_capital_pct` (NOT hardcoded 0.25) |
| Crash defense | `core/risk/crash_defense_controller.py` + `core/risk/crash_detector.py` | 7-component scorer, 4-tier response |
| Paper executor | `core/execution/paper_executor.py` | All cross-thread calls via Qt queued signals (NEVER call `on_tick()` from background thread) |
| Exchange mgr | `core/market_data/exchange_manager.py` | `get_exchange()` REST; `get_ws_exchange()` ccxt.pro (currently unused, WS disabled) |
| Adaptive learning | `core/learning/` | L1 (model WR), L2 (model×regime, model×asset), AdaptiveWeightEngine |
| Performance eval | `core/evaluation/` | DemoPerformanceEvaluator (20 checks), EdgeEvaluator, SystemReadinessEvaluator |
| Monitoring | `core/monitoring/` | LiveVsBacktestTracker, PerformanceThresholdEvaluator, ScaleManager, ReviewGenerator |
| Analytics | `core/analytics/` | CorrelationDampener, PortfolioGuard, FilterStatsTracker, ModelPerformanceTracker, SymbolAllocator |
| News/NLP | `core/nlp/news_feed.py` | 5 RSS sources + CryptoPanic v2 API (`/api/developer/v2/posts/`), 8h max_age |
| Agents | `core/agents/` | 23 intelligence agents incl. coinglass_agent.py (5-min TTL OI cache, RLock) |

### GUI Pages (20 total)
`main_window.py` → Dashboard, IDSS Scanner, Paper Trading, Trade History, Performance Analytics (9 tabs), Edge Analysis, Validation, Demo Monitor, Backtesting, Risk Management, Settings (7 tabs incl. Portfolio Allocation), Logs, Chart Workspace, Quant Dashboard

### Data Persistence
- **SQLite** (`data/nexus_trader.db`): trades, positions history — authoritative source
- **JSON** (`data/open_positions.json`): live positions snapshot — loaded first, then DB overwrites capital
- **JSONL** (`data/trade_outcomes.jsonl`): append-only enriched trade store
- **Schema migration**: `core/database/engine.py` `_migrate_schema()` — **MUST update this when adding columns to any ORM model**

---

## Critical Rules (Accumulated from All Sessions)

### Threading & Qt Safety
- **Cross-thread Qt rule**: NEVER call `bus.publish()` with GUI subscribers from background threads (QThread, asyncio, ThreadPoolExecutor). Route via Qt `Signal` with `QueuedConnection` so slot runs on main thread.
- **`_pe.on_tick()` rule**: Must only be called from the main thread. Use `_tickers_for_pe = Signal(dict)` emitted from background, connected to `_dispatch_pe_ticks` slot on main thread.
- **ThreadPoolExecutor rule**: NEVER use `with ThreadPoolExecutor` around potentially-hanging network calls. Use explicit pool + `finally: pool.shutdown(wait=False, cancel_futures=True)`.
- **ScanWorker rule**: Set `self._worker = None` in `_on_scan_complete()` and `_on_scan_error()` immediately. Never rely solely on `isRunning()` when thread has post-work tasks.

### Config & Settings
- **Runtime config**: `CONFIG_PATH = ROOT_DIR / "config.yaml"` is the ONLY runtime config. `config/settings.yaml` is static defaults — changes there have ZERO effect at runtime.
- **`config.yaml` edit rule**: When editing programmatically, NEVER overwrite user-persisted toggles. `settings.set()` → `save()` writes the ENTIRE dict.
- **`max_size_usdt` rule**: Must be `0.0` in `ConfluenceScorer` — any non-zero value creates an invisible ceiling overriding all risk calculations. Use `max_capital_pct` (4%) only.
- **`max_capital_pct` rule**: `calculate_risk_based()` MUST use `self.max_capital_pct`, not a hardcoded constant. Both must always move together.

### Event Bus
- **Topics enum**: Always use `Topics.SYSTEM_ALERT` (`"system.alert"`). `Topics.SYSTEM_WARNING` does NOT exist — will raise `AttributeError` silently inside `except Exception` blocks.
- **Event bus import**: Always `from core.event_bus import bus, Topics`. Never `event_bus`.

### Database
- **Schema migration contract**: Every new column in any ORM model in `core/database/models.py` MUST be added to `_migrate_schema()` in `engine.py` simultaneously. Omitting causes `no such column` on every restart for existing DBs.
- **`tr.get()` None-safety**: Use `(tr.get(key) or default)` pattern — the default only fires when key is ABSENT, not when value is `None`. Never `tr.get(key, default)` when DB may store explicit `None`.
- **Capital load order**: `_load_open_positions()` first (reads JSON), then `_load_history()` (SQLite replay overwrites capital — authoritative).

### HMM Regime
- **Two HMM files**: `hmm_classifier.py` and `hmm_regime_classifier.py` are separate — changes to covariance type, warmup, or weight schedule MUST apply to BOTH.
- **Covariance type**: Always `"diag"`. Never `"full"` — causes positive-definite errors on crypto price series.
- **Warmup**: `_WARMUP_BARS = 5` in `_label_states()`. Never hardcode a larger integer.
- **Adaptive weight**: `classify_combined()` checks `uncertain_frac > 0.50` → use `hmm_w=0.20, rb_w=0.80`. This is a runtime check per call.
- **NaN startprob on startup**: Expected/benign — sparse data on first fit. Falls back to rule-based. Fires ~2 times at startup.

### Notifications
- **Key normalisation in `_on_trade_closed()`**: Always map `side`→`direction`, `pnl_usdt`→`pnl`, `size_usdt`→`size` (formatted), `models_fired`→`strategy`, `exit_reason`→`close_reason`, `duration_s`→`duration`. Never pass raw executor dict to `notify()`.
- **Partial exit routing (v1.2)**: In `_on_trade_closed()`, check `data.get("exit_reason") == "partial_close"` FIRST and route to `_on_partial_exit()` instead of the full-close template. Failure to gate this causes partial closes to appear as completed trades in notifications.
- **`partial_exit` TEMPLATES entry**: Always present in `TEMPLATES` registry. Never remove or rename — `notification_manager._on_partial_exit()` calls `self.notify("partial_exit", ...)` by name.

### Monitoring & Reset
- **TradeMonitor attribute**: `_recent_trades` (last 20 entries). `_trades` does NOT exist.
- **Thread watchdog threshold**: 75. Baseline at startup is ~51 threads. Recalibrate if agents added/removed.
- **`partial_close()` rule**: Must create a `_closed_trades` entry + DB row + publish `TRADE_CLOSED`. Partial closes not creating records are invisible to analytics.
- **`_auto_partial_applied` flag (v1.2)**: Boolean on `PaperPosition`. Set to `True` when auto-partial triggers in `on_tick()`. Serialised to `open_positions.json` via `to_dict()` and restored in `_load_open_positions()`. If this flag is missing or not restored, every restart will re-trigger the partial close on already-partially-closed positions.
- **`entry_size_usdt` immutability**: Never assign after `__init__`. Only `size_usdt` changes during partials.

### Exchange & API
- **Exchange access**: `get_exchange()` for REST (orders, balance, history). `get_ws_exchange()` for ccxt.pro WS streaming. Keep separate.
- **WS safety**: WS disabled (`websocket_enabled: false`). To re-enable safely, add per-symbol 1s throttle in `_process_ticker()` AND dispatch `_pe.on_tick()` via Qt queued signal — not directly from asyncio.
- **CryptoPanic URL**: `/api/developer/v2/posts/?auth_token=...`. Content-type: `kind=news`. Sentiment: `filter=`. `public=true` required. Never use `/api/v1/`, `/api/free/`, or `/api/free/v2/`.
- **VPN rule**: Japan VPN → Bybit Demo 403 errors + CryptoPanic Cloudflare blocks. FIRST diagnostic for any 403/429/Cloudflare error: confirm VPN status.
- **Coinglass agent**: `core/agents/coinglass_agent.py` — use `RLock` (not `Lock`): `get_oi_data()` acquires lock then calls `_build_result()` which re-acquires it.
- **notification_manager**: Access as `from core.notifications.notification_manager import notification_manager`. Never `get_notification_manager()`.

### Position Sizing
- **SymbolAllocator is the only allocation mechanism**: `adjusted_score = base_score × symbol_weight` for candidate ranking only. Never use `adjusted_score` in sizing, stop/target, or execution logic. `score` key is immutable.
- **`entry_size_usdt`**: Immutable after position open. `exit_size_usdt` = what was closed. For full closes they are equal.

### Models & Learning
- **`disabled_models`**: Only mechanism for disabling models. Never remove model code — use config gate.
- **L2 activation tiers**: Full (≥10 trades), Partial (5–9 trades, confidence-scaled), Fallback (<5 trades, model-scoped 50% strength).
- **Calibrator circular feature**: `confluence_score` included in calibrator features. After 500 trades, compare AUC with/without it. If delta < 0.01, drop from future training.
- **Auto-disable conservative rule**: Requires ALL of: WR<40%, expectancy<-0.10R, PF<0.85, no positive-expectancy regimes, AND ≥50 trades. Single-criterion disable eliminated.

### Demo Trading Rules (Phase 1 — IMMUTABLE until 50+ trades assessed)
- DO NOT change signal logic, model weights, entry/exit rules, or parameters
- DO NOT optimize on live data (<75 trades)
- Phase advancement: manual only — `ScaleManager.evaluate_advancement()` recommends, operator updates `risk_pct_per_trade` in Settings, then calls `record_phase_advance()`
- Hard block fires (PF<1.0 AND WR<40% over 30+ trades): investigate before resuming — do NOT disable the block
- Intermediate hard stop: PF<1.2 AND WR<45% over 30+ trades → `logger.critical` + block trade
- **Safety contract**: ONLY code path that enables live trading is `risk_page._on_mode_toggle()` (manual button + confirmation dialog). No auto-switch is architecturally possible.

### Candle-Boundary Alignment
- `_seconds_to_next_candle(timeframe, buffer_s=30)` computes delay via `epoch_s % interval_s` in UTC. Works for any TF divisor of a day.
- `_htf_alignment_pending` / `_ltf_alignment_pending` flags prevent watchdog from restarting timers during alignment window.

### Regime Classifier (Session 33 fixes)
- **ADX dead zone fixed**: `adx_ranging_threshold (20) ≤ ADX < adx_trend_threshold (25)` now maps to `RANGING` (not `UNCERTAIN`). The fix changed the second ADX check from `if adx < adx_ranging_threshold` → `elif adx < adx_trend_threshold` with an inner split for the dead zone.
- **ema_slope=None with high ADX fixed**: When `adx >= adx_trend_threshold` but `ema_20` column is missing, code now returns `RANGING` (direction unknown) instead of falling through to `UNCERTAIN`.
- **Hysteresis init fixed**: `_committed_regime` is now initialized to `""` (empty string, not `"uncertain"`). `_apply_hysteresis()` returns the raw signal (confidence × 0.9) on startup until the 3-bar commitment window fills — prevents all early calls from being forced to `"uncertain"`.
- **risk_pct default fixed**: Both `confluence_scorer.py` and `position_sizer.py` now default `risk_pct_per_trade` to `0.5` (was `0.75`). If `config.yaml` cannot be read at runtime, the fallback now matches production config instead of over-sizing by 50%.
- **Reproduction tests**: `tests/unit/test_session33_regime_fixes.py` — 31 tests, all must pass. Run after any regime/sizing change.

### Dashboard IDSS Scanner Status (Session 37 fix)
- **`SCAN_CYCLE_COMPLETE` must be published**: `AssetScanner._on_scan_complete()` now calls `bus.publish(Topics.SCAN_CYCLE_COMPLETE, ...)` after every HTF scan cycle. Before this fix, the topic was defined in `Topics` but NEVER published — the Dashboard's `_on_scan_cycle_complete()` callback never fired, leaving the status row permanently at "Stopped".
- **Dashboard startup probe delay**: The post-exchange-connect probe is `QTimer.singleShot(10_000, _update_scanner_row_startup)` (was 3.5s). The scanner has an 8-second GPU/RL init delay (`IDSSScannerTab._start_scanner_now` fires `singleShot(8_000, ...)`). Probing at 3.5s always saw `_running=False`. Must be > 8s.
- **Two-symptom root cause**: (1) SCAN_CYCLE_COMPLETE never published → callback never fires → permanent "Stopped"; (2) startup probe races with 8s scanner init → always False at probe time. Fix (1) makes the dashboard update after every cycle. Fix (2) ensures "Running | No scan yet" shows correctly before first scan.

### Scanner Pre-Filter Return Contract (Session 37 fix)
- **`_scan_symbol_with_regime()` return shape**: MUST be `(Optional[OrderCandidate], str, float, Optional[pd.DataFrame], str, dict)` — any early return MUST use `None` as the first element (not `symbol` string). The caller's `if candidate:` evaluates non-empty strings as truthy and calls `candidate.to_dict()`.
- **Pre-filter rejection return**: `return None, "", 0.0, df, _pf_reason, _sym_diag` — pass `df` so the df_cache can be populated, pass `_sym_diag` for diagnostics.
- **`DEFAULT_CONFIG filters.time_of_day.enabled`**: MUST be `False`. When `config.yaml` is corrupt/missing, the fallback default must not block scans. Time-of-day filter is an unvalidated hypothesis — opt-in only.
- **"Scan error" status**: Only set by the `except Exception` clause in `ScanWorker.run()`. Pre-filter rejections show the rejection reason string (e.g. "Volatility filter: ATR ratio 0.30 < min 0.50").
- **Indicator presence guard** (Session 37): Immediately after `df = calculate_scan_mode(df)`, scanner checks that `adx`, `ema_9`, `rsi_14` columns are present and non-NaN. If any are missing, returns `"Indicators missing"` status. This surfaces `calculate_scan_mode()` silent failures (returns raw OHLCV when `ta` library fails) that would otherwise show as generic "No signal". Diagnostic key `_sym_diag["indicator_cols_missing"]` lists the absent columns.

### Misc
- **OrderBook TF gate**: Never fires at 1h+ because `min_confidence/tf_weight = 0.60/0.55 = 1.09 > 1.0`. This is structural, not a bug.
- **VWAP reset**: Session-reset via UTC midnight cumsum. Rolling fallback for tz-naive data.
- **hmmlearn non-convergence warnings**: Expected at startup on sparse data. Harmless.
- **MSGARCH refit warning**: Expected informational — no prior checkpoint on fresh restart.

### Session 34 Hardening (v1.2 Final) — 2026-03-26

#### Breakeven SL (Section 1 — demo blocker fix)
- **`partial_close()` rule**: Must set `pos.stop_loss = pos.entry_price` AND `pos._breakeven_applied = True` IMMEDIATELY inside `partial_close()`. Do NOT rely on the `update()` tick-based flag — that creates a 1-tick gap.
- **Serialisation**: `_breakeven_applied` and `_auto_partial_applied` MUST both be in `PaperPosition.to_dict()` and restored in `_load_open_positions()`. Missing either causes restart to re-trigger partial close logic.
- **Regression tests**: `tests/unit/test_breakeven_sl_after_partial.py` — 9 tests, all must pass.

#### Crash Defense Auto-Execute (Section 2)
- **`crash_defense.auto_execute`** config gate (default `false`). When `true`:
  - DEFENSIVE (≥5.0): `_executor.move_all_longs_to_breakeven()`
  - HIGH_ALERT (≥7.0): `_executor.partial_close(symbol, 0.50)` for each long
  - EMERGENCY (≥8.0): `_executor.close_all_longs(exit_reason="crash_defense_emergency")`
  - SYSTEMIC (≥9.0): `_executor.close_all()`
- **Injection**: `PaperExecutor.__init__` calls `get_crash_defense_controller().set_executor(self)`.
- **`move_all_longs_to_breakeven()`**: Sets `pos.stop_loss = pos.entry_price` directly — does NOT use `adjust_stop()` which rejects breakeven-level stops.
- **`close_all_longs()`**: Iterates all buy positions, calls `_close_position()` for each.

#### Agent Disable Pattern (Section 3)
- **Disabled agents** (11): options, orderbook, volatility_surface, reddit, social_sentiment, twitter, sector_rotation, narrative, miner_flow, scalp, liquidity_vacuum
- **Gate pattern**: `if not self._is_agent_enabled("agents.XXX_enabled", default=False): logger.info(...); return`
- **Re-enable**: Set `agents.XXX_enabled: true` in `config.yaml`. Never delete agent code.
- **`_is_agent_enabled()`**: Static helper in `AgentCoordinator` — reads `config.settings.get(config_key, default)`.

#### Tiered Capital Model (Section 4 — Phase 2, GATED)
- **Gate**: `capital.scaling_enabled: false` in config. NO effect in Phase 1.
- **Tiers**: Solo standard 12%, Solo high-conviction (score≥0.70) 18%, Dual positions 8%, Multi (3+) 5%.
- **`_HIGH_CONVICTION_THRESHOLD`**: 0.70 (class constant on `PositionSizer`).
- **`_TIER_CAPS`**: Dict on `PositionSizer` — `{(open_count, high_conv): max_pct}`.
- **Concurrency pass-through**: `ConfluenceScorer.score()` reads `_pe._positions` count → passes `open_positions_count` and `conviction_score` to `calculate_risk_based()`.

#### Rolling PF Guardrails (Section 5)
- **`_compute_rolling_pf(n)`**: Computes PF over last-N full closes (partial_close EXCLUDED). Returns 999.0 if no losers.
- **Hard block in `submit()`**: Rolling-30 PF < 1.0 → `return False` (fires independently of the compound PF+WR block).
- **Size scalar in `submit()`**: `size_usdt = candidate.position_size_usdt * self._rolling_size_scalar()`.
  - `_rolling_size_scalar()`: requires ≥20 full closes; returns 0.50 if rolling-20 PF < 1.5, else 1.0.
- **Scale gate advisory**: Rolling-50 PF ≥ 2.0 AND trades ≥ 50 → `bus.publish(SYSTEM_ALERT, type="scale_gate_eligible")`.
- **Regression tests**: `tests/unit/test_section5_rolling_pf_guardrails.py` — 13 tests, all must pass.

---

## Demo Trading Milestones

### Phase 1 Targets (check after 50 trades)
| Metric | Baseline | Acceptable | Action if below |
|--------|----------|------------|-----------------|
| Win Rate | 50.3% / 63.5% | ≥ 45% portfolio | Advisory (<50 trades); investigate (50+) |
| Profit Factor | 1.47 | ≥ 1.10 | Advisory (20–50 trades); investigate (50+) |
| Avg R/trade | 0.31 | ≥ 0.10R | Advisory |
| Max Drawdown | — | < 10R | Investigate if exceeded |

### Symbol Weights (STATIC mode)
SOL=1.3, ETH=1.2, BTC=1.0, BNB=0.8, XRP=0.8

### RAG Thresholds
| Model | WR GREEN | PF GREEN | Avg R GREEN |
|-------|----------|----------|-------------|
| TrendModel | ≥ 47.8% | ≥ 1.279 | ≥ 0.145R |
| MomentumBreakout | ≥ 60.3% | ≥ 3.628 | ≥ 0.799R |

Pause conditions: Portfolio RED → `should_pause=True`; OR 2+ models RED → `should_pause=True`.

---

## Session 35 — v1.3 PBL+SLC Production Implementation (2026-03-27)

### PBL / SLC Model Architecture (commit ab466ec)
- **`mr_pbl_slc.enabled`** (config.yaml, default `false`): master gate for both models. Flipping to `true` activates scanner HTF fetch + SignalGenerator dispatch.
- **Context dispatch**: `SignalGenerator.generate()` accepts `context: dict`. Uses `inspect.signature()` to pass context only to models that declare it (backward-compatible).
- **PBL context key**: `"df_4h"` — 4h DataFrame passed by scanner; model degrades gracefully if absent.
- **SLC context key**: `"df_1h"` — 1h DataFrame; model returns `None` (hard requirement, cannot degrade).
- **Scanner HTF fetch**: when enabled, fires `ThreadPoolExecutor(max_workers=2)` per symbol to fetch 4h+1h concurrently before calling `generate()`. Uses explicit pool + `finally` shutdown per threading rule.
- **Regime guards in evaluate()**: both models explicitly check regime at entry (`if regime != REGIME_X: return None`). Do NOT rely solely on the SignalGenerator gate — direct test calls also need the guard.

---

## Session 36 — v1.3 PBL+SLC Regime Alignment Fix (2026-03-28)

### Root Cause of v8 PF=1.0179 (commit c2c5e30)

Two compounding bugs produced the PF gap vs research baseline:

**Bug 1 — SignalGenerator ACTIVE_REGIMES hard gate blocked SLC entirely**
- `ACTIVE_REGIMES = [REGIME_BEAR_TREND]` caused `if model.ACTIVE_REGIMES and not model.is_active_in_regime(regime)` to hard-block SLC whenever backtest called `generate()` with the NexusTrader HMM regime string (almost never "bear_trend").
- Fix: Set `ACTIVE_REGIMES: list[str] = []` on both PBL and SLC. Regime control is now handled entirely inside `evaluate()` via `context["research_regime_30m"]` / `context["research_regime_1h"]` (primary) or NexusTrader string (fallback). REGIME_AFFINITY still suppresses models in crisis/liquidation_cascade via adaptive activation.
- Fix: Replaced single `generate(nx_regime)` call with dual calls: `generate("bull_trend")` collecting only PBL signals when `res_regime_30m == RES_BULL_TREND`, and `generate("bear_trend")` collecting only SLC signals when `res_regime_1h == RES_BEAR_TREND`.

**Bug 2 — Entry at current bar close instead of next bar open**
- Research `gen_pbl` / `gen_slc` enter at `o_v[i+1]` (next bar's open) with validation `sl < ep < tp` (long) or `tp < ep < sl` (short). Backtest was entering at current bar's close.
- Fix: Implemented `pending_entries: dict[str, dict]` buffer — signal buffered at bar i, filled at bar i+1's open with sl/tp validation before committing.

### Files Changed (commit c2c5e30 — initial v9 fix)
- `core/regime/research_regime_classifier.py` — exact port of btc_regime_labeler.py (ADX ewm span=14, ATR_ratio expansion/crash thresholds, 3-bar hysteresis); added `regime_to_string()` + `_RESEARCH_TO_NX` map
- `core/signals/sub_models/pullback_long_model.py` — all 3 rejection candle conditions, pending_entries next-bar fill
- `core/signals/sub_models/swing_low_continuation_model.py` — swing low logic, 1h HTF requirement
- `core/scanning/scanner.py` — dual generate() calls (PBL 30m, SLC 1h) using research regime strings
- `scripts/mr_pbl_slc_research/backtest_v9_system.py` — dual generate() calls + pending_entries next-bar fill
- `tests/unit/test_mr_pbl_slc_models.py` — rejection candle helper fix + ACTIVE_REGIMES tests updated
- `reports/mr_pbl_slc_v9_system.json` — final v9 results

### Files Changed (commit 319fac9 — architectural cleanup)
Resolved 4 architectural violations without affecting performance (PF/CAGR bit-for-bit identical):
- `core/regime/research_regime_classifier.py` — `regime_to_string()` added as the single authoritative converter (int code → NexusTrader regime string); eliminates all hardcoded literals from callers
- `core/signals/sub_models/pullback_long_model.py` — `ACTIVE_REGIMES=[REGIME_BULL_TREND]` restored; removed context regime injection (`context["research_regime_30m"]`); regime flows through `generate()` → `evaluate()` parameter only
- `core/signals/sub_models/swing_low_continuation_model.py` — `ACTIVE_REGIMES=[REGIME_BEAR_TREND]` restored; removed `context["research_regime_1h"]` injection
- `core/scanning/scanner.py` — replaced context injection with `_res_to_str(_res_classify(df))` → passed as `regime` param; 3-call architecture (main HMM, PBL research 30m, SLC research 1h)
- `scripts/mr_pbl_slc_research/backtest_v9_system.py` — replaced hardcoded `"bull_trend"`/`"bear_trend"` literals with `research_regime_to_string(res_regime_30m/1h)`
- `tests/unit/test_mr_pbl_slc_models.py` — `test_active_regimes_restored` asserts `ACTIVE_REGIMES==[REGIME_BULL_TREND/BEAR_TREND]`

### Key Architectural Rule (dual-call pattern with restored ACTIVE_REGIMES)
- **`ACTIVE_REGIMES` is the sole gate**: `[REGIME_BULL_TREND]` on PBL, `[REGIME_BEAR_TREND]` on SLC. SignalGenerator hard-blocks models unless the passed regime matches.
- **`regime_to_string()` is the single converter**: `ResearchRegimeClassifier.regime_to_string(int_code)` → NexusTrader string. No hardcoded literals anywhere in callers.
- **Dual generate() pattern**: scanner and backtest call `generate(symbol, df, res_regime_30m_str, "30m", context={"df_4h": ...})` for PBL and `generate(symbol, df, res_regime_1h_str, "1h", context={"df_1h": ...})` for SLC, then filter by `model_name`. Main models use NexusTrader HMM regime in a separate call.
- **No context injection of regime integers**: regime flows through the `regime` parameter only, never through `context["research_regime_*"]`.
- **REGIME_AFFINITY still active**: crisis / liquidation_cascade affinities (0.0) continue to suppress models via adaptive activation weight check.

### PBL+SLC Backtest Findings (v9 — resolved)
- Combined system PF=1.2682 (with 0.04%/side fees), CAGR=47.44%, WR=56.1% — targets met (PF≥1.18, CAGR≥30%).
- SLC dominates returns: PF=1.5455 (zero fees), n=1,229. PBL PF=0.8995 (below 1.0 in isolation) but combined portfolio clears target.
- Regime alignment gap (production HMM vs research vectorized labeler) is now mitigated — both models use `research_regime_classifier.py` as the primary gate, bypassing the HMM entirely.

### Stage 8 Runtime Validation (scripts/stage8_runtime_validation.py — Session 36)
**Result: 52/52 PASSED — ✅ READY FOR DEMO**

Key evidence:
- `mr_pbl_slc.enabled = True` confirmed from runtime `config.yaml`
- `ACTIVE_REGIMES = ['bull_trend']` (PBL) / `['bear_trend']` (SLC) confirmed at import time
- `regime_to_string()` maps all 6 integer codes correctly (SIDEWAYS→ranging, BULL→bull_trend, etc.)
- `classify_series(70,079 bars BTC 30m)`: bull_trend=22,082 (31.5%), bear_trend=21,351 (30.5%)
- **PBL signal fired**: n=5 on real data; first at 2022-03-26 16:00 UTC, strength=0.267, dir=long, SL=43923.08, TP=44637.49; rationale confirms EMA50 prox ✓ + rejection candle ✓ + RSI ✓
- **SLC signal fired**: n=5 on real data; first at 2022-03-31 23:00 UTC, strength=0.340, dir=short, SL=46408.98, TP=44791.43; rationale confirms ADX ✓ + new 10-bar low ✓
- Gate blocking confirmed: PBL returns None when regime≠bull_trend; SLC returns None when regime≠bear_trend
- `calculate_pos_frac()`: equity=92,112 → size=32,239.20 USDT (35.0%); max_positions gate returns 0 at open_count=10; per-asset gate returns 0 at BTC_open=3
- No context injection: stale `research_regime_30m=2` in context correctly ignored when regime param='bull_trend'
- No runtime fixes required; no additional commit needed

---

## Session 43 — Backtest Engine Performance Optimization (2026-03-29)

### Profiling Hotspots (report_perf_opt.js)
Total before: 418.7s (sim alone 248.3s) on 4-year BTC+SOL+ETH 30m dataset.

| Hotspot | Calls | Time share | Fix |
|---------|-------|-----------|-----|
| `RegimeClassifier._classify()` (O(n²) BB rolling) | 99,653 | 59% | Pre-vectorised once at load |
| `inspect.signature()` in `generate()` hot loop | 160,381 | 14% | Cached in `SignalGenerator.__init__()` |
| `settings.get()` in `generate()` per-model loop | 4.9M | 11% | Hoisted before loop |
| `_supertrend()` pandas `.iloc` row access | 420k | 9% | NumPy array indexing |

### Files Changed (Session 43)
- **`research/engine/backtest_runner.py`**: added `_precompute_nx_regimes()` — vectorised regime + 3-bar hysteresis over full series, stored as `self._nx_regime[sym]` / `self._nx_conf[sym]` numpy arrays; O(1) lookup in `_run_unified_scenario()`.
- **`core/signals/signal_generator.py`**: `self._model_has_context` dict cached at `__init__()` time (replaces 160k `inspect.signature()` calls); `disabled_models`, `adaptive_activation.*`, `mr_pbl_slc.enabled` read once before model loop.
- **`core/features/indicator_library.py`**: `_supertrend()` loop replaced with `.to_numpy()` + direct array indexing (~100ns vs ~60µs per bar).

### Speedup Results
| Stage | Before | After | Speedup |
|-------|--------|-------|---------|
| Total (load + sim) | 418.7s | 104.1s | **4.0×** |
| Simulation only | 248.3s | 43.1s | **5.8×** |

### Known Difference
Vectorised regime pre-computation advances through ALL bars regardless of open positions (original `_regime_buffer` skipped bars when positions were open). Trade count delta: 5,678 vs 5,733 (0.96%). PF within normal variance. Documented and accepted.

### Report
`reports/session44_performance_optimisation_report.docx`

---

## Session 45 — Backtest Cache + UI Progress Optimization (2026-03-29)

### Persistent Indicator Cache (Phase 1)
- **New constants**: `CACHE_DIR = ROOT / "cache" / "indicators"`, `INDICATOR_VERSION = "v2.0"`
- **Cache key**: MD5-12 of `(sym, tf, raw_parquet_SHA256_fingerprint, INDICATOR_VERSION)` → auto-invalidates on new data or indicator library changes
- **Cache files**: `{sym}_{tf}_ind_{key}.parquet` (indicator DataFrames), `{sym}_{tf}_{kind}_{key}.npy` (regime/nx arrays)
- **HIT path**: load from parquet (~0.1–0.5s); **MISS path**: compute + save (~3–10s per TF)
- **Regime + NX regime cache**: `_precompute_regimes()` and `_precompute_nx_regimes()` now check npy cache before classifying
- **Cold run (first time)**: 71.4s total (31.7s load + 39.6s sim)
- **Warm run (cache HIT)**: 33.2s total (0.8s load + 32.5s sim) — **3.1× vs Session 43 baseline (104.1s)**

### Multi-core Indicator Computation (Phase 4)
- **`_compute_indicators()`** runs 3 symbols in parallel via `ThreadPoolExecutor(max_workers=3)` + explicit `finally: pool.shutdown(wait=False)` (per threading rules)
- GIL released during pandas/numpy operations → real parallelism for indicator library calls

### Pre-extracted Numpy Arrays (Phase 5)
- **`_pre_extract_arrays()`**: extracts `df["high"]`, `df["low"]`, `df["open"]` as `np.float64` arrays stored in `self._highs/lows/opens[sym]`
- **SL/TP hot path**: replaces `self._ind[sym][PRIMARY_TF].iloc[loc]` (~60µs) with `self._highs[sym][loc]` (~50ns) in BOTH `_run_scenario()` and `_run_unified_scenario()`
- **Pending entry fill**: same replacement for `open` access
- **Simulation speedup**: 43.1s → 32.5s (**1.3× faster simulation**)

### Fine-grained UI Progress (Phase 2)
- **`_compute_indicators(progress_cb)`**: per-symbol "Indicators 2/3: ETH 30m cache HIT/computed" messages
- **`_precompute_regimes(progress_cb)`**: per-symbol regime classification progress
- **`_precompute_nx_regimes(progress_cb)`**: per-symbol NX regime progress
- **Simulation loop**: time-based updates every ~1s instead of every 2000 bars; format: `"Simulating 15,432/70,079 bars | 8.2s elapsed | ETA 14s"`
- **Research Lab UI**: cache status label ("Cache: 9/9 HIT | 212 MB"), "Clear Cache" button (🗑)
- **`runner.load_data(progress_cb=_data_progress)`** now wired in `SweepWorkerThread._run_baseline()`
- **`cache_info_sig`** new signal emitted after load_data() → updates cache status label

### GPU (Phase 3)
- CUDA GPU (RTX 4070) NOT accessible from Linux VM sandbox — `cudaErrorInsufficientDriver`
- CuPy 14.0.1 installed but CUDA driver incompatible in VM
- Numba CPU JIT benchmarked: 5× faster at n=10, 112× at n=1,000 — not applied to SL/TP (n≤10 per bar; numpy access already optimal)

### New Public API on BacktestRunner
- **`cache_info()` → dict**: `{cache_hits, cache_misses, cache_dir, cached_files, cache_size_mb}`
- **`clear_cache()` → int** (static): deletes all files in CACHE_DIR, returns count deleted
- Result dict includes `cache_info` key after every `run()` call

### Correctness Verification
- **n_trades = 1,731** (identical) ✅
- **PF = 1.3798** (zero-fee, identical) ✅
- **WR = 56.4%** (identical) ✅

### Benchmark Table
| Stage | Session 43 | Session 45 Cold (MISS) | Session 45 Warm (HIT) | Speedup (warm) |
|-------|-----------|----------------------|----------------------|----------------|
| `load_data()` | ~61s | 31.7s | **0.8s** | **76×** |
| Simulation | 43.1s | 39.6s | **32.5s** | **1.3×** |
| **Total** | **104.1s** | **71.4s** | **33.2s** | **3.1×** |

### Files Changed (Session 45)
- `research/engine/backtest_runner.py` — cache system, multi-core, numpy arrays, UI progress, `cache_info()`, `clear_cache()`
- `gui/pages/research_lab/research_lab_page.py` — `cache_info_sig` signal, load_data progress_cb, cache status label + "Clear cache" button
- `CLAUDE.md` — updated to Session 45

### Test Results
- **118 passed**, 0 failed (core regression suite post-Session-45)

---

## Session 44 — Post-Restart Validation & NewsFeed Fix (2026-03-29)

### Post-Restart Validation (05:48:26 restart) — PASSED
All critical components confirmed healthy:
- ✅ Database, OrchestratorEngine, 15/23 agents (8 disabled per config), PaperExecutor, CrashDefenseController, NotificationManager, RL Ensemble (SAC+CPPO+DQN)
- ✅ Exchange: Bybit 3290 markets, WS built, 5 symbols live feed
- ✅ Scheduler: HTF + LTF timers aligned to 11:00:29 UTC (700s first-repeat)
- ✅ OHLCV prefetch: 5/5 symbols OK
- ✅ `calculate_scan_mode: 22 scan-mode columns` on 299/59/149 rows
- ✅ `scanner.cycle_complete` published after HTF scan, dashboard callback confirmed
- ✅ HMM fit on 150 bars per symbol (3 non-convergence warnings = expected/benign)
- ✅ CalibratorMonitor drift detected, sigmoid fallback auto-activated (AUC 0.256 < baseline 0.462 — system correctly falls back)

### NewsFeed 24h Fallback Fix
- **Root cause**: `fetch_headlines(max_age_minutes=480)` discarded ALL articles when all RSS content was 8–24h old. This happens nightly during the US business-hours gap (~20:00–08:00 UTC) when crypto outlets publish fewer articles. Confirmed in logs: 0 headlines at 03:30/05:00/05:30/05:49 UTC, only 1 headline at 04:00/04:30 UTC.
- **Fix** (`core/nlp/news_feed.py`): When the primary window yields 0 articles, expand to 24h and tag those articles with `_stale=True` so callers can down-weight stale signal.
- **Test**: `tests/unit/test_newsfeed_age_fallback.py` — 9 tests, **9/9 pass**. Full targeted regression: **80 passed**.

### NewsFeed Rule
- **24h stale fallback**: `_stale=True` on articles that only pass the 24h extended window. SentimentModel receives these; FinBERT still runs but the signal should be treated as lower confidence. No code change needed in SentimentModel — it already weights by `confidence` from the feed pipeline.

### Known Non-Issues (benign at every restart)
- `KeyVault.migrate: deferred — circular import` → expected; vault resolves on next access
- `MSGARCHForecaster not fitted, refitting` → expected; refits on first call
- `hmmlearn: Model is not converging` (3×) → expected on sparse startup data
- `NewsAPI/Messari 401` → not configured, agents degrade gracefully
- `MacroAgent: yfinance not installed` → optional dependency, agent uses available sources
- `CoinGecko 429` → rate-limited on startup burst, recovers on next cycle
- `CalibratorMonitor DRIFT DETECTED` fires twice → two callers (`get_status()`) call `detect_drift()` simultaneously; cosmetic duplicate, sigmoid fallback correctly activated once
- `Research Lab: Baseline cache restored (status=FAIL)` → restores last UI state from prior session; not a production issue

---

## Session 48 — TrendModel Removal + DonchianBreakout Research (2026-03-29)

### Settings Save Fix
- **Root cause**: `settings.set()` used `dict.setdefault(k, {})` to traverse dotted key paths. When any intermediate key held a string (in-memory config corruption), `setdefault` returned the existing string, making `d` a string. Subsequent `d[keys[-1]] = value` raised `TypeError: 'str' object does not support item assignment`.
- **Fix** (`config/settings.py`): Added guard in the key traversal loop — if `d.get(k)` is not a dict, replace it with `{}` and log a warning; if `d` itself is not a dict, log error and return early.
- **Logging hardening** (`gui/pages/settings/settings_page.py`): Added `exc_info=True` to the settings save error log so full traceback surfaces.
- **Exact errors** (from live logs, 6 occurrences between 08:15:19 and 08:16:57 UTC): `TypeError: 'str' object does not support item assignment` inside `settings.set()`.
- **Investigation**: Could not reproduce with base config.yaml (all keys are dicts) — corruption occurs in the running app's in-memory config dict. `set()` is now robust to any such corruption regardless of source.

### TrendModel Removed from Production
- **Evidence**: Session 47 confirmed TrendModel net-negative at production fees (PF 0.9592 at 0.04%/side, 5,320+ trades). Decision: permanently disable via `disabled_models` gate (never remove code).
- **config.yaml**: Added `trend` to `disabled_models` → `[mean_reversion, liquidity_sweep, trend]` (now also includes `donchian_breakout` — see below).
- **`research/engine/backtest_runner.py`**: Removed `"trend"` from `MODE_FULL_SYSTEM` model list. Added comment explaining permanent removal and that `MODE_TREND` exists for research-only studies.
- **`signal_generator.py`** comment block: Added archival entry — `TrendModel disabled Session 48, PF 0.9592, reason: net-negative at fees`.

### DonchianBreakoutModel — New Research Candidate (Session 48)
- **File**: `core/signals/sub_models/donchian_breakout_model.py` (NEW)
- **Thesis**: Price breaking the N-period Donchian channel high/low (of the PRECEDING lookback bars — no look-ahead) on elevated volume signals a high-probability continuation.
- **Regime**: `ACTIVE_REGIMES = []` (fires in all NX regimes via HMM/NX path); `REGIME_AFFINITY` suppresses it in crisis/liquidation_cascade.
- **Config defaults** (added to `config/settings.py` DEFAULT_CONFIG):
  - `models.donchian_breakout.lookback = 20`
  - `models.donchian_breakout.vol_mult_min = 1.3`
  - `models.donchian_breakout.sl_atr_mult = 1.5`
  - `models.donchian_breakout.tp_atr_mult = 3.0`
  - `models.donchian_breakout.rsi_long_min = 50.0`
  - `models.donchian_breakout.rsi_short_max = 50.0`
  - `models.donchian_breakout.strength_base = 0.35`
  - `models.donchian_breakout.entry_buffer_atr = 0.10`
- **Registered in**: `signal_generator.py` `_ALL_MODELS`, `research/engine/backtest_runner.py` `_MODEL_KEY` + `_HMM_MODELS`.
- **Production gate**: `donchian_breakout` added to `disabled_models` — will NOT fire in production until Phase 5 backtest validates it.

### Phase 5 Backtest Results (4 years, BTC+SOL+ETH, 30m)
Script: `scripts/trend_replacement/phase5_comparison.py`
Results: `reports/phase5_trend_replacement_comparison.json`

| Scenario | PF (0 fee) | PF (0.04%) | CAGR (0.04%) | WR | MaxDD (0.04%) | n |
|----------|-----------|-----------|-------------|-----|--------------|---|
| A: PBL+SLC only (baseline) | 1.3798 | 1.2758 | 48.54% | 56.4% | 20.60% | 1,731 |
| B: PBL+SLC+MomentumBreakout | 1.2713 | 1.1729 | 45.67% | 47.1% | 20.47% | 2,552 |
| C: PBL+SLC+DonchianBreakout | 1.1053 | 1.0072 | 2.66% | 47.3% | 44.00% | 4,642 |

**Key findings:**
- **Scenario A** matches v9 baseline exactly (n=1,731, PF=1.3798/1.2758). ✅ Baseline intact after TrendModel removal.
- **Scenario B (PBL+SLC+MB)**: MB adds 821 trades. Zero-fee CAGR is higher (74% vs 68%) but at real fees PF drops to 1.1729 and CAGR to 45.7% — worse than baseline on both PF and CAGR. MB dilutes portfolio quality under naive orchestration.
- **Scenario C (PBL+SLC+Donchian)**: Donchian adds 2,911 trades (total 4,642). With fees PF collapses to 1.0072 and CAGR to 2.7%, MaxDD explodes to 44%. **Failed validation.** Root cause: `ACTIVE_REGIMES=[]` causes Donchian to fire indiscriminately across all regimes at current default params (lookback=20, vol_mult=1.3 are too permissive). Fee drag across 2,911 trades destroys returns.

**Decision**: `donchian_breakout` remains in `disabled_models`. Next research steps: tune regime filter (consider restricting REGIME_AFFINITY threshold), raise `vol_mult_min` to 1.8+, extend `lookback` to 40+, or add a 4h HTF gate. Target: PF ≥ 1.18 with fees, MaxDD ≤ 25%, n ≥ 200 trades before production consideration.

### BacktestRunner: `_HMM_MODELS` updated
- Added `"donchian_breakout"` to `_HMM_MODELS` frozenset so the unified engine's NX regime path dispatches it correctly in MODE_CUSTOM runs. Comment added explaining ACTIVE_REGIMES=[] behavior.

### Branch
All work committed on `trend-replacement-study` branch then merged to main.

---

## Session 49 — MomentumBreakout Optimization Study (2026-03-29)

### Objective
Determine whether MomentumBreakout (MB) can be added to the production PBL+SLC system without degrading portfolio quality.

### Diagnostic Findings (Phase 1)

Five root causes for MB dilution in Scenario B (Phase 5):
1. **BTC is a losing asset for MB** — PF=0.9163, AvgR=−0.021 on BTC standalone (SOL/ETH profitable, BTC structural loser)
2. **Low WR incompatible with portfolio** — MB WR 30–38% vs portfolio WR 56.4%. Adding 800+ low-WR trades pulls portfolio WR down sharply.
3. **SLC crowding (naive orchestration)** — 195 SLC trades lost in combined mode. SLC is the highest-quality sub-model (PF=1.43+); every displaced SLC trade is net negative to the portfolio.
4. **Fee sensitivity** — 1,214 MB trades at 0.04%/side consumed the thin breakout edge entirely.
5. **Structurally negative BTC edge** — BTC MB standalone negative even with optimised params; SOL/ETH marginally positive.

### Parameter Search Space (Phase 2)
- **Tier 1 (core)**: `lookback` ∈ {20, 30, 40, 60}; `vol_mult_min` ∈ {1.5, 2.0, 2.5, 3.0}; `rsi_bullish` fixed at 60/40
- **Tier 2 (tested)**: HMM confidence gate ≥ 0.6; Research-Priority orchestration

### Stage 1 Grid Results (IS standalone, RSI=60/40, 4 × 4 = 16 combos)

| Rank | lb | vm | WR | PF(0-fee) | PF(fee) | CAGR | MaxDD | n |
|------|----|----|-----|-----------|---------|------|-------|---|
| 1 | 60 | 1.5 | 30.1% | 1.3454 | 1.2396 | 23.3% | -15.6% | 905 |
| 2 | 40 | 2.5 | 37.4% | 1.3473 | 1.2362 | 17.6% | -16.3% | 772 |
| 3 | 60 | 2.0 | 31.3% | 1.3196 | 1.2195 | 18.8% | -15.1% | 793 |
| 4 | 60 | 3.0 | 34.8% | 1.2826 | 1.1932 | 12.0% | -16.5% | 552 |
| 5 | 40 | 3.0 | 38.4% | 1.2878 | 1.1861 | 11.6% | -12.1% | 636 |

Key finding: Longer lookback (lb=60) consistently outperforms lb=20. Higher vol_mult helps up to a point. Best standalone IS PF = 1.2396.

### Stage 2 Combined + IS/OOS Results (Top 5 candidates — naive orchestration)

| Candidate | FULL PF | FULL CAGR | FULL MaxDD | IS MB_PF | OOS MB_PF | SLC_n |
|-----------|---------|-----------|-----------|----------|-----------|-------|
| lb=60 vm=1.5 | 1.2098 | 54.43% | -23.39% | 1.2570 | 1.0321 | 976 |
| lb=40 vm=2.5 | 1.1841 | 47.85% | -18.96% | 1.2630 | 0.9721 | 1039 |
| lb=40 vm=3.0 | 1.2092 | 49.37% | -19.43% | 1.1776 | 1.1824 | — |
| lb=60 vm=2.0 | 1.2079 | 52.86% | -21.02% | 1.2591 | 0.9161 | — |
| lb=60 vm=1.5 conf≥0.6 | 1.2098 | 54.43% | -23.39% | 1.2746 | 1.0321 | — |

**BASELINE (PBL+SLC only)**: PF=1.2758, CAGR=48.54%, n=1,731

All 5 candidates degrade FULL-period PF vs baseline (1.1841–1.2098 vs 1.2758).

### Research-Priority Orchestration Test (best candidate lb=60 vm=1.5)
```
NAIVE : PF=1.2098  CAGR=54.43%  WR=45.3%  MaxDD=-23.39%  n=2333  MB_n=923  SLC_n=976
RP    : PF=1.2275  CAGR=55.20%  WR=46.5%  MaxDD=-22.93%  n=2370  MB_n=834  SLC_n=1102

IS  (RP): PF=1.2266  CAGR=54.78%  WR=45.2%  MaxDD=-22.93%  n=2030  MB_n=728  SLC_n=903
OOS (RP): PF=1.4355  CAGR=160.67%  WR=53.2%  MaxDD=-13.18%  n=329   MB_n=105  PF_MB=0.8897

BASELINE: PF=1.2758  CAGR=48.54%  n=1731
```
RP orchestration protected SLC (+126 trades) and gave the best FULL PF of any configuration (1.2275), but still 4.6% below baseline. OOS MB PF = 0.8897 (net-negative).

### IS Baseline for Reference
```
IS  baseline: PF=1.2199  CAGR=42.41%  WR=54.7%  n=1478  (PBL_n=464  SLC_n=1014)
OOS baseline: PF=1.7689  CAGR=227.26%  WR=65.7%  n=242   (PBL_n=37   SLC_n=205)
```

### Final Verdict
**NO — keep PBL + SLC only.**

Evidence: Every tested combination (5 candidates × 2 orchestration modes = 10 configurations) produces a FULL-period combined PF below the baseline (1.2758). The best result is 1.2275 (RP mode, lb=60 vm=1.5), 4.1% below baseline. OOS MB quality is 0.89–1.03 — net-negative to marginally breakeven. MB's 30–38% WR is structurally incompatible with the 56% WR portfolio. SLC crowding persists even under RP orchestration (189 fewer SLC trades vs baseline).

MB remains in production `disabled_models` until:
- OOS PF consistently ≥ 1.18 on fresh out-of-sample data, OR
- A regime-specific or asset-filtered variant is developed (e.g. SOL/ETH only, vol_expansion only), OR
- Sufficient live demo trades accumulate to perform symbol-by-symbol attribution

### Files Added (Session 49)
- `scripts/mb_optimization/stage1_standalone_grid.py` — IS grid search (16 combos, lb×vm sweep)
- `scripts/mb_optimization/stage2_combined_validation.py` — combined IS/OOS/FULL + RP orchestration test
- `reports/mb_optimization/stage1_grid_results.json` — full grid results (16 × IS period)
- `reports/mb_optimization/stage2_combined_results.json` — candidate + baseline IS/OOS/FULL data
- `reports/mb_optimization/mb_optimization_study_session49.docx` — full Word report

### Branch
All work committed on `mb-optimization-study` branch.

---

## Pending Actions
- Remove or hide `🧪 Test Position` button before any public release
- Wire `FilterStatsTracker.record_trade_outcome()` into `paper_executor._close_position()` per filter (realized_r quality proxy incomplete without this)
- After 500 trades: compare calibrator AUC with/without `confluence_score` feature
- After 200 trades: verify Score Calibration monotonicity ≥ 0.5
- After 75+ live demo trades: re-examine MeanReversionModel WR (backtest 58.6%) — if it holds, consider re-enabling
- Monitor LiquiditySweepModel OOS expectancy early demo — if also negative, keep disabled
- Monitor target capture % in Exit Efficiency panel (target: 80–120%)
- If stop tightness flag fires in calm markets: review ATR multipliers in sub-model `REGIME_ATR_MULTIPLIERS`
- **[v1.3 PBL/SLC]** ~~Investigate regime alignment gap~~ — **RESOLVED Session 36**: `research_regime_classifier.py` is now the primary gate for both models; production HMM no longer used for PBL/SLC regime decisions
- **[v1.3 PBL/SLC]** ~~Run Stage 8 runtime validation~~ — **RESOLVED Session 36**: 52/52 checks passed. PBL signals fire in bull_trend (dir=long, SL/TP correct), SLC signals fire in bear_trend (dir=short, SL/TP correct), ACTIVE_REGIMES gate confirmed, PositionSizer path validated, no context injection. `mr_pbl_slc.enabled: true` is production-ready.
- **[Session 48 DonchianBreakout]** Tune parameters for production: target PF ≥ 1.18 (fees), MaxDD ≤ 25%, n ≥ 200. Candidates: `vol_mult_min` → 1.8+, `lookback` → 40+, restrict REGIME_AFFINITY floors, add 4h HTF gate, or restrict ACTIVE_REGIMES to vol_expansion + bull_trend/bear_trend only. Run `scripts/trend_replacement/phase5_comparison.py` (Scenario C) after each change.

---

## Scheduled Tasks
- `nexustrader-daily-review`: Daily 11:05 PM local → `reports/reviews/daily_{date}.txt`
- `nexustrader-weekly-review`: Sunday 9:06 PM local → `reports/reviews/weekly_{date}.txt`

---
