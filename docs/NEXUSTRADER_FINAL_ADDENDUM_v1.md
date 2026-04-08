# NexusTrader Intraday Redesign — Final Addendum: Edge Validity, Capital Concentration & Signal Expiry

**Version:** 1.0
**Date:** 2026-04-06
**Parent Documents:** NEXUSTRADER_INTRADAY_REDESIGN_v1.md, NEXUSTRADER_PROFITABILITY_HARDENING_ADDENDUM_v1.md
**Status:** Final Addendum — Pending Audit
**Classification:** Internal Engineering

---

## 1. Edge Validity Monitor (Market Structure Detection)

### 1.1 Problem Statement

The Hardening Addendum's Learning Loop (§9) operates at the individual strategy level — it detects when MX underperforms in `ranging` regime, or when VR underperforms on DOGE. What it cannot detect is a market-wide structural shift that degrades an entire *class* of strategies simultaneously.

Example: crypto enters a prolonged low-volatility, high-correlation regime (as occurred in Q3 2023 and Q1 2025). In this environment, breakout strategies (MX + RBR) fail systematically — not because of a single asset or regime, but because the market microstructure no longer supports breakout continuation. The Learning Loop would eventually disable MX-in-ranging and RBR-in-ranging cell by cell, but it takes 20+ trades per cell × 5 regimes × 16 assets = hundreds of trades and weeks of losses before it fully adapts. The Edge Validity Monitor must detect this class-level degradation within 50 trades and respond within hours.

### 1.2 Strategy Class Definitions

The five strategies are grouped into three structural classes based on the market condition they exploit:

| Class | Strategies | Market Condition Exploited | Shared Failure Mode |
|---|---|---|---|
| **Breakout** | MX (Momentum Expansion), RBR (Range Breakout Reclaim) | Volatility expansion, directional conviction | Fails when breakouts are consistently rejected — price breaks range/consolidation but immediately reverses. Symptom: high trade count, low WR, stops hit rapidly. |
| **Pullback** | MPC (Micro Pullback Continuation) | Trend continuation after retracement | Fails when trends become unreliable — pullbacks don't resume, but instead reverse into new trends. Symptom: EMA support breaks repeatedly, time stops fire instead of TPs. |
| **Mean-Reversion** | VR (VWAP Reclaim/Rejection), LSR (Liquidity Sweep Reclaim) | Price returning to fair value after deviation | Fails when price trends persistently away from anchors — VWAP becomes irrelevant in strong trends, sweep reclaims fail because the sweep continues. Symptom: VWAP/level reclaims immediately re-break, stops hit. |

### 1.3 Class-Level Performance Tracking

Each strategy class maintains an aggregated performance window that pools trades from its member strategies:

```
class_tracker[class_name] = {
    trades: deque(maxlen=75),        # rolling window of last 75 trades across all member strategies
    pf: float,                        # PF of the window
    wr: float,                        # win rate of the window
    avg_r: float,                     # average R-multiple
    last_recalc: timestamp,
    consecutive_loss_bars: int,       # count of consecutive 5m bars where class PF < 1.0
    status: "ACTIVE" | "DEGRADED" | "SUSPENDED"
}
```

**Update trigger:** After every closed trade, if the trade belongs to a class member strategy, recalculate the class tracker. This means the Breakout tracker updates after every MX or RBR close, pooling their results.

**Why 75 trades (not 50):** The class window is larger than the per-strategy Learning Loop window (50) because class-level decisions have higher impact — suspending an entire class removes 2 strategies simultaneously. The larger window reduces the probability of false suspension due to short-term noise. At 15–22 trades/day with ~40% being breakout class, the Breakout tracker accumulates 75 trades in approximately 8–13 days (8 days at 22 trades/day, 13 days at 15 trades/day).

### 1.4 Degradation Detection Logic

**Tier 1 — DEGRADED (early warning):**

A class enters DEGRADED state when ANY of:

```
condition_a = class_pf < 1.05 AND class_trades >= 30
condition_b = class_wr < 0.40 AND class_trades >= 30
condition_c = class_avg_r < 0.0 AND class_trades >= 20 (negative expectancy)
```

`DEGRADED = condition_a OR condition_b OR condition_c`

**DEGRADED behavior:**
- All strategies in the class have their `trade_score_threshold` raised by +0.08 (from 0.40 to 0.48, or from current level if already elevated by loss streak gate)
- `risk_pct_per_trade` for all strategies in the class is reduced to 70% of standard (0.25% → 0.175%)
- Log: `EDGE_MONITOR: class={} status=DEGRADED pf={:.2f} wr={:.1f}% avg_r={:.3f} n={}`

**Tier 2 — SUSPENDED (edge lost):**

A class enters SUSPENDED state when:

```
suspended = class_pf < 0.90 AND class_trades >= 50
```

**SUSPENDED behavior:**
- ALL strategies in the class are globally disabled. No new setups are generated (Stage A skips these strategies entirely). Existing open positions from these strategies are managed to completion — SL/TP/time stop remain active.
- Log: `EDGE_MONITOR: class={} status=SUSPENDED pf={:.2f} wr={:.1f}% n={} — all member strategies disabled`
- Notification sent via NotificationManager.

### 1.5 Recovery Logic

**DEGRADED → ACTIVE:**

The class returns to ACTIVE when:
```
class_pf >= 1.15 AND class_wr >= 0.45 (using only trades that occurred AFTER entering DEGRADED)
AND post_degraded_trades >= 15
```

The post-degraded trades are collected at the reduced risk level (0.175%). The system must prove the edge has returned at reduced size before restoring full allocation.

**SUSPENDED → DEGRADED (probe mode):**

A suspended class does NOT directly return to ACTIVE. Instead, it enters a structured probe:

1. **Cooldown:** Minimum 72 hours after suspension. No probe trades during this period.
2. **Probe activation:** After 72h, re-enable ONE strategy from the class (the one with the highest `strategy_health` score from Hardening §9.5) at 40% of standard risk (0.10% per trade).
3. **Probe window:** 15 trades with the probe strategy.
4. **Probe pass:** If probe PF ≥ 1.10 AND WR ≥ 43%, transition to DEGRADED (which then requires meeting DEGRADED → ACTIVE criteria to restore full activity).
5. **Probe fail:** If probe PF < 1.10 OR WR < 43%, re-suspend for another 72 hours. Second probe uses the SAME strategy. After 3 consecutive probe failures (9+ days suspended), the class remains suspended until manual review or until 30 calendar days pass, at which point one final probe is attempted.

**SUSPENDED → manual override:** The system operator can force-reactivate a suspended class via config:
```yaml
edge_monitor:
  overrides:
    breakout: "force_active"    # override suspension
```
This resets the class to ACTIVE with full parameters. The override is a one-time flag — it does not prevent future suspension if performance degrades again.

### 1.6 Cross-Class Interaction Rules

**Rule 1 — Maximum one class suspended at any time.**

If the Breakout class is already SUSPENDED and the Pullback class triggers SUSPENDED, the Pullback class is held at DEGRADED (not SUSPENDED). Rationale: suspending 2 of 3 classes leaves only Mean-Reversion active (VR + LSR), which generates 4–8 trades/day — below the minimum activity level needed for the system to function. With only 1 class suspended, the remaining 2 classes provide 8–16 trades/day.

If a second class triggers SUSPENDED while the first is already suspended, escalate to system-level alert:
```
CRITICAL: Two strategy classes degraded simultaneously.
Class A: {breakout} SUSPENDED (PF {0.87})
Class B: {pullback} DEGRADED (PF {1.02})
Action: System entering conservation mode. Manual review recommended.
```

Conservation mode = Hardening §10.2 recovery mode parameters.

**Rule 2 — DEGRADED classes compete for allocation.**

If Breakout is DEGRADED and Pullback is ACTIVE, capital naturally flows toward Pullback strategies (MPC) because Breakout strategies (MX, RBR) have elevated thresholds and reduced risk. The Capital Concentration Engine (§2 below) handles this automatically — no special cross-class logic needed beyond the threshold/risk adjustments.

### 1.7 Integration Points

| Component | Interface | Direction |
|---|---|---|
| Learning Loop (Hardening §9) | Edge Validity Monitor reads Matrix A (Strategy × Regime) to validate class-level degradation is not a single-regime anomaly. If degradation is isolated to one regime, the Learning Loop handles it — Edge Monitor does NOT trigger. | Read-only |
| Global Trade Filter (Hardening §2) | When a class is DEGRADED, Edge Monitor sets an elevated `trade_score_threshold` for member strategies, which GTF enforces on every trigger. | Write (threshold override) |
| Strategy Health (Hardening §9.5) | Edge Monitor reads `strategy_health` to select probe strategy during SUSPENDED → DEGRADED recovery. | Read-only |
| PositionTracker | Edge Monitor subscribes to `TRADE_CLOSED` events and updates class trackers on every close. | Event subscription |
| Config (edge_monitor section) | Manual override, probe parameters, cooldown durations. | Read |

### 1.8 Regime-Isolation Check

Before triggering DEGRADED or SUSPENDED, the Edge Validity Monitor performs a regime-isolation check to avoid duplicating the Learning Loop's work:

```
For each regime R in [trend_bull, trend_bear, ranging, volatile, chop]:
    class_pf_in_R = PF of class trades where regime == R (last 75 trades)

isolation_detected = (
    count(regimes where class_pf_in_R < 0.90) == 1 AND
    count(regimes where class_pf_in_R >= 1.20) >= 1
)
```

If `isolation_detected` is True, the degradation is regime-specific (e.g., breakouts fail only in `ranging`). The Learning Loop (Hardening §9.2) handles this. Edge Monitor takes no action.

If `isolation_detected` is False (degradation spans 2+ regimes, or no regime has PF ≥ 1.20), this is a structural edge loss. Edge Monitor proceeds with DEGRADED/SUSPENDED logic.

---

## 2. Capital Concentration Engine

### 2.1 Problem Statement

The existing system treats all approved trades with equal capital allocation (subject to asset tier caps from Hardening §6.2: Active+ 3.0%, Active 2.5%, Reduced 1.5%). A TQS-0.80 trade on a high-performing strategy on the day's best asset receives the same 3.0% allocation as a TQS-0.56 trade on a middling strategy on an average asset.

This is capital-inefficient. The system should concentrate capital on its highest-conviction opportunities and allocate less to borderline trades. The Hardening Addendum's TQS (§8.3) already size-scales at the low end (70% at 0.45–0.54, 40% at 0.35–0.44), but does not scale UP for exceptional trades.

### 2.2 Architecture

The Capital Concentration Engine (CCE) sits between TQS scoring and the PositionSizer. It takes the TQS-approved trade and computes a `capital_weight` (0.40–1.50) that multiplies the PositionSizer's computed `risk_pct_per_trade`.

```
Stage B Trigger
    → GTF
    → RiskGate
    → TQS (approve/reject + size tier)
    → Capital Concentration Engine (compute capital_weight)
    → PositionSizer (risk_pct × capital_weight → final size)
    → Signal Expiry Check (§3)
    → ExecutionManager
```

### 2.3 Capital Weight Formula

```
capital_weight = base_weight × class_health_modifier × conviction_modifier

Where:
  base_weight     = f(TQS, asset_score, execution_score)
  class_health    = f(edge_monitor_status)
  conviction      = f(strategy_health, recent_pf_on_asset)
```

### 2.4 Base Weight Calculation

The base weight combines three scores that are already computed by upstream components:

```
raw_base = (
    0.45 × tqs_component +
    0.30 × asset_component +
    0.25 × execution_component
)

base_weight = scale(raw_base, input_range=[0.25, 0.85], output_range=[0.50, 1.30])
```

**Component mappings:**

```
tqs_component = TQS value directly (0.35–1.0 range since sub-0.35 is already rejected)

asset_component = asset_score from Hardening §6.1 (0.25–1.0 range since sub-0.25 is Dormant)

execution_component = execution_score from Hardening §4.3 (0.40–1.0 range since sub-0.40 is skipped)
```

**Scale function:** Linear mapping from `[input_min, input_max]` to `[output_min, output_max]`, clamped at bounds.

```python
def scale(value, input_range, output_range):
    in_min, in_max = input_range
    out_min, out_max = output_range
    normalized = (value - in_min) / (in_max - in_min)
    return max(out_min, min(out_max, out_min + normalized * (out_max - out_min)))
```

**Example calculations:**

| Scenario | TQS | Asset Score | Exec Score | raw_base | base_weight |
|---|---|---|---|---|---|
| Best case | 0.82 | 0.88 | 0.92 | 0.863 | 1.30 |
| Strong | 0.68 | 0.72 | 0.80 | 0.722 | 1.16 |
| Average | 0.55 | 0.55 | 0.65 | 0.575 | 1.01 |
| Borderline | 0.42 | 0.35 | 0.48 | 0.414 | 0.77 |
| Minimum (TQS 0.35, Reduced asset, exec skip boundary) | 0.36 | 0.26 | 0.42 | 0.345 | 0.58 |

### 2.5 Class Health Modifier

The Edge Validity Monitor's class status (§1.4) modifies capital allocation for all trades in that class:

| Class Status | Modifier |
|---|---|
| ACTIVE | 1.00 (no change) |
| DEGRADED | 0.70 |
| SUSPENDED | 0.00 (no trades — class is disabled) |
| Probe (during SUSPENDED recovery) | 0.40 |

This modifier stacks with the base weight. A DEGRADED class with a strong individual trade (base_weight 1.20) receives: 1.20 × 0.70 = 0.84 — effectively a below-average allocation despite the individual signal being strong. This is intentional: when the class's edge is in question, even strong-looking signals from that class should receive conservative sizing.

### 2.6 Conviction Modifier

The conviction modifier rewards trades where strategy-level and asset-level recent performance are both strong:

```
strategy_pf = rolling 50-trade PF for this specific strategy (from Hardening §9.5 strategy_health)
asset_pf = rolling 20-trade PF for this specific asset (from Hardening §6.1 recent_performance_score)

strategy_momentum = clip((strategy_pf - 1.0) / (2.0 - 1.0), -0.15, 0.15)
asset_momentum = clip((asset_pf - 1.0) / (2.0 - 1.0), -0.10, 0.10)

conviction_modifier = 1.0 + strategy_momentum + asset_momentum
```

**Range:** conviction_modifier ∈ [0.75, 1.25]

- Strategy PF = 2.0 AND asset PF = 2.0 → conviction = 1.0 + 0.15 + 0.10 = 1.25 (max boost)
- Strategy PF = 1.5 AND asset PF = 1.3 → conviction = 1.0 + 0.075 + 0.03 = 1.105
- Strategy PF = 1.0 AND asset PF = 1.0 → conviction = 1.0 (neutral)
- Strategy PF = 0.7 AND asset PF = 0.8 → conviction = 1.0 + (-0.15) + (-0.10) = 0.75 (max reduction)

If either strategy or asset has fewer than 10 closed trades, the corresponding momentum defaults to 0.0 (no contribution, not negative).

### 2.7 Final Capital Weight and Sizing

```
capital_weight = base_weight × class_health_modifier × conviction_modifier

Clamped to [0.40, 1.50]
```

**Applied to PositionSizer:**

```
effective_risk_pct = risk_pct_per_trade × capital_weight

Where risk_pct_per_trade = 0.25% (standard) or current adjusted value (loss streak, recovery mode, etc.)
```

**Hard safety bounds on effective_risk_pct:**
- Minimum: 0.05% (below this, position is too small to be meaningful after fees)
- Maximum: 0.40% (absolute ceiling regardless of conviction — prevents any single trade from risking > 0.40% of capital)

**Example sizing scenarios:**

| Scenario | risk_pct | capital_weight | effective_risk | Interpretation |
|---|---|---|---|---|
| High conviction, strong market | 0.25% | 1.45 | 0.3625% → capped at 0.40% | Near-max allocation on best opportunity |
| Standard trade | 0.25% | 1.00 | 0.25% | Default allocation |
| Borderline trade, weak class | 0.25% | 0.55 | 0.1375% | Half-size on low-quality opportunity |
| Recovery mode, degraded class | 0.10% | 0.70 | 0.07% | Minimal risk during system stress |
| Loss streak (threshold 5), weak asset | 0.15% | 0.60 | 0.09% | System protecting capital |

### 2.8 Capital Budget Enforcement

The CCE must not violate aggregate risk limits. After computing `effective_risk_pct`, verify:

```
total_deployed_risk = sum(effective_risk_pct for all open positions) + effective_risk_pct_new_trade
max_total_risk = 4.0% of capital  (8 positions × 0.50% theoretical max each)
```

If `total_deployed_risk > max_total_risk`, reduce the new trade's `effective_risk_pct` to fit within the remaining budget. If the remaining budget < 0.05%, skip the trade entirely.

This prevents the CCE's concentration boosting from accidentally over-deploying capital when multiple high-conviction trades fire in sequence.

### 2.9 Integration Points

| Component | Interface | Direction |
|---|---|---|
| TQS (Hardening §8) | CCE reads TQS value after TQS approval. TQS size-tier (70%/40%) is REPLACED by CCE's capital_weight — CCE is the single source of size scaling. | Read TQS; replaces TQS size scaling |
| Asset Ranking (Hardening §6) | CCE reads `asset_score` for base_weight calculation. Asset tier caps (`max_capital_pct`) remain as hard ceilings. | Read |
| Execution Adaptation (Hardening §4) | CCE reads `execution_score` for base_weight calculation. | Read |
| Edge Validity Monitor (§1) | CCE reads class status for class_health_modifier. | Read |
| Learning Loop (Hardening §9) | CCE reads `strategy_health` and per-asset PF for conviction_modifier. | Read |
| PositionSizer (V1 §5.2 via §10.1) | CCE outputs `effective_risk_pct` which replaces the standard `risk_pct_per_trade` input to PositionSizer. All other PositionSizer logic (stop distance, cap, floor) remains unchanged. | Write (risk_pct override) |
| RiskGate (V1 §4) | Portfolio heat check uses the CCE-adjusted position size. No RiskGate changes needed — it already validates the final size. | Indirect (via PositionSizer output) |

**Deprecation note:** The TQS size-tier system (Hardening §8.3: "70% at 0.45–0.54", "40% at 0.35–0.44") is superseded by the CCE. The CCE's `capital_weight` at TQS 0.45 naturally produces ~0.77 base_weight (depending on asset and execution scores), which translates to a similar 60–80% size reduction. The CCE provides finer granularity and accounts for more factors. TQS thresholds for accept/reject (≥0.35 to execute, <0.35 to reject) remain unchanged.

---

## 3. Signal Expiry System (Latency Protection)

### 3.1 Problem Statement

The V1 redesign targets signal-to-order latency of 120–330ms (V1 §7.3). The Hardening Addendum adds execution adaptation that can switch to market orders when limit fill rates degrade (Hardening §4.4). However, neither document addresses the scenario where system load, VPN disruption, or exchange API delays cause a signal to become stale between generation and execution.

A 1m candle closes at T=0. The system detects a MX breakout trigger. Under normal conditions, the order is submitted at T+300ms (within the V1 §7.3 latency budget of 120–330ms). But if the WS connection hiccups, or Stage B evaluation runs long due to 12 active setups, or the REST order endpoint has a 3-second queue, the order might not submit until T+8 seconds. The max signal age thresholds (6–12s per §3.3) are intentionally set 18–100× above the normal latency budget to handle these worst-case VPN/API delay scenarios — they are safety limits, not expected operating ranges. In 8 seconds on a volatile 1m candle, price can move 0.1–0.3% — potentially past the intended entry level, past the stop loss, or past the target. Executing a stale signal at the wrong price is worse than not executing at all.

### 3.2 Signal Lifecycle Model

Every signal generated by Stage B carries a lifecycle timestamp chain:

```python
@dataclass
class SignalLifecycle:
    candle_close_ts: float          # T0: when the triggering candle closed
    trigger_eval_ts: float          # T1: when Stage B evaluated the trigger
    gtf_pass_ts: float              # T2: when GTF approved (or None if rejected)
    riskgate_pass_ts: float         # T3: when RiskGate approved
    tqs_pass_ts: float              # T4: when TQS scored + CCE computed weight
    expiry_check_ts: float          # T5: Signal Expiry validation (this system)
    order_submit_ts: float          # T6: when order was submitted to exchange
    fill_ts: float                  # T7: when fill confirmation received

    @property
    def signal_age_ms(self) -> int:
        """Time from candle close to expiry check."""
        return int((self.expiry_check_ts - self.candle_close_ts) * 1000)

    @property
    def pipeline_latency_ms(self) -> int:
        """Time from trigger eval to expiry check (processing time)."""
        return int((self.expiry_check_ts - self.trigger_eval_ts) * 1000)
```

### 3.3 Per-Strategy Maximum Signal Age

Each strategy has a different tolerance for signal staleness based on the speed of the market condition it exploits:

| Strategy | Max Signal Age | Rationale |
|---|---|---|
| **MX** (Momentum Expansion) | **8 seconds** | Breakouts move fast. After 8s on a 1m candle, the breakout has either extended significantly (chasing) or failed (false break). Either way, the original entry level is no longer valid. |
| **VR** (VWAP Reclaim/Rejection) | **12 seconds** | VWAP interactions are slower — VWAP is a gradual magnet, not a spike event. Price near VWAP tends to consolidate. 12s of aging has less impact on entry quality. |
| **MPC** (Micro Pullback Continuation) | **10 seconds** | Pullback reversals at EMA are moderately time-sensitive. The reversal candle pattern is confirmed at the 3m/1m close, and the continuation move begins quickly. |
| **RBR** (Range Breakout Reclaim) | **10 seconds** | Retested breakouts re-accelerate. The retest-and-go pattern produces a relatively fast move after confirmation. |
| **LSR** (Liquidity Sweep Reclaim) | **6 seconds** | Sweep reclaims are the fastest-expiring pattern. The reclaim candle itself (wick + close) represents a momentary order flow imbalance. After 6 seconds, the imbalance may have dissipated — other participants have already reacted. |

### 3.4 Expiry Check Logic

The Signal Expiry System executes as the LAST check before order submission — after TQS, after CCE, immediately before the `OrderRouter.submit()` call:

```python
def check_signal_expiry(signal: SignalLifecycle, strategy: str) -> tuple[bool, str]:
    """
    Returns (valid: bool, reason: str).
    Called at T5 (expiry_check_ts is set to current time on entry).
    """
    signal.expiry_check_ts = time.time()

    max_age_ms = MAX_SIGNAL_AGE[strategy]  # from §3.3 table
    signal_age = signal.signal_age_ms

    # Check 1: absolute signal age
    if signal_age > max_age_ms:
        return (False, f"SIGNAL_EXPIRED: age={signal_age}ms > max={max_age_ms}ms strategy={strategy}")

    # Check 2: price drift since candle close
    current_price = latest_tick_price[signal.symbol]
    entry_price = signal.intended_entry_price
    drift_pct = abs(current_price - entry_price) / entry_price * 100
    max_drift_pct = MAX_PRICE_DRIFT[strategy]  # see §3.5

    if drift_pct > max_drift_pct:
        return (False, f"PRICE_DRIFT: drift={drift_pct:.3f}% > max={max_drift_pct}% strategy={strategy}")

    # Check 3: stop loss still valid
    if signal.direction == "long" and current_price <= signal.stop_loss:
        return (False, f"SL_ALREADY_HIT: price={current_price} <= sl={signal.stop_loss}")
    if signal.direction == "short" and current_price >= signal.stop_loss:
        return (False, f"SL_ALREADY_HIT: price={current_price} >= sl={signal.stop_loss}")

    return (True, "VALID")
```

### 3.5 Price Drift Thresholds

Even if the signal is within its age limit, the price may have moved significantly since the trigger candle closed. The price drift check prevents executing at a materially different price than intended:

| Strategy | Max Price Drift (% from intended entry) | Rationale |
|---|---|---|
| **MX** | 0.12% | Breakout entries are level-sensitive. 0.12% drift on a typical 0.8% stop = 15% of risk consumed by drift. Beyond this, risk:reward is materially degraded. |
| **VR** | 0.08% | VWAP entries have the tightest stops (0.8× ATR ≈ 0.4–0.6% of price). 0.08% drift = 13–20% of risk. |
| **MPC** | 0.10% | Pullback entries at EMA support — moderate sensitivity. |
| **RBR** | 0.10% | Breakout retest entries — moderate sensitivity. |
| **LSR** | 0.15% | Sweep reclaims often have wider stops (below the sweep wick). 0.15% drift is tolerable against a ~1.0% stop. |

### 3.6 Adjusted Entry on Minor Drift

If the price has drifted but is within the max threshold, the system adjusts the entry price to the current market price and recalculates stop loss distance and target accordingly:

```python
if valid and drift_pct > 0.0:
    # Adjust entry to current price
    adjusted_entry = current_price

    # Recalculate stop distance (stop level stays fixed)
    if signal.direction == "long":
        new_stop_distance = adjusted_entry - signal.stop_loss
    else:
        new_stop_distance = signal.stop_loss - adjusted_entry

    # Verify R:R hasn't degraded below 1.0
    if signal.direction == "long":
        new_target_distance = signal.take_profit - adjusted_entry
    else:
        new_target_distance = adjusted_entry - signal.take_profit

    new_rr = new_target_distance / new_stop_distance if new_stop_distance > 0 else 0

    if new_rr < 1.0:
        return (False, f"RR_DEGRADED: rr={new_rr:.2f} < 1.0 after price drift adjustment")

    signal.adjusted_entry_price = adjusted_entry
    signal.adjusted_rr = new_rr
```

**Key principle:** The stop loss level and take profit level are FIXED at the values computed during trigger evaluation (they are based on structural levels — ATR distances, range boundaries, VWAP, etc.). Only the entry price adjusts. This means favorable drift (price pulled back toward the entry direction) improves R:R, while adverse drift (price moved in the trade direction) degrades R:R. If R:R drops below 1.0 due to drift, the trade is rejected.

### 3.7 Stale Signal Queue Handling

The Global Trade Filter's clustering prevention (Hardening §2.6) queues triggers during the 60-second global cooldown. Queued signals age while waiting. When a queued signal is dequeued for execution, it must pass the Signal Expiry check at that moment, not at queue insertion time.

**Queue-specific rule:** Queued signals with `signal_age > max_signal_age × 0.75` at the moment of dequeue are discarded without evaluation. This prevents a "stale queue flush" where multiple old signals execute in rapid succession after a cooldown period.

```
On dequeue(signal):
    if signal.signal_age_ms > MAX_SIGNAL_AGE[signal.strategy] * 0.75:
        discard(signal, reason="QUEUE_AGED_OUT")
        return
    # Proceed to full expiry check
    valid, reason = check_signal_expiry(signal, signal.strategy)
    ...
```

### 3.8 Latency Tracking Integration

The Signal Lifecycle timestamps feed directly into the Execution Adaptation Engine (Hardening §4):

- `signal_age_ms` at order submission is recorded in every `ExecutionRecord`
- Rolling statistics added to `ExecutionProfile`:
  ```
  avg_signal_age_ms: float       # mean age at order submission (last 50 trades)
  expiry_rejection_rate: float   # % of signals rejected by expiry check (last 100 signals)
  queue_aged_out_rate: float     # % of queued signals that expired while waiting
  ```

**Adaptive response to rising signal age:**

| `avg_signal_age_ms` (50-trade rolling) | Action |
|---|---|
| < 3000 | Normal operation. |
| 3000–5000 | Log warning. Investigate WS latency and pipeline bottlenecks. |
| 5000–8000 | Reduce `max_concurrent_setups_evaluated` from 16 to 10 (Stage B evaluates fewer assets per 1m cycle to reduce processing time). Increase priority of signal processing thread. |
| > 8000 | Critical. Pipeline is too slow for intraday trading. Disable LSR and MX (the two most latency-sensitive strategies). Alert via notification. |

**Adaptive response to high expiry rejection rate:**

| `expiry_rejection_rate` (100-signal rolling) | Action |
|---|---|
| < 5% | Normal. A small number of expirations is expected during VPN hiccups. |
| 5–15% | Elevated. Increase all `max_signal_age` by 2 seconds (temporary, revert after 1 hour of < 5% rate). This gives the pipeline more headroom at the cost of slightly staler entries. |
| > 15% | Systemic latency problem. The WS pipeline or evaluation engine is consistently too slow. Enter conservation mode (Hardening §10.2) until rate drops below 10%. |

### 3.9 Integration Points

| Component | Interface | Direction |
|---|---|---|
| Stage B (V1 §6.3) | Stage B stamps `candle_close_ts` and `trigger_eval_ts` on the SignalLifecycle when generating a trigger. | Write (lifecycle init) |
| GTF (Hardening §2) | GTF stamps `gtf_pass_ts` on passage. Clustering queue stores signals with lifecycle intact. | Write (timestamp) |
| RiskGate (V1 §4) | RiskGate stamps `riskgate_pass_ts`. | Write (timestamp) |
| TQS + CCE (Hardening §8 + §2 above) | TQS stamps `tqs_pass_ts`. | Write (timestamp) |
| Signal Expiry System | Reads lifecycle, current price, strategy config. Stamps `expiry_check_ts`. Returns valid/invalid. | Read + Write |
| OrderRouter (V1 §9.4) | Only receives signals that pass expiry check. Stamps `order_submit_ts` and `fill_ts`. | Write (timestamps) |
| Execution Adaptation (Hardening §4) | Signal Expiry feeds `signal_age_ms` and `expiry_rejection_rate` into execution profile for adaptive behavior. | Write (profile data) |
| Latency Measurement (V1 §12.5) | Full lifecycle chain provides exact per-stage timing for latency reports. | Read (reporting) |

---

## 4. Expected Impact on PF and Execution Quality

### 4.1 Edge Validity Monitor — PF Impact

**The core value proposition:** The Learning Loop (Hardening §9) takes ~100–400 trades (2–4 weeks) to fully adapt to a market structure shift because it operates at the (strategy, regime, asset) cell level. The Edge Validity Monitor detects the same shift at the class level within 50–75 trades (5–8 days) and responds with class-wide suspension.

**Estimated PF protection:**

During a market structure shift (e.g., breakout edge disappears for 3 weeks):
- Without Edge Monitor: the system continues trading breakouts at full size for 2+ weeks before the Learning Loop accumulates enough per-cell data. Estimated cost: 50–80 losing breakout trades at 0.25% risk = 12.5–20.0% capital lost.
- With Edge Monitor: breakout class is DEGRADED within 30 trades (4–5 days) at 70% risk, then SUSPENDED within 50 trades (7–8 days) at 0% risk. Estimated cost: 30 trades at blended 85% risk + 20 trades at 70% risk = ~8.5% risk deployed vs. ~20% without. Net saving: ~7–11% of capital during a structural shift event.

**Annualized PF impact:** Assuming 2–3 structural shift events per year (each lasting 2–4 weeks), the Edge Monitor saves 14–33% of capital that would otherwise be lost. Across a full year with 4,000+ trades, this translates to PF improvement of +0.05 to +0.12.

### 4.2 Capital Concentration Engine — PF Impact

**The core value proposition:** Moving from flat allocation (0.25% on every trade) to conviction-weighted allocation (0.10–0.40% per trade) concentrates realized P&L in the highest-quality trades.

**Mathematical model:**

Assume 20 trades/day. Without CCE, all at 0.25% risk. With CCE:
- Top 5 trades (capital_weight ~1.35): risk = 0.34% → captures 5 × 1.35 = 6.75 weighted-R
- Middle 10 trades (capital_weight ~1.00): risk = 0.25% → captures 10 × 1.00 = 10.00 weighted-R
- Bottom 5 trades (capital_weight ~0.65): risk = 0.16% → captures 5 × 0.65 = 3.25 weighted-R

If the top 5 trades have PF 1.8, middle 10 have PF 1.3, and bottom 5 have PF 0.9:

Without CCE: total P&L = 20 × 0.25% × weighted-average-PF-contribution
With CCE: overweight the PF-1.8 group, underweight the PF-0.9 group.

Net PF improvement: the dollar-weighted PF shifts upward because more capital is deployed in high-PF trades. Estimated impact: +0.08 to +0.15.

**Additional stability impact:** By reducing size on borderline trades (capital_weight < 0.70), the CCE reduces variance. Maximum single-trade loss drops from 0.25% to 0.10% for weak trades, while maximum single-trade gain on strong trades increases to 0.40%. This improves the Sharpe ratio even if PF is unchanged.

### 4.3 Signal Expiry System — PF Impact

**The core value proposition:** Preventing stale signal execution eliminates the worst fills — trades that enter at materially different prices than intended.

**Quantitative model:**

Assume 5% of trades currently experience > 5s signal age (VPN hiccup, API delay, processing backlog). Of those, assume 60% result in adverse price drift that degrades R:R below the intended level.

- 20 trades/day × 5% = 1 stale trade/day
- 60% have degraded R:R → 0.6 trades/day with worse outcome
- Average R:R degradation: from 2.0R target to 1.3R effective (drift consumed 0.35R of target)
- Annual impact: 0.6 × 250 days = 150 trades with degraded R:R
- P&L drag: 150 × 0.35R × avg_risk = 52.5R-units of lost edge

At 5,000 annual trades with average risk 0.25%: each R-unit = 0.25% of capital. 52.5R = 13.1% of capital lost to stale execution.

Signal Expiry prevents ~80% of this (some drift is under the threshold and acceptable):
Net saving: ~10.5% of capital annually → PF improvement of +0.03 to +0.06.

**Beyond PF — execution quality improvement:**

The lifecycle timestamp chain provides the first complete instrumentation of the signal pipeline. This enables:
- Identification of bottleneck stages (which stage adds the most latency?)
- VPN performance monitoring (is Singapore VPN degrading over time?)
- Strategy-specific latency profiles (does LSR consistently have higher signal age because the sweep detection computation is expensive?)

These diagnostics enable targeted optimization that compounds over time.

### 4.4 Combined Impact Summary

| System | PF Impact | Drawdown Impact | Execution Quality |
|---|---|---|---|
| Edge Validity Monitor | +0.05 to +0.12 | -2% to -5% during structural shifts | No direct impact |
| Capital Concentration Engine | +0.08 to +0.15 | Reduced variance (smaller losses on weak trades) | No direct impact |
| Signal Expiry System | +0.03 to +0.06 | Prevents drift-induced excess losses | Eliminates stale fills, provides full pipeline instrumentation |
| **Combined** | **+0.16 to +0.33** | **Meaningful reduction in tail risk** | **Complete signal-to-fill traceability** |

**System-wide PF projection (cumulative across all three documents):**

| Layer | PF Contribution |
|---|---|
| V1 Baseline (5 strategies, fees, realistic execution) | 1.30 (target) |
| Hardening Addendum (9 mechanisms) | +0.38 to +0.75 |
| Final Addendum (3 systems) | +0.16 to +0.33 |
| **Total projected PF range** | **1.84 to 2.38** |

**Realistic expectation:** Applying a 50% realization factor (not all estimated improvements will materialize fully in live trading due to model assumptions, regime unpredictability, and implementation imperfections):

- Total improvement range: (0.38 + 0.16) to (0.75 + 0.33) = 0.54 to 1.08
- At 50% realization: 1.30 + 0.27 to 1.30 + 0.54

**Expected live PF: 1.57 to 1.84**

This exceeds the original 1.30 target by 21–42% and provides sufficient margin to remain above 1.30 even in adverse periods where only 30% of estimated improvements materialize (PF floor: 1.30 + 0.54 × 0.30 = 1.46).

---

## Appendix E: Configuration Additions for Final Addendum

```yaml
# Append to config from Hardening Addendum Appendix D

edge_validity_monitor:
  enabled: true
  classes:
    breakout:
      strategies: ["MX", "RBR"]
      window_size: 75
    pullback:
      strategies: ["MPC"]
      window_size: 75
    mean_reversion:
      strategies: ["VR", "LSR"]
      window_size: 75

  degraded:
    pf_threshold: 1.05              # PF below this triggers DEGRADED
    wr_threshold: 0.40              # WR below this triggers DEGRADED
    neg_expectancy_threshold: 0.0   # avg_R below this triggers DEGRADED
    min_trades_pf_wr: 30            # minimum trades for PF/WR check
    min_trades_expectancy: 20       # minimum trades for expectancy check
    score_threshold_boost: 0.08
    risk_multiplier: 0.70

  suspended:
    pf_threshold: 0.90
    min_trades: 50

  recovery:
    degraded_to_active:
      min_pf: 1.15
      min_wr: 0.45
      min_post_degraded_trades: 15
    suspended_cooldown_hours: 72
    probe:
      risk_multiplier: 0.40
      window_trades: 15
      pass_pf: 1.10
      pass_wr: 0.43
      max_consecutive_failures: 3
      retry_cooldown_hours: 72
      final_probe_after_days: 30

  regime_isolation:
    single_regime_pf_threshold: 0.90
    healthy_regime_pf_threshold: 1.20

  max_suspended_classes: 1
  overrides: {}                     # e.g., breakout: "force_active"

capital_concentration:
  enabled: true
  base_weight:
    tqs_weight: 0.45
    asset_weight: 0.30
    execution_weight: 0.25
    input_range: [0.25, 0.85]
    output_range: [0.50, 1.30]
  class_health_modifiers:
    active: 1.00
    degraded: 0.70
    suspended: 0.00
    probe: 0.40
  conviction:
    strategy_pf_range: [1.0, 2.0]
    strategy_max_momentum: 0.15
    asset_pf_range: [1.0, 2.0]
    asset_max_momentum: 0.10
    min_trades_for_momentum: 10
  bounds:
    min_capital_weight: 0.40
    max_capital_weight: 1.50
    min_effective_risk_pct: 0.05
    max_effective_risk_pct: 0.40
  budget:
    max_total_deployed_risk_pct: 4.0

signal_expiry:
  enabled: true
  max_signal_age_ms:
    MX: 8000
    VR: 12000
    MPC: 10000
    RBR: 10000
    LSR: 6000
  max_price_drift_pct:
    MX: 0.12
    VR: 0.08
    MPC: 0.10
    RBR: 0.10
    LSR: 0.15
  min_rr_after_drift: 1.0
  queue_age_discard_factor: 0.75    # discard queued signals at 75% of max age

  latency_adaptation:
    warn_avg_age_ms: 3000
    reduce_setups_avg_age_ms: 5000
    reduce_setups_to: 10
    disable_fast_strategies_avg_age_ms: 8000
    disable_strategies: ["LSR", "MX"]

  expiry_rate_adaptation:
    normal_rate: 0.05
    elevated_rate: 0.15
    elevated_age_extension_ms: 2000
    elevated_revert_after_seconds: 3600
    critical_rate: 0.15             # triggers conservation mode
```

---

## Appendix F: Complete Signal Pipeline (All Three Documents)

```
WebSocket Tick Stream (16 symbols)
    │
    ▼
CandleBuilder (1m → 3m/5m/15m/1h)
    │
    ├─── On 1h close ──── Bias Layer Update
    │
    ├─── On 15m close ─── Regime Reclassify
    │                      Asset Ranking Update (Hardening §6)
    │
    ├─── On 5m close ──── STAGE A: Setup Qualification
    │                      ├─ Regime gate
    │                      ├─ 5 strategy setup evaluation
    │                      ├─ Edge Validity class gate ◄── [Final §1]
    │                      ├─ Bias score (≥ 0.35, or tier-adjusted)
    │                      └─ → ActiveSetups registry
    │
    └─── On 1m close ──── STAGE B: Trigger Check (active setups only)
                           │
                           ▼
                    Global Trade Filter (Hardening §2)
                    ├─ Regime throttle
                    ├─ Chop detector
                    ├─ Volatility filter
                    ├─ Loss streak gate
                    ├─ Cluster guard (60s cooldown, queue)
                    └─ Session budget
                           │
                           ▼
                    RiskGate (V1 §4, adapted)
                    ├─ Position limits (1 per symbol)
                    ├─ Directional exposure (Hardening §5)
                    ├─ Portfolio heat (8%)
                    ├─ EV gate
                    ├─ Crash defense multiplier
                    └─ No-trade condition check (Hardening §3)
                           │
                           ▼
                    Trade Quality Score (Hardening §8)
                    ├─ setup + trigger + microstructure + execution + context
                    ├─ TQS < 0.35 → REJECT
                    └─ TQS ≥ 0.35 → APPROVE
                           │
                           ▼
                    Capital Concentration Engine ◄── [Final §2]
                    ├─ base_weight (TQS × asset × execution)
                    ├─ class_health_modifier (edge monitor status)
                    ├─ conviction_modifier (strategy PF × asset PF)
                    └─ → effective_risk_pct (0.05%–0.40%)
                           │
                           ▼
                    PositionSizer (V1, with effective_risk_pct)
                    ├─ risk_usdt = effective_risk_pct × capital
                    ├─ quantity = risk_usdt / stop_distance
                    └─ cap at max_capital_pct per asset tier
                           │
                           ▼
                    Signal Expiry Check ◄── [Final §3]
                    ├─ signal_age < max_age_ms?
                    ├─ price_drift < max_drift_pct?
                    ├─ stop_loss not already hit?
                    ├─ R:R still ≥ 1.0 after drift adjustment?
                    └─ EXPIRED → discard | VALID → execute
                           │
                           ▼
                    ExecutionManager
                    ├─ OrderRouter (limit-first or market per asset)
                    ├─ Fill tracking (ExecutionRecord)
                    └─ Position lifecycle (SL/TP/trailing/time_stop)
                           │
                           ▼
                    Post-Trade Recording
                    ├─ Learning Loop matrices (Hardening §9)
                    ├─ Edge Validity class trackers ◄── [Final §1]
                    ├─ Execution profile update (Hardening §4)
                    ├─ Time stop regret tracking (Hardening §7)
                    └─ Strategy health recalc (Hardening §9.5)
```

---

*End of Final Addendum*
