# ============================================================
# Phase 4 — Signal Contract Tests
#
# Tests SetupSignal, TriggerSignal validation, immutability,
# ID generation determinism, and invalid payload rejection.
# ============================================================
import pytest
import time

from core.intraday.signal_contracts import (
    ContractViolation,
    Direction,
    SetupLifecycle,
    SetupSignal,
    StrategyClass,
    TriggerLifecycle,
    TriggerSignal,
    make_setup_id,
    make_trigger_id,
    validate_setup_signal,
    validate_setup_signal_strict,
    validate_trigger_signal,
    validate_trigger_signal_strict,
)


# ── Fixtures ──────────────────────────────────────────────────

def _valid_setup(**overrides) -> SetupSignal:
    defaults = dict(
        setup_id=make_setup_id("test_strat", "BTC/USDT", "long", 1000000),
        strategy_name="test_strat",
        strategy_class=StrategyClass.MOMENTUM_EXPANSION,
        symbol="BTC/USDT",
        direction=Direction.LONG,
        setup_timeframe="15m",
        trigger_timeframe="1m",
        entry_zone_low=100.0,
        entry_zone_high=102.0,
        stop_loss=95.0,
        take_profit=110.0,
        atr_value=5.0,
        regime="bull_trend",
        regime_confidence=0.8,
        setup_candle_ts=1000000,
        candle_trace_ids=("abc123",),
        lifecycle=SetupLifecycle.QUALIFIED,
        max_age_ms=60000,
        drift_tolerance=0.003,
        base_time_stop_ms=3600000,
    )
    defaults.update(overrides)
    return SetupSignal(**defaults)


def _valid_trigger(setup=None, **overrides) -> TriggerSignal:
    if setup is None:
        setup = _valid_setup()
    defaults = dict(
        trigger_id=make_trigger_id(setup.setup_id, 2000000),
        setup_id=setup.setup_id,
        strategy_name="test_strat",
        strategy_class=StrategyClass.MOMENTUM_EXPANSION,
        symbol="BTC/USDT",
        direction=Direction.LONG,
        entry_price=101.0,
        stop_loss=95.0,
        take_profit=110.0,
        atr_value=5.0,
        strength=0.8,
        trigger_quality=0.75,
        setup_timeframe="15m",
        trigger_timeframe="1m",
        regime="bull_trend",
        regime_confidence=0.8,
        trigger_candle_ts=2000000,
        setup_candle_ts=1000000,
        candle_trace_ids=("def456",),
        setup_trace_ids=("abc123",),
        lifecycle=TriggerLifecycle.FIRED,
        max_age_ms=30000,
        drift_tolerance=0.003,
    )
    defaults.update(overrides)
    return TriggerSignal(**defaults)


# ── ID Generation ─────────────────────────────────────────────

class TestIDGeneration:
    def test_setup_id_deterministic(self):
        a = make_setup_id("mx", "BTC/USDT", "long", 1000)
        b = make_setup_id("mx", "BTC/USDT", "long", 1000)
        assert a == b

    def test_setup_id_different_inputs(self):
        a = make_setup_id("mx", "BTC/USDT", "long", 1000)
        b = make_setup_id("mx", "BTC/USDT", "short", 1000)
        assert a != b

    def test_trigger_id_deterministic(self):
        a = make_trigger_id("setup_abc", 2000)
        b = make_trigger_id("setup_abc", 2000)
        assert a == b

    def test_trigger_id_chained_from_setup(self):
        a = make_trigger_id("setup_abc", 2000)
        b = make_trigger_id("setup_def", 2000)
        assert a != b


# ── SetupSignal Validation ────────────────────────────────────

class TestSetupSignalValidation:
    def test_valid_setup_passes(self):
        setup = _valid_setup()
        assert validate_setup_signal(setup) == []

    def test_empty_setup_id_fails(self):
        setup = _valid_setup(setup_id="")
        violations = validate_setup_signal(setup)
        assert any("setup_id" in v for v in violations)

    def test_empty_symbol_fails(self):
        setup = _valid_setup(symbol="")
        violations = validate_setup_signal(setup)
        assert any("symbol" in v for v in violations)

    def test_invalid_timeframe_fails(self):
        setup = _valid_setup(setup_timeframe="2h")
        violations = validate_setup_signal(setup)
        assert any("setup_timeframe" in v for v in violations)

    def test_negative_stop_loss_fails(self):
        setup = _valid_setup(stop_loss=-1.0)
        violations = validate_setup_signal(setup)
        assert any("stop_loss" in v for v in violations)

    def test_long_stop_above_entry_fails(self):
        setup = _valid_setup(direction=Direction.LONG, stop_loss=105.0)
        violations = validate_setup_signal(setup)
        assert any("stop_loss" in v and "below" in v for v in violations)

    def test_long_tp_below_entry_fails(self):
        setup = _valid_setup(direction=Direction.LONG, take_profit=99.0)
        violations = validate_setup_signal(setup)
        assert any("take_profit" in v and "above" in v for v in violations)

    def test_short_stop_below_entry_fails(self):
        setup = _valid_setup(
            direction=Direction.SHORT,
            entry_zone_low=100.0, entry_zone_high=102.0,
            stop_loss=99.0, take_profit=90.0,
        )
        violations = validate_setup_signal(setup)
        assert any("stop_loss" in v and "above" in v for v in violations)

    def test_short_valid(self):
        setup = _valid_setup(
            direction=Direction.SHORT,
            entry_zone_low=100.0, entry_zone_high=102.0,
            stop_loss=105.0, take_profit=90.0,
        )
        assert validate_setup_signal(setup) == []

    def test_entry_zone_inverted_fails(self):
        setup = _valid_setup(entry_zone_low=105.0, entry_zone_high=100.0)
        violations = validate_setup_signal(setup)
        assert any("entry_zone_low > entry_zone_high" in v for v in violations)

    def test_empty_trace_ids_fails(self):
        setup = _valid_setup(candle_trace_ids=())
        violations = validate_setup_signal(setup)
        assert any("candle_trace_ids" in v for v in violations)

    def test_zero_max_age_fails(self):
        setup = _valid_setup(max_age_ms=0)
        violations = validate_setup_signal(setup)
        assert any("max_age_ms" in v for v in violations)

    def test_strict_raises(self):
        setup = _valid_setup(setup_id="")
        with pytest.raises(ContractViolation):
            validate_setup_signal_strict(setup)


# ── TriggerSignal Validation ──────────────────────────────────

class TestTriggerSignalValidation:
    def test_valid_trigger_passes(self):
        trigger = _valid_trigger()
        assert validate_trigger_signal(trigger) == []

    def test_empty_setup_id_fails(self):
        trigger = _valid_trigger(setup_id="")
        violations = validate_trigger_signal(trigger)
        assert any("setup_id" in v for v in violations)

    def test_strength_out_of_range_fails(self):
        trigger = _valid_trigger(strength=1.5)
        violations = validate_trigger_signal(trigger)
        assert any("strength" in v for v in violations)

    def test_trigger_quality_out_of_range_fails(self):
        trigger = _valid_trigger(trigger_quality=-0.1)
        violations = validate_trigger_signal(trigger)
        assert any("trigger_quality" in v for v in violations)

    def test_long_stop_above_entry_fails(self):
        trigger = _valid_trigger(entry_price=101.0, stop_loss=105.0)
        violations = validate_trigger_signal(trigger)
        assert any("stop_loss" in v for v in violations)

    def test_strict_raises(self):
        trigger = _valid_trigger(trigger_id="")
        with pytest.raises(ContractViolation):
            validate_trigger_signal_strict(trigger)


# ── Immutability ──────────────────────────────────────────────

class TestImmutability:
    def test_setup_is_frozen(self):
        setup = _valid_setup()
        with pytest.raises(AttributeError):
            setup.symbol = "ETH/USDT"

    def test_trigger_is_frozen(self):
        trigger = _valid_trigger()
        with pytest.raises(AttributeError):
            trigger.entry_price = 999.0

    def test_setup_to_dict_is_copy(self):
        setup = _valid_setup()
        d = setup.to_dict()
        d["symbol"] = "modified"
        assert setup.symbol == "BTC/USDT"

    def test_trigger_to_dict_is_copy(self):
        trigger = _valid_trigger()
        d = trigger.to_dict()
        d["entry_price"] = 999.0
        assert trigger.entry_price == 101.0


# ── Properties ────────────────────────────────────────────────

class TestProperties:
    def test_setup_rr_long(self):
        setup = _valid_setup(
            entry_zone_low=100.0, entry_zone_high=102.0,
            stop_loss=95.0, take_profit=110.0,
        )
        # mid = 101, risk = 6, reward = 9
        assert setup.risk_reward_ratio == pytest.approx(1.5, abs=0.01)

    def test_setup_rr_short(self):
        setup = _valid_setup(
            direction=Direction.SHORT,
            entry_zone_low=100.0, entry_zone_high=102.0,
            stop_loss=105.0, take_profit=90.0,
        )
        # mid = 101, risk = 4, reward = 11
        assert setup.risk_reward_ratio == pytest.approx(2.75, abs=0.01)

    def test_trigger_rr(self):
        trigger = _valid_trigger(entry_price=101.0, stop_loss=95.0, take_profit=110.0)
        # risk = 6, reward = 9
        assert trigger.risk_reward_ratio == pytest.approx(1.5, abs=0.01)
