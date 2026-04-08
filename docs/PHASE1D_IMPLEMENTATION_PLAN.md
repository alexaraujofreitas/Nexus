# Phase 1D — Implementation Plan by Module

**Date:** 2026-04-06
**Phase:** 1D of 9
**Status:** Complete

---

## Implementation Phase Breakdown

Each phase below specifies: modules built, acceptance criteria, and gate requirements. No phase starts until the prior phase passes its gate.

---

## Phase 2 — Headless Core Foundation

**Goal:** Decouple execution core from Qt. Establish pure-Python EventBus and headless engine lifecycle. System can start without PySide6.

### Module 2.1: Pure-Python EventBus
- **File:** `core/event_bus.py` (rewrite)
- **Work:**
  - Remove `QObject` inheritance and `Signal(object)` Qt signal
  - Keep `threading.RLock`, `defaultdict(list)` subscriber pattern
  - Keep `Event` dataclass unchanged
  - Keep `Topics` enum, add new intraday topics
  - Add `QtBridge` class (optional adapter): if PySide6 available, wraps EventBus.publish() to also emit a Qt Signal for GUI subscribers
  - Global singleton `bus = EventBus()` unchanged
- **Acceptance:** `from core.event_bus import bus, Topics` works without PySide6 installed. All existing tests that import EventBus still pass (mock PySide6 if needed).
- **Risk:** Every module in the codebase imports `bus`. API must be identical.
- **LOE:** ~100 lines changed

### Module 2.2: BaseAgent Threading Decouple
- **File:** `core/agents/base_agent.py`
- **Work:**
  - Replace `QThread` with `threading.Thread`
  - Replace Qt `Signal(dict)` / `Signal(str)` with plain Python callbacks or EventBus publish
  - Keep run-loop, fetch→process→publish, backoff, staleness tracking
- **Acceptance:** 4 retained agents start/stop without Qt. Existing agent tests pass.
- **LOE:** ~60 lines changed

### Module 2.3: AgentCoordinator Decouple
- **File:** `core/agents/agent_coordinator.py`
- **Work:**
  - Remove `QObject` parent and `Signal(dict)`
  - Reduce agent list to 4: funding_rate, liquidation_flow, crash_detection, order_book
  - Archive 19 agents to `core/agents/_archived/`
- **Acceptance:** `coordinator.start_all()` starts exactly 4 agents. No Qt import in coordinator.
- **LOE:** ~40 lines changed + file moves

### Module 2.4: OrchestratorEngine Decouple
- **File:** `core/orchestrator/orchestrator_engine.py`
- **Work:**
  - Remove `QObject` parent and `Signal`
  - Reduce weight tables from 12 to 4 agent slots
  - Mark as advisory (not gating)
- **Acceptance:** Orchestrator runs without Qt. `get_signal()` returns valid OrchestratorSignal.
- **LOE:** ~80 lines changed

### Module 2.5: Headless Engine
- **File:** `core/engine.py` (NEW)
- **Work:**
  - Extract initialization from `main.py`: DB init, ExchangeManager, AgentCoordinator, CrashDefenseController, NotificationManager
  - `NexusEngine.start()` / `NexusEngine.stop()` lifecycle
  - `main.py` becomes: `engine = NexusEngine(); engine.start(); if not headless: start_qt_gui(engine)`
- **Acceptance:** `python main.py --headless` starts engine, connects exchange, starts agents, without PySide6.
- **LOE:** ~200 lines new + ~100 lines refactored from main.py

### Module 2.6: Database Schema Preparation (Audit Finding 1 — CRITICAL)
- **File:** `core/database/engine.py`
- **Work:**
  - Pre-add `_migrate_schema()` entries for all 8 intraday Trade columns: `strategy_class`, `tqs_score`, `capital_weight`, `signal_age_ms`, `setup_bar_ts`, `trigger_bar_ts`, `gtf_passed`, `execution_quality_score`
  - Columns added with safe defaults (NULL or 0.0). ORM model attrs defined later in Phase 5.
  - Per CLAUDE.md: "Every new column in any ORM model MUST be added to `_migrate_schema()` simultaneously."
- **Acceptance:** `_migrate_schema()` entries exist for all 8 columns. Existing DB opens without error.
- **LOE:** ~30 lines

### Module 2.7: Thread Count Baseline Recalibration (Audit Finding 6 — MAJOR)
- **File:** `core/monitoring/trade_monitor.py` (or existing thread watchdog)
- **Work:**
  - On `NexusEngine.start()`, measure and log baseline thread count before agents start
  - Log: `STARTUP: baseline thread count = {count}`
  - Expected new baseline: 10–15 (headless, 4 agents) vs current 51 (Qt + 23 agents)
  - Watchdog threshold remains 75 (sufficient headroom for new baseline + DataEngine)
- **Acceptance:** Baseline thread count logged at startup. No spurious "thread count high" warnings.
- **LOE:** ~20 lines

### Phase 2 Gate
- [ ] `import core.event_bus` succeeds without PySide6
- [ ] `import core.agents.base_agent` succeeds without PySide6
- [ ] `import core.orchestrator.orchestrator_engine` succeeds without PySide6
- [ ] 4 agents start/stop in headless mode
- [ ] Existing GUI mode still works (Qt bridge adapter)
- [ ] `_migrate_schema()` entries exist for all 8 intraday columns (verified by inspection)
- [ ] Baseline thread count measured and logged at startup
- [ ] Watchdog threshold (75) documented as appropriate for new baseline
- [ ] All pre-existing tests pass (0 regressions)
- [ ] New tests: ≥24 (EventBus, BaseAgent, Coordinator, Engine lifecycle, schema, thread baseline)

---

## Phase 3 — Data Engine

**Goal:** Replace timer-based REST polling with WebSocket-first data ingestion. CandleBuilder derives all higher TFs from 1m stream.

### Module 3.1: CandleBuilder
- **File:** `core/data/candle_builder.py` (NEW)
- **Work:**
  - In-memory rolling buffers: per-symbol, per-TF
  - `on_1m_candle(symbol, ohlcv)` → accumulates and emits 3m, 5m, 15m, 1h candles on close
  - Buffer sizes: 300 bars per TF per symbol
  - Publishes `CANDLE_3M`, `CANDLE_5M`, `CANDLE_15M`, `CANDLE_1H` events via EventBus
  - Indicator computation on candle close (calls `calculate_all()` or `calculate_intraday()`)
- **Acceptance:** Feed 300 1m candles → verify correct 3m/5m/15m/1h candles emitted with matching OHLCV aggregation.
- **LOE:** ~350 lines new

### Module 3.2: DataEngine
- **File:** `core/data/data_engine.py` (NEW)
- **Work:**
  - asyncio event loop (dedicated thread)
  - Subscribe to 1m klines on all 16 symbols via `ccxt.pro.watch_ohlcv()`
  - On each 1m candle close → feed to CandleBuilder
  - REST fallback: if WS disconnects or 3 expected candles missed, switch to REST polling for affected symbol
  - Historical backfill on startup: fetch 300× 1m candles per symbol via `ExchangeManager.fetch_ohlcv()`
  - Feed status monitoring: publish `FEED_STATUS` events
- **Acceptance:** Connect to Bybit Demo WS → receive 1m candles for ≥5 symbols → CandleBuilder produces correct 5m/15m candles. REST fallback tested by simulating WS disconnect.
- **LOE:** ~500 lines new

### Module 3.3: Indicator Library Additions
- **File:** `core/features/indicator_library.py`
- **Work:**
  - Add `calculate_vwap(df)` — session-reset cumulative VWAP (UTC midnight reset)
  - Add `calculate_volume_profile(df, n_bins=20)` — price-volume histogram for range detection
  - Add `calculate_intraday(df)` — optimized subset of indicators for 1m data (skip slow indicators like 200-bar SMA that need 200 minutes of data)
  - Add microstructure columns: `spread_pct`, `book_imbalance` (populated by DataEngine from order book snapshots)
- **Acceptance:** VWAP matches manual calculation on test data. Volume profile correctly identifies HVN/LVN. `calculate_intraday()` runs in <10ms on 300 1m bars.
- **LOE:** ~200 lines new

### Module 3.4: WatchlistManager Expansion
- **File:** `core/scanning/watchlist.py`
- **Work:**
  - Expand default watchlist from 5 to 16 symbols
  - Add tier property per symbol (Active+/Active/Reduced/Dormant)
  - Tier determines scan frequency and capital allocation eligibility
- **Acceptance:** `get_active_symbols()` returns tiered list. Tier changes persist across restarts.
- **LOE:** ~60 lines changed

### Phase 3 Gate
- [ ] CandleBuilder unit tests: 1m→3m, 1m→5m, 1m→15m, 1m→1h aggregation correct
- [ ] DataEngine connects to Bybit Demo WS and receives 1m klines
- [ ] REST fallback activates within 3 missed candles
- [ ] Historical backfill seeds all buffers on startup
- [ ] Indicator library: VWAP, volume profile, intraday subset tested
- [ ] All Phase 2 tests still pass
- [ ] New tests: ≥30 (CandleBuilder, DataEngine, indicators, WS/REST fallback)

---

## Phase 4 — Strategy Engine

**Goal:** Implement 5 intraday strategies with two-stage pipeline (Setup → Trigger), StrategyBus, and signal types.

### Module 4.1: Signal Types
- **File:** `core/strategies/signal_types.py` (NEW)
- **Work:**
  - `SetupSignal` dataclass: symbol, strategy_name, strategy_class, direction, bias_score, regime, invalidation_conditions, timestamp, tf_setup
  - `TriggerSignal` dataclass: symbol, strategy_name, direction, entry_price, stop_loss, take_profit, trade_score, microstructure_snapshot, setup_ref, timestamp
  - Both carry full provenance for audit trail
- **LOE:** ~80 lines

### Module 4.2: BaseIntradayStrategy
- **File:** `core/strategies/intraday/base_strategy.py` (NEW)
- **Work:**
  - Abstract base with two methods: `evaluate_setup()` and `evaluate_trigger()`
  - Class attributes: `STRATEGY_CLASS`, `MAX_SIGNAL_AGE_S`, `PRICE_DRIFT_THRESHOLD_ATR`, `BASE_TIME_STOP_MINUTES`
  - Inherits regime affinity pattern from existing `BaseSubModel`
- **LOE:** ~120 lines

### Module 4.3: StrategyBus
- **File:** `core/strategies/strategy_bus.py` (NEW)
- **Work:**
  - Subscribes to `CANDLE_5M` (Stage A) and `CANDLE_1M` (Stage B)
  - On 5m close: run all enabled strategies' `evaluate_setup()` per symbol. Store qualified setups in pending queue.
  - On 1m close: for each pending setup, run strategy's `evaluate_trigger()`. If trigger fires → produce TriggerSignal → pass to GTF → RiskGate → ExecutionManager.
  - Strategy enable/disable via config flags (`intraday.strategies.mx.enabled`)
  - Signal expiry: check age and drift before forwarding to GTF
- **LOE:** ~300 lines

### Module 4.4: Five Strategies (MX, VR, MPC, RBR, LSR)
- **Files:** `core/strategies/intraday/{mx,vr,mpc,rbr,lsr}_strategy.py` (5 NEW files)
- **Work per strategy:**
  - `evaluate_setup()`: check 5m/15m conditions per V1 §5.2 specifications
  - `evaluate_trigger()`: check 1m conditions per V1 §5.2 specifications
  - Each ~150-250 lines
- **Total LOE:** ~1000 lines across 5 files

### Module 4.5: Signal Expiry (Audit Finding 2 — CRITICAL: clarified; External Review Correction 1: latency guardrail)
- **File:** `core/strategies/signal_expiry.py` (NEW)
- **Work:**
  - `validate_signal(trigger_signal, current_price, current_time) → (bool, str)`
  - Returns `(True, "")` if valid, `(False, reason)` if expired/drifted
  - Three checks in order:
    1. **Age check:** `(current_time - trigger_signal.timestamp).total_seconds() < effective_max_age_s`
    2. **Price drift check:** `abs(current_price - trigger_signal.entry_price) / trigger_signal.atr_value < 0.3`
    3. **R:R revalidation:** recalculate R:R with `current_price` as entry; must still meet `min_rr >= 1.5`
  - Per-strategy age thresholds hardcoded as constants, overridable via `config.yaml` keys `intraday.signal_expiry.{mx,vr,mpc,rbr,lsr}_max_age_s`
  - **Integration point:** Called by `StrategyBus` immediately after `evaluate_trigger()` returns, BEFORE publishing `TRIGGER_FIRED` event to GTF
  - On rejection: publish `SIGNAL_EXPIRED` event with `{symbol, strategy, reason, age_ms, drift_pct}`
  - **Latency guardrail (External Review Correction 1):**
    - Track `signal_pipeline_latency_ms` — rolling 50-sample EWMA of time from `CANDLE_1M` event arrival to `validate_signal()` call. Measured in StrategyBus per trigger evaluation.
    - `effective_max_age_s` = `strategy.MAX_SIGNAL_AGE_S` unless latency guardrail is active
    - **Dynamic adjustment rule:** If `rolling_avg_latency_ms > 0.60 × (strategy.MAX_SIGNAL_AGE_S × 1000)`:
      - `effective_max_age_s = strategy.MAX_SIGNAL_AGE_S × 1.50` (increase by 50%)
      - Log: `SIGNAL_EXPIRY: latency guardrail active — pipeline_latency={lat_ms}ms > 60% of max_age={max_age_s}s, effective_max_age extended to {eff_s}s`
    - **Revert rule:** When rolling avg drops back below 50% of original max_age: revert to base `MAX_SIGNAL_AGE_S`
    - **Rationale:** On high-latency VPN paths (Singapore → Bybit, occasional 150ms spikes) or under heavy load, the pipeline itself can consume a significant fraction of max_age. Without this guardrail, valid signals are dropped not because they're stale but because the pipeline is slow. The 50% extension preserves the safety intent (never execute truly stale signals) while accommodating transient infrastructure latency.
    - **Safety bound:** `effective_max_age_s` can never exceed `2.0 × strategy.MAX_SIGNAL_AGE_S` regardless of pipeline latency.
    - **Safety bound logging (Re-audit Recommendation 4):** When the safety bound clamps `effective_max_age_s`, log: `SIGNAL_EXPIRY: safety bound clamped effective_max_age from {unclamped_s}s to {clamped_s}s (2× base {base_s}s) for {strategy}`
- **Acceptance:** Fresh signals pass. Aged signals blocked. Drifted signals blocked. R:R-invalid signals blocked. `SIGNAL_EXPIRED` event published with reason. Under high pipeline latency (>60% of max_age), effective max_age extends by 50% and valid signals are not dropped.
- **LOE:** ~120 lines

### Module 4.6: RegimeEngine Adaptation
- **File:** `core/regime/regime_engine.py` (rename from `regime_classifier.py`)
- **Work:**
  - Run classify on 5m and 15m DataFrames
  - Cache regime per symbol per TF
  - Publish `REGIME_CHANGED` when regime transitions
  - Keep all 12 regime states
- **LOE:** ~80 lines changed

### Phase 4 Gate
- [ ] Each strategy has ≥15 unit tests covering setup qualification, trigger firing, edge cases, and rejection scenarios
- [ ] StrategyBus correctly routes 5m→setup and 1m→trigger
- [ ] Signal expiry correctly blocks stale signals (age > MAX_SIGNAL_AGE_S per strategy)
- [ ] Signal expiry correctly blocks drifted signals (price drift > 0.3 × ATR)
- [ ] Signal expiry correctly blocks R:R-invalid signals (R:R < 1.5 after drift)
- [ ] Signal expiry publishes `SIGNAL_EXPIRED` event with reason (age vs drift vs R:R)
- [ ] Fresh signals within all thresholds pass through to GTF
- [ ] Latency guardrail: when pipeline latency > 60% of max_age, effective_max_age extends by 50%
- [ ] Latency guardrail: valid signals not dropped excessively under simulated high latency
- [ ] Latency guardrail: effective_max_age never exceeds 2× base max_age (safety bound)
- [ ] Latency guardrail: reverts to base max_age when latency drops below 50% threshold
- [ ] End-to-end: synthetic candle data → setup → trigger → TriggerSignal produced
- [ ] All Phase 2-3 tests still pass
- [ ] New tests: ≥89 (5 strategies × 15 + StrategyBus 10 + signal types + expiry 9)

---

## Phase 5 — Profitability Hardening

**Goal:** Implement all 9 hardening mechanisms from the Profitability Addendum.

### Module 5.1: Global Trade Filter
- **File:** `core/filters/global_trade_filter.py` (NEW)
- **Sub-filters:** Regime throttle, chop detector, ATR volatility filter, loss streak gate, clustering cooldown, session budget
- **LOE:** ~400 lines

### Module 5.2: No-Trade Conditions
- **File:** `core/filters/no_trade_conditions.py` (NEW)
- **6 conditions:** Dead market, chaotic spike, spread widening, TF conflict, thin book, funding extreme
- **LOE:** ~250 lines

### Module 5.3: Trade Quality Scorer
- **File:** `core/scoring/trade_quality_scorer.py` (NEW)
- **5 components:** Setup (0.30) + trigger (0.25) + microstructure (0.20) + execution context (0.15) + historical (0.10)
- **LOE:** ~200 lines

### Module 5.4: Portfolio Coordinator
- **File:** `core/portfolio/portfolio_coordinator.py` (NEW)
- **Rules:** 1/symbol, max 3/class, no opposing signals, heat 8%, correlation check
- **LOE:** ~200 lines

### Module 5.5: Asset Ranker (Audit Finding 7 — MINOR: clarified)
- **File:** `core/analytics/asset_ranker.py` (adapt from `symbol_allocator.py`)
- **Work:**
  - 5-component score: volatility(0.30) + trend_clarity(0.25) + volume(0.20) + spread(0.15) + historical_perf(0.10)
  - 4 tiers: Active+ (top 4), Active (5–12), Reduced (13–15), Dormant (16)
  - **Refresh mechanism:** Subscribe to `CANDLE_1H`. Maintain `_hour_counter` (0–3). On `_hour_counter == 3` (every 4th hour): recalculate all scores, assign tiers, call `watchlist.update_tier(asset, tier)` for changed tiers, reset counter.
  - **WatchlistManager integration:**
    - `get_active_symbols(min_tier="Active")` returns assets with tier ≥ Active
    - Dormant assets: StrategyBus skips setup evaluation entirely
    - Reduced assets: participate but `asset_weight × 0.70` in capital calculations
  - Log: `ASSET_RANKER: {asset} tier changed {old} → {new}, score={score:.3f}`
- **LOE:** ~180 lines changed

### Module 5.6: Adaptive Time Stop
- **Integrated into:** `core/execution/execution_manager.py`
- **Per-strategy base × regime_mult × ATR_ratio_mult + winner extension**
- **LOE:** ~80 lines added

### Module 5.7: Performance Matrices (Learning Loop)
- **File:** `core/learning/performance_matrices.py` (adapt from `level2_tracker.py`)
- **3 matrices:** Strategy×Regime, Strategy×Asset, Strategy×Hour
- **Public interface for Edge Monitor (Re-audit Recommendation 1):**
  - `get_disabled_cells_for_class(strategy_class: str) → list[dict]` — returns all currently disabled cells belonging to the given class, each with `{"strategy": str, "regime": str, "disable_timestamp": datetime, "reason": str}`
  - This method is consumed by Module 6.1 (Edge Validity Monitor) for LL conflict resolution (Correction 3)
- **LOE:** ~200 lines changed

### Module 5.8: Execution Adapter
- **File:** `core/execution/execution_adapter.py` (NEW)
- **Rolling 20-trade fill quality tracking + order type adaptation**
- **LOE:** ~150 lines

### Phase 5 Gate
- [ ] GTF blocks trades in synthetic chop/streak/budget scenarios
- [ ] No-trade conditions correctly block all 6 condition types
- [ ] TQS produces scores in [0.0, 1.0] range for diverse scenarios
- [ ] Portfolio coordinator prevents same-symbol doubling and class overconcentration
- [ ] Asset ranker produces 4-tier ranking that changes with market conditions
- [ ] Learning matrices auto-disable/boost based on 50-trade windows
- [ ] All Phase 2-4 tests still pass
- [ ] New tests: ≥60

---

## Phase 6 — Final Addendum Controls

**Goal:** Edge Validity Monitor, Capital Concentration Engine, Failure Detectors, Recovery Mode.

### Module 6.1: Edge Validity Monitor (Audit Finding 5 — MAJOR: clarified; External Review Correction 3: LL conflict resolution)
- **File:** `core/monitoring/edge_validity_monitor.py` (NEW)
- **Work:**
  - 3 class trackers (Breakout, Pullback, Mean-Reversion), each with 75-trade rolling `deque`
  - DEGRADED at PF < 1.05 with ≥30 trades. SUSPENDED at PF < 0.90 with ≥50 trades.
  - Probe recovery: 72h cooldown → re-enable at 50% size for 10-trade probe → if PF ≥ 1.10, restore ACTIVE
  - **Regime isolation logic:**
    - When class enters DEGRADED, decompose 75-trade window by regime
    - Count trades per regime, compute per-regime PF
    - "Degraded regime" = regime where class PF < 1.00 with ≥10 trades
    - If `count(degraded_regimes) == 1`: defer to Learning Loop (no class action), `class_health_mod = 1.0`
    - If `count(degraded_regimes) >= 2`: proceed with DEGRADED behavior, `class_health_mod = 0.70`
    - If `count(degraded_regimes) >= 4` AND PF < 0.90: consider SUSPENDED
  - **Learning Loop conflict resolution rule (External Review Correction 3):**
    - Before the Edge Monitor can transition a class to DEGRADED or SUSPENDED, it MUST check how much of the class's poor performance is already attributable to Learning Loop disabled cells
    - **Attribution check:**
      1. Query Learning Loop (Module 5.7) for all currently disabled strategy×regime cells belonging to this class
      2. Count trades in the 75-trade window that occurred in those disabled cells AND whose `trade.timestamp >= cell.disable_timestamp` is FALSE (i.e., trades that happened BEFORE the LL disabled the cell — re-audit recommendation 2: temporal ordering clause)
      3. Compute `ll_attribution_ratio = count(trades_in_disabled_cells) / count(total_degraded_trades)`
    - **Conflict resolution rule:** If `ll_attribution_ratio > 0.50` (more than half of the class's degradation comes from cells the Learning Loop has already disabled):
      - Edge Monitor MUST NOT degrade the class
      - `class_health_mod` remains at 1.0
      - Log: `EDGE_MONITOR: class={} degradation attributable to LL-disabled cells ({ratio:.0%} > 50%). Deferring to Learning Loop. No class-level action.`
      - **Rationale:** The Learning Loop has already identified and disabled the specific regime×strategy combinations causing the poor performance. Degrading the entire class would punish the healthy cells and reduce capital allocation to strategy×regime pairs that are still profitable. This prevents the Edge Monitor from "double-penalizing" what the Learning Loop is already handling.
    - **When conflict rule does NOT apply:** If `ll_attribution_ratio ≤ 0.50`, the majority of degradation is in cells the Learning Loop has NOT disabled — meaning there's a structural class-level problem that cell-level disabling can't fix. Edge Monitor proceeds with DEGRADED/SUSPENDED normally.
  - **Interaction boundary:** Edge Monitor = class-level cross-regime. Learning Loop = cell-level per-regime. Conflict resolution prevents overlap.
- **LOE:** ~350 lines

### Module 6.2: Capital Concentration Engine (Audit Finding 3 — MAJOR: clarified; External Review Correction 2: de-correlation)
- **File:** `core/sizing/capital_concentration.py` (NEW)
- **Work:**
  - **base_weight:** `TQS_score × asset_weight × execution_score`
    - `TQS_score` ∈ [0.0, 1.0] from Module 5.3
    - `asset_weight` ∈ [0.40, 1.50] from Module 5.5 (tier-based)
    - `execution_score` ∈ [0.80, 1.20] from Module 5.8 (rolling 20-trade window)
    - Combined via product, clamped to [0.0, 2.0]
  - **class_health_mod:** from Edge Validity Monitor (ACTIVE=1.0, DEGRADED=0.70, SUSPENDED=0.0)
  - **conviction_mod:** `(0.6 × regime_confidence) + (0.4 × multi_strategy_agreement)`
    - `regime_confidence` = HMM probability of current 15m regime classification
    - `multi_strategy_agreement` = count of same-class strategies firing same direction / count of same-class strategies in portfolio
    - Clamped to [0.40, 1.50]
  - **De-correlation guardrail (External Review Correction 2):**
    - Track rolling Pearson correlation between `base_weight` and `conviction_mod` over last 100 capital_weight computations
    - Maintained as a 100-sample circular buffer of `(base_weight, conviction_mod)` pairs
    - **Adjustment rule:** If `corr(base_weight, conviction_mod) > 0.70`:
      - Apply dampening: `effective_conviction_mod = 1.0 + (conviction_mod - 1.0) × 0.70` (reduce deviation from 1.0 by 30%)
      - Log: `CCE: de-correlation active — base_weight↔conviction_mod corr={corr:.3f} > 0.70, conviction_mod dampened {raw:.3f} → {eff:.3f}`
    - **Rationale:** When `base_weight` and `conviction_mod` are highly correlated, their product amplifies concentration rather than diversifying signal sources. The high-TQS trades that produce high `base_weight` tend to also occur during high regime confidence (high `conviction_mod`), creating a feedback loop that over-allocates to "obvious" setups and under-allocates to diversifying trades. The 30% dampening preserves the multiplier's information content while preventing double-counting of the same underlying signal quality.
    - **Stability invariant:** The distribution of `capital_weight` across trades should have coefficient of variation (CV) between 0.15 and 0.60. CV < 0.15 = weights too uniform (CCE adds no value). CV > 0.60 = weights too extreme (concentration risk). Monitored via integration test.
  - **Final:** `capital_weight = base_weight × class_health_mod × effective_conviction_mod`, clamped to [0.40, 1.50]
  - **Integration:** Singleton, looked up by `ExecutionManager` per trade. Passed to `PositionSizer.calculate_risk_based()` as `capital_weight` parameter.
- **LOE:** ~300 lines

### Module 6.3: Failure Detectors
- **File:** `core/monitoring/failure_detectors.py` (NEW)
- **5 detectors:** PF drift (3-day rolling PF → 1.0), execution degradation (slippage rising), strategy concentration (>60% from 1 strategy), correlation spike (portfolio corr > 0.8), regime mismatch (strategy firing in wrong regime >20%)
- **Each emits:** WARNING or CRITICAL event. CRITICAL triggers Recovery Controller.
- **LOE:** ~300 lines

### Module 6.4: Recovery Controller (Audit Finding 4 — MAJOR: clarified)
- **File:** `core/monitoring/recovery_controller.py` (NEW)
- **Work:**
  - **Recovery entry:** On CRITICAL event from failure detectors:
    - `system.recovery_mode = True`, record `entry_timestamp`
    - Restrict to VR/MPC/RBR strategies only (MX/LSR disabled)
    - Risk: 0.10% (vs normal 0.25%), max concurrent: 4, TQS floor: 0.60
    - Existing open positions continue normally (SL/TP/time stop active, count toward max concurrent 4)
  - **Recovery exit condition:** BOTH of:
    1. Last 20 *completed* trades (settled outcomes) have rolling PF ≥ 1.20
    2. Time in recovery ≥ 4 hours (prevents yo-yo)
    - Rolling PF = sum(wins) / abs(sum(losses)) for last 20 closed trades
  - **Re-entry:** If CRITICAL fires again while in recovery: no action (already restricted). If CRITICAL fires after exit: immediate re-engage, no cooldown.
  - **Exit:** Restore normal rules (all strategies, 0.25% risk, max 8, TQS 0.40). Log: `RECOVERY: exited after {n} trades with PF={pf:.3f} after {hours:.1f}h`
- **LOE:** ~200 lines

### Phase 6 Gate
- [ ] Edge Validity correctly transitions ACTIVE→DEGRADED→SUSPENDED with synthetic trade data
- [ ] Regime isolation: single-regime degradation defers to Learning Loop (no class action)
- [ ] Regime isolation: multi-regime (≥2) degradation triggers class DEGRADED
- [ ] **LL conflict resolution:** If >50% of class degradation is from LL-disabled cells → Edge Monitor does NOT degrade class (Correction 3)
- [ ] **LL conflict resolution:** If ≤50% from LL-disabled cells → Edge Monitor proceeds with DEGRADED normally (Correction 3)
- [ ] **LL conflict resolution:** attribution ratio correctly computed from Learning Loop disabled cell query (Correction 3)
- [ ] Probe recovery re-enables class after 72h cooldown + 10-trade probe (PF ≥ 1.10)
- [ ] Capital weight formula: `base_weight × class_health_mod × effective_conviction_mod` produces [0.40, 1.50]
- [ ] `base_weight` correctly computes `TQS × asset_weight × execution_score`
- [ ] `class_health_mod` correctly reflects Edge Validity status (1.0/0.70/0.0)
- [ ] `conviction_mod` correctly combines regime confidence + multi-strategy agreement
- [ ] **De-correlation:** When base_weight↔conviction_mod Pearson corr > 0.70, conviction_mod dampened by 30% (Correction 2)
- [ ] **De-correlation:** capital_weight CV between 0.15 and 0.60 across 100+ trade scenarios (Correction 2)
- [ ] **De-correlation:** dampening reverts when correlation drops below 0.70 (Correction 2)
- [ ] Capital weight retrievable by ExecutionManager and passed to PositionSizer
- [ ] All 5 failure detectors fire on synthetic degradation scenarios
- [ ] Recovery mode engages on CRITICAL, restricts to VR/MPC/RBR, risk 0.10%, max 4
- [ ] Recovery exit: 20 completed trades with rolling PF ≥ 1.20 AND ≥4h in recovery
- [ ] Recovery re-entry: immediate on new CRITICAL after exit (no cooldown)
- [ ] Existing positions managed normally during recovery
- [ ] All Phase 2-5 tests still pass
- [ ] New tests: ≥60 (edge monitor 18 + capital concentration 15 + failure detectors 10 + recovery 15 + 2 integration)

---

## Phase 7 — UI/Dashboard Integration (Optional)

**Goal:** Connect headless core to existing Qt GUI via EventBus bridge.

- Qt bridge adapter: subscribes to pure-Python EventBus, re-emits as Qt Signals
- Add intraday dashboard page to GUI
- Update existing pages to display intraday metrics
- **Gate:** GUI displays real-time intraday data without modifying core modules.

---

## Phase 8 — Full Integration Testing

**Goal:** End-to-end testing with real Bybit Demo data.

- Connect DataEngine to Bybit Demo WS
- Run all 5 strategies on live data for ≥24h
- Verify full pipeline: WS → CandleBuilder → RegimeEngine → StrategyBus → GTF → RiskGate → ExecutionManager
- Verify all hardening mechanisms activate under real conditions
- Compare signal quality with backtest expectations
- **Gate:** ≥24h clean run, 0 crashes, 0 uncaught exceptions, ≥5 trades executed, all trades auditable.

---

## Phase 9 — Hardening & Soak Test

**Goal:** 7-day production soak with monitoring.

- Run headless for 7 continuous days on Bybit Demo
- Monitor: trade count, PF, max DD, strategy distribution, failure detector activity
- Tune GTF thresholds, TQS weights, time stops based on observed data
- **Gate:** 7-day PF > 1.15, no system crashes, no data gaps > 5min, all failure detectors quiescent.

---

## Total LOE Summary

| Phase | New Lines | Changed Lines | New Files | Changed Files | New Tests |
|---|---|---|---|---|---|
| 2 — Headless Core | ~250 | ~310 | 1 | 5 | 24 |
| 3 — Data Engine | ~1,110 | ~260 | 2 | 2 | 30 |
| 4 — Strategy Engine | ~1,620 | ~160 | 8 | 2 | 89 |
| 5 — Profitability | ~1,460 | ~380 | 5 | 3 | 60 |
| 6 — Final Controls | ~1,200 | ~0 | 4 | 0 | 60 |
| 7 — UI (optional) | ~300 | ~100 | 1 | 2 | 10 |
| **Totals (2-6)** | **~5,640** | **~1,110** | **20** | **12** | **263** |

*Updated to reflect: audit findings 1-7 corrections + external review corrections 1-3 (signal expiry latency guardrail, capital concentration de-correlation, edge monitor vs learning loop conflict resolution).*

---

*End of Phase 1D. Proceed to Phase 1E (Test Plan).*
