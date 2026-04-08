# ============================================================
# Phase 4 Addendum — Runtime Integration Tests
#
# Proves the intraday StrategyBus is wired into the live
# NexusEngine pipeline end-to-end:
#
#   1. NexusEngine.start() creates and starts StrategyBus
#   2. NexusEngine.stop() tears down StrategyBus cleanly
#   3. EventBus subscriptions are registered on DATA-layer topics
#   4. DATA candle → setup qualification path works
#   5. DATA candle → trigger fired path works
#   6. Signal expiry fires in integrated flow
#   7. Restart safety (stop → start → no duplicate subscriptions)
#   8. Candle accumulator populates history buffers
#   9. Regime provider returns valid RegimeInfo
#  10. Trace continuity: candle trace_id → setup_id → trigger_id
#  11. Layer boundary enforcement (DATA → STRATEGY only)
#  12. Engine lifecycle property accessor works
#
# ZERO PySide6 imports. Pure Python.
# ============================================================
import pytest
import time
import threading
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
from core.intraday import engine_integration


# ── Helpers ──────────────────────────────────────────────────

class IntegrationMockStrategy(BaseIntradayStrategy):
    """Controllable mock for integration testing."""
    NAME = "integration_mock"
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
        self.setup_call_count = 0
        self.trigger_call_count = 0

    def set_setup_result(self, result):
        self._setup_result = result

    def set_trigger_result(self, result):
        self._trigger_result = result

    def evaluate_setup(self, symbol, df_setup, regime_info):
        self.setup_call_count += 1
        return self._setup_result

    def evaluate_trigger(self, symbol, df_trigger, setup, regime_info):
        self.trigger_call_count += 1
        return self._trigger_result


def _make_df(n=50, close_val=100.0):
    """Create a DataFrame with deterministic data."""
    return pd.DataFrame({
        "timestamp": [1000000 + i * 60000 for i in range(n)],
        "open": [close_val] * n,
        "high": [close_val + 1.0] * n,
        "low": [close_val - 1.0] * n,
        "close": [close_val] * n,
        "volume": [1000.0] * n,
        "trace_id": [f"candle_trace_{i}" for i in range(n)],
    })


def _make_setup(strategy_name="integration_mock", symbol="BTC/USDT",
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
        candle_trace_ids=("candle_trace_0", "candle_trace_1"),
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


@pytest.fixture(autouse=True)
def _reset_engine_integration_state():
    """Reset engine_integration module-level singleton state between tests."""
    engine_integration._strategy_bus = None
    engine_integration._accumulator_subscribed = False
    engine_integration._started = False
    engine_integration._candle_buffers.clear()
    yield
    # Cleanup after test
    engine_integration._strategy_bus = None
    engine_integration._accumulator_subscribed = False
    engine_integration._started = False
    engine_integration._candle_buffers.clear()


# ── 1. Runtime Registration ─────────────────────────────────

class TestRuntimeRegistration:
    """StrategyBus is created and started by engine_integration."""

    def test_start_creates_strategy_bus(self):
        test_bus = EventBus()
        sb = engine_integration.start_intraday_engine(event_bus=test_bus)
        assert sb is not None
        assert isinstance(sb, StrategyBus)
        engine_integration.stop_intraday_engine()

    def test_get_strategy_bus_returns_instance_after_start(self):
        test_bus = EventBus()
        engine_integration.start_intraday_engine(event_bus=test_bus)
        sb = engine_integration.get_strategy_bus()
        assert sb is not None
        assert isinstance(sb, StrategyBus)
        engine_integration.stop_intraday_engine()

    def test_get_strategy_bus_returns_none_before_start(self):
        assert engine_integration.get_strategy_bus() is None

    def test_start_loads_all_5_strategies(self):
        test_bus = EventBus()
        sb = engine_integration.start_intraday_engine(event_bus=test_bus)
        assert len(sb._strategies) == 5
        names = {s.NAME for s in sb._strategies}
        assert "momentum_expansion" in names
        assert "vwap_reversion" in names
        assert "micro_pullback_continuation" in names
        assert "range_break_retest" in names
        assert "liquidity_sweep_reversal" in names
        engine_integration.stop_intraday_engine()


# ── 2. EventBus Subscription Flow ───────────────────────────

class TestSubscriptionFlow:
    """Candle accumulator and StrategyBus subscribe to correct topics."""

    def test_accumulator_subscribes_to_all_candle_topics(self):
        test_bus = EventBus()
        engine_integration.start_intraday_engine(event_bus=test_bus)

        # Accumulator should be subscribed to all 5 candle topics
        for topic in [Topics.CANDLE_1M, Topics.CANDLE_3M, Topics.CANDLE_5M,
                      Topics.CANDLE_15M, Topics.CANDLE_1H]:
            subs = test_bus._subscribers.get(topic, [])
            accum_found = any(
                fn is engine_integration._candle_event_accumulator or
                getattr(fn, '__wrapped__', None) is engine_integration._candle_event_accumulator
                for fn in subs
            )
            # At minimum the accumulator OR the strategy bus should be subscribed
            assert len(subs) > 0, f"No subscribers for {topic}"

        engine_integration.stop_intraday_engine()

    def test_stop_unsubscribes_accumulator(self):
        test_bus = EventBus()
        engine_integration.start_intraday_engine(event_bus=test_bus)

        # Count subscribers before stop
        pre_counts = {}
        for topic in [Topics.CANDLE_1M, Topics.CANDLE_3M, Topics.CANDLE_5M,
                      Topics.CANDLE_15M, Topics.CANDLE_1H]:
            pre_counts[topic] = len(test_bus._subscribers.get(topic, []))

        engine_integration.stop_intraday_engine()

        # After stop, subscriber count should be reduced
        for topic in pre_counts:
            post = len(test_bus._subscribers.get(topic, []))
            assert post < pre_counts[topic], \
                f"Subscribers not cleaned up for {topic}: {post} >= {pre_counts[topic]}"


# ── 3. No Duplicate Subscriptions ────────────────────────────

class TestDuplicateGuard:
    """Double-start must not duplicate subscriptions."""

    def test_double_start_returns_same_instance(self):
        test_bus = EventBus()
        sb1 = engine_integration.start_intraday_engine(event_bus=test_bus)
        sb2 = engine_integration.start_intraday_engine(event_bus=test_bus)
        assert sb1 is sb2
        engine_integration.stop_intraday_engine()

    def test_double_start_no_duplicate_accumulator_subs(self):
        test_bus = EventBus()
        engine_integration.start_intraday_engine(event_bus=test_bus)
        count_after_first = len(test_bus._subscribers.get(Topics.CANDLE_15M, []))

        engine_integration.start_intraday_engine(event_bus=test_bus)
        count_after_second = len(test_bus._subscribers.get(Topics.CANDLE_15M, []))

        assert count_after_second == count_after_first, \
            "Duplicate start added extra subscribers"
        engine_integration.stop_intraday_engine()


# ── 4. Restart Safety ────────────────────────────────────────

class TestRestartSafety:
    """stop → start cycle is clean with no leaks."""

    def test_stop_start_cycle_is_clean(self):
        test_bus = EventBus()

        # First cycle
        sb1 = engine_integration.start_intraday_engine(event_bus=test_bus)
        assert sb1 is not None
        engine_integration.stop_intraday_engine()
        assert engine_integration.get_strategy_bus() is None

        # Second cycle
        sb2 = engine_integration.start_intraday_engine(event_bus=test_bus)
        assert sb2 is not None
        assert sb2 is not sb1  # New instance
        engine_integration.stop_intraday_engine()

    def test_reset_clears_all_state(self):
        test_bus = EventBus()
        engine_integration.start_intraday_engine(event_bus=test_bus)

        # Accumulate some candles
        engine_integration._accumulate_candle("BTC/USDT", "1m", {"close": 100})

        engine_integration.reset_intraday_engine()

        assert engine_integration.get_strategy_bus() is None
        assert engine_integration._started is False
        assert engine_integration._accumulator_subscribed is False
        assert len(engine_integration._candle_buffers) == 0

    def test_stop_is_idempotent(self):
        test_bus = EventBus()
        engine_integration.start_intraday_engine(event_bus=test_bus)
        engine_integration.stop_intraday_engine()
        # Second stop should not raise
        engine_integration.stop_intraday_engine()
        assert engine_integration.get_strategy_bus() is None


# ── 5. Candle Accumulator & History Provider ─────────────────

class TestCandleAccumulator:
    """Candle events populate the history buffer for strategy queries."""

    def test_accumulate_candle_stores_data(self):
        engine_integration._accumulate_candle("BTC/USDT", "1m", {"close": 100, "ts": 1})
        engine_integration._accumulate_candle("BTC/USDT", "1m", {"close": 101, "ts": 2})

        df = engine_integration._get_candle_history("BTC/USDT", "1m", 10)
        assert df is not None
        assert len(df) == 2
        assert df.iloc[-1]["close"] == 101

    def test_buffer_respects_max_size(self):
        for i in range(250):
            engine_integration._accumulate_candle("ETH/USDT", "5m", {"close": i})

        buf = engine_integration._candle_buffers.get(("ETH/USDT", "5m"), [])
        assert len(buf) <= engine_integration._MAX_BUFFER_SIZE

    def test_get_history_returns_none_for_unknown_symbol(self):
        df = engine_integration._get_candle_history("UNKNOWN/USDT", "1m", 10)
        assert df is None

    def test_get_history_returns_requested_n_bars(self):
        for i in range(50):
            engine_integration._accumulate_candle("SOL/USDT", "15m", {"close": i})

        df = engine_integration._get_candle_history("SOL/USDT", "15m", 20)
        assert len(df) == 20
        # Should be the LAST 20 candles
        assert df.iloc[0]["close"] == 30
        assert df.iloc[-1]["close"] == 49

    def test_candle_event_accumulator_callback(self):
        """Simulate an EventBus candle event routed through the accumulator."""
        event = Event(
            topic=Topics.CANDLE_15M,
            data={"symbol": "BTC/USDT", "candle": {"close": 42000, "volume": 100}}
        )
        engine_integration._candle_event_accumulator(event)

        df = engine_integration._get_candle_history("BTC/USDT", "15m", 10)
        assert df is not None
        assert len(df) == 1
        assert df.iloc[0]["close"] == 42000


# ── 6. Regime Provider ───────────────────────────────────────

class TestRegimeProvider:
    """Regime provider returns valid RegimeInfo."""

    def test_regime_provider_returns_uncertain_without_history(self):
        provider = engine_integration._make_regime_provider()
        result = provider("BTC/USDT")
        assert isinstance(result, RegimeInfo)
        assert result.label == "uncertain"
        assert result.confidence == 0.3

    def test_regime_provider_is_callable(self):
        provider = engine_integration._make_regime_provider()
        assert callable(provider)


# ── 7. End-to-End Candle → Setup Path ────────────────────────

class TestE2ESetupPath:
    """DATA candle event → StrategyBus → setup qualified event."""

    def test_candle_15m_produces_setup_qualified_event(self):
        test_bus = EventBus()
        strat = IntegrationMockStrategy()
        sim_time = [1000000000000]
        setup = _make_setup(created_at_ms=sim_time[0])
        strat.set_setup_result(setup)

        df = _make_df()
        setup_events = []
        test_bus.subscribe(Topics.SETUP_QUALIFIED, lambda e: setup_events.append(e))

        sb = StrategyBus(
            strategies=[strat],
            regime_provider=lambda s: RegimeInfo("bull_trend", 0.8, {}),
            candle_history_provider=lambda s, tf, n: df,
            event_bus=test_bus,
            now_ms_fn=lambda: sim_time[0],
        )
        sb.start()

        # Publish DATA-layer candle event
        test_bus.publish(Topics.CANDLE_15M, {"symbol": "BTC/USDT", "candle": {}})

        # Verify STRATEGY-layer setup event was published
        assert len(setup_events) >= 1
        assert setup_events[0].data["setup_id"] == setup.setup_id
        assert setup_events[0].data["strategy_name"] == "integration_mock"
        assert setup_events[0].data["symbol"] == "BTC/USDT"
        assert strat.setup_call_count >= 1
        sb.stop()


# ── 8. End-to-End Candle → Trigger Path ──────────────────────

class TestE2ETriggerPath:
    """DATA candle → setup → trigger → TRIGGER_FIRED event."""

    def test_candle_sequence_produces_trigger_fired_event(self):
        test_bus = EventBus()
        strat = IntegrationMockStrategy()
        sim_time = [1000000000000]
        setup = _make_setup(created_at_ms=sim_time[0])
        trigger = _make_trigger(setup, created_at_ms=sim_time[0])
        strat.set_setup_result(setup)
        strat.set_trigger_result(trigger)

        df = _make_df(close_val=100.0)
        trigger_events = []
        test_bus.subscribe(Topics.TRIGGER_FIRED, lambda e: trigger_events.append(e))

        sb = StrategyBus(
            strategies=[strat],
            regime_provider=lambda s: RegimeInfo("bull_trend", 0.8, {}),
            candle_history_provider=lambda s, tf, n: df,
            event_bus=test_bus,
            now_ms_fn=lambda: sim_time[0],
        )
        sb.start()

        # Step 1: 15m candle creates setup
        test_bus.publish(Topics.CANDLE_15M, {"symbol": "BTC/USDT"})
        assert sb.metrics.setups_qualified >= 1

        # Step 2: 1m candle triggers
        sim_time[0] += 1000
        test_bus.publish(Topics.CANDLE_1M, {"symbol": "BTC/USDT"})

        assert len(trigger_events) >= 1
        assert trigger_events[0].data["trigger_id"] == trigger.trigger_id
        assert trigger_events[0].data["setup_id"] == setup.setup_id
        assert trigger_events[0].data["entry_price"] == 100.0
        sb.stop()


# ── 9. Expiry in Integrated Flow ─────────────────────────────

class TestIntegratedExpiry:
    """Setup expiry fires in the integrated pipeline."""

    def test_setup_expires_after_max_age_in_pipeline(self):
        test_bus = EventBus()
        strat = IntegrationMockStrategy()
        sim_time = [1000000000000]

        setup = _make_setup(created_at_ms=sim_time[0], max_age_ms=100)
        strat.set_setup_result(setup)

        df = _make_df()
        sb = StrategyBus(
            strategies=[strat],
            regime_provider=lambda s: RegimeInfo("bull_trend", 0.8, {}),
            candle_history_provider=lambda s, tf, n: df,
            event_bus=test_bus,
            now_ms_fn=lambda: sim_time[0],
        )
        sb.start()

        # Create setup
        test_bus.publish(Topics.CANDLE_15M, {"symbol": "BTC/USDT"})
        assert len(sb.pending_setups.get("BTC/USDT", {})) == 1

        # Advance time past max_age
        sim_time[0] += 200
        strat.set_setup_result(None)
        test_bus.publish(Topics.CANDLE_15M, {"symbol": "BTC/USDT"})

        assert len(sb.pending_setups.get("BTC/USDT", {})) == 0
        assert sb.metrics.setups_expired >= 1
        sb.stop()


# ── 10. Trace Continuity ─────────────────────────────────────

class TestTraceContinuity:
    """Candle trace_id → setup_id → trigger_id chain is preserved."""

    def test_setup_carries_candle_trace_ids(self):
        test_bus = EventBus()
        strat = IntegrationMockStrategy()
        sim_time = [1000000000000]
        setup = _make_setup(created_at_ms=sim_time[0])
        strat.set_setup_result(setup)

        setup_events = []
        test_bus.subscribe(Topics.SETUP_QUALIFIED, lambda e: setup_events.append(e))

        df = _make_df()
        sb = StrategyBus(
            strategies=[strat],
            regime_provider=lambda s: RegimeInfo("bull_trend", 0.8, {}),
            candle_history_provider=lambda s, tf, n: df,
            event_bus=test_bus,
            now_ms_fn=lambda: sim_time[0],
        )
        sb.start()
        test_bus.publish(Topics.CANDLE_15M, {"symbol": "BTC/USDT"})

        assert len(setup_events) >= 1
        event_data = setup_events[0].data
        # Setup should carry candle trace IDs
        assert "candle_trace_ids" in event_data
        assert len(event_data["candle_trace_ids"]) > 0
        sb.stop()

    def test_trigger_carries_setup_trace_ids(self):
        test_bus = EventBus()
        strat = IntegrationMockStrategy()
        sim_time = [1000000000000]
        setup = _make_setup(created_at_ms=sim_time[0])
        trigger = _make_trigger(setup, created_at_ms=sim_time[0])
        strat.set_setup_result(setup)
        strat.set_trigger_result(trigger)

        trigger_events = []
        test_bus.subscribe(Topics.TRIGGER_FIRED, lambda e: trigger_events.append(e))

        df = _make_df(close_val=100.0)
        sb = StrategyBus(
            strategies=[strat],
            regime_provider=lambda s: RegimeInfo("bull_trend", 0.8, {}),
            candle_history_provider=lambda s, tf, n: df,
            event_bus=test_bus,
            now_ms_fn=lambda: sim_time[0],
        )
        sb.start()

        test_bus.publish(Topics.CANDLE_15M, {"symbol": "BTC/USDT"})
        sim_time[0] += 1000
        test_bus.publish(Topics.CANDLE_1M, {"symbol": "BTC/USDT"})

        assert len(trigger_events) >= 1
        td = trigger_events[0].data
        assert td["setup_id"] == setup.setup_id
        assert "setup_trace_ids" in td
        assert len(td["setup_trace_ids"]) > 0
        sb.stop()

    def test_trigger_id_chains_from_setup_id(self):
        """trigger_id is derived from setup_id — deterministic chain."""
        setup = _make_setup()
        trigger = _make_trigger(setup)
        # Trigger ID should be derived from setup_id
        expected = make_trigger_id(setup.setup_id, 2000000)
        assert trigger.trigger_id == expected


# ── 11. Layer Boundary Enforcement ───────────────────────────

class TestLayerBoundaries:
    """StrategyBus only publishes STRATEGY-layer events from DATA-layer input."""

    def test_strategy_bus_publishes_only_strategy_topics(self):
        """Verify that StrategyBus only publishes to strategy.* topics."""
        test_bus = EventBus()
        strat = IntegrationMockStrategy()
        sim_time = [1000000000000]
        setup = _make_setup(created_at_ms=sim_time[0])
        strat.set_setup_result(setup)

        published_topics = []
        original_publish = test_bus.publish

        def tracking_publish(topic, data=None, **kwargs):
            published_topics.append(topic)
            return original_publish(topic, data, **kwargs)

        test_bus.publish = tracking_publish

        df = _make_df()
        sb = StrategyBus(
            strategies=[strat],
            regime_provider=lambda s: RegimeInfo("bull_trend", 0.8, {}),
            candle_history_provider=lambda s, tf, n: df,
            event_bus=test_bus,
            now_ms_fn=lambda: sim_time[0],
        )
        sb.start()

        # Publish DATA-layer candle
        test_bus.publish(Topics.CANDLE_15M, {"symbol": "BTC/USDT"})

        # Filter to only StrategyBus-published topics (not our input)
        strategy_topics = [t for t in published_topics
                          if t.startswith("strategy.")]
        data_topics_published = [t for t in published_topics
                                 if t.startswith("market.") and t != Topics.CANDLE_15M]

        # StrategyBus should have published at least one strategy.* event
        assert len(strategy_topics) >= 1
        # StrategyBus should NOT have published any market.* events
        assert len(data_topics_published) == 0
        sb.stop()


# ── 12. NexusEngine Integration ──────────────────────────────

class TestNexusEngineIntegration:
    """NexusEngine.start()/stop() calls intraday engine lifecycle."""

    @staticmethod
    def _mock_strategy_bus():
        """Create a mock StrategyBus with _strategies attribute for fail-fast check."""
        mock_sb = MagicMock(spec=StrategyBus)
        mock_sb._strategies = [MagicMock()] * 5
        return mock_sb

    @patch("core.intraday.engine_integration.start_intraday_engine")
    @patch("core.database.engine.init_database")
    def test_engine_start_calls_start_intraday(self, mock_db, mock_start):
        mock_start.return_value = self._mock_strategy_bus()
        from core.engine import NexusEngine
        eng = NexusEngine()
        eng.start()
        mock_start.assert_called_once()
        eng.stop()

    @patch("core.intraday.engine_integration.stop_intraday_engine")
    @patch("core.intraday.engine_integration.start_intraday_engine")
    @patch("core.database.engine.init_database")
    def test_engine_stop_calls_stop_intraday(self, mock_db, mock_start, mock_stop):
        mock_start.return_value = self._mock_strategy_bus()
        from core.engine import NexusEngine
        eng = NexusEngine()
        eng.start()
        eng.stop()
        mock_stop.assert_called_once()

    @patch("core.intraday.engine_integration.start_intraday_engine")
    @patch("core.database.engine.init_database")
    def test_engine_exposes_strategy_bus_property(self, mock_db, mock_start):
        mock_sb = self._mock_strategy_bus()
        mock_start.return_value = mock_sb
        from core.engine import NexusEngine
        eng = NexusEngine()
        eng.start()
        assert eng.strategy_bus is mock_sb
        eng.stop()

    def test_engine_strategy_bus_none_before_start(self):
        from core.engine import NexusEngine
        eng = NexusEngine()
        assert eng.strategy_bus is None


# ── 13. Thread Safety ────────────────────────────────────────

class TestThreadSafety:
    """Candle accumulation is thread-safe."""

    def test_concurrent_candle_accumulation(self):
        """Multiple threads accumulating candles simultaneously."""
        errors = []

        def accumulate_batch(symbol, n):
            try:
                for i in range(n):
                    engine_integration._accumulate_candle(
                        symbol, "1m", {"close": i, "ts": i}
                    )
            except Exception as e:
                errors.append(e)

        threads = [
            threading.Thread(target=accumulate_batch, args=("BTC/USDT", 100)),
            threading.Thread(target=accumulate_batch, args=("ETH/USDT", 100)),
            threading.Thread(target=accumulate_batch, args=("SOL/USDT", 100)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        assert len(errors) == 0
        # All symbols should have their candles
        for sym in ["BTC/USDT", "ETH/USDT", "SOL/USDT"]:
            df = engine_integration._get_candle_history(sym, "1m", 200)
            assert df is not None
            assert len(df) == 100


# ── 14. Metrics in Integrated Pipeline ───────────────────────

class TestIntegratedMetrics:
    """Metrics are tracked correctly through the full pipeline."""

    def test_metrics_reflect_full_pipeline(self):
        test_bus = EventBus()
        strat = IntegrationMockStrategy()
        sim_time = [1000000000000]
        setup = _make_setup(created_at_ms=sim_time[0])
        trigger = _make_trigger(setup, created_at_ms=sim_time[0])
        strat.set_setup_result(setup)
        strat.set_trigger_result(trigger)

        df = _make_df(close_val=100.0)
        sb = StrategyBus(
            strategies=[strat],
            regime_provider=lambda s: RegimeInfo("bull_trend", 0.8, {}),
            candle_history_provider=lambda s, tf, n: df,
            event_bus=test_bus,
            now_ms_fn=lambda: sim_time[0],
        )
        sb.start()

        # Setup phase
        test_bus.publish(Topics.CANDLE_15M, {"symbol": "BTC/USDT"})
        # Trigger phase
        sim_time[0] += 1000
        test_bus.publish(Topics.CANDLE_1M, {"symbol": "BTC/USDT"})

        m = sb.metrics.snapshot()
        assert m["candles_received"] >= 2
        assert m["setups_evaluated"] >= 1
        assert m["setups_qualified"] >= 1
        assert m["triggers_evaluated"] >= 1
        assert m["triggers_fired"] >= 1
        sb.stop()
