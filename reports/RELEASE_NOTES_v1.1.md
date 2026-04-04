# NexusTrader v1.1 — Release Notes

**Date:** 2026-03-26
**Branch:** main
**Test suite:** 1,593 passed · 13 skipped (GPU) · 0 failures

---

## Summary

v1.1 is a **stabilization release**. It closes out the CPS crash-prediction gating experiment (outcome: PAUSE — net EVI negative on all tested symbol/horizon combinations), removes all experiment scaffolding from the production codebase, and ships the full AI trade feedback loop as the stable baseline for Phase 1 demo trading.

No signal logic, model weights, entry/exit rules, risk parameters, or learning system components were changed. This release is a pure cleanup pass.

---

## What Was Removed

### CPS Crash-Prediction Gating Experiment
The CPS (Crash Prediction Score) experiment attempted to gate 1h trade entries by predicting 5m/10m crash probability using isotonic-calibrated XGBoost classifiers. After two calibration passes, the net EVI (Economic Value Index) was negative on all tested horizons (BTC/crash_5m: −$158, BTC/crash_10m: −$144). The Phase 1B verdict was **PAUSE** — the filter destroys more value than it saves at any viable fire-rate threshold.

All experiment code has been removed or archived:

| Item | Action |
|------|--------|
| `scripts/cps_calibration.py` | Deleted |
| `scripts/cps_calibration_v2.py` | Deleted |
| `scripts/generate_v04_doc.py` | Deleted |
| `scripts/generate_v05_doc.py` | Deleted |
| `data/validation/cps_calibration_*.{json,txt,png}` (14 files) | Archived → `data/validation/archived_cps_experiment/` |
| `NexusTrader_CrashRebound_Design_v0.1–v0.5.docx` | Archived → `archived_cps_experiment/` |
| `_CDA_OBS_FILE` constant + JSONL write block in `paper_executor.py` | Removed |
| "Phase 1B observability" comments in `risk_gate.py`, `order_candidate.py` | Cleaned (neutral wording) |
| CPS calibration hints in `fetch_btc_ohlcv_recovery.py`, `fetch_historical_data_v2.py`, `cpi_validation_realdata.py` | Removed |

---

## What Was Kept (Production Architecture — Unchanged)

The following crash-defense components are production risk-management infrastructure and were **not touched**:

- `core/risk/crash_detector.py` — 7-component real-time crash scorer
- `core/risk/crash_defense_controller.py` — 4-tier defensive response (NORMAL / DEFENSIVE / HIGH_ALERT / EMERGENCY / SYSTEMIC)
- `core/agents/crash_detection_agent.py` — aggregates signals, outputs `position_size_multiplier`
- `core/risk/risk_gate.py` — CDA multiplier application (scales `max_position_capital_pct` when tier < NORMAL)
- GUI: `crash_score_widget.py`, `dashboard_page.py`, `system_health_page.py`

The crash-defense system operates in real-time as a position-sizing safeguard. It is architecturally distinct from the CPS entry-gating experiment.

---

## What Was Kept (AI Trade Feedback Loop — Fully Intact)

All components of the AI trade analysis and closed-order learning loop are unchanged and verified passing:

- `core/ai/` — AI trade analysis engine, feedback generation, provider abstraction (Ollama / OpenAI)
- `core/learning/` — L1 (model WR), L2 (model×regime, model×asset), AdaptiveWeightEngine, ProbabilityCalibrator, AdaptiveLearningPolicy
- `core/evaluation/` — DemoPerformanceEvaluator (20 checks), EdgeEvaluator, SystemReadinessEvaluator
- `core/monitoring/` — LiveVsBacktestTracker, PerformanceThresholdEvaluator, ScaleManager, ReviewGenerator
- `core/analytics/` — FilterStatsTracker, ModelPerformanceTracker, CorrelationDampener, PortfolioGuard
- `scripts/daily_report.py` — Daily P&L + AI analysis + adaptive learning proposals (DetachedInstanceError fix applied)
- `scripts/launch_checklist.py` — Pre-session validation (DB write test + import fixes applied)

---

## Bug Fixes Included (Sessions 38–39)

| File | Fix |
|------|-----|
| `scripts/daily_report.py` | DetachedInstanceError: all metric computation moved inside `with session:` block |
| `scripts/launch_checklist.py` | `NOT NULL constraint failed: system_logs.module` — INSERT now includes `module='launch_checklist'` |
| `scripts/launch_checklist.py` | `cannot import name 'render_analysis'` — corrected to `render_for_channel` |
| `scripts/fetch_historical_data_v2.py` | JSON-level rate limit now retried with 60s sleep + `continue` (not `break`) |
| `scripts/fetch_historical_data_v2.py` | Write-then-rename pattern (`.parquet.tmp` → final path) prevents corruption on interrupt |
| `core/analytics/filter_stats.py` | `partial_close()` now calls `FilterStatsTracker.record_trade_outcome()` per filter |
| `core/monitoring/milestone_tracker.py` | Gate key mapping corrected; `analysis_success_rate` scale fixed (fraction not %) |
| `core/learning/adaptive_learning_policy.py` | Overfitting protection added — proposals blocked when out-of-sample performance degrades |

---

## Version Bump

- `main.py` header and startup log: `v1.0` → `v1.1`
- `CLAUDE.md`: System State section updated to v1.1, CPS Calibration Project State section removed

---

## Test Results

```
pytest tests/unit/ tests/intelligence/
1593 passed, 13 skipped (GPU), 0 failures — 19.5s

pytest tests/unit/test_session33_regime_fixes.py
31 passed, 0 failures — 0.4s

pytest tests/learning/ tests/evaluation/ tests/backtesting/ tests/validation/
310 passed, 0 failures — 16.4s
```

Total: **1,934 tests green** across unit / intelligence / learning / evaluation / backtesting / validation suites.

---

## Git Merge Summary

```bash
# From the CPS experiment branch, merge to main:
git add -A
git commit -m "v1.1: Remove CPS gating experiment, stabilize AI feedback loop

- Delete scripts/cps_calibration.py, cps_calibration_v2.py
- Delete scripts/generate_v04_doc.py, generate_v05_doc.py
- Archive CPS data artifacts and CrashRebound design docs
- Remove _CDA_OBS_FILE JSONL write from paper_executor.py
- Clean Phase 1B experiment comments from risk_gate.py, order_candidate.py
- Bump version to v1.1 in main.py
- Update CLAUDE.md: remove CPS project state, set v1.1 system state
- Fix daily_report.py DetachedInstanceError
- Fix launch_checklist.py DB write test + import
- Add AdaptiveLearningPolicy overfitting protection
- Fix FilterStatsTracker tracking gap in partial_close()
- Fix milestone key mapping + analysis_success_rate scale

Test suite: 1,934 passed / 0 failed across all suites"

git checkout main
git merge --no-ff feature/cps-experiment -m "Merge: CPS experiment concluded (PAUSE), stabilize v1.1"
git tag -a v1.1 -m "NexusTrader v1.1 — CPS experiment removed, AI feedback loop stable"
```

---

## Phase 1 Demo Status (Unchanged)

| Config | Value |
|--------|-------|
| `risk_pct_per_trade` | 0.5% |
| `max_capital_pct` | 4% |
| Active models | TrendModel (PF 1.47), MomentumBreakout (PF 4.17) |
| Symbol weights | SOL=1.3, ETH=1.2, BTC=1.0, BNB=0.8, XRP=0.8 |
| Pause threshold | 2+ models RED or portfolio RED |

Phase 1 advancement criteria and demo trading rules are unchanged. The hard block (PF < 1.0 AND WR < 40% over 30+ trades) remains in place.
