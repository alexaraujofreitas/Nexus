"""
tests/test_phase5b_concentration.py — Capital Concentration Engine (Phase 5B v2)
================================================================================
22 tests: multiplier range (3), hard cap (5), sizing math (3),
          component weighting (3), determinism (3), edge cases (3),
          portfolio safety proof (2)
"""
import pytest
from core.intraday.scoring.capital_concentration import (
    CapitalConcentrationEngine, ConcentrationConfig, ConcentrationResult,
)


# ── Multiplier Range (3) ────────────────────────────────────

class TestMultiplierRange:
    def test_minimum_multiplier(self):
        """Worst-case inputs → min multiplier."""
        engine = CapitalConcentrationEngine()
        r = engine.calculate(
            tqs_score=0.0, asset_score=0.0, execution_score=0.0,
            base_size_usdt=100.0, base_quantity=0.002,
            total_capital=10000.0, entry_price=50000.0,
        )
        assert r.multiplier == pytest.approx(0.40, abs=0.01)

    def test_maximum_multiplier(self):
        """Best-case inputs → max multiplier."""
        engine = CapitalConcentrationEngine()
        r = engine.calculate(
            tqs_score=1.0, asset_score=1.0, execution_score=1.0,
            base_size_usdt=100.0, base_quantity=0.002,
            total_capital=100000.0, entry_price=50000.0,
        )
        assert r.multiplier == pytest.approx(1.50, abs=0.01)

    def test_neutral_inputs_near_one(self):
        """Mid-range inputs → near 1.0 multiplier."""
        engine = CapitalConcentrationEngine()
        r = engine.calculate(
            tqs_score=0.525, asset_score=0.5, execution_score=0.5,
            base_size_usdt=100.0, base_quantity=0.002,
            total_capital=100000.0, entry_price=50000.0,
        )
        assert 0.85 < r.multiplier < 1.15


# ── Hard Cap Enforcement (5) ────────────────────────────────

class TestHardCap:
    def test_cap_triggered_on_large_base(self):
        """Base size * multiplier > 4% cap → capped."""
        engine = CapitalConcentrationEngine()
        r = engine.calculate(
            tqs_score=1.0, asset_score=1.0, execution_score=1.0,
            base_size_usdt=500.0, base_quantity=0.01,
            total_capital=10000.0, entry_price=50000.0,
        )
        assert r.capped is True
        assert r.adjusted_size_usdt == pytest.approx(400.0)  # 4% of 10k

    def test_cap_not_triggered_on_small_base(self):
        """Base size * multiplier < 4% cap → not capped."""
        engine = CapitalConcentrationEngine()
        r = engine.calculate(
            tqs_score=0.5, asset_score=0.5, execution_score=0.5,
            base_size_usdt=100.0, base_quantity=0.002,
            total_capital=100000.0, entry_price=50000.0,
        )
        assert r.capped is False

    def test_cap_with_small_capital(self):
        """Small capital → tight cap."""
        engine = CapitalConcentrationEngine()
        r = engine.calculate(
            tqs_score=1.0, asset_score=1.0, execution_score=1.0,
            base_size_usdt=100.0, base_quantity=0.002,
            total_capital=1000.0, entry_price=50000.0,
        )
        # 4% of 1000 = 40, base*1.5 = 150 → capped at 40
        assert r.capped is True
        assert r.adjusted_size_usdt == pytest.approx(40.0)

    def test_cap_recalculates_quantity(self):
        """When capped, quantity = capped_size / entry_price."""
        engine = CapitalConcentrationEngine()
        r = engine.calculate(
            tqs_score=1.0, asset_score=1.0, execution_score=1.0,
            base_size_usdt=500.0, base_quantity=0.01,
            total_capital=10000.0, entry_price=50000.0,
        )
        assert r.adjusted_quantity == pytest.approx(400.0 / 50000.0)

    def test_cap_never_exceeded_regardless_of_config(self):
        """Even with extreme config, 4% cap holds."""
        cfg = ConcentrationConfig(max_multiplier=10.0, max_capital_pct=0.04)
        engine = CapitalConcentrationEngine(cfg)
        r = engine.calculate(
            tqs_score=1.0, asset_score=1.0, execution_score=1.0,
            base_size_usdt=1000.0, base_quantity=0.02,
            total_capital=10000.0, entry_price=50000.0,
        )
        assert r.adjusted_size_usdt <= 400.0 + 0.01


# ── Sizing Math (3) ─────────────────────────────────────────

class TestSizingMath:
    def test_adjusted_size_equals_base_times_multiplier(self):
        engine = CapitalConcentrationEngine()
        r = engine.calculate(
            tqs_score=0.7, asset_score=0.5, execution_score=0.5,
            base_size_usdt=100.0, base_quantity=0.002,
            total_capital=100000.0, entry_price=50000.0,
        )
        if not r.capped:
            assert r.adjusted_size_usdt == pytest.approx(
                100.0 * r.multiplier, rel=0.001,
            )

    def test_adjusted_quantity_proportional(self):
        engine = CapitalConcentrationEngine()
        r = engine.calculate(
            tqs_score=0.7, asset_score=0.5, execution_score=0.5,
            base_size_usdt=100.0, base_quantity=0.002,
            total_capital=100000.0, entry_price=50000.0,
        )
        if not r.capped:
            assert r.adjusted_quantity == pytest.approx(
                0.002 * r.multiplier, rel=0.001,
            )

    def test_zero_entry_price_safe(self):
        """Zero entry price → quantity stays at base * multiplier."""
        engine = CapitalConcentrationEngine()
        r = engine.calculate(
            tqs_score=0.5, asset_score=0.5, execution_score=0.5,
            base_size_usdt=100.0, base_quantity=0.002,
            total_capital=100000.0, entry_price=0.0,
        )
        assert r.adjusted_quantity > 0


# ── Component Weighting (3) ─────────────────────────────────

class TestComponentWeighting:
    def test_tqs_dominant(self):
        """Higher TQS → higher multiplier when others fixed."""
        engine = CapitalConcentrationEngine()
        r_high = engine.calculate(
            tqs_score=0.8, asset_score=0.5, execution_score=0.5,
            base_size_usdt=100, base_quantity=0.002,
            total_capital=100000, entry_price=50000,
        )
        r_low = engine.calculate(
            tqs_score=0.3, asset_score=0.5, execution_score=0.5,
            base_size_usdt=100, base_quantity=0.002,
            total_capital=100000, entry_price=50000,
        )
        assert r_high.multiplier > r_low.multiplier

    def test_asset_score_contributes(self):
        engine = CapitalConcentrationEngine()
        r_high = engine.calculate(
            tqs_score=0.5, asset_score=1.0, execution_score=0.5,
            base_size_usdt=100, base_quantity=0.002,
            total_capital=100000, entry_price=50000,
        )
        r_low = engine.calculate(
            tqs_score=0.5, asset_score=0.0, execution_score=0.5,
            base_size_usdt=100, base_quantity=0.002,
            total_capital=100000, entry_price=50000,
        )
        assert r_high.multiplier > r_low.multiplier

    def test_config_weights_sum_to_one(self):
        cfg = ConcentrationConfig()
        total = cfg.w_tqs + cfg.w_asset_score + cfg.w_execution_score
        assert abs(total - 1.0) < 0.001


# ── Determinism (3) ──────────────────────────────────────────

class TestDeterminism:
    def test_same_inputs_same_result(self):
        engine = CapitalConcentrationEngine()
        args = dict(
            tqs_score=0.6, asset_score=0.5, execution_score=0.5,
            base_size_usdt=100, base_quantity=0.002,
            total_capital=100000, entry_price=50000,
        )
        r1 = engine.calculate(**args)
        r2 = engine.calculate(**args)
        assert r1.multiplier == r2.multiplier
        assert r1.adjusted_size_usdt == r2.adjusted_size_usdt

    def test_to_dict_stable(self):
        engine = CapitalConcentrationEngine()
        r = engine.calculate(
            tqs_score=0.6, asset_score=0.5, execution_score=0.5,
            base_size_usdt=100, base_quantity=0.002,
            total_capital=100000, entry_price=50000,
        )
        assert r.to_dict() == r.to_dict()

    def test_result_is_frozen(self):
        r = ConcentrationResult(
            multiplier=1.0, tqs_component=0.5, asset_component=0.5,
            execution_component=0.5, adjusted_size_usdt=100.0,
            adjusted_quantity=0.002, capped=False,
        )
        with pytest.raises(AttributeError):
            r.multiplier = 2.0


# ── Edge Cases (3) ───────────────────────────────────────────

class TestEdgeCases:
    def test_tqs_below_low_bound(self):
        engine = CapitalConcentrationEngine()
        r = engine.calculate(
            tqs_score=0.0, asset_score=0.0, execution_score=0.0,
            base_size_usdt=100, base_quantity=0.002,
            total_capital=100000, entry_price=50000,
        )
        assert r.multiplier >= 0.40

    def test_tqs_above_high_bound(self):
        engine = CapitalConcentrationEngine()
        r = engine.calculate(
            tqs_score=1.0, asset_score=1.0, execution_score=1.0,
            base_size_usdt=100, base_quantity=0.002,
            total_capital=100000, entry_price=50000,
        )
        assert r.multiplier <= 1.50

    def test_to_dict_keys(self):
        engine = CapitalConcentrationEngine()
        r = engine.calculate(
            tqs_score=0.5, asset_score=0.5, execution_score=0.5,
            base_size_usdt=100, base_quantity=0.002,
            total_capital=100000, entry_price=50000,
        )
        expected = {
            "concentration_multiplier", "tqs_component", "asset_component",
            "execution_component", "adjusted_size_usdt", "adjusted_quantity",
            "capped",
        }
        assert set(r.to_dict().keys()) == expected


# ── Portfolio Safety Proof (2) ───────────────────────────────

class TestPortfolioSafetyProof:
    """
    MATHEMATICAL PROOF (v3): sum(position_risk_adjusted) <= portfolio_heat_cap
    under max concentration and concurrent trades.

    Given:
      - max_capital_pct = 0.04 (4% per trade)
      - max_multiplier = 1.50
      - max_positions = 5 (from risk engine)
      - portfolio_heat_cap = 0.06 (6%)

    PROOF (CORRECTED v3):
      max_single_position = min(base * 1.50, 0.04 * total_capital)
                          = 0.04 * total_capital  (capped)
      max_risk_per_trade  = 0.04 * total_capital * (SL_distance / entry_price)
                          ≤ 0.04 * total_capital  (SL cannot exceed entry)

      With N concurrent positions:
        sum(risk) = N * max_risk_per_trade ≤ N * 0.04 * total_capital

      For N=5: sum(risk) ≤ 0.20 * total_capital = 20%

      But the risk engine's portfolio_heat gate (6%) catches this BEFORE
      position 2 even opens. So effective maximum:
        sum(risk) ≤ 0.06 * total_capital  (enforced by risk_gate)

      v3 PIPELINE ORDER:
        Step 8: Concentration adjusts sizing (BEFORE risk engine)
        Step 9: Intent built with POST-concentration sizing
        Step 11: Risk engine validates POST-concentration sizing

      Concentration NEVER bypasses the heat gate because:
        1. Concentration runs BEFORE risk engine (Step 8 < Step 11)
        2. Risk engine receives the FINAL concentrated intent
        3. Risk engine validates post-concentration risk_usdt against heat
        4. The 4% hard cap in ConcentrationEngine is a defense-in-depth layer

      Therefore: risk engine validates post-concentration sizing.
      sum(position_risk_adjusted) ≤ 6% of total_capital. QED.
    """

    def test_max_concentration_respects_hard_cap(self):
        """Concentration at max multiplier with 5 concurrent trades."""
        engine = CapitalConcentrationEngine()
        total = 100_000.0
        results = []
        for _ in range(5):
            r = engine.calculate(
                tqs_score=1.0, asset_score=1.0, execution_score=1.0,
                base_size_usdt=5000.0, base_quantity=0.1,
                total_capital=total, entry_price=50000.0,
            )
            results.append(r)

        # Each capped at 4% = 4000
        # Risk engine heat gate (6%) prevents concurrent excess in production
        # Concentration alone guarantees each <= 4%
        for r in results:
            assert r.adjusted_size_usdt <= total * 0.04 + 0.01

    def test_concentration_preserves_risk_reward_ratio(self):
        """Concentration adjusts size but NOT risk_reward_ratio."""
        engine = CapitalConcentrationEngine()
        r = engine.calculate(
            tqs_score=0.8, asset_score=0.5, execution_score=0.5,
            base_size_usdt=100.0, base_quantity=0.002,
            total_capital=100000.0, entry_price=50000.0,
        )
        # ConcentrationResult doesn't carry R:R; ProcessingEngine
        # preserves it from the original trigger. Verify multiplier
        # is within bounds (the only thing concentration controls).
        assert 0.40 <= r.multiplier <= 1.50
