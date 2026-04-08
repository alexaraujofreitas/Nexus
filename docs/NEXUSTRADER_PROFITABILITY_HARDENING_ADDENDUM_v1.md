# NexusTrader Intraday Redesign — Profitability Hardening Addendum

**Version:** 1.0
**Date:** 2026-04-06
**Parent Document:** NEXUSTRADER_INTRADAY_REDESIGN_v1.md
**Status:** Addendum — Pending Audit
**Classification:** Internal Engineering

---

## 1. Overview of Profitability Gaps

The V1 redesign document establishes a sound structural foundation — five intraday strategies, two-stage signal pipeline, WebSocket data ingestion, headless execution core. However, it contains nine gaps that, if unaddressed, will degrade live PF below the 1.30 target:

**Gap 1 — No overtrading governor.** The V1 system evaluates all 5 strategies across 16 assets on every 5m/1m close. With 15–31 projected trades/day, the system has no mechanism to distinguish a "10 genuine setups" day from a "10 noise signals in choppy markets" day. In backtesting, overtrading in chop is the single largest PF destroyer — a system that trades 30/day with PF 1.4 in trends can easily trade 30/day with PF 0.7 in chop, yielding a blended PF of ~1.05.

**Gap 2 — No hard no-trade conditions.** V1 defines spread-based deferral (>0.20%) and crash defense tiers, but lacks explicit definitions for dead markets, chaotic spikes, cross-timeframe conflict, and liquidity degradation.

**Gap 3 — Static execution assumptions.** V1 assumes Tier 1 slippage of 0.01–0.03% and a fixed limit-first → market-fallback strategy. In practice, slippage varies 3–5× within a single session based on liquidity conditions. A static model will systematically underperform during liquidity-thin periods.

**Gap 4 — No cross-strategy coordination.** MX and RBR are both breakout strategies that can fire simultaneously on the same asset. VR and MPC can produce opposing signals. Without a coordination layer, the system can enter two positions on the same asset with partially overlapping risk, doubling effective exposure.

**Gap 5 — Equal asset treatment.** All 16 assets receive identical bias_score thresholds and risk allocation. In reality, SOL at 2.5% daily ATR ratio offers 3× more R-opportunity than BTC at 0.8% daily ATR ratio for the same fee drag.

**Gap 6 — Static time stops.** V1 assigns fixed time stops per strategy (45–90 minutes). A 45-minute time stop on MPC is correct in strong trends but may cut winners short in moderate trends where the move takes 60 minutes to develop.

**Gap 7 — No pre-execution quality gate.** V1's two-stage pipeline (Stage A bias_score ≥ 0.35 → Stage B trade_score ≥ 0.40) does not incorporate real-time market microstructure (spread, book depth, recent execution quality) into the final trade decision.

**Gap 8 — No closed-loop learning.** V1 retains the L1/L2 adaptive weight system from the swing design but does not specify how intraday-specific performance data (per-strategy, per-asset, per-regime) feeds back to throttle or boost trading activity.

**Gap 9 — No failure mode automation.** V1's drawdown circuit breakers are threshold-based (-5%, -8%, -10%, -15%). It lacks detection of subtler degradation patterns: PF declining from 1.4 to 1.05 over 3 days without hitting drawdown limits, or execution quality systematically worsening.

Each section below addresses one or more of these gaps with specific logic, thresholds, and system behavior.

---

## 2. Global Trade Filtering System

### 2.1 Architecture

The Global Trade Filter (GTF) sits between Stage B (trigger evaluation) and the RiskGate. Every trade that passes Stage B must also pass GTF before reaching RiskGate. GTF is stateful — it maintains rolling windows of market conditions and system performance.

```
Stage B Trigger → Global Trade Filter → RiskGate → ExecutionManager
                        │
            ┌───────────┼───────────────┐
            ▼           ▼               ▼
     Regime Throttle  Vol Filter   Loss Streak Gate
            ▼           ▼               ▼
     Cluster Guard  Session Budget   Chop Detector
```

### 2.2 Regime-Based Throttle

**Purpose:** Reduce trade frequency when market conditions favor noise over signal.

The 15m regime classification (from RegimeEngine) drives a per-symbol `activity_multiplier` that scales the maximum allowed trades per symbol per hour:

| Regime (15m) | Activity Multiplier | Max Trades/Symbol/Hour | Rationale |
|---|---|---|---|
| `trend_bull` or `trend_bear` | 1.0 | 3 | Full activity — trends produce clean setups |
| `ranging` | 0.6 | 2 | Reduced — range-bound price generates false breakouts |
| `volatile` (ATR spike > 2× baseline) | 0.8 | 2 | Moderate — high vol creates opportunity but also noise |
| `chop` (see §2.4) | 0.2 | 1 | Heavily restricted — only highest-quality setups |

**Implementation:** On each Stage B trigger, GTF checks the 15m regime for the asset. If the count of trades opened for that symbol in the current clock hour ≥ `max_trades × activity_multiplier`, the trigger is suppressed. The suppressed trigger is logged with reason `GTF_REGIME_THROTTLE`.

**Global throttle:** Across all symbols combined, maximum 8 trades per rolling 30-minute window. This prevents the pathological case where 8 assets simultaneously enter trending regimes and the system opens 8 positions in 5 minutes. The 30-minute window is a sliding counter, not a fixed window.

### 2.3 Volatility Filter

**Purpose:** Suppress trading when ATR conditions make setups unreliable.

Two sub-filters operate independently:

**ATR Compression Filter:**
- Metric: `atr_ratio = ATR(14) on 5m / ATR(14) on 5m 50-bar rolling median`
- Threshold: `atr_ratio < 0.40`
- Behavior: When `atr_ratio < 0.40`, the asset is in ultra-low volatility. All strategies except VR (VWAP Reclaim) are suppressed for this asset. VR remains active because VWAP interactions in compressed markets are the highest-WR setup in the portfolio.
- Recovery: Resume full activity when `atr_ratio > 0.55` for 3 consecutive bars (15 minutes). Hysteresis prevents flicker.

**ATR Explosion Filter:**
- Metric: `atr_ratio > 3.0` (ATR is 3× the 50-bar median)
- Behavior: Suppress MPC (Micro Pullback) and RBR (Range Breakout Reclaim) — these patterns become unreliable in extreme volatility because pullback structures collapse. MX (Momentum Expansion) and LSR (Liquidity Sweep) remain active — they thrive in high-vol environments. VR remains active with widened stops (1.2× standard ATR multiplier instead of 0.8×).
- Recovery: Resume full activity when `atr_ratio < 2.5` for 3 consecutive bars.

### 2.4 Chop Detector

**Purpose:** Identify and suppress trading in directionless, whipsaw markets — the #1 PF destroyer for intraday systems.

**Detection logic:**

A symbol is classified as `chop` when ALL of the following are true on the 15m timeframe, evaluated over the last 20 bars (5 hours):

1. `ADX(14) < 18` — weak trend strength
2. `EMA(9)` has crossed EMA(21) ≥ 3 times in the last 20 bars — directional indecision
3. `abs(close[0] - close[-20]) / ATR(14) < 0.5` — net price displacement is less than half an ATR — price is going nowhere
4. The last 3 5m candles have alternating direction (green-red-green or red-green-red) — micro-whipsaw

**System behavior when chop detected:**
- `activity_multiplier = 0.2` (max 1 trade per symbol per hour)
- `bias_score_threshold` raised from 0.35 to 0.55 (only exceptional setups pass Stage A)
- `trade_score_threshold` raised from 0.40 to 0.55 (only exceptional triggers pass Stage B)
- MX and RBR disabled entirely for this symbol (breakouts in chop are false signals)
- Only VR, MPC (with ADX gate already > 25, so MPC self-filters), and LSR remain eligible

**Chop exit:** Chop classification clears when `ADX(14) > 22` for 2 consecutive 15m bars AND net displacement exceeds 1.0 × ATR.

### 2.5 Loss Streak Gate

**Purpose:** Progressively restrict trading after consecutive losses.

| Consecutive Losses | Action |
|---|---|
| 3 | Raise `trade_score_threshold` by +0.05 (from 0.40 to 0.45). Log warning. |
| 5 | Raise `trade_score_threshold` by +0.10 (to 0.50). Reduce `risk_pct_per_trade` to 0.15% (from 0.25%). Disable the strategy with the worst 10-trade rolling PF. |
| 7 | Pause ALL trading for 30 minutes. After pause, resume with 0.15% risk and threshold +0.10. |
| 10 | Pause ALL trading for 2 hours. Send notification. When resuming, enter "recovery mode" (§10.2). |

**Loss streak counter:** Tracked globally (not per-strategy, not per-asset). Rationale: per-strategy streaks are too granular — a system-wide losing streak indicates a market condition problem, not a strategy-specific one.

**Reset:** The loss streak counter resets to 0 after 2 consecutive winners.

### 2.6 Trade Clustering Prevention

**Purpose:** Prevent the system from opening multiple positions within a very short window, which concentrates execution risk and creates correlated entries.

**Rule:** After any trade entry, impose a 60-second global cooldown before the next entry is allowed. During the cooldown:
- Stage A and Stage B continue evaluating (setups are not lost)
- Triggers that fire during the cooldown are queued (max queue depth: 3)
- After cooldown expires, the queued trigger with the highest `trade_score` is executed first
- Remaining queued triggers are re-evaluated (they may no longer be valid if price has moved)

**Per-symbol cooldown:** 5 minutes between entries on the same symbol (regardless of strategy). This prevents the degenerate case where MX opens a long on BTC, price dips 0.1%, and MPC immediately opens another long on BTC.

### 2.7 Session Budget

**Purpose:** Hard cap on daily trading activity to prevent runaway behavior.

| Budget | Limit | Reset |
|---|---|---|
| Max trades per UTC day | 60 | UTC 00:00 |
| Max trades per 4-hour block | 20 | Rolling 4h window |
| Max rejected-by-SL trades per day | 25 | UTC 00:00 (if 25 SL hits in one day, market is unfavorable — stop trading) |
| Max capital churned per day | 300% of equity | Sum of all position sizes opened / equity. Prevents 100× small trades. |

When any budget is exhausted, the system enters "session end" mode: all new entries suppressed, existing positions managed to completion (SL/TP/time stop), system resumes at next budget reset.

---

## 3. No-Trade Conditions Framework

### 3.1 Hard No-Trade Registry

Each condition below forces immediate suppression of all new trade entries. Existing positions continue to be managed (SL/TP/trailing/time stop are unaffected).

#### Condition 1: Dead Market

**Detection:**
- 1h ATR(14) / close < 0.0015 (0.15% — BTC moving less than $100/hour at $67K)
- AND 5m volume on last 6 bars is below 20th percentile of the 200-bar volume distribution

**Threshold justification:** At 0.15% hourly ATR, a typical stop of 1.2× ATR(14) on 5m is ~0.05% of price. With 0.04%/side fees (0.08% round trip), the risk:fee ratio is 0.05:0.08 — fees consume 160% of the risk unit. No intraday strategy can be profitable in this regime.

**Behavior:** All entries suppressed. Resume when 1h ATR/close > 0.0025 for 2 consecutive bars.

**Logging:** `NO_TRADE: DEAD_MARKET symbol={} atr_ratio={:.4f} vol_pctile={}`

#### Condition 2: Chaotic Spike

**Detection:**
- Any 1m candle's range (high - low) > 4× the 30-bar ATR(14) on 1m
- OR: 3 consecutive 1m candles each with range > 2.5× ATR(14) on 1m

**Threshold justification:** A single 1m candle with 4× ATR indicates a flash event (liquidation cascade, news shock, exchange glitch). Stops placed at normal ATR distances will be blown through with extreme slippage. The 3-consecutive-bars version catches sustained chaos.

**Behavior:** Suppress all new entries for 5 minutes after the last chaotic bar. Existing positions: if CrashDefenseController is in DEFENSIVE or higher tier, CDA handles them. If not, tighten trailing stops to 0.5× ATR (half normal).

**Logging:** `NO_TRADE: CHAOTIC_SPIKE symbol={} candle_range={:.2f}% atr={:.2f}%`

#### Condition 3: Spread Widening

**Detection:**
- Current bid-ask spread > 0.15% (from `order_book_agent` cache)

This supersedes V1's 0.20% threshold. Rationale: at 0.15% spread, effective execution cost is 0.075% per side (spread/2) + 0.04% fee = 0.115% per side. Round-trip: 0.23%. For a typical 1.5R trade with 0.8% stop, the target is 1.2% — fees+spread consume 19% of the target. Above 0.15%, this ratio degrades quickly.

**Behavior:**
- 0.15–0.20%: Suppress all entries except LSR (liquidity sweep reclaim — benefits from spread widening because sweep candles create wider wicks that produce better entries)
- > 0.20%: Suppress ALL entries including LSR

**Recovery:** Resume when spread < 0.12% for 60 consecutive seconds (sustained tightening, not a momentary flicker).

#### Condition 4: Cross-Timeframe Conflict

**Detection:**
- 1h bias direction ≠ 15m regime direction for a given asset
- Specifically: 1h EMA20>EMA50 (bullish) but 15m regime = `trend_bear`, or vice versa

**Behavior:** For the conflicting asset only:
- Suppress MPC (pullback continuation) — requires trend alignment across timeframes
- Suppress MX (momentum expansion) — breakouts against higher-TF trend have low WR
- Allow VR (VWAP operates on session-level, less TF-dependent) and LSR (sweep-based, regime-independent)
- Allow RBR only if the range formed entirely within the conflict period (range is defined by the conflict itself)

**Logging:** `NO_TRADE: TF_CONFLICT symbol={} 1h_bias={} 15m_regime={}`

#### Condition 5: Liquidity Degradation

**Detection (from `order_book_agent`):**
- Sum of bid depth within 0.5% of mid price < $30,000 (for Tier 1 assets) or < $15,000 (for Tier 2/3)
- OR: bid-ask depth ratio > 3:1 or < 1:3 (extreme imbalance — one side of the book is absent)

**Behavior:** Suppress all entries for the affected symbol. Resume when depth recovers above threshold for 2 consecutive 30s polls.

**Logging:** `NO_TRADE: THIN_BOOK symbol={} bid_depth={:.0f} ask_depth={:.0f}`

### 3.2 No-Trade Summary Matrix

| Condition | Scope | Detection TF | Cooldown | Strategies Affected |
|---|---|---|---|---|
| Dead market | Per symbol | 1h + 5m | Until ATR recovers | All |
| Chaotic spike | Per symbol | 1m | 5 min after last spike bar | All |
| Spread > 0.15% | Per symbol | Real-time (30s poll) | Until < 0.12% for 60s | All except LSR (0.15–0.20%) |
| TF conflict | Per symbol | 15m + 1h | Until alignment | MPC, MX (VR, LSR, RBR conditional) |
| Thin book | Per symbol | Real-time (30s poll) | Until depth recovers × 2 polls | All |

---

## 4. Execution Adaptation Engine

### 4.1 Execution Quality Tracking

Every fill generates an `ExecutionRecord`:

```python
@dataclass
class ExecutionRecord:
    symbol: str
    timestamp: float
    strategy: str
    intended_price: float       # price at signal time (1m close)
    fill_price: float           # actual fill from exchange
    slippage_bps: float         # (fill - intended) / intended × 10000
    order_type: str             # "limit" or "market"
    fill_latency_ms: int        # signal timestamp → fill confirmation
    limit_fill_success: bool    # True if limit filled within 5s window
    spread_at_entry_bps: float  # spread at order submission time
```

Records are stored in a rolling window: 200 per symbol, 1000 globally. Updated on every fill.

### 4.2 Per-Asset Execution Profile

The engine maintains a live `ExecutionProfile` per symbol, recomputed every 10 fills:

```
ExecutionProfile:
  avg_slippage_bps: float         # mean slippage (last 50 fills)
  slippage_p95_bps: float         # 95th percentile slippage
  limit_fill_rate: float          # % of limit orders that filled within 5s
  avg_fill_latency_ms: float      # mean signal-to-fill latency
  execution_score: float          # composite 0.0–1.0 (see §4.3)
```

### 4.3 Execution Quality Score

Each symbol receives a composite `execution_score` (0.0–1.0):

```
execution_score = (
    0.35 × limit_fill_component +
    0.30 × slippage_component +
    0.20 × latency_component +
    0.15 × spread_component
)

Where:
  limit_fill_component = limit_fill_rate  (0.0 to 1.0 directly)
  slippage_component = max(0, 1.0 - avg_slippage_bps / 10.0)  (0 at 10bps, 1.0 at 0bps)
  latency_component = max(0, 1.0 - avg_fill_latency_ms / 2000)  (0 at 2000ms, 1.0 at 0ms)
  spread_component = max(0, 1.0 - spread_at_entry_bps / 15.0)  (0 at 15bps, 1.0 at 0bps)
```

### 4.4 Adaptive Execution Behavior

The `execution_score` drives three adaptive mechanisms:

**A. Order Type Selection (per asset):**

| `limit_fill_rate` (50-trade rolling) | Behavior |
|---|---|
| ≥ 80% | Limit-first (5s timeout → market IOC fallback). Default. |
| 60–79% | Limit with aggressive pricing: place limit at ask + 1 tick (longs) / bid - 1 tick (shorts). 3s timeout → market. |
| < 60% | Market-first. Limit orders are not economically viable for this asset — the 5s wait costs more in adverse price movement than the maker fee savings. |

**B. Position Size Adjustment:**

If `slippage_p95_bps > 8.0` for a symbol, reduce `max_capital_pct` for that symbol by the excess:
```
adjusted_max_capital_pct = base_max_capital_pct × max(0.5, 1.0 - (slippage_p95 - 8.0) / 20.0)
```
Example: base 3%, slippage_p95 = 15bps → adjustment factor = 1.0 - 7/20 = 0.65 → effective cap = 1.95%.

This ensures that assets with poor execution automatically receive smaller positions — the system self-corrects without manual intervention.

**C. Trade Skip on Execution Degradation:**

If `execution_score < 0.40` for a symbol, suppress all new entries for that symbol. This catches the compound case: high slippage + wide spread + low limit fill rate + high latency. Resume when `execution_score > 0.55` (hysteresis).

Log: `EXECUTION_SKIP symbol={} exec_score={:.2f} components={}`

### 4.5 Execution Scoring on Trade Records

Every closed trade is annotated with the `execution_score` at entry time. This feeds into the post-trade learning loop (§9) so that the system can correlate execution quality with trade outcome — a trade that entered with `execution_score < 0.50` and lost may not indicate a bad strategy; it may indicate bad execution conditions that should have been filtered.

---

## 5. Portfolio Coordination Layer

### 5.1 Per-Symbol Strategy Limit

**Rule:** Maximum 1 active position per symbol at any time.

V1 allowed 2 positions per symbol (different strategies). This is revised downward. Rationale: two positions on the same asset from different strategies (e.g., MX long and MPC long on SOL) create redundant exposure. If SOL drops, both positions lose — the strategies are decorrelated in entry logic but perfectly correlated in loss exposure.

**Exception:** If the second signal is in the OPPOSITE direction from the existing position, it is treated as a close signal for the existing position + potential reversal. The system closes the existing position first, waits one bar (60 seconds), then evaluates whether the reversal signal is still valid. If valid, open the new position. If the reversal signal is stale, do nothing.

### 5.2 Directional Exposure Control

**Rule:** Maximum net directional exposure across all symbols:

| Metric | Limit |
|---|---|
| Max long positions simultaneously | 6 (of 8 total max concurrent) |
| Max short positions simultaneously | 6 (of 8 total max concurrent) |
| Max positions in same direction on correlated pair | 2 |
| Max same-direction positions in Tier 1 | 3 |

**Correlated pair definition:** BTC+ETH (correlation ~0.85), SOL+AVAX (~0.65), LINK+DOT (~0.60). Maintained in a static correlation matrix, updated monthly from trailing 30-day 1h returns.

**Implementation:** When a new trigger fires (long on AVAX), the Portfolio Coordination Layer checks: how many existing long positions are on assets correlated > 0.60 with AVAX? If ≥ 2, the trigger is suppressed. This prevents the system from being "5× long the crypto market" via BTC + ETH + SOL + AVAX + LINK.

### 5.3 Strategy Conflict Resolution

When two strategies produce opposing signals for the same asset in the same 5m window:

1. Compare `trade_score` of each signal.
2. If difference > 0.10: the higher-scoring signal takes priority. The lower signal is discarded.
3. If difference ≤ 0.10: both signals are discarded (conflict = uncertainty). Log: `CONFLICT_DISCARD symbol={} strat_a={} score_a={:.2f} strat_b={} score_b={:.2f}`

When two strategies produce same-direction signals for the same asset:

1. Only the higher-scoring signal is executed (§5.1 — max 1 position per symbol).
2. The winning signal inherits a `conviction_boost` of +0.05 to its `trade_score` (two strategies agreeing is a positive confluence signal). This boost does NOT bypass thresholds — it only makes the winning signal slightly larger in the position sizer if it crosses the quality threshold.

### 5.4 Strategy Deduplication

MX (Momentum Expansion) and RBR (Range Breakout Reclaim) share structural similarity — both enter on breakouts. To prevent near-duplicate signals:

**Dedup rule:** If MX fires on symbol X within the same 5m candle that produced the RBR setup, check if the MX entry level is within 0.5× ATR of the RBR retest level. If yes, treat them as duplicates — only the one with higher `trade_score` executes.

Similarly, VR (VWAP Reclaim) and MPC (Micro Pullback) can fire on the same symbol if price is pulling back to an EMA that is near VWAP. Dedup: if VR entry level is within 0.3× ATR of MPC entry level, treat as duplicate.

---

## 6. Asset Ranking System

### 6.1 Dynamic Asset Score

Every 15 minutes (on 15m close), each symbol in the universe receives an `asset_score` (0.0–1.0):

```
asset_score = (
    0.30 × volatility_score +
    0.25 × trend_score +
    0.20 × volume_score +
    0.15 × spread_score +
    0.10 × recent_performance_score
)
```

**Components:**

**Volatility Score (0.0–1.0):**
```
atr_pct = ATR(14) on 5m / close × 100
volatility_score = clip((atr_pct - 0.10) / (0.60 - 0.10), 0, 1)
```
Mapping: 0.10% ATR → 0.0 (too quiet), 0.60% ATR → 1.0 (ideal intraday volatility), >0.60% still 1.0 (high vol is opportunity). Assets below 0.10% ATR/close are effectively dead markets (caught by no-trade condition §3.1.1) and score zero.

**Trend Score (0.0–1.0):**
```
adx = ADX(14) on 15m
trend_score = clip((adx - 15) / (40 - 15), 0, 1)
```
ADX < 15 → 0.0 (no trend = difficult for MPC, MX). ADX > 40 → 1.0 (strong trend = high-WR environment).

**Volume Score (0.0–1.0):**
```
vol_ratio = current_5m_volume / median_5m_volume(50)
volume_score = clip((vol_ratio - 0.5) / (2.0 - 0.5), 0, 1)
```
Volume below 50% of median → 0.0 (inactive). Volume at 2× median → 1.0 (strong participation).

**Spread Score (0.0–1.0):**
```
spread_score = clip(1.0 - spread_bps / 12.0, 0, 1)
```
0bps spread → 1.0. ≥12bps → 0.0. From `order_book_agent` cache.

**Recent Performance Score (0.0–1.0):**
```
recent_pf = PF of last 20 trades on this symbol (across all strategies)
performance_score = clip((recent_pf - 0.8) / (2.0 - 0.8), 0, 1)
```
PF ≤ 0.8 → 0.0. PF ≥ 2.0 → 1.0. If < 20 trades, default to 0.5 (neutral).

### 6.2 Tiered Allocation Based on Asset Score

| Asset Score Range | Tier | Behavior |
|---|---|---|
| ≥ 0.70 | **Active+** | Full allocation. `bias_score_threshold = 0.30` (slightly permissive). `max_capital_pct = 3.0%`. Priority in trade queue. |
| 0.45–0.69 | **Active** | Standard allocation. `bias_score_threshold = 0.35` (default). `max_capital_pct = 2.5%`. |
| 0.25–0.44 | **Reduced** | Reduced allocation. `bias_score_threshold = 0.45` (stricter). `max_capital_pct = 1.5%`. Only VR and LSR strategies active. |
| < 0.25 | **Dormant** | No new entries. Existing positions managed to exit. Resume when score > 0.35 for 2 consecutive 15m bars. |

### 6.3 Intraday Re-Ranking

Asset scores are recalculated every 15m close. This means the system dynamically shifts capital toward the assets that are currently most tradeable.

**Example scenario:** At 10:00 UTC, BTC (asset_score 0.82) and DOGE (asset_score 0.71) are both Active+. At 12:00 UTC, BTC enters a dead zone (ATR compresses, vol drops), and its score falls to 0.38 (Reduced). DOGE, experiencing a breakout, rises to 0.88 (Active+). The system automatically concentrates setup evaluation and capital allocation toward DOGE and away from BTC — no manual intervention.

### 6.4 Capital Allocation Weighting

When multiple triggers pass all filters simultaneously and compete for capital, the trade from the highest-`asset_score` symbol is executed first. The remaining trades are queued (§2.6 clustering prevention) and re-evaluated in descending `asset_score` order.

---

## 7. Time Stop Optimization

### 7.1 Per-Strategy Dynamic Time Stops

V1 defines static time stops: MX 90m, VR 60m, MPC 45m, RBR 75m, LSR 60m.

The hardened system replaces these with adaptive time stops that respond to real-time conditions:

**Base time stop** = V1 value (unchanged).

**Modifiers (multiplicative, applied to base):**

| Condition | Modifier | Rationale |
|---|---|---|
| Position is > +0.5R profit | ×1.3 (extend) | Trade is working — give it more time to reach target |
| Position is between +0.3R and +0.5R | ×1.15 (slight extend) | Trending in the right direction |
| Position is between -0.3R and +0.3R | ×1.0 (no change) | Neutral — base time is correct |
| Position is between -0.5R and -0.3R | ×0.8 (reduce) | Trending against — cut faster |
| Position is > -0.5R (underwater but not at SL) | ×0.6 (aggressively reduce) | Likely a failed setup — exit before SL triggers |
| ADX(14) on 5m currently > 30 (strong trend) | ×1.2 (extend in trends) | Trends need more time to fully express |
| Chop detected (§2.4) | ×0.7 (reduce in chop) | Chop erodes setups faster |

**Modifiers stack multiplicatively.**

Example: MPC (base 45m). Position is +0.6R (×1.3) AND 5m ADX is 35 (×1.2). Effective time stop = 45 × 1.3 × 1.2 = 70.2 minutes.

Example: RBR (base 75m). Position is -0.4R (×0.8) AND chop detected (×0.7). Effective time stop = 75 × 0.8 × 0.7 = 42 minutes.

**Hard bounds:** Time stop can never be extended beyond 120 minutes (absolute max) or reduced below 15 minutes (absolute min).

### 7.2 Time Stop Validation Tracking

Every time-stopped trade is annotated with a `would_have_won` flag computed post-hoc:

```
On time stop exit at time T with direction D:
  Monitor price for next 60 minutes (T to T+60)
  would_have_won = True if price reached the trade's TP level within T+60
```

This flag is stored in the trade record. Rolling 50-trade window tracking:

```
time_stop_regret_rate = count(time_stopped AND would_have_won) / count(time_stopped)
```

**Adaptive response:**
- If `time_stop_regret_rate > 0.35` for a strategy (more than 35% of time stops would have been winners): increase that strategy's base time stop by 10 minutes. Cap at one increase per week.
- If `time_stop_regret_rate < 0.15` (very few time stops were premature): decrease that strategy's base time stop by 5 minutes. Cap at one decrease per week.

This creates a slow, evidence-based feedback loop that optimizes time stops without overfitting to recent data.

---

## 8. Trade Quality Scoring Model

### 8.1 Trade Quality Score (TQS)

TQS is the final gate before any trade reaches the ExecutionManager. It synthesizes setup quality, trigger strength, market microstructure, and execution conditions into a single score (0.0–1.0).

```
TQS = (
    0.30 × setup_quality +
    0.25 × trigger_quality +
    0.20 × microstructure_quality +
    0.15 × execution_quality +
    0.10 × context_quality
)
```

### 8.2 Component Definitions

**Setup Quality (0.0–1.0):**
The `bias_score` from Stage A, normalized. Already computed in V1 as a 0.0–1.0 composite of 1h trend alignment, 15m ADX, regime confidence, and agent context. Passed through directly.

**Trigger Quality (0.0–1.0):**
The `trigger_score` from Stage B, normalized. Already computed in V1 as a composite of volume confirmation, price action quality, RSI alignment. Passed through.

**Microstructure Quality (0.0–1.0):**
Real-time market microstructure at the moment of trigger evaluation:
```
spread_component = max(0, 1.0 - spread_bps / 15.0)
depth_component = clip(bid_depth_50bps / 50000, 0, 1)  # $50K depth within 50bps = 1.0
vol_freshness = clip(last_1m_volume / vol_ma20, 0, 1.5) / 1.5  # recent volume vs average
microstructure_quality = 0.40 × spread_component + 0.35 × depth_component + 0.25 × vol_freshness
```

**Execution Quality (0.0–1.0):**
The per-asset `execution_score` from §4.3. Captures recent fill quality, slippage, latency.

**Context Quality (0.0–1.0):**
Broader market context:
```
crash_component = 1.0 - (crash_score / 10.0)  # 0 at crash score 10, 1.0 at score 0
agent_alignment = 1.0 if funding_rate is not extreme, else 0.5
chop_penalty = 0.5 if chop detected for any Tier 1 asset (market-wide chop), else 1.0
context_quality = 0.40 × crash_component + 0.30 × agent_alignment + 0.30 × chop_penalty
```

### 8.3 TQS Threshold and Behavior

| TQS Range | Action |
|---|---|
| ≥ 0.55 | **Execute.** Full position size. |
| 0.45–0.54 | **Execute with caution.** Position size reduced to 70% of computed size. |
| 0.35–0.44 | **Execute minimal.** Position size reduced to 40% of computed size. Only if `trade_score > 0.50` (high conviction required to overcome poor conditions). |
| < 0.35 | **Reject.** Trade not executed. Log: `TQS_REJECT symbol={} tqs={:.2f} components={}` |

**Important:** TQS replaces V1's simple `trade_score ≥ 0.40` threshold. The `trade_score` is now one input to TQS (via setup_quality and trigger_quality), not the final gate. The TQS adds microstructure, execution quality, and context awareness that the `trade_score` cannot capture.

### 8.4 TQS Calibration

After the first 200 trades in paper testing, compute:
- Mean TQS of winning trades vs. losing trades
- If the difference is < 0.05, the TQS is not discriminating — re-weight components based on correlation with trade outcome
- If winning trades cluster above 0.55 and losing below 0.45, the TQS is well-calibrated

Target: the top TQS quartile (0.60+) should have PF > 1.6, and the bottom quartile (0.35–0.44) should have PF between 0.95–1.05. If the bottom quartile is profitable, the threshold is too low — raise it.

---

## 9. Learning & Adaptation Loop

### 9.1 Rolling Performance Matrices

Three interlocking performance matrices, each tracked as a rolling window:

**Matrix A: Strategy × Regime (50-trade cells)**
```
              trend_bull  trend_bear  ranging  volatile  chop
MX              [PF,WR]    [PF,WR]   [PF,WR]  [PF,WR]  [PF,WR]
VR              [PF,WR]    [PF,WR]   [PF,WR]  [PF,WR]  [PF,WR]
MPC             [PF,WR]    [PF,WR]   [PF,WR]  [PF,WR]  [PF,WR]
RBR             [PF,WR]    [PF,WR]   [PF,WR]  [PF,WR]  [PF,WR]
LSR             [PF,WR]    [PF,WR]   [PF,WR]  [PF,WR]  [PF,WR]
```

**Matrix B: Strategy × Asset (50-trade cells)**
```
Rows: 5 strategies. Columns: 16 assets. Values: [PF, WR, avg_R].
```

**Matrix C: Strategy × Hour-of-Day (30-trade cells)**
```
Rows: 5 strategies. Columns: 24 UTC hours. Values: [PF, WR, trade_count].
```

### 9.2 Automatic Strategy Throttling

**Check frequency:** Every 25 new trades (not time-based — ensures statistical relevance).

**Rule for Strategy × Regime:**
If a cell has ≥ 20 trades AND PF < 0.90, that strategy is automatically disabled in that regime until:
- 30 calendar days pass (forced cool-off), OR
- The regime has changed for > 4 hours and the system has no data in the new regime phase

If PF is 0.90–1.10, reduce `risk_pct_per_trade` for that strategy-regime combination to 60% of standard.

**Rule for Strategy × Asset:**
If a cell has ≥ 20 trades AND PF < 0.85, that strategy is disabled for that asset. The asset remains tradeable by other strategies.

**Rule for Strategy × Hour:**
If a cell has ≥ 15 trades AND PF < 0.80, that strategy is suppressed during that UTC hour. This captures time-of-day effects (e.g., MX may underperform during UTC 04:00–06:00 low-volume Asian session overlap).

### 9.3 Automatic Boosting

Symmetrically, strong-performing cells receive positive adaptation:

If a cell has ≥ 30 trades AND PF > 1.8 AND WR > 55%:
- `bias_score_threshold` for that (strategy, regime/asset/hour) is reduced by 0.03 (more permissive — allow slightly weaker setups to pass)
- `risk_pct_per_trade` is increased to 130% of standard for that combination (max 0.35%)

**Safety bound:** Boost never exceeds 130% of standard risk. Threshold never drops below 0.28.

### 9.4 Persistence and Decay

All performance matrices persist to JSON files (same pattern as V1's `level2_tracker.json`). Loaded on startup.

**Decay:** Cells older than 30 days are exponentially decayed:
```
effective_weight = exp(-0.03 × age_days)
```
A 30-day-old trade has weight 0.41. A 60-day-old trade has weight 0.17. This ensures the system adapts to recent market microstructure without discarding all historical context.

### 9.5 Meta-Strategy Allocation

Every UTC day at 00:00, the system computes a `strategy_health` score for each of the 5 strategies based on the last 7 days of performance:

```
strategy_health = 0.50 × trailing_7d_PF_normalized + 0.30 × trailing_7d_WR_normalized + 0.20 × consistency_score

Where:
  trailing_7d_PF_normalized = clip((PF - 0.8) / (2.0 - 0.8), 0, 1)
  trailing_7d_WR_normalized = clip((WR - 0.35) / (0.65 - 0.35), 0, 1)
  consistency_score = 1.0 - (std_dev_of_daily_PnL / mean_abs_daily_PnL)  clipped [0, 1]
```

**Behavior by `strategy_health`:**

| Health | Action |
|---|---|
| ≥ 0.60 | Fully active |
| 0.40–0.59 | Active but size reduced to 70% |
| 0.20–0.39 | Only highest-quality setups (TQS ≥ 0.60 required) |
| < 0.20 | Disabled for the UTC day. Re-evaluate at next 00:00. |

---

## 10. Failure Mode Protection

### 10.1 Degradation Detectors

Five automated detectors run continuously:

**Detector 1: PF Drift**
- Metric: Rolling 50-trade PF, sampled every 10 new trades
- Alert: PF < 1.10 (below target but not catastrophic)
- Action: Raise `trade_score_threshold` by +0.05. Log alert.
- Critical: PF < 0.95 (system is losing money)
- Action: Enter "conservation mode" — only TQS ≥ 0.60 trades execute, risk reduced to 0.15%, MX and LSR disabled (highest variance strategies).

**Detector 2: Abnormal Loss Clustering**
- Metric: Count losses in rolling 20-trade window
- Alert: ≥ 14 losses in 20 trades (WR < 30%)
- Action: Immediate 2-hour pause. Send notification. On resume, enter recovery mode (§10.2).
- This catches the case where PF might still be > 1.0 (few large winners) but the WR collapse indicates strategy logic is misaligned with current market conditions.

**Detector 3: Execution Anomaly**
- Metric: Rolling 20-fill average slippage
- Alert: Average slippage > 8bps (vs. normal 2–4bps) for > 5 consecutive fills
- Action: Switch all orders to market-only (eliminate limit wait time that causes worse fills in moving markets). If slippage persists > 12bps, suppress new entries for the affected symbol.

**Detector 4: Data Inconsistency**
- Metric: Compare last 1m OHLCV from WS against REST spot-check (every 5 minutes, one random symbol)
- Alert: Price divergence > 0.5% between WS and REST
- Action: Flag data pipeline as suspect. Switch to REST-only mode for all data. Log critical. If divergence persists for 3 consecutive checks (15 minutes), shut down trading and send notification.

**Detector 5: Latency Drift**
- Metric: Rolling 20-trade P95 end-to-end latency (candle close → fill)
- Alert: P95 > 5000ms (vs. normal ~1500ms)
- Action: Investigate VPN/network. Temporarily increase order timeout from 5s to 10s for limit orders. If P95 > 10000ms, switch to market-only orders. If P95 > 15000ms, pause trading — execution is too slow to be safe.

### 10.2 Recovery Mode

When the system enters recovery mode (triggered by loss streak 10 (§2.5), PF < 0.95, or WR < 30%):

**Recovery mode parameters:**
- `risk_pct_per_trade`: 0.10% (40% of standard 0.25%)
- `max_concurrent_positions`: 4 (half of standard 8)
- `trade_score_threshold`: 0.55 (+0.15 above standard)
- TQS threshold: 0.60 (only high-quality trades)
- Only 3 strategies active: VR, MPC, RBR (the three highest-WR strategies)
- Session budget: 20 trades/day (vs. 60 standard)

**Exit recovery mode when:**
- Rolling 30-trade PF > 1.20 AND WR > 48%, OR
- 72 hours have passed with no further degradation triggers

Recovery mode is designed to keep the system active at minimal risk while it "re-learns" the current market. Complete shutdown is reserved for -10% drawdown (V1 §10.4).

### 10.3 Automatic Daily Health Report

At UTC 23:00 daily, the system generates a health report logged to `reports/intraday_health/`:

```
Date: 2026-04-06
Trades: 22 (target: 15-30) ✓
PF (today): 1.38 ✓ | PF (7d rolling): 1.25 ⚠
WR (today): 54.5% ✓ | WR (7d rolling): 51.2% ✓
Drawdown: -1.8% from peak (threshold: -5%) ✓
Avg TQS: 0.58 | Min TQS executed: 0.41
Avg Slippage: 3.2bps | P95 Slippage: 7.1bps
Avg Latency: 890ms | P95 Latency: 2100ms
Strategies: MX(✓) VR(✓) MPC(✓) RBR(70% size) LSR(✓)
Assets: 12 Active+, 3 Active, 1 Reduced, 0 Dormant
No-trade events: 4 (2 spread, 1 dead_market, 1 chaotic_spike)
GTF suppressions: 7 (3 regime_throttle, 2 chop, 1 clustering, 1 session_budget)
Loss streak max: 3 (reset)
Time stop regret rate: 22% (target: <35%) ✓
Recovery mode: NO
Detectors: All GREEN
```

---

## 11. Expected Impact on PF, Drawdown, and Stability

### 11.1 PF Impact Analysis

Each hardening mechanism's estimated contribution to PF improvement, based on typical intraday system failure modes:

| Mechanism | Failure Prevented | Est. PF Impact |
|---|---|---|
| Chop Detector (§2.4) | Trading in directionless markets (PF 0.5–0.8 in chop) | +0.08 to +0.15 |
| No-Trade Conditions (§3) | Dead market trades (0.06% stops vs 0.08% fees = guaranteed loss) | +0.03 to +0.05 |
| Execution Adaptation (§4) | 3–5bps excess slippage on 30% of trades | +0.04 to +0.08 |
| Portfolio Coordination (§5) | Correlated drawdowns from 2+ same-direction positions | +0.02 to +0.05 |
| Asset Ranking (§6) | Equal allocation to high-spread/low-vol assets dragging portfolio | +0.05 to +0.10 |
| Time Stop Optimization (§7) | Cutting 35%+ of time-stopped trades that would have won | +0.03 to +0.06 |
| Trade Quality Scoring (§8) | Low-microstructure trades entering portfolio (est. 15% of raw triggers) | +0.06 to +0.12 |
| Learning Loop (§9) | Continued trading of strategy-regime-asset combos with PF < 0.9 | +0.05 to +0.10 |
| Failure Mode Protection (§10) | Multi-day PF degradation without intervention | +0.02 to +0.04 |

**Cumulative estimated PF improvement: +0.38 to +0.75**

Applied to the V1 baseline target of PF 1.30 (Scenario A), the hardened system targets PF 1.50–1.80 under normal conditions. Even under pessimistic realization (50% of estimated impact), PF improves to 1.50–1.68.

The most critical mechanisms are Chop Detection, Trade Quality Scoring, and Asset Ranking — these three alone account for +0.19 to +0.37 of PF improvement.

### 11.2 Drawdown Impact

| Mechanism | Drawdown Reduction Mechanism | Est. Max DD Impact |
|---|---|---|
| Loss Streak Gate (§2.5) | Pauses at 7 losses, stops at 10 | -1.5% to -3.0% reduction |
| Session Budget (§2.7) | Caps daily loss exposure via trade count | -1.0% to -2.0% reduction |
| Recovery Mode (§10.2) | 40% risk, 50% capacity during degradation | -2.0% to -4.0% reduction |
| Directional Exposure (§5.2) | Prevents 6+ same-direction positions | -1.0% to -3.0% reduction |
| Portfolio Coordination (§5.1) | Max 1 position per symbol | -0.5% to -1.5% reduction |

V1's max drawdown target was 15%. With hardening, expected max drawdown drops to 8–12% under stress conditions, with typical drawdown of 3–6%.

### 11.3 Stability Impact

| Risk | V1 Exposure | Hardened Exposure |
|---|---|---|
| Overtrading in chop | Uncontrolled (up to 31 trades/day in any market) | Suppressed to 5–10 trades/day in chop |
| Execution quality degradation | Static — trades execute regardless of fill quality | Adaptive — skip trades on degraded assets |
| Strategy drift | L1/L2 weights adjust slowly (30-trade window) | Strategy × Regime disable at 20 trades PF < 0.90 |
| Single-asset concentration | 2 positions per symbol | 1 position per symbol |
| Correlated exposure | Implicit (CorrelationController in RiskGate) | Explicit directional limits + correlation matrix |
| Time-of-day effects | Not addressed | Strategy × Hour auto-disable |
| Data pipeline failure | WS reconnect + REST fallback | + Active validation (WS vs REST cross-check) |

### 11.4 Trade Count Impact

The hardening mechanisms reduce raw trade count by approximately 25–40%:

| Filter | Est. Trades Suppressed |
|---|---|
| Chop Detector | 3–5/day on chop days (0 on trend days) |
| No-Trade Conditions | 1–3/day (spread, dead market, spikes) |
| GTF Regime Throttle | 2–4/day (chop + ranging suppression) |
| TQS Rejection (< 0.35) | 2–5/day (poor microstructure) |
| Portfolio Coordination | 1–2/day (same-symbol, correlated direction) |
| Execution Score Skip | 0–2/day (asset-specific degradation) |

**Total suppressed:** ~9–21 trades/day.

V1 projected 15–31 raw triggers/day. After hardening filters: **10–22 executed trades/day.**

This is a feature, not a bug. The 9–21 suppressed trades are predominantly the low-quality entries that drag PF toward 1.0. Removing them concentrates capital on high-quality setups. The mathematical relationship:

```
PF_filtered = PF_unfiltered × (1 + filtered_loss_rate / retained_trade_count)
```

If the filtered trades had average PF of 0.8 (they lost money on average), removing them from a portfolio of 25 trades with PF 1.30 yields:

```
Retained: 17 trades, PF 1.50 (the good trades)
Removed: 8 trades, PF 0.80 (the bad trades)
Combined (unfiltered): 25 trades, PF 1.30
Filtered: 17 trades, PF 1.50
```

This demonstrates the core thesis: filtering bad trades improves PF more than adding good trades.

---

## Appendix D: Configuration Additions for Profitability Hardening

```yaml
# Append to intraday config from V1 Appendix B

global_trade_filter:
  enabled: true
  max_trades_per_30m_window: 8          # global across all symbols
  per_symbol_cooldown_minutes: 5
  global_cooldown_seconds: 60
  session_budget:
    max_trades_per_day: 60
    max_trades_per_4h: 20
    max_sl_hits_per_day: 25
    max_capital_churn_pct: 300

  regime_throttle:
    trend_bull: { multiplier: 1.0, max_per_symbol_hour: 3 }
    trend_bear: { multiplier: 1.0, max_per_symbol_hour: 3 }
    ranging:    { multiplier: 0.6, max_per_symbol_hour: 2 }
    volatile:   { multiplier: 0.8, max_per_symbol_hour: 2 }
    chop:       { multiplier: 0.2, max_per_symbol_hour: 1 }

  chop_detection:
    adx_threshold: 18
    ema_cross_count_20bars: 3
    displacement_atr_ratio: 0.5
    alternating_candle_count: 3
    exit_adx_threshold: 22
    exit_displacement_atr: 1.0

  volatility_filter:
    compression_threshold: 0.40         # atr_ratio below this = dead
    compression_recovery: 0.55
    explosion_threshold: 3.0
    explosion_recovery: 2.5

  loss_streak:
    threshold_3: { score_boost: 0.05 }
    threshold_5: { score_boost: 0.10, risk_mult: 0.60, disable_worst_strategy: true }
    threshold_7: { pause_minutes: 30 }
    threshold_10: { pause_minutes: 120, enter_recovery: true }
    reset_after_consecutive_wins: 2

no_trade_conditions:
  dead_market:
    atr_close_ratio_threshold: 0.0015
    volume_percentile_threshold: 20
    recovery_atr_ratio: 0.0025
    recovery_consecutive_bars: 2
  chaotic_spike:
    single_bar_atr_mult: 4.0
    consecutive_bar_atr_mult: 2.5
    consecutive_bar_count: 3
    cooldown_minutes: 5
  spread:
    soft_threshold_bps: 15              # suppress all except LSR
    hard_threshold_bps: 20              # suppress all
    recovery_bps: 12
    recovery_duration_seconds: 60
  tf_conflict:
    suppress_strategies: ["MPC", "MX"]
    allow_strategies: ["VR", "LSR", "RBR"]
  liquidity:
    min_depth_tier1_usd: 30000
    min_depth_tier23_usd: 15000
    max_imbalance_ratio: 3.0
    recovery_polls: 2

execution_engine:
  adaptation_enabled: true
  profile_window: 50                    # fills per asset for rolling stats
  global_window: 1000
  recompute_interval_fills: 10
  limit_fill_rate_thresholds:
    aggressive_pricing: 0.60            # below 80%, use aggressive limit
    market_only: 0.60                   # below 60%, use market orders
  slippage_size_adjustment:
    trigger_p95_bps: 8.0
    max_reduction_factor: 0.50
  execution_skip_threshold: 0.40
  execution_skip_recovery: 0.55

portfolio_coordination:
  max_positions_per_symbol: 1
  max_long_positions: 6
  max_short_positions: 6
  max_same_direction_correlated: 2
  max_same_direction_tier1: 3
  correlation_matrix_update_days: 30
  correlation_threshold: 0.60
  conflict_score_diff_threshold: 0.10
  dedup_mx_rbr_atr_tolerance: 0.5
  dedup_vr_mpc_atr_tolerance: 0.3

asset_ranking:
  enabled: true
  update_interval: "15m"
  weights:
    volatility: 0.30
    trend: 0.25
    volume: 0.20
    spread: 0.15
    performance: 0.10
  tiers:
    active_plus: { min_score: 0.70, bias_threshold: 0.30, max_capital_pct: 0.030 }
    active:      { min_score: 0.45, bias_threshold: 0.35, max_capital_pct: 0.025 }
    reduced:     { min_score: 0.25, bias_threshold: 0.45, max_capital_pct: 0.015 }
    dormant:     { max_score: 0.25, recovery_threshold: 0.35, recovery_bars: 2 }

time_stop:
  adaptive_enabled: true
  modifiers:
    profit_above_0_5R: 1.30
    profit_0_3_to_0_5R: 1.15
    neutral: 1.00
    loss_0_3_to_0_5R: 0.80
    loss_below_0_5R: 0.60
    strong_trend_adx_30: 1.20
    chop_detected: 0.70
  bounds:
    absolute_max_minutes: 120
    absolute_min_minutes: 15
  validation:
    forward_window_minutes: 60
    regret_rate_increase_threshold: 0.35
    regret_rate_decrease_threshold: 0.15
    adjustment_minutes_increase: 10
    adjustment_minutes_decrease: 5
    max_adjustments_per_week: 1

trade_quality_score:
  enabled: true
  weights:
    setup_quality: 0.30
    trigger_quality: 0.25
    microstructure_quality: 0.20
    execution_quality: 0.15
    context_quality: 0.10
  thresholds:
    full_execution: 0.55
    cautious_execution: 0.45           # 70% size
    minimal_execution: 0.35            # 40% size, requires trade_score > 0.50
    rejection: 0.35                    # below this = reject

learning_loop:
  matrices:
    strategy_regime: { window: 50, disable_pf_threshold: 0.90, min_trades: 20 }
    strategy_asset:  { window: 50, disable_pf_threshold: 0.85, min_trades: 20 }
    strategy_hour:   { window: 30, disable_pf_threshold: 0.80, min_trades: 15 }
  boosting:
    min_trades: 30
    min_pf: 1.80
    min_wr: 0.55
    threshold_reduction: 0.03
    risk_boost_mult: 1.30
    max_risk_pct: 0.35
    min_threshold: 0.28
  decay_rate_per_day: 0.03
  health_check_interval_trades: 25
  daily_health:
    full_active_threshold: 0.60
    reduced_threshold: 0.40
    tqs_only_threshold: 0.20

failure_protection:
  pf_drift:
    alert_threshold: 1.10
    critical_threshold: 0.95
    window_trades: 50
    sample_interval_trades: 10
  loss_clustering:
    max_losses_in_20: 14
    pause_hours: 2
  execution_anomaly:
    slippage_alert_bps: 8.0
    consecutive_fills: 5
    slippage_critical_bps: 12.0
  data_inconsistency:
    check_interval_minutes: 5
    max_divergence_pct: 0.50
    shutdown_consecutive_checks: 3
  latency_drift:
    alert_p95_ms: 5000
    market_only_p95_ms: 10000
    pause_p95_ms: 15000
    window_trades: 20

  recovery_mode:
    risk_pct: 0.10
    max_concurrent: 4
    trade_score_threshold: 0.55
    tqs_threshold: 0.60
    active_strategies: ["VR", "MPC", "RBR"]
    session_budget: 20
    exit_conditions:
      pf_threshold: 1.20
      wr_threshold: 0.48
      min_trades: 30
      max_duration_hours: 72
```

---

*End of Addendum*
