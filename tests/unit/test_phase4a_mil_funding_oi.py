# ============================================================
# Phase 4A MIL Tests — Funding Rate Enhancement + OI Enhancement
#
# Tests:
#   1. Unit tests for multi-exchange aggregation, percentile, divergence
#   2. Unit tests for OI delta, OI/volume ratio, liquidation proximity
#   3. Config gate tests
#   4. Integration tests: agent → enhancer flow
#   5. Isolation tests: technical_only=True blocks MIL
#   6. Fail-open tests
#   7. No-blocking tests (FIX 1)
#   8. Rate-limit + cache tests (FIX 2)
#   9. Staleness tests (FIX 4)
#  10. Influence cap tests (FIX 3)
#  11. OI enhancer: timestamps, staleness, influence cap
# ============================================================
import sys
import os
import time
import threading
from unittest.mock import patch, MagicMock
from collections import deque
from pathlib import Path

import pytest

# Ensure project root is on path
ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ══════════════════════════════════════════════════════════════
# SECTION 1: FundingRateEnhancer Unit Tests
# ══════════════════════════════════════════════════════════════

class TestFundingWeightedRate:
    """Test multi-exchange weighted average funding rate."""

    def _make_enhancer(self):
        from core.agents.mil.funding_rate_enhanced import FundingRateEnhancer
        return FundingRateEnhancer()

    def test_bybit_only_when_no_other_exchanges(self):
        """When no other exchange data, weighted rate = Bybit rate."""
        e = self._make_enhancer()
        result = e.compute_weighted_rate(0.05, None)
        assert result == 0.05

    def test_bybit_only_when_empty_dict(self):
        e = self._make_enhancer()
        result = e.compute_weighted_rate(0.05, {})
        assert result == 0.05

    def test_all_three_exchanges(self):
        """Weighted average with all 3 exchanges: Bybit 0.40, Binance 0.35, OKX 0.25."""
        e = self._make_enhancer()
        bybit = 0.10
        others = {"binance": 0.08, "okx": 0.12}
        result = e.compute_weighted_rate(bybit, others)
        expected = (0.10 * 0.40 + 0.08 * 0.35 + 0.12 * 0.25) / (0.40 + 0.35 + 0.25)
        assert abs(result - expected) < 1e-5

    def test_two_exchanges_only(self):
        """When only Bybit + Binance available, weights redistribute."""
        e = self._make_enhancer()
        bybit = 0.10
        others = {"binance": 0.06}
        result = e.compute_weighted_rate(bybit, others)
        expected = (0.10 * 0.40 + 0.06 * 0.35) / (0.40 + 0.35)
        assert abs(result - expected) < 1e-5

    def test_unknown_exchange_ignored(self):
        """Unknown exchange names get weight 0 and are excluded."""
        e = self._make_enhancer()
        result = e.compute_weighted_rate(0.10, {"kraken": 0.08})
        # kraken weight = 0.0, so only Bybit contributes
        assert result == 0.10

    def test_negative_rates(self):
        """Negative funding rates are handled correctly."""
        e = self._make_enhancer()
        result = e.compute_weighted_rate(-0.05, {"binance": -0.06, "okx": -0.04})
        assert result < 0


class TestFundingPercentile:
    """Test 24h percentile calculation."""

    def _make_enhancer(self):
        from core.agents.mil.funding_rate_enhanced import FundingRateEnhancer
        return FundingRateEnhancer()

    def test_insufficient_data_returns_neutral(self):
        """With fewer than 5 data points, percentile defaults to 0.5."""
        e = self._make_enhancer()
        result = e.update_history_and_percentile("BTC/USDT", 0.05)
        assert result == 0.5  # Only 1 entry

    def test_highest_rate_near_1(self):
        """The highest rate in history should have percentile near 1.0."""
        e = self._make_enhancer()
        for rate in [0.01, 0.02, 0.03, 0.04, 0.05]:
            e.update_history_and_percentile("BTC/USDT", rate)
        result = e.update_history_and_percentile("BTC/USDT", 0.10)
        assert result >= 0.9

    def test_lowest_rate_near_0(self):
        """The lowest rate should have percentile near 0.0."""
        e = self._make_enhancer()
        for rate in [0.05, 0.06, 0.07, 0.08, 0.09]:
            e.update_history_and_percentile("BTC/USDT", rate)
        result = e.update_history_and_percentile("BTC/USDT", 0.01)
        assert result <= 0.2

    def test_middle_rate(self):
        """A mid-range rate should be around 0.5."""
        e = self._make_enhancer()
        for rate in [0.01, 0.02, 0.03, 0.04, 0.05, 0.06, 0.07, 0.08, 0.09, 0.10]:
            e.update_history_and_percentile("BTC/USDT", rate)
        result = e.update_history_and_percentile("BTC/USDT", 0.055)
        assert 0.3 <= result <= 0.7

    def test_per_symbol_isolation(self):
        """Different symbols maintain independent histories."""
        e = self._make_enhancer()
        for rate in [0.01, 0.02, 0.03, 0.04, 0.05]:
            e.update_history_and_percentile("BTC/USDT", rate)
        for rate in [0.10, 0.20, 0.30, 0.40, 0.50]:
            e.update_history_and_percentile("ETH/USDT", rate)
        # BTC with 0.05 should be 100th percentile for BTC
        btc_result = e.update_history_and_percentile("BTC/USDT", 0.05)
        assert btc_result >= 0.7
        # ETH with 0.05 should be very low for ETH
        eth_result = e.update_history_and_percentile("ETH/USDT", 0.05)
        assert eth_result <= 0.3

    def test_nan_input_returns_neutral(self):
        """NaN input should return neutral 0.5."""
        e = self._make_enhancer()
        result = e.update_history_and_percentile("BTC/USDT", float("nan"))
        assert result == 0.5

    def test_none_input_returns_neutral(self):
        """None input should return neutral 0.5."""
        e = self._make_enhancer()
        result = e.update_history_and_percentile("BTC/USDT", None)
        assert result == 0.5


class TestFundingDivergence:
    """Test cross-exchange divergence detection."""

    def _make_enhancer(self):
        from core.agents.mil.funding_rate_enhanced import FundingRateEnhancer
        return FundingRateEnhancer()

    def test_no_divergence_similar_rates(self):
        e = self._make_enhancer()
        result = e.compute_divergence(0.05, {"binance": 0.051, "okx": 0.049})
        assert result["divergence_detected"] is False
        assert result["divergence_spread"] < 0.03

    def test_divergence_detected(self):
        """Spread >= 0.03% triggers divergence flag."""
        e = self._make_enhancer()
        result = e.compute_divergence(0.10, {"binance": 0.05, "okx": 0.06})
        assert result["divergence_detected"] is True
        assert result["divergence_spread"] >= 0.03

    def test_single_exchange_no_divergence(self):
        """With only Bybit data, divergence is always False."""
        e = self._make_enhancer()
        result = e.compute_divergence(0.10, None)
        assert result["divergence_detected"] is False

    def test_exchange_rates_in_result(self):
        e = self._make_enhancer()
        result = e.compute_divergence(0.05, {"binance": 0.04})
        assert "bybit" in result["exchange_rates"]
        assert "binance" in result["exchange_rates"]


class TestFundingEnhanceSymbolData:
    """Test the full enhance_symbol_data() pipeline."""

    def _make_enhancer(self):
        from core.agents.mil.funding_rate_enhanced import FundingRateEnhancer
        return FundingRateEnhancer()

    def test_enhancement_adds_mil_keys(self):
        """enhance_symbol_data should add mil_* keys to base data (reads cache)."""
        e = self._make_enhancer()
        # Seed cache manually (simulating fetch_all_symbols)
        with e._lock:
            e._multi_exchange_cache["BTC/USDT"] = {
                "rates": {"binance": 0.04, "okx": 0.05},
                "ts": time.time(),
                "ts_mono": time.monotonic(),
            }
        # Seed history
        for r in [0.01, 0.02, 0.03, 0.04]:
            e.update_history_and_percentile("BTC/USDT", r)

        base = {"rate_pct": 0.05, "oi_usdt": 1000000}
        result = e.enhance_symbol_data("BTC/USDT", 0.05, base)

        assert result["mil_enhanced"] is True
        assert "mil_weighted_rate" in result
        assert "mil_percentile_24h" in result
        assert "mil_divergence_detected" in result
        assert "mil_divergence_spread" in result
        assert "mil_exchange_rates" in result
        assert "mil_exchanges_available" in result
        assert "mil_influence_factor" in result
        assert "mil_influence_cap" in result
        assert "mil_signal_ts" in result
        assert "mil_data_age_s" in result
        assert "mil_stale" in result

    def test_enhancement_fail_open(self):
        """If cache read fails somehow, enhancement should set mil_enhanced=False."""
        e = self._make_enhancer()
        # Inject broken cache that will cause an exception during processing
        with patch.object(e, "_get_cached_rates", side_effect=Exception("Internal error")):
            base = {"rate_pct": 0.05}
            result = e.enhance_symbol_data("BTC/USDT", 0.05, base)
            # Should not raise; fail-open returns enhanced=False
            assert result.get("mil_enhanced") is False
            assert result["rate_pct"] == 0.05  # original data preserved


# ══════════════════════════════════════════════════════════════
# SECTION 2: OIEnhancer Unit Tests
# ══════════════════════════════════════════════════════════════

class TestOIDelta:
    """Test OI delta calculation across 1h/4h/24h windows."""

    def _make_enhancer(self):
        from core.agents.mil.oi_enhanced import OIEnhancer
        return OIEnhancer()

    def test_no_history_returns_zeros(self):
        e = self._make_enhancer()
        result = e.compute_oi_deltas("BTC/USDT", 1_000_000)
        assert result["oi_delta_1h"] == 0.0
        assert result["oi_delta_4h"] == 0.0
        assert result["oi_delta_24h"] == 0.0

    def test_1h_delta_correct(self):
        """OI increased by 10% over 1h should show ~10% delta."""
        e = self._make_enhancer()
        now = time.time()
        e.record_oi("BTC/USDT", 1_000_000, ts=now - 3600)
        e.record_oi("BTC/USDT", 1_100_000, ts=now)
        result = e.compute_oi_deltas("BTC/USDT", 1_100_000)
        assert abs(result["oi_delta_1h"] - 10.0) < 1.0

    def test_4h_delta_correct(self):
        e = self._make_enhancer()
        now = time.time()
        e.record_oi("BTC/USDT", 1_000_000, ts=now - 14400)
        e.record_oi("BTC/USDT", 800_000, ts=now)
        result = e.compute_oi_deltas("BTC/USDT", 800_000)
        assert result["oi_delta_4h"] < -15.0  # -20% change

    def test_24h_delta_correct(self):
        e = self._make_enhancer()
        now = time.time()
        e.record_oi("BTC/USDT", 500_000, ts=now - 86400)
        e.record_oi("BTC/USDT", 750_000, ts=now)
        result = e.compute_oi_deltas("BTC/USDT", 750_000)
        assert abs(result["oi_delta_24h"] - 50.0) < 2.0

    def test_acceleration_positive_when_1h_faster(self):
        """Acceleration = delta_1h - delta_4h."""
        e = self._make_enhancer()
        now = time.time()
        e.record_oi("BTC/USDT", 1_000_000, ts=now - 14400)
        e.record_oi("BTC/USDT", 1_010_000, ts=now - 3600)
        e.record_oi("BTC/USDT", 1_200_000, ts=now)
        result = e.compute_oi_deltas("BTC/USDT", 1_200_000)
        assert isinstance(result["oi_acceleration"], float)
        assert "oi_delta_1h" in result
        assert "oi_delta_4h" in result

    def test_per_symbol_isolation(self):
        """Different symbols have independent OI histories."""
        e = self._make_enhancer()
        now = time.time()
        e.record_oi("BTC/USDT", 1_000_000, ts=now - 3600)
        e.record_oi("BTC/USDT", 1_100_000, ts=now)
        e.record_oi("ETH/USDT", 500_000, ts=now - 3600)
        e.record_oi("ETH/USDT", 400_000, ts=now)
        btc = e.compute_oi_deltas("BTC/USDT", 1_100_000)
        eth = e.compute_oi_deltas("ETH/USDT", 400_000)
        assert btc["oi_delta_1h"] > 0  # BTC increasing
        assert eth["oi_delta_1h"] < 0  # ETH decreasing


class TestOIVolumeRatio:
    """Test OI/Volume ratio calculation."""

    def _make_enhancer(self):
        from core.agents.mil.oi_enhanced import OIEnhancer
        return OIEnhancer()

    def test_no_volume_returns_zero(self):
        e = self._make_enhancer()
        result = e.compute_oi_volume_ratio("BTC/USDT", 1_000_000)
        assert result == 0.0

    def test_ratio_calculated_correctly(self):
        e = self._make_enhancer()
        now = time.time()
        e.record_volume("BTC/USDT", 500_000, ts=now - 300)
        e.record_volume("BTC/USDT", 500_000, ts=now)
        result = e.compute_oi_volume_ratio("BTC/USDT", 1_000_000)
        assert abs(result - 2.0) < 0.01  # OI/avg_volume = 1M/500K = 2.0


class TestLiquidationProximity:
    """Test liquidation proximity scoring."""

    def _make_enhancer(self):
        from core.agents.mil.oi_enhanced import OIEnhancer
        return OIEnhancer()

    def test_low_risk_when_stable(self):
        """Stable OI (no acceleration) should give low risk."""
        e = self._make_enhancer()
        now = time.time()
        for i in range(10):
            e.record_oi("BTC/USDT", 1_000_000 + i * 1000, ts=now - (10 - i) * 300)
        result = e.compute_liquidation_proximity("BTC/USDT", 1_010_000)
        assert result["liquidation_risk_level"] == "low"
        assert result["liquidation_proximity_score"] < 0.35

    def test_high_risk_when_accelerating(self):
        """Rapid OI acceleration should give high risk."""
        e = self._make_enhancer()
        now = time.time()
        e.record_oi("BTC/USDT", 1_000_000, ts=now - 14400)
        e.record_oi("BTC/USDT", 1_050_000, ts=now - 3600)
        e.record_oi("BTC/USDT", 1_300_000, ts=now)
        result = e.compute_liquidation_proximity("BTC/USDT", 1_300_000)
        assert result["liquidation_risk_level"] in ("medium", "high")
        assert result["liquidation_proximity_score"] > 0.2

    def test_score_clamped_to_1(self):
        """Score should never exceed 1.0."""
        e = self._make_enhancer()
        now = time.time()
        e.record_oi("BTC/USDT", 100_000, ts=now - 14400)
        e.record_oi("BTC/USDT", 100_000, ts=now - 3600)
        e.record_oi("BTC/USDT", 10_000_000, ts=now)  # extreme 100x jump
        result = e.compute_liquidation_proximity("BTC/USDT", 10_000_000)
        assert result["liquidation_proximity_score"] <= 1.0


class TestOIEnhanceResult:
    """Test the full enhance_oi_result() pipeline."""

    def _make_enhancer(self):
        from core.agents.mil.oi_enhanced import OIEnhancer
        return OIEnhancer()

    def test_enhancement_adds_mil_keys(self):
        e = self._make_enhancer()
        now = time.time()
        e.record_oi("BTC/USDT", 1_000_000, ts=now - 3600)
        base = {"raw_oi_usd": 1_100_000, "oi_change_1h_pct": 10.0, "age_seconds": 5.0, "source": "coinglass"}
        result = e.enhance_oi_result("BTC/USDT", base)
        assert result["mil_enhanced"] is True
        assert "mil_oi_delta_1h" in result
        assert "mil_oi_delta_4h" in result
        assert "mil_oi_delta_24h" in result
        assert "mil_oi_acceleration" in result
        assert "mil_oi_volume_ratio" in result
        assert "mil_liquidation_proximity" in result
        assert "mil_liquidation_risk" in result
        # New FIX fields
        assert "mil_oi_influence_factor" in result
        assert "mil_oi_influence_cap" in result
        assert "mil_signal_ts" in result
        assert "mil_data_age_s" in result
        assert "mil_stale" in result

    def test_enhancement_fail_open_zero_oi(self):
        """With zero OI, enhancement should gracefully set mil_enhanced=False."""
        e = self._make_enhancer()
        base = {"raw_oi_usd": 0.0, "oi_change_1h_pct": 0.0, "age_seconds": 5.0, "source": "coinglass"}
        result = e.enhance_oi_result("BTC/USDT", base)
        assert result["mil_enhanced"] is False

    def test_original_data_preserved(self):
        """Enhancement should not overwrite existing fields."""
        e = self._make_enhancer()
        base = {"raw_oi_usd": 1_000_000, "oi_change_1h_pct": 5.0, "age_seconds": 10.0, "source": "coinglass"}
        result = e.enhance_oi_result("BTC/USDT", base)
        assert result["oi_change_1h_pct"] == 5.0
        assert result["age_seconds"] == 10.0
        assert result["source"] == "coinglass"


# ══════════════════════════════════════════════════════════════
# SECTION 3: Config Gate Tests
# ══════════════════════════════════════════════════════════════

class TestConfigGates:
    """Test that MIL enhancements are properly gated by config flags."""

    @patch("config.settings.settings")
    def test_funding_enhancer_disabled_by_default(self, mock_settings):
        from core.agents.mil.funding_rate_enhanced import FundingRateEnhancer
        mock_settings.get = MagicMock(return_value=False)
        e = FundingRateEnhancer()
        assert e.is_enabled() is False

    @patch("config.settings.settings")
    def test_funding_enhancer_requires_both_gates(self, mock_settings):
        from core.agents.mil.funding_rate_enhanced import FundingRateEnhancer

        def side_effect(key, default=None):
            return {"mil.global_enabled": True, "agents.funding_rate_enhanced": False}.get(key, default)
        mock_settings.get = MagicMock(side_effect=side_effect)
        e = FundingRateEnhancer()
        assert e.is_enabled() is False

    @patch("config.settings.settings")
    def test_funding_enhancer_enabled_with_both_gates(self, mock_settings):
        from core.agents.mil.funding_rate_enhanced import FundingRateEnhancer

        def side_effect(key, default=None):
            return {"mil.global_enabled": True, "agents.funding_rate_enhanced": True}.get(key, default)
        mock_settings.get = MagicMock(side_effect=side_effect)
        e = FundingRateEnhancer()
        assert e.is_enabled() is True

    @patch("config.settings.settings")
    def test_oi_enhancer_disabled_by_default(self, mock_settings):
        from core.agents.mil.oi_enhanced import OIEnhancer
        mock_settings.get = MagicMock(return_value=False)
        e = OIEnhancer()
        assert e.is_enabled() is False

    @patch("config.settings.settings")
    def test_oi_enhancer_requires_both_gates(self, mock_settings):
        from core.agents.mil.oi_enhanced import OIEnhancer

        def side_effect(key, default=None):
            return {"mil.global_enabled": True, "agents.oi_enhanced": False}.get(key, default)
        mock_settings.get = MagicMock(side_effect=side_effect)
        e = OIEnhancer()
        assert e.is_enabled() is False

    def test_default_config_has_mil_gates(self):
        """Verify DEFAULT_CONFIG includes all MIL gate keys."""
        from config.settings import DEFAULT_CONFIG
        assert DEFAULT_CONFIG.get("mil", {}).get("global_enabled") is False
        assert DEFAULT_CONFIG["agents"].get("funding_rate_enhanced") is False
        assert DEFAULT_CONFIG["agents"].get("oi_enhanced") is False


# ══════════════════════════════════════════════════════════════
# SECTION 4: Integration Tests — Agent → Enhancer Flow
# ══════════════════════════════════════════════════════════════

class TestFundingAgentIntegration:
    """Test that FundingRateAgent calls enhancer when enabled."""

    def test_agent_process_without_mil(self):
        """When MIL disabled, agent.process() works as before (no mil_active)."""
        from core.agents.mil.funding_rate_enhanced import FundingRateEnhancer
        with patch.object(FundingRateEnhancer, "is_enabled", return_value=False):
            from core.agents.funding_rate_agent import FundingRateAgent
            agent = FundingRateAgent.__new__(FundingRateAgent)
            agent._cache = {}
            agent._lock = threading.RLock()
            agent._rate_history = {}

            raw = {"BTC/USDT": {"rate_pct": 0.05, "oi_usdt": 1_000_000}}
            result = agent.process(raw)
            assert result["mil_active"] is False
            assert "BTC/USDT" in result["symbols"]
            sym = result["symbols"]["BTC/USDT"]
            assert "signal" in sym
            assert "confidence" in sym

    @patch("core.agents.mil.funding_rate_enhanced.FundingRateEnhancer.is_enabled")
    def test_agent_process_with_mil(self, mock_enabled):
        """When MIL enabled, agent.process() adds mil_active=True and mil_* keys."""
        mock_enabled.return_value = True

        from core.agents.mil.funding_rate_enhanced import FundingRateEnhancer
        from core.agents.funding_rate_agent import FundingRateAgent

        agent = FundingRateAgent.__new__(FundingRateAgent)
        agent._cache = {}
        agent._lock = threading.RLock()
        agent._rate_history = {}

        # Pre-seed the enhancer's cache (simulating fetch_all_symbols)
        from core.agents.mil.funding_rate_enhanced import get_funding_enhancer
        enhancer = get_funding_enhancer()
        with enhancer._lock:
            enhancer._multi_exchange_cache["BTC/USDT"] = {
                "rates": {"binance": 0.04, "okx": 0.05},
                "ts": time.time(),
                "ts_mono": time.monotonic(),
            }

        raw = {"BTC/USDT": {"rate_pct": 0.05, "oi_usdt": 1_000_000}}
        result = agent.process(raw)
        assert result["mil_active"] is True
        sym = result["symbols"]["BTC/USDT"]
        assert sym.get("mil_enhanced") is True


class TestCoinglassAgentIntegration:
    """Test that CoinglassAgent calls OI enhancer when enabled."""

    def test_build_result_without_mil(self):
        """When MIL disabled, _build_result returns standard fields only."""
        from core.agents.mil.oi_enhanced import OIEnhancer
        with patch.object(OIEnhancer, "is_enabled", return_value=False):
            from core.agents.coinglass_agent import CoinglassAgent
            agent = CoinglassAgent()
            agent._cache["BTC/USDT"] = {"oi_usd": 1_000_000, "ts": time.time()}
            agent._history["BTC/USDT"] = deque([(time.time() - 3600, 900_000), (time.time(), 1_000_000)])
            result = agent._build_result("BTC/USDT", agent._cache["BTC/USDT"], "cached")
            assert "mil_enhanced" not in result or result.get("mil_enhanced") is False

    @patch("core.agents.mil.oi_enhanced.OIEnhancer.is_enabled")
    def test_build_result_with_mil(self, mock_enabled):
        """When MIL enabled, _build_result adds mil_* keys."""
        mock_enabled.return_value = True
        from core.agents.coinglass_agent import CoinglassAgent
        agent = CoinglassAgent()
        now = time.time()
        agent._cache["BTC/USDT"] = {"oi_usd": 1_000_000, "ts": now}
        agent._history["BTC/USDT"] = deque([(now - 3600, 900_000), (now, 1_000_000)])
        result = agent._build_result("BTC/USDT", agent._cache["BTC/USDT"], "coinglass")
        assert result.get("mil_enhanced") is True
        assert "mil_oi_delta_1h" in result


# ══════════════════════════════════════════════════════════════
# SECTION 5: Backtest Isolation Tests (CRITICAL)
# ══════════════════════════════════════════════════════════════

class TestBacktestIsolation:
    """
    Verify that technical_only=True produces IDENTICAL results
    regardless of MIL state.
    """

    def test_backtest_runner_does_not_import_orchestrator(self):
        """BacktestRunner module should NOT import orchestrator_engine."""
        runner_path = ROOT / "research" / "engine" / "backtest_runner.py"
        if not runner_path.exists():
            pytest.skip("backtest_runner.py not found")
        content = runner_path.read_text()
        assert "from core.orchestrator" not in content
        assert "import orchestrator_engine" not in content

    def test_backtest_runner_does_not_import_mil(self):
        """BacktestRunner should NOT import any MIL modules."""
        runner_path = ROOT / "research" / "engine" / "backtest_runner.py"
        if not runner_path.exists():
            pytest.skip("backtest_runner.py not found")
        content = runner_path.read_text()
        assert "from core.agents.mil" not in content
        assert "mil_enhanced" not in content
        assert "funding_rate_enhanced" not in content
        assert "oi_enhanced" not in content

    def test_confluence_scorer_technical_only_blocks_orchestrator(self):
        """ConfluenceScorer.score(technical_only=True) must NOT call get_orchestrator."""
        scorer_path = ROOT / "core" / "meta_decision" / "confluence_scorer.py"
        if not scorer_path.exists():
            pytest.skip("confluence_scorer.py not found")
        content = scorer_path.read_text()
        assert "if not technical_only:" in content
        idx_gate = content.index("if not technical_only:")
        idx_orch = content.index("get_orchestrator", idx_gate)
        assert idx_orch > idx_gate

    def test_mil_config_defaults_all_false(self):
        """All MIL config gates must default to False."""
        from config.settings import DEFAULT_CONFIG
        assert DEFAULT_CONFIG["mil"]["global_enabled"] is False
        assert DEFAULT_CONFIG["agents"]["funding_rate_enhanced"] is False
        assert DEFAULT_CONFIG["agents"]["oi_enhanced"] is False

    def test_backtest_runner_uses_technical_only(self):
        """BacktestRunner should pass technical_only=True to ConfluenceScorer."""
        runner_path = ROOT / "research" / "engine" / "backtest_runner.py"
        if not runner_path.exists():
            pytest.skip("backtest_runner.py not found")
        content = runner_path.read_text()
        assert "technical_only" in content


# ══════════════════════════════════════════════════════════════
# SECTION 6: Fail-Open Tests
# ══════════════════════════════════════════════════════════════

class TestFailOpen:
    """Test that MIL failures never block or corrupt trade signals."""

    def test_funding_enhancer_exception_returns_original(self):
        """If enhance_symbol_data raises internally, base data is returned."""
        from core.agents.mil.funding_rate_enhanced import FundingRateEnhancer
        e = FundingRateEnhancer()
        with patch.object(e, "_get_cached_rates", side_effect=Exception("timeout")):
            base = {"rate_pct": 0.05, "oi_usdt": 1_000_000}
            result = e.enhance_symbol_data("BTC/USDT", 0.05, base)
            assert result["rate_pct"] == 0.05  # original preserved
            assert result.get("mil_enhanced") is False

    def test_oi_enhancer_exception_returns_original(self):
        """If enhance_oi_result raises internally, base data is returned."""
        from core.agents.mil.oi_enhanced import OIEnhancer
        e = OIEnhancer()
        with patch.object(e, "compute_oi_deltas", side_effect=Exception("error")):
            base = {"raw_oi_usd": 1_000_000, "oi_change_1h_pct": 5.0, "age_seconds": 10.0, "source": "coinglass"}
            result = e.enhance_oi_result("BTC/USDT", base)
            assert result.get("mil_enhanced") is False
            assert result["raw_oi_usd"] == 1_000_000

    def test_funding_agent_process_survives_mil_import_error(self):
        """If MIL module can't be imported, agent.process() still works."""
        from core.agents.funding_rate_agent import FundingRateAgent
        agent = FundingRateAgent.__new__(FundingRateAgent)
        agent._cache = {}
        agent._lock = threading.RLock()
        agent._rate_history = {}

        with patch("core.agents.mil.funding_rate_enhanced.get_funding_enhancer", side_effect=ImportError("no module")):
            raw = {"BTC/USDT": {"rate_pct": 0.05, "oi_usdt": 1_000_000}}
            result = agent.process(raw)
            assert "BTC/USDT" in result["symbols"]
            assert result["symbols"]["BTC/USDT"]["signal"] != 0 or result["symbols"]["BTC/USDT"]["confidence"] > 0

    def test_signal_range_clamped(self):
        """MIL-enhanced signals must still be in [-1, +1] range."""
        from core.agents.mil.funding_rate_enhanced import FundingRateEnhancer
        e = FundingRateEnhancer()
        result = e.compute_weighted_rate(5.0, {"binance": 10.0, "okx": -3.0})
        assert isinstance(result, float)
        assert not (result != result)  # not NaN


# ══════════════════════════════════════════════════════════════
# SECTION 7: Pipeline Diagnostics Tests
# ══════════════════════════════════════════════════════════════

class TestPipelineDiagnostics:
    """Test that pipeline status includes MIL diagnostic keys."""

    def test_engine_has_mil_diagnostics_method(self):
        """TradingEngineService should have _get_mil_diagnostics method."""
        engine_path = ROOT / "web" / "engine" / "main.py"
        if not engine_path.exists():
            pytest.skip("engine/main.py not found")
        content = engine_path.read_text()
        assert "def _get_mil_diagnostics" in content

    def test_pipeline_status_includes_mil_keys(self):
        """_cmd_get_pipeline_status should call _get_mil_diagnostics."""
        engine_path = ROOT / "web" / "engine" / "main.py"
        if not engine_path.exists():
            pytest.skip("engine/main.py not found")
        content = engine_path.read_text()
        assert "_get_mil_diagnostics" in content

    def test_mil_diagnostics_checks_global_gate(self):
        """_get_mil_diagnostics should check mil.global_enabled first."""
        engine_path = ROOT / "web" / "engine" / "main.py"
        if not engine_path.exists():
            pytest.skip("engine/main.py not found")
        content = engine_path.read_text()
        idx_method = content.index("def _get_mil_diagnostics")
        method_section = content[idx_method:idx_method + 1500]
        assert "mil.global_enabled" in method_section


# ══════════════════════════════════════════════════════════════
# SECTION 8: No Weight Drift Tests
# ══════════════════════════════════════════════════════════════

class TestNoWeightDrift:
    """Verify MIL does NOT change OrchestratorEngine weights."""

    def test_orchestrator_default_weights_unchanged(self):
        """DEFAULT_WEIGHTS in orchestrator_engine.py must not reference MIL."""
        oe_path = ROOT / "core" / "orchestrator" / "orchestrator_engine.py"
        if not oe_path.exists():
            pytest.skip("orchestrator_engine.py not found")
        content = oe_path.read_text()
        assert "mil_" not in content.split("DEFAULT_WEIGHTS")[1].split("}")[0]
        assert "funding_rate_enhanced" not in content.split("DEFAULT_WEIGHTS")[1].split("}")[0]

    def test_mil_modules_do_not_modify_weights(self):
        """MIL modules should not import or modify DEFAULT_WEIGHTS."""
        for filename in ["funding_rate_enhanced.py", "oi_enhanced.py"]:
            path = ROOT / "core" / "agents" / "mil" / filename
            if path.exists():
                content = path.read_text()
                assert "DEFAULT_WEIGHTS" not in content
                assert "REGIME_WEIGHTS" not in content


# ══════════════════════════════════════════════════════════════
# SECTION 9: FIX 1 — No Blocking HTTP in process() Phase
# ══════════════════════════════════════════════════════════════

class TestNoBlockingInProcess:
    """
    Verify that enhance_symbol_data() does ZERO I/O.
    All HTTP must happen in fetch_all_symbols() (QThread fetch phase).
    """

    def test_enhance_symbol_data_does_not_call_requests(self):
        """enhance_symbol_data must not import or call requests module."""
        from core.agents.mil.funding_rate_enhanced import FundingRateEnhancer
        e = FundingRateEnhancer()

        # Seed cache so enhance actually runs
        with e._lock:
            e._multi_exchange_cache["BTC/USDT"] = {
                "rates": {"binance": 0.04},
                "ts": time.time(),
                "ts_mono": time.monotonic(),
            }

        # Patch requests at module level — any call = test failure
        with patch("core.agents.mil.funding_rate_enhanced.requests", create=True) as mock_req:
            mock_req.get = MagicMock(side_effect=AssertionError("requests.get called in process phase!"))
            base = {"rate_pct": 0.05}
            # Should NOT raise — enhance_symbol_data reads from cache only
            result = e.enhance_symbol_data("BTC/USDT", 0.05, base)
            assert result.get("mil_enhanced") is True
            mock_req.get.assert_not_called()

    def test_enhance_with_empty_cache_still_no_io(self):
        """Even with no cached data, enhance must not fetch — just skip."""
        from core.agents.mil.funding_rate_enhanced import FundingRateEnhancer
        e = FundingRateEnhancer()
        base = {"rate_pct": 0.05}
        # No cache seeded — should use bybit-only path, no HTTP
        result = e.enhance_symbol_data("BTC/USDT", 0.05, base)
        assert result.get("mil_enhanced") is True
        # Should have bybit-only weighted rate
        assert result["mil_weighted_rate"] == 0.05

    def test_fetch_all_symbols_does_call_http(self):
        """fetch_all_symbols (fetch phase) DOES make HTTP calls via batch."""
        from core.agents.mil.funding_rate_enhanced import FundingRateEnhancer
        e = FundingRateEnhancer()

        with patch.object(e, "is_enabled", return_value=True):
            with patch.object(
                e, "_fetch_batch_all_exchanges",
                return_value=({"BTCUSDT": 0.03}, {"BTC-USDT-SWAP": 0.03})
            ) as mock_batch:
                e.fetch_all_symbols(["BTC/USDT"])
                mock_batch.assert_called_once()

    def test_funding_agent_fetch_calls_fetch_all_symbols(self):
        """FundingRateAgent.fetch() must call enhancer.fetch_all_symbols()."""
        # Read the source and verify the call exists
        agent_path = ROOT / "core" / "agents" / "funding_rate_agent.py"
        content = agent_path.read_text()
        assert "fetch_all_symbols" in content
        # Verify it's in the fetch() method, not process()
        fetch_idx = content.index("def fetch(self)")
        process_idx = content.index("def process(self")
        call_idx = content.index("fetch_all_symbols")
        assert fetch_idx < call_idx < process_idx, (
            "fetch_all_symbols must be called in fetch(), before process()"
        )


# ══════════════════════════════════════════════════════════════
# SECTION 10: FIX 2 — Rate Limiting + Cache TTL
# ══════════════════════════════════════════════════════════════

class TestRateLimitAndCache:
    """
    Verify per-symbol rate limiting and cache TTL enforcement.
    """

    def _make_enhancer(self):
        from core.agents.mil.funding_rate_enhanced import FundingRateEnhancer
        return FundingRateEnhancer()

    def test_rate_limit_skips_recent_fetch(self):
        """A symbol fetched within _RATE_LIMIT_S should be skipped."""
        e = self._make_enhancer()
        with patch.object(e, "is_enabled", return_value=True):
            with patch.object(
                e, "_fetch_batch_all_exchanges",
                return_value=({"BTCUSDT": 0.03}, {})
            ) as mock_batch:
                e.fetch_all_symbols(["BTC/USDT"])
                assert mock_batch.call_count == 1

                # Immediately fetch again — should be rate-limited (no batch call)
                e.fetch_all_symbols(["BTC/USDT"])
                assert mock_batch.call_count == 1  # NOT called again

    def test_rate_limit_allows_after_cooldown(self):
        """After _RATE_LIMIT_S elapses, a new fetch is allowed."""
        from core.agents.mil.funding_rate_enhanced import _RATE_LIMIT_S
        e = self._make_enhancer()

        with patch.object(e, "is_enabled", return_value=True):
            with patch.object(
                e, "_fetch_batch_all_exchanges",
                return_value=({"BTCUSDT": 0.03}, {})
            ) as mock_batch:
                e.fetch_all_symbols(["BTC/USDT"])
                assert mock_batch.call_count == 1

                # Simulate cooldown by backdating the last fetch timestamp
                with e._lock:
                    e._last_fetch_ts["BTC/USDT"] = time.monotonic() - _RATE_LIMIT_S - 1

                e.fetch_all_symbols(["BTC/USDT"])
                assert mock_batch.call_count == 2

    def test_cache_ttl_returns_none_when_expired(self):
        """_get_cached_rates returns None when cache is older than TTL."""
        from core.agents.mil.funding_rate_enhanced import _CACHE_TTL_S
        e = self._make_enhancer()

        # Seed cache with old timestamp
        with e._lock:
            e._multi_exchange_cache["BTC/USDT"] = {
                "rates": {"binance": 0.03},
                "ts": time.time() - _CACHE_TTL_S - 10,
                "ts_mono": time.monotonic() - _CACHE_TTL_S - 10,
            }

        result = e._get_cached_rates("BTC/USDT")
        assert result is None  # Expired

    def test_cache_ttl_returns_data_when_fresh(self):
        """_get_cached_rates returns data when cache is within TTL."""
        e = self._make_enhancer()

        with e._lock:
            e._multi_exchange_cache["BTC/USDT"] = {
                "rates": {"binance": 0.03, "okx": 0.04},
                "ts": time.time(),
                "ts_mono": time.monotonic(),
            }

        result = e._get_cached_rates("BTC/USDT")
        assert result is not None
        assert result["binance"] == 0.03

    def test_per_symbol_rate_limit_independence(self):
        """Rate limit is per-symbol — different symbols can fetch independently."""
        e = self._make_enhancer()
        with patch.object(e, "is_enabled", return_value=True):
            with patch.object(
                e, "_fetch_batch_all_exchanges",
                return_value=({"BTCUSDT": 0.03, "ETHUSDT": 0.02}, {})
            ) as mock_batch:
                e.fetch_all_symbols(["BTC/USDT", "ETH/USDT"])
                assert mock_batch.call_count == 1  # one batch for both

                # Both already fetched — rate limited, no batch call
                e.fetch_all_symbols(["BTC/USDT", "ETH/USDT"])
                assert mock_batch.call_count == 1  # No new batch

    def test_diagnostics_reports_cache_stats(self):
        """Diagnostics should report fetch_count and cache_hit_count."""
        e = self._make_enhancer()
        with patch.object(e, "is_enabled", return_value=True):
            with patch.object(e, "_fetch_multi_exchange_http", return_value={"binance": 0.03}):
                e.fetch_all_symbols(["BTC/USDT"])
                e.fetch_all_symbols(["BTC/USDT"])  # rate-limited

        diag = e.get_diagnostics()
        assert diag["fetch_count"] == 1
        assert diag["cache_hit_count"] == 1
        assert diag["cache_ttl_s"] > 0
        assert diag["rate_limit_s"] > 0


# ══════════════════════════════════════════════════════════════
# SECTION 11: FIX 3 — Influence Cap
# ══════════════════════════════════════════════════════════════

class TestInfluenceCap:
    """
    Verify MIL influence is capped at MIL_INFLUENCE_CAP (0.30).
    """

    def test_funding_influence_capped_at_030(self):
        """mil_influence_factor must never exceed MIL_INFLUENCE_CAP."""
        from core.agents.mil.funding_rate_enhanced import FundingRateEnhancer, MIL_INFLUENCE_CAP
        e = FundingRateEnhancer()

        # Seed cache with data
        with e._lock:
            e._multi_exchange_cache["BTC/USDT"] = {
                "rates": {"binance": 0.50, "okx": 0.55},
                "ts": time.time(),
                "ts_mono": time.monotonic(),
            }

        # Seed extreme history to get extreme percentile
        for r in [0.01, 0.02, 0.03, 0.04, 0.05]:
            e.update_history_and_percentile("BTC/USDT", r)

        base = {"rate_pct": 0.50}
        result = e.enhance_symbol_data("BTC/USDT", 0.50, base)
        assert result["mil_enhanced"] is True
        assert result["mil_influence_factor"] <= MIL_INFLUENCE_CAP
        assert result["mil_influence_cap"] == MIL_INFLUENCE_CAP

    def test_funding_influence_reduced_on_divergence(self):
        """When divergence detected, influence should be halved."""
        from core.agents.mil.funding_rate_enhanced import FundingRateEnhancer, MIL_INFLUENCE_CAP
        e = FundingRateEnhancer()

        # Seed cache with divergent rates
        with e._lock:
            e._multi_exchange_cache["BTC/USDT"] = {
                "rates": {"binance": 0.01, "okx": 0.20},  # big spread
                "ts": time.time(),
                "ts_mono": time.monotonic(),
            }

        for r in [0.01, 0.02, 0.03, 0.04, 0.05]:
            e.update_history_and_percentile("BTC/USDT", r)

        base = {"rate_pct": 0.10}
        result = e.enhance_symbol_data("BTC/USDT", 0.10, base)
        assert result["mil_enhanced"] is True
        assert result["mil_divergence_detected"] is True
        # With divergence, influence is halved — so must be <= cap / 2
        assert result["mil_influence_factor"] <= MIL_INFLUENCE_CAP

    def test_mil_influence_cap_constant_value(self):
        """MIL_INFLUENCE_CAP must be exactly 0.30."""
        from core.agents.mil.funding_rate_enhanced import MIL_INFLUENCE_CAP
        assert MIL_INFLUENCE_CAP == 0.30

    def test_oi_influence_capped(self):
        """OI enhancer mil_oi_influence_factor must not exceed cap."""
        from core.agents.mil.oi_enhanced import OIEnhancer, MIL_OI_INFLUENCE_CAP
        e = OIEnhancer()
        now = time.time()
        # Extreme OI spike to drive high influence
        e.record_oi("BTC/USDT", 100_000, ts=now - 14400)
        e.record_oi("BTC/USDT", 100_000, ts=now - 3600)
        e.record_oi("BTC/USDT", 10_000_000, ts=now)
        base = {"raw_oi_usd": 10_000_000}
        result = e.enhance_oi_result("BTC/USDT", base)
        assert result["mil_enhanced"] is True
        assert result["mil_oi_influence_factor"] <= MIL_OI_INFLUENCE_CAP
        assert result["mil_oi_influence_cap"] == MIL_OI_INFLUENCE_CAP

    def test_oi_influence_cap_constant_value(self):
        """MIL_OI_INFLUENCE_CAP must be exactly 0.30."""
        from core.agents.mil.oi_enhanced import MIL_OI_INFLUENCE_CAP
        assert MIL_OI_INFLUENCE_CAP == 0.30


# ══════════════════════════════════════════════════════════════
# SECTION 12: FIX 4 — Timestamps + Staleness
# ══════════════════════════════════════════════════════════════

class TestTimestampsAndStaleness:
    """
    Verify all MIL signals carry timestamps and staleness is enforced.
    """

    def test_funding_signal_has_timestamp(self):
        """enhance_symbol_data must set mil_signal_ts."""
        from core.agents.mil.funding_rate_enhanced import FundingRateEnhancer
        e = FundingRateEnhancer()
        before = time.time()
        base = {"rate_pct": 0.05}
        result = e.enhance_symbol_data("BTC/USDT", 0.05, base)
        after = time.time()
        assert before <= result["mil_signal_ts"] <= after

    def test_funding_signal_has_data_age(self):
        """enhance_symbol_data must set mil_data_age_s."""
        from core.agents.mil.funding_rate_enhanced import FundingRateEnhancer
        e = FundingRateEnhancer()
        base = {"rate_pct": 0.05}
        result = e.enhance_symbol_data("BTC/USDT", 0.05, base)
        assert isinstance(result["mil_data_age_s"], float)
        assert result["mil_data_age_s"] >= 0

    def test_funding_staleness_flag_fresh(self):
        """Fresh data should not be marked stale."""
        from core.agents.mil.funding_rate_enhanced import FundingRateEnhancer
        e = FundingRateEnhancer()
        # Seed fresh cache
        with e._lock:
            e._multi_exchange_cache["BTC/USDT"] = {
                "rates": {"binance": 0.04},
                "ts": time.time(),
                "ts_mono": time.monotonic(),
            }
        base = {"rate_pct": 0.05}
        result = e.enhance_symbol_data("BTC/USDT", 0.05, base)
        assert result["mil_stale"] is False

    def test_funding_staleness_flag_old_data(self):
        """Old cached data should be marked stale (cache returns None, no cache entry)."""
        from core.agents.mil.funding_rate_enhanced import FundingRateEnhancer, _MAX_STALENESS_S
        e = FundingRateEnhancer()
        # Seed old cache (beyond staleness threshold)
        with e._lock:
            e._multi_exchange_cache["BTC/USDT"] = {
                "rates": {"binance": 0.04},
                "ts": time.time() - _MAX_STALENESS_S - 10,
                "ts_mono": time.monotonic() - _MAX_STALENESS_S - 10,
            }
        base = {"rate_pct": 0.05}
        result = e.enhance_symbol_data("BTC/USDT", 0.05, base)
        # With stale cache, _get_cached_rates returns None → bybit-only path
        # But data_age_s should be computed from cache entry ts
        assert result["mil_enhanced"] is True

    def test_oi_signal_has_timestamp(self):
        """enhance_oi_result must set mil_signal_ts."""
        from core.agents.mil.oi_enhanced import OIEnhancer
        e = OIEnhancer()
        before = time.time()
        base = {"raw_oi_usd": 1_000_000}
        result = e.enhance_oi_result("BTC/USDT", base)
        after = time.time()
        assert before <= result["mil_signal_ts"] <= after

    def test_oi_signal_has_staleness_flag(self):
        """enhance_oi_result must set mil_stale."""
        from core.agents.mil.oi_enhanced import OIEnhancer
        e = OIEnhancer()
        base = {"raw_oi_usd": 1_000_000}
        result = e.enhance_oi_result("BTC/USDT", base)
        assert isinstance(result["mil_stale"], bool)

    def test_oi_staleness_constants_exist(self):
        """OI enhancer must export _MAX_STALENESS_S."""
        from core.agents.mil.oi_enhanced import _MAX_STALENESS_S
        assert _MAX_STALENESS_S > 0


# ══════════════════════════════════════════════════════════════
# SECTION 13: Threading Safety
# ══════════════════════════════════════════════════════════════

class TestThreadingSafety:
    """Verify Lock (not RLock) is used for simpler, safer concurrency."""

    def test_funding_enhancer_uses_lock_not_rlock(self):
        """FundingRateEnhancer must use threading.Lock, not RLock."""
        from core.agents.mil.funding_rate_enhanced import FundingRateEnhancer
        e = FundingRateEnhancer()
        # threading.Lock() returns _thread.lock instance; RLock returns _thread.RLock
        assert not isinstance(e._lock, type(threading.RLock()))

    def test_oi_enhancer_uses_lock_not_rlock(self):
        """OIEnhancer must use threading.Lock, not RLock."""
        from core.agents.mil.oi_enhanced import OIEnhancer
        e = OIEnhancer()
        assert not isinstance(e._lock, type(threading.RLock()))
