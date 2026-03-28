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

## Current System State (v1.3 Session 37 — 2026-03-28)

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
| TrendModel | 50.3% | 1.47 | +0.22 | ✅ Active |
| MomentumBreakout | 63.5% | 4.17 | +1.21 | ✅ Active |
| FundingRateModel | — | — | — | ✅ Active (context enrichment, low weight) |
| SentimentModel | — | — | — | ✅ Active (FinBERT/VADER, low weight) |
| PullbackLong | 44.6% | 0.8995 | — | ✅ Active v1.3 (mr_pbl_slc.enabled=true, Stage 8 validated Session 36) |
| SwingLowContinuation | 60.9% | 1.5455 | — | ✅ Active v1.3 (mr_pbl_slc.enabled=true, Stage 8 validated Session 36) |
| MeanReversion | 32.2% | 0.21 | — | ❌ Archived v1.2 (−$18k Study 4) |
| LiquiditySweep | 19.3% | 0.28 | — | ❌ Archived v1.2 (−$15k Study 4) |
| VWAPReversion | — | 0.28 | — | ❌ Archived v1.2 (below 1.0 threshold, 2026-03-24) |
| OrderBook | — | ≤1.0 | — | ❌ Archived v1.2 (structural 1h+ TF gate) |

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

### Test Suite (latest full run — Session 37, 2026-03-28)
- **85 passed**, 0 failed (mr_pbl_slc suite post-fix; full regression from Session 35: 1,652 passed, 11 skipped)
- Session 36 updated: `test_mr_pbl_slc_models.py` — fixed rejection candle helper (`_make_pbl_df` explicit high/low), updated `test_active_regimes_restored` assertions for ACTIVE_REGIMES=[REGIME_BULL_TREND]/[REGIME_BEAR_TREND], corrected settings mock namespace in pos-sizer test
- **Stage 8 runtime validation**: `scripts/stage8_runtime_validation.py` — **52 passed, 0 failed** (13 sections; no runtime fixes required)
- Session 37 added: `test_session37_reset_and_ui_fixes.py` (16 tests) + `test_session37_scan_error_fix.py` (12 tests — 7 original pre-filter fix + 3 UnboundLocalError fix + 2 indicator-presence guard)
- **Full suite (Session 37)**: 1558 passed, 11 skipped, 0 failed (pre-indicator-guard baseline); +2 new tests after indicator guard added

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

---

## Scheduled Tasks
- `nexustrader-daily-review`: Daily 11:05 PM local → `reports/reviews/daily_{date}.txt`
- `nexustrader-weekly-review`: Sunday 9:06 PM local → `reports/reviews/weekly_{date}.txt`

---

## Pre-Session Checklist
Before each Bybit Demo session:
```bash
pytest tests/intelligence/ -v -m "not slow"           # 193 tests, 0 failures required
pytest tests/unit/test_session33_regime_fixes.py -v   # 31 tests, 0 failures required
python scripts/run_ui_checks.py --no-screenshots      # 69 checks, 0 failures required
python scripts/validate_v1_2_parity.py                # v1.2: all PASS required before session
```
