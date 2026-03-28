# NexusTrader — PBL/SLC Optimization Research Plan
## Branch: `research/pbl-slc-optimization-matrix`
**Date:** 2026-03-28 | **Session:** 37 | **Author:** Research Workstream

---

## 1. Objectives

### Primary Objectives
1. **Improve PBL standalone performance** — current PF=0.8995 (Scenario A, zero fees) is below 1.0 in isolation. Understand root cause and test whether parameter changes can raise standalone PF ≥ 1.10 without degrading combined-system performance.
2. **Explore superior 30m + 4h confirmation combinations** — current setup: 4h EMA20 > EMA50. Test whether alternative 4h filters (different EMAs, ADX, trend strength) can improve combined CAGR/PF/MaxDD.

### Secondary Objective
3. **Controlled evaluation of HYPE/USDT** as an add/replace candidate in a sub-study.

### Non-Objectives (this phase)
- Live parameter changes to `main` without full OOS validation
- Modifying SLC core logic (SLC PF=1.5455 is satisfactory)
- Re-enabling archived models (MeanReversion, LiquiditySweep)

---

## 2. Current Baselines (to be reproduced in Stage A)

| Model | WR | PF (zero fees) | PF (0.04%/side) | CAGR | MaxDD | n |
|-------|-----|----------------|-----------------|------|-------|---|
| PBL standalone | 44.6% | 0.8995 | ~0.75 | negative | - | 516 |
| SLC standalone | 60.9% | 1.5455 | ~1.42 | - | - | 1,229 |
| Combined (BTC+SOL+ETH) | 56.1% | 1.3708 | 1.2682 | 47.44% | -20.33% | 1,745 |
| Research baseline (v7_final, BTC only) | 61.1% | 1.2975 | ~1.20 | 50.41% | -20.66% | 1,476 |

**Backtest parameters (current baseline):**
- Symbols: BTC/USDT, SOL/USDT, ETH/USDT
- Primary TF: 30m | HTF: 4h (PBL) | SLC: 1h
- Data: 2022-03-22 → 2026-03-21 (4 years)
- Position sizing: 35% equity per trade (pos_frac mode)
- Max heat: 80% portfolio
- Fees: Scenario A = 0%, Scenario B = 0.04%/side

---

## 3. Root Cause Hypotheses for PBL PF < 1.0

| # | Hypothesis | Testable Via |
|---|-----------|--------------|
| H1 | EMA50 proximity threshold (0.5×ATR) is too wide, admitting low-quality setups | Vary `ema_prox_atr_mult` ∈ [0.2, 0.8] |
| H2 | 4h HTF confirmation (EMA20>EMA50) is too weak; price can still be in local correction | Stronger 4h filters (EMA slope, ADX, price vs EMA200) |
| H3 | SL multiplier (2.5×ATR) is too tight relative to TP (3.0×ATR), yielding poor R:R in practice | Vary SL ∈ [1.5, 3.5], TP ∈ [2.0, 5.0] |
| H4 | RSI floor of 40 admits too many weak momentum setups | Raise RSI floor to 45–55 |
| H5 | Rejection candle criteria lack sufficient strictness (lw>body as only body filter) | Add body ratio filter (body/range < 0.4) |
| H6 | Model fires in ranging sub-regimes within bull_trend, degrading WR | Add secondary strength filter (prox_score + rsi_score > threshold) |
| H7 | PBL is structurally unprofitable on crypto 30m without higher confluence | Combined-system performance is the appropriate scope |

---

## 4. Search Spaces

### 4.1 PBL Parameter Matrix

| Parameter | Baseline | Test Range | Step | Type |
|-----------|----------|------------|------|------|
| `ema_prox_atr_mult` | 0.50 | 0.20 – 0.80 | 0.10 | continuous |
| `rsi_min` | 40.0 | 35.0 – 60.0 | 5.0 | continuous |
| `sl_atr_mult` | 2.50 | 1.50 – 3.50 | 0.50 | continuous |
| `tp_atr_mult` | 3.00 | 2.00 – 5.00 | 0.50 | continuous |
| `htf_ema_fast` | 20 | 10, 20, 50 | - | categorical |
| `htf_ema_slow` | 50 | 50, 100, 200 | - | categorical |
| `body_ratio_max` | disabled | 0.30, 0.40, 0.50, OFF | - | categorical |
| `htf_adx_min` | disabled | OFF, 20, 25 | - | categorical |
| `htf_price_above_ema200` | disabled | OFF, ON | - | categorical |

**Coarse sweep size:** ~7×6×5×7×3×3×4×3×2 → but staged (not all combinations at once).
**Staged approach:** Coarse (step=0.20/10.0/1.0) → Focused (step=0.10/5.0/0.5) → Fine.

### 4.2 SLC Parameter Matrix (limited — SLC already performs well)

| Parameter | Baseline | Test Range | Step |
|-----------|----------|------------|------|
| `adx_min` | 28.0 | 22.0 – 35.0 | 3.0 |
| `swing_bars` | 10 | 7, 10, 14, 20 | - |
| `sl_atr_mult` | 2.50 | 1.50 – 3.50 | 0.50 |
| `tp_atr_mult` | 2.00 | 1.50 – 3.50 | 0.50 |

SLC sweep is secondary. Do not degrade SLC PF below 1.40.

### 4.3 Combined System (30m + 4h Confirmation Matrix)

| Variant | 4h Filter Logic | 4h EMAs | 4h ADX | Price Gate |
|---------|----------------|---------|--------|-----------|
| V0 | Baseline: EMA20>EMA50 | 20/50 | - | - |
| V1 | EMA9>EMA21 | 9/21 | - | - |
| V2 | EMA20>EMA100 | 20/100 | - | - |
| V3 | EMA50>EMA200 | 50/200 | - | - |
| V4 | EMA20>EMA50 + ADX≥20 | 20/50 | 20 | - |
| V5 | EMA20>EMA50 + ADX≥25 | 20/50 | 25 | - |
| V6 | EMA20>EMA50 + price>EMA200 | 20/50 | - | EMA200 |
| V7 | EMA20>EMA50 + ADX≥20 + price>EMA200 | 20/50 | 20 | EMA200 |
| V8 | EMA9>EMA21 + ADX≥20 | 9/21 | 20 | - |
| V9 | EMA50>EMA200 + ADX≥25 | 50/200 | 25 | - |

### 4.4 HYPE/USDT Sub-Study (Stage D only if warranted)
- Add HYPE/USDT alongside existing 3 symbols
- Replace BNB/USDT with HYPE/USDT (BNB currently not in production watchlist)
- Baseline: BTC+SOL+ETH only (current production)

---

## 5. Acceptance Criteria for Promotion to `main`

All criteria must be met simultaneously:

| Metric | Minimum Threshold | Preferred |
|--------|-------------------|-----------|
| PBL standalone PF (zero fees) | ≥ 1.10 | ≥ 1.20 |
| Combined system PF (0.04%/side) | ≥ 1.18 (current: 1.2682) | ≥ 1.30 |
| Combined system CAGR | ≥ 40% (current: 47.44%) | ≥ 50% |
| Combined MaxDD | ≤ -25% (current: -20.33%) | ≤ -18% |
| Trade count (combined) | ≥ 1,200 (current: 1,745) | ≥ 1,500 |
| Walk-forward OOS PF | ≥ 1.10 across all windows | ≥ 1.20 |
| Walk-forward PF stability (std/mean) | ≤ 0.30 | ≤ 0.20 |
| Holdout test PF (final 6 months) | ≥ 1.05 | ≥ 1.15 |
| Configs tested before winner | Must be logged | ≥ 100 |

**Reject if ANY of:**
- OOS holdout PF < 1.0
- MaxDD worsens by > 5% absolute
- Trade count drops below 800
- Only in-sample metrics are positive (OOS degrades sharply)

---

## 6. Anti-Overfitting Protocol

### 6.1 Data Splits
```
Full dataset:  2022-03-22 → 2026-03-21 (4 years)
Train (IS):    2022-03-22 → 2025-03-21 (3 years — 75%)
Holdout (OOS): 2025-03-22 → 2026-03-21 (1 year — 25%, NEVER touched during search)
```

### 6.2 Walk-Forward Optimization
- Window size: 12 months in-sample, 3 months OOS
- Step: 3 months
- Windows: 2022Q2→2023Q1 test 2023Q2, 2022Q3→2023Q2 test 2023Q3, etc.
- Result: WF efficiency = mean(OOS PF) / mean(IS PF); acceptable ≥ 0.75

### 6.3 Trial Count Logging
- Every trial is numbered and logged with parameters + all metrics
- Number of configs tested is reported in final output
- Deflated Sharpe proxy: penalize IS metric by sqrt(n_trials_tested / n_trades)

### 6.4 Robustness Ranking
- Score = 0.4 × IS_PF + 0.4 × WF_OOS_PF + 0.2 × holdout_PF
- Ranked by robustness score, not raw IS metric
- Final candidate must rank top-10 on both IS and OOS

### 6.5 Overfitting Red Flags (automatic disqualification)
- IS PF ≥ 1.5 but OOS PF < 1.0
- Trade count in OOS < 50
- Parameter on edge of tested range (edge-of-space warning)
- WF efficiency < 0.50

---

## 7. Output Artifacts

| Artifact | Path | Format |
|----------|------|--------|
| Research plan | `docs/research_pbl_slc_optimization_plan.md` | Markdown |
| External research | `docs/external_research_inputs_pbl_slc.md` | Markdown |
| Search space definition | `research/configs/search_spaces.json` | JSON |
| Optimization harness | `research/harness/optimize_pbl.py` | Python |
| All trial results | `research/results/trials_pbl_coarse.csv` | CSV |
| Top-N leaderboard snapshots | `research/leaderboards/` | JSON |
| Walk-forward results | `research/results/walkforward_results.csv` | CSV |
| Holdout results | `research/results/holdout_results.csv` | CSV |
| Final Word report | `reports/research_pbl_slc_final_report.docx` | DOCX |
| Promotion package | `research/promotion_package.md` | Markdown (if applicable) |

---

## 8. Compute Strategy

- **CPU:** Multiprocessing via `multiprocessing.Pool` — use `os.cpu_count()` workers
- **GPU:** Optional CuPy/RAPIDS for indicator pre-computation if available; pandas fallback guaranteed
- **Parallelization unit:** One full backtest per process (PBL or combined, single param set)
- **Progress:** `tqdm` + rolling leaderboard printed every 10 completions
- **Result persistence:** Append to CSV after each batch completes (no data loss if interrupted)
- **ETA estimation:** Rolling average trial time × remaining trials

---

## 9. Staged Search Design

```
Stage A — Baseline Reproduction (1 trial)
  → Confirm exact match to reported PBL PF=0.8995, SLC PF=1.5455, combined PF=1.2682

Stage B — Coarse Sweep (PBL standalone, ~200 trials)
  → Large steps across all PBL parameters
  → Identify promising zones (PF > 1.05)
  → Print rolling top-10 every 25 trials

Stage C — Focused Sweep (PBL + combined, ~300 trials)
  → Fine-grained search in promising zones from Stage B
  → Test top-10 coarse PBL configs against combined system
  → Confirm combined PF not degraded

Stage D — Confirmation Variants (10 variants × 3 symbols = 30 trials)
  → Test 4h confirmation variants V0–V9 defined in Section 4.3
  → Combined system for each variant

Stage E — Walk-Forward + Holdout (top-5 candidates)
  → WF across 4 rolling windows
  → Final holdout (2025-03-22 → 2026-03-21)
  → Robustness scoring

Stage F — Stress Review + Final Report
  → Compare IS vs OOS
  → Fee sensitivity (0% vs 0.04% vs 0.08%)
  → Final recommendation
```

---

## 10. Promotion Rule

**NOTHING merges to `main` unless ALL acceptance criteria in Section 5 are met.**

If no config clears all criteria:
- Branch remains `EXPERIMENTAL ONLY`
- Production `main` is unchanged
- Report documents findings with `NEEDS MORE RESEARCH` classification

If at least one config clears all criteria:
- Promotion package specifies:
  - Exact parameter values to change
  - Exact config.yaml keys
  - Exact source files (if any model code changes)
  - Expected impact on live paper trading
  - Rollback procedure
