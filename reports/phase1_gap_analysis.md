# NexusTrader — Phase 1: Professional System Gap Analysis
**Date:** 2026-03-26
**Version assessed:** v1.1
**Analyst:** Senior Quantitative Trading Architect
**Purpose:** Establish baseline gaps vs. professional-grade profitable trading systems across 10 dimensions. Every finding is sourced from actual code, not assumptions.

---

## Scoring Legend
| Rating | Meaning |
|--------|---------|
| ✅ STRONG | Meets or exceeds professional standard |
| ⚠️ GAP | Present but materially incomplete or misaligned |
| ❌ MISSING | Not implemented; represents real risk or missed alpha |

---

## Dimension 1: Research Fidelity

**Professional standard:** All strategy parameters are derived from statistically valid backtests on real OHLCV data with out-of-sample validation. No synthetic data, no parameter tuning on in-sample data, no unreported survivorship bias.

### Findings

**What exists:**
- Study 4 (13-month backtest, March 2024–March 2025, 1,870 trades) produced per-model baselines: TrendModel WR 50.3%/PF 1.47, MomentumBreakout WR 63.5%/PF 4.17.
- `live_vs_backtest.py` tracks rolling deviations from Study 4 baselines after every closed trade.
- Walk-forward results stored in `reports/walk_forward/`.
- RAG thresholds defined per-model (TrendModel GREEN ≥47.8% WR, MomentumBreakout GREEN ≥60.3% WR).

**Gaps identified:**

1. **Study 4 is a single in-sample backtest, not a walk-forward study.** The comment in `live_vs_backtest.py` reads _"13-month synthetic backtest"_. There is no evidence of out-of-sample hold-out periods or k-fold regime validation. A single period backtest cannot distinguish signal from overfit noise.

2. **Backtest data provenance is unclear.** The `data/` directory contains parquet files but no manifest documenting: exchange source, OHLCV version, whether adjusted for splits/delistings, or gap-fill methodology. The backtester in `backtest_engine.py` is a rule-evaluator (condition trees) — not the same signal pipeline (ConfluenceScorer → RiskGate → PositionSizer) used in live scanning. **Backtest and live use different execution paths** — this is a structural gap. A trade that fires in backtest may not fire identically in live because the live path includes regime affinity weighting, orchestrator veto, OI modifiers, and EV gating that are absent from `backtest_engine.py`.

3. **No parameter sensitivity analysis.** ADX minimum of 25, RSI bounds of [45, 70], ATR stop multiplier of 1.5 — these are hardcoded in sub-model files (`trend_model.py` line 60–68 reads from settings, but no grid search or sensitivity curve documented). A ±10% parameter perturbation test is absent.

4. **Sub-models without individual backtest baselines:** `FundingRateModel`, `OrderBookModel`, `SentimentModel`, `OrchestratorEngine` have no Study 4 entries. Their contributions to live signals are unvalidated.

5. **Regime-stratified performance not available.** Study 4 reports only aggregate WR/PF. No breakdown by regime label (bull_trend vs ranging vs crisis). If TrendModel's PF 1.47 is entirely driven by bull_trend periods, it has no edge in ranging markets but the confluence scorer still activates it at 10% affinity — creating negative-EV trades.

**Rating: ⚠️ GAP** — Study 4 exists but is a single-pass backtest on a different code path with no OOS validation or regime stratification.

---

## Dimension 2: Signal Quality

**Professional standard:** Signals carry measurable alpha (positive expectancy net of costs), are non-redundant, have documented fire rates, and do not rely on indicators that are causally irrelevant to the predicted market move.

### Findings

**What exists:**
- 8 signal sub-models: TrendModel, MeanReversionModel (disabled), MomentumBreakoutModel, VWAPReversionModel (disabled after Study 4 PF 0.28), LiquiditySweepModel (disabled), FundingRateModel, OrderBookModel, SentimentModel.
- 2 active models with validated baselines (TrendModel, MomentumBreakout).
- Correlation dampening via `correlation_dampener.py` — fires per scan cycle to penalize correlated models.
- Direction dominance check (≥30% dominance required) prevents split-signal garbage candidates.

**Gaps identified:**

1. **TrendModel uses EMA9/EMA21 crossover as primary entry gate.** On 1h candles with 300 bars, this introduces 2–4 bar lag. In trending crypto markets, lag ≥2 bars at 1h timeframe = 2+ hours of missed move. The resulting entry at `close + 0.20×ATR` compounds this by entering at the peak of a micro-impulse.

2. **Signal strength is additive, not multiplicative, in ConfluenceScorer.** The weighted score formula sums `weight × strength / total_weight`. When only one model fires (e.g., TrendModel alone), the score is just `strength × 1.0`. This means the confluence threshold is not a genuine confluence requirement — a single model firing at high strength (e.g., 0.70) with threshold 0.55 passes. This is signal corroboration theater: the framework *looks* like it requires agreement but single-model passes are common.

3. **OrderBookModel has a structural zero-fire problem.** CLAUDE.md states: _"OrderBook TF gate: Never fires at 1h+ because `min_confidence/tf_weight = 0.60/0.55 = 1.09 > 1.0`. This is structural, not a bug."_ This means at 1h — the primary scanner timeframe — the OrderBookModel contributes nothing. It occupies weight=0.18 in the MODEL_WEIGHTS dict, drawing the total weight pool and diluting other models, while generating no output. This is dead weight.

4. **SentimentModel (FinBERT/VADER) fires on NLP news sentiment with an 8-hour max_age.** On 1h candles, an 8-hour-old news event has been priced in. Sentiment is a low-frequency signal used at a high-frequency timeframe. No evidence that SentimentModel's individual contribution improves signal quality vs. noise.

5. **FundingRateModel fires contrarian signals based on extreme funding rates.** This is a legitimate signal with documented academic backing. However, funding rate resets occur every 8 hours — meaning the signal is stale for up to 7:59 between resets. No staleness adjustment is evident in the model.

6. **No IC (Information Coefficient) measurement per signal.** IC = correlation between signal strength and forward return over the trade holding period. This is the primary quality metric for institutional signals. NexusTrader has no IC computation.

**Rating: ⚠️ GAP** — Signal framework is well-structured but contains dead weight (OrderBook at 1h), lag-prone entry logic, and no IC measurement.

---

## Dimension 3: Regime Design

**Professional standard:** Regime classifier achieves >70% accuracy on out-of-sample data, regime transitions are smooth, and regime-conditional signal performance is tracked separately.

### Findings

**What exists:**
- HMM (6-state GaussianHMM with diag covariance) blended with rule-based classifier.
- 12 regime labels: bull_trend, bear_trend, ranging, volatility_expansion, volatility_compression, uncertain, crisis, liquidation_cascade, squeeze, recovery, accumulation, distribution.
- Adaptive blend: when uncertain_frac > 50%, weight shifts to rule-based (hmm_w=0.20, rb_w=0.80).
- Session 33 fixes: ADX dead zone corrected, EMA slope None handling, hysteresis initialization corrected.
- 300 bars of 1h data → ~12.5 days lookback for HMM training per symbol.

**Gaps identified:**

1. **300-bar 1h lookback = 12.5 days for HMM training.** Professional regime classifiers train on 1–2 years of data. 12.5 days of crypto data is insufficient to distinguish regime from noise — especially since crypto can have a 2-week bull run that maps to "bull_trend" without the HMM having seen a single bear_trend period in its training window.

2. **HMM is re-fit every scan cycle** (or per-startup, unclear from code). Online refitting on 300 bars means the HMM has no stable state mapping across sessions — the state→regime label mapping is re-learned empirically each time. A regime label "bull_trend" in session N may correspond to a different HMM state than "bull_trend" in session N+1.

3. **12 regime labels is too many for 300-bar windows.** With 6 HMM states and 12 possible output labels, several labels (accumulation, distribution, squeeze, recovery) will have near-zero occurrence in any 12-day window. Their appearance introduces false confidence in rare regime identification.

4. **No regime accuracy metric tracked live.** There is no ground-truth label to measure against, but forward-return-stratified analysis (mean return by regime label over subsequent N bars) is not computed. The system doesn't know if "bull_trend" label actually precedes positive returns.

5. **MTF confirmation is present** (`multi_tf.confirmation_required: true`) but the higher-TF regime is only checked for directional conflict, not for regime quality. A 4h "uncertain" regime with a 1h "bull_trend" signal is allowed through as long as there's no directional conflict.

**Rating: ⚠️ GAP** — Architecture is sophisticated but lookback is critically short, re-fitting introduces state mapping instability, and regime accuracy is unmeasured.

---

## Dimension 4: Risk Management

**Professional standard:** Portfolio-level risk is measured continuously, position sizing is calibrated, drawdown protection is multi-layer, and all risk parameters are stress-tested.

### Findings

**What exists:**
- Risk-based position sizing: `size = (risk_pct% × capital) / stop_distance × entry_price`.
- 0.5% risk per trade, 4% max capital per position.
- CrashDefenseController: 7-component scorer, 4-tier multiplier (NORMAL/DEFENSIVE/HIGH_ALERT/EMERGENCY/SYSTEMIC).
- Portfolio heat check: max 6% concurrent stop-fire exposure.
- 10% drawdown circuit breaker in PaperExecutor.
- Slippage-adjusted EV gate: `ev = win_prob × (reward - slippage) - (1-win_prob) × (risk + slippage)`.
- Correlation controller and directional exposure check in RiskGate.
- Max 5 concurrent positions default.

**Gaps identified:**

1. **Drawdown circuit breaker fires at 10% but the position-level cap is 4% and risk per trade is 0.5%.** Even with maximum consecutive losses, reaching 10% drawdown requires ~20 losing trades in a row (5R each). At 5 max positions × 0.5% risk each = 2.5% max simultaneous risk exposure. The 10% circuit breaker is too loose relative to the actual risk being taken — it will almost never fire under normal market conditions. A tighter adaptive circuit breaker at 3–5% drawdown would be more appropriate.

2. **Portfolio heat formula uses `stop_distance_pct = 0.02` as default for positions that lack this field.** The default 2% stop assumption applies to all restored positions from JSON. If actual stops are tighter (e.g., 0.5% ATR-based), the heat calculation overestimates exposure and may block valid trades. If stops are wider, it underestimates. The 0.02 fallback is not documented or validated.

3. **No daily loss limit.** A kill switch for intra-day drawdown (e.g., stop trading after losing 2% in a single day) is absent. The circuit breaker only fires on total equity drawdown, not session drawdown.

4. **No maximum consecutive loss throttle per symbol.** If BTC loses 5 trades in a row, the risk gate does not reduce BTC position sizes unless the global drawdown circuit fires. Per-symbol loss streaks are untracked.

5. **EV gate uses confluence score as win probability via sigmoid.** The sigmoid maps score=0.55 → ~50% win probability (sigmoid midpoint = 0.55, k=8). But at the threshold, every trade that passes appears to have ~50% win probability regardless of regime, signal quality, or historical accuracy. Until ProbabilityCalibrator is trained (requires ≥300 trades), the EV gate is mathematically decorative — it approves everything above the threshold because the sigmoid-derived win probability always produces positive EV when R:R ≥ 1.3.

**Rating: ⚠️ GAP** — Risk framework is architecturally sound but circuit breaker calibration is too loose, no daily loss limit, and EV gate is decorative pre-300 trades.

---

## Dimension 5: Execution Quality

**Professional standard:** Fills are realistic (accounting for spread, slippage, partial fills, market impact), order management is idempotent, and execution deviations from signals are tracked.

### Findings

**What exists:**
- PaperExecutor simulates limit orders with random slippage (0.01–0.05%) and half-spread (0.02%).
- Position persistence across restarts via JSON + SQLite.
- Trailing stop, 1R breakeven move, time-based exit implemented.
- Partial close via `partial_close()`.
- Fill simulation: limit orders fill when price reaches entry_price.

**Gaps identified:**

1. **Limit orders always fill at exact entry_price.** In reality, limit orders at the current price compete with other orders. At 1h timeframes with BTC spreads of 0.02–0.05%, a limit order 0.20×ATR beyond close may never fill if the candle reverses before touching that level. The current simulation has a ~100% fill rate for any price that technically touches entry_price.

2. **Slippage is random uniform between _SLIPPAGE_MIN (0.01%) and _SLIPPAGE_MAX (0.05%).** This does not model actual crypto market impact. On Bybit, a $4,000 USDT position in BTC has essentially zero market impact, so 0.01% is accurate. But the same random slippage is applied to all positions regardless of size, symbol liquidity, or volatility state — meaning a $4,000 BTC trade and a $4,000 XRP trade get the same slippage model.

3. **No partial fill modeling.** At 1h timeframes with limit orders in illiquid regimes, partial fills (e.g., 40% fill on a limit order) are realistic. The system assumes binary: fully filled or not triggered.

4. **No commission model beyond slippage.** Bybit Demo charges 0.02% maker / 0.055% taker. At 0.5% risk per trade, a round-trip taker fee of 0.11% represents 22% of the risk budget. This is not explicitly modeled — it's partially covered by slippage but conflated rather than separated.

5. **PaperExecutor uses `random.uniform` for slippage.** This is non-deterministic across restarts, making exact trade replay impossible. For audit or debugging, slippage should be seeded or recorded per-trade.

6. **Limit order fill logic checks `current_price <= entry_price` for buy.** This means any tick at or below entry fills the order instantly. A price that dips through entry_price on the same bar as the signal could produce a same-candle fill — which is not available at 1h timeframes where entry is set beyond the close.

**Rating: ⚠️ GAP** — Execution simulation has realistic slippage magnitudes but does not model partial fills, commission separately, or fill probability realistically.

---

## Dimension 6: Data Integrity

**Professional standard:** All data used for signal generation and backtesting is sourced from the exchange, timestamped, validated for gaps and outliers, and versioned.

### Findings

**What exists:**
- OHLCV data fetched from Bybit via ccxt REST (`exchange_manager.py`).
- Parquet storage in `backtest_data/` for historical data.
- `fetch_historical_data_v2.py`: write-then-rename pattern prevents corrupt parquet on interrupt.
- JSON-level rate limit retry with 60s sleep + continue (not break).
- 300-bar lookback per scan cycle.

**Gaps identified:**

1. **No data quality validation pipeline.** There is no check for: zero-volume candles, duplicate timestamps, out-of-order timestamps, OHLC relationship violations (high < low), price spikes >3σ from local mean, or exchange outage gaps. A single bad candle (e.g., a fat-finger spike at 10× normal price) would propagate through the indicator library, potentially generating a false regime classification and a false signal.

2. **No 30m OHLCV data confirmed available.** The system runs on 1h as primary. Phase 4 (candle interval optimization) will require 5m, 15m, 30m data. Only 1h and some shorter timeframes are confirmed fetched.

3. **Historical backtest data and live scan data use different fetch paths.** Historical data is fetched via `fetch_historical_data_v2.py` into parquet files. Live scan data is fetched inline by the ScanWorker via `exchange.fetch_ohlcv()`. If there are any differences in how these paths handle timezone, bar alignment, or volume normalization, the backtest results may not be reproducible live.

4. **300-bar window provides only ~12.5 days of 1h data.** For HMM training, regime classification with 12 labels, and all indicator computations (EMA200 needs 200+ bars to be meaningful), 300 bars is the absolute minimum — not a comfortable margin. EMA200 will be unreliable for the first 200 bars and requires 300+ bars to stabilize.

5. **No candle-close timestamp validation.** The scanner fires 30 seconds after the expected candle close time. If the exchange serves a partially-formed bar (e.g., due to server lag), the scanner will process incomplete data. No timestamp comparison against the bar's expected close time is performed.

**Rating: ⚠️ GAP** — Data fetching is robust against interruption but has no quality validation, no manifest of validated data, and relies on insufficient lookback windows.

---

## Dimension 7: Observability

**Professional standard:** Every signal, trade, and system event is logged with sufficient detail to reconstruct decisions. Dashboards show leading indicators, not just lagging P&L.

### Findings

**What exists:**
- 20 GUI pages including Performance Analytics (9 tabs), Edge Analysis, Validation, Demo Monitor.
- Signal log persisted to SQLite `SignalLog` table on every approve/reject decision.
- AI trade analysis stored in `TradeFeedbackStore` per closed trade.
- `FilterStatsTracker`: per-filter win rates and realized R tracked.
- `ModelPerformanceTracker`: per-model P&L.
- `LiveVsBacktestTracker`: rolling deviation from Study 4 baselines.
- Daily report script generates P&L + AI analysis proposals.
- Per-scan `ConfluenceScorer` diagnostics: `raw_score`, `effective_threshold`, `direction_split`, `per_model`, `damp_factors`, `oi_modifier`.
- `RiskGate._persist_signal_log()`: approved/rejected signal with full rationale saved.

**Gaps identified:**

1. **No signal-to-trade conversion funnel dashboard.** How many signals fired this week? How many passed ConfluenceScorer? How many passed RiskGate? How many were executed? The filter funnel (signals → candidates → approved → executed) is not presented as a single dashboard metric. Understanding where trades are being lost (too few signals? too many rejections?) requires manual log scraping.

2. **AI feedback loop quality is tracked structurally but P&L correlation is unmeasured.** The AI generates `TuningProposalGenerator` recommendations per closed trade. Whether acting on these recommendations improves P&L is not tracked. There is no A/B framework comparing performance with and without AI recommendations applied.

3. **No real-time latency monitoring.** The scan cycle at 1h fires every 3,630 seconds (1h + 30s buffer). There is no timing telemetry that measures: fetch duration, indicator calculation duration, signal generation duration, risk gate duration. If a scan cycle takes 45 seconds (vs. expected 5 seconds), that's a data quality risk — but it would be invisible.

4. **No fill price vs. signal price tracking.** The signal sets entry_price; the fill simulates slippage. But the ratio of (fill_price - signal_price) / ATR is not tracked over time. If slippage is consistently adverse (fills at the top of the 1h bar's range), this would show up as a systematic alpha leak.

5. **Logs contain unquantified warnings.** CLAUDE.md notes that `hmmlearn non-convergence warnings` and `MSGARCH refit warning` are `expected informational` and `harmless`. Having known-harmless warnings in production logs makes it harder to detect genuinely harmful new warnings.

**Rating: ✅ STRONG (with gaps)** — Observability is the strongest dimension. Signal log, per-model tracking, and AI feedback loop are institutionally solid. Primary gap is the signal funnel dashboard and AI ROI tracking.

---

## Dimension 8: Computational Efficiency

**Professional standard:** Hot paths (per-bar indicator calculation, signal evaluation, risk checks) execute in <100ms. No redundant computation. Thread safety enforced.

### Findings

**What exists:**
- Indicator library calculates 17 EMA periods, 17 SMA periods, 9 RSI periods, 9 ATR periods, 3 SuperTrend periods, Bollinger, Keltner, Donchian, VWAP, MACD, Stochastic, CCI, Williams %R, Ichimoku, OBV, A/D, MFI, CMF, Pivot Points, Fibonacci — approximately 80+ columns per symbol per scan.
- Thread-safety enforced via `GIT_INDEX_FILE` patterns and import lock avoidance in confluence_scorer.
- FinBERT on GPU (~5–10ms/batch).
- ScanWorker runs in QThread, results returned via Signal/Slot with QueuedConnection.

**Gaps identified:**

1. **Massive indicator over-computation.** The indicator library computes EMA for 17 periods, SMA for 17 periods, ATR for 9 periods, RSI for 9 periods. The active sub-models use: EMA9, EMA20, EMA21, EMA100 (TrendModel), RSI14, ATR14 (all models), VWAP (VWAP model), BB (MomentumBreakout, VWAP), ADX (TrendModel). The remaining 60+ columns are computed on every scan cycle, stored in a DataFrame copy, and discarded after scoring. No lazy evaluation.

2. **`df = df.copy()` called repeatedly.** The indicator library explicitly calls `df = df.copy()` after SuperTrend insertions to defragment the DataFrame. Combined with `pd.concat()` for each batch of indicators, this creates 5–10 DataFrame copies per symbol per scan. For 5 symbols × 300 rows × 80 columns of float64, this is ~5 × 300 × 80 × 8 bytes = ~1MB per copy, with 5–10 copies = 5–10MB of transient allocation per scan cycle.

3. **No indicator caching between scan cycles.** Each 1h scan recomputes all 80+ indicators from scratch on 300 candles. Only the last candle changes between cycles. Incremental indicator update (compute new values for the one new candle, append) would reduce computation by ~99.7% for EMA/SMA and ~95% for ATR/RSI.

4. **OrderBookModel is evaluated but structurally cannot fire at 1h.** Evaluating a model that provably cannot fire wastes CPU time in `generate()` and signal aggregation logic.

5. **HMM refit on every scan cycle?** If `HMMRegimeClassifier.fit()` is called on every scan, that's 300 bars × 4 features × HMM EM iteration (n_iter=200) per symbol per hour. For 5 symbols, this could take several seconds per cycle.

**Rating: ⚠️ GAP** — Indicator over-computation is the primary inefficiency. Caching last cycle's indicators and only updating incrementally would dramatically reduce per-cycle overhead.

---

## Dimension 9: Capital Deployment Efficiency

**Professional standard:** Available capital is deployed at 70–100% utilization in expected-positive market conditions. Idle capital in a positive-EV environment is waste.

### Findings

**What exists:**
- `max_capital_pct = 4%` per position, `risk_pct_per_trade = 0.5%`.
- Max 5 concurrent positions = theoretical max 20% capital deployed simultaneously.
- 5 symbols in watchlist (BTC, ETH, SOL, BNB, XRP).
- Scanner fires once per 1h candle close. Between scans, no new entries are considered.

**Gaps identified:**

1. **Capital utilization is structurally capped at 20%.** With max 5 positions × 4% each = 20% of capital in use at peak. For a $100,000 paper account, $80,000 is always idle. At any realistic trade frequency (Phase 1 demo: ~2–5 trades/day across 5 symbols at 1h), average utilization is likely 4–8%. This means the system is barely deploying any capital and P&L will be minimal even if signals are excellent.

2. **Position sizing does not scale with conviction.** All approved candidates receive `risk_pct_per_trade = 0.5%` regardless of whether the confluence score is 0.56 (barely over threshold) or 0.92 (all models agree, orchestrator confirms, regime strongly favorable). Professional systems use tiered or proportional sizing: 0.5% at threshold, scaling to 1.5% for highest-conviction setups.

3. **No portfolio-level capital allocation logic.** With 5 symbols and up to 5 concurrent positions, the system could be 100% in BTC (5 BTC positions) while ETH/SOL have strong signals. The correlation controller checks directional exposure, but there is no mechanism to ensure diversified capital deployment across uncorrelated symbols.

4. **Symbol weights (SOL=1.3, ETH=1.2, BTC=1.0) are applied to confluence score, not to position size.** A higher symbol weight adjusts the score used for ranking candidates, not the actual capital deployed. If SOL has weight 1.3 and BTC has weight 1.0, but both are capped at 4% max capital per position, the weight makes no difference to actual capital allocation.

5. **No dynamic utilization monitoring.** There is no dashboard metric showing current capital utilization % with an alert when utilization falls below a target floor (e.g., "utilization <5% for 24 hours in non-crisis conditions").

**Rating: ❌ MISSING** — Capital deployment is the most significant gap for profitability. 80% idle capital in a profitable strategy produces 80% less profit than achievable. This is the #1 ROI lever.

---

## Dimension 10: Learning System Effectiveness

**Professional standard:** The learning system demonstrably improves out-of-sample performance over time. Improvements are measurable, not self-reported.

### Findings

**What exists:**
- L1 (global win-rate per model, ±15% weight adjustment, rolling 30-trade window).
- L2 (regime×model and asset×model contextual, ±10%/±8%, combined clamped to [0.70, 1.30]).
- AdaptiveWeightEngine combines L1 × L2.
- ProbabilityCalibrator (isotonic regression, activates after 300 trades).
- AdaptiveLearningPolicy: blocks proposals when out-of-sample performance degrades (overfitting protection added v1.1).
- AI feedback loop: TradeAnalysisService → TuningProposalGenerator → recommendations per closed trade.
- Source-tagging: `test`/`synthetic` outcomes rejected by TradeOutcomeTracker.

**Gaps identified:**

1. **L1+L2 combined multiplier is clamped to [0.70, 1.30].** This is a ±30% maximum adjustment range. For a model with weight 0.35 (TrendModel), the effective range is 0.245–0.455. This is sensible conservatism but means the learning system cannot zero out a consistently-losing model — only reduce its weight by 30%. A model with 35% WR (consistently below 50% baseline) would receive a maximum multiplier of 0.70, still contributing 70% of its base weight to the score. **The learning system cannot disable underperformers** — that requires manual `disabled_models` config.

2. **L1 window is only 30 trades.** For a model that fires in a specific regime, 30 trades could span 3+ months of 1h trading. The win rate computed from 30 trades has a standard error of √(0.5×0.5/30) ≈ 9%. This means a 59% WR and a 41% WR are statistically indistinguishable in the L1 window. Weight adjustments are being made on statistical noise.

3. **ProbabilityCalibrator requires 300 trades before activation.** At Phase 1 demo pace (~2–5 trades/day), 300 trades = 60–150 days. The EV gate sigmoid fallback during this period provides no per-symbol or per-regime calibration — just a global curve. EV gate is essentially disabled for the first quarter of live trading.

4. **AI tuning proposals are generated but proposal → outcome → measurement cycle is absent.** The `TuningProposalGenerator` creates recommendations after each closed trade. But there is no mechanism to: (a) track which proposals were applied to config, (b) measure the before/after performance change, (c) attribute performance changes to specific proposals. The AI feedback loop is an open loop — it generates output but does not close the loop with outcome measurement.

5. **No regime-conditional performance attribution for learning.** L2 tracks performance by regime label × model. But regime label accuracy is itself uncertain (Dimension 3 gap). If the regime classifier mislabels 40% of periods, L2 adjustments trained on mislabeled context will amplify noise rather than signal.

**Rating: ⚠️ GAP** — Learning infrastructure is well-designed but operates on insufficient data (30-trade L1 window), cannot disable underperformers, and the AI proposal→outcome loop is open.

---

## Summary Table

| Dimension | Rating | Primary Gap | Priority |
|-----------|--------|-------------|----------|
| 1. Research Fidelity | ⚠️ GAP | Single-pass backtest, different code path from live, no OOS validation | HIGH |
| 2. Signal Quality | ⚠️ GAP | Dead weight models, lag-prone entries, no IC measurement | HIGH |
| 3. Regime Design | ⚠️ GAP | 12.5-day lookback insufficient, 12 labels too many, no regime accuracy metric | MEDIUM |
| 4. Risk Management | ⚠️ GAP | Circuit breaker too loose, no daily loss limit, EV gate decorative pre-300 trades | HIGH |
| 5. Execution Quality | ⚠️ GAP | 100% fill rate for touched limits, no partial fills, no commission separation | MEDIUM |
| 6. Data Integrity | ⚠️ GAP | No quality validation, insufficient lookback, different fetch paths backtest vs. live | HIGH |
| 7. Observability | ✅ STRONG | Missing signal funnel dashboard and AI ROI tracking | LOW |
| 8. Computational Efficiency | ⚠️ GAP | Massive indicator over-computation, no incremental cache | MEDIUM |
| 9. Capital Deployment | ❌ MISSING | 80% idle capital structurally, no conviction-scaled sizing | CRITICAL |
| 10. Learning System | ⚠️ GAP | Open AI loop, 30-trade window too small, calibrator needs 300 trades | MEDIUM |

---

## Top 5 Critical Recommendations (Phase 1 Output)

### R1 — Fix Capital Deployment (Dimension 9) [CRITICAL]
At 0.5% risk × 4% cap × 5 max positions, capital utilization peaks at 20%. Even a perfect strategy earns only 20¢ on the dollar. **Recommended:** Increase max positions to 10, increase max_capital_pct to 8%, add conviction-scaling (0.5–1.5% risk tier based on score quintile). Expected: 3–4× improvement in absolute P&L without changing signal quality.

### R2 — Fix Backtest Code Path Parity (Dimension 1) [HIGH]
The backtest engine (`backtest_engine.py`) is a rule-evaluator that does not use ConfluenceScorer, RiskGate, or the live signal pipeline. All backtest results are produced on a different code path than live trading. **Recommended:** Replace with an `IDSSBacktester` that replays OHLCV through the exact live pipeline (`SignalGenerator → ConfluenceScorer → RiskGate → PositionSizer`). This will likely change backtest results substantially and may invalidate Study 4 baselines.

### R3 — Remove or Conditionally Activate Dead Weight Models (Dimension 2) [HIGH]
OrderBookModel never fires at 1h (structural), SentimentModel fires on 8h-stale news at 1h, VWAPReversionModel is disabled but still evaluated. Remove these from the active scan path or add TF-gate logic that skips evaluation when the model cannot meaningfully contribute. This reduces compute, eliminates weight dilution, and improves score interpretability.

### R4 — Add Daily Loss Limit Kill Switch (Dimension 4) [HIGH]
Implement a daily loss cap: if intra-day paper P&L falls below −2% of starting capital, halt new positions for the remainder of the session. This is standard risk management in professional systems and prevents a systematic failure mode (e.g., HMM misclassifying a regime during a fast market move) from causing disproportionate damage.

### R5 — Extend HMM Training Window to 1000+ Bars (Dimension 3) [MEDIUM]
Increase the OHLCV fetch per scan cycle from 300 to 1000 bars (41 days at 1h). This allows the HMM to see multiple regime transitions in its training window, produces more stable state→label mappings, and makes EMA200 actually reliable. Memory cost: 5 symbols × 1000 bars × 80 columns × 8 bytes ≈ 3.2MB — negligible.

---

## Metrics Baseline (Pre-Transformation)

| Metric | Current Value | Professional Target |
|--------|--------------|---------------------|
| Max capital utilization | ~20% | 70–90% |
| Average daily utilization (estimated) | ~4–8% | >50% |
| Backtest-to-live code path parity | ❌ Different paths | ✅ Identical |
| IC measurement available | ❌ No | ✅ Per model |
| Walk-forward OOS validation | ❌ None | ✅ 3+ folds |
| Daily loss limit | ❌ None | ✅ −2% daily cap |
| Indicator columns computed per scan | ~80+ | ~15–20 (lazy) |
| HMM training lookback | 300 bars (12.5d) | 1000+ bars (42d+) |
| L1 window size | 30 trades | 100+ trades |
| AI proposal→outcome measurement | ❌ Open loop | ✅ Closed loop |

---

*Next: Phase 2 — Indicator & Computation Audit (classify all 80+ computed columns as CORE/CONDITIONAL/UNUSED/REMOVE)*
