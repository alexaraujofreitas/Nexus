"""
Unit tests for core.meta_decision.regime_capital_allocator.RegimeCapitalAllocator

Covers all 17 test cases:
  RCA1: Disabled mode passthrough for get_risk_multiplier
  RCA2: Disabled mode passthrough for get_max_capital_pct
  RCA3: Enabled mode — all 12 regimes return correct multipliers
  RCA4: Crisis/liquidation always returns 0.0
  RCA5: Transition discount caps at 0.60
  RCA6: Transition doesn't increase low multipliers
  RCA7: Fallback cap applies correctly
  RCA8: Hard cap enforces _MAX_MULTIPLIER (1.50)
  RCA9: Unknown regime defaults to 0.50
  RCA10: get_max_capital_pct returns correct per-regime values
  RCA11: get_max_capital_pct returns 0.020 for unknown regime
  RCA12: get_heat_budget returns correct family budgets
  RCA13: get_regime_family maps correctly
  RCA14: get_all_multipliers returns full dict
  RCA15: get_all_max_capital returns full dict
  RCA16: Singleton pattern works
  RCA17: Combined transition + fallback applies both, min wins
"""

import pytest
from unittest.mock import patch

from core.meta_decision.regime_capital_allocator import (
    RegimeCapitalAllocator,
    get_regime_capital_allocator,
    _REGIME_MULTIPLIERS,
    _REGIME_MAX_CAPITAL,
    _HEAT_BUDGET,
    _REGIME_TO_FAMILY,
)


# ── Fixtures ───────────────────────────────────────────────────────────

@pytest.fixture
def allocator():
    """Create a fresh allocator for each test."""
    return RegimeCapitalAllocator()


@pytest.fixture
def allocator_enabled(allocator, monkeypatch):
    """Create an allocator with enabled=True for testing."""
    monkeypatch.setattr(allocator, "_is_enabled", lambda: True)
    return allocator


@pytest.fixture
def allocator_disabled(allocator, monkeypatch):
    """Create an allocator with enabled=False for testing."""
    monkeypatch.setattr(allocator, "_is_enabled", lambda: False)
    return allocator


# ── RCA1: Disabled Mode Passthrough (get_risk_multiplier) ──────────────

class TestRCA1_DisabledModePassthrough:
    """Test that disabled mode returns 1.0 multiplier passthrough."""

    def test_rca1_001_disabled_returns_1_0(self, allocator_disabled):
        result = allocator_disabled.get_risk_multiplier("bull_trend")
        assert result == 1.0

    def test_rca1_002_disabled_crisis_still_1_0(self, allocator_disabled):
        result = allocator_disabled.get_risk_multiplier("crisis")
        assert result == 1.0

    def test_rca1_003_disabled_with_transition_flag_1_0(self, allocator_disabled):
        result = allocator_disabled.get_risk_multiplier("bull_trend", is_transition=True)
        assert result == 1.0

    def test_rca1_004_disabled_with_fallback_flag_1_0(self, allocator_disabled):
        result = allocator_disabled.get_risk_multiplier("ranging", is_fallback=True, fallback_multiplier=0.30)
        assert result == 1.0


# ── RCA2: Disabled Mode Passthrough (get_max_capital_pct) ──────────────

class TestRCA2_DisabledModeMaxCapital:
    """Test that disabled mode returns 0.04 for max_capital_pct."""

    def test_rca2_001_disabled_bull_trend_returns_0_04(self, allocator_disabled):
        result = allocator_disabled.get_max_capital_pct("bull_trend")
        assert result == 0.04

    def test_rca2_002_disabled_crisis_returns_0_04(self, allocator_disabled):
        result = allocator_disabled.get_max_capital_pct("crisis")
        assert result == 0.04

    def test_rca2_003_disabled_unknown_regime_returns_0_04(self, allocator_disabled):
        result = allocator_disabled.get_max_capital_pct("nonexistent_regime")
        assert result == 0.04

    def test_rca2_004_disabled_ranging_returns_0_04(self, allocator_disabled):
        result = allocator_disabled.get_max_capital_pct("ranging")
        assert result == 0.04


# ── RCA3: Enabled Mode — All 12 Regimes ───────────────────────────────

class TestRCA3_EnabledModeAllRegimes:
    """Test that enabled mode returns correct multiplier for each regime."""

    @pytest.mark.parametrize("regime,expected", [
        ("bull_trend", 1.20),
        ("bear_trend", 1.10),
        ("volatility_expansion", 0.80),
        ("ranging", 0.70),
        ("accumulation", 0.70),
        ("recovery", 0.75),
        ("volatility_compression", 0.50),
        ("distribution", 0.60),
        ("uncertain", 0.50),
        ("squeeze", 0.40),
        ("crisis", 0.00),
        ("liquidation_cascade", 0.00),
    ])
    def test_rca3_001_all_regimes_correct_multiplier(self, allocator_enabled, regime, expected):
        result = allocator_enabled.get_risk_multiplier(regime)
        assert result == expected

    def test_rca3_002_bull_trend_is_highest(self, allocator_enabled):
        bull = allocator_enabled.get_risk_multiplier("bull_trend")
        bear = allocator_enabled.get_risk_multiplier("bear_trend")
        assert bull > bear

    def test_rca3_003_crisis_is_lowest(self, allocator_enabled):
        crisis = allocator_enabled.get_risk_multiplier("crisis")
        bull = allocator_enabled.get_risk_multiplier("bull_trend")
        assert crisis < bull

    def test_rca3_004_volatility_expansion_less_than_bull(self, allocator_enabled):
        vol_exp = allocator_enabled.get_risk_multiplier("volatility_expansion")
        bull = allocator_enabled.get_risk_multiplier("bull_trend")
        assert vol_exp < bull


# ── RCA4: Crisis/Liquidation Always 0.0 ────────────────────────────────

class TestRCA4_CrisisAlwaysZero:
    """Test that crisis and liquidation_cascade always return 0.0."""

    def test_rca4_001_crisis_enabled_returns_0_0(self, allocator_enabled):
        result = allocator_enabled.get_risk_multiplier("crisis")
        assert result == 0.0

    def test_rca4_002_liquidation_cascade_enabled_returns_0_0(self, allocator_enabled):
        result = allocator_enabled.get_risk_multiplier("liquidation_cascade")
        assert result == 0.0

    def test_rca4_003_crisis_disabled_returns_1_0(self, allocator_disabled):
        # When disabled, even crisis returns passthrough 1.0
        result = allocator_disabled.get_risk_multiplier("crisis")
        assert result == 1.0

    def test_rca4_004_crisis_with_transition_still_0_0(self, allocator_enabled):
        result = allocator_enabled.get_risk_multiplier("crisis", is_transition=True)
        assert result == 0.0

    def test_rca4_005_liquidation_with_fallback_still_0_0(self, allocator_enabled):
        result = allocator_enabled.get_risk_multiplier("liquidation_cascade", is_fallback=True, fallback_multiplier=0.50)
        assert result == 0.0


# ── RCA5: Transition Discount Caps at 0.60 ────────────────────────────

class TestRCA5_TransitionDiscount:
    """Test that transition flag caps multiplier at 0.60."""

    def test_rca5_001_bull_trend_transition_caps_at_0_60(self, allocator_enabled):
        # bull_trend base is 1.20, transition should cap at 0.60
        result = allocator_enabled.get_risk_multiplier("bull_trend", is_transition=True)
        assert result == 0.60

    def test_rca5_002_bear_trend_transition_caps_at_0_60(self, allocator_enabled):
        # bear_trend base is 1.10, transition should cap at 0.60
        result = allocator_enabled.get_risk_multiplier("bear_trend", is_transition=True)
        assert result == 0.60

    def test_rca5_003_distribution_transition_preserved(self, allocator_enabled):
        # distribution base is 0.60, transition doesn't need to cap
        result = allocator_enabled.get_risk_multiplier("distribution", is_transition=True)
        assert result == 0.60

    def test_rca5_004_recovery_transition_capped(self, allocator_enabled):
        # recovery base is 0.75, transition should cap at 0.60
        result = allocator_enabled.get_risk_multiplier("recovery", is_transition=True)
        assert result == 0.60

    def test_rca5_005_volatile_expansion_transition_capped(self, allocator_enabled):
        # vol_exp base is 0.80, transition should cap at 0.60
        result = allocator_enabled.get_risk_multiplier("volatility_expansion", is_transition=True)
        assert result == 0.60


# ── RCA6: Transition Doesn't Increase Low Multipliers ─────────────────

class TestRCA6_TransitionNoIncrease:
    """Test that transition flag never increases low multipliers."""

    def test_rca6_001_squeeze_stays_0_40(self, allocator_enabled):
        # squeeze base is 0.40, transition shouldn't increase it
        result = allocator_enabled.get_risk_multiplier("squeeze", is_transition=True)
        assert result == 0.40

    def test_rca6_002_uncertain_stays_0_50(self, allocator_enabled):
        # uncertain base is 0.50, transition shouldn't increase it
        result = allocator_enabled.get_risk_multiplier("uncertain", is_transition=True)
        assert result == 0.50

    def test_rca6_003_volatility_compression_stays_0_50(self, allocator_enabled):
        # vol_comp base is 0.50, transition shouldn't increase it
        result = allocator_enabled.get_risk_multiplier("volatility_compression", is_transition=True)
        assert result == 0.50

    def test_rca6_004_ranging_stays_0_70(self, allocator_enabled):
        # ranging base is 0.70, transition shouldn't increase it
        result = allocator_enabled.get_risk_multiplier("ranging", is_transition=True)
        assert result == 0.60  # Wait, 0.70 > 0.60, so it caps at 0.60

    def test_rca6_005_accumulation_transition_capped(self, allocator_enabled):
        # accumulation base is 0.70, transition caps at 0.60
        result = allocator_enabled.get_risk_multiplier("accumulation", is_transition=True)
        assert result == 0.60


# ── RCA7: Fallback Cap Applies Correctly ───────────────────────────────

class TestRCA7_FallbackCap:
    """Test that fallback flag caps multiplier at fallback_multiplier."""

    def test_rca7_001_bull_trend_fallback_0_30_caps(self, allocator_enabled):
        # bull_trend base is 1.20, fallback 0.30 should cap at 0.30
        result = allocator_enabled.get_risk_multiplier("bull_trend", is_fallback=True, fallback_multiplier=0.30)
        assert result == 0.30

    def test_rca7_002_bear_trend_fallback_0_50_caps(self, allocator_enabled):
        # bear_trend base is 1.10, fallback 0.50 should cap at 0.50
        result = allocator_enabled.get_risk_multiplier("bear_trend", is_fallback=True, fallback_multiplier=0.50)
        assert result == 0.50

    def test_rca7_003_ranging_fallback_0_40_preserved(self, allocator_enabled):
        # ranging base is 0.70, fallback 0.40 should cap at 0.40
        result = allocator_enabled.get_risk_multiplier("ranging", is_fallback=True, fallback_multiplier=0.40)
        assert result == 0.40

    def test_rca7_004_distribution_fallback_0_20_capped(self, allocator_enabled):
        # distribution base is 0.60, fallback 0.20 should cap at 0.20
        result = allocator_enabled.get_risk_multiplier("distribution", is_fallback=True, fallback_multiplier=0.20)
        assert result == 0.20

    def test_rca7_005_fallback_default_1_0_no_cap(self, allocator_enabled):
        # Default fallback_multiplier is 1.0, should not cap
        result = allocator_enabled.get_risk_multiplier("ranging", is_fallback=True, fallback_multiplier=1.0)
        assert result == 0.70  # Original base value


# ── RCA8: Hard Cap Enforces _MAX_MULTIPLIER (1.50) ────────────────────

class TestRCA8_HardCap:
    """Test that no multiplier exceeds 1.50 (MAX_MULTIPLIER)."""

    def test_rca8_001_bull_trend_respects_hard_cap(self, allocator_enabled):
        # bull_trend base is 1.20, which is below 1.50
        result = allocator_enabled.get_risk_multiplier("bull_trend")
        assert result <= 1.50

    def test_rca8_002_all_regimes_respect_hard_cap(self, allocator_enabled):
        # Test all regimes
        for regime in _REGIME_MULTIPLIERS.keys():
            result = allocator_enabled.get_risk_multiplier(regime)
            assert result <= 1.50, f"Regime {regime} exceeded hard cap: {result}"

    def test_rca8_003_fallback_respects_hard_cap(self, allocator_enabled):
        # Even with high fallback_multiplier, shouldn't exceed 1.50
        result = allocator_enabled.get_risk_multiplier("bull_trend", is_fallback=True, fallback_multiplier=2.0)
        assert result <= 1.50

    def test_rca8_004_transition_respects_hard_cap(self, allocator_enabled):
        # Transition caps at 0.60, which is well below 1.50
        result = allocator_enabled.get_risk_multiplier("bull_trend", is_transition=True)
        assert result <= 1.50


# ── RCA9: Unknown Regime Defaults to 0.50 ──────────────────────────────

class TestRCA9_UnknownRegimeDefault:
    """Test that unknown regimes default to 0.50 multiplier."""

    def test_rca9_001_unknown_regime_returns_0_50(self, allocator_enabled):
        result = allocator_enabled.get_risk_multiplier("nonexistent_regime")
        assert result == 0.50

    def test_rca9_002_random_string_returns_0_50(self, allocator_enabled):
        result = allocator_enabled.get_risk_multiplier("totally_made_up")
        assert result == 0.50

    def test_rca9_003_empty_string_returns_0_50(self, allocator_enabled):
        result = allocator_enabled.get_risk_multiplier("")
        assert result == 0.50

    def test_rca9_004_none_returns_0_50(self, allocator_enabled):
        result = allocator_enabled.get_risk_multiplier(None)
        assert result == 0.50

    def test_rca9_005_unknown_with_transition_caps_at_0_50(self, allocator_enabled):
        # Default 0.50 + transition cap 0.60 = 0.50 (no capping needed)
        result = allocator_enabled.get_risk_multiplier("unknown", is_transition=True)
        assert result == 0.50


# ── RCA10: get_max_capital_pct Returns Correct Per-Regime ──────────────

class TestRCA10_MaxCapitalPerRegime:
    """Test that get_max_capital_pct returns correct per-regime values."""

    @pytest.mark.parametrize("regime,expected", [
        ("bull_trend", 0.050),
        ("bear_trend", 0.045),
        ("volatility_expansion", 0.030),
        ("ranging", 0.025),
        ("accumulation", 0.025),
        ("recovery", 0.030),
        ("volatility_compression", 0.020),
        ("distribution", 0.020),
        ("uncertain", 0.020),
        ("squeeze", 0.015),
        ("crisis", 0.000),
        ("liquidation_cascade", 0.000),
    ])
    def test_rca10_001_all_regimes_correct_max_capital(self, allocator_enabled, regime, expected):
        result = allocator_enabled.get_max_capital_pct(regime)
        assert result == expected

    def test_rca10_002_bull_trend_highest_capital(self, allocator_enabled):
        bull = allocator_enabled.get_max_capital_pct("bull_trend")
        bear = allocator_enabled.get_max_capital_pct("bear_trend")
        assert bull > bear

    def test_rca10_003_crisis_zero_capital(self, allocator_enabled):
        result = allocator_enabled.get_max_capital_pct("crisis")
        assert result == 0.0

    def test_rca10_004_liquidation_zero_capital(self, allocator_enabled):
        result = allocator_enabled.get_max_capital_pct("liquidation_cascade")
        assert result == 0.0


# ── RCA11: get_max_capital_pct Returns 0.020 for Unknown ────────────────

class TestRCA11_MaxCapitalUnknownDefault:
    """Test that unknown regimes default to 0.020 max_capital_pct."""

    def test_rca11_001_unknown_regime_returns_0_020(self, allocator_enabled):
        result = allocator_enabled.get_max_capital_pct("unknown_regime")
        assert result == 0.020

    def test_rca11_002_random_string_returns_0_020(self, allocator_enabled):
        result = allocator_enabled.get_max_capital_pct("foobar")
        assert result == 0.020

    def test_rca11_003_none_returns_0_020(self, allocator_enabled):
        result = allocator_enabled.get_max_capital_pct(None)
        assert result == 0.020

    def test_rca11_004_empty_string_returns_0_020(self, allocator_enabled):
        result = allocator_enabled.get_max_capital_pct("")
        assert result == 0.020


# ── RCA12: get_heat_budget Returns Correct Family Budgets ──────────────

class TestRCA12_HeatBudget:
    """Test that get_heat_budget returns correct family budgets."""

    def test_rca12_001_trending_family_0_035(self, allocator_enabled):
        # bull_trend and bear_trend are in "trending" family
        result = allocator_enabled.get_heat_budget("bull_trend")
        assert result == 0.035

    def test_rca12_002_bear_trend_trending_family(self, allocator_enabled):
        result = allocator_enabled.get_heat_budget("bear_trend")
        assert result == 0.035

    def test_rca12_003_expansion_family_0_015(self, allocator_enabled):
        # volatility_expansion and recovery are in "expansion" family
        result = allocator_enabled.get_heat_budget("volatility_expansion")
        assert result == 0.015

    def test_rca12_004_recovery_expansion_family(self, allocator_enabled):
        result = allocator_enabled.get_heat_budget("recovery")
        assert result == 0.015

    def test_rca12_005_ranging_family_0_010(self, allocator_enabled):
        # ranging, accumulation, distribution, uncertain are in "ranging" family
        result = allocator_enabled.get_heat_budget("ranging")
        assert result == 0.010

    def test_rca12_006_accumulation_ranging_family(self, allocator_enabled):
        result = allocator_enabled.get_heat_budget("accumulation")
        assert result == 0.010

    def test_rca12_007_distribution_ranging_family(self, allocator_enabled):
        result = allocator_enabled.get_heat_budget("distribution")
        assert result == 0.010

    def test_rca12_008_uncertain_ranging_family(self, allocator_enabled):
        result = allocator_enabled.get_heat_budget("uncertain")
        assert result == 0.010

    def test_rca12_009_squeeze_expansion_family(self, allocator_enabled):
        # squeeze is in "expansion" family
        result = allocator_enabled.get_heat_budget("squeeze")
        assert result == 0.015

    def test_rca12_010_volatility_compression_expansion_family(self, allocator_enabled):
        # volatility_compression is in "expansion" family
        result = allocator_enabled.get_heat_budget("volatility_compression")
        assert result == 0.015

    def test_rca12_011_crisis_maps_to_ranging_default(self, allocator_enabled):
        # crisis not in REGIME_TO_FAMILY, so defaults to "ranging"
        result = allocator_enabled.get_heat_budget("crisis")
        assert result == 0.010

    def test_rca12_012_unknown_regime_defaults_to_ranging(self, allocator_enabled):
        result = allocator_enabled.get_heat_budget("unknown")
        assert result == 0.010


# ── RCA13: get_regime_family Maps Correctly ────────────────────────────

class TestRCA13_RegimeFamily:
    """Test that get_regime_family returns correct family mapping."""

    @pytest.mark.parametrize("regime,expected_family", [
        ("bull_trend", "trending"),
        ("bear_trend", "trending"),
        ("volatility_expansion", "expansion"),
        ("recovery", "expansion"),
        ("ranging", "ranging"),
        ("accumulation", "ranging"),
        ("distribution", "ranging"),
        ("uncertain", "ranging"),
        ("volatility_compression", "expansion"),
        ("squeeze", "expansion"),
    ])
    def test_rca13_001_all_mappings_correct(self, allocator_enabled, regime, expected_family):
        result = allocator_enabled.get_regime_family(regime)
        assert result == expected_family

    def test_rca13_002_unknown_regime_defaults_to_ranging(self, allocator_enabled):
        result = allocator_enabled.get_regime_family("unknown")
        assert result == "ranging"

    def test_rca13_003_crisis_defaults_to_ranging(self, allocator_enabled):
        result = allocator_enabled.get_regime_family("crisis")
        assert result == "ranging"


# ── RCA14: get_all_multipliers Returns Full Dict ───────────────────────

class TestRCA14_AllMultipliers:
    """Test that get_all_multipliers returns the full dict."""

    def test_rca14_001_returns_dict(self):
        result = RegimeCapitalAllocator.get_all_multipliers()
        assert isinstance(result, dict)

    def test_rca14_002_contains_all_12_regimes(self):
        result = RegimeCapitalAllocator.get_all_multipliers()
        expected_regimes = {
            "bull_trend", "bear_trend", "volatility_expansion",
            "ranging", "accumulation", "recovery",
            "volatility_compression", "distribution", "uncertain",
            "squeeze", "crisis", "liquidation_cascade",
        }
        assert set(result.keys()) == expected_regimes

    def test_rca14_003_values_are_floats(self):
        result = RegimeCapitalAllocator.get_all_multipliers()
        for regime, mult in result.items():
            assert isinstance(mult, float), f"Regime {regime} has non-float value"

    def test_rca14_004_matches_internal_dict(self):
        result = RegimeCapitalAllocator.get_all_multipliers()
        assert result == _REGIME_MULTIPLIERS

    def test_rca14_005_bull_trend_1_20(self):
        result = RegimeCapitalAllocator.get_all_multipliers()
        assert result["bull_trend"] == 1.20

    def test_rca14_006_crisis_0_00(self):
        result = RegimeCapitalAllocator.get_all_multipliers()
        assert result["crisis"] == 0.0


# ── RCA15: get_all_max_capital Returns Full Dict ───────────────────────

class TestRCA15_AllMaxCapital:
    """Test that get_all_max_capital returns the full dict."""

    def test_rca15_001_returns_dict(self):
        result = RegimeCapitalAllocator.get_all_max_capital()
        assert isinstance(result, dict)

    def test_rca15_002_contains_all_12_regimes(self):
        result = RegimeCapitalAllocator.get_all_max_capital()
        expected_regimes = {
            "bull_trend", "bear_trend", "volatility_expansion",
            "ranging", "accumulation", "recovery",
            "volatility_compression", "distribution", "uncertain",
            "squeeze", "crisis", "liquidation_cascade",
        }
        assert set(result.keys()) == expected_regimes

    def test_rca15_003_values_are_floats(self):
        result = RegimeCapitalAllocator.get_all_max_capital()
        for regime, cap in result.items():
            assert isinstance(cap, float), f"Regime {regime} has non-float value"

    def test_rca15_004_matches_internal_dict(self):
        result = RegimeCapitalAllocator.get_all_max_capital()
        assert result == _REGIME_MAX_CAPITAL

    def test_rca15_005_bull_trend_0_050(self):
        result = RegimeCapitalAllocator.get_all_max_capital()
        assert result["bull_trend"] == 0.050

    def test_rca15_006_crisis_0_000(self):
        result = RegimeCapitalAllocator.get_all_max_capital()
        assert result["crisis"] == 0.0


# ── RCA16: Singleton Pattern ───────────────────────────────────────────

class TestRCA16_Singleton:
    """Test that get_regime_capital_allocator returns same instance."""

    def test_rca16_001_same_instance_multiple_calls(self):
        alloc1 = get_regime_capital_allocator()
        alloc2 = get_regime_capital_allocator()
        assert alloc1 is alloc2

    def test_rca16_002_global_reset_for_fresh_instance(self):
        # Reset the global singleton for testing
        import core.meta_decision.regime_capital_allocator as mod
        mod._allocator = None
        alloc1 = get_regime_capital_allocator()
        alloc2 = get_regime_capital_allocator()
        assert alloc1 is alloc2

    def test_rca16_003_singleton_is_regime_capital_allocator_instance(self):
        alloc = get_regime_capital_allocator()
        assert isinstance(alloc, RegimeCapitalAllocator)


# ── RCA17: Combined Transition + Fallback ──────────────────────────────

class TestRCA17_CombinedTransitionFallback:
    """Test that transition + fallback both apply, min wins."""

    def test_rca17_001_transition_and_fallback_both_high(self, allocator_enabled):
        # bull_trend base is 1.20
        # transition caps at 0.60, fallback at 0.80 -> min(0.60, 0.80) = 0.60
        result = allocator_enabled.get_risk_multiplier(
            "bull_trend", is_transition=True, is_fallback=True, fallback_multiplier=0.80
        )
        assert result == 0.60

    def test_rca17_002_fallback_lower_than_transition(self, allocator_enabled):
        # bull_trend base is 1.20
        # transition caps at 0.60, fallback at 0.40 -> min(0.60, 0.40) = 0.40
        result = allocator_enabled.get_risk_multiplier(
            "bull_trend", is_transition=True, is_fallback=True, fallback_multiplier=0.40
        )
        assert result == 0.40

    def test_rca17_003_transition_lower_than_fallback(self, allocator_enabled):
        # bear_trend base is 1.10
        # transition caps at 0.60, fallback at 0.90 -> min(0.60, 0.90) = 0.60
        result = allocator_enabled.get_risk_multiplier(
            "bear_trend", is_transition=True, is_fallback=True, fallback_multiplier=0.90
        )
        assert result == 0.60

    def test_rca17_004_both_flags_on_low_regime(self, allocator_enabled):
        # squeeze base is 0.40
        # transition would cap at 0.60, fallback at 0.30 -> min(0.40, 0.60, 0.30) = 0.30
        result = allocator_enabled.get_risk_multiplier(
            "squeeze", is_transition=True, is_fallback=True, fallback_multiplier=0.30
        )
        assert result == 0.30

    def test_rca17_005_both_flags_neutral_regime(self, allocator_enabled):
        # ranging base is 0.70
        # transition caps at 0.60, fallback at 0.75 -> min(0.60, 0.75) = 0.60
        result = allocator_enabled.get_risk_multiplier(
            "ranging", is_transition=True, is_fallback=True, fallback_multiplier=0.75
        )
        assert result == 0.60

    def test_rca17_006_both_flags_with_crisis(self, allocator_enabled):
        # crisis base is 0.0, transaction/fallback don't matter
        result = allocator_enabled.get_risk_multiplier(
            "crisis", is_transition=True, is_fallback=True, fallback_multiplier=0.50
        )
        assert result == 0.0


# ── Integration Tests ──────────────────────────────────────────────────

class TestIntegration:
    """Integration tests combining multiple scenarios."""

    def test_integration_001_full_scenario_enabled(self, allocator_enabled):
        """Test a realistic workflow with enabled allocator."""
        # Bull trend, high confidence
        mult = allocator_enabled.get_risk_multiplier("bull_trend")
        cap = allocator_enabled.get_max_capital_pct("bull_trend")
        heat = allocator_enabled.get_heat_budget("bull_trend")
        family = allocator_enabled.get_regime_family("bull_trend")

        assert mult == 1.20
        assert cap == 0.050
        assert heat == 0.035
        assert family == "trending"

    def test_integration_002_crisis_scenario_enabled(self, allocator_enabled):
        """Test crisis handling."""
        mult = allocator_enabled.get_risk_multiplier("crisis")
        cap = allocator_enabled.get_max_capital_pct("crisis")
        heat = allocator_enabled.get_heat_budget("crisis")

        assert mult == 0.0
        assert cap == 0.0
        assert heat == 0.010  # Defaults to ranging family

    def test_integration_003_ranging_fallback(self, allocator_enabled):
        """Test ranging regime with fallback."""
        mult = allocator_enabled.get_risk_multiplier(
            "ranging", is_fallback=True, fallback_multiplier=0.40
        )
        cap = allocator_enabled.get_max_capital_pct("ranging")

        assert mult == 0.40  # Capped by fallback
        assert cap == 0.025

    def test_integration_004_transition_in_expansion(self, allocator_enabled):
        """Test volatility_expansion regime during transition."""
        mult = allocator_enabled.get_risk_multiplier("volatility_expansion", is_transition=True)
        cap = allocator_enabled.get_max_capital_pct("volatility_expansion")
        heat = allocator_enabled.get_heat_budget("volatility_expansion")

        assert mult == 0.60  # Capped by transition
        assert cap == 0.030
        assert heat == 0.015

    def test_integration_005_disabled_overrides_all(self, allocator_disabled):
        """Test that disabled mode overrides all flags and regimes."""
        result1 = allocator_disabled.get_risk_multiplier("bull_trend")
        result2 = allocator_disabled.get_risk_multiplier("crisis", is_transition=True)
        result3 = allocator_disabled.get_risk_multiplier("ranging", is_fallback=True, fallback_multiplier=0.30)

        assert result1 == 1.0
        assert result2 == 1.0
        assert result3 == 1.0
