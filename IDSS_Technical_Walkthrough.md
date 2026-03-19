# IDSS AI Scanner — Full Technical Walkthrough
**NexusTrader | As of 2026-03-14 (post-architecture upgrade)**
*Prepared for independent audit. All code references are exact.*

---

## Table of Contents

1. Purpose of the IDSS AI Scanner
2. End-to-End Workflow
3. Multiple Timeframe Logic
4. Per-Pair Computation
5. Column-by-Column Explanation
6. Data Sources
7. Why Values Show as "-"
8. Why Regime Shows "Uncertain"
9. Current Limitations and Gaps
10. Audit-Ready Mapping Table

---

## 1. Purpose of the IDSS AI Scanner

IDSS stands for **Intelligent Dynamic Signal System**. Its role is to autonomously scan a configured watchlist of cryptocurrency pairs, classify the market regime for each pair, run multiple signal-generation models, aggregate and score their outputs, apply portfolio risk constraints, and surface only those trade proposals that meet a minimum evidence threshold.

The scanner does not place orders. It produces `OrderCandidate` objects — structured trade proposals containing a direction, entry price, stop-loss, take-profit, position size, and a prose rationale — which are then passed to the execution layer if the user approves (or if the system is in automated mode).

The scanner operates on **closed candles only**. It re-runs on a timer aligned to the selected timeframe (e.g., every 3,600 seconds for the 1h timeframe). Each run is a complete, independent evaluation of every symbol on the watchlist.

---

## 2. End-to-End Workflow

This section follows a single scan cycle from trigger to UI display. Every class, method, and config key referenced below exists verbatim in the codebase.

### 2.1 Trigger — `AssetScanner._trigger_scan()` (`core/scanning/scanner.py:432`)

A `QTimer` fires every `TF_POLL_SECONDS[timeframe]` seconds (e.g., 3600s for 1h). When it fires:

1. `WatchlistManager.get_active_symbols()` returns the list of symbols from the enabled watchlists in `config/settings.py` under `scanner.watchlists`.
2. If `scanner.btc_only_mode` is `true`, only symbols starting with "BTC" are kept.
3. `exchange_manager.get_exchange()` returns the live ccxt exchange instance.
4. The active executor (paper or live) supplies `open_positions`, `available_capital`, and `drawdown_pct`.
5. A new `ScanWorker` QThread is created and started.

### 2.2 Ticker Fetch — `ScanWorker.run()` (`scanner.py:123`)

Before any per-symbol work, the worker calls:
```
tickers = exchange.fetch_tickers(symbols)
```
This fetches a batch snapshot of all symbols at once (bid, ask, last, volume, fundingRate, bidVolume, askVolume). These tickers feed:
- The `UniverseFilter` for volume and spread checks
- The spread map used later in `RiskGate.validate_batch()`
- The `CrashDetector` for funding rate flip and order book imbalance components

### 2.3 Universe Filter — `UniverseFilter.apply()` (`core/scanning/universe_filter.py`)

Applied to the full symbol list before any OHLCV fetch. Filters out pairs that fail any of:
- Minimum 24h volume (configurable; default guards against illiquid pairs)
- Spread percentage too wide

Only "qualifying" symbols proceed to the per-symbol pipeline.

### 2.4 Per-Symbol Pipeline — `ScanWorker._scan_symbol_with_regime()` (`scanner.py:231`)

For each qualifying symbol, the following steps run **sequentially** in the same thread:

#### Step 1: OHLCV Fetch
```python
limit = int(settings.get("scanner.ohlcv_bars", 300))
raw = exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
```
Returns at most 300 closed candles (configurable via `scanner.ohlcv_bars`). If fewer than 30 candles are returned (e.g., new listing), the symbol is skipped entirely and returns `(None, "", 0.0)`.

The raw list of `[timestamp_ms, open, high, low, close, volume]` rows is converted to a pandas DataFrame with a UTC DatetimeIndex.

#### Step 2: Indicator Calculation — `calculate_all(df)` (`core/features/indicator_library.py`)

A single function call computes every technical indicator into new columns on the same DataFrame. The resulting columns include (non-exhaustive):

- `ema_9`, `ema_20`, `ema_21`, `ema_50`, `ema_100`, `ema_200`
- `rsi_14`
- `macd`, `macd_signal`, `macd_hist`
- `adx`, `adx_pos`, `adx_neg`
- `atr_14`
- `bb_upper`, `bb_middle`, `bb_lower`, `bb_width`
- `stoch_k`, `stoch_d`
- `vwap` (rolling session VWAP)
- `volume_sma_20`

All indicator values are computed from the closed candles. The **last row** of the resulting DataFrame always represents the most recent closed candle.

#### Step 3: Regime Classification — `EnsembleRegimeClassifier.classify(df)` or `RegimeClassifier.classify(df)`

The scanner tries to instantiate `EnsembleRegimeClassifier` first (`core/regime/ensemble_regime_classifier.py`); if that import fails, it falls back to `RegimeClassifier` (`core/regime/regime_classifier.py`).

The rule-based `RegimeClassifier` examines indicator values on the last ~50 bars and applies a heuristic decision tree to assign one of 12 regime labels:

| Label | Meaning |
|---|---|
| `bull_trend` | EMA alignment up, ADX > 25 |
| `bear_trend` | EMA alignment down, ADX > 25 |
| `ranging` | Flat EMAs, low ADX, price bouncing inside Bollinger Bands |
| `volatility_expansion` | BB width expanding, ATR rising sharply |
| `volatility_compression` | BB width contracting, ATR falling |
| `accumulation` | Range-bound with rising volume |
| `distribution` | Range-bound with declining volume and weakening momentum |
| `recovery` | Recovering from recent drawdown with rising momentum |
| `squeeze` | Bollinger Band inside Keltner Channel |
| `crisis` | Rapid decline, high ATR, vol spike |
| `liquidation_cascade` | Extreme crash conditions |
| `uncertain` | None of the above rules fire conclusively |

Returns `(regime_label: str, confidence: float, features: dict)`.

#### Step 4: Regime Transition Controller (hysteresis)

If `RegimeTransitionController` is available (`core/regime/regime_transition_controller.py`), it applies a dwell-time requirement before accepting a regime change. This prevents rapid regime flipping between scan cycles.

#### Step 5: MS-GARCH Adjustment — `ms_garch.forecast(df)` and `ms_garch.get_regime_adjustment(regime, forecast)`

If `ms_garch.enabled` is `true` in settings, the Markov-Switching GARCH forecaster (`core/regime/ms_garch_forecaster.py`) fits a volatility model on the OHLCV data and forecasts 3-bar-ahead volatility. Its output can override the regime label (e.g., if GARCH flags high expected volatility, `ranging` may be promoted to `volatility_expansion`). If GARCH confidence > 0.7, the classifier's confidence is bumped by 5%, capped at 1.0.

This step is wrapped in `try/except` and is fully advisory — any failure is silently ignored.

#### Step 6: HMM Probabilistic Classification — `HMMRegimeClassifier.classify_combined(df)` (`core/regime/hmm_regime_classifier.py:210`)

This is the most architecturally significant step added in the 2026-03-14 upgrade.

The `HMMRegimeClassifier` wraps a `GaussianHMM` with 6 hidden states (one per primary regime). Its feature matrix is 4-dimensional per bar:

```
X = [log_return, realised_volatility_5bar, adx_normalised, volume_momentum]
```

Where:
- `log_return = ln(close_t / close_{t-1})`, clipped to [-0.15, 0.15]
- `realised_volatility_5bar` = rolling 5-bar std of log returns
- `adx_normalised = adx / 100`, clipped to [0, 1]
- `volume_momentum = volume / rolling_20bar_mean(volume)`, clipped to [0, 5]

Training: `HMMRegimeClassifier.fit(df)` requires ≥200 bars, trains for up to 200 EM iterations with tolerance 1e-4, then runs `_label_states()` which aligns each of the 6 HMM states to a rule-based regime label using majority vote.

`classify_combined(df)` blends the two classifiers:
```
blended_probs[regime] = hmm_probs[regime] * 0.60 + rule_based_probs[regime] * 0.40
```

The HMM `predict_proba()` returns a probability over all 6 states for the last observation. These state probabilities are summed into the 12-regime label space using the learned `state_map`. The rule-based output is converted to a pseudo-probability dict via `_one_hot_probs(label, confidence)`, which sets the assigned label's probability to `confidence` and distributes `(1 - confidence)` uniformly across the remaining 11 regimes.

The blended result is `regime_probs: dict[str, float]` — a continuous probability distribution over all 12 regimes that sums to 1.0. This dict flows downstream through every subsequent step.

If the HMM's `confidence` exceeds the rule-based classifier's `confidence`, the HMM's label and confidence replace the rule-based values as the authoritative regime.

If `HMMRegimeClassifier` is not fitted yet at scan time, `fit(df)` is called inline on the current 300-bar DataFrame (which satisfies the 200-bar minimum). Subsequent scans reuse the fitted model.

#### Step 7: Signal Generation — `SignalGenerator.generate()` (`core/signals/signal_generator.py:69`)

`generate(symbol, df, regime, timeframe, regime_probs=regime_probs)` iterates over 8 sub-model instances (plus optional RL and custom models) and collects `ModelSignal` objects from those that pass the activation gate and return a non-None result.

**Probabilistic activation gate** (when `regime_probs` is available and `adaptive_activation.enabled` is `true`):

```python
activation_wt = model.get_activation_weight(regime_probs)
# = sum(REGIME_AFFINITY[regime] * prob for regime, prob in regime_probs.items())
if activation_wt < settings.get("adaptive_activation.min_activation_weight", 0.10):
    skip model
```

Each model has a `REGIME_AFFINITY` dict mapping regime names to activation weights. The dot product of this dict with `regime_probs` gives a continuous activation weight. A model is skipped only if this weight falls below the minimum threshold (default 0.10). This means that in uncertain markets where probabilities are spread across regimes, most models still fire with reduced weights rather than being completely suppressed.

For example, `TrendModel.REGIME_AFFINITY`:
```python
{"bull_trend": 1.0, "bear_trend": 0.9, "ranging": 0.1,
 "volatility_expansion": 0.6, "volatility_compression": 0.2,
 "uncertain": 0.3, "crisis": 0.0, "liquidation_cascade": 0.0,
 "squeeze": 0.3, "recovery": 0.7, "accumulation": 0.2, "distribution": 0.2}
```

If `regime_probs` is not available (HMM failed), the code falls back to the binary `is_active_in_regime(regime)` check using each model's `ACTIVE_REGIMES` list.

The 8 models in evaluation order:

| Model | Class | Primary regime specialisation |
|---|---|---|
| trend | `TrendModel` | bull_trend, bear_trend |
| mean_reversion | `MeanReversionModel` | ranging, volatility_compression |
| momentum_breakout | `MomentumBreakoutModel` | volatility_expansion, squeeze |
| vwap_reversion | `VWAPReversionModel` | ranging, volatility_compression |
| liquidity_sweep | `LiquiditySweepModel` | ranging, distribution, accumulation |
| funding_rate | `FundingRateModel` | contrarian funding signal; regime-agnostic |
| order_book | `OrderBookModel` | microstructure imbalance; regime-agnostic |
| sentiment | `SentimentModel` | FinBERT/VADER news NLP; regime-weighted |

Each model's `evaluate(symbol, df, regime, timeframe)` method reads indicator columns from `df` using the safe helper `_col(df, column_name)` which returns `None` (not a crash) when the column is missing. If a required indicator is absent or out of range, the model returns `None`.

When a model fires, it returns a `ModelSignal` dataclass:
```python
@dataclass
class ModelSignal:
    symbol:       str
    model_name:   str
    direction:    str   # "long" | "short"
    strength:     float # 0.0–1.0
    entry_price:  float
    stop_loss:    float
    take_profit:  float
    timeframe:    str
    regime:       str
    rationale:    str
    atr_value:    float
```

Stop-loss and take-profit are calculated using ATR with regime-adjusted multipliers from `BaseSubModel.REGIME_ATR_MULTIPLIERS`:

```python
atr_mult = self.get_atr_multiplier(regime)
# e.g., bull_trend=1.5, ranging=2.5, volatility_expansion=3.0, uncertain=2.0

# TrendModel (long):
stop_loss   = close - atr * atr_mult
take_profit = close + atr * (atr_mult + 1.0)
```

Model-internal `strength` is computed from ADX bonus, multi-TF EMA alignment, and MACD confirmation, capped at 1.0.

The Orchestrator signal is a warm-up guard: `SignalGenerator._warmup_complete` is set to `True` before the scanner calls `generate()` (`scanner.py:106`), so the 100-bar warmup guard is intentionally bypassed in scanner context.

#### Step 8: Confluence Scoring — `ConfluenceScorer.score()` (`core/meta_decision/confluence_scorer.py:155`)

Takes the list of `ModelSignal` objects from Step 7 and produces a single `OrderCandidate` or `None`.

**Pre-checks:**
1. **Orchestrator veto**: if `OrchestratorEngine.is_veto_active()` returns True (macro conditions hostile), the entire symbol is suppressed — returns `None` immediately.
2. **Threshold adjustment from orchestrator**: `get_threshold_adjustment()` may raise the effective threshold by up to +0.30.
3. **Per-asset threshold**: `MultiAssetConfig.get_min_confluence_score(symbol)` may impose a higher floor for specific symbols.

**Orchestrator vote injection**: if the OrchestratorEngine has a `meta_signal` > 0.10 with `meta_confidence` ≥ 0.20, a synthetic `ModelSignal` with `model_name="orchestrator"` is appended to the signal list.

**Direction determination**: signals are split by direction. If long count ≥ short count, direction is "buy"; else "sell". Only same-direction signals participate in scoring.

**Adaptive weight computation** (the key change from the architecture upgrade):
```python
def _get_adaptive_weight(model_name, base_weight):
    if not regime_probs:
        return base_weight
    affinity = REGIME_AFFINITY[model_name]  # dict from confluence_scorer.py:32
    activation = sum(affinity.get(r, 0.3) * p for r, p in regime_probs.items())
    activation = clamp(activation, 0.0, 1.0)
    win_rate_adj = _outcome_tracker.get_weight_adjustment(model_name)
    return base_weight * activation * win_rate_adj
```

`win_rate_adj` is 1.0 ± 0.15 based on rolling 30-trade win rate per model. If a model has fewer than 5 recorded trades, `win_rate_adj = 1.0`.

**Base weights** (before adaptation):
```
trend:              0.35
mean_reversion:     0.25
momentum_breakout:  0.25
vwap_reversion:     0.28
liquidity_sweep:    0.15
funding_rate:       0.20
order_book:         0.18
sentiment:          0.12
rl_ensemble:        0.30 (0.0 when rl.enabled=false)
orchestrator:       0.22
```

**Weighted score formula**:
```
total_weight = sum of adaptive_weight(model) for all fired models
weighted_score = sum( (adaptive_weight(model) / total_weight) * model.strength )
                 for all fired models
```

**Dynamic threshold** (applied when `dynamic_confluence.enabled=true`):
```
top_prob = max(regime_probs.values())
if top_prob >= 0.70: regime_conf_factor = 1.05  # more certain → higher bar
elif top_prob <= 0.40: regime_conf_factor = 0.85  # very uncertain → lower bar
else: regime_conf_factor = 1.0 + (top_prob - 0.55) * 0.2  # linear

model_count_factor = max(0.75, sum_active_weights / sum_all_weights)

if "volatility_expansion" or "crisis" in best_regime: vol_factor = 1.15
elif "compression" in best_regime: vol_factor = 0.95
else: vol_factor = 1.0

effective_threshold = clamp(base_threshold * regime_conf_factor * model_count_factor * vol_factor,
                             0.28, 0.65)
```

If `weighted_score < effective_threshold`, returns `None`.

**Price level synthesis**: the primary signal (highest base weight among fired models) provides `entry_price`, `atr_value`, `timeframe`, and `regime`. Stop-loss and take-profit are averaged across all contributing signals.

**Position sizing** via `PositionSizer.calculate()` (see Section 5.9 for full formula).

**Order expiry**: `expiry = utcnow() + timedelta(minutes = TF_MINUTES[timeframe] * 5)`. A 1h signal expires in 5 hours if not filled.

Returns `OrderCandidate` (see dataclass in `core/meta_decision/order_candidate.py:36`).

#### Step 9: Multi-Timeframe Confirmation (optional)

If `multi_tf.confirmation_required` is `true` in settings (disabled by default):

The scanner fetches 50 bars on the next-higher timeframe (mapped: 1h → 4h, 4h → 1d, etc.), runs `calculate_all()` on them, classifies regime with the rule-based classifier, and stores the result in `candidate.higher_tf_regime`.

The RiskGate then checks for directional conflict:
- A "buy" candidate with `higher_tf_regime` containing "bear" → rejected
- A "sell" candidate with `higher_tf_regime` containing "bull" → rejected

#### Step 10: Risk Gate (batch) — `RiskGate.validate_batch()` (`core/risk/risk_gate.py:251`)

All candidates from all symbols are sorted **descending by score** and validated one-by-one. Higher-quality signals consume capacity first. The validator maintains a `simulated_positions` list that grows as candidates are approved, so later candidates in the batch see an accurate position count.

Each `validate(candidate, open_positions, capital, drawdown, spread)` runs these checks in order:

1. **Expiry**: if `candidate.expiry < utcnow()`, reject.
2. **Already in position**: if `candidate.symbol` is in `open_symbols`, reject.
3. **Correlation cap** (`CorrelationController`): if the new symbol has correlation > 75% with any open position symbol, reject.
4. **Directional exposure** (`CorrelationController`): if adding this position would give > X% of capital on one side, reject.
5. **Max concurrent positions**: if `len(open_positions) >= max_concurrent_positions` (default 3), reject.
6. **Portfolio drawdown**: if `drawdown_pct >= max_portfolio_drawdown_pct` (default 15%), reject.
7. **Capital available**: if `available_capital <= 0`, reject.
8. **Capital allocation cap**: if `position_size > capital * max_position_capital_pct` (default 25%), reduce size (not reject).
9. **Max capital USDT gate**: if `max_capital_usdt > 0` and `position_size > max_capital_usdt`, cap size.
10. **Portfolio heat**:
    ```
    existing_heat = sum(pos.position_size_usdt * pos.stop_distance_pct for pos in open_positions)
    new_heat = candidate.position_size * abs(entry - stop) / entry
    if (existing_heat + new_heat) / capital > 0.06: reject
    ```
11. **R:R floor**: if `risk_reward_ratio < 1.0`, reject.
12. **EV gate**:
    ```
    win_prob = 1 / (1 + exp(-8 * (score - 0.55)))
    if regime == "uncertain": win_prob *= 0.85
    reward = abs(take_profit - entry)
    risk   = abs(entry - stop_loss)
    EV = win_prob * reward - (1 - win_prob) * risk
    EV_normalised = EV / risk
    if EV_normalised < 0.05: reject
    ```
    Stores `candidate.expected_value = EV_normalised`.
13. **MTF conflict**: if `multi_tf.confirmation_required` and `higher_tf_regime` contradicts `side`, reject.

If all checks pass: `candidate.approved = True`. The candidate is written to the `SignalLog` database table via `_persist_signal_log()`.

#### Step 11: CrashDetector Update — `CrashDetector.evaluate()` (`core/risk/crash_detector.py:81`)

After the batch risk check, the scanner runs a second OHLCV fetch for all qualifying symbols (reusing the same 300-bar limit), reconstructs their DataFrames, and calls `get_crash_detector().evaluate(tickers, df_by_symbol)`.

The crash detector computes 7 component scores, applies configurable weights, and produces a composite score 0–10:
```
composite = (sum(weight[i] * component_score[i]) / total_weight) * 10.0
```

| Component | Weight | Trigger logic |
|---|---|---|
| ATR spike | 2.0 | current_atr / 20bar_mean > 1.5 → `(ratio - 1.0) / 1.5` |
| Price velocity | 1.8 | 3-bar return z-score < -1.5 → `(-z - 1.5) / 2.5` |
| Liquidation cascade | 1.5 | from `LiquidationFlowAgent.run()` → `severity / 100` |
| Cross-asset decline | 1.5 | all symbols declining same bar → `(declining/n) * (mean_decline / 3%)` |
| Order book imbalance | 1.2 | bid/ask volume ratio extreme → `(ratio - 0.5) / 0.15` |
| Funding rate flip | 1.0 | funding < -0.0001 → `(-funding - 0.0001) / 0.0008` |
| OI collapse | 1.0 | from `OnChainAgent.run()` → `abs(oi_change) / 100` if > 20% decline |

Tier transitions (immediate entry, hysteresis on exit):
- `normal`: score < 5.0
- `defensive`: score ≥ 5.0
- `high_alert`: score ≥ 7.0
- `emergency`: score ≥ 8.0
- `systemic`: score ≥ 9.0

Recovery to `normal` requires 5 consecutive evaluations below `5.0 - 1.5 = 3.5`.

When crash mode is active (`is_crash_mode = tier != normal`), the `PositionSizer` applies `defensive_mode_multiplier = 0.25` to all long-side positions.

#### Step 12: Regime Broadcast

The scanner tallies regime votes across all symbols (each symbol casts one vote for the regime it was classified as), determines the dominant regime, and publishes `Topics.REGIME_CHANGED` to the event bus with `{new_regime, confidence, regime_probs, symbol_count}`.

#### Step 13: Emit Results

`ScanWorker.scan_complete.emit([c.to_dict() for c in approved])` fires. `AssetScanner._on_scan_complete()` receives the list, emits `candidates_ready`, and publishes `Topics.SIGNAL_CONFIRMED` to the event bus.

The IDSS page UI receives the dicts and updates the candidate table.

---

## 3. Multiple Timeframe Logic

The primary timeframe is set globally for the scanner and determines the candle granularity for all OHLCV fetches, all indicator calculations, and all model evaluations. It also sets the QTimer interval.

**Multi-TF confirmation** (`multi_tf.confirmation_required`, default `false`) is the only place where a second timeframe is explicitly consulted. When enabled, a 50-bar fetch on the next-higher timeframe is performed for each candidate that makes it past confluence scoring. The relationship is:

```
primary → higher
1m  → 5m
5m  → 15m
15m → 1h
1h  → 4h
4h  → 1d
1d  → 1w
```

No indicator calculation is performed on the higher-TF data beyond what `calculate_all()` returns. Regime is classified on the higher-TF bars using only the rule-based classifier (not HMM, to keep latency low). The result is stored in `candidate.higher_tf_regime` and evaluated by the RiskGate's MTF conflict check.

There is no multi-TF signal generation — the sub-models only see the primary timeframe DataFrame.

The HMM's `classify_combined()` blends HMM probabilities with rule-based probabilities from the same primary-timeframe DataFrame. It does not independently fetch a second timeframe.

---

## 4. Per-Pair Computation

The scanner processes each symbol independently and sequentially within the same `ScanWorker` thread. There is no shared indicator state between symbols. Each symbol gets its own:

- Fresh OHLCV DataFrame
- Fresh indicator calculation (`calculate_all()`)
- Fresh regime classification (HMM re-uses the same fitted model but classifies on the new DataFrame)
- Fresh signal generation
- Fresh confluence scoring

**Important**: the HMM model is **one model per ScanWorker instance**. It is fitted on the first symbol's DataFrame that provides ≥200 bars, then reused for all subsequent symbols in the same scan cycle. This means the HMM's state-to-regime mapping reflects the first symbol's history, not a per-symbol trained model. This is a deliberate simplification for startup latency; a production upgrade would maintain per-symbol HMM instances.

Position size computation is consistent: the `PositionSizer` object is shared across all symbols within one scan cycle, but its `calculate()` method is stateless except for loss streak counters which are updated by the execution layer, not the scanner.

---

## 5. Column-by-Column Explanation

This section documents every column shown in the IDSS candidate table.

### 5.1 Side

**What it shows**: `"Buy"` or `"Sell"`.

**How computed**: In `ConfluenceScorer.score()`, after all fired signals are collected:
```python
long_signals  = [s for s in signals if s.direction == "long"]
short_signals = [s for s in signals if s.direction == "short"]
if len(long_signals) >= len(short_signals):
    side = "buy"
else:
    side = "sell"
```
Only signals in the winning direction contribute to the final score. Mixed-direction signal sets are not rejected — the minority direction is discarded.

**Why "-"**: Displayed as "-" if no approved candidate exists for that row (candidate was rejected or no signals fired).

### 5.2 Regime

**What it shows**: The market regime label for the symbol at the time of the last scan, e.g., `"bull_trend"`, `"ranging"`, `"uncertain"`.

**How computed**: The final `regime` is the output of the full classification pipeline:
1. `EnsembleRegimeClassifier` or `RegimeClassifier` produces the initial label
2. `RegimeTransitionController` may hold the previous label due to hysteresis
3. MS-GARCH may override the label
4. HMM's `classify_combined()` may override the label if `hmm_conf > rule_based_conf`

The final label is from step 4. It is set by the scanner on `ModelSignal` and then copied to `OrderCandidate.regime`.

**Why "Uncertain"**: See Section 8.

### 5.3 Score

**What it shows**: The confluence score from `ConfluenceScorer`, as a decimal (e.g., `0.63`). This is `OrderCandidate.score`.

**How computed**:
```
score = sum((adaptive_weight(model) / total_adaptive_weight) * model.strength)
        for all fired, same-direction models
```

Where `adaptive_weight(model) = base_weight * regime_affinity_dot_product * win_rate_adj`.

Score is clamped to [0, 1] implicitly by the strength values (which are capped at 1.0 by each model).

Only candidates with `score > effective_threshold` (dynamic, nominally 0.45–0.65) appear as approved candidates in the table.

**Why "-"**: Score is "-" when the row represents a rejected candidate. Rejected candidates are not shown by default; only approved candidates appear in the table.

### 5.4 Models

**What it shows**: A comma-separated list of sub-model names whose signals contributed to the final candidate, e.g., `"trend, momentum_breakout"`.

**How computed**: `OrderCandidate.models_fired = [s.model_name for s in active_signals]`.

`active_signals` is the list of same-direction `ModelSignal` objects that passed the activation gate and returned a non-None result. Signals from the minority direction are excluded.

### 5.5 Entry

**What it shows**: The suggested limit entry price for the trade, in USDT. E.g., `87,523.00`.

**How computed**: `entry_price = primary.entry_price` where `primary` is the fired model with the highest **base** weight (not adaptive weight):
```python
primary = max(active_signals, key=lambda s: weights.get(s.model_name, 0.1))
```

Each model sets `entry_price = float(df["close"].iloc[-1])` — the last closed candle's close price — in its `evaluate()` method. So the entry price is always the close of the most recent candle at scan time. There is no lookahead; it is not a projected future price.

**Why "-"**: Entry is "-" if no approved candidate exists for the row.

### 5.6 Stop

**What it shows**: The absolute stop-loss price. E.g., `85,341.00`.

**How computed**: Each model calculates:
```python
atr_mult = BaseSubModel.REGIME_ATR_MULTIPLIERS.get(regime, 2.0)
# TrendModel long:
stop_loss = close - atr * atr_mult
# TrendModel short:
stop_loss = close + atr * atr_mult
```

The `ConfluenceScorer` then **averages** stop prices across all contributing signals:
```python
stop_prices = [s.stop_loss for s in active_signals]
stop_loss_price = sum(stop_prices) / len(stop_prices)
```

The RiskGate may then modify the position size based on the stop distance but does not move the stop price.

### 5.7 Target

**What it shows**: The absolute take-profit price. E.g., `91,200.00`.

**How computed**: Same averaging logic as Stop:
```python
# TrendModel long:
take_profit = close + atr * (atr_mult + 1.0)  # reward > risk
# TrendModel short:
take_profit = close - atr * (atr_mult + 1.0)
target_prices = [s.take_profit for s in active_signals]
take_profit_price = sum(target_prices) / len(target_prices)
```

### 5.8 R:R

**What it shows**: The risk-to-reward ratio, computed as `reward / risk`. E.g., `1.67`.

**How computed**: In `OrderCandidate.__post_init__()`:
```python
risk   = abs(entry_price - stop_loss_price)
reward = abs(take_profit_price - entry_price)
risk_reward_ratio = round(reward / risk, 2) if risk > 0 else 0.0
```

RiskGate applies a hard floor of `min_rr_floor = 1.0` — any candidate with R:R < 1.0 is rejected outright before the EV gate.

### 5.9 Size

**What it shows**: The suggested position size in USDT. E.g., `42.30`.

**How computed**: `PositionSizer.calculate()` (`core/meta_decision/position_sizer.py:94`):

```
base_kelly = kelly_fraction * available_capital_usdt
           = 0.25 * capital

current_atr_pct = atr_value / entry_price
vol_scalar = target_atr_pct / current_atr_pct   (clamped [0.2, 3.0])
           = 0.008 / current_atr_pct            (target_atr_pct default 0.008 = 0.8%)

regime_mult = REGIME_RISK_MULTIPLIERS[regime]
           # bull_trend=1.0, bear_trend=0.7, crisis=0.0 (halt), uncertain=0.4, etc.

score_mult:
  score < 0.60 → 0.75
  score < 0.70 → 0.85
  score < 0.80 → 1.00
  score < 0.90 → 1.15
  else         → 1.30

drawdown_scalar (linear interpolation):
  dd = 0%  → 1.0
  dd = 5%  → 0.8
  dd = 10% → 0.6
  dd >= 15% → 0.0 (halt)

loss_streak_scalar:
  if consecutive_losses >= 3: 0.50
  else: 1.0

defensive_scalar:
  if crash_mode and side=="long": 0.25
  else: 1.0

size = base_kelly * vol_scalar * regime_mult * score_mult * drawdown_scalar
       * loss_streak_scalar * defensive_scalar

# Capital bounds:
cap_min = capital * 0.003   (0.3%)
cap_max = capital * 0.04    (4%)
size = clamp(size, cap_min, cap_max)

# Absolute limits:
size = min(size, max_size_usdt)  if max_size_usdt > 0
size = max(size, min_size_usdt)  (floor: 10.0 USDT)
```

Note: in `ConfluenceScorer`, `available_capital_usdt` is passed as `self._base_size * 25 = 40 * 25 = 1000 USDT` as a proxy. The real position size is recalculated with actual capital by the execution layer. The size shown in the IDSS table is therefore an **estimate**, not the final filled size.

### 5.10 Age

**What it shows**: Time elapsed since the candidate was generated, in minutes or hours.

**How computed**: `generated_at = datetime.utcnow()` in `OrderCandidate.__post_init__()`. The UI computes `utcnow() - generated_at` for display.

Candidates have an expiry: `expiry = utcnow() + timedelta(minutes = TF_MINUTES[timeframe] * 5)`. An unexpired candidate from a previous scan cycle remains in the table until either it is traded, manually dismissed, or its age exceeds 5 candle-lengths. At that point it is stale and should be disregarded even if still visible.

---

## 6. Data Sources

| Data | Source | API call | Notes |
|---|---|---|---|
| OHLCV candles (primary TF) | Bybit via ccxt | `fetch_ohlcv(symbol, tf, limit=300)` | REST polling; WS disabled (`data.websocket_enabled: false`) |
| OHLCV candles (higher TF, MTF only) | Bybit via ccxt | `fetch_ohlcv(symbol, higher_tf, limit=50)` | Only when `multi_tf.confirmation_required: true` |
| Bid/ask/volume tickers | Bybit via ccxt | `fetch_tickers(symbols)` | Batch call at scan start |
| Funding rates | Bybit via ccxt | Embedded in ticker `fundingRate` field | Used by FundingRateModel and CrashDetector |
| Order book L2 depth | Bybit via ccxt | `fetch_order_book(symbol)` in OrderBookModel | Per-model, on-demand |
| Liquidation data | CoinglassAPI | `LiquidationFlowAgent.run()` | Requires `coinglass_api_key` in vault |
| Open interest | CoinglassAPI or OnChainAgent | `OnChainAgent.run()` | Used by CrashDetector OI collapse component |
| News sentiment | CryptoPanic API + FinBERT | `SentimentModel` + `FinBERTPipeline` | Requires `cryptopanic_api_key`; FinBERT runs on GPU |
| Social sentiment | Reddit API | `RedditSentimentAgent` | Used by OrchestratorEngine; requires Reddit API credentials |
| Historical data for HMM training | Bybit via ccxt | Reuses the 300-bar OHLCV fetch | No separate API call |

All API keys are stored encrypted in the NexusTrader credential vault, not in plaintext in config files.

---

## 7. Why Values Show as "-"

The IDSS table shows "-" in most columns under two conditions:

**Condition A: No signal fired for the symbol.** This is by far the most common case. A symbol shows "-" in Score, Models, Entry, Stop, Target, R:R, and Size when either:
- No sub-model returned a non-None `ModelSignal` (indicator conditions were not met)
- The `ConfluenceScorer` returned `None` (score below effective threshold)
- The candidate was rejected by `RiskGate`

Even if the regime and side are populated, the quantitative columns will show "-" if the candidate did not make it through the full pipeline.

**Condition B: Indicator data was insufficient.**
- The OHLCV fetch returned fewer than 30 bars → entire symbol returns `(None, "", 0.0)` early
- The DataFrame had enough rows (≥30) but specific indicator columns are NaN → `_col(df, name)` returns `None` → the model's required-indicator check fails → model returns `None`

The most common reason for NaN indicators is insufficient warmup bars. EMA100 requires at least 100 candles. If the exchange returns 300 bars but 280 of them are needed for warmup, only 20 bars of valid EMA100 values exist. The last bar's EMA100 may still be valid, but early oscillators like ADX (requires ~27 bars) must fill in first.

**Condition C: Symbol filtered by UniverseFilter.** If a symbol fails the volume or spread filter, it is removed from `qualifying` before any OHLCV fetch. It will not appear in the table at all.

---

## 8. Why Regime Shows "Uncertain"

"Uncertain" is both an explicit regime label and the most common output on startup and in ambiguous markets. There are several mechanisms that produce it:

**Mechanism 1: Insufficient warmup bars.** The rule-based `RegimeClassifier` examines patterns across the last ~50 bars. If the DataFrame has fewer than 50 bars, most heuristic rules do not fire, and the classifier returns `uncertain` as the catch-all.

**Mechanism 2: No rule fires conclusively.** The `RegimeClassifier` applies an ordered decision tree. If the price is neither strongly trending (ADX < 25), nor clearly ranging (BB metrics indeterminate), nor in a volatility spike, the final branch returns `uncertain`.

**Mechanism 3: HMM not yet fitted.** On the first scan cycle, `HMMRegimeClassifier.is_fitted` is `False`. `classify_combined()` falls back to pure rule-based output. If the rule-based output is `uncertain`, that propagates.

**Mechanism 4: HMM blended probabilities are spread.** After the HMM is fitted, if the 4-feature observation does not align clearly with any of the 6 trained states, `predict_proba()` may return a flat distribution (e.g., 20% each across 5 regimes). The best regime from blending still wins, but the confidence will be low. The uncertainty penalty in `classify()` triggers when the top two probabilities differ by less than 0.15:
```python
if sorted_probs[0] - sorted_probs[1] < 0.15:
    confidence *= 0.75
```
This further reduces confidence but does not change the label to "uncertain" — that only happens if "uncertain" was genuinely the highest-probability regime.

**Mechanism 5: `RegimeTransitionController` is holding a stale label.** The transition controller requires consecutive bars in the new regime before confirming a change. During a transition, it may return the old label, which could be `uncertain` if the previous state was `uncertain`.

**Practical impact of "Uncertain":**
- `TrendModel.REGIME_AFFINITY["uncertain"] = 0.30` — trend model fires at ~30% activation weight
- `PositionSizer.REGIME_RISK_MULTIPLIERS["uncertain"] = 0.4` — 40% position size reduction
- `RiskGate` EV gate applies `win_prob *= 0.85` — 15% probability penalty
- `ConfluenceScorer` dynamic threshold: if `top_prob ≤ 0.40`, `regime_conf_factor = 0.85` → threshold drops slightly, making it slightly *easier* to pass when regime is very uncertain

This means `uncertain` produces smaller positions, harder EV gates, but does not suppress trading entirely — a deliberate design choice to maintain signal flow in uncertain markets.

---

## 9. Current Limitations and Gaps

**L1: Single HMM instance shared across symbols.**
The `HMMRegimeClassifier` in `ScanWorker` is trained once (on the first 300-bar DataFrame it encounters) and reused for all symbols in the same session. Its state-to-regime mapping reflects one symbol's market history. BTC/USDT and ETH/USDT may have very different regime patterns. A per-symbol HMM would be more accurate but significantly more expensive.

**L2: CrashDetector OHLCV double-fetch.**
In `ScanWorker.run()` (line 188), the CrashDetector update fetches OHLCV for all qualifying symbols a second time after the scan loop has already fetched them once per symbol. This doubles the API calls at scan time. The DataFrames computed in the main scan loop are not cached for reuse by the CrashDetector.

**L3: Liquidation cascade and OI collapse components return 0 when agents are unavailable.**
`_compute_liquidation_cascade()` and `_compute_oi_collapse()` in `CrashDetector` call `LiquidationFlowAgent` and `OnChainAgent` synchronously inside a try/except. If the Coinglass API key is expired or rate-limited, these components silently return 0.0. The crash detector will underestimate risk when these data sources fail.

**L4: PositionSizer proxy capital in ConfluenceScorer.**
`ConfluenceScorer` passes `self._base_size * 25 = 1000 USDT` as proxy capital to `PositionSizer.calculate()`. The actual capital from the executor is not available at scoring time (only at risk gate time). The displayed `Size` column is therefore always a fixed approximation, not a live capital-aware computation.

**L5: Model signal strength is not calibrated to actual win rates during backtests.**
Model `strength` values (e.g., 0.65, 0.80) are computed from indicator alignment heuristics, not from historically observed win rates. They represent the model's internal conviction, not a calibrated probability. The `TradeOutcomeTracker` adjusts model weights over live trades (after ≥5 trades), but on startup all win-rate adjustments are 1.0 (neutral).

**L6: MTF disabled by default.**
`multi_tf.confirmation_required: false`. When disabled, there is no check that a 1h "buy" signal agrees with the 4h trend. A signal can fire against the higher-timeframe trend. Enabling MTF confirmation would reduce false positives at the cost of signal frequency.

**L7: RL ensemble disabled by default.**
`rl.enabled: false`. The RL ensemble (`rl_ensemble` model weight 0.30) contributes zero to all scores until enabled and trained. The system therefore operates at reduced model diversity on startup.

**L8: Regime hysteresis may cause lag.**
`RegimeTransitionController` requires consecutive bars before accepting a regime change. In fast-moving markets, this can cause the scanner to apply the wrong regime's multipliers and affinity weights for 1–3 scan cycles after a true regime transition occurs.

**L9: No intra-candle updates.**
The scanner fires on closed candles. A stop-loss breach that occurs mid-candle is handled by the execution layer's price monitoring loop, not IDSS. The IDSS table does not refresh intra-candle.

---

## 10. Audit-Ready Mapping Table

| IDSS Column | Source Attribute | Computed in | Formula / Logic |
|---|---|---|---|
| Side | `OrderCandidate.side` | `ConfluenceScorer.score()` | majority direction of fired model signals |
| Regime | `OrderCandidate.regime` / `ModelSignal.regime` | `ScanWorker._scan_symbol_with_regime()` | HMM-blended > rule-based > GARCH-adjusted; copied from primary signal |
| Score | `OrderCandidate.score` | `ConfluenceScorer.score()` | weighted mean of model strengths; weights = base × regime_affinity_dot × win_rate_adj |
| Models | `OrderCandidate.models_fired` | `ConfluenceScorer.score()` | names of same-direction ModelSignal objects |
| Entry | `OrderCandidate.entry_price` | `ConfluenceScorer.score()` | `df["close"].iloc[-1]` from primary signal (highest base weight model) |
| Stop | `OrderCandidate.stop_loss_price` | `ConfluenceScorer.score()` | mean of `model.stop_loss` across fired signals |
| Target | `OrderCandidate.take_profit_price` | `ConfluenceScorer.score()` | mean of `model.take_profit` across fired signals |
| R:R | `OrderCandidate.risk_reward_ratio` | `OrderCandidate.__post_init__()` | `abs(take_profit - entry) / abs(entry - stop)` |
| Size | `OrderCandidate.position_size_usdt` | `PositionSizer.calculate()` (called from `ConfluenceScorer.score()`) | quarter-Kelly × vol_scalar × regime_mult × score_mult × drawdown_scalar × streak_scalar × defensive_scalar |
| Age | computed in UI | UI layer | `utcnow() - OrderCandidate.generated_at` |

| Pipeline Stage | Class | File | Key config key(s) |
|---|---|---|---|
| Timer trigger | `AssetScanner` | `core/scanning/scanner.py` | `scanner.timeframe` |
| Symbol list | `WatchlistManager` | `core/scanning/watchlist.py` | `scanner.watchlists` |
| Universe filter | `UniverseFilter` | `core/scanning/universe_filter.py` | `scanner.min_volume_usdt` |
| OHLCV fetch | ccxt exchange | — | `scanner.ohlcv_bars` (default 300) |
| Indicators | `calculate_all()` | `core/features/indicator_library.py` | — |
| Rule-based regime | `RegimeClassifier` | `core/regime/regime_classifier.py` | `regime_classifier.adx_threshold` |
| GARCH regime adj. | `ms_garch` | `core/regime/ms_garch_forecaster.py` | `ms_garch.enabled` |
| HMM probabilistic regime | `HMMRegimeClassifier` | `core/regime/hmm_regime_classifier.py` | `hmm_regime.enabled` |
| Signal generation | `SignalGenerator` | `core/signals/signal_generator.py` | `adaptive_activation.enabled`, `adaptive_activation.min_activation_weight` |
| Confluence scoring | `ConfluenceScorer` | `core/meta_decision/confluence_scorer.py` | `idss.min_confluence_score`, `dynamic_confluence.*` |
| Position sizing | `PositionSizer` | `core/meta_decision/position_sizer.py` | `position_sizer.*` (kelly_fraction, max_capital_pct, etc.) |
| Risk gate | `RiskGate` | `core/risk/risk_gate.py` | `risk.*`, `expected_value.*`, `risk_engine.*`, `multi_tf.*` |
| Crash detector | `CrashDetector` | `core/risk/crash_detector.py` | `crash_detector.*` |
| MTF confirmation | inline in `ScanWorker` | `core/scanning/scanner.py` | `multi_tf.confirmation_required` |
| Persistence | `RiskGate._persist_signal_log()` | `core/risk/risk_gate.py` | — |
| Trade outcome tracking | `TradeOutcomeTracker` | `core/meta_decision/confluence_scorer.py` | — |

---

*End of IDSS Technical Walkthrough — NexusTrader 2026-03-14*
