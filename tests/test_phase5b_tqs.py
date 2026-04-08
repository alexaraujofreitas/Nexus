"""
tests/test_phase5b_tqs.py — Trade Quality Scorer (Phase 5B v2)
==============================================================
25 tests: config (4), components (5), R:R mapping (4),
          composite bounds (3), floor gate (3), determinism (3),
          edge cases (3)
"""
import pytest
from core.intraday.scoring.trade_quality_scorer import (
    TradeQualityScorer, TQSConfig, TQSResult,
)
from core.intraday.execution_contracts import (
    CapitalSnapshot, ExposureSnapshot, PortfolioSnapshot,
)
from core.intraday.signal_contracts import (
    Direction, StrategyClass, TriggerSignal, TriggerLifecycle,
)


# ── Helpers ──────────────────────────────────────────────────

def _trigger(
    strength=0.7, trigger_quality=0.6,
    regime_confidence=0.5, symbol="BTC", direction=Direction.LONG,
    entry_price=50000.0, stop_loss=49000.0, take_profit=52000.0,
):
    """Build a TriggerSignal. R:R is computed from prices."""
    return TriggerSignal(
        trigger_id="t001", setup_id="s001", symbol=symbol,
        direction=direction, strategy_name="Test",
        strategy_class=StrategyClass.MOMENTUM_EXPANSION,
        entry_price=entry_price, stop_loss=stop_loss, take_profit=take_profit,
        atr_value=500.0, strength=strength, trigger_quality=trigger_quality,
        regime="BULL_TREND", regime_confidence=regime_confidence,
        setup_timeframe="30m", trigger_timeframe="5m",
        trigger_candle_ts=1_000_000_000_000,
        setup_candle_ts=1_000_000_000_000,
        lifecycle=TriggerLifecycle.EVALUATED,
        created_at_ms=1_000_000_000_000, max_age_ms=300_000,
        candle_trace_ids=("c1",), setup_trace_ids=("s1",),
    )


def _snap(available=5000.0, total=10000.0, heat=0.02):
    return PortfolioSnapshot(
        capital=CapitalSnapshot(
            total_capital=total, available_capital=available,
            reserved_capital=total - available, equity=total,
            peak_equity=total, drawdown_pct=0.0,
            realized_pnl_today=0.0, total_realized_pnl=0.0,
            total_fees=0.0, trade_count_today=0, consecutive_losses=0,
        ),
        exposure=ExposureSnapshot(
            per_symbol={}, portfolio_heat=heat,
            long_exposure=0.0, short_exposure=0.0, net_exposure=0.0,
        ),
        open_positions=(), open_position_count=0,
    )


# ── Config Validation (4) ───────────────────────────────────

class TestTQSConfig:
    def test_default_weights_sum_to_one(self):
        cfg = TQSConfig()
        total = (cfg.w_setup_quality + cfg.w_trigger_strength
                 + cfg.w_market_context + cfg.w_execution_context)
        assert abs(total - 1.0) < 0.001

    def test_weights_must_sum_to_one(self):
        with pytest.raises(ValueError, match="must sum to 1.0"):
            TQSConfig(w_setup_quality=0.5, w_trigger_strength=0.5,
                      w_market_context=0.5, w_execution_context=0.5)

    def test_config_is_frozen(self):
        cfg = TQSConfig()
        with pytest.raises(AttributeError):
            cfg.min_tqs = 0.99

    def test_result_is_frozen(self):
        r = TQSResult(score=0.5, setup_quality=0.5, trigger_strength=0.5,
                       market_context=0.5, execution_context=0.5, passed=True)
        with pytest.raises(AttributeError):
            r.score = 0.9


# ── Component Scoring (5) ───────────────────────────────────

class TestComponents:
    def test_setup_quality_from_trigger(self):
        scorer = TradeQualityScorer()
        r = scorer.evaluate(_trigger(trigger_quality=0.8), _snap())
        assert r.setup_quality == pytest.approx(0.8)

    def test_trigger_strength_from_trigger(self):
        scorer = TradeQualityScorer()
        r = scorer.evaluate(_trigger(strength=0.9), _snap())
        assert r.trigger_strength == pytest.approx(0.9)

    def test_market_context_combines_regime_and_rr(self):
        scorer = TradeQualityScorer()
        # R:R = (53000-50000)/(50000-49000) = 3.0
        r = scorer.evaluate(
            _trigger(regime_confidence=1.0, take_profit=53000.0), _snap(),
        )
        assert r.market_context == pytest.approx(1.0, abs=0.01)

    def test_execution_context_uses_capital_and_heat(self):
        scorer = TradeQualityScorer()
        r = scorer.evaluate(_trigger(), _snap(available=5000, total=10000, heat=0.02))
        assert 0.0 < r.execution_context <= 1.0

    def test_execution_context_zero_capital(self):
        scorer = TradeQualityScorer()
        r = scorer.evaluate(_trigger(), _snap(available=0, total=0, heat=0))
        assert r.execution_context == pytest.approx(0.5)


# ── R:R Mapping (4) ─────────────────────────────────────────

class TestRRMapping:
    def test_rr_excellent(self):
        scorer = TradeQualityScorer()
        assert scorer._score_risk_reward(3.5) == pytest.approx(1.0)

    def test_rr_good(self):
        scorer = TradeQualityScorer()
        assert scorer._score_risk_reward(2.0) == pytest.approx(0.7)

    def test_rr_minimum(self):
        scorer = TradeQualityScorer()
        assert scorer._score_risk_reward(1.0) == pytest.approx(0.3)

    def test_rr_below_minimum(self):
        scorer = TradeQualityScorer()
        assert scorer._score_risk_reward(0.5) == 0.0


# ── Composite Bounds (3) ────────────────────────────────────

class TestCompositeBounds:
    def test_score_between_zero_and_one(self):
        scorer = TradeQualityScorer()
        r = scorer.evaluate(_trigger(), _snap())
        assert 0.0 <= r.score <= 1.0

    def test_perfect_inputs_near_one(self):
        scorer = TradeQualityScorer()
        # R:R = (55000-50000)/(50000-49000) = 5.0
        r = scorer.evaluate(
            _trigger(strength=1.0, trigger_quality=1.0,
                     take_profit=55000.0, regime_confidence=1.0),
            _snap(available=10000, total=10000, heat=0.0),
        )
        assert r.score >= 0.90

    def test_worst_inputs_near_zero(self):
        scorer = TradeQualityScorer()
        # R:R = (50500-50000)/(50000-49000) = 0.5
        r = scorer.evaluate(
            _trigger(strength=0.0, trigger_quality=0.0,
                     take_profit=50500.0, regime_confidence=0.0),
            _snap(available=0, total=10000, heat=0.06),
        )
        assert r.score <= 0.10


# ── TQS Floor Gate (3) ──────────────────────────────────────

class TestTQSFloor:
    def test_above_floor_passes(self):
        scorer = TradeQualityScorer()
        r = scorer.evaluate(_trigger(), _snap())
        assert r.passed is True
        assert r.reason == ""

    def test_below_floor_fails(self):
        scorer = TradeQualityScorer(TQSConfig(min_tqs=0.99))
        r = scorer.evaluate(_trigger(), _snap())
        assert r.passed is False
        assert "below floor" in r.reason

    def test_custom_floor(self):
        scorer = TradeQualityScorer(TQSConfig(min_tqs=0.10))
        # R:R = (51000-50000)/(50000-49000) = 1.0
        r = scorer.evaluate(
            _trigger(strength=0.2, trigger_quality=0.2,
                     take_profit=51000.0, regime_confidence=0.2),
            _snap(available=1000, total=10000, heat=0.04),
        )
        assert r.passed is True


# ── Determinism (3) ──────────────────────────────────────────

class TestDeterminism:
    def test_same_inputs_same_score(self):
        scorer = TradeQualityScorer()
        t, s = _trigger(), _snap()
        r1 = scorer.evaluate(t, s)
        r2 = scorer.evaluate(t, s)
        assert r1.score == r2.score
        assert r1.passed == r2.passed

    def test_to_dict_stable(self):
        scorer = TradeQualityScorer()
        r = scorer.evaluate(_trigger(), _snap())
        assert r.to_dict() == r.to_dict()

    def test_different_inputs_different_scores(self):
        scorer = TradeQualityScorer()
        r1 = scorer.evaluate(_trigger(strength=0.9), _snap())
        r2 = scorer.evaluate(_trigger(strength=0.1), _snap())
        assert r1.score != r2.score


# ── Edge Cases (3) ───────────────────────────────────────────

class TestEdgeCases:
    def test_clamped_above_one(self):
        scorer = TradeQualityScorer()
        r = scorer.evaluate(_trigger(strength=1.5, trigger_quality=2.0), _snap())
        assert r.setup_quality == 1.0
        assert r.trigger_strength == 1.0

    def test_negative_heat(self):
        scorer = TradeQualityScorer()
        r = scorer.evaluate(_trigger(), _snap(heat=-0.01))
        assert r.execution_context > 0

    def test_to_dict_keys(self):
        scorer = TradeQualityScorer()
        r = scorer.evaluate(_trigger(), _snap())
        expected = {"tqs_score", "setup_quality", "trigger_strength",
                    "market_context", "execution_context", "passed", "reason"}
        assert set(r.to_dict().keys()) == expected
