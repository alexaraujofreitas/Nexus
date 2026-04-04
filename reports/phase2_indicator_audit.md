# NexusTrader — Phase 2: Indicator & Computation Audit
**Date:** 2026-03-26
**Version assessed:** v1.1
**Source:** `core/features/indicator_library.py`, all `core/signals/sub_models/*.py`, `core/regime/regime_classifier.py`, `core/regime/hmm_regime_classifier.py`

---

## Objective
Classify every computed indicator column as:
- **CORE** — Required by an active, firing signal or regime classifier. Cannot be removed.
- **CONDITIONAL** — Required only by a disabled or TF-restricted model. Keep the computation if that model may be re-enabled; remove otherwise.
- **UNUSED** — Computed but never consumed by any live code path. Verified by tracing all consumers.
- **REMOVE** — Actively wasteful: either always UNUSED or consumed only by dead code.

Primary consumers checked:
1. `TrendModel` — active, 1h, bull_trend / bear_trend
2. `MomentumBreakoutModel` — active, 1h, volatility_expansion
3. `VWAPReversionModel` — disabled (Study 4 PF 0.28), code still evaluates
4. `MeanReversionModel` — disabled (Study 4 PF 0.21), code still evaluates
5. `LiquiditySweepModel` — disabled, code still evaluates
6. `FundingRateModel` — active, reads FundingRateAgent (not DataFrame), doesn't consume indicators
7. `OrderBookModel` — active code, never fires at 1h (structural zero), reads OrderBookAgent (not DataFrame)
8. `SentimentModel` — active, reads NewsFeed/FinBERT (not DataFrame), uses `atr_14` only
9. `RegimeClassifier` (rule-based) — reads ADX, EMA slopes, BB width, volume trend, RSI
10. `HMMRegimeClassifier` — reads log_return, BB width, ADX normalised, volume/vol_mean ratio
11. `BacktestEngine` (condition evaluator) — all columns exposed for user-defined rules

---

## Active Consumer Column Map

### TrendModel (active)
| Column | Usage |
|--------|-------|
| `close` | Entry price base, regime checks |
| `ema_9` | Primary trend direction signal |
| `ema_21` | Primary trend direction signal (EMA9 vs EMA21 crossover) |
| `ema_20` | Secondary confirmation (EMA20 > EMA100 for multi-TF trend) |
| `ema_100` | Secondary confirmation |
| `adx` / `adx_14` | Trend strength gate (≥ 25) |
| `rsi_14` | Momentum zone gate [45–70] long, [30–55] short |
| `macd` | Bonus strength: MACD above/below signal |
| `macd_signal` | As above |
| `atr_14` | Stop/target computation |

### MomentumBreakoutModel (active)
| Column | Usage |
|--------|-------|
| `close` | Breakout comparison to range_high/range_low |
| `high` | `range_high = prev 20-bar high` |
| `low` | `range_low = prev 20-bar low` |
| `volume` | Volume confirmation (vol_mult ≥ 1.5×) |
| `rsi_14` | Directional confirmation (>55 long, <45 short) |
| `atr_14` | Stop/target computation |

### VWAPReversionModel (disabled, still evaluated)
| Column | Usage |
|--------|-------|
| `vwap` | VWAP deviation z-score |
| `rsi_14` | RSI confirmation |
| `atr_14` | Stop/target |
| `close` | Deviation calculation |

### MeanReversionModel (disabled, still evaluated)
| Column | Usage |
|--------|-------|
| `bb_upper`, `bb_lower`, `bb_mid` | Mean reversion zone detection |
| `rsi_14` | Oversold/overbought gate |
| `atr_14` | Stop/target |
| `close` | BB distance calculation |

### LiquiditySweepModel (disabled, still evaluated)
| Column | Usage |
|--------|-------|
| `high`, `low`, `close` | Sweep detection against prior range |
| `volume` | Rejection candle confirmation |
| `atr_14` | Stop/target |

### SentimentModel (active, agent-based)
| Column | Usage |
|--------|-------|
| `atr_14` | Stop/target only |
| `close` | Entry price |

### FundingRateModel (active, agent-based)
| Column | Usage |
|--------|-------|
| `close` | Entry price only |
| `atr_14` | Stop/target only |

### OrderBookModel (active code, never fires at 1h)
| Column | Usage |
|--------|-------|
| `close` | Entry price (if it could fire) |
| `atr_14` | Stop/target (if it could fire) |

### RegimeClassifier (rule-based, active)
| Column | Usage |
|--------|-------|
| `adx` / `adx_14` | Trend strength gate |
| `ema_20` | EMA slope calculation |
| `bb_width` | Volatility state |
| `volume` | Volume trend (accumulation/distribution) |
| `rsi_14` | Overbought/oversold extremes |
| `close` | Price level checks |

### HMMRegimeClassifier (active, blended)
| Column | Usage |
|--------|-------|
| `close` | Log return computation |
| `bb_width` | Volatility feature |
| `adx` | Trend strength feature |
| `volume` | Volume momentum (volume / rolling mean) |

---

## Full Column Classification

### EMA Variants
| Column | Classification | Reason |
|--------|---------------|--------|
| `ema_9` | **CORE** | TrendModel primary signal |
| `ema_20` | **CORE** | TrendModel secondary confirmation; RegimeClassifier slope |
| `ema_21` | **CORE** | TrendModel primary signal (EMA9 vs EMA21) |
| `ema_100` | **CORE** | TrendModel secondary confirmation |
| `ema_50` | **CONDITIONAL** | Used by BacktestEngine condition tree (user rules). Keep for backtesting, not scanning. |
| `ema_200` | **CONDITIONAL** | BacktestEngine only. Note: unreliable at 300 bars (needs 200+) |
| `ema_2` | **REMOVE** | Not consumed by any active signal or regime classifier |
| `ema_3` | **REMOVE** | Not consumed |
| `ema_5` | **REMOVE** | Not consumed |
| `ema_8` | **REMOVE** | Not consumed |
| `ema_10` | **REMOVE** | Not consumed |
| `ema_12` | **REMOVE** | Not consumed |
| `ema_26` | **REMOVE** | Not consumed by signal models (MACD internal, but ta library handles it internally) |
| `ema_27` | **REMOVE** | Not consumed |
| `ema_32` | **REMOVE** | Not consumed |
| `ema_55` | **REMOVE** | Not consumed |
| `ema_63` | **REMOVE** | Not consumed |

**EMA cost reduction: 17 computed → 4 CORE + 2 CONDITIONAL = remove 11 (65% reduction)**

### SMA Variants
| Column | Classification | Reason |
|--------|---------------|--------|
| `sma_20` | **CONDITIONAL** | BacktestEngine user rules |
| `sma_50` | **CONDITIONAL** | BacktestEngine user rules |
| `sma_200` | **CONDITIONAL** | BacktestEngine user rules. Unreliable at 300 bars. |
| `sma_2` | **REMOVE** | Not consumed |
| `sma_3` | **REMOVE** | Not consumed |
| `sma_5` | **REMOVE** | Not consumed |
| `sma_8` | **REMOVE** | Not consumed |
| `sma_9` | **REMOVE** | Not consumed |
| `sma_10` | **REMOVE** | Not consumed |
| `sma_12` | **REMOVE** | Not consumed |
| `sma_21` | **REMOVE** | Not consumed (EMA21 is used, not SMA21) |
| `sma_26` | **REMOVE** | Not consumed |
| `sma_27` | **REMOVE** | Not consumed |
| `sma_32` | **REMOVE** | Not consumed |
| `sma_55` | **REMOVE** | Not consumed |
| `sma_63` | **REMOVE** | Not consumed |
| `sma_100` | **REMOVE** | Not consumed (EMA100 is used) |

**SMA cost reduction: 17 computed → 3 CONDITIONAL = remove 14 (82% reduction)**

### WMA
| Column | Classification | Reason |
|--------|---------------|--------|
| `wma_20` | **REMOVE** | Not consumed by any active signal, regime classifier, or documented backtesting use case |

### ADX
| Column | Classification | Reason |
|--------|---------------|--------|
| `adx` / `adx_14` | **CORE** | TrendModel primary gate, RegimeClassifier, HMM feature |
| `adx_20` | **REMOVE** | Not consumed by any active model (adx_20 is computed but alias `adx` points to adx_14) |

### VWAP
| Column | Classification | Reason |
|--------|---------------|--------|
| `vwap` | **CONDITIONAL** | VWAPReversionModel (disabled). If VWAPReversionModel re-enabled, becomes CORE. |

### MACD
| Column | Classification | Reason |
|--------|---------------|--------|
| `macd` | **CORE** | TrendModel bonus strength signal |
| `macd_signal` | **CORE** | TrendModel MACD confirmation |
| `macd_hist` | **CONDITIONAL** | BacktestEngine user rules only; no active sub-model reads it |

### SuperTrend
| Column | Classification | Reason |
|--------|---------------|--------|
| `supertrend` (alias) | **CONDITIONAL** | BacktestEngine user rules only |
| `supertrend_5` | **CONDITIONAL** | BacktestEngine |
| `supertrend_10` | **CONDITIONAL** | BacktestEngine (source of `supertrend` alias) |
| `supertrend_15` | **CONDITIONAL** | BacktestEngine |

**Note:** SuperTrend computation involves iterative loop over all bars — one of the most expensive indicators. Three variants × 300 bars is substantial compute for backtest-only coverage.

### Ichimoku
| Column | Classification | Reason |
|--------|---------------|--------|
| `ichi_conversion` | **CONDITIONAL** | BacktestEngine user rules only |
| `ichi_base` | **CONDITIONAL** | BacktestEngine |
| `ichi_a` | **REMOVE** | Not in BacktestEngine INDICATOR_OPTIONS (not listed for user rules) |
| `ichi_b` | **REMOVE** | Not in BacktestEngine INDICATOR_OPTIONS |

### Stochastic
| Column | Classification | Reason |
|--------|---------------|--------|
| `stoch_rsi_k` | **CONDITIONAL** | BacktestEngine user rules |
| `stoch_rsi_d` | **CONDITIONAL** | BacktestEngine user rules |
| `stoch_k` | **CONDITIONAL** | BacktestEngine (listed as Stochastic K) |
| `stoch_d` | **CONDITIONAL** | BacktestEngine (listed as Stochastic D - not in options but symmetric) |

### RSI Variants
| Column | Classification | Reason |
|--------|---------------|--------|
| `rsi_14` | **CORE** | TrendModel, MomentumBreakout, VWAPReversion, MeanReversion, RegimeClassifier |
| `rsi_2` | **CONDITIONAL** | BacktestEngine user rules |
| `rsi_3` | **CONDITIONAL** | BacktestEngine user rules |
| `rsi_5` | **CONDITIONAL** | BacktestEngine user rules |
| `rsi_6` | **CONDITIONAL** | BacktestEngine user rules |
| `rsi_7` | **CONDITIONAL** | BacktestEngine user rules |
| `rsi_8` | **CONDITIONAL** | BacktestEngine user rules |
| `rsi_12` | **CONDITIONAL** | BacktestEngine user rules |
| `rsi_24` | **CONDITIONAL** | BacktestEngine user rules |

**RSI cost reduction for scanning: 9 computed → 1 CORE + 8 CONDITIONAL (backtest only)**

### ATR Variants
| Column | Classification | Reason |
|--------|---------------|--------|
| `atr_14` | **CORE** | All sub-models for stop/target computation |
| `atr_2` | **CONDITIONAL** | BacktestEngine user rules |
| `atr_3` | **CONDITIONAL** | BacktestEngine user rules |
| `atr_5` | **CONDITIONAL** | BacktestEngine user rules |
| `atr_6` | **CONDITIONAL** | BacktestEngine user rules |
| `atr_7` | **CONDITIONAL** | BacktestEngine user rules |
| `atr_8` | **CONDITIONAL** | BacktestEngine user rules |
| `atr_12` | **CONDITIONAL** | BacktestEngine user rules |
| `atr_24` | **CONDITIONAL** | BacktestEngine user rules |

**ATR cost reduction for scanning: 9 computed → 1 CORE + 8 CONDITIONAL**

### Bollinger Bands
| Column | Classification | Reason |
|--------|---------------|--------|
| `bb_upper` | **CONDITIONAL** | MeanReversionModel (disabled), BacktestEngine |
| `bb_lower` | **CONDITIONAL** | MeanReversionModel (disabled), BacktestEngine |
| `bb_mid` | **CONDITIONAL** | MeanReversionModel (disabled), BacktestEngine |
| `bb_width` | **CORE** | RegimeClassifier volatility state, HMM feature |
| `bb_pct` | **CONDITIONAL** | BacktestEngine only |

### Keltner Channels
| Column | Classification | Reason |
|--------|---------------|--------|
| `kc_upper` | **REMOVE** | Not in BacktestEngine INDICATOR_OPTIONS; no active consumer |
| `kc_mid` | **REMOVE** | Not consumed |
| `kc_lower` | **REMOVE** | Not consumed |

### Donchian Channels
| Column | Classification | Reason |
|--------|---------------|--------|
| `dc_upper` | **REMOVE** | Not in BacktestEngine INDICATOR_OPTIONS; no active consumer |
| `dc_mid` | **REMOVE** | Not consumed |
| `dc_lower` | **REMOVE** | Not consumed |

### Volume Indicators
| Column | Classification | Reason |
|--------|---------------|--------|
| `volume` | **CORE** | MomentumBreakout volume confirmation, RegimeClassifier, HMM |
| `obv` | **CONDITIONAL** | BacktestEngine user rules |
| `ad` | **REMOVE** | Not in BacktestEngine INDICATOR_OPTIONS; no active consumer |
| `mfi` | **CONDITIONAL** | BacktestEngine user rules |
| `cmf` | **REMOVE** | Not in BacktestEngine INDICATOR_OPTIONS; no active consumer |

### Momentum (non-RSI)
| Column | Classification | Reason |
|--------|---------------|--------|
| `momentum` (ROC 10) | **REMOVE** | Not consumed (duplicate of `roc`) |
| `roc` | **CONDITIONAL** | BacktestEngine user rules |
| `cci` | **CONDITIONAL** | BacktestEngine user rules |
| `williams_r` | **CONDITIONAL** | BacktestEngine user rules |
| `twap` | **CONDITIONAL** | BacktestEngine user rules |

### Market Structure
| Column | Classification | Reason |
|--------|---------------|--------|
| `pivot_p`, `pivot_r1/r2/r3`, `pivot_s1/s2/s3` | **REMOVE** | Pivot point columns computed by `_pivot_points()`. Not in BacktestEngine INDICATOR_OPTIONS. No active consumer verified. |
| `fib_*` (multiple columns) | **REMOVE** | Fibonacci levels from `_fibonacci_levels()`. Not in BacktestEngine INDICATOR_OPTIONS. No active consumer. |

---

## Quantified Waste Summary

### Columns computed on every scan cycle
| Category | Total Computed | CORE | CONDITIONAL | REMOVE | Waste % |
|----------|---------------|------|-------------|--------|---------|
| EMA | 17 | 4 | 2 | 11 | 65% |
| SMA | 17 | 0 | 3 | 14 | 82% |
| WMA | 1 | 0 | 0 | 1 | 100% |
| ADX | 2 | 1 | 0 | 1 | 50% |
| VWAP | 1 | 0 | 1 | 0 | 0% (model disabled) |
| MACD | 3 | 2 | 1 | 0 | 0% |
| SuperTrend | 4 | 0 | 4 | 0 | 0% (backtest only) |
| Ichimoku | 4 | 0 | 2 | 2 | 50% |
| Stochastic | 4 | 0 | 4 | 0 | 0% |
| RSI | 9 | 1 | 8 | 0 | 0% |
| ATR | 9 | 1 | 8 | 0 | 0% |
| Bollinger | 5 | 1 | 4 | 0 | 0% |
| Keltner | 3 | 0 | 0 | 3 | 100% |
| Donchian | 3 | 0 | 0 | 3 | 100% |
| Volume indicators | 4 | 0 | 2 | 2 | 50% |
| Momentum/other | 5 | 0 | 4 | 1 | 20% |
| Pivot Points | ~8 | 0 | 0 | 8 | 100% |
| Fibonacci | ~8 | 0 | 0 | 8 | 100% |
| **TOTAL** | **~112** | **10** | **43** | **54** | **~48%** |

**Approximately 54 of ~112 computed columns are completely unused by any active or recoverable code path.** These are REMOVE-level waste.

An additional **43 CONDITIONAL columns** are only used by BacktestEngine (user-defined rules) and disabled models. For the live scanning path, these are also unused — they cost CPU and memory on every scan cycle but contribute zero alpha.

**Conclusion: The live scan path requires only ~10 CORE columns. Currently computing ~112 columns = 11× over-computation per scan cycle.**

---

## Recommended Action Per Category

### Immediate Removals (no signal loss risk)
1. **Keltner Channels** (3 cols) — REMOVE immediately. No consumer.
2. **Donchian Channels** (3 cols) — REMOVE immediately. No consumer.
3. **Pivot Points** (~8 cols) — REMOVE immediately. No consumer in any live path.
4. **Fibonacci levels** (~8 cols) — REMOVE immediately. No consumer.
5. **WMA** (1 col) — REMOVE immediately.
6. **Momentum/ROC duplicate** — REMOVE `momentum` column (keep `roc`).
7. **EMA 2/3/5/8/10/12/26/27/32/55/63** (11 cols) — REMOVE. None consumed by active signals.
8. **SMA 2/3/5/8/9/10/12/21/26/27/32/55/63/100** (14 cols) — REMOVE. None consumed by active signals.
9. **ADX-20** (1 col) — REMOVE. `adx` alias points to adx_14; adx_20 is never read.
10. **Ichimoku A/B** (2 cols) — REMOVE. Not in BacktestEngine options.
11. **A/D index and CMF** (2 cols) — REMOVE. Not consumed.

**Total immediate removal: ~55 columns. ~49% reduction in indicator columns with zero impact on any live signal.**

### Architecture Recommendation: Split Indicator Computation
The fundamental fix is to separate indicator computation into two modes:

**Mode A — Scan mode (live scanning):** Compute only the 10 CORE columns needed for live signal generation:
- `ema_9`, `ema_20`, `ema_21`, `ema_100` (TrendModel)
- `adx_14` (TrendModel, RegimeClassifier)
- `rsi_14` (TrendModel, MomentumBreakout, RegimeClassifier)
- `macd`, `macd_signal` (TrendModel)
- `atr_14` (all models, stop/target)
- `bb_width` (RegimeClassifier, HMM)

Plus CONDITIONAL for enabled models: `vwap` (if VWAPReversion re-enabled), `bb_upper/lower/mid` (if MeanReversion re-enabled).

**Mode B — Backtest mode (condition tree evaluation):** Compute all CONDITIONAL columns that the BacktestEngine exposes for user-defined rules. Only invoked when BacktestEngine is running, not during live scan cycles.

This split achieves:
- ~92% reduction in indicator computation columns during live scanning
- No change in backtesting capability
- ~5–10ms saved per symbol per scan cycle (5 symbols × 10ms ≈ 50ms saved per hour)

### Secondary Recommendation: Incremental Indicator Update
For the 10 CORE columns, rather than recomputing on 300 bars every scan cycle:
- Cache the last indicator state (e.g., EMA = previous EMA × (1-k) + new_close × k)
- Append only the new candle's values
- Full 300-bar recompute only needed on startup or after a data gap

Incremental EMA update is O(1) vs. O(N). Same for ATR. RSI requires a rolling mean which is O(window) but cached. This reduces per-cycle computation from O(300 × 10) to O(10) for the CORE set.

---

*Next: Phase 3 — Multi-Asset Architecture Validation*
