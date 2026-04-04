# NexusTrader — Phases 3–6: Architecture, Data & Optimization
**Date:** 2026-03-26 | Version: v1.1

---

## Phase 3: Multi-Asset Architecture Validation

### What Exists (Verified from Code)

**MultiAssetConfig (`core/strategy/multi_asset_config.py`):**
- Per-asset `AssetProfile` with: active_strategies, max_position_pct, risk_multiplier, min_confluence_score, feature flags (RL, FinBERT, HMM, WebSocket).
- 5 default profiles loaded: BTC/USDT, ETH/USDT, SOL/USDT, BNB/USDT, XRP/USDT.
- Fallback to BTC profile for unknown symbols.
- Profiles persist to `settings.yaml` via `save_to_settings()`.

**SymbolAllocator (`core/analytics/symbol_allocator.py`):**
- Per-symbol weights: SOL=1.3, ETH=1.2, BTC=1.0, BNB=0.8, XRP=0.8 (STATIC mode).
- DYNAMIC mode: 3 profiles keyed to BTC Dominance (BTC_DOMINANT / NEUTRAL / ALT_SEASON).
- `adjusted_score = base_score × symbol_weight` for candidate ranking ONLY — no effect on position sizing, stops, or targets.

**ConfluenceScorer integration:**
- `multi_asset_config.get_min_confluence_score(symbol)` — per-asset threshold floor.
- Orchestrator veto checked before scoring.

### Gaps

1. **MultiAssetConfig `active_strategies` list is disconnected from SignalGenerator.** The profile lists strategy names like `"trend_following"`, `"mean_reversion"` etc. But `SignalGenerator` doesn't read these strings — it instantiates all sub-model objects at startup (`_ALL_MODELS`) and runs all of them. The per-asset strategy gate in MultiAssetConfig is not enforced. All 8 sub-models are evaluated for every symbol regardless of per-asset config.

2. **max_position_pct per-asset is not applied.** `MultiAssetConfig.get_max_position_pct()` returns per-asset values (BTC 10%, ETH 8%, SOL 6%, BNB/XRP 5%). But `RiskGate` uses `self.max_position_capital_pct` (global 25% default, effectively overridden by CDA multiplier). The per-asset max_position_pct from MultiAssetConfig is only used for `get_min_confluence_score()` — not for actual position sizing limits.

3. **risk_multiplier per-asset is not applied.** `AssetProfile.risk_multiplier` (ETH=0.90, SOL=0.75, BNB=0.70, XRP=0.65) is defined but no code path reads it and applies it to position sizing or risk calculation.

4. **Symbol weights affect score ranking, not capital allocation.** `adjusted_score = base_score × 1.3` (SOL) may move SOL to the top of the candidate batch. But if both BTC and SOL produce candidates and max_positions allows both, SOL's higher weight doesn't result in more capital in SOL — both get the same 4% cap.

### Scalability Assessment

The architecture is designed for N symbols:
- `WatchlistManager` can hold arbitrary symbols.
- `ScanWorker` iterates `self._symbols` list.
- All state is per-symbol (HMM models persisted per-symbol in `hmm_models` dict).
- MultiAssetConfig supports arbitrary symbols with per-asset profiles.

**Scalability verdict: ✅ Architecture supports multi-asset at scale. Gaps are configuration enforcement bugs, not architectural limits.** Adding BTC dominance-aware dynamic weighting would require only a BTC dominance data feed (available via CoinGlass agent).

### Phase 3 Recommendations

1. **Enforce active_strategies per asset in SignalGenerator.** Pass the MultiAssetConfig profile into `SignalGenerator.generate()` and skip models not in `profile.active_strategies`. This reduces compute and enforces the per-asset strategy gate that was already designed.

2. **Apply risk_multiplier per asset in PositionSizer.** Multiply `risk_pct_per_trade` by `profile.risk_multiplier` for each asset. This allows conservative sizing for volatile alts (XRP at 0.65×, BNB at 0.70×) vs. BTC/ETH.

3. **Route max_position_pct through RiskGate per candidate.** Instead of using the global 25% cap, look up `multi_asset_config.get_max_position_pct(symbol)` in `RiskGate.validate()` and apply it as the per-symbol ceiling.

---

## Phase 4: Candle Interval Optimization Analysis

### Data Availability (Confirmed)

All 5 symbols have verified parquet data from **2022-03-22 to 2026-03-21** (~4 years):

| Timeframe | Bars (BTC example) | Coverage | Status |
|-----------|-------------------|----------|--------|
| 1m | — | — | ❌ NOT AVAILABLE |
| 5m | 420,465 | 4 years | ✅ Available |
| 15m | 140,155 | 4 years | ✅ Available |
| 30m | — | — | ❌ NOT AVAILABLE |
| 1h | 35,039 | 4 years | ✅ Available (current live TF) |
| 4h | 8,760 | 4 years | ✅ Available |

**Missing timeframes: 1m and 30m.** Phase 6 covers fetch scripts for these.

### Framework for Candle Interval Optimization

For each timeframe × symbol combination, a proper optimization study requires:

**Metrics to compute per timeframe:**
1. Win rate (%)
2. Expectancy (avg R per trade)
3. Trade frequency (trades per 30 days)
4. Max drawdown (R)
5. Profit factor
6. Net P&L (USDT)
7. Regime sensitivity (does performance degrade in specific regimes?)

**Methodology:**
- Use the IDSS-compatible backtester (once Phase 1 R2 fix is implemented — i.e., live pipeline replay).
- Split: 2022-03-22 to 2024-12-31 = **in-sample** (34 months). 2025-01-01 to 2026-03-21 = **out-of-sample** (15 months).
- Run each model separately: TrendModel-only, MomentumBreakout-only, Full System.

**Hypothesis for each timeframe:**

| TF | Hypothesis | Expected outcome |
|----|-----------|-----------------|
| 5m | High frequency, fast trend detection | High trade count (100+/month), but noise-prone. ATR stops may be too tight for BTC volatility. Slippage impact significant. |
| 15m | Balance between frequency and signal quality | Good breakout detection for MomentumBreakout. Regime classifier may struggle with 12 labels at 15m bar frequency. |
| 1h | Current live TF. Known Study 4 baselines. | Benchmark: TrendModel PF 1.47, MB PF 4.17. |
| 4h | Low frequency, high quality. | Very few trades (<10/month). EMA9/EMA21 crossover lags significantly at 4h. Drawdown per trade may be larger (wider ATR). |

**Multi-TF combination hypothesis:**
- 15m signal + 1h regime confirmation: Higher quality than 15m alone (fewer false breakouts filtered by 1h regime), better trade frequency than 1h alone.
- 5m entry + 1h direction: Scalping into the 1h trend. Requires accurate 1h regime determination first.

### Phase 4 Implementation Plan

This phase requires actual backtesting code changes (Phase 1 R2 fix first). Until the IDSS backtester uses the live pipeline, interval optimization results will be unreliable. **Phase 4 is blocked by Phase 1 R2 (backtest code path parity).**

Preliminary recommendation based on theory and Study 4 data:
- **MomentumBreakoutModel**: Test at 15m as primary TF. Breakouts at 15m are faster and the measured-move target is more achievable than at 1h.
- **TrendModel**: Stay at 1h or test 4h. EMA crossovers are more meaningful at longer TFs where noise is filtered.
- **Combined system**: 15m signal + 1h regime guard is the most promising multi-TF combination.

---

## Phase 5: Parameter Optimization Framework Design

### Current State

Sub-model parameters are stored in `config.yaml` under `models.*` and read via `_s.get('models.trend.adx_min', 25.0)`. All are tunable without code changes.

Current parameter set per model:

**TrendModel:** adx_min (25), rsi_long_min (45), rsi_long_max (70), rsi_short_min (30), rsi_short_max (55), strength_base (0.15), ema20_bonus (0.25), macd_bonus (0.20), adx_bonus_max (0.40), entry_buffer_atr (0.20)

**MomentumBreakoutModel:** lookback (20), vol_mult_min (1.5), rsi_bullish (55), rsi_bearish (45), strength_base (0.35), entry_buffer_atr (0.10)

**VWAPReversionModel:** deviation_threshold, rsi_oversold, rsi_overbought, atr_mult (from settings, not audited in detail)

**ConfluenceScorer:** SCORE_THRESHOLD (0.55), dynamic_confluence params (floor 0.28, ceiling 0.65)

**RiskGate:** ev_threshold (0.05), min_rr_floor (1.0), sigmoid_steepness (8.0), score_midpoint (0.55)

### Parameter Optimization Methodology

#### Approach 1: Grid Search (for small parameter spaces)
Suitable for: TrendModel (ADX threshold, RSI bounds = ~4 parameters × 5–10 values each = 500–10,000 combinations).

**Grid for TrendModel:**
```
adx_min: [20, 22, 25, 28, 30]
rsi_long_min: [40, 43, 45, 48, 50]
rsi_long_max: [65, 68, 70, 73, 75]
entry_buffer_atr: [0.10, 0.15, 0.20, 0.25, 0.30]
```
Total: 5^4 = 625 combinations. At 1h on 35,039 bars ≈ 30 trades/combo average = feasible.

#### Approach 2: Bayesian Optimization (for larger spaces)
Suitable for combined model + risk parameters (10+ dimensions). Use `scikit-optimize` or `optuna`.

Target metric: **Sharpe ratio on out-of-sample period** (NOT profit factor or win rate — these overfit faster).

#### Walk-Forward Validation Protocol
This is mandatory before any parameter change reaches live config:

```
In-sample window:  12 months
Out-of-sample:      6 months
Step size:          3 months
Minimum folds:      4

Fold 1: Train 2022-03-22→2023-03-21 | Test 2023-03-22→2023-09-21
Fold 2: Train 2022-09-22→2023-09-21 | Test 2023-09-22→2024-03-21
Fold 3: Train 2023-03-22→2024-03-21 | Test 2024-03-22→2024-09-21
Fold 4: Train 2023-09-22→2024-09-21 | Test 2024-09-22→2025-03-21
Fold 5: Train 2024-03-22→2025-03-21 | Test 2025-03-22→2025-09-21
Fold 6: Train 2024-09-22→2025-09-21 | Test 2025-09-22→2026-03-21
```

**Stability requirement:** A parameter set is valid only if:
- Out-of-sample Sharpe ≥ 0.5 on all folds
- Out-of-sample PF ≥ 1.1 on ≥4 of 6 folds
- Maximum performance degradation (in-sample PF - out-of-sample PF) ≤ 0.5

#### Overfitting Guard
- Reject any parameter set where the ratio `OOS_metric / IS_metric < 0.60` (40%+ degradation = likely overfit).
- Require ≥ 30 trades in each OOS fold to ensure statistical significance.
- Use Bonferroni correction when testing multiple parameters simultaneously.

### Phase 5 Implementation Requirements

1. IDSS backtester with live pipeline replay (Phase 1 R2 prerequisite).
2. Parameter sweep runner script (`scripts/parameter_optimization.py`) that:
   - Accepts parameter grid as JSON config
   - Runs each combination through IDSSBacktester
   - Returns per-fold metrics table
   - Identifies Pareto-optimal parameter sets
3. Walk-forward validator (`scripts/walk_forward_validator.py`).
4. Results stored to `data/optimization/` with git-tracked parameter history.

---

## Phase 6: Real Data Audit & Exchange Scripts

### Data Inventory (Confirmed via Parquet Analysis)

| Symbol | 5m | 15m | 30m | 1h | 4h | Date Range |
|--------|----|----|-----|----|----|------------|
| BTC/USDT | ✅ 420K | ✅ 140K | ❌ | ✅ 35K | ✅ 8.7K | 2022-03-22 → 2026-03-21 |
| ETH/USDT | ✅ 420K | ✅ 140K | ❌ | ✅ 35K | ✅ 8.7K | 2022-03-22 → 2026-03-21 |
| SOL/USDT | ✅ 420K | ✅ 140K | ❌ | ✅ 35K | ✅ 8.7K | 2022-03-22 → 2026-03-21 |
| BNB/USDT | ✅ 420K | ✅ 140K | ❌ | ✅ 35K | ✅ 8.7K | 2022-03-22 → 2026-03-21 |
| XRP/USDT | ✅ 420K | ✅ 140K | ❌ | ✅ 35K | ✅ 8.7K | 2022-03-22 → 2026-03-21 |

**30m data is missing for all symbols.** 1m data is also absent — but 1m optimization is not a priority given the noise characteristics at that timeframe for this signal set.

### Data Source Notes
- BTC 5m: source=`binance` (per download_summary.json) — all other parquet files sourced from `cache`.
- All data ends at 2026-03-21, missing the last 5 days to present. Live data fetched inline.
- Timestamps are UTC timezone-aware in parquet (confirmed from `pd.read_parquet` output showing `+00:00`).

### Data Quality Validation (Missing — Phase 1 Gap #6)

The following validation checks are not currently performed and must be added:

```python
def validate_ohlcv(df: pd.DataFrame, symbol: str, timeframe: str) -> dict:
    """
    Returns validation report for OHLCV parquet file.
    All checks are non-destructive — report only, no auto-correction.
    """
    issues = []
    # 1. Timestamp continuity
    expected_freq = {'1m': '1T', '5m': '5T', '15m': '15T', '30m': '30T',
                     '1h': '1H', '4h': '4H'}[timeframe]
    expected_idx = pd.date_range(df.index[0], df.index[-1], freq=expected_freq)
    missing_bars = len(expected_idx) - len(df)
    if missing_bars > 0:
        issues.append(f"GAPS: {missing_bars} missing bars ({missing_bars/len(expected_idx)*100:.2f}%)")

    # 2. OHLC relationship
    bad_ohlc = ((df['high'] < df['low']) | (df['close'] > df['high']) |
                (df['close'] < df['low'])).sum()
    if bad_ohlc > 0:
        issues.append(f"OHLC_VIOLATION: {bad_ohlc} bars with high < low or close outside range")

    # 3. Zero volume
    zero_vol = (df['volume'] == 0).sum()
    if zero_vol > 0:
        issues.append(f"ZERO_VOLUME: {zero_vol} bars ({zero_vol/len(df)*100:.2f}%)")

    # 4. Price spikes (>10× neighbor)
    pct_change = df['close'].pct_change().abs()
    spikes = (pct_change > 0.50).sum()  # >50% single-bar move
    if spikes > 0:
        issues.append(f"PRICE_SPIKES: {spikes} bars with >50% single-bar price change")

    # 5. Duplicate timestamps
    dups = df.index.duplicated().sum()
    if dups > 0:
        issues.append(f"DUPLICATE_TIMESTAMPS: {dups} duplicates")

    return {"symbol": symbol, "timeframe": timeframe, "bars": len(df),
            "issues": issues, "pass": len(issues) == 0}
```

### 30m Fetch Script

The existing `fetch_historical_data_v2.py` supports arbitrary timeframes. The 30m data can be fetched with the following command:

```bash
python scripts/fetch_historical_data_v2.py \
  --symbols BTC/USDT ETH/USDT SOL/USDT BNB/USDT XRP/USDT \
  --timeframe 30m \
  --start 2022-03-22 \
  --end 2026-03-26 \
  --exchange bybit \
  --output backtest_data/
```

**Note:** Verify that `fetch_historical_data_v2.py` supports the `--timeframe 30m` parameter and that Bybit's ccxt interface returns 30m bars. If not, use `--exchange binance` as fallback (BTC 5m was already sourced from Binance).

### Live Data Staleness

The parquet files end at 2026-03-21. The gap between parquet end and today (2026-03-26) is 5 days. For backtesting, this is acceptable. For real-time signal validation (Phase 7), the live scan path fetches the last 300 bars inline via ccxt — this is always current.

### Data Consistency Check

Both fetch paths (parquet for backtesting and inline scan for live) must produce identical OHLCV values for overlapping periods. This can be verified by:

```python
# Cross-check: last 10 bars of parquet vs. live fetch
parquet_tail = pd.read_parquet('backtest_data/BTC_USDT_1h.parquet').tail(10)
live_bars = exchange.fetch_ohlcv('BTC/USDT', '1h', limit=15)
live_df = pd.DataFrame(live_bars, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
# Compare overlapping timestamps — should be identical or within exchange rounding
```

This check is not currently automated. It should be added to `scripts/launch_checklist.py` as a data integrity check.

---

*Next: Phases 7–10 (AI Validation, Feedback Loop, Capital Deployment, Execution Hardening)*
