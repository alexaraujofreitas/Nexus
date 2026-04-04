# NexusTrader — Phases 7–10: AI Validation, Feedback Loop, Capital, Execution
**Date:** 2026-03-26 | Version: v1.1

---

## Phase 7: AI Agents Validation Framework

### Agent Inventory (23 agents in `core/agents/`)

| Agent | Data Source | Backtestable? | Forward-Validate? | Current Status |
|-------|-------------|---------------|-------------------|----------------|
| crash_detection_agent | rule-based (7 components) | ✅ Partial (OHLCV) | ✅ Yes | Active, production risk |
| funding_rate_agent | Bybit funding API | ❌ No historical | ✅ Forward only | Active |
| coinglass_agent | Coinglass API (OI, liq) | ❌ Limited API | ✅ Forward only | Active, 5-min TTL |
| news_agent | RSS feeds, CryptoPanic | ❌ No | ✅ Forward only | Active |
| reddit_agent | Reddit API | ❌ No | ✅ Forward only | Active |
| twitter_agent | Twitter/X | ❌ No | ✅ Forward only | Active (if key set) |
| telegram_agent | Telegram channels | ❌ No | ✅ Forward only | Active (if set) |
| social_sentiment_agent | Aggregated | ❌ No | ✅ Forward only | Active |
| narrative_agent | News themes | ❌ No | ✅ Forward only | Active |
| geopolitical_agent | VADER sentiment | ❌ No (real-time) | ✅ Forward only | Active |
| whale_agent | On-chain / Coinglass | ❌ No | ✅ Forward only | Active |
| onchain_agent | Bitcoin/ETH on-chain | ❌ Limited | ✅ Forward only | Active |
| liquidation_flow_agent | Coinglass liq data | ❌ No historical | ✅ Forward only | Active |
| liquidation_intelligence_agent | Derived | ❌ No | ✅ Forward only | Active |
| liquidity_vacuum_agent | L2 order book | ❌ No | ✅ Forward only | Active |
| order_book_agent | L2 snapshot | ❌ No | ✅ Forward only | Active |
| macro_agent | FRED / macro data | ⚠️ Partial | ✅ Forward only | Active |
| miner_flow_agent | On-chain | ❌ No | ✅ Forward only | Active |
| options_flow_agent | Deribit/Coinglass | ❌ No | ✅ Forward only | Active |
| scalp_agent | Short-TF price | ❌ No | ✅ Forward only | Active |
| sector_rotation_agent | Market cap derived | ❌ No | ✅ Forward only | Active |
| stablecoin_agent | On-chain stablecoin | ❌ No | ✅ Forward only | Active |
| volatility_surface_agent | Options surface | ❌ No | ✅ Forward only | Active |

**Split: 1 partially backtestable (crash_detection) + 22 forward-only agents.**

### Current Signal Flow

Agents run on independent QThread loops. Each publishes to the EventBus. The `OrchestratorEngine` aggregates agent signals into a meta-signal that injects into `ConfluenceScorer` as the "orchestrator" model vote. `FundingRateModel` and `OrderBookModel` read directly from agent caches.

**Staleness handling:** `BaseAgent.is_stale()` fires if `now - last_updated > max_staleness_seconds`. Stale agents emit a zero-confidence stale signal. Sub-models check for stale agent data and return `None` rather than firing.

### Gaps

1. **No individual agent → trade outcome correlation.** The 22 forward-only agents contribute to the orchestrator meta-signal. But there is no mechanism to measure: "when FundingRateAgent fired with signal > 0.4 and trades were taken, what was the subsequent WR?" Without this, there is no evidence any of the 22 agents add value.

2. **OrchestratorEngine is a black box from signal perspective.** The orchestrator takes 23 agent signals and produces one meta-signal. The weighting (REGIME_WEIGHTS in regime_classifier.py) is hardcoded. No evidence exists that these weights are empirically derived from agent performance.

3. **Agent poll intervals are uncorrelated with signal utility.** FundingRateAgent polls every few minutes; funding rates reset every 8 hours. Over-polling wastes API calls.

4. **No agent signal event store.** Agent signal state is in-memory only. When a trade fires, the agent signals at that moment are not persisted alongside the trade record. This makes retrospective analysis (which agents predicted this trade?) impossible.

### Agent Signal Event Store Design

Every time a trade opens, the system should snapshot current agent state alongside the trade. Required implementation:

```python
# core/analysis/agent_signal_snapshot.py

@dataclass
class AgentSignalSnapshot:
    """Snapshot of all agent signals at trade open time."""
    trade_id: str
    snapshot_time: datetime
    agent_signals: dict  # {agent_name: {signal, confidence, stale}}
    orchestrator_meta: float
    orchestrator_confidence: float
    regime_at_entry: str
    confluence_score: float
    models_fired: list[str]
```

Persisted to SQLite `agent_signal_snapshots` table. Join with `trade_outcomes` to compute per-agent predictive value.

### Forward-Validation Framework

Since most agents cannot be backtested, their value must be assessed forward:

**Shadow Mode Protocol:**
1. Run each agent in shadow mode: log its signal + direction on every scan cycle.
2. For each agent signal, record the subsequent N-bar forward return (1h, 4h, 24h).
3. After 100+ signal events, compute IC (Information Coefficient) = correlation between agent signal strength and forward return.
4. IC > 0.05 over 100+ events = statistically significant (approximate threshold at 90% confidence).
5. IC < 0 over 50+ events = agent is adding negative value → deactivate from orchestrator.

**Implementation:** Add `AgentForwardReturnTracker` that:
- Subscribes to all agent signal events.
- Records (signal_strength, direction, timestamp) per agent per signal.
- On each candle close, computes realized return at 1h/4h/24h for all open snapshots.
- Writes to `data/agent_forward_returns.jsonl`.

---

## Phase 8: AI Feedback Loop Audit

### What the Loop Does

```
Trade closes → PaperExecutor publishes TRADE_CLOSED
    → TradeAnalysisService.on_trade_closed(trade)
    → score_trade() → root_cause_analyzer() → improvement_recommender()
    → TradeFeedbackStore persists analysis
    → TuningProposalGenerator reads aggregated feedback
    → Generates StrategyTuningProposal (status=pending)
    → UI shows proposals in "AI Tuning" panel
    → AdaptiveLearningPolicy gates proposals (overfitting check)
    → Proposals applied to config only via manual operator action
```

### Structural Assessment

**What works:**
- Every closed trade generates a structured analysis: setup_score, risk_score, execution_score, decision_score, root_causes, recommendations.
- Proposals require ≥10 trades and ≥20% occurrence to trigger.
- `AdaptiveLearningPolicy` blocks proposals when OOS performance degrades.
- Proposals are backtest-gated (status transitions: pending → backtesting → approved/rejected → applied).

**Gaps identified:**

1. **The loop is open — no proposal→outcome measurement.** When a proposal is "applied" (changes a config parameter), the system does not automatically: (a) snapshot the parameter value before/after, (b) measure performance in the N trades following application vs. the N trades before, (c) compute whether the proposal improved expectancy. The `STATUS_APPLIED` terminal state has no feedback path back to the analysis.

2. **AI enrichment quality is unmeasured.** `TradeAnalysisService` calls `LLMProvider` (Ollama deepseek-r1:14b locally) to generate `ai_explanation`. The quality of these explanations is subjective. There is no metric for: (a) how often AI recommendations match realized root causes, (b) whether AI-enriched proposals are more accurate than rule-based proposals.

3. **Root cause catalog is rule-based.** `root_cause_analyzer.py` and `root_cause_catalog.py` use predefined rules (e.g., "entry too early if RSI was in [45-50]"). These rules were authored manually. There is no mechanism to discover new root cause patterns from data.

4. **No A/B measurement framework.** To know if the AI loop improves P&L, you need a baseline (N trades without AI-applied changes) vs. treatment (N trades after applying a specific proposal). This requires careful attribution, which is not currently designed.

### Does the AI Feedback Loop Improve P&L?

**Current evidence: Insufficient to determine.**

The loop has been running since Session 36+ but with few live trades at 0.5% risk. The L1/L2 adaptive weights operate but have 30-trade windows that are too small for statistical validity. The ProbabilityCalibrator is untrained (needs 300 trades). The TuningProposalGenerator requires 10 trades minimum to even start proposing.

**What would constitute evidence:**
- Sharpe ratio trajectory over time (improving = positive signal)
- Per-proposal performance delta (applied proposals improve expectancy ≥ 0.05R)
- L1/L2 multiplier history vs. per-model out-of-sample WR trajectory

**Recommendation:** Add an `AILoopROITracker` that:
1. Snapshots performance baseline (30-trade rolling metrics) whenever a proposal is applied.
2. Measures performance in the 30 trades following application.
3. Computes delta and attributes it (partially) to the proposal.
4. Reports "AI loop lifetime ROI" as a dashboard metric.

---

## Phase 9: Capital Deployment Redesign

### Current State (Verified)

- `risk_pct_per_trade = 0.5%`
- `max_capital_pct = 4%`
- `max_concurrent_positions = 5`
- Peak theoretical utilization = 5 × 4% = 20% of capital.
- Actual average utilization: far lower due to sparse signal frequency at 1h.
- With $100,000 capital and 0.5% risk: max loss per trade = $500. Max concurrent exposure = $2,500.

### Target Design

**Goal:** 70–90% capital utilization in expected-positive market conditions (non-crisis, non-EMERGENCY crash tier), without increasing per-trade risk.

The key insight: **utilization and per-trade risk are independent variables.** We can increase utilization (position counts, position size caps) without changing risk-per-trade (0.5%).

### Proposed Changes

#### 9.1: Increase Max Concurrent Positions

Raise `max_concurrent_positions` from 5 to 10.

**Impact analysis:**
- With 5 symbols × 2 positions each = 10 concurrent positions possible.
- Max theoretical capital deployed = 10 × 4% = 40%.
- Risk budget = 10 × 0.5% = 5% simultaneous stop-fire exposure.
- Portfolio heat cap (currently 6%) governs this naturally — 5% < 6%, so no immediate circuit trip.

**Risk check:** Portfolio heat = sum of all position_size × stop_distance_pct. At 0.5% risk per trade, stop fire on all 10 simultaneous positions = 5% total loss (one bad hour). This is acceptable for demo trading.

#### 9.2: Conviction-Scaled Position Sizing

Implement score-tiered risk allocation:

```python
SCORE_TIERS = [
    (0.80, 1.5),   # score ≥ 0.80 → 1.5× risk (0.75% per trade)
    (0.70, 1.25),  # score ≥ 0.70 → 1.25× (0.625%)
    (0.60, 1.0),   # score ≥ 0.60 → 1.0× (0.5%)
    (0.00, 0.75),  # below 0.60 → 0.75× (0.375%) — barely over threshold
]
```

Cap absolute risk at 1.5% per trade (3× the base 0.5%). This doubles expected P&L on highest-conviction setups vs. lowest-conviction setups that just barely cleared the threshold.

**Important:** The Phase 1 B constraints prohibit changing signal logic, model weights, or risk parameters during demo. Conviction scaling should be implemented as a **new configuration option** with documentation, to be enabled post-Phase 1 demonstration (after 50+ trades with baseline metrics established).

#### 9.3: Dynamic Utilization Monitoring

Add `CapitalUtilizationMonitor`:
```python
class CapitalUtilizationMonitor:
    """Tracks real-time capital utilization."""

    def get_utilization_pct(self) -> float:
        """Sum of all open position sizes / total capital."""

    def get_idle_capital_usdt(self) -> float:
        """Capital not currently deployed."""

    def get_utilization_target(self) -> float:
        """Target from config (default 0.70)."""

    def get_deployment_alert(self) -> Optional[str]:
        """
        Returns alert if utilization is below target for >24h
        in non-crisis conditions. Enables operator to diagnose
        why trades aren't being taken (signal frequency too low?
        risk gate too strict? confluence threshold too high?).
        """
```

#### 9.4: Symbol Weight → Position Size Integration

Currently symbol weights (SOL=1.3, ETH=1.2) affect only score ranking. Proposed: allow weights to also scale the `risk_pct_per_trade` by a bounded factor:

```
effective_risk_pct = base_risk_pct × symbol_weight × (1 / mean_weight)
```

For SOL (weight 1.3), mean weight = (1.3+1.2+1.0+0.8+0.8)/5 = 1.02:
`effective_risk_pct = 0.5% × 1.3 / 1.02 = 0.637%`

For XRP (weight 0.8):
`effective_risk_pct = 0.5% × 0.8 / 1.02 = 0.392%`

This aligns capital allocation with the empirically-established symbol rankings from Study 4.

**This is a Phase 1B+ feature — do not enable during Phase 1 demo.**

### Utilization Gap Summary

| Scenario | Current | After 9.1 (max_pos=10) | After 9.1+9.2 (tiered) |
|----------|---------|----------------------|----------------------|
| Max theoretical utilization | 20% | 40% | 40–60% |
| Avg daily utilization (est.) | 4–8% | 8–16% | 12–24% |
| Max simultaneous risk (stop-fire) | 2.5% | 5% | 7.5% |
| Daily loss limit needed | ❌ Absent | ❌ Absent | **✅ Required** |

Note: If max_positions is increased to 10 and conviction scaling is added, a daily loss limit becomes **mandatory** risk management (not optional). An intra-day loss of 7.5% capital (all stops hitting) would be catastrophic without a daily halt.

---

## Phase 10: Execution & Risk Hardening

### 10.1: Daily Loss Limit Kill Switch

**Gap confirmed in Phase 1 (Dimension 4, Gap #3).**

Required implementation in `PaperExecutor`:

```python
_DAILY_LOSS_LIMIT_PCT = 2.0  # halt new positions if day P&L < -2%

def _check_daily_loss_limit(self) -> bool:
    """
    Returns True if daily loss limit has been breached.
    'Day' = UTC calendar day (reset at 00:00 UTC).
    """
    today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    day_pnl = sum(
        t.get("pnl_usdt", 0) for t in self._closed_trades
        if t.get("closed_at") and
        datetime.fromisoformat(t["closed_at"]) >= today_start
    )
    day_pnl_pct = day_pnl / self._initial_capital * 100
    if day_pnl_pct < -self._DAILY_LOSS_LIMIT_PCT:
        logger.warning(
            "PaperExecutor: DAILY LOSS LIMIT BREACHED — "
            "day P&L = %.2f%% (limit = %.2f%%). "
            "New positions blocked for remainder of session.",
            day_pnl_pct, -self._DAILY_LOSS_LIMIT_PCT
        )
        bus.publish(Topics.SYSTEM_ALERT, {
            "level": "CRITICAL",
            "message": f"Daily loss limit: {day_pnl_pct:.2f}%. Trading halted.",
        })
        return True
    return False
```

Call `_check_daily_loss_limit()` at the start of `submit()` before any position opens.

### 10.2: Idempotent Order Submission

Current gap: If `ScanWorker` fires twice for the same candle (e.g., due to timer drift or scan retry), two identical candidates could be submitted. `RiskGate` prevents adding a second position for the same symbol only after `max_positions_per_symbol` is reached. For the default of 10, two rapid submits for the same symbol would both open.

**Fix:** Add candidate deduplication by (symbol, side, entry_price_rounded) within a scan cycle window:

```python
# In PaperExecutor.submit():
_dedup_key = (candidate.symbol, candidate.side,
               round(candidate.entry_price, 2))
_dedup_expiry = datetime.utcnow() - timedelta(seconds=300)  # 5-min window
if _dedup_key in self._recent_submissions:
    submitted_at = self._recent_submissions[_dedup_key]
    if submitted_at > _dedup_expiry:
        logger.warning("PaperExecutor: duplicate submission blocked for %s", _dedup_key)
        return None
self._recent_submissions[_dedup_key] = datetime.utcnow()
```

### 10.3: Partial Fill Simulation

At current 4% cap / 0.5% risk per trade with $100,000 capital:
- Max position size = $4,000.
- BTC average daily volume on Bybit Demo: $100M+.
- At $4,000 position size, market impact is negligible — 100% fill is realistic for limit orders.

**Verdict:** Partial fill simulation is not a material concern at Phase 1 demo scale ($4,000 positions). It becomes relevant if position sizes reach $50,000+ or if smaller-cap alts are added. No immediate action required.

### 10.4: Commission Model Separation

Currently slippage covers commission implicitly. Separate them for cleaner P&L attribution:

```yaml
# config.yaml additions:
execution:
  commission_pct_maker: 0.02   # Bybit Demo maker fee
  commission_pct_taker: 0.055  # Bybit Demo taker fee
  order_type: limit             # limit = maker fill (0.02%)
```

For limit orders that fill as maker: round-trip commission = 2 × 0.02% = 0.04%. At 0.5% risk/trade: commission = 0.04/0.5 = 8% of risk budget. This is material and should be explicitly logged per trade as `commission_usdt`.

### 10.5: Slippage Seeding for Reproducibility

Current `random.uniform(_SLIPPAGE_MIN, _SLIPPAGE_MAX)` is non-deterministic. For audit trail:

```python
# Record actual slippage applied per trade:
actual_slippage_pct = random.uniform(self._SLIPPAGE_MIN, self._SLIPPAGE_MAX)
# Store in trade record: trade["slippage_pct"] = actual_slippage_pct
```

This allows post-hoc analysis of whether slippage is consistently adverse (e.g., always > 0.03%).

### 10.6: Position Size Minimum Enforcement

Current minimum: `min_size_usdt = 10.0`. At Bybit Demo, minimum order values are approximately:
- BTC: $1 minimum notional
- ETH: $1 minimum notional
- XRP: $1 minimum notional

At 0.5% risk with $100,000 capital and a 5% stop distance: `size = (0.5%×$100k) / 0.05 × price/price = $1,000`. The $10 floor is never triggered in normal operation. However, if capital drops below $2,000 (drawdown scenario), the formula would produce `size = (0.5%×$2k) / 0.05 = $20`, which is above the floor.

**Verdict:** No change needed to the minimum size floor for Phase 1.

### 10.7: Backtest Realism in PaperExecutor

**Same-bar fill problem (Phase 1 Gap #5.6):** A limit order at `close + 0.20×ATR` cannot fill on the same 1h bar that generated the signal (the bar has already closed). Current code allows this if the next `on_tick()` price immediately crosses the entry level.

**Fix:** Add a 1-bar delay for limit order fills. A candidate generated at candle close T should not be fillable until the candle at T+1 opens:

```python
# In PaperPosition or submit():
self._eligible_at = next_candle_open_time  # T + 1 bar
# In update():
if datetime.utcnow() < self._eligible_at:
    return None  # not yet eligible for fill
```

---

*Next: Phases 11–17 (Backtesting, Ablation, Dashboards, Email, Inefficiencies, Decisions, Final Report)*
