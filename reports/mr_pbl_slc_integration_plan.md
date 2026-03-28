# MR / PBL / SLC Integration Plan — NexusTrader v1.3
**Date:** 2026-03-27
**Author:** Phase 5/6 Backtest Investigation
**Status:** Ready for implementation

---

## 1. Phase 5 Findings Summary

### 1.1 Validated Configuration

After 7 backtest versions (v1–v7) the closest reproducible match to the original target metrics is:

| Parameter | Value |
|-----------|-------|
| **Symbols** | BTC/USDT (PBL + SLC), SOL/USDT (SLC), ETH/USDT (SLC) |
| **MR strategy** | **EXCLUDED** (BTC MR PF=0.88–0.98 across all configs; negative contributor) |
| **pos_frac** | **35%** of current equity per position |
| **max_heat** | 80% (≤ 2 simultaneous positions at 35% each) |
| **max_positions** | 10 |
| **max_per_asset** | 3 |
| **Fees** | Matched at zero fees; 0.04%/side reduces CAGR to ~27% |

**Final best result** (Period: 2022-03-22 → 2026-03-21, 4.00 years, $100,000):

| Metric | Backtest Result | Original Target | Gap |
|--------|-----------------|-----------------|-----|
| CAGR | **50.41%** | ~50.8% | 0.4pp ✓ |
| Profit Factor | **1.2975** | ~1.361 | 0.064 (persistent) |
| Win Rate | **61.11%** | — | — |
| Max Drawdown | **-20.66%** | ~-29% | 8pp |
| Trades | **1,476** | ~2,116 | 640 fewer |

### 1.2 Why the PF and MaxDD Gaps Persist

- **PF gap (1.30 vs 1.361)**: SLC's structural PF ceiling is ~1.28–1.30 with the locked parameters (ADX≥28, BEAR_TREND regime, 10-bar swing low). The original target may have used slightly different parameters, a different date range, or a different PF calculation method. The gap is not closable without changing locked strategy definitions.
- **MaxDD gap (-20.7% vs -29%)**: The original likely used pos_frac≥40% or included XRP/BNB (5-symbol SLC). With pos_frac=40% and 5 symbols: CAGR=52.55%, MaxDD=-26.62%.
- **Trade count gap (1,476 vs 2,116)**: At pos_frac=35%, the heat constraint limits to ≤2 simultaneous positions, rejecting more signals. The original may have used pos_frac=20% with a different overall configuration.

### 1.3 Per-Strategy Performance (final validated config)

| Strategy | N | WR | PF | Net PnL |
|----------|---|----|----|---------|
| **PBL** (BTC only) | 143 | 55.2% | 1.617 | +$38,555 |
| **SLC** (BTC+SOL+ETH) | 1,333 | 61.7% | 1.282 | +$369,729 |
| **SLC by symbol** | | | | |
| — BTC | 681 | 59.9% | 1.241 | +$114,455 |
| — SOL | 511 | 62.4% | 1.396 | +$241,365 |
| — ETH | 284 | 61.6% | 1.182 | +$52,463 |

**MR (BTC only, excluded from config): n=119 signals over 4 years, PF=0.88–0.98 → do not integrate.**

---

## 2. Strategy Definitions (Locked)

### 2.1 PBL — Pullback Long

- **Timeframe**: 30m primary, 4h HTF gate
- **Regime**: `bull_trend` (NexusTrader) = BULL_TREND(1) in backtest
- **Entry conditions** (signal at bar t, entry at bar t+1 open):
  - 30m BULL_TREND regime
  - `|close − EMA50_30m| ≤ 0.5 × ATR14_30m` (proximity to EMA50)
  - Rejection candle: `close > open`, `lower_wick > upper_wick`, `lower_wick > body`
  - `RSI14_30m > 40`
  - **4h HTF gate**: `4h_close > 4h_EMA50` (merged backward — no lookahead)
- **Stop**: `signal_close − 2.5 × ATR14_30m`
- **Target**: `signal_close + 3.0 × ATR14_30m`
- **Direction**: LONG only
- **Symbols**: BTC/USDT only (BULL_TREND is rare on altcoins in this period)

### 2.2 SLC — Swing Low Continuation

- **Timeframe**: 1h bars; positions managed on 30m tick grid
- **Regime**: `bear_trend` (NexusTrader) = BEAR_TREND(2) in backtest
- **Entry conditions** (signal at 1h bar t, entry at 1h bar t+1 open):
  - 1h BEAR_TREND regime
  - `ADX14_1h ≥ 28`
  - `close_1h < min(close_1h[-10:])` — new 10-bar closing low (prior bar lookback, no lookahead)
- **Stop**: `signal_close + 2.5 × ATR14_1h`
- **Target**: `signal_close − 2.0 × ATR14_1h`
- **Direction**: SHORT only
- **Symbols**: BTC/USDT, SOL/USDT, ETH/USDT (XRP and BNB optional — see section 4.3)

### 2.3 MR — Mean Reversion (NOT integrated)

The backtest MR has consistent PF < 1.0 with this regime definition. The existing `MeanReversionModel` in NexusTrader has different parameters and is also archived (`disabled_models`). **No action needed for MR.**

---

## 3. Implementation Plan

### 3.1 New Sub-Model Files

Create two new files in `core/signals/sub_models/`:

#### `pullback_long_model.py`

```python
class PullbackLongModel(BaseSubModel):
    """
    Pullback Long — 30m bull_trend + 4h HTF gate.
    BTC/USDT only.
    """
    ACTIVE_REGIMES = ["bull_trend"]

    REGIME_AFFINITY = {
        "bull_trend": 1.0, "bear_trend": 0.0, "ranging": 0.0,
        "volatility_expansion": 0.4, "volatility_compression": 0.1,
        "uncertain": 0.15, "crisis": 0.0, "liquidation_cascade": 0.0,
        "squeeze": 0.2, "recovery": 0.7, "accumulation": 0.4, "distribution": 0.0,
    }

    # No entry buffer — enter at next bar open
    ENTRY_BUFFER_ATR: float = 0.0

    @property
    def name(self) -> str:
        return "pullback_long"

    def evaluate(self, symbol, df, regime, timeframe):
        # symbol filter: BTC/USDT only
        if "BTC" not in symbol:
            return None
        if len(df) < 60:
            return None

        # 4h HTF data must be injected into df (see section 3.3)
        c4h   = self._col(df, "c4h_close")    # 4h close (merged_asof)
        e50_4h= self._col(df, "ema50_4h")     # 4h EMA50 (merged_asof)
        if c4h is None or e50_4h is None:
            return None

        atr   = self._atr(df, 14)
        close = float(df["close"].iloc[-1])
        open_ = float(df["open"].iloc[-1])
        high  = float(df["high"].iloc[-1])
        low   = float(df["low"].iloc[-1])
        e50   = self._col(df, "ema_50")
        rsi   = self._col(df, "rsi_14")

        if None in (e50, rsi):
            return None

        # Proximity to EMA50
        if abs(close - e50) > 0.5 * atr:
            return None

        # Rejection candle (bullish close + lower wick dominant)
        body = abs(close - open_)
        lw   = min(open_, close) - low
        uw   = high - max(open_, close)
        if not (close > open_ and lw > uw and lw > body):
            return None

        # RSI filter
        if rsi <= 40:
            return None

        # 4h HTF: close must be above 4h EMA50
        if c4h <= e50_4h:
            return None

        sl = close - 2.5 * atr
        tp = close + 3.0 * atr

        # Validity check (entry at next open should be between sl and tp)
        # Guard: do not validate here — scanner handles this at fill time

        strength = 0.55  # base; tune in production

        return ModelSignal(
            symbol=symbol, model_name=self.name, direction="long",
            strength=strength, entry_price=close, stop_loss=sl,
            take_profit=tp, timeframe=timeframe, regime=regime,
            rationale=(f"PBL: price {close:.2f} within 0.5×ATR of EMA50 {e50:.2f}, "
                       f"rejection candle, RSI={rsi:.1f}, 4h {c4h:.2f}>EMA50_4h {e50_4h:.2f}"),
            atr_value=atr,
        )
```

#### `swing_low_continuation_model.py`

```python
class SwingLowContinuationModel(BaseSubModel):
    """
    Swing Low Continuation — 1h bear_trend.
    BTC/USDT, SOL/USDT, ETH/USDT.
    SHORT only — new 10-bar closing low with ADX≥28.
    """
    ACTIVE_REGIMES = ["bear_trend"]
    _ALLOWED_SYMBOLS = {"BTC/USDT", "SOL/USDT", "ETH/USDT"}

    REGIME_AFFINITY = {
        "bull_trend": 0.0, "bear_trend": 1.0, "ranging": 0.0,
        "volatility_expansion": 0.5, "volatility_compression": 0.1,
        "uncertain": 0.15, "crisis": 0.3, "liquidation_cascade": 0.4,
        "squeeze": 0.1, "recovery": 0.0, "accumulation": 0.0, "distribution": 0.6,
    }

    ENTRY_BUFFER_ATR: float = 0.0

    @property
    def name(self) -> str:
        return "swing_low_continuation"

    def evaluate(self, symbol, df, regime, timeframe):
        if symbol not in self._ALLOWED_SYMBOLS:
            return None
        if len(df) < 30:
            return None

        adx   = self._col(df, "adx")
        if adx is None or adx < 28:
            return None

        close = float(df["close"].iloc[-1])
        # 10-bar swing low: minimum of the prior 10 closes (excluding current)
        if len(df) < 11:
            return None
        sw10 = float(df["close"].iloc[-11:-1].min())

        if close >= sw10:
            return None

        atr = self._atr(df, 14)
        sl  = close + 2.5 * atr
        tp  = close - 2.0 * atr

        strength = 0.55  # base; tune in production

        return ModelSignal(
            symbol=symbol, model_name=self.name, direction="short",
            strength=strength, entry_price=close, stop_loss=sl,
            take_profit=tp, timeframe=timeframe, regime=regime,
            rationale=(f"SLC: {symbol} 1h close {close:.4f} < 10-bar low {sw10:.4f}, "
                       f"ADX={adx:.1f}≥28, bear_trend"),
            atr_value=atr,
        )
```

### 3.2 Register Models in `signal_generator.py`

In `SignalGenerator.__init__()`, add alongside existing models:

```python
from core.signals.sub_models.pullback_long_model import PullbackLongModel
from core.signals.sub_models.swing_low_continuation_model import SwingLowContinuationModel

# In sub_models list:
self._sub_models = [
    TrendModel(),
    MomentumBreakoutModel(),
    SentimentModel(),
    FundingRateModel(),
    PullbackLongModel(),           # NEW
    SwingLowContinuationModel(),   # NEW
]
```

### 3.3 4h HTF Data Injection for PBL

The PBL model requires 4h columns (`c4h_close`, `ema50_4h`) merged into the 30m DataFrame. The injection point is `scanner.py` / the feature preparation step:

**Option A — In scanner indicator pipeline** (recommended):
In `_prepare_indicators()` or the indicator library, when 4h data is available, merge backward into the 30m frame:

```python
# In indicator_library.calculate_all() or a new calculate_htf_merge():
if '4h' in self._htf_data and symbol in self._htf_data['4h']:
    df4h = self._htf_data['4h'][symbol].copy()
    df4h['ema50_4h'] = df4h['close'].ewm(span=50, adjust=False).mean()
    df4h_m = df4h[['close','ema50_4h']].rename(columns={'close':'c4h_close'})
    df = pd.merge_asof(df.sort_index(), df4h_m.sort_index(),
                       left_index=True, right_index=True, direction='backward')
```

**Option B — Inside PullbackLongModel.evaluate()** (self-contained):
Request 4h data via exchange manager directly. Higher latency, simpler code path.

Recommended: **Option A** to keep data fetching in one place.

### 3.4 SLC Timeframe Routing

The SLC evaluates on **1h bars**, not 30m. The existing scanner already supports multi-timeframe evaluation. Ensure `SwingLowContinuationModel` is only called when timeframe == "1h":

```python
# In signal_generator.evaluate_all(symbol, df, regime, timeframe):
for model in self._sub_models:
    if model.name == "swing_low_continuation" and timeframe != "1h":
        continue
    if model.name == "pullback_long" and timeframe != "30m":
        continue
```

Or add a `REQUIRED_TIMEFRAME` class attribute to `BaseSubModel` and filter in the caller.

---

## 4. Configuration Changes

### 4.1 `config.yaml` — Enable New Models

```yaml
# Add to disabled_models ONLY if you want to disable — leave absent to enable
disabled_models:
  - mean_reversion         # existing — keep disabled (PF<1.0)
  - liquidity_sweep        # existing — keep disabled
  - vwap_reversion         # existing — keep disabled
  - order_book             # existing — keep disabled
  # pullback_long          ← NOT listed → enabled by default
  # swing_low_continuation ← NOT listed → enabled by default

# Add model parameters section:
models:
  pullback_long:
    ema50_proximity_atr: 0.5       # |close - EMA50| ≤ mult × ATR14
    rsi_min: 40
    sl_atr_mult: 2.5
    tp_atr_mult: 3.0
    htf_timeframe: 4h
    allowed_symbols: ["BTC/USDT"]
    strength_base: 0.55

  swing_low_continuation:
    adx_min: 28
    sw_lookback: 10                # 10-bar closing low
    sl_atr_mult: 2.5
    tp_atr_mult: 2.0
    allowed_symbols: ["BTC/USDT", "SOL/USDT", "ETH/USDT"]
    strength_base: 0.55
```

### 4.2 Capital Allocation — `pos_frac`

The backtest validated `pos_frac = 35%`. NexusTrader does NOT currently have a direct `pos_frac` setting — it uses `risk_pct_per_trade` (Kelly-fraction of equity risked on stop distance). The relationship is:

```
pos_frac = position_size_usdt / equity
risk_pct = (entry - stop_loss) / entry × pos_frac  [for longs]
```

For SLC: entry ≈ close, stop = close + 2.5×ATR → risk on position = 2.5×ATR/close.
With pos_frac=35%: `risk_pct = (2.5 × ATR/close) × 0.35`

For BTC at ATR/close ≈ 0.02 (2%): `risk_pct = 2.5 × 0.02 × 0.35 = 1.75%`.

**This is not a fixed risk_pct** — it varies with ATR/price ratio. The correct approach is to implement a dedicated `pos_frac` sizing path:

```python
# In PositionSizer.calculate_risk_based():
# Add a new method or mode:
def calculate_pos_frac(self, equity: float, pos_frac: float = 0.35,
                       max_heat: float = 0.80, open_positions: int = 0) -> float:
    """
    Size = pos_frac × equity, capped by heat.
    max_allowed_positions = floor(max_heat / pos_frac)
    """
    deployed = open_positions * (equity * pos_frac)  # approximate
    heat = deployed / equity if equity > 0 else 1.0
    if heat + pos_frac > max_heat:
        return 0.0  # heat limit reached
    return min(equity * pos_frac, equity * (max_heat - heat))
```

**Config key to add** (gated, does not affect existing Phase 1 sizing):
```yaml
mr_pbl_slc:
  enabled: false          # GATE: false until Phase 2 advancement
  pos_frac: 0.35          # 35% of current equity per trade
  max_heat: 0.80          # 80% max capital deployed
  max_positions: 10
  max_per_asset: 3
```

### 4.3 Watchlist

All 5 symbols (BTC, ETH, SOL, XRP, BNB) are already in the Default watchlist. No change needed. XRP and BNB will not receive SLC signals from `SwingLowContinuationModel` because they are not in `_ALLOWED_SYMBOLS`. To enable them, add to the list and re-evaluate.

---

## 5. Regime Mapping

The backtest used the NexusTrader 6-regime HMM labels. The mapping to NexusTrader regime strings:

| Backtest Integer | Backtest Name | NexusTrader String | % of 30m bars (BTC) |
|-----------------|---------------|-------------------|----------------------|
| 0 | SIDEWAYS | `ranging` | 51.1% |
| 1 | BULL_TREND | `bull_trend` | 23.4% |
| 2 | BEAR_TREND | `bear_trend` | 24.4% |
| 3 | BULL_EXPANSION | `volatility_expansion` | 0.6% |
| 4 | BEAR_EXPANSION | `volatility_expansion` | 0.5% |
| 5 | CRASH_PANIC | `crisis` | 0.04% |

The new models use ACTIVE_REGIMES that match exactly:
- `PullbackLongModel.ACTIVE_REGIMES = ["bull_trend"]` → fires when regime = BULL_TREND(1)
- `SwingLowContinuationModel.ACTIVE_REGIMES = ["bear_trend"]` → fires when regime = BEAR_TREND(2)

---

## 6. Testing Requirements

### 6.1 New Unit Tests

**File**: `tests/unit/test_mr_pbl_slc_models.py`

Required tests (minimum):
1. `test_pbl_fires_in_bull_trend_only` — PBL returns None in ranging/bear regimes
2. `test_pbl_requires_ema50_proximity` — rejects signals when |close-EMA50| > 0.5×ATR
3. `test_pbl_requires_rejection_candle` — rejects doji/bearish candle
4. `test_pbl_requires_4h_htf_gate` — rejects when 4h close < 4h EMA50
5. `test_pbl_btc_only` — returns None for SOL/USDT, ETH/USDT
6. `test_slc_fires_in_bear_trend_only` — SLC returns None in bull/ranging regimes
7. `test_slc_requires_adx_28` — rejects when ADX < 28
8. `test_slc_requires_new_swing_low` — rejects when close ≥ 10-bar min
9. `test_slc_symbol_filter` — returns None for XRP/USDT, BNB/USDT
10. `test_slc_sl_tp_calculation` — verifies SL = close + 2.5×ATR, TP = close - 2.0×ATR
11. `test_pbl_sl_tp_calculation` — verifies SL = close - 2.5×ATR, TP = close + 3.0×ATR
12. `test_mr_pbl_slc_config_gate` — when `mr_pbl_slc.enabled: false`, sizing returns 0

### 6.2 Integration Tests

**File**: `tests/integration/test_mr_pbl_slc_integration.py`

1. `test_signal_flow_btc_bull` — inject a synthetic BTC/USDT 30m frame with bull_trend regime; verify PBL signal reaches ConfluenceScorer
2. `test_signal_flow_sol_bear` — inject a synthetic SOL/USDT 1h frame with bear_trend; verify SLC signal is generated
3. `test_heat_gate_blocks_3rd_position` — with pos_frac=35%, verify 3rd simultaneous position is rejected (2×35%=70%, adding 35% would exceed 80%)

### 6.3 Pre-session Checklist Addition

Add to `CLAUDE.md` pre-session checklist:
```bash
pytest tests/unit/test_mr_pbl_slc_models.py -v        # N tests, 0 failures required
```

---

## 7. Activation Gating Strategy

**Phase 1 (current)**: Do NOT activate `mr_pbl_slc` sizing. New models can be installed and *monitored in signal logs only* with `mr_pbl_slc.enabled: false`.

**Activation criteria** (manual only):
1. Current NexusTrader Phase 1 must have completed 50+ demo trades with WR≥45% and PF≥1.10
2. New model test suite at zero failures
3. Set `mr_pbl_slc.enabled: true` in config.yaml
4. Start with BTC PBL only for the first 20 trades, then add SLC

This gated approach prevents the new 35%-pos_frac allocation from conflicting with the existing 0.5% risk system.

---

## 8. Implementation Priority Order

1. **Week 1**: Write `PullbackLongModel`, `SwingLowContinuationModel` — unit tests all passing
2. **Week 2**: Wire 4h HTF data injection into indicator pipeline; integration tests passing
3. **Week 3**: Add `mr_pbl_slc` config section; implement `calculate_pos_frac()` in PositionSizer
4. **Week 4**: Enable signal logging (disabled execution) for 2 weeks of paper signal observation
5. **After Phase 1 completion**: Enable execution with BTC PBL first; add SLC after 20 PBL trades

---

## 9. Known Risks and Open Questions

| Item | Risk | Mitigation |
|------|------|-----------|
| pos_frac=35% vs risk_pct=0.5% | Sizing conflict between existing system and new | Hard gate via `mr_pbl_slc.enabled` flag |
| SLC short bias in bull markets | BEAR_TREND accounts for 24.4% of bars; PBL captures bull | Regime gate ensures mutual exclusion |
| 4h HTF lookahead | If merge is not strictly backward-looking, PBL will be snooped | Always use `merge_asof(direction='backward')` |
| Fee sensitivity | CAGR drops from 50.4% to ~27% at 0.04%/side | Target maker fills only for SLC; consider limit orders |
| MR exclusion | Existing MeanReversionModel (disabled) vs new backtest MR spec | Do not conflate — backtest MR is a separate design; keep both disabled |
| MaxDD -20.7% vs expected -29% | System is actually less risky than original target | Accept — lower drawdown is acceptable; monitor for underperformance |

---

## 10. File Checklist

New files to create:
- [ ] `core/signals/sub_models/pullback_long_model.py`
- [ ] `core/signals/sub_models/swing_low_continuation_model.py`
- [ ] `tests/unit/test_mr_pbl_slc_models.py`
- [ ] `tests/integration/test_mr_pbl_slc_integration.py`

Files to modify:
- [ ] `core/signals/signal_generator.py` — register new models
- [ ] `core/meta_decision/position_sizer.py` — add `calculate_pos_frac()` method
- [ ] `config.yaml` — add `mr_pbl_slc:` block + model parameter sections
- [ ] `CLAUDE.md` — update pre-session checklist + Architecture section

Files to NOT modify (no changes needed):
- `core/signals/sub_models/mean_reversion_model.py` — archived, different spec, leave as-is
- `core/regime/hmm_regime_classifier.py` — already produces correct labels
- `core/scanning/scanner.py` — no changes; models route themselves via ACTIVE_REGIMES

---

*End of Phase 6 Integration Plan*
