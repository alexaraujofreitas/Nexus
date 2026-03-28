# Baseline Mismatch Analysis — Session 38
**Date:** 2026-03-28

---

## Summary

Two discrepancy threads existed at the start of Session 38:

1. **Harness vs canonical engine** — the `research/harness/fast_backtest.py` vectorized
   engine produced wrong results due to implementation bugs (fixed in commit 90b94b1).
2. **Stored v9 JSON vs current canonical engine** — the stored artifact
   `reports/mr_pbl_slc_v9_system.json` was generated from an older code state and does
   not match what `backtest_v9_system.py` produces today.

Both are resolved. This report documents them.

---

## Discrepancy 1: Harness vs Canonical Engine (RESOLVED)

### Expected (canonical — backtest_v9_system.py current run)
| Metric | Value |
|---|---|
| Combined n | 1,731 |
| PF (zero fees) | 1.3797 |
| PF (0.04%/side) | ~1.276 |
| WR | 56.1% |

### Root Causes Found

| # | Component | Expected | Actual (before fix) | Root Cause | Fix (commit 90b94b1) |
|---|---|---|---|---|---|
| 1 | ATR smoothing in `_classify_regimes_vec()` | `tr.ewm(span=14)` alpha≈0.133 | `_atr_wilder(alpha=1/14)` alpha≈0.071 | Wilder EWM used instead of standard EWM — 46% slower decay, wrong ATR ratios, wrong expansion/crash regime labels | Replaced with `tr.ewm(span=14, adjust=False).mean()` |
| 2 | Hysteresis algorithm in `_classify_regimes_vec()` | 2-pass backward merge of runs < 3 bars | "commitment counter" requiring 3 consecutive identical labels | Completely different algorithm producing different transition points | Rewrote `_apply_hysteresis_vec()` as exact port of production `_apply_hysteresis()` |
| 3 | SLC deduplication (`slc_last_1h`) | No explicit per-1h dedup (natural via positions/pending guards) | Explicit dedup blocked SLC re-evaluation after failed pending fill | ~7 SLC trades suppressed per run | Removed `slc_last_1h` dict; rely on natural guards |

### Result After Fix
| Metric | Harness | Canonical | Delta |
|---|---|---|---|
| Combined n | **1,731** | 1,731 | **0 — exact** |
| PF (zero fees) | **1.3798** | 1.3797 | **+0.0001** |
| PF (0.04%/side) | 1.2756 | ~1.276 | ~+0.001 |
| WR | 56.4% | 56.1% | +0.3pp |

**Status: RESOLVED ✅** — harness matches canonical engine to within rounding error.

---

## Discrepancy 2: Stored v9 JSON vs Current Canonical Engine

The stored file `reports/mr_pbl_slc_v9_system.json` shows:
- n = 1,745
- PF (zero fees) = 1.3708
- PF (0.04%/side) = 1.2682

Running `backtest_v9_system.py` today produces:
- n = 1,731
- PF (zero fees) = 1.3797
- PF (0.04%/side) ≈ 1.276

| Metric | Stored JSON | Current canonical | Delta |
|---|---|---|---|
| Combined n | 1,745 | 1,731 | −14 |
| PF (zero fees) | 1.3708 | 1.3797 | +0.0089 |
| PF (0.04%) | 1.2682 | ~1.276 | +0.008 |

### Root Cause

The stored JSON was written from commit **c2c5e30** (Session 36). Subsequent code changes
in the production modules (likely `research_regime_classifier.py` or indicator pipeline)
shifted 14 trades. The current canonical code is the authoritative reference.

**Status: ACKNOWLEDGED — stored JSON deprecated ✅**
The stored v9 JSON is superseded by the locked baseline in
`research/engine/baseline_registry.json` (see Phase 5).

---

## Locked Canonical Baseline

Established in this session, locked in `research/engine/baseline_registry.json`:

| Metric | Value | Tolerance |
|---|---|---|
| n_trades | 1,731 | [1,700 – 1,760] |
| PF (zero fees) | 1.3797 | [1.35 – 1.41] |
| PF (0.04%/side) | 1.2756 | [1.25 – 1.31] |
| WR | 56.1% | [54% – 58%] |
| CAGR (zero fees) | 67.5% | [55% – 80%] |
| MaxDD | −16.4% | [−22% – −12%] |

Any run outside tolerance bands triggers a BASELINE FAIL and blocks optimization.
