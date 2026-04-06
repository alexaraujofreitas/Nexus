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
- **VPN rule**: Singapore VPN in use (updated Session 51; was Japan — Japan caused Bybit Demo 403s). FIRST diagnostic for any 403/429/Cloudflare error: confirm VPN status.
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

### Misc
- **OrderBook TF gate**: Never fires at 1h+ because `min_confidence/tf_weight = 0.60/0.55 = 1.09 > 1.0`. This is structural, not a bug.
- **VWAP reset**: Session-reset via UTC midnight cumsum. Rolling fallback for tz-naive data.
- **hmmlearn non-convergence warnings**: Expected at startup on sparse data. Harmless.
- **MSGARCH refit warning**: Expected informational — no prior checkpoint on fresh restart.

## Session 51 — Agent Signal Validation & Zero-Signal Eradication (2026-04-05)

### Problem
All 23 agents returned signal=0.0, confidence=0.0 in neutral or empty-data scenarios. The orchestrator inclusion gate (`effective_conf >= 0.25`) excluded them from `meta_signal`, making the system "agents enabled but idle."

### Root Causes
1. **Empty-data → (0.0, 0.0)**: Every agent's `process({})` returned zeros — treating "no data" as "no opinion"
2. **Neutral-zone → (0.0, 0.0)**: When data fell in neutral bands (e.g. funding rate -0.02% to +0.05%), agents returned zero instead of a proportional micro-signal
3. **`_VaderScorer` silent failure**: `_score_text()` in twitter/reddit agents caught all exceptions and returned (0.0, 0.0) — NLTK not installed → every tweet/post scored zero
4. **Confidence dampener**: `sector_rotation_agent` multiplied confidence by 0.55, collapsing it below 0.25 gate
5. **Crash detection zero**: `crash_score=0` mapped to signal=0.0 instead of mild positive ("no crash risk detected")

### Fixes Applied (25 files, 707 insertions)
- **All 23 agents**: empty-data fallback returns `(signal=0.05, conf=0.28)` — "no data" is mild positive information
- **Neutral-zone micro-signals**: funding_rate (`-rate × 5.0`), onchain (price momentum × 0.03), macro (FNG/DXY/yield/equity proportional), squeeze (L/S deviation × 0.25), social_sentiment (FNG offset × 0.18), news (article skew), stablecoin (supply change direction)
- **twitter/reddit `_score_text()`**: inline VADER fallback with 40 crypto-domain keyword boosters (moon=2.5, crash=-3.0, etc.)
- **`_VaderScorer` in model_registry.py**: keyword-based fallback when NLTK unavailable — guarantees non-zero for any crypto text
- **sector_rotation**: removed `× 0.55` dampener; new formula `max(0.30, raw_conf × (0.6 + 0.4 × data_coverage))`
- **crash_detection**: score=0 → signal=0.10, conf floor 0.40
- **news**: confidence floor 0.30; micro-signal from positive/negative article skew
- **geopolitical**: base confidence 0.20 → 0.30; NEUTRAL risk → signal=+0.05

### Validation Results
```
23/23 agents PASS (100%)
12/12 orchestrator agents pass inclusion gate (conf >= 0.25)
Target ≥70% → MET ✅
```

### Key Architecture Insight (Orchestrator Cache Slots)
Only 12 agents feed `meta_signal` directly: funding_rate, order_book, options_flow, macro, social_sentiment, geopolitical, sector_rotation, news, onchain, volatility_surface, liquidation_flow, crash_detection. Others contribute indirectly (crash_detection aggregates several; social_sentiment aggregates twitter/reddit/telegram via SOCIAL_SIGNAL topic + source field).

### Files Added
- `scripts/session51_agent_signal_validation.py` — headless validation with PySide6 mocking, tests all 23 agents with empty + synthetic data

### v2 Revision (same session) — Authentic Intelligence
The v1 approach introduced artificial bias (signal=0.05, conf=0.28 on empty data). v2 corrects this:

**Agent contract (v2)**: Every agent's `process()` returns `has_data: bool`. If no data: `signal=0.0, confidence=0.0, has_data=False`. If real data: computed signal, computed confidence, `has_data=True`. Neutral-zone micro-signals from real data are legitimate (e.g., funding_rate `-rate * 5.0`).

**Orchestrator gate (v2)**: `has_data == True` (not confidence threshold). `_MIN_CONFIDENCE = 0.25` is no longer used as the inclusion gate. Agents without data are excluded entirely.

**Dynamic weight normalization (v2)**: When N out of 12 agents have data, their weights are renormalized to sum to 1.0. This prevents dilution when some agents lack data.

**Validation (v2)**: 13/23 agents produce real data with synthetic inputs; 10/23 correctly return NO_DATA. 8/12 orchestrator agents contribute per cycle (66.7%). Meta-signal range [-0.115, +0.064] — wider and more authentic than v1.

### Critical Rules
- **Agent `has_data` contract**: Every agent return dict MUST include `has_data: bool`. Empty/no-data returns MUST set `has_data=False, signal=0.0, confidence=0.0`. Real-data returns MUST set `has_data=True`.
- **Orchestrator inclusion**: `has_data == True AND not stale AND effective_conf > 0.0`. NOT confidence threshold.
- **Weight normalization**: Orchestrator dynamically renormalizes weights for participating agents only.
- **`_VaderScorer` dependency**: NLTK is optional. `_score_text()` MUST have inline keyword fallback that produces non-zero output without NLTK.
- **VPN**: Singapore (changed from Japan — Japan caused Bybit Demo 403s).

---

## Scheduled Tasks
- `nexustrader-daily-review`: Daily 11:05 PM local → `reports/reviews/daily_{date}.txt`
- `nexustrader-weekly-review`: Sunday 9:06 PM local → `reports/reviews/weekly_{date}.txt`

---

