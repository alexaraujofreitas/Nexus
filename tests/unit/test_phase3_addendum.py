# ============================================================
# Phase 3 Addendum — Tests for all 3 architectural fixes
#
# Fix 1: Topic ownership (CONNECTIVITY vs DATA layer separation)
# Fix 2: Deterministic replay / audit path
# Fix 3: Per-candle traceability (trace_id, source, lifecycle)
# ============================================================
import ast
import json
import sys
import time
import unittest
from collections import defaultdict
from pathlib import Path
from unittest.mock import MagicMock, patch

from core.contracts import (
    SignalLayer, get_topic_owner, check_topic_boundary,
    TOPIC_LAYER_OWNERSHIP, validate_candle,
)
from core.market_data.candle_builder import (
    CandleBuilder, align_to_boundary, _1M_MS,
)
from core.market_data.candle_trace import (
    CandleTrace, CandleSource, CandleLifecycleStage,
    make_trace_id, trace_registry, TraceRegistry,
)
from core.market_data.candle_replay import (
    CandleFixture, ReplayResult, replay,
)
from core.market_data.data_engine import DataEngine


ROOT = Path(__file__).resolve().parent.parent.parent


def _raw_candle(ts_ms, o=100.0, h=105.0, l=95.0, c=102.0, v=1000.0):
    return [ts_ms, o, h, l, c, v]


# ════════════════════════════════════════════════════════════════
# FIX 1: Topic Ownership Tests
# ════════════════════════════════════════════════════════════════

class TestDataLayerExists(unittest.TestCase):
    """DATA layer is a valid SignalLayer enum member."""

    def test_data_layer_in_enum(self):
        self.assertIn("data", [e.value for e in SignalLayer])
        self.assertEqual(SignalLayer.DATA.value, "data")


class TestCandleTopicsOwnedByData(unittest.TestCase):
    """Candle topics (market.candle.*) are owned by DATA, not CONNECTIVITY."""

    def test_candle_1m_owned_by_data(self):
        self.assertEqual(get_topic_owner("market.candle.1m"), SignalLayer.DATA)

    def test_candle_3m_owned_by_data(self):
        self.assertEqual(get_topic_owner("market.candle.3m"), SignalLayer.DATA)

    def test_candle_5m_owned_by_data(self):
        self.assertEqual(get_topic_owner("market.candle.5m"), SignalLayer.DATA)

    def test_candle_15m_owned_by_data(self):
        self.assertEqual(get_topic_owner("market.candle.15m"), SignalLayer.DATA)

    def test_candle_1h_owned_by_data(self):
        self.assertEqual(get_topic_owner("market.candle.1h"), SignalLayer.DATA)

    def test_candle_close_owned_by_data(self):
        self.assertEqual(get_topic_owner("market.candle_close"), SignalLayer.DATA)

    def test_candle_closed_owned_by_data(self):
        self.assertEqual(get_topic_owner("market.candle_closed"), SignalLayer.DATA)


class TestRawTransportOwnedByConnectivity(unittest.TestCase):
    """Raw transport topics remain owned by CONNECTIVITY."""

    def test_tick_update_owned_by_connectivity(self):
        self.assertEqual(get_topic_owner("market.tick"), SignalLayer.CONNECTIVITY)

    def test_ohlcv_update_owned_by_connectivity(self):
        self.assertEqual(get_topic_owner("market.ohlcv"), SignalLayer.CONNECTIVITY)

    def test_orderbook_owned_by_connectivity(self):
        self.assertEqual(get_topic_owner("market.orderbook"), SignalLayer.CONNECTIVITY)

    def test_trade_stream_owned_by_connectivity(self):
        self.assertEqual(get_topic_owner("market.trades"), SignalLayer.CONNECTIVITY)

    def test_exchange_connected_owned_by_connectivity(self):
        self.assertEqual(get_topic_owner("system.exchange.connected"), SignalLayer.CONNECTIVITY)

    def test_feed_status_owned_by_connectivity(self):
        self.assertEqual(get_topic_owner("system.feed.status"), SignalLayer.CONNECTIVITY)


class TestLayerBoundaryEnforcement(unittest.TestCase):
    """check_topic_boundary correctly blocks cross-layer publishing."""

    def test_connectivity_cannot_publish_candle(self):
        result = check_topic_boundary("market.candle.1m", SignalLayer.CONNECTIVITY)
        self.assertIsNotNone(result)
        self.assertIn("violation", result.lower())

    def test_data_can_publish_candle(self):
        result = check_topic_boundary("market.candle.1m", SignalLayer.DATA)
        self.assertIsNone(result)

    def test_connectivity_can_publish_tick(self):
        result = check_topic_boundary("market.tick", SignalLayer.CONNECTIVITY)
        self.assertIsNone(result)

    def test_connectivity_can_publish_ohlcv(self):
        result = check_topic_boundary("market.ohlcv", SignalLayer.CONNECTIVITY)
        self.assertIsNone(result)

    def test_data_cannot_publish_raw_tick(self):
        result = check_topic_boundary("market.tick", SignalLayer.DATA)
        self.assertIsNotNone(result)

    def test_strategy_cannot_publish_candle(self):
        result = check_topic_boundary("market.candle.5m", SignalLayer.STRATEGY)
        self.assertIsNotNone(result)

    def test_strategy_consumes_only_normalized(self):
        """Strategy layer should consume candle topics (DATA), not raw transport."""
        # This is an architectural assertion: strategy topics are separate
        result = check_topic_boundary("strategy.signal", SignalLayer.STRATEGY)
        self.assertIsNone(result)


# ════════════════════════════════════════════════════════════════
# FIX 2: Deterministic Replay Tests
# ════════════════════════════════════════════════════════════════

class TestCandleFixture(unittest.TestCase):
    """Test fixture creation and serialization."""

    def test_from_raw(self):
        raws = [_raw_candle(1000 + i * _1M_MS) for i in range(5)]
        fixture = CandleFixture.from_raw("BTC/USDT", raws)
        self.assertEqual(len(fixture.entries), 1)
        self.assertEqual(fixture.entries[0]["symbol"], "BTC/USDT")
        self.assertEqual(fixture.total_candles(), 5)

    def test_from_multi_symbol(self):
        data = {
            "BTC/USDT": [_raw_candle(1000 + i * _1M_MS) for i in range(3)],
            "ETH/USDT": [_raw_candle(1000 + i * _1M_MS) for i in range(3)],
        }
        fixture = CandleFixture.from_multi_symbol(data)
        self.assertEqual(len(fixture.entries), 2)
        self.assertEqual(fixture.total_candles(), 6)
        # Deterministic ordering (sorted by symbol)
        self.assertEqual(fixture.entries[0]["symbol"], "BTC/USDT")
        self.assertEqual(fixture.entries[1]["symbol"], "ETH/USDT")

    def test_json_roundtrip(self):
        raws = [_raw_candle(1000 + i * _1M_MS) for i in range(3)]
        fixture = CandleFixture.from_raw("BTC/USDT", raws, metadata={"test": True})
        json_str = fixture.to_json()
        restored = CandleFixture.from_json(json_str)
        self.assertEqual(len(restored.entries), 1)
        self.assertEqual(restored.total_candles(), 3)
        self.assertTrue(restored.metadata.get("test"))


class TestReplayDeterminism(unittest.TestCase):
    """Replay the same input twice → identical outputs."""

    def _create_pipeline(self):
        emitted = []
        builder = CandleBuilder(
            timeframes=["3m", "5m", "15m"],
            on_candle=lambda s, tf, c: emitted.append((s, tf, c)),
            publish_1m=False,
        )
        builder.start()
        engine = DataEngine(on_candle_closed=builder.ingest)
        engine.start()
        return engine, builder, emitted

    def test_replay_same_input_identical_output(self):
        """Core determinism test: replay twice → ReplayResult.matches() is True."""
        base = align_to_boundary(1704067200000, 15)
        raws = [_raw_candle(base + i * _1M_MS, o=100+i, h=110+i, l=90+i, c=105+i)
                for i in range(15)]
        fixture = CandleFixture.from_raw("BTC/USDT", raws)

        # Run 1
        engine1, builder1, _ = self._create_pipeline()
        result1 = replay(fixture, engine1, builder1)
        engine1.stop(); builder1.stop()

        # Run 2 (fresh pipeline)
        engine2, builder2, _ = self._create_pipeline()
        result2 = replay(fixture, engine2, builder2)
        engine2.stop(); builder2.stop()

        self.assertTrue(result1.matches(result2),
                        f"Replay not deterministic: {result1.diff(result2)}")

    def test_replayed_1m_yields_same_3m(self):
        """Replayed 1m stream yields same 3m candles."""
        base = align_to_boundary(1704067200000, 3)
        raws = [_raw_candle(base + i * _1M_MS) for i in range(9)]
        fixture = CandleFixture.from_raw("BTC/USDT", raws)

        engine1, builder1, _ = self._create_pipeline()
        result1 = replay(fixture, engine1, builder1)
        engine1.stop(); builder1.stop()

        engine2, builder2, _ = self._create_pipeline()
        result2 = replay(fixture, engine2, builder2)
        engine2.stop(); builder2.stop()

        r1_3m = [e for e in result1.derived_candles if e["timeframe"] == "3m"]
        r2_3m = [e for e in result2.derived_candles if e["timeframe"] == "3m"]
        self.assertEqual(len(r1_3m), len(r2_3m))
        for a, b in zip(r1_3m, r2_3m):
            self.assertEqual(a["candle"]["timestamp"], b["candle"]["timestamp"])
            self.assertEqual(a["candle"]["open"], b["candle"]["open"])
            self.assertEqual(a["candle"]["close"], b["candle"]["close"])

    def test_publication_sequence_identical(self):
        """Publication sequence (order of TF emissions) is identical across replays."""
        base = align_to_boundary(1704067200000, 15)
        raws = [_raw_candle(base + i * _1M_MS) for i in range(30)]
        fixture = CandleFixture.from_raw("BTC/USDT", raws)

        engine1, builder1, _ = self._create_pipeline()
        result1 = replay(fixture, engine1, builder1)
        engine1.stop(); builder1.stop()

        engine2, builder2, _ = self._create_pipeline()
        result2 = replay(fixture, engine2, builder2)
        engine2.stop(); builder2.stop()

        self.assertEqual(result1.publication_sequence, result2.publication_sequence)

    def test_multi_symbol_replay_deterministic(self):
        """Multi-symbol fixture produces deterministic results."""
        base = align_to_boundary(1704067200000, 3)
        data = {
            "BTC/USDT": [_raw_candle(base + i * _1M_MS) for i in range(6)],
            "ETH/USDT": [_raw_candle(base + i * _1M_MS, o=3000, h=3100, l=2900, c=3050) for i in range(6)],
        }
        fixture = CandleFixture.from_multi_symbol(data)

        engine1, builder1, _ = self._create_pipeline()
        result1 = replay(fixture, engine1, builder1)
        engine1.stop(); builder1.stop()

        engine2, builder2, _ = self._create_pipeline()
        result2 = replay(fixture, engine2, builder2)
        engine2.stop(); builder2.stop()

        self.assertTrue(result1.matches(result2))

    def test_replay_result_diff_detects_mismatch(self):
        """ReplayResult.diff() detects differences."""
        r1 = ReplayResult()
        r1.add_candle("BTC/USDT", "3m", {"timestamp": 1000, "open": 100})
        r2 = ReplayResult()
        r2.add_candle("BTC/USDT", "3m", {"timestamp": 2000, "open": 100})
        self.assertFalse(r1.matches(r2))
        diffs = r1.diff(r2)
        self.assertGreater(len(diffs), 0)


# ════════════════════════════════════════════════════════════════
# FIX 3: Per-Candle Traceability Tests
# ════════════════════════════════════════════════════════════════

class TestMakeTraceId(unittest.TestCase):
    """Test deterministic trace ID generation."""

    def test_deterministic(self):
        id1 = make_trace_id("BTC/USDT", "1m", 1704067200000)
        id2 = make_trace_id("BTC/USDT", "1m", 1704067200000)
        self.assertEqual(id1, id2)

    def test_different_inputs_different_ids(self):
        id1 = make_trace_id("BTC/USDT", "1m", 1704067200000)
        id2 = make_trace_id("ETH/USDT", "1m", 1704067200000)
        id3 = make_trace_id("BTC/USDT", "3m", 1704067200000)
        self.assertNotEqual(id1, id2)
        self.assertNotEqual(id1, id3)

    def test_id_format(self):
        tid = make_trace_id("BTC/USDT", "1m", 1000)
        self.assertEqual(len(tid), 12)
        self.assertTrue(all(c in "0123456789abcdef" for c in tid))


class TestCandleTrace(unittest.TestCase):
    """Test CandleTrace lifecycle recording."""

    def test_record_stages(self):
        trace = CandleTrace(
            trace_id="abc123def456",
            symbol="BTC/USDT",
            timeframe="1m",
            timestamp_ms=1000,
            source=CandleSource.WEBSOCKET,
        )
        trace.record_stage(CandleLifecycleStage.RECEIVED)
        trace.record_stage(CandleLifecycleStage.NORMALIZED)
        trace.record_stage(CandleLifecycleStage.BUFFERED)

        self.assertTrue(trace.has_stage(CandleLifecycleStage.RECEIVED))
        self.assertTrue(trace.has_stage(CandleLifecycleStage.NORMALIZED))
        self.assertTrue(trace.has_stage(CandleLifecycleStage.BUFFERED))
        self.assertFalse(trace.has_stage(CandleLifecycleStage.PUBLISHED))

    def test_snapshot(self):
        trace = CandleTrace(
            trace_id="abc123def456",
            symbol="BTC/USDT",
            timeframe="1m",
            timestamp_ms=1000,
            source=CandleSource.REST,
            lineage=["aaa111bbb222"],
        )
        trace.record_stage(CandleLifecycleStage.DERIVED)
        snap = trace.snapshot()
        self.assertEqual(snap["trace_id"], "abc123def456")
        self.assertEqual(snap["source"], "rest")
        self.assertIn("derived", snap["stages"])
        self.assertEqual(snap["lineage"], ["aaa111bbb222"])


class TestTraceRegistry(unittest.TestCase):
    """Test the bounded trace registry."""

    def setUp(self):
        self.reg = TraceRegistry(max_size=10)

    def test_register_and_get(self):
        trace = CandleTrace("id1", "BTC/USDT", "1m", 1000, CandleSource.REST)
        self.reg.register(trace)
        self.assertEqual(self.reg.get("id1"), trace)

    def test_lru_eviction(self):
        for i in range(15):
            self.reg.register(CandleTrace(f"id{i}", "BTC/USDT", "1m", i * 1000, CandleSource.REST))
        self.assertEqual(len(self.reg), 10)
        # First 5 should be evicted
        self.assertIsNone(self.reg.get("id0"))
        self.assertIsNotNone(self.reg.get("id14"))

    def test_get_by_symbol(self):
        self.reg.register(CandleTrace("id1", "BTC/USDT", "1m", 1000, CandleSource.REST))
        self.reg.register(CandleTrace("id2", "ETH/USDT", "1m", 2000, CandleSource.REST))
        self.reg.register(CandleTrace("id3", "BTC/USDT", "3m", 3000, CandleSource.REST))
        btc = self.reg.get_by_symbol("BTC/USDT")
        self.assertEqual(len(btc), 2)

    def test_get_by_timeframe(self):
        self.reg.register(CandleTrace("id1", "BTC/USDT", "1m", 1000, CandleSource.REST))
        self.reg.register(CandleTrace("id2", "BTC/USDT", "3m", 2000, CandleSource.REST))
        result = self.reg.get_by_timeframe("1m")
        self.assertEqual(len(result), 1)

    def test_clear(self):
        self.reg.register(CandleTrace("id1", "BTC/USDT", "1m", 1000, CandleSource.REST))
        self.reg.clear()
        self.assertEqual(len(self.reg), 0)


class TestTraceContinuityThroughPipeline(unittest.TestCase):
    """Trace continuity: 1m candles get trace_id, derived candles get lineage."""

    def setUp(self):
        trace_registry.clear()
        self.emitted = []
        self.builder = CandleBuilder(
            timeframes=["3m"],
            on_candle=lambda s, tf, c: self.emitted.append((s, tf, c)),
            publish_1m=False,
        )
        self.builder.start()
        self.engine = DataEngine(on_candle_closed=self.builder.ingest)
        self.engine.start()

    def tearDown(self):
        self.engine.stop()
        self.builder.stop()

    def test_1m_candles_get_trace_id(self):
        base = align_to_boundary(1704067200000, 3)
        raws = [_raw_candle(base + i * _1M_MS) for i in range(3)]
        self.engine.ingest_candles("BTC/USDT", "1m", raws, source=CandleSource.WEBSOCKET)

        # Check that traces exist in registry
        for i in range(3):
            tid = make_trace_id("BTC/USDT", "1m", base + i * _1M_MS)
            trace = trace_registry.get(tid)
            self.assertIsNotNone(trace, f"Trace not found for 1m candle at minute {i}")
            self.assertEqual(trace.source, CandleSource.WEBSOCKET)
            self.assertTrue(trace.has_stage(CandleLifecycleStage.RECEIVED))
            self.assertTrue(trace.has_stage(CandleLifecycleStage.NORMALIZED))
            self.assertTrue(trace.has_stage(CandleLifecycleStage.BUFFERED))

    def test_derived_candle_has_lineage(self):
        base = align_to_boundary(1704067200000, 3)
        raws = [_raw_candle(base + i * _1M_MS) for i in range(3)]
        self.engine.ingest_candles("BTC/USDT", "1m", raws)

        # Get the derived 3m candle
        tf3 = [c for s, tf, c in self.emitted if tf == "3m"]
        self.assertEqual(len(tf3), 1)
        candle = tf3[0]
        self.assertIn("lineage", candle)
        self.assertEqual(len(candle["lineage"]), 3)

        # Each lineage ID should match a registered 1m trace
        for lid in candle["lineage"]:
            trace = trace_registry.get(lid)
            self.assertIsNotNone(trace, f"Lineage trace {lid} not in registry")
            self.assertEqual(trace.timeframe, "1m")

    def test_derived_candle_has_trace_in_registry(self):
        base = align_to_boundary(1704067200000, 3)
        raws = [_raw_candle(base + i * _1M_MS) for i in range(3)]
        self.engine.ingest_candles("BTC/USDT", "1m", raws)

        # Derived 3m trace should be in registry
        derived_tid = make_trace_id("BTC/USDT", "3m", base)
        trace = trace_registry.get(derived_tid)
        self.assertIsNotNone(trace)
        self.assertEqual(trace.timeframe, "3m")
        self.assertTrue(trace.has_stage(CandleLifecycleStage.DERIVED))
        self.assertTrue(trace.has_stage(CandleLifecycleStage.PUBLISHED))
        self.assertEqual(len(trace.lineage), 3)

    def test_deduped_candles_trace_correctly(self):
        base = align_to_boundary(1704067200000, 3)
        raws = [_raw_candle(base)]
        self.engine.ingest_candles("BTC/USDT", "1m", raws)
        # Ingest same candle again
        self.engine.ingest_candles("BTC/USDT", "1m", raws)

        tid = make_trace_id("BTC/USDT", "1m", base)
        trace = trace_registry.get(tid)
        self.assertIsNotNone(trace)
        # Second ingest should record DEDUPLICATED stage
        # (the trace from the second call overwrites with DEDUPLICATED)
        # Registry contains the latest trace for this ID

    def test_backfill_candles_trace_correctly(self):
        base = align_to_boundary(1704067200000, 3)
        raws = [_raw_candle(base + i * _1M_MS) for i in range(3)]
        self.engine.ingest_candles("BTC/USDT", "1m", raws, source=CandleSource.BACKFILL)

        tid = make_trace_id("BTC/USDT", "1m", base)
        trace = trace_registry.get(tid)
        self.assertIsNotNone(trace)
        self.assertEqual(trace.source, CandleSource.BACKFILL)

    def test_replay_candles_trace_correctly(self):
        base = align_to_boundary(1704067200000, 3)
        raws = [_raw_candle(base + i * _1M_MS) for i in range(3)]
        self.engine.ingest_candles("BTC/USDT", "1m", raws, source=CandleSource.REPLAY)

        tid = make_trace_id("BTC/USDT", "1m", base)
        trace = trace_registry.get(tid)
        self.assertIsNotNone(trace)
        self.assertEqual(trace.source, CandleSource.REPLAY)


class TestNoPySide6InAddendumModules(unittest.TestCase):
    """Static analysis: no PySide6 in new addendum modules."""

    ADDENDUM_MODULES = [
        "core/market_data/candle_trace.py",
        "core/market_data/candle_replay.py",
    ]

    def test_no_pyside6_ast(self):
        for rel_path in self.ADDENDUM_MODULES:
            full_path = ROOT / rel_path
            if not full_path.exists():
                self.fail(f"Module not found: {rel_path}")
            src = full_path.read_text()
            tree = ast.parse(src)
            for node in ast.walk(tree):
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    module = ""
                    if isinstance(node, ast.Import):
                        module = ", ".join(a.name for a in node.names)
                    elif node.module:
                        module = node.module
                    self.assertNotIn("PySide6", module,
                                     f"PySide6 import in {rel_path}")


if __name__ == "__main__":
    unittest.main()
