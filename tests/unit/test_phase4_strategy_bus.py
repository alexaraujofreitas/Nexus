# ============================================================
# Phase 4 — StrategyBus Tests
#
# Tests for the StrategyBus orchestrator:
#   - Correct routing of timeframe events
#   - Setup lifecycle management
#   - Pending setup invalidation / expiry
#   - Trigger only fires from valid setup
#   - Expired signal blocked
#   - Duplicate / conflicting setup handling
#   - Metrics tracking
# ============================================================
import pytest
import time
import numpy as np
import pandas as pd
from unittest.mock import MagicMock, patch

from core.event_bus import Event, EventBus, Topics
from core.intraday.base_strategy import BaseIntradayStrategy, RegimeInfo
from core.intraday.signal_contracts import (
    Direction,
    SetupLifecycle,
    SetupSignal,
    StrategyClass,
    TriggerLifecycle,
    TriggerSignal,
    make_setup_id,
    make_trigger_id,
)
from core.intraday.strategy_bus import StrategyBus


# ── Mock Strategy ─────────────────────────────────────────────

class MockStrategy(BaseIntradayStrategy):
    NAME = "mock_strategy"
    STRATEGY_CLASS = StrategyClass.MOMENTUM_EXPANSION
    SETUP_TIMEFRAME = "15m"
    TRIGGER_TIMEFRAME = "1m"
    MAX_SETUP_AGE_MS = 60000
    MAX_TRIGGER_AGE_MS = 5000
    DRIFT_TOLERANCE = 0.003
    BASE_TIME_STOP_MS = 3600000
    REGIME_AFFINITY = {"bull_trend": 1.0, "uncertain": 0.5}

    def __init__(self):
        self._setup_result = None
        self._trigger_result = None

    def set_setup_result(self, result):
        self._setup_result = result

    def set_trigger_result(self, result):
        self._trigger_result = result

    def evaluate_setup(self, symbol, df_setup, regime_info):
        return self._setup_result

    def evaluate_trigger(self, symbol, df_trigger, setup, regime_info):
        return self._trigger_result


def _make_df(n=50):
    np.random.seed(42)
    return pd.DataFrame({
        "timestamp": [1000000 + i * 60000 for i in range(n)],
        "open": np.random.uniform(99, 101, n),
        "high": np.random.uniform(100, 102, n),
        "low": np.random.uniform(98, 100, n),
        "close": np.random.uniform(99, 101, n),
        "volume": np.random.uniform(900, 1100, n),
        "trace_id": [f"t_{i}" for i in range(n)],
    })


def _make_setup(strategy_name="mock_strategy", symbol="BTC/USDT",
                direction=Direction.LONG, created_at_ms=None, max_age_ms=60000):
    ts = 1000000
    if created_at_ms is None:
        created_at_ms = int(time.time() * 1000)
    return SetupSignal(
        setup_id=make_setup_id(strategy_name, symbol, direction.value, ts),
        strategy_name=strategy_name,
        strategy_class=StrategyClass.MOMENTUM_EXPANSION,
        symbol=symbol,
        direction=direction,
        setup_timeframe="15m",
        trigger_timeframe="1m",
        entry_zone_low=99.0,
        entry_zone_high=101.0,
        stop_loss=95.0,
        take_profit=110.0,
        atr_value=2.0,
        regime="bull_trend",
        regime_confidence=0.8,
        setup_candle_ts=ts,
        candle_trace_ids=("trace_001",),
        lifecycle=SetupLifecycle.QUALIFIED,
        max_age_ms=max_age_ms,
        drift_tolerance=0.003,
        base_time_stop_ms=3600000,
        created_at_ms=created_at_ms,
    )


def _make_trigger(setup, created_at_ms=None):
    ts = 2000000
    if created_at_ms is None:
        created_at_ms = int(time.time() * 1000)
    return TriggerSignal(
        trigger_id=make_trigger_id(setup.setup_id, ts),
        setup_id=setup.setup_id,
        strategy_name=setup.strategy_name,
        strategy_class=StrategyClass.MOMENTUM_EXPANSION,
        symbol=setup.symbol,
        direction=setup.direction,
        entry_price=100.0,
        stop_loss=95.0,
        take_profit=110.0,
        atr_value=2.0,
        strength=0.8,
        trigger_quality=0.7,
        setup_timeframe="15m",
        trigger_timeframe="1m",
        regime="bull_trend",
        regime_confidence=0.8,
        trigger_candle_ts=ts,
        setup_candle_ts=setup.setup_candle_ts,
        candle_trace_ids=("trig_trace_001",),
        setup_trace_ids=setup.candle_trace_ids,
        lifecycle=TriggerLifecycle.FIRED,
        max_age_ms=5000,
        drift_tolerance=0.003,
        created_at_ms=created_at_ms,
    )


# ── Tests ─────────────────────────────────────────────────────

class TestStrategyBusRouting:
    def test_subscribes_to_candle_topics(self):
        test_bus = EventBus()
        strat = MockStrategy()
        sb = StrategyBus(strategies=[strat], event_bus=test_bus)
        sb.start()
        assert len(test_bus._subscribers[Topics.CANDLE_1M]) > 0
        assert len(test_bus._subscribers[Topics.CANDLE_15M]) > 0
        sb.stop()

    def test_unsubscribes_on_stop(self):
        test_bus = EventBus()
        strat = MockStrategy()
        sb = StrategyBus(strategies=[strat], event_bus=test_bus)
        sb.start()
        sb.stop()
        assert len(test_bus._subscribers[Topics.CANDLE_1M]) == 0

    def test_routes_setup_tf_to_setup_evaluation(self):
        test_bus = EventBus()
        strat = MockStrategy()
        now = int(time.time() * 1000)
        setup = _make_setup(created_at_ms=now)
        strat.set_setup_result(setup)

        df = _make_df()
        sb = StrategyBus(
            strategies=[strat],
            regime_provider=lambda s: RegimeInfo("bull_trend", 0.8, {}),
            candle_history_provider=lambda s, tf, n: df,
            event_bus=test_bus,
        )
        sb.start()

        # Publish 15m candle (setup TF)
        test_bus.publish(Topics.CANDLE_15M, {"symbol": "BTC/USDT", "candle": {}})

        assert sb.metrics.setups_evaluated >= 1
        assert sb.metrics.setups_qualified >= 1
        assert len(sb.pending_setups.get("BTC/USDT", {})) >= 1
        sb.stop()

    def test_routes_trigger_tf_to_trigger_evaluation(self):
        test_bus = EventBus()
        strat = MockStrategy()
        now = int(time.time() * 1000)
        setup = _make_setup(created_at_ms=now)
        trigger = _make_trigger(setup, created_at_ms=now)
        strat.set_setup_result(setup)
        strat.set_trigger_result(trigger)

        df = _make_df()
        sb = StrategyBus(
            strategies=[strat],
            regime_provider=lambda s: RegimeInfo("bull_trend", 0.8, {}),
            candle_history_provider=lambda s, tf, n: df,
            event_bus=test_bus,
        )
        sb.start()

        # First, create a setup
        test_bus.publish(Topics.CANDLE_15M, {"symbol": "BTC/USDT"})
        assert sb.metrics.setups_qualified >= 1

        # Then trigger
        test_bus.publish(Topics.CANDLE_1M, {"symbol": "BTC/USDT"})
        assert sb.metrics.triggers_evaluated >= 1
        sb.stop()


class TestSetupLifecycle:
    def test_setup_stored_as_pending(self):
        test_bus = EventBus()
        strat = MockStrategy()
        now = int(time.time() * 1000)
        setup = _make_setup(created_at_ms=now)
        strat.set_setup_result(setup)

        sb = StrategyBus(
            strategies=[strat],
            regime_provider=lambda s: RegimeInfo("bull_trend", 0.8, {}),
            candle_history_provider=lambda s, tf, n: _make_df(),
            event_bus=test_bus,
        )
        sb.start()
        test_bus.publish(Topics.CANDLE_15M, {"symbol": "BTC/USDT"})

        pending = sb.pending_setups
        assert "BTC/USDT" in pending
        assert setup.setup_id in pending["BTC/USDT"]
        sb.stop()

    def test_duplicate_setup_replaces_old(self):
        test_bus = EventBus()
        strat = MockStrategy()
        now = int(time.time() * 1000)

        setup1 = _make_setup(created_at_ms=now)
        strat.set_setup_result(setup1)

        sb = StrategyBus(
            strategies=[strat],
            regime_provider=lambda s: RegimeInfo("bull_trend", 0.8, {}),
            candle_history_provider=lambda s, tf, n: _make_df(),
            event_bus=test_bus,
        )
        sb.start()
        test_bus.publish(Topics.CANDLE_15M, {"symbol": "BTC/USDT"})

        # Same strategy, same direction → should replace
        setup2 = _make_setup(created_at_ms=now + 1000)
        strat.set_setup_result(setup2)
        test_bus.publish(Topics.CANDLE_15M, {"symbol": "BTC/USDT"})

        pending = sb.pending_setups.get("BTC/USDT", {})
        # Should have exactly 1 setup (replaced)
        assert len(pending) == 1
        sb.stop()

    def test_setup_expires_after_max_age(self):
        test_bus = EventBus()
        strat = MockStrategy()
        sim_time = [1000000000000]

        # Setup with 100ms max age
        setup = _make_setup(created_at_ms=sim_time[0], max_age_ms=100)
        strat.set_setup_result(setup)

        sb = StrategyBus(
            strategies=[strat],
            regime_provider=lambda s: RegimeInfo("bull_trend", 0.8, {}),
            candle_history_provider=lambda s, tf, n: _make_df(),
            event_bus=test_bus,
            now_ms_fn=lambda: sim_time[0],
        )
        sb.start()
        test_bus.publish(Topics.CANDLE_15M, {"symbol": "BTC/USDT"})
        assert len(sb.pending_setups.get("BTC/USDT", {})) == 1

        # Advance time past expiry
        sim_time[0] += 200
        strat.set_setup_result(None)  # No new setup
        test_bus.publish(Topics.CANDLE_15M, {"symbol": "BTC/USDT"})

        assert len(sb.pending_setups.get("BTC/USDT", {})) == 0
        assert sb.metrics.setups_expired >= 1
        sb.stop()


class TestTriggerFromSetup:
    def test_trigger_only_fires_with_valid_setup(self):
        test_bus = EventBus()
        strat = MockStrategy()
        # Don't create any setup
        strat.set_setup_result(None)
        strat.set_trigger_result(None)

        sb = StrategyBus(
            strategies=[strat],
            regime_provider=lambda s: RegimeInfo("bull_trend", 0.8, {}),
            candle_history_provider=lambda s, tf, n: _make_df(),
            event_bus=test_bus,
        )
        sb.start()

        # Trigger TF event with no pending setup
        test_bus.publish(Topics.CANDLE_1M, {"symbol": "BTC/USDT"})
        assert sb.metrics.triggers_evaluated == 0  # No setup → no evaluation
        sb.stop()

    def test_setup_consumed_after_trigger(self):
        test_bus = EventBus()
        strat = MockStrategy()
        sim_time = [1000000000000]
        setup = _make_setup(created_at_ms=sim_time[0])
        trigger = _make_trigger(setup, created_at_ms=sim_time[0])
        strat.set_setup_result(setup)
        strat.set_trigger_result(trigger)

        # DF with close=100 matching trigger entry_price
        n = 50
        df = pd.DataFrame({
            "timestamp": [1000000 + i * 60000 for i in range(n)],
            "open": [100.0] * n,
            "high": [101.0] * n,
            "low": [99.0] * n,
            "close": [100.0] * n,
            "volume": [1000.0] * n,
            "trace_id": [f"t_{i}" for i in range(n)],
        })

        sb = StrategyBus(
            strategies=[strat],
            regime_provider=lambda s: RegimeInfo("bull_trend", 0.8, {}),
            candle_history_provider=lambda s, tf, n: df,
            event_bus=test_bus,
            now_ms_fn=lambda: sim_time[0],
        )
        sb.start()

        test_bus.publish(Topics.CANDLE_15M, {"symbol": "BTC/USDT"})
        assert len(sb.pending_setups.get("BTC/USDT", {})) >= 1

        sim_time[0] += 1000  # Advance 1 second
        test_bus.publish(Topics.CANDLE_1M, {"symbol": "BTC/USDT"})
        # Setup should be consumed after trigger fires
        pending = sb.pending_setups.get("BTC/USDT", {})
        assert setup.setup_id not in pending
        sb.stop()


class TestEventPublishing:
    def test_publishes_setup_qualified_event(self):
        test_bus = EventBus()
        strat = MockStrategy()
        now = int(time.time() * 1000)
        setup = _make_setup(created_at_ms=now)
        strat.set_setup_result(setup)

        events_received = []
        test_bus.subscribe(Topics.SETUP_QUALIFIED, lambda e: events_received.append(e))

        sb = StrategyBus(
            strategies=[strat],
            regime_provider=lambda s: RegimeInfo("bull_trend", 0.8, {}),
            candle_history_provider=lambda s, tf, n: _make_df(),
            event_bus=test_bus,
        )
        sb.start()
        test_bus.publish(Topics.CANDLE_15M, {"symbol": "BTC/USDT"})

        assert len(events_received) >= 1
        assert events_received[0].data["setup_id"] == setup.setup_id
        sb.stop()

    def test_publishes_trigger_fired_event(self):
        test_bus = EventBus()
        strat = MockStrategy()
        sim_time = [1000000000000]
        setup = _make_setup(created_at_ms=sim_time[0])
        trigger = _make_trigger(setup, created_at_ms=sim_time[0])
        strat.set_setup_result(setup)
        strat.set_trigger_result(trigger)

        # DF with close=100 matching trigger entry_price
        n = 50
        df = pd.DataFrame({
            "timestamp": [1000000 + i * 60000 for i in range(n)],
            "open": [100.0] * n,
            "high": [101.0] * n,
            "low": [99.0] * n,
            "close": [100.0] * n,
            "volume": [1000.0] * n,
            "trace_id": [f"t_{i}" for i in range(n)],
        })

        trigger_events = []
        test_bus.subscribe(Topics.TRIGGER_FIRED, lambda e: trigger_events.append(e))

        sb = StrategyBus(
            strategies=[strat],
            regime_provider=lambda s: RegimeInfo("bull_trend", 0.8, {}),
            candle_history_provider=lambda s, tf, n_bars: df,
            event_bus=test_bus,
            now_ms_fn=lambda: sim_time[0],
        )
        sb.start()
        test_bus.publish(Topics.CANDLE_15M, {"symbol": "BTC/USDT"})
        sim_time[0] += 1000
        test_bus.publish(Topics.CANDLE_1M, {"symbol": "BTC/USDT"})

        assert len(trigger_events) >= 1
        assert trigger_events[0].data["trigger_id"] == trigger.trigger_id
        sb.stop()


class TestMetrics:
    def test_metrics_tracking(self):
        test_bus = EventBus()
        strat = MockStrategy()
        now = int(time.time() * 1000)
        setup = _make_setup(created_at_ms=now)
        strat.set_setup_result(setup)

        sb = StrategyBus(
            strategies=[strat],
            regime_provider=lambda s: RegimeInfo("bull_trend", 0.8, {}),
            candle_history_provider=lambda s, tf, n: _make_df(),
            event_bus=test_bus,
        )
        sb.start()
        test_bus.publish(Topics.CANDLE_15M, {"symbol": "BTC/USDT"})

        m = sb.metrics.snapshot()
        assert m["candles_received"] >= 1
        assert m["setups_evaluated"] >= 1
        assert m["setups_qualified"] >= 1
        sb.stop()
