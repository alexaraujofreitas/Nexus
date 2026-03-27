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

## Current System State (v1.2 — 2026-03-26)

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
| MeanReversion | 32.2% | 0.21 | — | ❌ Archived v1.2 (−$18k Study 4) |
| LiquiditySweep | 19.3% | 0.28 | — | ❌ Archived v1.2 (−$15k Study 4) |
| VWAPReversion | — | 0.28 | — | ❌ Archived v1.2 (below 1.0 threshold, 2026-03-24) |
| OrderBook | — | ≤1.0 | — | ❌ Archived v1.2 (structural 1h+ TF gate) |

### v1.2 Phase 5 Exit Performance Comparison
| Exit Mode | PF | Max DD | WR |
|-----------|-----|--------|-----|
| Full exit (v1.1) | 1.825 | 8.2R | 51.3% |
| Partial 33% at 1R (v1.2) | 2.634 | 4.7R | 53.8% |
| **Combined best (30m+4h MTF)** | **2.976** | **4.1R** | **57.1%** |

### API Keys (encrypted in vault)
- CryptoPanic, Coinglass, Reddit Client ID+Secret — all set

### Test Suite (latest full run — Session 33, 2026-03-23)
- **1,611 passed**, 11 skipped (GPU), 0 failures — unit / intelligence / learning / evaluation / backtesting / validation

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
| Signal models | `core/signals/sub_models/` | base.py, trend, momentum_breakout, vwap_reversion, order_book, funding_rate |
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

### Misc
- **OrderBook TF gate**: Never fires at 1h+ because `min_confidence/tf_weight = 0.60/0.55 = 1.09 > 1.0`. This is structural, not a bug.
- **VWAP reset**: Session-reset via UTC midnight cumsum. Rolling fallback for tz-naive data.
- **hmmlearn non-convergence warnings**: Expected at startup on sparse data. Harmless.
- **MSGARCH refit warning**: Expected informational — no prior checkpoint on fresh restart.

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

## Pending Actions
- Remove or hide `🧪 Test Position` button before any public release
- Wire `FilterStatsTracker.record_trade_outcome()` into `paper_executor._close_position()` per filter (realized_r quality proxy incomplete without this)
- After 500 trades: compare calibrator AUC with/without `confluence_score` feature
- After 200 trades: verify Score Calibration monotonicity ≥ 0.5
- After 75+ live demo trades: re-examine MeanReversionModel WR (backtest 58.6%) — if it holds, consider re-enabling
- Monitor LiquiditySweepModel OOS expectancy early demo — if also negative, keep disabled
- Monitor target capture % in Exit Efficiency panel (target: 80–120%)
- If stop tightness flag fires in calm markets: review ATR multipliers in sub-model `REGIME_ATR_MULTIPLIERS`

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
