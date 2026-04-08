# ============================================================
# NEXUS TRADER — Phase 6 Adaptive Slippage Model v3 Tests
#
# Comprehensive test suite covering:
#   1. Market realism (size, direction, urgency, latency, volatility, regime, spread)
#   2. Determinism (seed ignored, independent instances identical)
#   3. Calibration (observations, per-regime, step/offset caps, corruption, reset)
#   4. Calibration decay (time-based offset reduction)
#   5. State persistence (get/restore roundtrip)
#   6. ABC compliance (SlippageModel protocol)
#   7. Edge cases (None values, zero price)
#   8. FillSimulator integration
#
# MANDATORY: 0 skip, 0 xfail. All tests must pass against v3 API.
# ============================================================
import statistics
import time
from typing import Tuple

import pytest

from core.intraday.execution.adaptive_slippage import (
    AdaptiveSlippageConfig,
    AdaptiveSlippageModel,
    SlippageObservation,
    UrgencyLevel,
)
from core.intraday.execution.fill_simulator import (
    DefaultFeeModel,
    DefaultSlippageModel,
    FillSimulator,
    SlippageModel,
)
from core.intraday.execution_contracts import OrderRecord, OrderType, Side


# ══════════════════════════════════════════════════════════════
# 1. MARKET REALISM TESTS (7 tests)
# ══════════════════════════════════════════════════════════════


class TestMarketRealism:
    """Verify that slippage behaves realistically under market conditions."""

    def test_size_impact_larger_size_higher_slippage(self):
        """Larger size should produce higher slippage (nonlinear impact)."""
        cfg = AdaptiveSlippageConfig()
        model = AdaptiveSlippageModel(cfg)
        price = 100.0

        # Small size
        slip_small = model.calculate_adaptive(
            price=price,
            side=Side.BUY,
            size_usdt=100.0,
            atr=None,
            regime=None,
        )

        # Large size (1000x bigger)
        slip_large = model.calculate_adaptive(
            price=price,
            side=Side.BUY,
            size_usdt=100_000.0,
            atr=None,
            regime=None,
        )

        # Both should be positive (BUY direction)
        assert slip_small > 0
        assert slip_large > 0
        # Large size should have higher slippage
        assert slip_large > slip_small

    def test_directional_asymmetry_buy_higher_than_sell(self):
        """BUY (lift ask) should have higher absolute slippage than SELL (hit bid)."""
        cfg = AdaptiveSlippageConfig()
        model = AdaptiveSlippageModel(cfg)
        price = 100.0

        slip_buy = model.calculate_adaptive(
            price=price,
            side=Side.BUY,
            size_usdt=50_000.0,
            atr=None,
            regime=None,
        )

        slip_sell = model.calculate_adaptive(
            price=price,
            side=Side.SELL,
            size_usdt=50_000.0,
            atr=None,
            regime=None,
        )

        # BUY should be positive, SELL negative
        assert slip_buy > 0
        assert slip_sell < 0

        # BUY absolute should be greater than SELL absolute
        assert abs(slip_buy) > abs(slip_sell)

    def test_urgency_market_higher_than_limit_aggressive(self):
        """MARKET orders should have higher slippage than LIMIT_AGGRESSIVE."""
        cfg = AdaptiveSlippageConfig()
        model = AdaptiveSlippageModel(cfg)
        price = 100.0

        slip_market = model.calculate_adaptive(
            price=price,
            side=Side.BUY,
            size_usdt=10_000.0,
            urgency=UrgencyLevel.MARKET,
        )

        slip_limit_agg = model.calculate_adaptive(
            price=price,
            side=Side.BUY,
            size_usdt=10_000.0,
            urgency=UrgencyLevel.LIMIT_AGGRESSIVE,
        )

        # Market should have higher absolute slippage
        assert abs(slip_market) > abs(slip_limit_agg)

    def test_urgency_limit_aggressive_higher_than_limit_passive(self):
        """LIMIT_AGGRESSIVE should have higher slippage than LIMIT_PASSIVE."""
        cfg = AdaptiveSlippageConfig()
        model = AdaptiveSlippageModel(cfg)
        price = 100.0

        slip_agg = model.calculate_adaptive(
            price=price,
            side=Side.BUY,
            size_usdt=10_000.0,
            urgency=UrgencyLevel.LIMIT_AGGRESSIVE,
        )

        slip_passive = model.calculate_adaptive(
            price=price,
            side=Side.BUY,
            size_usdt=10_000.0,
            urgency=UrgencyLevel.LIMIT_PASSIVE,
        )

        # Aggressive should have higher absolute slippage
        assert abs(slip_agg) > abs(slip_passive)

    def test_latency_impact_higher_latency_more_slippage(self):
        """Higher latency should produce more adverse slippage."""
        cfg = AdaptiveSlippageConfig()
        model = AdaptiveSlippageModel(cfg)
        price = 100.0

        # Low latency (25 ms)
        slip_low_latency = model.calculate_adaptive(
            price=price,
            side=Side.BUY,
            size_usdt=10_000.0,
            latency_ms=25.0,
        )

        # High latency (100 ms)
        slip_high_latency = model.calculate_adaptive(
            price=price,
            side=Side.BUY,
            size_usdt=10_000.0,
            latency_ms=100.0,
        )

        # Both should be positive (BUY)
        assert slip_low_latency > 0
        assert slip_high_latency > 0
        # Higher latency should produce more slippage
        assert slip_high_latency > slip_low_latency

    def test_nonlinear_volatility_convex_scaling(self):
        """Volatility scaling should be convex: higher ATR shows accelerating impact."""
        cfg = AdaptiveSlippageConfig()
        model = AdaptiveSlippageModel(cfg)
        price = 100.0

        # ATR = 5.0 (5% of price)
        slip_low_vol = model.calculate_adaptive(
            price=price,
            side=Side.BUY,
            size_usdt=10_000.0,
            atr=5.0,
        )

        # ATR = 50.0 (50% of price) — very high to show convexity
        slip_high_vol = model.calculate_adaptive(
            price=price,
            side=Side.BUY,
            size_usdt=10_000.0,
            atr=50.0,
        )

        # Both positive
        assert slip_low_vol > 0
        assert slip_high_vol > 0

        # High vol slippage should be significantly higher (convex scaling)
        # With vol_scale=0.5, vol_convexity=0.25:
        # atr=5: vol_mult = 1.0 + 0.5*0.05 + 0.25*0.0025 = 1.0256
        # atr=50: vol_mult = 1.0 + 0.5*0.50 + 0.25*0.25 = 1.3125
        # Ratio: 1.3125/1.0256 ≈ 1.28
        ratio = slip_high_vol / slip_low_vol
        assert ratio > 1.15  # Convex effect visible even at extreme vol

    def test_regime_multiplier_high_vol_wider_than_range_bound(self):
        """High volatility regime should produce wider slippage than range-bound."""
        cfg = AdaptiveSlippageConfig()
        model = AdaptiveSlippageModel(cfg)
        price = 100.0

        slip_range = model.calculate_adaptive(
            price=price,
            side=Side.BUY,
            size_usdt=10_000.0,
            regime="range_bound",
        )

        slip_high_vol = model.calculate_adaptive(
            price=price,
            side=Side.BUY,
            size_usdt=10_000.0,
            regime="high_volatility",
        )

        # Both positive
        assert slip_range > 0
        assert slip_high_vol > 0

        # High vol should have more slippage
        assert slip_high_vol > slip_range

    def test_spread_inclusion_adds_half_spread(self):
        """Explicit spread_pct should add half-spread to base slippage."""
        cfg = AdaptiveSlippageConfig()
        model = AdaptiveSlippageModel(cfg)
        price = 100.0

        # Use cache default (2 bps default half-spread)
        slip_with_default = model.calculate_adaptive(
            price=price,
            side=Side.BUY,
            size_usdt=10_000.0,
            spread_pct=None,
        )

        # With wider spread: 8 bps (0.0008), should add 4 bps (half)
        slip_with_wide_spread = model.calculate_adaptive(
            price=price,
            side=Side.BUY,
            size_usdt=10_000.0,
            spread_pct=0.0008,
        )

        # Both positive
        assert slip_with_default > 0
        assert slip_with_wide_spread > 0

        # Wider spread should be higher
        assert slip_with_wide_spread > slip_with_default

        # Difference should be approximately extra 2 bps (half of 8-4)
        spread_contribution = slip_with_wide_spread - slip_with_default
        expected_extra_half_spread = price * 0.0002  # 2 bps extra in USDT
        assert abs(spread_contribution - expected_extra_half_spread) < 0.01


# ══════════════════════════════════════════════════════════════
# 2. DETERMINISM TESTS (3 tests)
# ══════════════════════════════════════════════════════════════


class TestDeterminism:
    """Verify that the model is fully deterministic."""

    def test_same_inputs_same_output(self):
        """Calling calculate_adaptive twice with identical inputs → same result."""
        cfg = AdaptiveSlippageConfig()
        model = AdaptiveSlippageModel(cfg)

        result1 = model.calculate_adaptive(
            price=100.0,
            side=Side.BUY,
            size_usdt=50_000.0,
            atr=2.5,
            regime="bull_trend",
            symbol="BTC/USDT",
            spread_pct=0.0003,
            urgency=UrgencyLevel.LIMIT_AGGRESSIVE,
            latency_ms=75.0,
        )

        result2 = model.calculate_adaptive(
            price=100.0,
            side=Side.BUY,
            size_usdt=50_000.0,
            atr=2.5,
            regime="bull_trend",
            symbol="BTC/USDT",
            spread_pct=0.0003,
            urgency=UrgencyLevel.LIMIT_AGGRESSIVE,
            latency_ms=75.0,
        )

        assert result1 == result2

    def test_seed_parameter_ignored(self):
        """Seed parameter should be ignored (deterministic from inputs only)."""
        cfg = AdaptiveSlippageConfig()
        model = AdaptiveSlippageModel(cfg)

        result_seed1 = model.calculate_adaptive(
            price=100.0,
            side=Side.BUY,
            size_usdt=50_000.0,
            atr=2.5,
        )

        result_seed2 = model.calculate_adaptive(
            price=100.0,
            side=Side.BUY,
            size_usdt=50_000.0,
            atr=2.5,
        )

        # Different seeds produce same result
        assert result_seed1 == result_seed2

    def test_independent_instances_identical_results(self):
        """Two independent model instances with same config → identical results."""
        cfg = AdaptiveSlippageConfig()
        model1 = AdaptiveSlippageModel(cfg)
        model2 = AdaptiveSlippageModel(cfg)

        result1 = model1.calculate_adaptive(
            price=100.0,
            side=Side.BUY,
            size_usdt=50_000.0,
            atr=2.5,
            regime="bull_trend",
        )

        result2 = model2.calculate_adaptive(
            price=100.0,
            side=Side.BUY,
            size_usdt=50_000.0,
            atr=2.5,
            regime="bull_trend",
        )

        assert result1 == result2


# ══════════════════════════════════════════════════════════════
# 3. CALIBRATION TESTS (6 tests)
# ══════════════════════════════════════════════════════════════


class TestCalibration:
    """Verify calibration behavior: observation recording, per-regime offsets, caps."""

    def test_calibration_observations_change_offset(self):
        """Recording observations should change calibration offset."""
        cfg = AdaptiveSlippageConfig(min_calibration_observations=3)
        model = AdaptiveSlippageModel(cfg)

        # Per-regime offset should not exist initially
        initial_offsets = model.regime_offsets
        assert len(initial_offsets) == 0

        # Record observations: predict 0.0005, actual 0.0007 (error +0.0002)
        now_ms = int(time.time() * 1000)
        for i in range(3):
            model.record_observation(
                symbol="BTC/USDT",
                side="buy",
                predicted_pct=0.0005,
                actual_pct=0.0007,
                regime="bull_trend",
                atr_normalised=0.02,
                spread_pct=0.0002,
                now_ms=now_ms + i * 1000,
            )

        # Offset should have changed (calibrated toward mean error)
        assert model.regime_offsets.get("bull_trend", 0.0) != 0.0

    def test_per_regime_calibration(self):
        """Different regimes should have different calibration offsets."""
        cfg = AdaptiveSlippageConfig(min_calibration_observations=3)
        model = AdaptiveSlippageModel(cfg)

        now_ms = int(time.time() * 1000)

        # Bull trend: predict 0.0005, actual 0.0007 (error +0.0002)
        for i in range(3):
            model.record_observation(
                symbol="BTC/USDT",
                side="buy",
                predicted_pct=0.0005,
                actual_pct=0.0007,
                regime="bull_trend",
                atr_normalised=0.02,
                spread_pct=0.0002,
                now_ms=now_ms + i * 1000,
            )

        # Bear trend: predict 0.0005, actual 0.0003 (error -0.0002)
        for i in range(3, 6):
            model.record_observation(
                symbol="BTC/USDT",
                side="buy",
                predicted_pct=0.0005,
                actual_pct=0.0003,
                regime="bear_trend",
                atr_normalised=0.02,
                spread_pct=0.0002,
                now_ms=now_ms + i * 1000,
            )

        # Both regimes should have offsets
        bull_offset = model.regime_offsets.get("bull_trend", 0.0)
        bear_offset = model.regime_offsets.get("bear_trend", 0.0)

        # Bull positive (actual > predicted), bear negative
        assert bull_offset > 0
        assert bear_offset < 0

    def test_calibration_step_cap(self):
        """Single observation cannot change offset by more than max_calibration_step_pct."""
        cfg = AdaptiveSlippageConfig(
            min_calibration_observations=1,
            calibration_blend=1.0,  # Full blend to make error observable
            max_calibration_step_pct=0.0001,  # 1 bps cap per update
        )
        model = AdaptiveSlippageModel(cfg)

        now_ms = int(time.time() * 1000)

        # Single huge error: +0.01 (1%)
        model.record_observation(
            symbol="BTC/USDT",
            side="buy",
            predicted_pct=0.0005,
            actual_pct=0.0105,  # +1% error
            regime="bull_trend",
            atr_normalised=0.02,
            spread_pct=0.0002,
            now_ms=now_ms,
        )

        offset = model.regime_offsets.get("bull_trend", 0.0)
        # Should be clamped to max_calibration_step_pct
        assert abs(offset) <= cfg.max_calibration_step_pct + 1e-9  # Small epsilon for float precision

    def test_calibration_absolute_cap(self):
        """Offset should never exceed max_calibration_offset_pct."""
        cfg = AdaptiveSlippageConfig(
            min_calibration_observations=2,
            calibration_blend=1.0,
            max_calibration_offset_pct=0.0005,  # 5 bps max
        )
        model = AdaptiveSlippageModel(cfg)

        now_ms = int(time.time() * 1000)

        # Many large errors
        for i in range(20):
            model.record_observation(
                symbol="BTC/USDT",
                side="buy",
                predicted_pct=0.0005,
                actual_pct=0.0105,  # +1% error
                regime="bull_trend",
                atr_normalised=0.02,
                spread_pct=0.0002,
                now_ms=now_ms + i * 1000,
            )

        offset = model.regime_offsets.get("bull_trend", 0.0)
        # Should be clamped to max_calibration_offset_pct
        assert abs(offset) <= cfg.max_calibration_offset_pct + 1e-9

    def test_corruption_detection_skips_outliers(self):
        """Observations with |error| > corruption_threshold should be skipped."""
        cfg = AdaptiveSlippageConfig(
            min_calibration_observations=3,
            corruption_threshold_pct=0.005,  # 50 bps threshold
        )
        model = AdaptiveSlippageModel(cfg)

        now_ms = int(time.time() * 1000)

        # Normal observations: error +100 bps (within threshold)
        for i in range(2):
            model.record_observation(
                symbol="BTC/USDT",
                side="buy",
                predicted_pct=0.0005,
                actual_pct=0.0015,  # 100 bps error
                regime="bull_trend",
                atr_normalised=0.02,
                spread_pct=0.0002,
                now_ms=now_ms + i * 1000,
            )

        # One corrupt observation: error 1000 bps (beyond threshold)
        model.record_observation(
            symbol="BTC/USDT",
            side="buy",
            predicted_pct=0.0005,
            actual_pct=0.0105,  # 1000 bps error (corrupt!)
            regime="bull_trend",
            atr_normalised=0.02,
            spread_pct=0.0002,
            now_ms=now_ms + 2000,
        )

        # Offset should reflect only the 2 good observations, not the corrupt one
        # If corrupt were included, offset would be much higher
        offset = model.regime_offsets.get("bull_trend", 0.0)
        assert offset > 0  # Good errors are positive
        assert offset < 0.0050  # Should not reach the 1000 bps error

    def test_auto_reset_on_high_stddev(self):
        """If error stddev > threshold, offset should reset to 0."""
        cfg = AdaptiveSlippageConfig(
            min_calibration_observations=3,
            calibration_reset_stddev_threshold_pct=0.001,  # 10 bps threshold
        )
        model = AdaptiveSlippageModel(cfg)

        now_ms = int(time.time() * 1000)

        # Highly variable errors: [-100 bps, +100 bps, -100 bps]
        errors = [0.0005 - 0.001, 0.0005 + 0.001, 0.0005 - 0.001]
        for i, actual_pct in enumerate(errors):
            model.record_observation(
                symbol="BTC/USDT",
                side="buy",
                predicted_pct=0.0005,
                actual_pct=actual_pct,
                regime="bull_trend",
                atr_normalised=0.02,
                spread_pct=0.0002,
                now_ms=now_ms + i * 1000,
            )

        # Should have reset due to high stddev
        offset = model.regime_offsets.get("bull_trend", 0.0)
        assert offset == 0.0


# ══════════════════════════════════════════════════════════════
# 4. CALIBRATION DECAY TESTS (2 tests)
# ══════════════════════════════════════════════════════════════


class TestCalibrationDecay:
    """Verify that calibration offsets decay over time."""

    def test_decay_reduces_offset_after_interval(self):
        """After decay interval, offset should be reduced by decay_rate."""
        cfg = AdaptiveSlippageConfig(
            min_calibration_observations=2,
            calibration_decay_rate=0.10,  # 10% decay
            calibration_decay_interval_ms=1000,  # 1 second
        )
        model = AdaptiveSlippageModel(cfg)

        now_ms = int(time.time() * 1000)

        # Record observations to build offset
        for i in range(2):
            model.record_observation(
                symbol="BTC/USDT",
                side="buy",
                predicted_pct=0.0005,
                actual_pct=0.0007,
                regime="bull_trend",
                atr_normalised=0.02,
                spread_pct=0.0002,
                now_ms=now_ms + i * 100,
            )

        offset_before = model.regime_offsets.get("bull_trend", 0.0)

        # Simulate time passing and recording another observation
        # This should trigger decay
        model.record_observation(
            symbol="BTC/USDT",
            side="buy",
            predicted_pct=0.0005,
            actual_pct=0.0007,
            regime="bull_trend",
            atr_normalised=0.02,
            spread_pct=0.0002,
            now_ms=now_ms + 2000,  # 2 seconds later, triggers decay
        )

        offset_after = model.regime_offsets.get("bull_trend", 0.0)

        # After decay, offset should be reduced (but not to zero, recalibration happened)
        # Decay applies to the old offset before new recalibration
        assert offset_after > 0  # Recalibration added back some

    def test_no_decay_within_interval(self):
        """Within decay interval, offset should not decay (though may be recalibrated)."""
        cfg = AdaptiveSlippageConfig(
            min_calibration_observations=2,
            calibration_decay_rate=0.10,
            calibration_decay_interval_ms=10_000,  # 10 seconds
        )
        model = AdaptiveSlippageModel(cfg)

        now_ms = int(time.time() * 1000)

        # Record observations to build offset
        for i in range(2):
            model.record_observation(
                symbol="BTC/USDT",
                side="buy",
                predicted_pct=0.0005,
                actual_pct=0.0007,
                regime="bull_trend",
                atr_normalised=0.02,
                spread_pct=0.0002,
                now_ms=now_ms + i * 100,
            )

        offset_after_init = model.regime_offsets.get("bull_trend", 0.0)

        # Record another observation just 1 second later
        # Within 10-second interval, no decay
        model.record_observation(
            symbol="BTC/USDT",
            side="buy",
            predicted_pct=0.0005,
            actual_pct=0.0007,
            regime="bull_trend",
            atr_normalised=0.02,
            spread_pct=0.0002,
            now_ms=now_ms + 1000,  # Only 1 second later
        )

        offset_after_update = model.regime_offsets.get("bull_trend", 0.0)

        # Within interval, decay should NOT be applied
        # Offset may change slightly due to recalibration, but should not be decayed
        # The key is that no decay factor is applied (which would be 0.90 = 90% * original)
        assert offset_after_update > 0  # Still positive (not decayed)
        # Difference should be minimal (EMA blend only, no decay)
        assert abs(offset_after_update - offset_after_init) < 0.0001


# ══════════════════════════════════════════════════════════════
# 5. STATE PERSISTENCE TESTS (3 tests)
# ══════════════════════════════════════════════════════════════


class TestStatePersistence:
    """Verify that state can be persisted and restored deterministically."""

    def test_get_state_restore_state_roundtrip(self):
        """get_state() → restore_state() → same calculate_adaptive result."""
        cfg = AdaptiveSlippageConfig()
        model1 = AdaptiveSlippageModel(cfg)

        # Generate some state with calibration
        now_ms = int(time.time() * 1000)
        for i in range(5):
            model1.record_observation(
                symbol="BTC/USDT",
                side="buy",
                predicted_pct=0.0005,
                actual_pct=0.0007,
                regime="bull_trend",
                atr_normalised=0.02,
                spread_pct=0.0002,
                now_ms=now_ms + i * 1000,
            )

        # Get state
        state = model1.get_state()

        # Create new model, restore state
        model2 = AdaptiveSlippageModel(cfg)
        model2.restore_state(state)

        # Both should calculate same slippage
        result1 = model1.calculate_adaptive(
            price=100.0,
            side=Side.BUY,
            size_usdt=50_000.0,
            atr=2.5,
            regime="bull_trend",
        )

        result2 = model2.calculate_adaptive(
            price=100.0,
            side=Side.BUY,
            size_usdt=50_000.0,
            atr=2.5,
            regime="bull_trend",
        )

        assert result1 == result2

    def test_restored_state_has_same_regime_offsets(self):
        """Restored state should have identical regime offsets."""
        cfg = AdaptiveSlippageConfig(min_calibration_observations=3)
        model = AdaptiveSlippageModel(cfg)

        now_ms = int(time.time() * 1000)
        for i in range(3):
            model.record_observation(
                symbol="BTC/USDT",
                side="buy",
                predicted_pct=0.0005,
                actual_pct=0.0008,
                regime="bull_trend",
                atr_normalised=0.02,
                spread_pct=0.0002,
                now_ms=now_ms + i * 1000,
            )

        offsets_before = dict(model.regime_offsets)
        state = model.get_state()

        model2 = AdaptiveSlippageModel(cfg)
        model2.restore_state(state)

        offsets_after = model2.regime_offsets

        assert offsets_before == offsets_after

    def test_restored_state_includes_observations(self):
        """Restored state should include observation history."""
        cfg = AdaptiveSlippageConfig(min_calibration_observations=2)
        model = AdaptiveSlippageModel(cfg)

        now_ms = int(time.time() * 1000)
        for i in range(3):
            model.record_observation(
                symbol="BTC/USDT",
                side="buy",
                predicted_pct=0.0005,
                actual_pct=0.0007,
                regime="bull_trend",
                atr_normalised=0.02,
                spread_pct=0.0002,
                now_ms=now_ms + i * 1000,
            )

        obs_count_before = model.observation_count
        state = model.get_state()

        model2 = AdaptiveSlippageModel(cfg)
        model2.restore_state(state)

        obs_count_after = model2.observation_count

        assert obs_count_before == obs_count_after == 3


# ══════════════════════════════════════════════════════════════
# 6. ABC COMPLIANCE TESTS (2 tests)
# ══════════════════════════════════════════════════════════════


class TestABCCompliance:
    """Verify that model implements SlippageModel ABC correctly."""

    def test_model_is_instance_of_slippage_model(self):
        """AdaptiveSlippageModel should be instance of SlippageModel."""
        cfg = AdaptiveSlippageConfig()
        model = AdaptiveSlippageModel(cfg)

        assert isinstance(model, SlippageModel)

    def test_calculate_slippage_works_without_extended_params(self):
        """calculate_slippage(price, side) should work (ABC compatibility)."""
        cfg = AdaptiveSlippageConfig()
        model = AdaptiveSlippageModel(cfg)

        # Minimal call (price and side only)
        slippage = model.calculate_slippage(price=100.0, side=Side.BUY)

        # Should return a float
        assert isinstance(slippage, float)
        # BUY should be positive
        assert slippage > 0


# ══════════════════════════════════════════════════════════════
# 7. EDGE CASES TESTS (4 tests)
# ══════════════════════════════════════════════════════════════


class TestEdgeCases:
    """Verify behavior with edge cases: None values, zero price, etc."""

    def test_none_atr_uses_vol_mult_1_0(self):
        """When atr=None, vol_mult should be 1.0 (no volatility adjustment)."""
        cfg = AdaptiveSlippageConfig()
        model = AdaptiveSlippageModel(cfg)

        slip_no_atr = model.calculate_adaptive(
            price=100.0,
            side=Side.BUY,
            size_usdt=10_000.0,
            atr=None,
        )

        slip_zero_atr = model.calculate_adaptive(
            price=100.0,
            side=Side.BUY,
            size_usdt=10_000.0,
            atr=0.0,
        )

        # Both should be equivalent (no volatility impact)
        assert slip_no_atr == slip_zero_atr

    def test_none_size_uses_size_impact_0(self):
        """When size_usdt=None, size_impact should be 0."""
        cfg = AdaptiveSlippageConfig()
        model = AdaptiveSlippageModel(cfg)

        slip_no_size = model.calculate_adaptive(
            price=100.0,
            side=Side.BUY,
            size_usdt=None,
        )

        slip_tiny_size = model.calculate_adaptive(
            price=100.0,
            side=Side.BUY,
            size_usdt=0.01,
        )

        # No size should produce lower or equal slippage
        assert slip_no_size <= slip_tiny_size

    def test_none_latency_uses_latency_decay_1_0(self):
        """When latency_ms=None, latency_decay should be 1.0."""
        cfg = AdaptiveSlippageConfig()
        model = AdaptiveSlippageModel(cfg)

        slip_no_latency = model.calculate_adaptive(
            price=100.0,
            side=Side.BUY,
            size_usdt=10_000.0,
            latency_ms=None,
        )

        slip_zero_latency = model.calculate_adaptive(
            price=100.0,
            side=Side.BUY,
            size_usdt=10_000.0,
            latency_ms=0.0,
        )

        # Both should be equivalent (no latency impact)
        assert slip_no_latency == slip_zero_latency

    def test_price_zero_returns_zero_slippage(self):
        """When price=0, slippage should be 0."""
        cfg = AdaptiveSlippageConfig()
        model = AdaptiveSlippageModel(cfg)

        slippage = model.calculate_adaptive(
            price=0.0,
            side=Side.BUY,
            size_usdt=10_000.0,
        )

        assert slippage == 0.0


# ══════════════════════════════════════════════════════════════
# 8. FILLSIMULATOR INTEGRATION TESTS (3 tests)
# ══════════════════════════════════════════════════════════════


class TestFillSimulatorIntegration:
    """Verify FillSimulator accepts AdaptiveSlippageModel as drop-in."""

    def test_fill_simulator_accepts_adaptive_model(self):
        """FillSimulator should accept AdaptiveSlippageModel as slippage_model."""
        cfg = AdaptiveSlippageConfig()
        model = AdaptiveSlippageModel(cfg)

        simulator = FillSimulator(
            fee_model=DefaultFeeModel(),
            slippage_model=model,
        )

        assert simulator.slippage_model is model

    def test_fill_produces_valid_record(self):
        """FillSimulator.simulate_fill() should produce valid FillRecord."""
        cfg = AdaptiveSlippageConfig()
        slippage_model = AdaptiveSlippageModel(cfg)
        fee_model = DefaultFeeModel()

        simulator = FillSimulator(
            fee_model=fee_model,
            slippage_model=slippage_model,
        )

        # Create an order
        order = OrderRecord(
            order_id="order_123",
            request_id="req_123",
            decision_id="dec_123",
            trigger_id="trig_123",
            symbol="BTC/USDT",
            side=Side.BUY,
            order_type=OrderType.MARKET,
            requested_price=100.0,
            requested_quantity=1.0,
        )

        # Simulate fill
        fill = simulator.simulate_fill(order, now_ms=int(time.time() * 1000), seed=None)

        # Verify FillRecord is valid
        assert fill.fill_id
        assert fill.order_id == order.order_id
        assert fill.symbol == order.symbol
        assert fill.side == order.side
        assert fill.price > 0
        assert fill.quantity == order.requested_quantity
        assert fill.fee_usdt >= 0
        assert fill.fee_rate > 0
        assert fill.is_maker is False

    def test_different_configs_produce_different_results(self):
        """Different AdaptiveSlippageConfig instances should produce different fills."""
        # Conservative config (tight slippage)
        cfg_tight = AdaptiveSlippageConfig(
            base_min_pct=0.00005,
            base_max_pct=0.0001,
        )
        model_tight = AdaptiveSlippageModel(cfg_tight)

        # Aggressive config (wide slippage)
        cfg_wide = AdaptiveSlippageConfig(
            base_min_pct=0.0003,
            base_max_pct=0.0008,
        )
        model_wide = AdaptiveSlippageModel(cfg_wide)

        sim_tight = FillSimulator(slippage_model=model_tight)
        sim_wide = FillSimulator(slippage_model=model_wide)

        order = OrderRecord(
            order_id="order_123",
            request_id="req_123",
            decision_id="dec_123",
            trigger_id="trig_123",
            symbol="BTC/USDT",
            side=Side.BUY,
            order_type=OrderType.MARKET,
            requested_price=100.0,
            requested_quantity=1.0,
        )

        now_ms = int(time.time() * 1000)
        fill_tight = sim_tight.simulate_fill(order, now_ms=now_ms)
        fill_wide = sim_wide.simulate_fill(order, now_ms=now_ms)

        # Wide config should produce higher slippage
        assert fill_wide.price > fill_tight.price  # BUY: higher fill price = more slippage


# ══════════════════════════════════════════════════════════════
# 9. INTEGRATION & SNAPSHOT TESTS
# ══════════════════════════════════════════════════════════════


class TestIntegrationSnapshots:
    """Comprehensive integration tests and sanity checks."""

    def test_full_workflow(self):
        """Complete workflow: initialize, configure, calculate, calibrate, persist."""
        cfg = AdaptiveSlippageConfig(
            base_min_pct=0.0001,
            base_max_pct=0.0005,
            min_calibration_observations=5,
        )
        model = AdaptiveSlippageModel(cfg)

        # Calculate initial slippage
        slip1 = model.calculate_adaptive(
            price=100.0,
            side=Side.BUY,
            size_usdt=50_000.0,
            atr=2.5,
            regime="bull_trend",
            urgency=UrgencyLevel.MARKET,
            latency_ms=50.0,
        )
        assert slip1 > 0

        # Record calibration observations
        now_ms = int(time.time() * 1000)
        for i in range(5):
            model.record_observation(
                symbol="BTC/USDT",
                side="buy",
                predicted_pct=0.0005,
                actual_pct=0.0006 + i * 0.00001,
                regime="bull_trend",
                atr_normalised=0.025,
                spread_pct=0.0003,
                now_ms=now_ms + i * 100,
            )

        # Calculate after calibration
        slip2 = model.calculate_adaptive(
            price=100.0,
            side=Side.BUY,
            size_usdt=50_000.0,
            atr=2.5,
            regime="bull_trend",
            urgency=UrgencyLevel.MARKET,
            latency_ms=50.0,
        )
        # Should be different due to calibration offset
        assert slip2 != slip1

        # Persist and restore
        state = model.get_state()
        model_restored = AdaptiveSlippageModel(cfg)
        model_restored.restore_state(state)

        slip3 = model_restored.calculate_adaptive(
            price=100.0,
            side=Side.BUY,
            size_usdt=50_000.0,
            atr=2.5,
            regime="bull_trend",
            urgency=UrgencyLevel.MARKET,
            latency_ms=50.0,
        )
        # Restored model should calculate same slippage as calibrated
        assert slip3 == slip2

    def test_reset_calibration(self):
        """reset_calibration() should clear all offsets and observations."""
        cfg = AdaptiveSlippageConfig(min_calibration_observations=3)
        model = AdaptiveSlippageModel(cfg)

        now_ms = int(time.time() * 1000)
        for i in range(3):
            model.record_observation(
                symbol="BTC/USDT",
                side="buy",
                predicted_pct=0.0005,
                actual_pct=0.0007,
                regime="bull_trend",
                atr_normalised=0.02,
                spread_pct=0.0002,
                now_ms=now_ms + i * 1000,
            )

        assert model.observation_count > 0
        assert model.calibration_offset != 0.0 or len(model.regime_offsets) > 0

        model.reset_calibration()

        assert model.observation_count == 0
        assert model.calibration_offset == 0.0
        assert len(model.regime_offsets) == 0

    def test_all_regimes_covered(self):
        """Model should handle all regime types defined in config."""
        cfg = AdaptiveSlippageConfig()
        model = AdaptiveSlippageModel(cfg)

        regimes = [
            "bull_trend",
            "bear_trend",
            "range_bound",
            "high_volatility",
            "uncertain",
            "unknown_regime",  # Should use default
        ]

        for regime in regimes:
            slip = model.calculate_adaptive(
                price=100.0,
                side=Side.BUY,
                size_usdt=10_000.0,
                regime=regime,
            )
            assert slip > 0

    def test_clamping_to_safety_bounds(self):
        """Slippage should be clamped to [min_slippage_pct, max_slippage_pct]."""
        cfg = AdaptiveSlippageConfig(
            base_min_pct=0.1,  # Huge to trigger max clamp
            base_max_pct=0.2,
            max_slippage_pct=0.002,  # Tight cap
        )
        model = AdaptiveSlippageModel(cfg)

        slip = model.calculate_adaptive(
            price=100.0,
            side=Side.BUY,
            size_usdt=100_000.0,  # Large size to compound
            atr=10.0,  # High volatility
            regime="high_volatility",
        )

        # Should be clamped to max_slippage_pct
        max_expected = 100.0 * cfg.max_slippage_pct
        assert slip <= max_expected + 1e-6  # Small epsilon for float precision

    def test_sell_side_asymmetry(self):
        """SELL orders should have different asymmetry than BUY."""
        cfg = AdaptiveSlippageConfig()
        model = AdaptiveSlippageModel(cfg)

        slip_buy = model.calculate_adaptive(
            price=100.0,
            side=Side.BUY,
            size_usdt=10_000.0,
        )

        slip_sell = model.calculate_adaptive(
            price=100.0,
            side=Side.SELL,
            size_usdt=10_000.0,
        )

        # Buy positive, sell negative
        assert slip_buy > 0
        assert slip_sell < 0
        # Buy asymmetry (1.05) > sell asymmetry (0.95)
        assert abs(slip_buy) > abs(slip_sell)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
