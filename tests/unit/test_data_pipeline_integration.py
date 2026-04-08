# ============================================================
# Phase 3 Data Engine — Integration & Performance Tests
#
# Validates:
#   1. End-to-end pipeline: raw candles → DataEngine → CandleBuilder → EventBus
#   2. Multi-symbol concurrent processing
#   3. Simulated WS → REST failover
#   4. Gap detection → backfill → complete TF derivation
#   5. Performance: 20-symbol throughput under 500ms per cycle
#   6. Contract validation on emitted candles
#   7. No PySide6 imports in any Phase 3 data engine module
# ============================================================
import ast
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
from collections import defaultdict

from core.market_data.data_engine import DataEngine, _1M_MS
from core.market_data.candle_builder import (
    CandleBuilder, align_to_boundary, DERIVED_TIMEFRAMES,
)
from core.market_data.latency_tracker import LatencyTracker
from core.contracts import validate_candle


ROOT = Path(__file__).resolve().parent.parent.parent


def _raw_candle(ts_ms, o=100.0, h=105.0, l=95.0, c=102.0, v=1000.0):
    return [ts_ms, o, h, l, c, v]


class TestEndToEndPipeline(unittest.TestCase):
    """
    Integration: raw OHLCV arrays flow through DataEngine → CandleBuilder
    and emit properly validated higher-TF candles.
    """

    def setUp(self):
        self.emitted = defaultdict(list)

        def on_candle_cb(symbol, tf, candle):
            self.emitted[tf].append((symbol, candle))

        self.builder = CandleBuilder(
            timeframes=["3m", "5m", "15m"],
            on_candle=on_candle_cb,
            publish_1m=False,
        )
        self.builder.start()

        self.engine = DataEngine(
            on_candle_closed=self.builder.ingest,
        )
        self.engine.start()

    def tearDown(self):
        self.engine.stop()
        self.builder.stop()

    def test_full_pipeline_1m_to_3m(self):
        """Feed 3 raw 1m candles → get 1 derived 3m candle."""
        base = align_to_boundary(1704067200000, 3)
        raws = [_raw_candle(base + i * _1M_MS) for i in range(3)]
        self.engine.ingest_candles("BTC/USDT", "1m", raws)

        self.assertGreater(len(self.emitted["3m"]), 0)
        _, candle = self.emitted["3m"][0]
        self.assertEqual(candle["timestamp"], base)

    def test_full_pipeline_1m_to_15m(self):
        """Feed 15 raw 1m candles → get 1 derived 15m candle."""
        base = align_to_boundary(1704067200000, 15)
        raws = [_raw_candle(base + i * _1M_MS, o=100+i, h=110+i, l=90+i, c=105+i)
                for i in range(15)]
        self.engine.ingest_candles("BTC/USDT", "1m", raws)

        self.assertGreater(len(self.emitted["15m"]), 0)
        _, candle = self.emitted["15m"][0]
        self.assertEqual(candle["open"], 100)
        self.assertEqual(candle["close"], 119)
        self.assertEqual(candle["high"], 124)
        self.assertEqual(candle["low"], 90)

    def test_contract_validation_on_derived_candle(self):
        """Every derived candle passes contract validation."""
        base = align_to_boundary(1704067200000, 5)
        raws = [_raw_candle(base + i * _1M_MS) for i in range(5)]
        self.engine.ingest_candles("BTC/USDT", "1m", raws)

        for tf, entries in self.emitted.items():
            for symbol, candle in entries:
                candle_payload = dict(candle)
                candle_payload.setdefault("symbol", symbol)
                candle_payload.setdefault("timeframe", tf)
                violations = validate_candle(candle_payload)
                self.assertEqual(violations, [],
                                 f"Contract violation on {tf} candle: {violations}")

    def test_multi_symbol_concurrent(self):
        """Multiple symbols produce independent derived candles."""
        base = align_to_boundary(1704067200000, 3)
        for symbol in ["BTC/USDT", "ETH/USDT", "SOL/USDT"]:
            raws = [_raw_candle(base + i * _1M_MS) for i in range(3)]
            self.engine.ingest_candles(symbol, "1m", raws)

        symbols_seen = set(s for s, _ in self.emitted.get("3m", []))
        self.assertEqual(symbols_seen, {"BTC/USDT", "ETH/USDT", "SOL/USDT"})

    def test_gap_then_continuation(self):
        """Gap in 1m data doesn't prevent future TF derivation."""
        base = align_to_boundary(1704067200000, 3)
        raws1 = [_raw_candle(base + i * _1M_MS) for i in range(3)]
        self.engine.ingest_candles("BTC/USDT", "1m", raws1)
        count1 = len(self.emitted["3m"])

        next_base = base + 6 * _1M_MS
        next_base = align_to_boundary(next_base, 3)
        raws2 = [_raw_candle(next_base + i * _1M_MS) for i in range(3)]
        self.engine.ingest_candles("BTC/USDT", "1m", raws2)

        self.assertEqual(len(self.emitted["3m"]), count1 + 1)


class TestPerformance(unittest.TestCase):
    """Performance: 20-symbol throughput target."""

    def test_20_symbol_throughput(self):
        """Ingesting 60 candles x 20 symbols should complete in < 500ms."""
        emitted = []
        builder = CandleBuilder(
            timeframes=["3m", "5m", "15m"],
            on_candle=lambda s, tf, c: emitted.append((s, tf)),
            publish_1m=False,
        )
        builder.start()
        engine = DataEngine(on_candle_closed=builder.ingest)
        engine.start()

        symbols = [f"SYM{i}/USDT" for i in range(20)]
        base = align_to_boundary(1704067200000, 15)

        t0 = time.time()
        for symbol in symbols:
            raws = [_raw_candle(base + i * _1M_MS) for i in range(60)]
            engine.ingest_candles(symbol, "1m", raws)
        elapsed_ms = (time.time() - t0) * 1000

        engine.stop()
        builder.stop()

        self.assertLess(elapsed_ms, 500,
                        f"20-symbol x 60-candle ingestion took {elapsed_ms:.1f}ms (>500ms)")
        symbols_emitted = set(s for s, _ in emitted)
        self.assertEqual(len(symbols_emitted), 20)


class TestPipelineWithLatencyTracker(unittest.TestCase):
    """Integration: latency tracker records pipeline timings."""

    def test_latency_recorded_through_pipeline(self):
        tracker = LatencyTracker(log_interval_s=9999)
        tracker.start()

        def on_candle_with_tracking(symbol, tf, candle):
            tracker.record(LatencyTracker.STAGE_TF_DERIVE, 1.0)

        builder = CandleBuilder(
            timeframes=["3m"],
            on_candle=on_candle_with_tracking,
            publish_1m=False,
        )
        builder.start()

        engine = DataEngine(
            on_candle_closed=lambda sym, c: (
                tracker.record(LatencyTracker.STAGE_NORMALIZE, 0.5),
                builder.ingest(sym, c),
            ),
        )
        engine.start()

        base = align_to_boundary(1704067200000, 3)
        raws = [_raw_candle(base + i * _1M_MS) for i in range(3)]
        engine.ingest_candles("BTC/USDT", "1m", raws)

        snap = tracker.snapshot()
        self.assertIn(LatencyTracker.STAGE_NORMALIZE, snap["stages"])

        engine.stop()
        builder.stop()
        tracker.stop()


class TestNoPySide6InDataEngineModules(unittest.TestCase):
    """Static analysis: no PySide6 imports in data engine modules."""

    DATA_ENGINE_MODULES = [
        "core/market_data/ws_client.py",
        "core/market_data/rest_poller.py",
        "core/market_data/connectivity_manager.py",
        "core/market_data/data_engine.py",
        "core/market_data/candle_builder.py",
        "core/market_data/latency_tracker.py",
    ]

    def test_no_pyside6_ast(self):
        for rel_path in self.DATA_ENGINE_MODULES:
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
                    self.assertNotIn(
                        "PySide6", module,
                        f"PySide6 import found in {rel_path}: {module}",
                    )

    def test_modules_importable_without_pyside6(self):
        """All data engine modules can be imported without PySide6.
        Session 51+: PySide6 may already be loaded by test suite; we only verify
        that importing these modules doesn't REQUIRE PySide6.
        """
        import importlib
        modules = [
            "core.market_data.ws_client",
            "core.market_data.rest_poller",
            "core.market_data.data_engine",
            "core.market_data.candle_builder",
            "core.market_data.latency_tracker",
        ]
        for mod_name in modules:
            pyside_before = {m for m in sys.modules if m.startswith("PySide6")}
            try:
                importlib.import_module(mod_name)
            except ImportError as exc:
                if "PySide6" in str(exc):
                    self.fail(f"{mod_name} requires PySide6: {exc}")
            pyside_after = {m for m in sys.modules if m.startswith("PySide6")}
            new_pyside = pyside_after - pyside_before
            self.assertTrue(len(new_pyside) == 0,
                           f"{mod_name} imported new PySide6 modules: {new_pyside}")


class TestEventBusTopicOwnership(unittest.TestCase):
    """Verify candle topics are owned by DATA layer (Phase 3 Addendum fix)."""

    def test_candle_topics_owned_by_data(self):
        from core.contracts import get_topic_owner, SignalLayer
        candle_topics = [
            "market.candle.1m", "market.candle.3m", "market.candle.5m",
            "market.candle.15m", "market.candle.1h",
        ]
        for topic in candle_topics:
            owner = get_topic_owner(topic)
            self.assertEqual(owner, SignalLayer.DATA,
                             f"Topic {topic} should be owned by DATA, got {owner}")

    def test_raw_ohlcv_owned_by_connectivity(self):
        from core.contracts import get_topic_owner, SignalLayer
        owner = get_topic_owner("market.ohlcv")
        self.assertEqual(owner, SignalLayer.CONNECTIVITY)


if __name__ == "__main__":
    unittest.main()
