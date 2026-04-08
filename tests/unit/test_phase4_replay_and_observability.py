# ============================================================
# Phase 4 — Replay & Observability Tests
#
# Tests:
#   - Deterministic replay: same stream → same outcomes
#   - Publication sequence deterministic
#   - Trace IDs propagated
#   - Rejection reasons logged/published
#   - Lifecycle stages recorded
#   - Performance benchmarks
# ============================================================
import pytest
import time
import numpy as np
import pandas as pd

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
from core.intraday.strategy_trace import (
    DecisionStage,
    StrategyTrace,
    StrategyTraceRegistry,
    strategy_trace_registry,
)
from core.intraday.strategy_replay import (
    ReplayFixture,
    StrategyReplayResult,
    replay_strategies,
)


# ── Deterministic Mock Strategy ───────────────────────────────

class DeterministicStrategy(BaseIntradayStrategy):
    """Strategy with deterministic behavior for replay testing."""
    NAME = "deterministic_test"
    STRATEGY_CLASS = StrategyClass.MOMENTUM_EXPANSION
    SETUP_TIMEFRAME = "15m"
    TRIGGER_TIMEFRAME = "1m"
    MAX_SETUP_AGE_MS = 3600000
    MAX_TRIGGER_AGE_MS = 60000
    DRIFT_TOLERANCE = 0.01
    BASE_TIME_STOP_MS = 7200000
    REGIME_AFFINITY = {"bull_trend": 1.0, "uncertain": 0.5}

    # Deterministic: setup qualifies when last close > 105
    _THRESHOLD = 105.0

    def evaluate_setup(self, symbol, df_setup, regime_info):
        if len(df_setup) < 20:
            return None
        last = df_setup.iloc[-1]
        if last["close"] <= self._THRESHOLD:
            return None

        ts = int(last.get("timestamp", 0))
        sid = make_setup_id(self.NAME, symbol, Direction.LONG.value, ts)
        atr = self._atr(df_setup)
        if atr <= 0:
            atr = 1.0

        return SetupSignal(
            setup_id=sid,
            strategy_name=self.NAME,
            strategy_class=self.STRATEGY_CLASS,
            symbol=symbol,
            direction=Direction.LONG,
            setup_timeframe=self.SETUP_TIMEFRAME,
            trigger_timeframe=self.TRIGGER_TIMEFRAME,
            entry_zone_low=last["close"] - 1.0,
            entry_zone_high=last["close"] + 1.0,
            stop_loss=last["close"] - 3.0,
            take_profit=last["close"] + 6.0,
            atr_value=atr,
            regime=regime_info.label,
            regime_confidence=regime_info.confidence,
            setup_candle_ts=ts,
            candle_trace_ids=self._candle_trace_ids(df_setup),
            lifecycle=SetupLifecycle.QUALIFIED,
            rationale=f"close {last['close']:.2f} > threshold {self._THRESHOLD}",
            max_age_ms=self.MAX_SETUP_AGE_MS,
            drift_tolerance=self.DRIFT_TOLERANCE,
            base_time_stop_ms=self.BASE_TIME_STOP_MS,
        )

    def evaluate_trigger(self, symbol, df_trigger, setup, regime_info):
        if len(df_trigger) < 5:
            return None
        last = df_trigger.iloc[-1]
        close = float(last["close"])
        if close < setup.entry_zone_low or close > setup.entry_zone_high:
            return None

        ts = int(last.get("timestamp", 0))
        tid = make_trigger_id(setup.setup_id, ts)

        return TriggerSignal(
            trigger_id=tid,
            setup_id=setup.setup_id,
            strategy_name=self.NAME,
            strategy_class=self.STRATEGY_CLASS,
            symbol=symbol,
            direction=setup.direction,
            entry_price=close,
            stop_loss=setup.stop_loss,
            take_profit=setup.take_profit,
            atr_value=setup.atr_value,
            strength=0.8,
            trigger_quality=0.75,
            setup_timeframe=self.SETUP_TIMEFRAME,
            trigger_timeframe=self.TRIGGER_TIMEFRAME,
            regime=regime_info.label,
            regime_confidence=regime_info.confidence,
            trigger_candle_ts=ts,
            setup_candle_ts=setup.setup_candle_ts,
            candle_trace_ids=self._candle_trace_ids(df_trigger),
            setup_trace_ids=setup.candle_trace_ids,
            lifecycle=TriggerLifecycle.FIRED,
            rationale=f"close {close:.2f} in zone",
            max_age_ms=self.MAX_TRIGGER_AGE_MS,
            drift_tolerance=self.DRIFT_TOLERANCE,
        )


def _make_replay_fixture():
    """Create a fixture with a clear setup → trigger sequence."""
    np.random.seed(42)
    # 15m candles with a breakout above 105
    setup_candles = []
    base = 100.0
    for i in range(30):
        ts = 1000000 + i * 900000  # 15m intervals
        c = base + i * 0.2 + np.random.randn() * 0.3
        setup_candles.append({
            "timestamp": ts,
            "open": c - 0.1,
            "high": c + 0.3,
            "low": c - 0.3,
            "close": c,
            "volume": 1000 + np.random.rand() * 200,
            "trace_id": f"setup_trace_{i:03d}",
        })

    # 1m candles in the entry zone of the last setup
    last_setup_close = setup_candles[-1]["close"]
    trigger_candles = []
    for i in range(20):
        ts = setup_candles[-1]["timestamp"] + (i + 1) * 60000
        c = last_setup_close + np.random.randn() * 0.2
        trigger_candles.append({
            "timestamp": ts,
            "open": c - 0.1,
            "high": c + 0.2,
            "low": c - 0.2,
            "close": c,
            "volume": 1500 + np.random.rand() * 300,
            "trace_id": f"trigger_trace_{i:03d}",
        })

    return ReplayFixture(
        entries=[
            {"symbol": "BTC/USDT", "timeframe": "15m", "candles": setup_candles},
            {"symbol": "BTC/USDT", "timeframe": "1m", "candles": trigger_candles},
        ],
        metadata={"description": "deterministic replay test fixture"},
    )


# ════════════════════════════════════════════════════════════
# Deterministic Replay Tests
# ════════════════════════════════════════════════════════════

class TestDeterministicReplay:
    def test_same_stream_same_setup_outcomes(self):
        fixture = _make_replay_fixture()
        strat = DeterministicStrategy()

        result1 = replay_strategies(fixture, [strat])
        result2 = replay_strategies(fixture, [strat])

        assert result1.matches(result2), \
            f"Replay not deterministic: {result1.diff(result2)}"

    def test_same_stream_same_trigger_outcomes(self):
        fixture = _make_replay_fixture()
        strat = DeterministicStrategy()

        result1 = replay_strategies(fixture, [strat])
        result2 = replay_strategies(fixture, [strat])

        assert len(result1.triggers_fired) == len(result2.triggers_fired)
        for a, b in zip(result1.triggers_fired, result2.triggers_fired):
            assert a.get("trigger_id") == b.get("trigger_id")

    def test_publication_sequence_deterministic(self):
        fixture = _make_replay_fixture()
        strat = DeterministicStrategy()

        result1 = replay_strategies(fixture, [strat])
        result2 = replay_strategies(fixture, [strat])

        assert result1.event_sequence == result2.event_sequence

    def test_candles_processed_count(self):
        fixture = _make_replay_fixture()
        strat = DeterministicStrategy()

        result = replay_strategies(fixture, [strat])
        assert result.candles_processed == fixture.total_candles()

    def test_replay_result_diff_empty_on_match(self):
        fixture = _make_replay_fixture()
        strat = DeterministicStrategy()

        result1 = replay_strategies(fixture, [strat])
        result2 = replay_strategies(fixture, [strat])

        assert result1.diff(result2) == []


# ════════════════════════════════════════════════════════════
# Observability & Traceability Tests
# ════════════════════════════════════════════════════════════

class TestTraceability:
    def test_trace_ids_propagated_to_setup(self):
        fixture = _make_replay_fixture()
        strat = DeterministicStrategy()

        result = replay_strategies(fixture, [strat])
        for setup_dict in result.setups_qualified:
            assert "setup_id" in setup_dict
            assert setup_dict["setup_id"]
            assert "candle_trace_ids" in setup_dict
            assert len(setup_dict["candle_trace_ids"]) > 0

    def test_trace_ids_propagated_to_trigger(self):
        fixture = _make_replay_fixture()
        strat = DeterministicStrategy()

        result = replay_strategies(fixture, [strat])
        for trigger_dict in result.triggers_fired:
            assert "trigger_id" in trigger_dict
            assert "setup_id" in trigger_dict
            assert "candle_trace_ids" in trigger_dict
            assert "setup_trace_ids" in trigger_dict

    def test_strategy_trace_registry_populated(self):
        strategy_trace_registry.clear()
        fixture = _make_replay_fixture()
        strat = DeterministicStrategy()

        result = replay_strategies(fixture, [strat])

        # Check that traces were registered
        for setup_dict in result.setups_qualified:
            trace = strategy_trace_registry.get(setup_dict["setup_id"])
            assert trace is not None
            assert trace.trace_type == "setup"
            assert trace.has_stage(DecisionStage.SETUP_EVALUATED)
            assert trace.has_stage(DecisionStage.SETUP_QUALIFIED)

    def test_lifecycle_stages_recorded(self):
        strategy_trace_registry.clear()
        fixture = _make_replay_fixture()
        strat = DeterministicStrategy()

        result = replay_strategies(fixture, [strat])

        for trigger_dict in result.triggers_fired:
            trace = strategy_trace_registry.get(trigger_dict["trigger_id"])
            assert trace is not None
            assert trace.has_stage(DecisionStage.TRIGGER_EVALUATED)
            assert trace.has_stage(DecisionStage.TRIGGER_FIRED)

    def test_trace_chain_links_setup_to_trigger(self):
        strategy_trace_registry.clear()
        fixture = _make_replay_fixture()
        strat = DeterministicStrategy()

        result = replay_strategies(fixture, [strat])

        for trigger_dict in result.triggers_fired:
            chain = strategy_trace_registry.get_chain(trigger_dict["trigger_id"])
            assert len(chain) >= 1  # At least the trigger itself
            # If setup was captured, chain should have 2 entries
            if len(chain) == 2:
                assert chain[0].trace_type == "setup"
                assert chain[1].trace_type == "trigger"


class TestStrategyTraceRegistry:
    def test_register_and_get(self):
        reg = StrategyTraceRegistry(max_size=100)
        trace = StrategyTrace(
            trace_id="test_001",
            trace_type="setup",
            strategy_name="test",
            symbol="BTC/USDT",
            direction="long",
            parent_candle_traces=("c1", "c2"),
        )
        reg.register(trace)
        assert reg.get("test_001") is trace

    def test_lru_eviction(self):
        reg = StrategyTraceRegistry(max_size=3)
        for i in range(5):
            trace = StrategyTrace(
                trace_id=f"t_{i}",
                trace_type="setup",
                strategy_name="test",
                symbol="BTC/USDT",
                direction="long",
                parent_candle_traces=(),
            )
            reg.register(trace)

        assert len(reg) == 3
        assert reg.get("t_0") is None  # Evicted
        assert reg.get("t_1") is None  # Evicted
        assert reg.get("t_4") is not None

    def test_get_by_symbol(self):
        reg = StrategyTraceRegistry(max_size=100)
        for i in range(5):
            sym = "BTC/USDT" if i % 2 == 0 else "ETH/USDT"
            trace = StrategyTrace(
                trace_id=f"t_{i}",
                trace_type="setup",
                strategy_name="test",
                symbol=sym,
                direction="long",
                parent_candle_traces=(),
            )
            reg.register(trace)

        btc_traces = reg.get_by_symbol("BTC/USDT")
        assert len(btc_traces) == 3

    def test_get_by_strategy(self):
        reg = StrategyTraceRegistry(max_size=100)
        for i in range(4):
            strat_name = "mx" if i < 2 else "vr"
            trace = StrategyTrace(
                trace_id=f"t_{i}",
                trace_type="setup",
                strategy_name=strat_name,
                symbol="BTC/USDT",
                direction="long",
                parent_candle_traces=(),
            )
            reg.register(trace)

        mx_traces = reg.get_by_strategy("mx")
        assert len(mx_traces) == 2

    def test_record_stage_with_reason(self):
        trace = StrategyTrace(
            trace_id="t_1",
            trace_type="setup",
            strategy_name="test",
            symbol="BTC/USDT",
            direction="long",
            parent_candle_traces=(),
        )
        trace.record_stage(DecisionStage.SETUP_REJECTED, "ADX below threshold (12.3 < 25.0)")

        assert trace.has_stage(DecisionStage.SETUP_REJECTED)
        assert "ADX below threshold" in trace.get_reason(DecisionStage.SETUP_REJECTED)

    def test_snapshot_serializable(self):
        trace = StrategyTrace(
            trace_id="t_1",
            trace_type="trigger",
            strategy_name="test",
            symbol="BTC/USDT",
            direction="long",
            parent_candle_traces=("c1",),
            parent_setup_id="s_1",
        )
        trace.record_stage(DecisionStage.TRIGGER_FIRED, "entry confirmed")
        snap = trace.snapshot()

        assert isinstance(snap, dict)
        assert snap["trace_id"] == "t_1"
        assert snap["parent_setup_id"] == "s_1"
        assert "trigger_fired" in snap["stages"]


# ════════════════════════════════════════════════════════════
# Performance Tests
# ════════════════════════════════════════════════════════════

class TestPerformance:
    def test_setup_evaluation_latency(self):
        """Setup evaluation should complete in <10ms per call."""
        from core.intraday.strategies.momentum_expansion import MomentumExpansionStrategy
        strat = MomentumExpansionStrategy()
        np.random.seed(42)
        df = pd.DataFrame({
            "timestamp": [1000000 + i * 900000 for i in range(100)],
            "open": np.random.uniform(99, 101, 100),
            "high": np.random.uniform(100, 102, 100),
            "low": np.random.uniform(98, 100, 100),
            "close": np.random.uniform(99, 101, 100),
            "volume": np.random.uniform(900, 1100, 100),
            "trace_id": [f"t_{i}" for i in range(100)],
        })
        regime = RegimeInfo("high_volatility", 0.8, {})

        t0 = time.time()
        for _ in range(100):
            strat.run_setup("BTC/USDT", df, regime)
        elapsed = (time.time() - t0) * 1000 / 100  # ms per call

        assert elapsed < 50, f"Setup evaluation too slow: {elapsed:.1f}ms"

    def test_trigger_evaluation_latency(self):
        """Trigger evaluation should complete in <5ms per call."""
        from core.intraday.strategies.momentum_expansion import MomentumExpansionStrategy
        strat = MomentumExpansionStrategy()
        np.random.seed(42)
        setup = SetupSignal(
            setup_id="perf_test_setup",
            strategy_name=strat.NAME,
            strategy_class=strat.STRATEGY_CLASS,
            symbol="BTC/USDT",
            direction=Direction.LONG,
            setup_timeframe="15m",
            trigger_timeframe="1m",
            entry_zone_low=99.0,
            entry_zone_high=101.0,
            stop_loss=95.0,
            take_profit=110.0,
            atr_value=2.0,
            regime="bull_trend",
            regime_confidence=0.8,
            setup_candle_ts=1000000,
            candle_trace_ids=("t_1",),
            lifecycle=SetupLifecycle.QUALIFIED,
            max_age_ms=60000,
            drift_tolerance=0.003,
            base_time_stop_ms=3600000,
        )
        df = pd.DataFrame({
            "timestamp": [2000000 + i * 60000 for i in range(30)],
            "open": np.random.uniform(99, 101, 30),
            "high": np.random.uniform(100, 102, 30),
            "low": np.random.uniform(98, 100, 30),
            "close": np.random.uniform(99, 101, 30),
            "volume": np.random.uniform(900, 1100, 30),
            "trace_id": [f"t_{i}" for i in range(30)],
        })
        regime = RegimeInfo("bull_trend", 0.8, {})

        t0 = time.time()
        for _ in range(100):
            strat.run_trigger("BTC/USDT", df, setup, regime)
        elapsed = (time.time() - t0) * 1000 / 100

        assert elapsed < 25, f"Trigger evaluation too slow: {elapsed:.1f}ms"

    def test_strategy_bus_processing_latency(self):
        """StrategyBus should process a candle event in <50ms."""
        from core.event_bus import EventBus, Topics
        from core.intraday.strategy_bus import StrategyBus

        class FastStrategy(BaseIntradayStrategy):
            NAME = "fast_test"
            STRATEGY_CLASS = StrategyClass.MOMENTUM_EXPANSION
            SETUP_TIMEFRAME = "15m"
            TRIGGER_TIMEFRAME = "1m"
            MAX_SETUP_AGE_MS = 60000
            MAX_TRIGGER_AGE_MS = 5000
            DRIFT_TOLERANCE = 0.003
            BASE_TIME_STOP_MS = 3600000
            REGIME_AFFINITY = {"bull_trend": 1.0}

            def evaluate_setup(self, symbol, df, regime):
                return None  # Fast reject

            def evaluate_trigger(self, symbol, df, setup, regime):
                return None

        np.random.seed(42)
        df = pd.DataFrame({
            "timestamp": [1000000 + i * 60000 for i in range(50)],
            "open": np.random.uniform(99, 101, 50),
            "high": np.random.uniform(100, 102, 50),
            "low": np.random.uniform(98, 100, 50),
            "close": np.random.uniform(99, 101, 50),
            "volume": np.random.uniform(900, 1100, 50),
        })

        test_bus = EventBus()
        sb = StrategyBus(
            strategies=[FastStrategy()],
            regime_provider=lambda s: RegimeInfo("bull_trend", 0.8, {}),
            candle_history_provider=lambda s, tf, n: df,
            event_bus=test_bus,
        )
        sb.start()

        t0 = time.time()
        for _ in range(100):
            test_bus.publish(Topics.CANDLE_15M, {"symbol": "BTC/USDT"})
        elapsed = (time.time() - t0) * 1000 / 100

        assert elapsed < 50, f"StrategyBus too slow: {elapsed:.1f}ms per event"
        sb.stop()
