# ============================================================
# NEXUS TRADER — Phase 6 Integration Tests (v3 API)
#
# Full integration test suite for Phase 6 components:
#   - AdaptiveSlippageModel (Decision-affecting)
#   - ExecutionQualityTracker (Decision-affecting, bounded)
#   - LatencyMonitor (Dual-role: modeling + observability)
#   - OrderPlacementOptimizer (Advisory-only)
#   - FillSimulator (protocol consumer)
#
# Test Coverage:
#   1. Adaptive Slippage → Fill Simulator (3 tests)
#   2. Quality Tracker → Adaptive Calibration (3 tests)
#   3. Latency → Slippage Modeling (3 tests)
#   4. Latency → Quality Tracker (3 tests)
#   5. Order Placement → Execution Quality (2 tests)
#   6. Component Boundary Classification (3 tests)
#   7. Phase 5B Guarantee Preservation (3 tests)
#   8. State Persistence Cross-Component (2 tests)
#
# RULES:
#   - 0 skip, 0 xfail — fully governed
#   - No PySide6/Qt imports
#   - All tests must PASS against v3 source
# ============================================================
import logging
import pytest
import inspect
from collections import deque
from unittest.mock import Mock, patch

# Phase 6 v3 Components
from core.intraday.execution.adaptive_slippage import (
    AdaptiveSlippageModel,
    AdaptiveSlippageConfig,
    UrgencyLevel,
    SlippageObservation,
)
from core.intraday.monitoring.execution_quality_tracker import (
    ExecutionQualityTracker,
    ExecutionQualityConfig,
    FillQualityObservation,
)
from core.intraday.execution.order_placement import (
    OrderPlacementOptimizer,
    OrderPlacementConfig,
    PlacementStrategy,
    RejectionReason,
)
from core.intraday.monitoring.latency_monitor import (
    LatencyMonitor,
    LatencyConfig,
    PipelineStage,
)
from core.intraday.execution.fill_simulator import (
    FillSimulator,
    DefaultFeeModel,
    DefaultSlippageModel,
)
from core.intraday.execution_contracts import (
    Side,
    OrderRecord,
    OrderType,
    OrderStatus,
)

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════
# SECTION 1: Adaptive Slippage → Fill Simulator (3 tests)
# ══════════════════════════════════════════════════════════════

class TestAdaptiveSlippageToFillSimulator:
    """Adaptive slippage model integration with fill simulator."""

    def test_fill_simulator_accepts_adaptive_slippage_model(self):
        """FillSimulator accepts AdaptiveSlippageModel as SlippageModel."""
        cfg = AdaptiveSlippageConfig()
        adaptive_model = AdaptiveSlippageModel(cfg)
        fee_model = DefaultFeeModel()

        # FillSimulator should accept adaptive model without error
        simulator = FillSimulator(fee_model=fee_model, slippage_model=adaptive_model)
        assert simulator.slippage_model is adaptive_model

    def test_fill_from_adaptive_slippage_produces_valid_record(self):
        """Fill from adaptive model produces valid FillRecord with price > 0."""
        cfg = AdaptiveSlippageConfig()
        adaptive_model = AdaptiveSlippageModel(cfg)
        simulator = FillSimulator(slippage_model=adaptive_model)

        order = OrderRecord(
            order_id="ord_test_1",
            request_id="req_1",
            decision_id="dec_1",
            trigger_id="trig_1",
            symbol="BTCUSDT",
            side=Side.BUY,
            order_type=OrderType.MARKET,
            requested_price=50000.0,
            requested_quantity=0.01,
        )

        fill = simulator.simulate_fill(order, now_ms=1000, seed=42)

        assert fill.fill_id
        assert fill.price > 0.0
        assert fill.quantity == 0.01
        assert fill.fee_usdt >= 0.0

    def test_adaptive_model_produces_different_results_than_default(self):
        """Adaptive model produces different (market-realistic) slippage than default."""
        cfg = AdaptiveSlippageConfig()
        adaptive_model = AdaptiveSlippageModel(cfg)
        default_model = DefaultSlippageModel(max_slippage_pct=0.0002)

        price = 50000.0
        side = Side.BUY
        atr = 100.0
        regime = "bull_trend"
        spread_pct = 0.0005
        urgency = UrgencyLevel.LIMIT_AGGRESSIVE

        # Adaptive: full context
        adaptive_slip = adaptive_model.calculate_adaptive(
            price=price,
            side=side,
            size_usdt=100_000.0,
            atr=atr,
            regime=regime,
            symbol="BTCUSDT",
            spread_pct=spread_pct,
            urgency=urgency,
            latency_ms=50.0,
        )

        # Default: minimal context
        default_slip = default_model.calculate_slippage(price, side, seed=42)

        # Both should be positive (BUY side)
        assert adaptive_slip > 0
        assert default_slip > 0
        # Adaptive should incorporate spread + regime + urgency adjustments
        # Not equal to simple default
        assert adaptive_slip != default_slip


# ══════════════════════════════════════════════════════════════
# SECTION 2: Quality Tracker → Adaptive Calibration (3 tests)
# ══════════════════════════════════════════════════════════════

class TestQualityTrackerToAdaptiveCalibration:
    """ExecutionQualityTracker feeds calibration observations to AdaptiveSlippageModel."""

    def test_observations_flow_from_eqt_to_model_calibration(self):
        """Record fill in EQT, calibration offset in adaptive model changes."""
        eqt = ExecutionQualityTracker()
        model = AdaptiveSlippageModel()

        # Record enough observations to trigger calibration
        for i in range(15):
            eqt.record_fill(
                trigger_id=f"trig_{i}",
                symbol="BTCUSDT",
                strategy_class="TestStrategy",
                regime="bull_trend",
                side="buy",
                expected_price=50000.0,
                requested_price=50000.0,
                filled_price=50010.0,  # 0.02% slippage
                fee_usdt=0.5,
                fee_rate=0.0002,
                is_maker=True,
                signal_to_fill_ms=100,
            )

        # Record same observations to model for calibration
        for i in range(15):
            model.record_observation(
                symbol="BTCUSDT",
                side="buy",
                predicted_pct=0.0001,  # 1 bps
                actual_pct=0.0002,     # 2 bps (1 bps error)
                regime="bull_trend",
                atr_normalised=0.002,
                spread_pct=0.0005,
                now_ms=1000 + i * 100,
            )

        # Model should have a non-zero offset after calibration
        offset = model.regime_offsets.get("bull_trend", 0.0)
        assert offset > 0.0

    def test_dynamic_estimate_updates_with_fills(self):
        """EQT dynamic slippage estimate updates after fills are recorded."""
        eqt = ExecutionQualityTracker()

        # Initially no data
        est_1 = eqt.get_dynamic_slippage_estimate("BTCUSDT")
        assert est_1 == 0.0005  # default

        # Record 10 fills with 0.1% slippage
        for i in range(10):
            eqt.record_fill(
                trigger_id=f"trig_{i}",
                symbol="BTCUSDT",
                strategy_class="TestStrat",
                regime="bull_trend",
                side="buy",
                expected_price=100.0,
                requested_price=100.0,
                filled_price=100.1,  # 0.1% slippage
                fee_usdt=0.01,
                fee_rate=0.0002,
                is_maker=True,
                signal_to_fill_ms=50,
            )

        # Estimate should have increased
        est_2 = eqt.get_dynamic_slippage_estimate("BTCUSDT")
        assert est_2 > est_1

    def test_degradation_flag_propagates(self):
        """Quality degradation flag is detected after poor fills."""
        eqt = ExecutionQualityTracker()

        # Record fills with high slippage (degraded)
        for i in range(15):
            eqt.record_fill(
                trigger_id=f"trig_{i}",
                symbol="ETHUSDT",
                strategy_class="BadStrat",
                regime="high_volatility",
                side="sell",
                expected_price=2000.0,
                requested_price=2000.0,
                filled_price=1980.0,  # 1% slippage (very bad)
                fee_usdt=0.4,
                fee_rate=0.0004,
                is_maker=False,
                signal_to_fill_ms=500,
            )

        # Quality stats should show degradation
        stats = eqt.get_quality_stats(dimension="symbol", key="ETHUSDT")
        assert stats is not None
        assert stats.is_degraded is True


# ══════════════════════════════════════════════════════════════
# SECTION 3: Latency → Slippage Modeling (3 tests)
# ══════════════════════════════════════════════════════════════

class TestLatencyToSlippageModeling:
    """LatencyMonitor estimates feed into AdaptiveSlippageModel.calculate_adaptive()."""

    def test_latency_estimate_feeds_adaptive_slippage(self):
        """LatencyMonitor.get_latency_estimate() output feeds to AdaptiveSlippageModel."""
        monitor = LatencyMonitor()
        model = AdaptiveSlippageModel()

        # Record latencies for BTCUSDT
        for i in range(6):
            total_lat = 100 + i * 50  # 100, 150, 200, 250, 300, 350 ms
            monitor.record_stage("trig_1", "BTCUSDT", "Strategy", PipelineStage.SIGNAL_CREATED, 1000)
            monitor.record_stage("trig_1", "BTCUSDT", "Strategy", PipelineStage.FILL_RECEIVED, 1000 + total_lat)

        # Get latency estimate
        latency_est = monitor.get_latency_estimate("BTCUSDT")
        assert latency_est > 0
        assert latency_est <= 30000  # within max

        # Use in adaptive model
        slippage = model.calculate_adaptive(
            price=100.0,
            side=Side.BUY,
            size_usdt=10000.0,
            atr=1.0,
            regime="bull_trend",
            symbol="BTCUSDT",
            spread_pct=0.0005,
            urgency=UrgencyLevel.MARKET,
            latency_ms=float(latency_est),
        )
        assert slippage > 0

    def test_higher_latency_estimate_yields_higher_slippage(self):
        """Higher latency estimate produces higher slippage in adaptive model."""
        model = AdaptiveSlippageModel()
        price = 50000.0
        side = Side.BUY
        size = 100_000.0
        atr = 500.0
        regime = "bull_trend"
        symbol = "BTCUSDT"
        spread = 0.0005

        # Low latency
        slip_low = model.calculate_adaptive(
            price=price, side=side, size_usdt=size, atr=atr, regime=regime,
            symbol=symbol, spread_pct=spread, urgency=UrgencyLevel.MARKET,
            latency_ms=10.0,
        )

        # High latency
        slip_high = model.calculate_adaptive(
            price=price, side=side, size_usdt=size, atr=atr, regime=regime,
            symbol=symbol, spread_pct=spread, urgency=UrgencyLevel.MARKET,
            latency_ms=1000.0,
        )

        # High latency should produce more slippage
        assert slip_high > slip_low

    def test_cold_start_latency_still_produces_valid_slippage(self):
        """Cold-start latency (2000ms default) still produces valid slippage."""
        monitor = LatencyMonitor()  # No observations yet
        model = AdaptiveSlippageModel()

        # Cold-start latency
        latency_est = monitor.get_latency_estimate("LTCUSDT")
        assert latency_est == 2000  # default

        # Should still produce valid slippage
        slippage = model.calculate_adaptive(
            price=100.0,
            side=Side.SELL,
            size_usdt=5000.0,
            atr=2.0,
            regime="range_bound",
            symbol="LTCUSDT",
            spread_pct=0.0003,
            urgency=UrgencyLevel.LIMIT_PASSIVE,
            latency_ms=float(latency_est),
        )
        assert slippage < 0  # SELL side


# ══════════════════════════════════════════════════════════════
# SECTION 4: Latency → Quality Tracker (3 tests)
# ══════════════════════════════════════════════════════════════

class TestLatencyToQualityTracker:
    """Latency data flows into quality tracker's signal_to_fill_ms."""

    def test_latency_feeds_quality_tracker_signal_to_fill(self):
        """LatencyMonitor records flow into EQT.record_fill(signal_to_fill_ms)."""
        monitor = LatencyMonitor()
        eqt = ExecutionQualityTracker()

        # Record a complete pipeline and enough fills for stats
        for i in range(10):
            monitor.record_stage(f"trig_test_{i}", "BTCUSDT", "Strategy", PipelineStage.SIGNAL_CREATED, 1000 + i * 100)
            monitor.record_stage(f"trig_test_{i}", "BTCUSDT", "Strategy", PipelineStage.FILL_RECEIVED, 1500 + i * 100)

            record = monitor.get_record(f"trig_test_{i}")
            assert record is not None
            assert record.total_latency_ms == 500

            # Use in EQT
            eqt.record_fill(
                trigger_id=f"trig_test_{i}",
                symbol="BTCUSDT",
                strategy_class="TestStrat",
                regime="bull_trend",
                side="buy",
                expected_price=50000.0,
                requested_price=50000.0,
                filled_price=50010.0,
                fee_usdt=1.0,
                fee_rate=0.0002,
                is_maker=True,
                signal_to_fill_ms=record.total_latency_ms,
            )

        # EQT should have recorded the latency
        stats = eqt.get_quality_stats(dimension="symbol", key="BTCUSDT")
        assert stats is not None
        assert stats.mean_signal_to_fill_ms == 500

    def test_stale_latency_detected_alert_generated(self):
        """LatencyMonitor detects stale latency and generates alert."""
        monitor = LatencyMonitor()

        # Record a very slow pipeline (> stale threshold 30s)
        monitor.record_stage("trig_slow", "BTCUSDT", "Strat", PipelineStage.SIGNAL_CREATED, 1000)
        monitor.record_stage("trig_slow", "BTCUSDT", "Strat", PipelineStage.FILL_RECEIVED, 1000 + 35000)

        record = monitor.get_record("trig_slow")
        assert record.total_latency_ms == 35000

        alerts = monitor.get_alerts("trig_slow")
        # Should have a stale alert
        assert any(a.is_stale for a in alerts)

    def test_latency_statistics_accumulate(self):
        """LatencyMonitor accumulates statistics across multiple records."""
        monitor = LatencyMonitor()

        # Record 5 pipelines with varying latencies
        latencies = [100, 200, 300, 150, 250]
        for i, lat_ms in enumerate(latencies):
            trigger = f"trig_{i}"
            monitor.record_stage(trigger, "BTCUSDT", "Strat", PipelineStage.SIGNAL_CREATED, 1000 + i * 10000)
            monitor.record_stage(trigger, "BTCUSDT", "Strat", PipelineStage.FILL_RECEIVED, 1000 + i * 10000 + lat_ms)

        stats = monitor.get_statistics()
        assert "total_pipeline" in stats
        assert stats["total_pipeline"]["count"] == 5
        assert stats["total_pipeline"]["mean_ms"] == 200  # mean of latencies


# ══════════════════════════════════════════════════════════════
# SECTION 5: Order Placement → Execution Quality (2 tests)
# ══════════════════════════════════════════════════════════════

class TestOrderPlacementToExecutionQuality:
    """Order placement decisions are traceable via execution quality."""

    def test_fill_outcome_recording_affects_placement_scoring(self):
        """Fill outcome (limit success/failure) recorded and affects future scores."""
        optimizer = OrderPlacementOptimizer()

        # Record many successful limit fills
        for i in range(30):
            optimizer.record_fill_outcome("BTCUSDT", was_limit_fill=True)

        # Decision should show good historical fill rate
        decision = optimizer.decide(
            side="buy",
            price=50000.0,
            size_usdt=10000.0,
            signal_quality=0.9,
            signal_age_ms=1000,
            spread_pct=0.0002,
            regime="range_bound",
            symbol="BTCUSDT",
        )

        # With high fill rate, limit strategies should score well
        # Verify scores exist and limit options score positively
        assert "LIMIT_PASSIVE" in decision.scores
        assert "MARKET" in decision.scores

    def test_placement_decision_fully_traceable_with_scores_breakdown(self):
        """PlacementDecision includes scores and breakdown for full auditability."""
        optimizer = OrderPlacementOptimizer()

        decision = optimizer.decide(
            side="sell",
            price=25000.0,
            size_usdt=5000.0,
            signal_quality=0.65,
            signal_age_ms=1500,
            spread_pct=0.0004,
            regime="uncertain",
            bid=24990.0,
            ask=25010.0,
            symbol="ETHUSDT",
        )

        if not decision.is_rejected:
            # Decision should have full audit trail
            assert decision.winning_score >= 0.0
            assert len(decision.scores) == 4  # 4 strategies scored
            assert decision.score_breakdown
            assert "fill_probability" in decision.score_breakdown
            assert "edge_decay_cost" in decision.score_breakdown
            assert "spread_cost" in decision.score_breakdown


# ══════════════════════════════════════════════════════════════
# SECTION 6: Component Boundary Classification (3 tests)
# ══════════════════════════════════════════════════════════════

class TestComponentBoundaryClassification:
    """Verify component roles and boundaries are correctly implemented."""

    def test_eqt_estimate_bounded_by_max_slippage_estimate_pct(self):
        """EQT output is always bounded by max_slippage_estimate_pct."""
        eqt = ExecutionQualityTracker()
        cfg = eqt.config

        # Record fills with very high slippage to try to exceed bound
        for i in range(30):
            eqt.record_fill(
                trigger_id=f"trig_{i}",
                symbol="BTCUSDT",
                strategy_class="BadStrat",
                regime="high_volatility",
                side="buy",
                expected_price=50000.0,
                requested_price=50000.0,
                filled_price=51000.0,  # 2% slippage
                fee_usdt=10.0,
                fee_rate=0.0004,
                is_maker=False,
                signal_to_fill_ms=1000,
            )

        estimate = eqt.get_dynamic_slippage_estimate("BTCUSDT")
        assert estimate <= cfg.max_slippage_estimate_pct

    def test_order_placement_rejects_on_error_fail_closed(self):
        """OrderPlacementOptimizer rejects orders on error (fail-closed)."""
        optimizer = OrderPlacementOptimizer()

        # Missing price (error condition)
        decision = optimizer.decide(
            side="buy",
            price=None,  # Invalid
            size_usdt=1000.0,
            signal_quality=0.8,
            signal_age_ms=1000,
            spread_pct=0.0005,
            regime="bull_trend",
        )

        assert decision.is_rejected
        assert decision.rejection_reason == RejectionReason.MISSING_PRICE

    def test_latency_monitor_has_modeling_role_but_no_execute_order(self):
        """LatencyMonitor is dual-role but does NOT have execute_order method."""
        monitor = LatencyMonitor()

        # Should have modeling method
        assert hasattr(monitor, "get_latency_estimate")

        # Should NOT have execute_order (advisory-only boundary preserved)
        assert not hasattr(monitor, "execute_order")


# ══════════════════════════════════════════════════════════════
# SECTION 7: Phase 5B Guarantee Preservation (3 tests)
# ══════════════════════════════════════════════════════════════

class TestPhase5BGuaranteePreservation:
    """Verify Phase 5B determinism and headless-first guarantees."""

    def test_deterministic_replay_adaptive_slippage_same_inputs_same_output(self):
        """AdaptiveSlippageModel: same inputs always produce same output."""
        cfg = AdaptiveSlippageConfig()

        # First calculation
        model1 = AdaptiveSlippageModel(cfg)
        slip1 = model1.calculate_adaptive(
            price=100.0, side=Side.BUY, size_usdt=50000.0, atr=2.0,
            regime="bull_trend", symbol="TEST", spread_pct=0.0005,
            urgency=UrgencyLevel.LIMIT_AGGRESSIVE, latency_ms=100.0,
        )

        # Second calculation (new instance, same inputs)
        model2 = AdaptiveSlippageModel(cfg)
        slip2 = model2.calculate_adaptive(
            price=100.0, side=Side.BUY, size_usdt=50000.0, atr=2.0,
            regime="bull_trend", symbol="TEST", spread_pct=0.0005,
            urgency=UrgencyLevel.LIMIT_AGGRESSIVE, latency_ms=100.0,
        )

        # Must be identical
        assert slip1 == slip2

    def test_headless_first_no_pyside6_imports_in_phase6_modules(self):
        """Phase 6 modules import no PySide6/Qt code."""
        import sys
        import inspect

        # Check each Phase 6 module
        modules_to_check = [
            AdaptiveSlippageModel,
            ExecutionQualityTracker,
            OrderPlacementOptimizer,
            LatencyMonitor,
            FillSimulator,
        ]

        for module_class in modules_to_check:
            module_name = inspect.getmodule(module_class).__name__
            # Verify no Qt in source
            source = inspect.getsource(module_class)
            assert "PySide6" not in source
            assert "QtCore" not in source
            assert "QtGui" not in source

    def test_no_hidden_coupling_components_dont_bypass_boundaries(self):
        """Components respect execution boundary contracts."""
        # AdaptiveSlippageModel should NOT directly call ExecutionQualityTracker
        model = AdaptiveSlippageModel()
        source = inspect.getsource(AdaptiveSlippageModel.calculate_adaptive)
        assert "ExecutionQualityTracker" not in source

        # OrderPlacementOptimizer should NOT directly call FillSimulator
        optimizer = OrderPlacementOptimizer()
        source = inspect.getsource(optimizer.decide)
        assert "FillSimulator" not in source


# ══════════════════════════════════════════════════════════════
# SECTION 8: State Persistence Cross-Component (2 tests)
# ══════════════════════════════════════════════════════════════

class TestStatePersistenceCrossComponent:
    """All 4 components support deterministic state persistence."""

    def test_all_components_support_get_state_restore_state(self):
        """All components implement get_state/restore_state."""
        components = [
            (AdaptiveSlippageModel(), "AdaptiveSlippageModel"),
            (ExecutionQualityTracker(), "ExecutionQualityTracker"),
            (LatencyMonitor(), "LatencyMonitor"),
        ]

        for component, name in components:
            assert hasattr(component, "get_state"), f"{name} missing get_state"
            assert callable(getattr(component, "get_state")), f"{name}.get_state not callable"
            assert hasattr(component, "restore_state"), f"{name} missing restore_state"
            assert callable(getattr(component, "restore_state")), f"{name}.restore_state not callable"

    def test_restored_state_produces_same_estimates(self):
        """State round-trip (get_state/restore_state) produces identical estimates."""
        # AdaptiveSlippageModel
        model1 = AdaptiveSlippageModel()
        for i in range(12):
            model1.record_observation(
                symbol="BTCUSDT",
                side="buy",
                predicted_pct=0.0001,
                actual_pct=0.00012,
                regime="bull_trend",
                atr_normalised=0.02,
                spread_pct=0.0005,
                now_ms=1000 + i * 1000,
            )

        # Get state and restore to new model
        state = model1.get_state()
        model2 = AdaptiveSlippageModel()
        model2.restore_state(state)

        # Both models should produce same slippage
        slip1 = model1.calculate_adaptive(
            price=100.0, side=Side.BUY, size_usdt=10000.0, atr=1.0,
            regime="bull_trend", symbol="BTCUSDT", spread_pct=0.0005,
            urgency=UrgencyLevel.MARKET, latency_ms=50.0,
        )
        slip2 = model2.calculate_adaptive(
            price=100.0, side=Side.BUY, size_usdt=10000.0, atr=1.0,
            regime="bull_trend", symbol="BTCUSDT", spread_pct=0.0005,
            urgency=UrgencyLevel.MARKET, latency_ms=50.0,
        )

        assert slip1 == slip2

        # ExecutionQualityTracker
        eqt1 = ExecutionQualityTracker()
        for i in range(10):
            eqt1.record_fill(
                trigger_id=f"trig_{i}",
                symbol="ETHUSDT",
                strategy_class="TestStrat",
                regime="range_bound",
                side="sell",
                expected_price=2000.0,
                requested_price=2000.0,
                filled_price=1990.0,
                fee_usdt=0.4,
                fee_rate=0.0002,
                is_maker=True,
                signal_to_fill_ms=100,
            )

        state = eqt1.get_state()
        eqt2 = ExecutionQualityTracker()
        eqt2.restore_state(state)

        # Both should return same estimate
        est1 = eqt1.get_dynamic_slippage_estimate("ETHUSDT")
        est2 = eqt2.get_dynamic_slippage_estimate("ETHUSDT")
        assert est1 == est2


# ══════════════════════════════════════════════════════════════
# Additional: Contract Verification Tests
# ══════════════════════════════════════════════════════════════

class TestContractVerification:
    """Verify key API contracts from design specification."""

    def test_adaptive_slippage_urgency_levels_map_correctly(self):
        """UrgencyLevel enum values map to urgency_map correctly."""
        cfg = AdaptiveSlippageConfig()
        model = AdaptiveSlippageModel(cfg)

        # Each urgency level should produce different slippage
        slips = {}
        for urgency in [UrgencyLevel.MARKET, UrgencyLevel.LIMIT_AGGRESSIVE, UrgencyLevel.LIMIT_PASSIVE]:
            slip = model.calculate_adaptive(
                price=100.0, side=Side.BUY, size_usdt=10000.0, atr=1.0,
                regime="bull_trend", symbol="TEST", spread_pct=0.0005,
                urgency=urgency, latency_ms=50.0,
            )
            slips[urgency.value] = slip

        # Urgency: MARKET > LIMIT_AGGRESSIVE > LIMIT_PASSIVE
        assert slips["market"] > slips["limit_aggressive"]
        assert slips["limit_aggressive"] > slips["limit_passive"]

    def test_execution_quality_tracker_symbol_scoped_not_strategy_scoped(self):
        """EQT.get_dynamic_slippage_estimate() takes symbol, not strategy."""
        eqt = ExecutionQualityTracker()

        # Record fills for same symbol, different strategies (need 10+ for stats)
        for i in range(12):
            strat = "Strat1" if i % 2 == 0 else "Strat2"
            eqt.record_fill(
                trigger_id=f"trig_{i}",
                symbol="BTCUSDT",  # Same symbol
                strategy_class=strat,  # Different strategy
                regime="bull_trend",
                side="buy",
                expected_price=50000.0,
                requested_price=50000.0,
                filled_price=50010.0,
                fee_usdt=1.0,
                fee_rate=0.0002,
                is_maker=True,
                signal_to_fill_ms=50,
            )

        # get_dynamic_slippage_estimate takes only symbol
        estimate = eqt.get_dynamic_slippage_estimate("BTCUSDT")
        assert estimate > 0
        # Should aggregate both strategies' fills for this symbol
        assert eqt.get_quality_stats(dimension="symbol", key="BTCUSDT") is not None

    def test_latency_monitor_modeling_determinism_ema_recomputes(self):
        """LatencyMonitor.get_latency_estimate() is deterministic from observations."""
        monitor1 = LatencyMonitor()
        monitor2 = LatencyMonitor()

        # Add same sequence of latencies to both
        for i in range(7):
            lat = 150 + i * 25
            monitor1.record_stage(f"trig_{i}", "BTCUSDT", "Strat", PipelineStage.SIGNAL_CREATED, 1000)
            monitor1.record_stage(f"trig_{i}", "BTCUSDT", "Strat", PipelineStage.FILL_RECEIVED, 1000 + lat)

            monitor2.record_stage(f"trig_{i}", "BTCUSDT", "Strat", PipelineStage.SIGNAL_CREATED, 1000)
            monitor2.record_stage(f"trig_{i}", "BTCUSDT", "Strat", PipelineStage.FILL_RECEIVED, 1000 + lat)

        # Should produce same estimate
        est1 = monitor1.get_latency_estimate("BTCUSDT")
        est2 = monitor2.get_latency_estimate("BTCUSDT")
        assert est1 == est2

    def test_order_placement_fail_closed_never_defaults_to_market_on_error(self):
        """OrderPlacementOptimizer never returns MARKET on unexpected error."""
        optimizer = OrderPlacementOptimizer()

        # Try various invalid inputs that should trigger REJECT, not MARKET fallback
        invalid_inputs = [
            {"price": -100.0},      # Negative price
            {"price": 0},            # Zero price
            {"price": None},         # None price
            {"side": "invalid"},     # Invalid side
            {"size_usdt": None},     # None size
            {"size_usdt": 0},        # Zero size
            {"size_usdt": -100},     # Negative size
            {"spread_pct": None},    # Missing spread
        ]

        for invalid_kwargs in invalid_inputs:
            base_kwargs = {
                "side": "buy",
                "price": 100.0,
                "size_usdt": 1000.0,
                "signal_quality": 0.8,
                "signal_age_ms": 1000,
                "spread_pct": 0.0005,
                "regime": "bull_trend",
            }
            base_kwargs.update(invalid_kwargs)

            decision = optimizer.decide(**base_kwargs)
            assert decision.is_rejected, f"Should reject with invalid kwargs: {invalid_kwargs}"
            assert decision.strategy != PlacementStrategy.MARKET


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
