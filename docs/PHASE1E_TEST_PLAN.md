# Phase 1E — Test Plan & Harness Requirements

**Date:** 2026-04-06
**Phase:** 1E of 9
**Status:** Complete

---

## 1. Testing Philosophy

Every module gets tested at three levels:

1. **Unit tests** — isolated, fast, mocked dependencies. Run in <30s total.
2. **Integration tests** — real component wiring, synthetic data. Run in <5min.
3. **System tests** — full pipeline, Bybit Demo data. Run on demand (Phase 8-9).

**Non-negotiable:** No phase gate passes with any test failure. Zero tolerance.

---

## 2. Test Harness Requirements

### 2.1 Existing Infrastructure (Retain)

| Component | Location | Status |
|---|---|---|
| pytest runner | `pytest.ini` or `pyproject.toml` | ✅ Already configured |
| Test directory | `tests/` | ✅ ~90 files, organized by module |
| unittest.mock | Python stdlib | ✅ Used throughout |
| Fixture data | Various `tests/fixtures/` and inline | ✅ Sufficient for swing models |

### 2.2 New Infrastructure Needed

| Component | Purpose | Phase |
|---|---|---|
| `pytest-asyncio` | Test async DataEngine code | 3 |
| `tests/fixtures/candle_data/` | Synthetic 1m OHLCV data (300-bar sequences with known patterns for each strategy) | 3 |
| `tests/fixtures/intraday/` | Pre-computed 5m/15m DataFrames with indicators for strategy testing | 4 |
| `tests/conftest.py` additions | Shared fixtures: `mock_event_bus`, `mock_data_engine`, `sample_1m_candles`, `sample_setup_signal` | 2 |
| Candle data generator | `tests/helpers/candle_generator.py` — produces synthetic OHLCV with controllable patterns (trend, range, spike, chop) | 3 |

### 2.3 Test Data Strategy

**Synthetic data preferred over recorded data** for unit/integration tests:

- Deterministic (same output every run)
- Controllable (create exact patterns needed: momentum breakout, VWAP touch, pullback, range, sweep)
- Fast (no file I/O)
- No Bybit API dependency

**Recorded data for system tests only** (Phase 8-9):

- 24h of real 1m Bybit Demo data per symbol
- Stored in `tests/data/recorded/` as CSV
- Used for end-to-end pipeline validation

---

## 3. Test Plan by Phase

### Phase 2 Tests — Headless Core (~20 tests)

```
tests/test_event_bus_pure.py (8 tests)
  ├── test_subscribe_and_publish
  ├── test_wildcard_subscriber
  ├── test_unsubscribe
  ├── test_thread_safety_concurrent_publish
  ├── test_no_pyside6_import (verify no Qt dependency)
  ├── test_event_history
  ├── test_callback_error_isolation
  └── test_qt_bridge_adapter_optional

tests/test_base_agent_thread.py (4 tests)
  ├── test_agent_starts_and_stops
  ├── test_fetch_process_publish_cycle
  ├── test_error_backoff
  └── test_no_pyside6_import

tests/test_agent_coordinator_headless.py (4 tests)
  ├── test_starts_exactly_4_agents
  ├── test_stop_all_cleans_up
  ├── test_no_pyside6_import
  └── test_agent_status_reporting

tests/test_engine_lifecycle.py (4 tests)
  ├── test_headless_start_stop
  ├── test_db_initialized
  ├── test_exchange_connected
  └── test_agents_started

tests/test_schema_migration.py (2 tests) — Audit Finding 1
  ├── test_migrate_schema_has_all_8_intraday_columns
  └── test_existing_db_opens_after_migration

tests/test_thread_baseline.py (2 tests) — Audit Finding 6
  ├── test_baseline_thread_count_logged
  └── test_no_spurious_watchdog_warnings
```

### Phase 3 Tests — Data Engine (~30 tests)

```
tests/test_candle_builder.py (12 tests)
  ├── test_1m_to_3m_aggregation
  ├── test_1m_to_5m_aggregation
  ├── test_1m_to_15m_aggregation
  ├── test_1m_to_1h_aggregation
  ├── test_high_is_max_of_candles
  ├── test_low_is_min_of_candles
  ├── test_volume_is_sum
  ├── test_open_is_first_close_is_last
  ├── test_partial_candle_not_emitted
  ├── test_multiple_symbols_independent
  ├── test_buffer_rolling_window_size
  └── test_indicator_computation_on_close

tests/test_data_engine.py (10 tests)
  ├── test_ws_connection_establishes (mock ccxt.pro)
  ├── test_1m_candle_fed_to_builder
  ├── test_rest_fallback_on_ws_disconnect
  ├── test_rest_fallback_on_3_missed_candles
  ├── test_historical_backfill_on_startup
  ├── test_feed_status_published
  ├── test_multiple_symbol_subscriptions
  ├── test_reconnect_after_disconnect
  ├── test_windows_event_loop_policy
  └── test_graceful_shutdown

tests/test_indicator_intraday.py (8 tests)
  ├── test_vwap_calculation
  ├── test_vwap_session_reset_at_midnight
  ├── test_volume_profile_bins
  ├── test_volume_profile_hvn_detection
  ├── test_calculate_intraday_subset
  ├── test_calculate_intraday_performance_under_10ms
  ├── test_spread_pct_column
  └── test_book_imbalance_column
```

### Phase 4 Tests — Strategy Engine (~80 tests)

```
tests/strategies/test_mx_strategy.py (15 tests)
  ├── test_setup_qualifies_on_adx_ema_volume
  ├── test_setup_rejects_low_adx
  ├── test_setup_rejects_wrong_ema_alignment
  ├── test_setup_rejects_low_volume
  ├── test_trigger_fires_on_breakout_candle
  ├── test_trigger_rejects_no_volume_confirm
  ├── test_trigger_rejects_candle_inside_range
  ├── test_long_direction_in_bull_trend
  ├── test_short_direction_in_bear_trend
  ├── test_sl_placement_below_5m_low
  ├── test_tp_calculation_2r_target
  ├── test_regime_gating
  ├── test_disabled_via_config
  ├── test_signal_age_metadata
  └── test_edge_case_exactly_at_threshold

tests/strategies/test_vr_strategy.py (15 tests)
  └── [parallel structure to MX: setup/trigger/direction/sl/tp/regime/config/edge]

tests/strategies/test_mpc_strategy.py (15 tests)
  └── [parallel structure]

tests/strategies/test_rbr_strategy.py (15 tests)
  └── [parallel structure]

tests/strategies/test_lsr_strategy.py (15 tests)
  └── [parallel structure]

tests/strategies/test_strategy_bus.py (10 tests)
  ├── test_5m_candle_triggers_setup_eval
  ├── test_1m_candle_triggers_trigger_eval
  ├── test_pending_setup_queue
  ├── test_setup_invalidation_clears_pending
  ├── test_trigger_produces_trigger_signal
  ├── test_signal_forwarded_to_gtf
  ├── test_disabled_strategy_skipped
  ├── test_multiple_strategies_same_symbol
  ├── test_signal_expiry_blocks_stale
  └── test_signal_expiry_blocks_drifted

tests/strategies/test_signal_expiry.py (9 tests) — expanded per External Review Correction 1
  ├── test_fresh_signal_passes
  ├── test_aged_signal_blocked
  ├── test_drifted_signal_blocked
  ├── test_rr_invalidated_after_drift
  ├── test_per_strategy_age_limits
  ├── test_latency_guardrail_activates_above_60pct (Correction 1)
  ├── test_latency_guardrail_extends_max_age_by_50pct (Correction 1)
  ├── test_latency_guardrail_reverts_when_latency_drops (Correction 1)
  └── test_latency_guardrail_safety_bound_2x_max (Correction 1)
```

### Phase 5 Tests — Profitability Hardening (~60 tests)

```
tests/filters/test_global_trade_filter.py (18 tests)
  ├── test_regime_throttle_limits_trades_per_hour
  ├── test_chop_detector_activates_on_low_adx
  ├── test_chop_detector_deactivates_on_adx_recovery
  ├── test_atr_volatility_filter_blocks_low_vol
  ├── test_atr_volatility_filter_blocks_extreme_vol
  ├── test_loss_streak_pauses_after_3
  ├── test_loss_streak_halves_size
  ├── test_loss_streak_recovers_on_2_wins
  ├── test_clustering_cooldown_same_symbol_win
  ├── test_clustering_cooldown_same_symbol_loss
  ├── test_session_budget_max_daily
  ├── test_session_budget_max_per_symbol
  ├── test_session_budget_resets_at_midnight
  ├── test_multiple_filters_cumulative
  ├── test_gtf_passes_clean_trade
  ├── test_gtf_returns_block_reason
  ├── test_stateful_counters_persist_across_calls
  └── test_gtf_reset_on_new_day

tests/filters/test_no_trade_conditions.py (12 tests)
  ├── test_dead_market_blocks
  ├── test_chaotic_spike_blocks
  ├── test_spread_widening_blocks
  ├── test_tf_conflict_blocks
  ├── test_thin_book_blocks
  ├── test_funding_extreme_blocks
  ├── test_all_conditions_pass
  ├── test_warn_vs_block_distinction
  ├── test_condition_independence
  ├── test_condition_with_missing_data
  ├── test_conditions_composable
  └── test_conditions_log_reasons

tests/scoring/test_trade_quality_scorer.py (8 tests)
  ├── test_perfect_score_scenario
  ├── test_minimum_score_scenario
  ├── test_component_weights_sum_to_1
  ├── test_below_threshold_rejected
  ├── test_above_threshold_accepted
  ├── test_microstructure_component
  ├── test_historical_context_component
  └── test_score_deterministic

tests/portfolio/test_portfolio_coordinator.py (8 tests)
  ├── test_blocks_same_symbol_doubling
  ├── test_blocks_same_class_over_3
  ├── test_blocks_opposing_signals
  ├── test_blocks_correlation_over_threshold
  ├── test_allows_uncorrelated_positions
  ├── test_heat_limit_enforcement
  ├── test_priority_ranking_when_multiple
  └── test_coordinator_with_existing_positions

tests/analytics/test_asset_ranker.py (6 tests)
tests/learning/test_performance_matrices.py (8 tests)
```

### Phase 6 Tests — Final Controls (~40 tests)

```
tests/monitoring/test_edge_validity_monitor.py (18 tests) — expanded per Audit Finding 5 + External Review Correction 3
  ├── test_active_to_degraded_transition
  ├── test_degraded_to_suspended_transition
  ├── test_suspended_probe_recovery
  ├── test_72h_cooldown_enforced
  ├── test_probe_size_50pct
  ├── test_probe_success_restores_active
  ├── test_probe_failure_re_suspends
  ├── test_regime_isolation_single_regime_defers_to_learning_loop
  ├── test_regime_isolation_two_regimes_triggers_degraded
  ├── test_regime_isolation_four_regimes_considers_suspended
  ├── test_regime_decomposition_per_regime_pf
  ├── test_75_trade_window_rolling
  ├── test_class_pf_calculation
  ├── test_concurrent_class_tracking
  ├── test_class_health_mod_values_by_status
  ├── test_ll_conflict_over_50pct_blocks_degradation (Correction 3)
  ├── test_ll_conflict_under_50pct_allows_degradation (Correction 3)
  └── test_ll_attribution_ratio_computation_accuracy (Correction 3)

tests/sizing/test_capital_concentration.py (15 tests) — expanded per Audit Finding 3 + External Review Correction 2
  ├── test_base_weight_tqs_times_asset_times_execution
  ├── test_base_weight_clamped_0_to_2
  ├── test_class_health_modifier_active_1_0
  ├── test_class_health_modifier_degraded_0_70
  ├── test_class_health_modifier_suspended_0_0
  ├── test_conviction_mod_formula_regime_plus_agreement
  ├── test_conviction_mod_clamped_040_150
  ├── test_combined_weight_clamped_at_040
  ├── test_combined_weight_clamped_at_150
  ├── test_weight_integration_with_position_sizer
  ├── test_weight_varies_across_scenarios
  ├── test_weight_deterministic
  ├── test_decorrelation_dampens_conviction_when_corr_above_070 (Correction 2)
  ├── test_decorrelation_reverts_when_corr_below_070 (Correction 2)
  └── test_capital_weight_cv_between_015_and_060 (Correction 2 — integration)

tests/monitoring/test_failure_detectors.py (10 tests)
  ├── test_pf_drift_detector
  ├── test_execution_degradation_detector
  ├── test_strategy_concentration_detector
  ├── test_correlation_spike_detector
  ├── test_regime_mismatch_detector
  ├── test_warning_vs_critical_levels
  ├── test_detector_independence
  ├── test_detector_reset
  ├── test_detector_with_insufficient_data
  └── test_all_detectors_quiescent_in_healthy_system

tests/monitoring/test_recovery_controller.py (13 tests) — expanded per Audit Finding 4
  ├── test_critical_engages_recovery
  ├── test_recovery_risk_reduced_to_010_pct
  ├── test_recovery_max_concurrent_reduced_to_4
  ├── test_recovery_strategy_restriction_vr_mpc_rbr_only
  ├── test_recovery_tqs_floor_060
  ├── test_recovery_exit_20_trades_rolling_pf_120
  ├── test_recovery_exit_requires_4h_minimum
  ├── test_recovery_exit_restores_normal_rules
  ├── test_recovery_reentry_no_cooldown
  ├── test_existing_positions_managed_during_recovery
  ├── test_existing_positions_count_toward_max_concurrent
  ├── test_recovery_persists_across_restart
  └── test_recovery_does_not_engage_on_warning
```

---

## 4. Regression Policy

After each phase:

1. Run **full test suite** (`pytest tests/ -v`)
2. Report exact counts: **passed / failed / skipped**
3. **Zero failures required** to pass gate
4. Any new test that fails blocks the phase — fix before proceeding
5. Any pre-existing test that regresses blocks the phase — fix before proceeding

---

## 5. Test Data Files to Create

| File | Phase | Contents |
|---|---|---|
| `tests/helpers/candle_generator.py` | 3 | Synthetic OHLCV generator with patterns: trend, range, breakout, VWAP touch, spike, chop |
| `tests/fixtures/candle_data/btc_1m_300.csv` | 3 | 300 synthetic 1m BTC candles with momentum breakout pattern embedded |
| `tests/fixtures/candle_data/sol_1m_300.csv` | 3 | 300 synthetic 1m SOL candles with pullback pattern embedded |
| `tests/fixtures/intraday/setup_scenarios.json` | 4 | Pre-computed strategy setup conditions for all 5 strategies |
| `tests/fixtures/intraday/trigger_scenarios.json` | 4 | Pre-computed strategy trigger conditions for all 5 strategies |
| `tests/fixtures/trade_sequences/` | 5-6 | JSON sequences of trades for testing learning matrices, edge monitor, failure detectors |
| `tests/fixtures/trade_sequences/ll_conflict_scenarios.json` | 6 | Trade sequences with explicit `disable_timestamp` per LL cell for Correction 3 attribution tests (re-audit recommendation 3). Each scenario has: trades with timestamps, disabled cells with disable timestamps, expected `ll_attribution_ratio`, expected Edge Monitor decision (degrade vs defer). |

---

## 6. Performance Test Requirements

| Test | Target | Phase |
|---|---|---|
| CandleBuilder: 300 1m bars → all TFs | <5ms | 3 |
| `calculate_intraday()` on 300 bars | <10ms | 3 |
| StrategyBus: 5m close → all setups evaluated | <20ms | 4 |
| StrategyBus: 1m close → all triggers evaluated | <10ms | 4 |
| GTF validation per trade | <1ms | 5 |
| TQS scoring per trade | <1ms | 5 |
| Full pipeline: 1m candle → ExecutionManager | <50ms | 8 |

---

*End of Phase 1E. Proceed to Phase 1F (Internal Audit).*
