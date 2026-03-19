# IDSS Architecture Assessment
## NexusTrader — Technical Review & Upgrade Evaluation
**Date:** 2026-03-14
**Scope:** Full evaluation of 6 proposed structural upgrades to the Intelligent Dynamic Signal System
**Analyst:** Claude (Anthropic)

---

## Executive Summary

Before evaluating each proposed upgrade, the most important finding of this review is that **NexusTrader already implements substantial portions of all six proposed upgrades**. The architecture is considerably more mature than the surface-level description in the prompt suggests. The real work is not building these systems — it is **wiring them together** so they actually influence the live IDSS pipeline at runtime. Several critical components exist but are not integrated into the scanner loop.

The table below maps each upgrade to its current implementation status:

| Upgrade | Description | Current Status |
|---------|-------------|----------------|
| 1 | Probabilistic Regime Model | ✅ Built — `HMMRegimeClassifier` (`hmm_regime_classifier.py`) with `classify_combined()` blending HMM 60% + rule-based 40%. **Not wired into scanner.** |
| 2 | Crash Detection & Emergency System | ✅ Built — `CrashDefenseController` (`crash_defense_controller.py`) with 4-tier graduated response (DEFENSIVE/HIGH_ALERT/EMERGENCY/SYSTEMIC). **Crash scoring source unclear.** |
| 3 | Adaptive Model Activation | ❌ Not built — regime probabilities from HMM exist but are not passed to signal models |
| 4 | Dynamic Confluence Scoring | ❌ Not built — static 0.45 threshold; regime probabilities not used to adjust it |
| 5 | Expected Value Optimization | ❌ Not built — R:R filter is static 1.3; no win-rate estimation |
| 6 | Institutional Risk Engine | 🟡 Partially built — `CorrelationController` (live correlation + directional caps) exists. Volatility-adjusted exposure and tail-risk are missing. |

The highest-leverage work is therefore: (1) wiring HMM probabilities into signal model weights and confluence scoring, (2) adding a real-time crash score source that feeds `CrashDefenseController`, and (3) adding volatility-adjusted exposure scaling.

---

## Upgrade 1 — Probabilistic Market Regime Model

### What already exists

`core/regime/hmm_regime_classifier.py` contains a complete `HMMRegimeClassifier` with:
- 6-state Gaussian HMM with full covariance, trained on 4 features (log returns, realised vol, ADX-normalised, volume momentum)
- `classify()` — returns `(regime_label, confidence, regime_probs)` as a full probability distribution over all 6 regimes
- `classify_combined()` — blends HMM (60%) with rule-based RegimeClassifier (40%), which is the correct ensemble approach
- Graceful fallback to rule-based if hmmlearn unavailable
- Uncertainty penalty: if two states are within 0.15 probability of each other, confidence is multiplied by 0.75

There is also `core/regime/hmm_classifier.py`, a simpler 4-state HMM (`HMMClassifier`) with online retraining every 5 bars. This is the earlier Phase 2 implementation, superseded by `HMMRegimeClassifier`.

Additionally, `core/regime/ms_garch_forecaster.py` contains a Markov-Switching GARCH forecaster for volatility regime classification (LOW_VOL / HIGH_VOL) using the `arch` library.

### The problem

Despite all of this existing, `scanner.py` imports `RegimeClassifier` and optionally `EnsembleRegimeClassifier` — but **not** `HMMRegimeClassifier`. The probabilistic `regime_probs` dict returned by `classify_combined()` is never passed into the signal pipeline. The scanner produces a regime label and confidence, but discards the probability distribution entirely.

The result is that the ADX dead-zone problem persists in practice, even though the tools to solve it are already written and sitting unused.

### Recommendation

The correct approach is already implemented — it just needs to be wired in. Specifically:

**In `ScanWorker.__init__`**, replace or augment the regime classifier with `HMMRegimeClassifier` and call `classify_combined()` instead of the raw rule-based `classify()`. The returned `regime_probs` dict should be stored alongside the regime label and passed downstream.

**MS-GARCH integration**: use `MSGARCHForecaster` output to produce a `volatility_probability` scalar (0–1) representing probability of high-volatility regime. This complements the HMM by specialising in volatility dynamics rather than trend structure.

**Feature expansion**: The current HMM uses 4 features (return, vol, ADX, volume momentum). For BTC-dominant crypto markets, two additional features would improve state separation:
- **Funding rate normalised** (rolling 24h mean vs historical): captures leverage buildup which precedes both squeeze rallies and cascades
- **Cross-asset momentum z-score** (mean of 5-pair 4h returns z-scored): distinguishes BTC-led moves from altcoin-isolated ones

**State count**: 6 states (current) is appropriate. The practical crypto regimes worth distinguishing are: bull_trend, bear_trend, ranging, volatility_expansion, volatility_compression, uncertain. Adding `crash` as a 7th state is tempting but problematic — crash events are too rare in training data for HMM to learn them reliably. Crash detection belongs in its own dedicated system (Upgrade 2), not inside the regime model.

**Lookback window**: The current 200-bar training minimum is marginal. On 4h bars, 200 bars = ~33 days. HMM with 6 states and full covariance on 4 features needs enough samples per state to estimate a 4×4 covariance matrix (minimum ~20 samples per state, so ~120 total). 200 bars provides adequate coverage assuming states are roughly equally distributed. However, for the initial fit on startup, 500 bars (available via backtesting loader) would give noticeably more stable state mappings. **Recommendation: increase `_MIN_TRAIN_BARS` to 300, and on first startup fetch 500 bars for the initial HMM fit.**

### Suggested configuration parameters

```yaml
hmm_regime:
  enabled: true
  n_components: 6
  n_iter: 200
  min_train_bars: 300
  retrain_every_n_bars: 50
  hmm_weight: 0.60          # weight of HMM vs rule-based in blend
  uncertainty_gap_threshold: 0.15   # gap below which confidence is penalised
  feature_funding_rate: true
  feature_cross_asset_momentum: true
```

---

## Upgrade 2 — Crash Detection & Emergency Risk System

### What already exists

`core/risk/crash_defense_controller.py` implements a complete 4-tier graduated response engine:
- **DEFENSIVE** (score ≥ 5.0): halt new longs, tighten stops to 1.5×ATR, reduce size cap to 15%
- **HIGH_ALERT** (score ≥ 7.0): disable auto-execute, close 50% of longs, trailing stops at 1%
- **EMERGENCY** (score ≥ 8.0): close all longs, read-only mode
- **SYSTEMIC** (score ≥ 9.0): close all positions including shorts, safe mode requiring manual override

This is well-designed. The graduated approach is correct — binary "crash/no-crash" systems cause unnecessary whipsaw. The tier thresholds (5/7/8/9) on a 0–10 scale give good granularity.

### The critical gap

**The crash score itself has no confirmed source.** `CrashDefenseController.respond_to_tier()` accepts a `score` argument from the caller, but there is no `CrashDetector` or `CrashScorer` module in `core/risk/`. The controller exists without the detector that feeds it. This means the 4-tier response system is currently inert.

### Recommended CrashDetector design

A `CrashDetector` module should produce a composite crash score (0–10) by combining 7 individual signal components. Each component produces a sub-score (0–1), the weighted sum is mapped to the 0–10 range. All weights and thresholds must be configurable.

**Component 1 — ATR velocity spike (weight 2.0)**
Compute current ATR versus a 20-bar rolling ATR baseline. Sub-score = `min(1.0, (current_atr / baseline_atr - 1.0) / 1.5)`. A 2.5× ATR spike maps to a sub-score of 1.0. This is the fastest-reacting indicator and gets the highest weight.

**Component 2 — Price velocity (weight 1.8)**
Compute the 3-bar return z-score using a 30-bar rolling mean and std. Sub-score activates only for negative moves: `max(0, min(1.0, (-z_score - 1.5) / 2.5))`. A −4σ 3-bar move maps to sub-score 1.0. This directly measures crash velocity.

**Component 3 — Liquidation cascade volume (weight 1.5)**
Use Coinglass data (API key already configured). Sub-score = `min(1.0, liquidation_volume_24h / liquidation_baseline_30d)`. A 3× spike in hourly liquidation volume versus 30-day baseline maps to 1.0. This is a lagging but high-conviction indicator.

**Component 4 — Order book imbalance (weight 1.2)**
From active exchange ticker: `bid_volume / (bid_volume + ask_volume)`. Sub-score = `max(0, 1.0 - imbalance_ratio / 0.35)`. When ask volume dominates (imbalance below 0.35), sub-score increases. This is real-time but noisy; lower weight is appropriate.

**Component 5 — Funding rate flip (weight 1.0)**
Funding rate moving from positive to significantly negative within 2 periods. Sub-score = `min(1.0, max(0, (-funding_rate - 0.0001) / 0.0008))`. Strongly negative funding (−0.08%+ per period) maps to sub-score 1.0.

**Component 6 — Cross-asset correlated decline (weight 1.5)**
All 5 symbols declining simultaneously over 1-hour window. Sub-score = `declining_count / 5 * average_decline_magnitude_normalised`. If all 5 pairs drop >2% in one hour, sub-score approaches 1.0. This distinguishes market-wide crash from single-asset event.

**Component 7 — Open interest collapse (weight 1.0)**
Rapid OI decrease from Coinglass. Sub-score = `min(1.0, max(0, (oi_drop_pct - 5) / 15))`. A 20%+ OI drop maps to 1.0. Slower signal but highly reliable — OI collapse accompanies forced deleveraging.

**Total crash score = sum(weight × sub_score) / sum(weights) × 10**

Maximum possible score: 10.0. Tier thresholds remain as designed (5/7/8/9).

**State machine transitions:**
Entry into a crash tier should be fast (single bar confirmation). Recovery requires seeing `score < tier_threshold − 1.5` for **5 consecutive bars** (configurable). This hysteresis prevents bouncing in and out of defensive mode during a choppy bottom.

**Recovery resume conditions (all must pass before NORMAL is restored):**
- ATR returning toward baseline (< 1.3× the 20-bar average)
- Funding rate stabilising (absolute value < 0.0002)
- Cross-asset returns turning mixed/positive over 3 bars

**Important architectural note:** The CrashDetector should run on every **price tick** or at minimum every **REST poll cycle** — not just on scan cycles. Crashes can develop in 5–10 minutes; waiting for the next 4h scan cycle to detect them defeats the purpose entirely.

### Suggested configuration parameters

```yaml
crash_detector:
  enabled: true
  eval_interval_seconds: 60     # run every 60s regardless of scan cycle
  weights:
    atr_spike: 2.0
    price_velocity: 1.8
    liquidation_cascade: 1.5
    cross_asset_decline: 1.5
    orderbook_imbalance: 1.2
    funding_rate_flip: 1.0
    oi_collapse: 1.0
  tier_thresholds:
    defensive: 5.0
    high_alert: 7.0
    emergency: 8.0
    systemic: 9.0
  recovery_bars_required: 5     # consecutive bars below threshold before deescalation
  recovery_hysteresis: 1.5      # score must be this far below threshold to recover
```

---

## Upgrade 3 — Adaptive Model Activation

### Current state

Every signal model calls `is_active_in_regime(regime)` which returns a binary True/False based on the committed regime label. In uncertain regime, most models return False. Since the HMM probability distribution is not currently passed downstream, models cannot use the probabilistic information even though it exists.

### Recommended design

The fix is to replace the binary `is_active_in_regime()` gate with a continuous `activation_weight` computed from `regime_probs`. This is a clean change that does not alter any model logic — it only changes how `ConfluenceScorer` weights each model's output.

Each model gets a regime affinity profile: a dict mapping regime names to a base weight multiplier. Example:

```python
REGIME_AFFINITY = {
    "trend_model":            {"bull_trend": 1.0, "bear_trend": 0.9, "ranging": 0.1, "vol_expansion": 0.6, "vol_compression": 0.2, "uncertain": 0.3},
    "mean_reversion_model":   {"bull_trend": 0.2, "bear_trend": 0.2, "ranging": 1.0, "vol_expansion": 0.1, "vol_compression": 0.8, "uncertain": 0.4},
    "momentum_breakout_model":{"bull_trend": 0.7, "bear_trend": 0.7, "ranging": 0.1, "vol_expansion": 1.0, "vol_compression": 0.1, "uncertain": 0.2},
    "vwap_reversion_model":   {"bull_trend": 0.5, "bear_trend": 0.5, "ranging": 0.8, "vol_expansion": 0.3, "vol_compression": 0.7, "uncertain": 0.5},
    "liquidity_sweep_model":  {"bull_trend": 0.4, "bear_trend": 0.6, "ranging": 0.9, "vol_expansion": 0.5, "vol_compression": 0.5, "uncertain": 0.4},
    "funding_rate_model":     {"bull_trend": 0.8, "bear_trend": 0.8, "ranging": 0.5, "vol_expansion": 0.7, "vol_compression": 0.4, "uncertain": 0.5},
    "order_book_model":       {"bull_trend": 0.7, "bear_trend": 0.7, "ranging": 0.6, "vol_expansion": 0.5, "vol_compression": 0.6, "uncertain": 0.5},
    "sentiment_model":        {"bull_trend": 0.9, "bear_trend": 0.7, "ranging": 0.4, "vol_expansion": 0.5, "vol_compression": 0.3, "uncertain": 0.4},
}
```

The `activation_weight` for a model given `regime_probs` is the dot product of the affinity vector and the probability vector:

```
activation_weight = sum(affinity[regime] × regime_probs[regime] for regime in regimes)
```

This is fed into `ConfluenceScorer` as an additional multiplier on the model's base weight. The result:
- In a pure bull_trend regime, TrendModel fires at 100% weight and MeanReversionModel at 20%
- In uncertain regime (probs distributed roughly evenly), TrendModel fires at ~38% weight (average of its affinities), which is far better than zero
- The total confluence score in uncertain regime will be lower, which is correct — but it will not always be zero, which means more candidates surface

**Important nuance about "uncertain" activation weights:** The uncertain-regime affinities above are intentionally non-zero (0.2–0.5). This is deliberate. When all regime probabilities are roughly equal (genuine uncertainty), every model should contribute a reduced weight rather than most being disabled. This solves the core "all values are '—'" problem while still producing lower scores that respect the reduced conviction.

**Crash interaction:** When `CrashDetector` fires DEFENSIVE or higher, all activation weights for long-direction models should be multiplied by zero regardless of regime probabilities. Crash mode overrides everything.

### Suggested configuration parameters

```yaml
adaptive_model_activation:
  enabled: true
  use_regime_probabilities: true
  regime_affinity_overrideable: true   # allow per-model affinity tuning in UI
  min_activation_weight: 0.10         # floor — never fully disable any model
  crash_long_multiplier: 0.0          # zero out longs in crash mode
  crash_short_multiplier: 1.5         # amplify shorts in crash mode
```

---

## Upgrade 4 — Dynamic Confluence Scoring

### Current state

`ConfluenceScorer` uses a static threshold read from `idss.min_confluence_score` (currently 0.45). While this is configurable, it does not adapt to the number of models that fired or the regime confidence level.

### The mathematical problem

With 8 models and only 3 firing in uncertain regime, even if all 3 agree strongly (strength=0.8), the maximum achievable score depends entirely on which 3 models fired and how their weights are normalised. If 3 low-weight models fire, even perfect agreement may produce a score below 0.45. This is structurally incorrect — the threshold should reflect the number of available models, not a fixed bar.

### Recommended dynamic threshold formula

```
adjusted_threshold = base_threshold
                   × regime_confidence_factor
                   × model_count_factor
                   × volatility_factor
```

**regime_confidence_factor**: When HMM confidence is high (≥ 0.7), raise threshold slightly (factor 1.1) — we're confident in the regime so be more selective. When confidence is low (≤ 0.4), reduce threshold (factor 0.85) — fewer good models will fire so lower the bar. Linear interpolation between these extremes.

**model_count_factor**: Adjusts for how many models are "eligible" given current regime activations. If the weighted sum of activation weights is less than 40% of maximum (most models inactive), lower threshold proportionally: `factor = max(0.75, active_weight_sum / max_weight_sum)`. This prevents good signals from being discarded simply because the regime deactivated most models.

**volatility_factor**: During high-volatility regimes (vol_expansion or MS-GARCH HIGH_VOL), raise threshold by 1.15 — high-vol environments have noisier indicators, demand stronger confluence. During vol_compression (pre-breakout coiling), lower to 0.95 — models will be more consistent in low-vol environments.

**Example calculations:**
- Confident bull_trend, all models active, low volatility: 0.45 × 1.05 × 1.00 × 0.95 = 0.45
- Uncertain regime, 3 models active, normal volatility: 0.45 × 0.85 × 0.78 × 1.00 = 0.30
- Vol_expansion, 5 models active, high confidence: 0.45 × 1.10 × 0.92 × 1.15 = 0.52

**Per-model win rate tracking**: The highest-value enhancement to the confluence scorer is tracking per-model historical accuracy. After each closed trade, record which models contributed to the signal and whether the trade was profitable. Use a rolling window of the last 30 trades per model to compute a win_rate for each. Feed this back as a Bayesian prior adjustment to model weights in the scorer. A model with a recent 65% win rate should receive weight +15% above its base; a model at 40% win rate should be at −10%. This transforms the scorer from static to adaptive over time.

### Suggested configuration parameters

```yaml
dynamic_confluence:
  enabled: true
  base_threshold: 0.45
  min_threshold_floor: 0.28        # never go below this regardless of factors
  max_threshold_ceiling: 0.65      # never go above this regardless of factors
  regime_confidence_high: 0.70     # threshold above which confidence_factor = 1.10
  regime_confidence_low: 0.40      # threshold below which confidence_factor = 0.85
  vol_expansion_factor: 1.15       # raise threshold in volatile regimes
  vol_compression_factor: 0.95     # lower threshold in coiling regimes
  win_rate_tracking_enabled: true
  win_rate_window: 30              # rolling trades per model
  win_rate_weight_adjustment: 0.15 # max weight adjustment from win rate
```

---

## Upgrade 5 — Expected Value Optimization

### The problem with static R:R

A fixed R:R ≥ 1.3 is only meaningful if win rate is held constant. Consider two trades:
- Trade A: R:R = 1.5, win rate 35% → EV = (0.35 × 1.5) − (0.65 × 1.0) = 0.525 − 0.650 = **−0.125 (losing)**
- Trade B: R:R = 1.1, win rate 60% → EV = (0.60 × 1.1) − (0.40 × 1.0) = 0.660 − 0.400 = **+0.260 (profitable)**

The current system would take Trade A and reject Trade B. This is backwards.

### Recommended EV framework

The first requirement is a win probability estimator. Three approaches are available, listed in order of readiness:

**Option A — Confluence score as calibrated win probability (immediate, no historical data needed)**
Map the confluence score to an estimated win probability using a sigmoid calibration: `win_prob = sigmoid(k × (score − midpoint))` where k and midpoint are calibrated empirically. A reasonable starting calibration: score=0.45 → win_prob=0.45, score=0.60 → win_prob=0.55, score=0.80 → win_prob=0.68. This is an approximation but it's immediately deployable.

**Option B — Per-regime historical win rates (available after 50+ closed trades)**
Maintain a rolling win rate per (regime, timeframe, signal_direction) tuple. Use this as the win probability estimate. This is more accurate than Option A and converges to truth over time.

**Option C — RL ensemble calibration (available once RL is enabled)**
The RL ensemble already produces probability estimates. Once trained, its output is better calibrated than either Option A or B.

**Recommended implementation**: Start with Option A as the base, blend in Option B as trade history accumulates (weight Option B by `min(1.0, closed_trades_count / 50)`), and eventually blend in Option C when RL is enabled.

**EV calculation:**
```
EV = (win_prob × reward) - ((1 - win_prob) × risk)
```

where `reward = target - entry` and `risk = entry - stop` (for longs).

**Replace static R:R gate with EV gate.** Keep minimum R:R = 1.0 as an absolute floor (never take a trade with less reward than risk regardless of win probability), but make the primary filter `EV > ev_threshold`. Starting `ev_threshold = 0.05` (5% positive expectation).

**R:R still has a role** as a sanity check on position structure, but it should not override EV. A trade with R:R = 1.1 and 62% win rate is far better than one with R:R = 1.8 and 35% win rate.

**Regime-adjusted EV**: The winning probability estimate should be multiplied by a regime reliability factor. In high-confidence trending regimes, the HMM classification is reliable — no adjustment. In uncertain regime (HMM confidence < 0.4), reduce all win probability estimates by 15% to account for the increased uncertainty. In crash-adjacent conditions (crash score > 3.0), reduce by 30%.

### Suggested configuration parameters

```yaml
expected_value:
  enabled: true
  ev_threshold: 0.05                 # minimum EV to pass gate
  min_rr_floor: 1.0                  # absolute floor regardless of EV
  win_prob_calibration:
    score_midpoint: 0.55             # confluence score → 50% win prob
    sigmoid_steepness: 8.0
  regime_uncertainty_penalty: 0.15   # reduce win_prob by this in uncertain
  crash_proximity_penalty: 0.30      # reduce win_prob by this near crash tier
  historical_weight_ramp_trades: 50  # trades to ramp Option B weight from 0→1
```

---

## Upgrade 6 — Institutional-Grade Risk Engine

### What already exists

`CorrelationController` already handles:
- Pairwise correlation cap (75% max between any two open positions)
- Directional exposure limit (70% max on one side)
- Live correlation computation from actual returns (60-period rolling)
- Weekend position reduction (Friday 17:00 UTC)

The `RiskGate` already handles: expiry, already-in-position, correlation cap, max concurrent positions (3), drawdown gate (15%), capital check, spread check (0.3%), R:R check (1.3).

### Gaps to close

**Volatility-adjusted exposure caps**: The current system uses a fixed 15% max position size. This is not volatility-aware. During high-volatility regimes, a 15% position in BTC with 3× ATR stop-width represents a much larger dollar loss than the same 15% position during low-volatility ranging. The fix is to normalise position risk by ATR rank:

```
vol_adjusted_max_size = base_max_size / atr_percentile_rank_90d
```

Where `atr_percentile_rank_90d` is the current ATR expressed as a percentile of the past 90 days of ATR values (0.1 to 1.0). During extreme volatility (top decile), maximum position size is automatically halved relative to the calm-market maximum.

**Portfolio heat limit**: Total "portfolio heat" (sum of maximum dollar losses across all open positions at their stops) should not exceed a configurable fraction of capital. Currently there is no such check. Add a `portfolio_heat_check` to `RiskGate`: `sum(position_size × (entry - stop) / entry for each open position) / total_capital ≤ max_portfolio_heat`. Start at 6% — meaning at any moment, if every open stop fires, you lose at most 6% of capital.

**Loss streak protection**: After 3 consecutive losing trades, position size should automatically reduce by 50% for the next N trades until a winning trade restores confidence. This is a standard institutional risk management protocol. It is not currently implemented. The `PositionSizer` is the right place to add this as a `loss_streak_scalar`.

**CVaR tail risk**: For each candidate trade, estimate the conditional value at risk (expected loss in the worst 5% of scenarios) using the ATR-based stop distance and the fat-tail distribution of crypto returns. Crypto return distributions have excess kurtosis significantly higher than normal distribution assumptions — using simple volatility-scaled estimates understates tail risk. A practical approximation: multiply the ATR-based stop by a fat-tail factor of 1.5 for risk calculation purposes while keeping the actual stop at the ATR level. This accounts for gap risk (weekend opens, news spikes) without widening stops to impractical levels.

**Tier-based leverage limits**: Currently there are no leverage limits by regime. Add to `RiskGate`:
```
max_leverage:
  bull_trend: 3.0
  bear_trend: 2.0
  ranging: 1.5
  vol_expansion: 1.5
  vol_compression: 2.0
  uncertain: 1.0
  defensive_mode: 1.0   # overrides all when crash tier active
```

### Suggested configuration parameters

```yaml
risk_engine:
  portfolio_heat_max_pct: 0.06         # max 6% total stop-fire loss
  vol_adjusted_sizing_enabled: true
  vol_adjustment_base: 1.0
  atr_percentile_lookback_days: 90
  loss_streak_trigger: 3               # consecutive losses before size reduction
  loss_streak_size_multiplier: 0.50
  loss_streak_recovery_wins: 2         # winning trades to restore full size
  fat_tail_risk_multiplier: 1.5        # multiply ATR for risk calculation
  max_leverage_by_regime:
    bull_trend: 3.0
    bear_trend: 2.0
    ranging: 1.5
    vol_expansion: 1.5
    vol_compression: 2.0
    uncertain: 1.0
  max_leverage_defensive_mode: 1.0
```

---

## Additional Analysis — ATR Stops, Kelly Sizing, Lookback, Multi-TF

### ATR-based stop logic

The current ATR-based stop is regime-agnostic. This is its primary flaw. In ranging regimes, ATR is naturally small and tight stops get clipped by normal price noise constantly. In volatile regimes, ATR is large but the stop may be too wide to produce meaningful R:R.

**Recommended: regime-adjusted ATR multipliers**

| Regime | Stop Multiplier | Rationale |
|--------|----------------|-----------|
| bull_trend | 1.5× ATR | Trending environment, use tight-ish stop above structure |
| bear_trend | 1.5× ATR | Same logic |
| ranging | 2.5× ATR | Wide stop required to avoid noise-induced exits |
| vol_expansion | 3.0× ATR | Volatility is high, require wide stop or pass on trade |
| vol_compression | 1.8× ATR | Coiling pre-breakout, moderate stop |
| uncertain | 2.0× ATR | Default wide to protect against misclassification |

**Structure-based stop as alternative**: For BTC specifically, swing-high/swing-low based stops are often more meaningful than ATR. If the last significant swing low is within 2×ATR of entry, use the swing low minus a 0.3% buffer. This provides a stop that the market has "validated" as significant. This should be optional (configurable) and falls back to ATR when no recent structure is identifiable.

### Half-Kelly position sizing for crypto

The half-Kelly formula is theoretically sound but has two practical problems in crypto:

**Problem 1 — Kelly assumes stationarity.** It assumes the win rate and R:R are constant across trades. Crypto markets have heavy regime-dependence: win rates during trending markets can be 60%+; during choppy uncertain regimes they may be 35%. Applying a single Kelly fraction across both is incorrect.

**Problem 2 — Kelly assumes no ruin risk from single trades.** With the fat-tailed return distribution in crypto, a Kelly-optimal position can lose dramatically more than its stop implies during gap events. The standard recommendation for asset classes with fat tails is to use quarter-Kelly, not half-Kelly.

**Recommended modification**: Move to **quarter-Kelly** as the base, then add the existing regime and volatility multipliers on top. Also add hard caps:
- Minimum position size: 0.3% of capital
- Maximum position size: 4% of capital per trade regardless of Kelly output
- Maximum total open risk (portfolio heat): 6% of capital as above

The regime multipliers in `PositionSizer` are well-designed. The `uncertain` multiplier of 0.4 and `crisis` multiplier of 0.0 are appropriate. Add a `defensive_mode` multiplier of 0.25 (quarter-sized during Tier 1 defensive, not zero — you still want some exposure to benefit if the dip is shallow).

### 200-bar lookback window for regime classification

On **4h bars**, 200 bars = 33 days. This is adequate for detecting current regime but has two edge cases:

- **ADX convergence**: ADX needs 28 bars minimum to stabilise (2× period). On 200-bar fetch this is fine.
- **HMM training quality**: As noted in Upgrade 1, 300 bars would be more reliable for the initial HMM fit. The marginal computation cost is minimal.
- **Regime transition detection**: Recent history (last 50 bars) matters more than older history for current regime, but older history (bars 51–200) is needed to estimate what a "normal" regime looks like for this asset.

**On 1h bars**, 200 bars = 8.3 days. This is borderline — consider fetching 300 bars minimum on 1h timeframes.

**On 1d bars**, 200 bars = ~6.5 months. This is excellent.

**Recommendation**: Keep 200 bars as the minimum, but make the fetch count configurable and increase defaults: 300 bars for 1h and 4h, 250 bars for 1d. These are trivially small API calls.

### Multi-timeframe confirmation

Multi-TF confirmation is currently optional. It should become **mandatory for long-duration trades**, specifically trades on 4h and 1d timeframes.

The recommended hierarchy:

| Trade TF | Required Higher TF Check | Logic |
|----------|-------------------------|-------|
| 1h | 4h bias check | 1h signal only passes if 4h regime is consistent (not opposite) |
| 4h | Daily bias check | 4h signal only passes if daily trend direction agrees |
| 1d | Weekly trend check | Daily signal requires weekly EMA slope in same direction |

**Implementation**: Add an optional `higher_tf_bias` parameter to `OrderCandidate`. In `ScanWorker`, after generating the 1h/4h signal, fetch the higher timeframe's last 50 bars, run `RegimeClassifier`, and store the higher TF regime as metadata. In `RiskGate`, if `require_mtf_confirmation: true` (configurable), reject candidates where the signal direction contradicts the higher TF regime.

**The practical impact**: MTF confirmation will reduce trade frequency by approximately 15–25%, but the trades that survive will have a meaningfully higher win rate. This is the correct trade-off for capital preservation.

---

## Priority Implementation Roadmap

Given everything above, here is the recommended implementation sequence, ordered by impact-to-effort ratio:

**Phase A — Wire existing components (highest impact, low effort)**

These are things that are already built but not connected:

1. Wire `HMMRegimeClassifier.classify_combined()` into `ScanWorker` to get `regime_probs` dict in the pipeline
2. Pass `regime_probs` to `ConfluenceScorer` — update scorer to use adaptive model weights
3. Build `CrashDetector` using the 7-component design above and connect it to the existing `CrashDefenseController`
4. Run `CrashDetector` on every REST poll cycle, not just on scan cycles

**Phase B — Scoring improvements (medium impact, medium effort)**

5. Implement dynamic confluence threshold formula
6. Implement regime-adjusted ATR multipliers for stops/targets
7. Add portfolio heat check to `RiskGate`
8. Switch half-Kelly to quarter-Kelly with 4% hard cap

**Phase C — Advanced improvements (high impact, higher effort)**

9. Add win-rate tracking per model and use as Bayesian weight adjustment
10. Implement EV scoring as primary filter, demote R:R to floor-only
11. Add loss-streak protection to `PositionSizer`
12. Implement MTF confirmation requirement for 4h+ trades

**Phase D — Long-term (highest potential, requires data accumulation)**

13. Expand HMM features (funding rate, cross-asset momentum)
14. Calibrate win probability estimates from closed trade history
15. Enable RL ensemble and integrate its probability estimates into EV scoring
16. Implement live correlation recomputation on each scan cycle

---

## Risk & Implementation Pitfalls

**HMM non-stationarity**: HMM assumes the state transition matrix is constant over time. Crypto markets violate this — bull cycles have different transition dynamics than bear cycles. Mitigate by retraining HMM every 50 new bars rather than only at startup. The current `_RETRAIN_THRESHOLD = 5` in `hmm_classifier.py` is too frequent (retraining every 5 bars on a 150-bar window is noisy). Use 50 bars for `HMMRegimeClassifier`.

**Crash detector false positives**: High-volume legitimate breakouts can look like crash components 1, 2, and 4 simultaneously. A BTC breakout to new highs with high ATR, velocity, and order book imbalance will generate a crash score even though the move is bullish. Guard against this by requiring that price_velocity component only fires for **negative** moves (already specified above), and cross-asset component only fires when the correlation of declines is above 0.7 (not just that prices are declining).

**Win rate estimation bootstrap problem**: Option A (sigmoid calibration of confluence score) makes a strong assumption — that confluence score is correlated with trade success. This is theoretically sound but empirically unverified until you have 30+ closed trades. Start conservatively (use Option A as a soft signal, not a hard gate) and transition to Option B once you have the data.

**Kelly overcalculation at small sample sizes**: In early operation with few closed trades, don't let the Kelly fraction be calculated from a sample size smaller than 20 trades. With fewer trades, default to 0.5% fixed position size regardless of Kelly output.

**Correlation matrix staleness**: The `CorrelationController` initialises with static pre-computed correlations. During extreme market events, correlations among crypto assets spike toward 1.0 — exactly when you most need them to be accurately measured. The `update_live_correlation()` method uses 60-period returns. Make sure this is called after every scan cycle for all symbol pairs, so the correlation matrix stays live.

**CrashDefenseController deactivation**: The current `_deactivate_defensive_mode()` triggers immediately when score drops below 5.0. This can create rapid cycling (crash score oscillates around 5.0 during a volatile bottom). Enforce the 5-bar recovery requirement before deactivation.

---

## Configuration Summary — All New Parameters

```yaml
# Add to config/settings.py under respective sections

hmm_regime:
  enabled: true
  n_components: 6
  min_train_bars: 300
  retrain_every_n_bars: 50
  hmm_rule_blend_weight: 0.60

crash_detector:
  enabled: true
  eval_interval_seconds: 60
  recovery_bars_required: 5
  recovery_hysteresis: 1.5
  weights:
    atr_spike: 2.0
    price_velocity: 1.8
    liquidation_cascade: 1.5
    cross_asset_decline: 1.5
    orderbook_imbalance: 1.2
    funding_rate_flip: 1.0
    oi_collapse: 1.0

adaptive_activation:
  enabled: true
  min_activation_weight: 0.10
  crash_long_multiplier: 0.0
  crash_short_multiplier: 1.5

dynamic_confluence:
  enabled: true
  base_threshold: 0.45
  min_floor: 0.28
  max_ceiling: 0.65
  win_rate_tracking: true
  win_rate_window: 30

expected_value:
  enabled: true
  ev_threshold: 0.05
  min_rr_floor: 1.0
  regime_uncertainty_penalty: 0.15

risk_engine:
  portfolio_heat_max_pct: 0.06
  vol_adjusted_sizing: true
  atr_percentile_lookback_days: 90
  loss_streak_trigger: 3
  loss_streak_size_multiplier: 0.50
  fat_tail_risk_multiplier: 1.5
  kelly_fraction: 0.25           # quarter-Kelly (was 0.50)
  max_position_pct: 0.04         # hard cap 4% per trade
  min_position_pct: 0.003        # minimum 0.3%
  max_leverage_by_regime:
    bull_trend: 3.0
    bear_trend: 2.0
    ranging: 1.5
    vol_expansion: 1.5
    uncertain: 1.0

multi_tf:
  confirmation_required: true
  confirmation_timeframes:
    "1h": "4h"
    "4h": "1d"
    "1d": "1w"

scanner:
  ohlcv_bars:
    "1h": 300
    "4h": 300
    "1d": 250
```

---

*Document generated: 2026-03-14 | NexusTrader IDSS Architecture Assessment*
