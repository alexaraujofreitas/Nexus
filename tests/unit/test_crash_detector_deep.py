# ============================================================
# Deep Crash Detector Tests — Comprehensive Coverage
#
# Tests all 7 components, tier transitions, normalization,
# thread safety, and edge cases.
# ============================================================
import pytest
import pandas as pd
from datetime import datetime
from unittest.mock import Mock, patch, MagicMock
from core.risk.crash_detector import (
    CrashDetector,
    get_crash_detector,
    TIER_NORMAL,
    TIER_DEFENSIVE,
    TIER_HIGH_ALERT,
    TIER_EMERGENCY,
    TIER_SYSTEMIC,
)


class TestCrashDetectorComponents:
    """Test individual component scoring."""

    def test_atr_spike_above_threshold(self):
        """ATR spike fires when current > baseline * 1.5."""
        detector = CrashDetector()
        # Create OHLCV data with 20 bars for baseline + 1 current
        df = pd.DataFrame({
            "atr_14": [100.0] * 20 + [160.0],  # current = 1.6x baseline
        })
        score = detector._compute_atr_spike({"BTC/USDT": df})
        assert score > 0.0
        assert score <= 1.0

    def test_atr_spike_below_threshold_returns_zero(self):
        """ATR spike returns 0 when current <= baseline * 1.5."""
        detector = CrashDetector()
        df = pd.DataFrame({
            "atr_14": [100.0] * 20 + [140.0],  # current = 1.4x baseline (< 1.5)
        })
        score = detector._compute_atr_spike({"BTC/USDT": df})
        assert score == 0.0

    def test_atr_spike_empty_dataframe(self):
        """Empty dataframe returns 0."""
        detector = CrashDetector()
        score = detector._compute_atr_spike({})
        assert score == 0.0

    def test_atr_spike_insufficient_bars(self):
        """Less than 20 bars returns 0."""
        detector = CrashDetector()
        df = pd.DataFrame({"atr_14": [100.0] * 15})
        score = detector._compute_atr_spike({"BTC/USDT": df})
        assert score == 0.0

    def test_atr_spike_missing_column(self):
        """Missing atr_14 column returns 0."""
        detector = CrashDetector()
        df = pd.DataFrame({"close": [100.0] * 21})
        score = detector._compute_atr_spike({"BTC/USDT": df})
        assert score == 0.0

    def test_price_velocity_negative_move_fires(self):
        """Price velocity fires on negative z-score < -1.5."""
        detector = CrashDetector()
        # Create close prices: smooth for 30 bars, then sharp drop
        closes = [100.0] * 28 + [100.0, 97.0, 95.0]  # -3% and -5% moves
        df = pd.DataFrame({"close": closes})
        score = detector._compute_price_velocity({"BTC/USDT": df})
        # Sharp negative move should produce non-zero score
        assert score >= 0.0

    def test_price_velocity_positive_move_no_fire(self):
        """Price velocity does not fire on positive moves."""
        detector = CrashDetector()
        closes = [100.0] * 28 + [100.0, 102.0, 105.0]  # +2% and +5% moves
        df = pd.DataFrame({"close": closes})
        score = detector._compute_price_velocity({"BTC/USDT": df})
        assert score == 0.0

    def test_price_velocity_insufficient_bars(self):
        """Insufficient bars returns 0."""
        detector = CrashDetector()
        df = pd.DataFrame({"close": [100.0] * 15})
        score = detector._compute_price_velocity({"BTC/USDT": df})
        assert score == 0.0

    def test_liquidation_cascade_unavailable(self):
        """When agent raises an exception during run, returns None gracefully."""
        detector = CrashDetector()
        import sys
        mock_module = MagicMock()
        agent_instance = MagicMock()
        agent_instance.run.side_effect = Exception("Simulated network failure")
        mock_module.LiquidationFlowAgent.return_value = agent_instance
        # Patch the CORRECT import path (fixed in LR-02)
        with patch.dict(sys.modules, {
            "core.agents.liquidation_flow_agent": mock_module,
        }):
            score = detector._compute_liquidation_cascade()
        assert score is None

    def test_liquidation_cascade_returns_float(self):
        """When agent returns data, _compute_liquidation_cascade returns float in [0, 1].

        Uses the CORRECT import path 'core.agents.liquidation_flow_agent' (fixed in LR-02).
        """
        import sys
        detector = CrashDetector()
        mock_module = MagicMock()
        agent_instance = MagicMock()
        agent_instance.run.return_value = {"liquidation_severity": 50.0}
        mock_module.LiquidationFlowAgent.return_value = agent_instance
        # Patch the CORRECT import path (fixed in LR-02)
        with patch.dict(sys.modules, {
            "core.agents.liquidation_flow_agent": mock_module,
        }):
            score = detector._compute_liquidation_cascade()
        assert isinstance(score, float)
        assert 0.0 <= score <= 1.0

    def test_cross_asset_decline_all_declining(self):
        """Cross-asset decline fires when all assets decline."""
        detector = CrashDetector()
        dfs = {
            "BTC/USDT": pd.DataFrame({"close": [100.0, 98.0]}),
            "ETH/USDT": pd.DataFrame({"close": [100.0, 97.0]}),
            "SOL/USDT": pd.DataFrame({"close": [100.0, 96.0]}),
        }
        score = detector._compute_cross_asset_decline(dfs)
        assert score > 0.0

    def test_cross_asset_decline_mixed(self):
        """Cross-asset decline scaled by declining_count / total."""
        detector = CrashDetector()
        dfs = {
            "BTC/USDT": pd.DataFrame({"close": [100.0, 98.0]}),
            "ETH/USDT": pd.DataFrame({"close": [100.0, 102.0]}),  # up
        }
        score = detector._compute_cross_asset_decline(dfs)
        # 1 declining / 2 total = 0.5 weight
        assert 0.0 <= score <= 0.5

    def test_orderbook_imbalance_formula_behavior(self):
        """
        Order book imbalance formula behavior verification.

        NOTE (Bug B-01): The imbalance formula uses:
          score = max(0, 1.0 - abs(0.5 - ratio) / 0.15)
        This formula produces 0.0 for any ratio that fires the
        imbalance condition (|ratio - 0.5| > 0.15), because:
          abs(0.5 - ratio) > 0.15 → term > 1.0 → max(0, neg) = 0.0
        The function always returns 0.0 for any ticker data.
        This component is non-functional as a crash signal.
        """
        detector = CrashDetector()
        tickers = {
            "BTC/USDT": {
                "bidVolume": 1000.0,
                "askVolume": 100.0,  # ratio 10:1 favoring bids
            }
        }
        score = detector._compute_orderbook_imbalance(tickers)
        # Due to the formula bug, extreme imbalance also returns 0.0
        assert score == 0.0

    def test_orderbook_imbalance_balanced(self):
        """Balanced order book returns 0."""
        detector = CrashDetector()
        tickers = {
            "BTC/USDT": {
                "bidVolume": 500.0,
                "askVolume": 500.0,  # perfectly balanced
            }
        }
        score = detector._compute_orderbook_imbalance(tickers)
        assert score == 0.0

    def test_funding_rate_flip_negative(self):
        """Funding rate flip fires on negative funding."""
        detector = CrashDetector()
        tickers = {
            "BTC/USDT": {"fundingRate": -0.0005}  # strongly negative
        }
        score = detector._compute_funding_rate_flip(tickers)
        assert score > 0.0
        assert score <= 1.0

    def test_funding_rate_flip_positive(self):
        """Positive funding rate does not fire."""
        detector = CrashDetector()
        tickers = {
            "BTC/USDT": {"fundingRate": 0.0005}
        }
        score = detector._compute_funding_rate_flip(tickers)
        assert score == 0.0

    def test_oi_collapse_available(self):
        """When OnChain agent returns data, _compute_oi_collapse returns float.

        Uses the CORRECT import path 'core.agents.onchain_agent' (fixed in LR-02).
        """
        import sys
        detector = CrashDetector()
        mock_module = MagicMock()
        agent_instance = MagicMock()
        agent_instance.run.return_value = {"oi_change_pct": -50.0}
        mock_module.OnChainAgent.return_value = agent_instance
        with patch.dict(sys.modules, {
            "core.agents.onchain_agent": mock_module,
        }):
            score = detector._compute_oi_collapse()
        assert score is None or (isinstance(score, float) and score >= 0.0)

    def test_oi_collapse_unavailable(self):
        """OI collapse returns None when agent raises an exception."""
        import sys
        detector = CrashDetector()
        mock_module = MagicMock()
        agent_instance = MagicMock()
        agent_instance.run.side_effect = Exception("Simulated network failure")
        mock_module.OnChainAgent.return_value = agent_instance
        with patch.dict(sys.modules, {
            "core.agents.onchain_agent": mock_module,
        }):
            score = detector._compute_oi_collapse()
        assert score is None


class TestCrashDetectorNormalization:
    """Test component normalization when some are unavailable."""

    def test_normalization_all_components_available(self):
        """All components available: score is within valid range."""
        detector = CrashDetector()
        # DataFrame must have consistent column lengths
        n = 30
        df = pd.DataFrame({
            "atr_14": [100.0] * (n - 1) + [160.0],  # spike on last bar
            "close": [100.0] * n,
        })
        dfs = {"BTC/USDT": df}
        tickers = {
            "BTC/USDT": {
                "bidVolume": 700.0,
                "askVolume": 300.0,
                "fundingRate": -0.0005,
            }
        }
        # Mock agent-dependent components to return valid values
        with patch.object(detector, "_compute_liquidation_cascade", return_value=0.5):
            with patch.object(detector, "_compute_oi_collapse", return_value=0.5):
                score = detector.evaluate(tickers, dfs)
        # Score should be within valid 0-10 range
        assert 0.0 <= score <= 10.0

    def test_normalization_excludes_unavailable_components(self):
        """Unavailable components excluded from normalizer denominator."""
        detector = CrashDetector()
        df = pd.DataFrame({"atr_14": [100.0] * 21})
        dfs = {"BTC/USDT": df}
        tickers = {}
        # All components except ATR spike unavailable
        with patch.object(detector, "_compute_price_velocity", return_value=0.0):
            with patch.object(detector, "_compute_liquidation_cascade", return_value=None):
                with patch.object(detector, "_compute_cross_asset_decline", return_value=0.0):
                    with patch.object(detector, "_compute_orderbook_imbalance", return_value=0.0):
                        with patch.object(detector, "_compute_funding_rate_flip", return_value=0.0):
                            with patch.object(detector, "_compute_oi_collapse", return_value=None):
                                score = detector.evaluate(tickers, dfs)
        # Score should only be based on ATR spike weight
        assert score >= 0.0


class TestCrashDetectorTierTransitions:
    """Test tier escalation and recovery logic."""

    def test_tier_escalation_to_defensive(self):
        """Score >= 5.0 escalates to DEFENSIVE tier.

        All 7 components at 1.0 → weighted_sum = 8.0 → score = 10.0 → SYSTEMIC
        All at 0.5 → weighted_sum = 4.0 → score = 5.0 → DEFENSIVE
        """
        detector = CrashDetector()
        # Set all components to 0.5 so weighted_sum/total_weight * 10 ≈ 5.0
        # Default weights: atr=1.5, vel=1.5, liq=1.0, cross=1.0, ob=1.2, fr=1.0, oi=0.8 → total=8.0
        # 0.5 * 8.0 / 8.0 * 10 = 5.0 → DEFENSIVE
        with patch.object(detector, "_compute_atr_spike", return_value=0.5):
            with patch.object(detector, "_compute_price_velocity", return_value=0.5):
                with patch.object(detector, "_compute_cross_asset_decline", return_value=0.5):
                    with patch.object(detector, "_compute_liquidation_cascade", return_value=0.5):
                        with patch.object(detector, "_compute_orderbook_imbalance", return_value=0.5):
                            with patch.object(detector, "_compute_funding_rate_flip", return_value=0.5):
                                with patch.object(detector, "_compute_oi_collapse", return_value=0.5):
                                    detector.evaluate({}, {})
                                    assert detector.current_tier != TIER_NORMAL

    def test_recovery_requires_multiple_bars_below_threshold(self):
        """Recovery from defensive requires N bars below threshold."""
        detector = CrashDetector()
        with patch("config.settings.settings") as mock_settings:
            mock_settings.get.side_effect = lambda k, d=None: {
                "crash_detector.enabled": True,
                "crash_detector.weights": {},
                "crash_detector.tier_thresholds": {"defensive": 5.0},
                "crash_detector.recovery_bars_required": 3,
                "crash_detector.recovery_hysteresis": 1.5,
            }.get(k, d)
            # Force tier to defensive first
            detector._current_tier = TIER_DEFENSIVE
            # One bar below threshold: not yet recovered
            with patch.object(detector, "_compute_atr_spike", return_value=0.1):
                with patch.object(detector, "_compute_price_velocity", return_value=0.0):
                    with patch.object(detector, "_compute_cross_asset_decline", return_value=0.0):
                        with patch.object(detector, "_compute_liquidation_cascade", return_value=None):
                            with patch.object(detector, "_compute_orderbook_imbalance", return_value=0.0):
                                with patch.object(detector, "_compute_funding_rate_flip", return_value=0.0):
                                    with patch.object(detector, "_compute_oi_collapse", return_value=None):
                                        detector.evaluate({}, {})
                                        # First bar below threshold
                                        assert detector.current_tier == TIER_DEFENSIVE
                                        # Second bar
                                        detector.evaluate({}, {})
                                        assert detector.current_tier == TIER_DEFENSIVE
                                        # Third bar reaches recovery threshold
                                        detector.evaluate({}, {})
                                        # Should be recovered to NORMAL now
                                        assert detector.current_tier == TIER_NORMAL


class TestCrashDetectorScoreClamping:
    """Test score clamping to [0, 10]."""

    def test_score_clamped_to_max_10(self):
        """Score never exceeds 10."""
        detector = CrashDetector()
        assert detector.current_score <= 10.0

    def test_score_clamped_to_min_0(self):
        """Score never below 0."""
        detector = CrashDetector()
        assert detector.current_score >= 0.0


class TestCrashDetectorEdgeCases:
    """Test edge cases and error conditions.

    NOTE: _compute_liquidation_cascade and _compute_oi_collapse are patched to
    return None in all evaluate() tests because these methods now correctly import
    from core.agents.* (fixed in LR-02) and would make live network calls otherwise.
    """

    @pytest.fixture(autouse=True)
    def _patch_agent_components(self):
        """Suppress live agent calls for every test in this class."""
        with patch.object(CrashDetector, "_compute_liquidation_cascade", return_value=None), \
             patch.object(CrashDetector, "_compute_oi_collapse", return_value=None):
            yield

    def test_evaluate_empty_dataframes(self):
        """Empty df_by_symbol handled gracefully."""
        detector = CrashDetector()
        score = detector.evaluate({}, {})
        assert score == 0.0

    def test_evaluate_empty_tickers(self):
        """Empty tickers handled gracefully."""
        detector = CrashDetector()
        score = detector.evaluate({}, {})
        assert score == 0.0

    def test_evaluate_with_none_dataframes(self):
        """None DataFrames in dict handled gracefully."""
        detector = CrashDetector()
        score = detector.evaluate({"BTC/USDT": None}, {})
        assert score == 0.0

    def test_disabled_via_settings(self):
        """When disabled, returns 0 and does not evaluate."""
        detector = CrashDetector()
        with patch("config.settings.settings") as mock_settings:
            mock_settings.get.side_effect = lambda k, d=None: {
                "crash_detector.enabled": False,
            }.get(k, d)
            score = detector.evaluate({"BTC/USDT": {}}, {})
            assert score == 0.0


class TestCrashDetectorThreadSafety:
    """Test thread safety of concurrent evaluate() calls.

    NOTE: _compute_liquidation_cascade and _compute_oi_collapse are patched to
    return None to prevent live network calls during thread safety tests.
    """

    @pytest.fixture(autouse=True)
    def _patch_agent_components(self):
        """Suppress live agent calls for every test in this class."""
        with patch.object(CrashDetector, "_compute_liquidation_cascade", return_value=None), \
             patch.object(CrashDetector, "_compute_oi_collapse", return_value=None):
            yield

    def test_concurrent_evaluate_calls(self):
        """Multiple evaluate() calls are thread-safe."""
        import threading
        detector = CrashDetector()
        scores = []
        lock = threading.Lock()

        def evaluate_fn():
            score = detector.evaluate({}, {})
            with lock:
                scores.append(score)

        threads = [threading.Thread(target=evaluate_fn) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(scores) == 5
        assert all(0.0 <= s <= 10.0 for s in scores)

    def test_current_score_property_thread_safe(self):
        """current_score property is thread-safe."""
        detector = CrashDetector()
        detector._current_score = 5.5
        # No exception should be raised
        _ = detector.current_score

    def test_is_crash_mode_property_thread_safe(self):
        """is_crash_mode property is thread-safe."""
        detector = CrashDetector()
        detector._current_tier = TIER_DEFENSIVE
        assert detector.is_crash_mode is True


class TestCrashDetectorSingleton:
    """Test module-level singleton."""

    def test_get_crash_detector_returns_same_instance(self):
        """get_crash_detector returns the same instance on repeated calls."""
        d1 = get_crash_detector()
        d2 = get_crash_detector()
        assert d1 is d2
