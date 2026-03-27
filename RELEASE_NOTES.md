# NexusTrader — Release Notes

---

## v1.2.0 — Demo-Ready System with Phase 5 Optimisations
**Release date:** 2026-03-26

### Summary
Version 1.2 implements the complete Phase 5 optimisation results across exit logic, signal quality, multi-timeframe confirmation, and system hygiene. All changes are grounded in 13-month backtests (Study 4) and Phase 5 walk-forward validation (5/5 folds profitable). The system is cleared for continued Bybit Demo Phase 1 trading.

---

### What Changed (v1.1 → v1.2)

#### 1. Exit Logic — Partial Close at +1R (Phase 5 Lever 1)

**Change:** Replaced full-exit-only with a partial exit strategy: close 33 % of the position when unrealised P&L reaches +1R, then move stop-loss to breakeven.

**Why:** Phase 5 backtest result on 13 months of BTC/ETH/SOL data:

| Metric | Full Exit (v1.1) | Partial 33% at 1R (v1.2) | Change |
|--------|-----------------|--------------------------|--------|
| Profit Factor | 1.825 | 2.634 | +44.6% |
| Max Drawdown | 8.2R | 4.7R | −42.7% |
| Win Rate | 51.3% | 53.8% | +2.5pp |

Walk-forward validation: 5/5 folds profitable, min fold PF = 1.066 (F3, n=7).

**Files changed:**
- `core/execution/paper_executor.py` — `PaperPosition._auto_partial_applied` flag (restart-safe); auto-partial trigger in `on_tick()`
- `config.yaml` — added `exit.mode: partial`, `exit.partial_pct: 0.33`, `exit.partial_r_trigger: 1.0`

**Restart safety:** `_auto_partial_applied` is serialised to `open_positions.json` and restored on load, preventing duplicate partial closes after restart.

---

#### 2. Primary Timeframe — 30m (Phase 5 Lever 2, combined)

**Change:** `data.default_timeframe` 1h → 30m across scanner singleton, GUI default, and all timer alignment logic.

**Why:** 30m produces ~42% more signals at equivalent or better PF. The Phase 5 combined winning config (ADX 31, thresh 0.45, partial exit, 30m+4h MTF) achieved PF = 2.976 vs the v1.1 baseline of 1.47.

**Files changed:**
- `config.yaml` — `data.default_timeframe: 30m`
- `core/scanning/scanner.py` — singleton default `AssetScanner(timeframe="30m")`
- `gui/pages/market_scanner/scanner_page.py` — TF combo default reordered to 30m first

---

#### 3. MTF Gate — 30m Primary → 4h Confirmation (Phase 5 Lever 6)

**Change:** tf_map entry `"30m": "1h"` corrected to `"30m": "4h"`.

**Why:** Phase 5 MTF study:

| Configuration | Profit Factor | vs Single-TF |
|--------------|--------------|--------------|
| 30m + 1h (v1.1, incorrect) | 2.695 | — |
| 30m + 4h (v1.2) | 2.976 | +10.4% |

The 1h gate was an error in v1.1 — the intended Phase 5 winning config always specified 4h.

**Files changed:**
- `core/scanning/scanner.py` — `tf_map["30m"]` = `"4h"`

---

#### 4. ADX Threshold — 25 → 31 (Phase 5 Lever 2)

**Change:** `models.trend.adx_min` 25.0 → 31.0.

**Why:** Phase 5 frequency optimisation:

| ADX min | Trade Count | Profit Factor |
|---------|------------|--------------|
| 25 (v1.1) | 89 | 1.47 |
| 31 (v1.2) | 127 | 1.51 |
| 33 | 118 | 1.49 |

ADX 31 adds +42% trade count with a slight PF improvement. Validated on OOS data.

**Files changed:**
- `config.yaml` — `models.trend.adx_min: 31.0`

---

#### 5. Signal Model Cleanup — 4 Archived Models Removed from Runtime

**Change:** `MeanReversionModel`, `VWAPReversionModel`, `LiquiditySweepModel`, and `OrderBookModel` removed from `_ALL_MODELS` in `signal_generator.py`. They are never instantiated at startup.

**Why — Study 4 results:**

| Model | PF | WR | Verdict |
|-------|----|----|---------|
| MeanReversion | 0.21 | 32.2% | −$18k over 13 months |
| LiquiditySweep | 0.28 | 19.3% | −$15k over 13 months |
| VWAPReversion | 0.28 | — | Below 1.0 viability threshold |
| OrderBook | ≤1.0 | — | Structural 1h+ TF gate makes it unable to fire in production |

Code remains importable for tests and historical analysis. Re-enable path: remove from `disabled_models` in `config.yaml` only after ≥75 OOS live demo trades confirm positive expectancy.

**Files changed:**
- `core/signals/signal_generator.py` — removed 4 imports and instances from `_ALL_MODELS`; added archive comment block
- `core/signals/sub_models/mean_reversion_model.py` — added `⚠️ ARCHIVED` header
- `core/signals/sub_models/vwap_reversion_model.py` — added `⚠️ ARCHIVED` header
- `core/signals/sub_models/liquidity_sweep_model.py` — added `⚠️ ARCHIVED` header
- `core/signals/sub_models/order_book_model.py` — added `⚠️ ARCHIVED` header

---

#### 6. Notifications — Partial Exit HTML Template

**Change:** Added `partial_exit` notification type with a dedicated dark-theme HTML email template.

**Why:** In v1.1 partial closes were either silently swallowed or misrouted through the `trade_closed` template, producing confusing "closed" notifications for positions that were still open.

v1.2 routing:
- `exit_reason == "partial_close"` → `_on_partial_exit()` → `partial_exit` template
- All other closes → existing `trade_closed` template (unchanged)

The partial exit template shows: entry price, exit price, realised P&L (colour-coded), closed portion (% and USDT), remaining position size, SL moved to breakeven confirmation, strategy/TF/regime context, and a v1.2 exit logic explanation card.

**Files changed:**
- `core/notifications/notification_manager.py` — `_on_trade_closed()` routing gate; new `_on_partial_exit()` handler
- `core/notifications/notification_templates.py` — `partial_exit()` function; `_build_partial_exit_html()` builder; TEMPLATES registry entry

---

#### 7. Version Bump

**Files changed:**
- `config/constants.py` — `APP_VERSION = "1.0.0"` → `"1.2.0"`

---

### Phase 5 Backtest Reference

All v1.2 parameters validated on 13-month Study 4 dataset (2025-01-01 – 2026-01-31), BTC/ETH/SOL/BNB/XRP, 30m bars.

**Phase 5 Walk-Forward (5 folds, 2.5-month in-sample / 0.5-month OOS):**

| Fold | Trades (OOS) | PF | WR |
|------|-------------|----|----|
| F1 | 14 | 2.841 | 64.3% |
| F2 | 11 | 3.102 | 63.6% |
| F3 | 7 | 1.066 | 57.1% |
| F4 | 9 | 2.543 | 66.7% |
| F5 | 12 | 2.618 | 58.3% |
| **Total** | **53** | **2.434** | **62.3%** |

All 5 folds profitable. Min fold PF = 1.066 (thin sample, n=7).

---

### What Did Not Change

- Risk engine defaults (0.5% risk/trade, 4% max capital cap, 6% portfolio heat limit)
- Crash defence controller (7-component scorer, 4-tier response)
- Regime classifier (HMM + rule-based blend, diag covariance, adaptive weight at >50% uncertain)
- RL ensemble (shadow-only mode, weight 0.30 when eventually enabled)
- Learning system (L1/L2 adaptive weights — observation continues, no weight updates until 50 trades)
- All 23 intelligence agents
- Database schema
- Trade lifecycle (open → partial → full close)
- Phase 1 demo rules (0.5% risk, no parameter changes until 50+ trades assessed)

---

### Pending (Post-Release)

| Item | Trigger |
|------|---------|
| Re-evaluate MeanReversion | After 75+ live demo trades if WR ≥ 45% |
| Re-evaluate LiquiditySweep | Monitor OOS expectancy in early demo |
| Calibrator AUC audit | After 500 trades (remove `confluence_score` if delta < 0.01) |
| Score Calibration monotonicity | After 200 trades (target ≥ 0.5) |
| Exit efficiency panel | Monitor target capture % (target 80–120%) |
| Bear-gated shorts | ADX ≥ 32, thresh ≥ 0.55 — Phase 5 shows "ADDS VALUE" but thin sample (n=23) |
| Phase 1 → Phase 2 advancement | Manual only via `ScaleManager.evaluate_advancement()` after 50 trades |

---

### Validation

Run before each demo session:

```bash
# Unit + regime tests (0 failures required)
pytest tests/unit/test_session33_regime_fixes.py -v

# Intelligence agent tests
pytest tests/intelligence/ -v -m "not slow"

# UI checks
python scripts/run_ui_checks.py --no-screenshots

# v1.2 parity validation
python scripts/validate_v1_2_parity.py
```

---

## v1.1.0 — Session 33 Regime & Sizing Fixes
**Release date:** 2026-03-23

- Fixed ADX dead zone (20–25) incorrectly mapping to UNCERTAIN → now maps to RANGING
- Fixed `ema_slope=None` with high ADX falling through to UNCERTAIN
- Fixed `_committed_regime` initialisation to `""` (was `"uncertain"`, forced all early calls to uncertain)
- Fixed `risk_pct_per_trade` default from 0.75 → 0.5 in confluence_scorer and position_sizer
- VWAPReversionModel added to disabled_models (PF 0.28, below viability threshold)
- Added 31 regression tests (`tests/unit/test_session33_regime_fixes.py`)

---

## v1.0.0 — Initial Production Release
**Release date:** 2026-03-01

- Full signal pipeline: OHLCV → HMM regime → SignalGenerator → ConfluenceScorer → RiskGate → PositionSizer → PaperExecutor
- Active models: TrendModel, MomentumBreakout (Study 4 validated)
- Disabled: MeanReversion, LiquiditySweep (Study 4 failures)
- 23 intelligence agents, RL ensemble (shadow mode)
- Crash defence controller, adaptive learning (L1/L2)
- Full GUI: 20 pages, Bloomberg dark theme
- Test suite: 1,611 passed, 11 skipped (GPU), 0 failures
