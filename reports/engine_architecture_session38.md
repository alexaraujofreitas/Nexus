# NexusTrader — Canonical Backtest Engine Architecture
**Session 38 | 2026-03-28**

---

## 1. Overview

NexusTrader has ONE canonical backtest engine. It is defined by the classes and
modules used in **live production scanning**. Any backtest that diverges from these
classes is invalid and cannot be used to promote parameters to production.

The reference implementation is:
```
scripts/mr_pbl_slc_research/backtest_v9_system.py
```

As of Session 38 the canonical engine is also exposed as an importable module at:
```
research/engine/backtest_runner.py   ← BacktestRunner (canonical, parameterisable)
```

---

## 2. Canonical Pipeline (full flow)

```
┌─────────────────────────────────────────────────────────────────────────────┐
│  DATA LAYER                                                                  │
│  ─────────                                                                   │
│  backtest_data/{SYM}_{TF}.parquet                                            │
│    BTC/USDT, SOL/USDT, ETH/USDT                                              │
│    Timeframes: 30m (primary) · 4h (PBL HTF) · 1h (SLC context)              │
│  Master timeline: BTC/USDT 30m index (all other symbols aligned to it)       │
│  Date range (IS): 2022-03-22 → 2026-03-21 (4 years)                          │
└──────────────────────────┬──────────────────────────────────────────────────┘
                           │ pandas DataFrames
┌──────────────────────────▼──────────────────────────────────────────────────┐
│  INDICATOR LAYER                                                             │
│  ────────────────                                                            │
│  30m:  core.features.indicator_library.calculate_all(df)                     │
│  4h:   core.features.indicator_library.calculate_scan_mode(df)               │
│  1h:   core.features.indicator_library.calculate_scan_mode(df)               │
│  Precomputed ONCE per session and cached in memory                           │
└──────────────────────────┬──────────────────────────────────────────────────┘
                           │ indicator-enriched DataFrames
┌──────────────────────────▼──────────────────────────────────────────────────┐
│  REGIME LAYER                                                                │
│  ────────────                                                                │
│  ResearchRegimeClassifier  (core/regime/research_regime_classifier.py)       │
│    classify_series(df_30m) → int8 array (0=SIDEWAYS…5=CRASH)                 │
│    classify_series(df_1h)  → int8 array (BEAR_TREND used by SLC)            │
│    regime_to_string(int)   → NexusTrader regime str ("bull_trend" etc.)      │
│    ATR: tr.ewm(span=14)  |  Hysteresis: 2-pass backward merge of runs < 3   │
│  Pre-classified for full series; looked up per-bar during simulation         │
│                                                                              │
│  NexusTrader RegimeClassifier (HMM + rule-based)                            │
│    Only used for non-PBL/SLC models (trend, momentum_breakout, etc.)        │
│    NOT used as primary gate for PBL or SLC                                  │
└──────────────────────────┬──────────────────────────────────────────────────┘
                           │ regime string per bar
┌──────────────────────────▼──────────────────────────────────────────────────┐
│  SIGNAL GENERATION                                                           │
│  ─────────────────                                                           │
│  core.signals.signal_generator.SignalGenerator                               │
│    .generate(symbol, df_window, regime_str, timeframe, context)              │
│                                                                              │
│  PBL path (per 30m bar where res_regime_30m == BULL_TREND):                 │
│    generate(sym, df_window[350 bars], "bull_trend", "30m",                  │
│             context={"df_4h": df_4h_last_60_bars})                          │
│    → PullbackLongModel.evaluate() fires if:                                  │
│        |close − EMA50| ≤ ema_prox_atr_mult × ATR14                          │
│        rejection candle (close>open, lower_wick>upper_wick, lw>body)        │
│        RSI14 > rsi_min                                                       │
│        4h EMA20 > 4h EMA50                                                  │
│                                                                              │
│  SLC path (per 30m bar where res_regime_1h == BEAR_TREND):                  │
│    generate(sym, df_window[350 bars], "bear_trend", "30m",                  │
│             context={"df_1h": df_1h_last_150_bars})                         │
│    → SwingLowContinuationModel.evaluate() fires if:                         │
│        ADX_1h > adx_min                                                      │
│        close_1h < min(prev swing_bars closes on 1h)                         │
│                                                                              │
│  Gate: SignalGenerator.ACTIVE_REGIMES is the sole regime filter.             │
│    PBL: ACTIVE_REGIMES=["bull_trend"]   (blocks unless regime=="bull_trend") │
│    SLC: ACTIVE_REGIMES=["bear_trend"]   (blocks unless regime=="bear_trend") │
└──────────────────────────┬──────────────────────────────────────────────────┘
                           │ ModelSignal (direction, sl, tp, atr_value)
┌──────────────────────────▼──────────────────────────────────────────────────┐
│  SIZING                                                                      │
│  ──────                                                                      │
│  core.meta_decision.position_sizer.PositionSizer                            │
│    .calculate_pos_frac(equity, open_count, open_by_symbol, symbol)           │
│    deployed_est = open_count × POS_FRAC × equity                            │
│    heat = (deployed_est + new_size) / equity                                 │
│    Rejects if heat > MAX_HEAT (0.80) or open_count >= MAX_POSITIONS (10)    │
│    Returns: size_usdt = equity × POS_FRAC  (0.35 = 35%)                    │
└──────────────────────────┬──────────────────────────────────────────────────┘
                           │ size_usdt
┌──────────────────────────▼──────────────────────────────────────────────────┐
│  EXECUTION                                                                   │
│  ─────────                                                                   │
│  PendingEntry buffer (mirrors PaperExecutor partial-fill logic):             │
│    Signal fires at bar i close → buffered in pending_entries[sym]            │
│    Fill attempted at bar i+1 OPEN with validation:                           │
│      Long:  sl < next_bar_open < tp  → fill at open × (1 + cost_per_side)  │
│      Short: tp < next_bar_open < sl  → fill at open × (1 − cost_per_side)  │
│    SL/TP check each subsequent bar:                                          │
│      Long exit: if low ≤ sl → exit at sl × (1 − cost_per_side)             │
│             or: if high ≥ tp → exit at tp × (1 − cost_per_side)            │
│      Short exit: symmetric                                                   │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## 3. Execution Assumptions (LOCKED — do not modify without new baseline)

| Assumption | Value | Justification |
|---|---|---|
| Initial capital | $100,000 USDT | Backtest standard |
| Position fraction | 35% of equity | `POS_FRAC = 0.35` |
| Max heat | 80% | `MAX_HEAT = 0.80` |
| Max open positions | 10 | `MAX_POSITIONS = 10` |
| Cost per side | 0.04% (Scenario B) | Bybit maker fee |
| Master timeline | BTC/USDT 30m only | `master_ts = list(btc_30m.index)` |
| Warmup bars | 120 | `warmup_bars = 120` |
| 30m window for signals | 350 bars | `MODEL_LOOKBACK = 350` |
| 4h window for PBL HTF | 60 bars | `HTF_LOOKBACK = 60` |
| 1h window for SLC | 150 bars (inclusive) | `SLC_1H_LOOKBACK = 150` |
| Min bars before signal | 70 | `if len(df_window) < 70: continue` |
| Entry timing | Next bar open | pending_entries buffer |
| SL/TP check | Bar's H/L | same-bar fill+exit possible |

---

## 4. Key Architectural Rules

### Rule 1 — ResearchRegimeClassifier is the PBL/SLC gate
The ResearchRegimeClassifier (`core/regime/research_regime_classifier.py`) provides
the regime string passed to `SignalGenerator.generate()` for both PBL and SLC. The
NexusTrader HMM+rule RegimeClassifier is **not** used as the PBL/SLC gate.

### Rule 2 — ACTIVE_REGIMES is the sole filter
`PullbackLongModel.ACTIVE_REGIMES = ["bull_trend"]`
`SwingLowContinuationModel.ACTIVE_REGIMES = ["bear_trend"]`

These are checked by `SignalGenerator.generate()` before calling `evaluate()`. No
regime integers are injected via `context` — regime flows through the `regime`
parameter only.

### Rule 3 — Dual generate() calls (separate regime strings)
PBL and SLC use different regime strings derived from different timeframes:
```
PBL: generate(sym, df_30m, research_regime_to_string(res_regime_30m), "30m", ...)
SLC: generate(sym, df_30m, research_regime_to_string(res_regime_1h), "1h", ...)
```

### Rule 4 — Parameter injection via settings
All tunable parameters are read from `config.settings` at evaluation time.
To inject custom values for research: `settings.set(key, value)` before calling
`generate()`. Do NOT call `settings.save()` during research runs.

### Rule 5 — No data versioning bypass
The `backtest_data/` parquet files are locked at the dataset fingerprint in
`research/engine/baseline_registry.json`. Any change to source data requires
re-running the baseline and updating the registry.

---

## 5. Parameter Registry (tunable for Research Lab)

### PBL Parameters
| Key | Default | Range | Notes |
|---|---|---|---|
| `mr_pbl_slc.pullback_long.ema_prox_atr_mult` | 0.5 | 0.30–0.80 | EMA50 proximity gate |
| `mr_pbl_slc.pullback_long.sl_atr_mult` | 2.5 | 1.5–4.0 | Stop-loss distance |
| `mr_pbl_slc.pullback_long.tp_atr_mult` | 3.0 | 2.0–5.0 | Take-profit distance |
| `mr_pbl_slc.pullback_long.rsi_min` | 40.0 | 25.0–55.0 | RSI entry gate |

### SLC Parameters
| Key | Default | Range | Notes |
|---|---|---|---|
| `mr_pbl_slc.swing_low_continuation.adx_min` | 28.0 | 20.0–35.0 | Trend strength gate |
| `mr_pbl_slc.swing_low_continuation.swing_bars` | 10 | 5–15 | Swing lookback bars |
| `mr_pbl_slc.swing_low_continuation.sl_atr_mult` | 2.5 | 1.5–4.0 | Stop-loss distance |
| `mr_pbl_slc.swing_low_continuation.tp_atr_mult` | 2.0 | 1.5–3.5 | Take-profit distance |

---

## 6. Validated Baseline (IS Period)

| Metric | Value |
|---|---|
| Period | 2022-03-22 → 2026-03-21 |
| Symbols | BTC/USDT, SOL/USDT, ETH/USDT |
| **Combined n** | **1,731** |
| **PF (zero fees)** | **1.3797** |
| **PF (0.04%/side)** | **≈1.276** |
| **WR** | **56.1%** |
| CAGR (zero fees) | 67.5% |
| MaxDD | −16.4% |

Source: current `backtest_v9_system.py` run (2026-03-28). Stored v9 JSON (1745/1.2682) is stale.
