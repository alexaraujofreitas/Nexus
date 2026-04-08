# Phase 1B — Design Element to Code Mapping

**Date:** 2026-04-06
**Phase:** 1B of 9 (Architecture Baseline & Dependency Audit)
**Inputs:** NEXUSTRADER_INTRADAY_REDESIGN_v1.md, NEXUSTRADER_PROFITABILITY_HARDENING_ADDENDUM_v1.md, NEXUSTRADER_FINAL_ADDENDUM_v1.md
**Status:** Complete

---

## Legend

| Action | Meaning |
|--------|---------|
| **REPLACE** | Existing file is removed from the execution path; new file takes its place |
| **ADAPT** | Existing file is modified in-place to support new requirements |
| **NEW** | No existing equivalent; entirely new module |
| **RETAIN** | Used as-is (no changes needed) |
| **DECOUPLE** | Existing logic is correct but must have Qt dependency removed |
| **ARCHIVE** | Moved out of active import paths; kept for reference/tests |

---

## 1. HEADLESS CORE (V1 §3.1 — Pure-Python EventBus, Qt Decoupling)

### 1.1 EventBus (V1 §3.1, §3.7)

| Design Element | Current File | Action | Target File | Notes |
|---|---|---|---|---|
| Pure-Python EventBus (no Qt) | `core/event_bus.py` | **REPLACE** | `core/event_bus.py` | Remove `QObject` inheritance, `Signal(object)`, `PySide6` import. Keep `threading.RLock`, `defaultdict(list)` subscriber pattern. Add `asyncio`-compatible `publish_async()`. Qt GUI adapter layer subscribes via bridge. |
| Topics enum | `core/event_bus.py` (lines 18–152) | **ADAPT** | `core/event_bus.py` | Add new intraday topics: `CANDLE_1M`, `CANDLE_3M`, `CANDLE_5M`, `CANDLE_15M`, `SETUP_QUALIFIED`, `TRIGGER_FIRED`, `SIGNAL_EXPIRED`, `GTF_BLOCKED`, `TQS_SCORED`, `CLASS_DEGRADED`, `CLASS_SUSPENDED`. Remove unused agent topics (19 agent-specific topics for archived agents). |
| Event dataclass | `core/event_bus.py` (lines 154–163) | **RETAIN** | — | `Event(topic, data, source, timestamp)` is already decoupled from Qt. |

### 1.2 BaseAgent Qt Decoupling (V1 §10)

| Design Element | Current File | Action | Target File | Notes |
|---|---|---|---|---|
| Agent base (no QThread) | `core/agents/base_agent.py` | **DECOUPLE** | `core/agents/base_agent.py` | Replace `QThread` with `threading.Thread`. Remove `Signal(dict)`, `Signal(str)` Qt signals. Keep fetch→process→publish loop and backoff logic. Only 4 agents retained (see §2 below). |
| AgentCoordinator (no QObject) | `core/agents/agent_coordinator.py` | **DECOUPLE** | `core/agents/agent_coordinator.py` | Remove `QObject` parent, `Signal(dict)`. Keep lifecycle management. Reduce agent list from 23 to 4. |
| 19 archived agents | `core/agents/{whale,miner_flow,narrative,twitter,reddit,telegram,scalp,squeeze_detection,stablecoin,liquidity_vacuum,liquidation_intelligence,social_sentiment,sector_rotation,geopolitical,macro,news,options_flow,onchain,volatility_surface}_agent.py` | **ARCHIVE** | `core/agents/_archived/` | Move out of active imports. AgentCoordinator only starts 4 retained agents. |
| 4 retained agents | `core/agents/{funding_rate,liquidation_flow,crash_detection,order_book}_agent.py` | **DECOUPLE** | Same paths | Remove QThread→Thread. Keep existing logic. These provide context enrichment only (not on execution path). |

### 1.3 OrchestratorEngine (V1 §5.3)

| Design Element | Current File | Action | Target File | Notes |
|---|---|---|---|---|
| Orchestrator removed from execution path | `core/orchestrator/orchestrator_engine.py` | **ADAPT** | Same | Remove `QObject` parent. Reduce from 12 agent weight slots to 4. Meta-signal becomes advisory context (not gating). Remove from `SignalGenerator` dependency chain. |
| Orchestrator direction in SignalGenerator | `core/signals/signal_generator.py` (lines 140–151) | **ADAPT** | — | Remove `_orch_ref` lazy import and `orch_direction`/`orch_meta` injection. New StrategyBus does not depend on orchestrator. |

### 1.4 PaperExecutor / ExecutionManager (V1 §8)

| Design Element | Current File | Action | Target File | Notes |
|---|---|---|---|---|
| Headless ExecutionManager | `core/execution/paper_executor.py` | **REPLACE** | `core/execution/execution_manager.py` | No Qt signals. No main-thread requirement. Pure position tracking + order simulation. Retains PaperPosition logic (entry, SL, TP, partial close, trailing). Adds intraday-specific: time stop per strategy, adaptive time stop (Addendum §7). |
| PaperPosition dataclass | `core/execution/paper_executor.py` (lines 35–82) | **ADAPT** | `core/execution/execution_manager.py` | Add fields: `strategy_class`, `tqs_score`, `signal_age_ms`, `capital_weight`, `entry_bar_ts`. Remove v1.2 `_auto_partial_applied` (new exit logic replaces it). |
| Order router | `core/execution/order_router.py` | **ADAPT** | Same | Route to new ExecutionManager instead of PaperExecutor. |

### 1.5 main.py Headless Mode (V1 §3.1)

| Design Element | Current File | Action | Target File | Notes |
|---|---|---|---|---|
| Headless entry point | `main.py` | **ADAPT** | `main.py` + `core/engine.py` | Extract all non-GUI initialization into `core/engine.py::NexusEngine`. `main.py` imports `NexusEngine` then optionally starts Qt GUI. Headless mode: `python main.py --headless`. |
| Qt GUI as optional observer | `gui/` (60+ files) | **RETAIN** (Phase 7) | — | GUI subscribes to EventBus via a Qt bridge adapter. Zero changes to GUI in Phases 2–6. Phase 7 adds intraday dashboard. |

---

## 2. DATA ENGINE (V1 §4 — WebSocket + CandleBuilder)

### 2.1 WebSocket-First Data Ingestion

| Design Element | Current File | Action | Target File | Notes |
|---|---|---|---|---|
| DataEngine (WS primary, REST fallback) | `core/market_data/exchange_manager.py` + `core/scanning/scanner.py` | **NEW** | `core/data/data_engine.py` | Manages ccxt.pro WS subscriptions for 1m klines on all 16 assets. Publishes `CANDLE_1M` events. REST fallback on WS disconnect (3-miss threshold). Uses `ExchangeManager.get_ws_exchange()` (already built). |
| CandleBuilder (1m → all TFs) | None (currently fetches each TF separately via REST) | **NEW** | `core/data/candle_builder.py` | Derives 3m, 5m, 15m, 1h from 1m stream. Publishes `CANDLE_3M`, `CANDLE_5M`, `CANDLE_15M`, `CANDLE_1H` on close. Maintains rolling buffers per symbol per TF. |
| Historical 1m backfill | `core/market_data/exchange_manager.py::fetch_ohlcv()` | **ADAPT** | `core/data/data_engine.py` | On startup, fetch 300× 1m candles per symbol via REST to seed CandleBuilder buffers. Use existing `fetch_ohlcv()` method. |
| OHLCV cache | `core/scanning/scanner.py::_OHLCVCache` | **ARCHIVE** | — | Replaced by CandleBuilder's in-memory rolling buffers. TTL-based cache logic no longer needed (WS provides real-time updates). |
| ExchangeManager | `core/market_data/exchange_manager.py` | **RETAIN** | — | Singleton stays. `get_exchange()` for REST orders/balance. `get_ws_exchange()` for WS streaming. No structural changes needed. |
| WebSocket feed | `core/market_data/websocket_feed.py` | **ARCHIVE** | — | Replaced by DataEngine's ccxt.pro integration. |

### 2.2 AssetScanner Replacement

| Design Element | Current File | Action | Target File | Notes |
|---|---|---|---|---|
| Timer-based scanner | `core/scanning/scanner.py` (AssetScanner + ScanWorker) | **REPLACE** | `core/data/data_engine.py` | No more timer-based scanning. DataEngine emits candle events; StrategyBus reacts to them. Event-driven, not poll-driven. |
| WatchlistManager | `core/scanning/watchlist.py` | **ADAPT** | Same | Expand from 5-symbol watchlist to 16-asset universe. Add tier support (Active+/Active/Reduced/Dormant per Addendum §6). |
| UniverseFilter | `core/scanning/universe_filter.py` | **ADAPT** | Same | Update liquidity/spread/ATR thresholds for intraday (tighter spread requirement: 0.10% vs current). |
| Closed candle guard | `core/scanning/closed_candle_guard.py` | **ARCHIVE** | — | CandleBuilder only emits on actual candle close. Guard logic moves into CandleBuilder. |
| BTC priority filter | `core/scanning/btc_priority.py` | **ADAPT** | Same | Reframe for intraday context. BTC bias check on 15m/1h, not 4h. |

---

## 3. STRATEGY ENGINE (V1 §5 — Five Intraday Strategies + Two-Stage Pipeline)

### 3.1 Strategy Base and Sub-Models

| Design Element | Current File | Action | Target File | Notes |
|---|---|---|---|---|
| BaseSubModel | `core/signals/sub_models/base.py` | **ADAPT** | `core/strategies/intraday/base_strategy.py` | New base class: `BaseIntradayStrategy`. Add two-stage interface: `evaluate_setup(symbol, df_5m, df_15m, regime) → SetupSignal` and `evaluate_trigger(symbol, df_1m, setup, microstructure) → TriggerSignal`. Add `STRATEGY_CLASS` attribute ("breakout"/"pullback"/"mean_reversion"). Add `MAX_SIGNAL_AGE_S` and `PRICE_DRIFT_THRESHOLD_ATR` for signal expiry (Final Addendum §3). |
| SignalGenerator | `core/signals/signal_generator.py` | **REPLACE** | `core/strategies/strategy_bus.py` | New `StrategyBus` subscribes to `CANDLE_5M` for Stage A setup evaluation and `CANDLE_1M` for Stage B trigger evaluation. Replaces model loop with strategy registry pattern. No orchestrator dependency. No warmup guard (regime warmup handled by RegimeEngine). |
| ModelSignal dataclass | `core/meta_decision/order_candidate.py` | **ADAPT** | `core/strategies/signal_types.py` | Split into `SetupSignal` (Stage A output) and `TriggerSignal` (Stage B output). SetupSignal carries bias_score, direction, regime, invalidation conditions. TriggerSignal carries entry_price, SL, TP, trade_score, microstructure snapshot. |
| OrderCandidate | `core/meta_decision/order_candidate.py` | **ADAPT** | Same | Add fields: `strategy_class`, `tqs_score`, `capital_weight`, `signal_age_ms`, `setup_bar_ts`, `trigger_bar_ts`. Remove `higher_tf_regime` (replaced by `bias_regime_15m` + `bias_regime_1h`). |

### 3.2 Five New Strategies (V1 §5.2)

| Strategy | Current Equivalent | Action | Target File | Notes |
|---|---|---|---|---|
| MX (Momentum Expansion) | `momentum_breakout_model.py` | **NEW** (inspired by) | `core/strategies/intraday/mx_strategy.py` | 5m setup: ADX≥25 + EMA alignment + volume surge. 1m trigger: close above 5m high with volume confirm. Different logic than MomentumBreakout (which uses 30m bars and 4h confirmation). |
| VR (VWAP Reclaim/Rejection) | `vwap_reversion_model.py` (archived) | **NEW** (clean rewrite) | `core/strategies/intraday/vr_strategy.py` | 5m setup: price within 0.3% of VWAP + volume rising. 1m trigger: reclaim/rejection candle pattern at VWAP. Not a revival of the archived model (which had PF 0.28). |
| MPC (Micro Pullback Continuation) | `pullback_long_model.py` | **NEW** (inspired by) | `core/strategies/intraday/mpc_strategy.py` | 5m setup: trend EMA stack (8>21>50) + pullback to 8/21 EMA zone. 1m trigger: rejection from EMA zone. Shorter timeframe version of PBL but with fundamentally different entry mechanics. |
| RBR (Range Breakout Reclaim) | None | **NEW** | `core/strategies/intraday/rbr_strategy.py` | 15m setup: identify range bounds (Donchian 20 + volume profile). 5m trigger: break above/below range → pullback → reclaim. |
| LSR (Liquidity Sweep Reclaim) | `liquidity_sweep_model.py` (archived) | **NEW** (clean rewrite) | `core/strategies/intraday/lsr_strategy.py` | 5m setup: identify liquidity level (swing high/low cluster). 1m trigger: price sweeps level, then reclaims within 3 bars. Not a revival of archived model. |

### 3.3 Existing Swing Strategies

| Model | Current File | Action | Notes |
|---|---|---|---|
| TrendModel | `core/signals/sub_models/trend_model.py` | **ARCHIVE** | Already disabled (Session 48). Move to `_archived/`. |
| MomentumBreakoutModel | `core/signals/sub_models/momentum_breakout_model.py` | **ARCHIVE** | Replaced by MX. Different timeframe and logic. |
| PullbackLongModel | `core/signals/sub_models/pullback_long_model.py` | **ARCHIVE** | Replaced by MPC. Different timeframe hierarchy. |
| SwingLowContinuationModel | `core/signals/sub_models/swing_low_continuation_model.py` | **ARCHIVE** | No intraday equivalent (bear swing continuation doesn't fit 10-90min holds). |
| DonchianBreakoutModel | `core/signals/sub_models/donchian_breakout_model.py` | **ARCHIVE** | Was research candidate. Range logic partially informs RBR. |
| FundingRateModel | `core/signals/sub_models/funding_rate_model.py` | **ARCHIVE** | Replaced by retained funding_rate_agent (context only). |
| SentimentModel | `core/signals/sub_models/sentiment_model.py` | **ARCHIVE** | FinBERT/VADER too slow for intraday. Removed from execution path. |
| Archived models (MR, VWAP, LS, OB) | `core/signals/sub_models/{mean_reversion,vwap_reversion,liquidity_sweep,order_book}_model.py` | **RETAIN** (already archived) | Already in disabled state. No action needed. |

---

## 4. REGIME ENGINE (V1 §6)

| Design Element | Current File | Action | Target File | Notes |
|---|---|---|---|---|
| Fast regime (5m + 15m) | `core/regime/regime_classifier.py` | **ADAPT** | `core/regime/regime_engine.py` | Rename to RegimeEngine. Run on 5m candle close (fast) and 15m candle close (bias). Drop 4h dependency. Keep 8-state regime set. Current rule-based logic (ADX, EMA slope, BB width, volume, RSI) works on any TF — just needs 5m/15m DataFrames. |
| HMM classifier | `core/regime/hmm_classifier.py` + `hmm_regime_classifier.py` | **ADAPT** | Same files | Retrain on 5m bars (currently trained on 30m/1h). Keep `"diag"` covariance. Warmup bars adjusted for 5m (100 bars = 8.3h vs current 100 bars = 50h on 30m). |
| Ensemble classifier | `core/regime/ensemble_regime_classifier.py` | **ADAPT** | Same | Combine rule-based (5m) + HMM (5m) as before. No structural change. |
| MS-GARCH forecaster | `core/regime/ms_garch_forecaster.py` | **RETAIN** | — | Volatility forecasting still useful. Refit on 5m data. |
| Regime constants | `core/regime/regime_classifier.py` (lines 31–51) | **RETAIN** | — | All 12 regime states remain. No additions needed. |

---

## 5. CONFLUENCE & RISK (V1 §7, §8)

### 5.1 Confluence Scoring

| Design Element | Current File | Action | Target File | Notes |
|---|---|---|---|---|
| ConfluenceScorer | `core/meta_decision/confluence_scorer.py` | **ADAPT** | Same | Simplify: remove 12-model weighted voting. New logic: single strategy produces SetupSignal → StrategyBus computes `bias_score` from regime + TF alignment + strategy-specific quality metrics. Threshold: bias_score ≥ 0.35 (Stage A). REGIME_AFFINITY matrix updated for 5 new strategies. |
| PositionSizer | `core/meta_decision/position_sizer.py` | **ADAPT** | Same | Update risk parameters: 0.25% per trade (down from 0.5%), 8 max concurrent (up from 5). Keep `calculate_risk_based()` method. Add `capital_weight` input from Capital Concentration Engine. |

### 5.2 Risk Gate

| Design Element | Current File | Action | Target File | Notes |
|---|---|---|---|---|
| RiskGate | `core/risk/risk_gate.py` | **ADAPT** | Same | Update limits: max_concurrent=8, max_spread=0.10%, min_rr=1.5 (up from 1.3). Add per-symbol limit (1 position per symbol). Add daily loss limit check (-2%). Add drawdown escalation tiers (-5%/-8%/-10%/-15%). |
| CrashDefenseController | `core/risk/crash_defense_controller.py` | **RETAIN** | — | 4-tier system unchanged. Already decoupled from Qt. |
| CrashDetector | `core/risk/crash_detector.py` | **RETAIN** | — | 7-component scorer unchanged. |
| CorrelationController | `core/portfolio/correlation_controller.py` | **ADAPT** | Same | Update pre-computed correlation matrix for 16-asset universe (currently covers ~19 pairs). |

---

## 6. PROFITABILITY HARDENING (Addendum §2–§10)

### 6.1 Global Trade Filter (Addendum §2)

| Design Element | Current Equivalent | Action | Target File | Notes |
|---|---|---|---|---|
| GTF system | None | **NEW** | `core/filters/global_trade_filter.py` | Sits between StrategyBus trigger output and RiskGate. 6 sub-filters: regime throttle, chop detector, ATR volatility filter, loss streak gate, clustering cooldown, session budget. Stateful (rolling windows). |
| Regime throttle | None | **NEW** | Part of GTF | Uses 15m regime from RegimeEngine. Activity multiplier per regime (bull=1.0, ranging=0.5, uncertain=0.3, etc.). |
| Chop detector | None (partial in `core/filters/trade_filters.py`) | **NEW** | Part of GTF | ADX<18 on 15m for 3+ consecutive bars = chop. Reduces max trades to 1/symbol/hour. |
| Loss streak gate | Partial in `position_sizer.py` (lines 76-78) | **ADAPT** | Part of GTF | Move from PositionSizer to GTF. After 3 consecutive losses: pause 30min + halve size. After 5: pause 60min. Reset on 2 wins. |
| Clustering cooldown | None | **NEW** | Part of GTF | After trade close, same-symbol cooldown: 10min (win) / 20min (loss). Prevents revenge trading. |
| Session budget | None | **NEW** | Part of GTF | Max 30 trades/day. Max 8/symbol/day. Tracked via daily counter, reset at 00:00 UTC. |

### 6.2 No-Trade Conditions (Addendum §3)

| Design Element | Current Equivalent | Action | Target File | Notes |
|---|---|---|---|---|
| No-trade system | Partial in `core/scanning/universe_filter.py` | **NEW** | `core/filters/no_trade_conditions.py` | 6 conditions: dead market (ATR<0.3% on 15m), chaotic spike (ATR>3× 20-bar average), spread widening (>0.15%), TF conflict (1h and 15m regimes opposing), thin book (top-5 depth < $50k), funding rate extreme (>0.1%/8h). Each returns BLOCK/WARN/PASS. |

### 6.3 Execution Adaptation Engine (Addendum §4)

| Design Element | Current Equivalent | Action | Target File | Notes |
|---|---|---|---|---|
| Execution engine | `core/execution/smart_order_executor.py` | **ADAPT** | `core/execution/execution_adapter.py` | Track fill quality over rolling 20-trade window. Adjust order type (limit vs market), aggressiveness, and timing based on measured slippage. Current `smart_order_executor.py` has partial logic — expand. |

### 6.4 Portfolio Coordination Layer (Addendum §5)

| Design Element | Current Equivalent | Action | Target File | Notes |
|---|---|---|---|---|
| Cross-strategy coordinator | Partial in `core/portfolio/correlation_controller.py` | **NEW** | `core/portfolio/portfolio_coordinator.py` | Prevents same-symbol opposing signals, same-class doubling, and correlated-asset clustering. Uses existing correlation matrix. Enforces 1-per-symbol, max 3 from same strategy class, heat limit 8%. |

### 6.5 Asset Ranking System (Addendum §6)

| Design Element | Current Equivalent | Action | Target File | Notes |
|---|---|---|---|---|
| Asset ranker + tiering | `core/analytics/symbol_allocator.py` | **ADAPT** | `core/analytics/asset_ranker.py` | Rename + expand. 5-component score: volatility (0.30), trend clarity (0.25), volume (0.20), spread (0.15), historical performance (0.10). Output: 4 tiers (Active+/Active/Reduced/Dormant). Recalculate every 4h. Feed into WatchlistManager for tier-based scanning. |

### 6.6 Time Stop Adaptation (Addendum §7)

| Design Element | Current Equivalent | Action | Target File | Notes |
|---|---|---|---|---|
| Adaptive time stop | `PaperPosition.max_hold_bars` (lines 75-76) | **ADAPT** | Part of ExecutionManager | Current: static `max_hold_bars`. New: per-strategy base time stop (MX=45min, VR=60min, MPC=30min, RBR=45min, LSR=20min) × regime multiplier × ATR ratio multiplier. If position >50% of TP distance, extend by 50%. |

### 6.7 Trade Quality Score (Addendum §8)

| Design Element | Current Equivalent | Action | Target File | Notes |
|---|---|---|---|---|
| TQS system | `OrderCandidate.score` (single number) | **NEW** | `core/scoring/trade_quality_scorer.py` | 5-component weighted score: setup quality (0.30) + trigger quality (0.25) + microstructure (0.20) + execution context (0.15) + historical context (0.10). Computed after Stage B trigger, before GTF. Score range [0.0, 1.0]. Min threshold: 0.40 to enter. |

### 6.8 Learning & Adaptation Loop (Addendum §9)

| Design Element | Current Equivalent | Action | Target File | Notes |
|---|---|---|---|---|
| 3 performance matrices | `core/learning/level2_tracker.py` | **ADAPT** | `core/learning/performance_matrices.py` | Rename + expand. Current: model×regime, model×asset (2 matrices). New: strategy×regime, strategy×asset, strategy×hour (3 matrices). 50-trade rolling window per cell. Auto-disable at WR<35% + PF<0.85 for ≥30 trades. Auto-boost at PF>1.5 for ≥20 trades. |
| AdaptiveWeightEngine | `core/learning/adaptive_weight_engine.py` | **ADAPT** | Same | Update to read from 3 new matrices instead of L1+L2. Clamp range stays [0.70, 1.30]. |
| TradeOutcomeStore | `core/learning/trade_outcome_store.py` | **ADAPT** | Same | Add strategy_class, tqs_score, hour_of_day fields to trade records. |

### 6.9 Failure Mode Protection (Addendum §10)

| Design Element | Current Equivalent | Action | Target File | Notes |
|---|---|---|---|---|
| 5 failure detectors | Partial in `core/monitoring/performance_thresholds.py` | **NEW** | `core/monitoring/failure_detectors.py` | 5 detectors: PF drift (3-day rolling PF approaching 1.0), execution degradation (slippage rising), strategy concentration (>60% of trades from 1 strategy), correlation spike (portfolio correlation >0.8), regime mismatch (strategy firing in wrong regime >20% of time). Each emits WARNING or CRITICAL. |
| Recovery mode | Partial in `core/risk/crash_defense_controller.py` | **NEW** | `core/monitoring/recovery_controller.py` | When CRITICAL fires: risk→0.10%, max concurrent→4, only VR/MPC/RBR (mean-reversion class + pullback), TQS≥0.60 required. Exit recovery after 20 consecutive trades with PF>1.2. |

---

## 7. FINAL ADDENDUM CONTROLS (Final Addendum §1–§3)

### 7.1 Edge Validity Monitor (Final Addendum §1)

| Design Element | Current Equivalent | Action | Target File | Notes |
|---|---|---|---|---|
| Class-level tracker | None | **NEW** | `core/monitoring/edge_validity_monitor.py` | 3 strategy classes (Breakout, Pullback, Mean-Reversion). 75-trade rolling window per class. DEGRADED at PF<1.05 (30 trades). SUSPENDED at PF<0.90 (50 trades). Probe recovery: re-enable after 72h cooldown with 10-trade probe at 50% size. |
| Regime isolation check | None | **NEW** | Part of edge_validity_monitor | If degradation is only in 1 regime → Learning Loop handles it (not class suspension). Class suspension only when degradation spans 2+ regimes. |

### 7.2 Capital Concentration Engine (Final Addendum §2)

| Design Element | Current Equivalent | Action | Target File | Notes |
|---|---|---|---|---|
| Dynamic capital weighting | `core/meta_decision/regime_capital_allocator.py` | **ADAPT** | `core/sizing/capital_concentration.py` | Rename + expand. Current: regime-based allocation. New: `capital_weight = base_weight × class_health_mod × conviction_mod`. base_weight from TQS + asset_score + execution_score. class_health_mod from Edge Validity Monitor. conviction_mod from regime confidence + multi-strategy agreement. Range [0.40, 1.50]. |

### 7.3 Signal Expiry System (Final Addendum §3)

| Design Element | Current Equivalent | Action | Target File | Notes |
|---|---|---|---|---|
| Signal lifecycle management | `OrderCandidate.expiry` field exists but unused | **NEW** | `core/strategies/signal_expiry.py` | Per-strategy max age: MX=8s, VR=12s, MPC=10s, RBR=10s, LSR=6s. After Stage B trigger fires, timestamp the signal. Before RiskGate, check: age < max_age, price drift < threshold (0.3× ATR), R:R still valid after drift. Expired signals discarded with logging. |

---

## 8. INDICATOR LIBRARY (V1 §5.4)

| Design Element | Current File | Action | Notes |
|---|---|---|---|
| Indicator library | `core/features/indicator_library.py` | **ADAPT** | Add VWAP (session reset, cumulative). Add volume profile (for RBR range detection). Add microstructure indicators (spread, book imbalance, trade flow). Keep all existing indicators (EMA, SMA, ADX, RSI, BB, ATR, MACD, etc.) — still needed by RegimeEngine. Add `calculate_intraday()` function optimized for 1m/5m data (skip slow indicators not needed at 1m). |

---

## 9. DATABASE & PERSISTENCE (V1 §11)

| Design Element | Current File | Action | Notes |
|---|---|---|---|
| ORM models | `core/database/models.py` | **ADAPT** | Add columns to Trade model: `strategy_class`, `tqs_score`, `capital_weight`, `signal_age_ms`, `setup_bar_ts`, `trigger_bar_ts`, `gtf_passed`, `execution_quality_score`. Add `_migrate_schema()` entries (MANDATORY per CLAUDE.md rules). |
| Engine | `core/database/engine.py` | **ADAPT** | Update `_migrate_schema()` for all new columns. |
| open_positions.json | `data/open_positions.json` | **ADAPT** | Add new fields to `to_dict()`/`from_dict()`. |
| trade_outcomes.jsonl | `data/trade_outcomes.jsonl` | **ADAPT** | Expand record format with intraday fields. |

---

## 10. CONFIGURATION (V1 Appendix B, Addendum Appendix D, Final Addendum Appendix E)

| Design Element | Current File | Action | Notes |
|---|---|---|---|
| config.yaml | `config.yaml` | **ADAPT** | Add sections: `intraday` (timeframes, strategies), `global_trade_filter`, `no_trade_conditions`, `execution_engine`, `portfolio_coordination`, `asset_ranking`, `time_stop`, `trade_quality_score`, `learning_loop`, `failure_protection`, `edge_validity_monitor`, `capital_concentration`, `signal_expiry`. Preserve all existing keys (backward compatible). |
| settings.py | `config/settings.py` | **ADAPT** | Ensure new nested keys are accessible via `settings.get("intraday.strategies.mx.enabled")` dot-path syntax. |

---

## 11. CROSS-CUTTING CONCERNS

### 11.1 Files That Need NO Changes

| File/Module | Reason |
|---|---|
| `core/risk/crash_defense_controller.py` | Already Qt-free logic. 4-tier system unchanged. |
| `core/risk/crash_detector.py` | 7-component scorer works on any TF. |
| `core/nlp/finbert_pipeline.py` | Not on intraday execution path (agent context only). |
| `core/security/key_vault.py` | Unchanged. |
| `core/database/engine.py` (base) | Only `_migrate_schema()` needs update. |
| `core/notifications/` | Notification system unchanged. |
| `core/audio/` | Voice engine unchanged. |
| `gui/` (all 60+ files) | No GUI changes until Phase 7. |
| `core/rl/` | RL ensemble not on intraday path. Retained for future integration. |
| `core/backtesting/` | Backtesting engine retained. May need TF updates later. |

### 11.2 Qt Import Dependencies (Must Be Removed)

| File | Qt Import | Replacement |
|---|---|---|
| `core/event_bus.py` | `PySide6.QtCore.QObject, Signal` | Pure Python class, `threading.RLock` |
| `core/agents/base_agent.py` | `PySide6.QtCore.QThread, Signal` | `threading.Thread` |
| `core/agents/agent_coordinator.py` | `PySide6.QtCore.QObject, Signal` | Plain class |
| `core/orchestrator/orchestrator_engine.py` | `PySide6.QtCore.QObject, Signal` | Plain class |
| `core/scanning/scanner.py` | `PySide6.QtCore.QObject, QThread, Signal, QTimer, Slot` | Replaced entirely by DataEngine |

### 11.3 New Files Summary (All Phases)

| Phase | New File | Purpose |
|---|---|---|
| 2 | `core/engine.py` | Headless NexusEngine (startup, lifecycle) |
| 2 | `core/event_bus.py` (rewrite) | Pure-Python EventBus |
| 3 | `core/data/data_engine.py` | WS + REST data ingestion |
| 3 | `core/data/candle_builder.py` | 1m → all TFs derivation |
| 4 | `core/strategies/intraday/base_strategy.py` | Intraday strategy base class |
| 4 | `core/strategies/intraday/mx_strategy.py` | Momentum Expansion |
| 4 | `core/strategies/intraday/vr_strategy.py` | VWAP Reclaim/Rejection |
| 4 | `core/strategies/intraday/mpc_strategy.py` | Micro Pullback Continuation |
| 4 | `core/strategies/intraday/rbr_strategy.py` | Range Breakout Reclaim |
| 4 | `core/strategies/intraday/lsr_strategy.py` | Liquidity Sweep Reclaim |
| 4 | `core/strategies/strategy_bus.py` | Two-stage signal pipeline |
| 4 | `core/strategies/signal_types.py` | SetupSignal + TriggerSignal |
| 4 | `core/strategies/signal_expiry.py` | Signal age/drift validation |
| 5 | `core/filters/global_trade_filter.py` | GTF (6 sub-filters) |
| 5 | `core/filters/no_trade_conditions.py` | 6 hard no-trade conditions |
| 5 | `core/scoring/trade_quality_scorer.py` | TQS (5-component score) |
| 5 | `core/portfolio/portfolio_coordinator.py` | Cross-strategy coordination |
| 6 | `core/monitoring/edge_validity_monitor.py` | Class-level degradation |
| 6 | `core/monitoring/failure_detectors.py` | 5 failure mode detectors |
| 6 | `core/monitoring/recovery_controller.py` | Recovery mode controller |
| 6 | `core/sizing/capital_concentration.py` | Dynamic capital weighting |
| 6 | `core/execution/execution_adapter.py` | Execution quality adaptation |

---

## 12. DEPENDENCY CHAIN (Implementation Order Constraints)

```
Phase 2: Headless Core
  └─ EventBus (pure Python) — MUST be first (everything depends on it)
  └─ core/engine.py — headless lifecycle
  └─ BaseAgent decouple — threading.Thread
  └─ OrchestratorEngine decouple — remove QObject

Phase 3: Data Engine
  └─ Depends on: Phase 2 (EventBus)
  └─ CandleBuilder — in-memory, no external deps
  └─ DataEngine — depends on ExchangeManager.get_ws_exchange()

Phase 4: Strategy Engine
  └─ Depends on: Phase 3 (candle events), Phase 2 (EventBus)
  └─ BaseIntradayStrategy — depends on new signal types
  └─ StrategyBus — depends on CandleBuilder events + RegimeEngine
  └─ 5 strategies — depend on BaseIntradayStrategy + indicator_library
  └─ Signal expiry — depends on StrategyBus output

Phase 5: Profitability Hardening
  └─ Depends on: Phase 4 (strategy output to filter)
  └─ GTF — depends on RegimeEngine + trade history
  └─ No-trade conditions — depends on DataEngine (spread, depth)
  └─ TQS — depends on Stage B output + microstructure data
  └─ Portfolio coordinator — depends on RiskGate + correlation matrix
  └─ Asset ranker — depends on DataEngine (rolling metrics)
  └─ Learning matrices — depends on trade outcome store

Phase 6: Final Addendum Controls
  └─ Depends on: Phase 5 (TQS, learning matrices)
  └─ Edge Validity Monitor — depends on trade outcome store
  └─ Capital Concentration — depends on TQS + Edge Validity + asset score
  └─ Failure detectors — depends on all Phase 5 components
  └─ Recovery controller — depends on failure detectors

Phase 7: UI/Dashboard (optional)
  └─ Depends on: Phases 2-6 complete
  └─ Qt bridge adapter subscribes to pure-Python EventBus

Phase 8: Full Integration Testing
Phase 9: Hardening & Soak Test
```

---

## 13. RISK REGISTER

| Risk | Impact | Mitigation |
|---|---|---|
| EventBus rewrite breaks all 60+ GUI subscribers | HIGH | Qt bridge adapter maintains exact same `subscribe(topic, callback)` API. GUI code unchanged. |
| WS disconnects cause data gaps | MEDIUM | CandleBuilder tracks expected vs received candle count. 3-miss → auto REST fallback. Publishes `FEED_STATUS` for monitoring. |
| 5 new strategies all underperform | HIGH | Each strategy gated behind config flag (`intraday.strategies.mx.enabled: false`). Enable one at a time after backtest validation. |
| Loss of existing swing trading capability | LOW | All swing code archived (not deleted). `config.yaml` can re-enable swing mode by pointing to archived strategy paths. |
| HMM retraining on 5m data diverges | MEDIUM | Run HMM on both 5m and 30m in parallel during Phase 8 soak. Compare regime accuracy. Fallback to rule-based-only if HMM degrades. |
| Database migration breaks existing DB | MEDIUM | `_migrate_schema()` only adds columns with defaults. Never removes columns. Backward compatible. |

---

*End of Phase 1B mapping. Proceed to Phase 1C (Dependency & Tooling Audit).*
