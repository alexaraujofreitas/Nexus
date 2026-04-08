# ============================================================
# Phase 3 — DataEngine Unit Tests
#
# Validates:
#   1. Raw OHLCV normalization to canonical candle dict
#   2. Buffer insertion and deduplication
#   3. Gap detection in candle series
#   4. Forward-to-CandleBuilder on closed candles
#   5. Invalid data rejection
#   6. Buffer ring behavior (capacity limit)
#   7. Multi-symbol isolation
#   8. Metrics tracking
# ============================================================
import unittest
import time
from unittest.mock import MagicMock

from core.market_data.data_engine import (
    DataEngine, CandleBuffer, _1M_MS, _BUFFER_SIZE,
)


def _raw_candle(ts_ms: int, o=100.0, h=105.0, l=95.0, c=102.0, v=1000.0):
    """Helper: create a raw OHLCV array."""
    return [ts_ms, o, h, l, c, v]


class TestCandleBuffer(unittest.TestCase):
    """Test the per-symbol ring buffer."""

    def test_insert_and_retrieve(self):
        buf = CandleBuffer(max_size=100)
        candle = {"timestamp": 1000, "open": 100, "high": 110, "low": 90,
                  "close": 105, "volume": 500, "is_closed": True}
        self.assertTrue(buf.insert(candle))
        self.assertEqual(len(buf), 1)
        result = buf.get_latest(1)
        self.assertEqual(result[0]["timestamp"], 1000)

    def test_duplicate_rejected(self):
        buf = CandleBuffer()
        candle = {"timestamp": 1000, "is_closed": True}
        self.assertTrue(buf.insert(candle))
        self.assertFalse(buf.insert(candle))
        self.assertEqual(len(buf), 1)

    def test_duplicate_updated_if_closed(self):
        """Open candle updated to closed version."""
        buf = CandleBuffer()
        open_c = {"timestamp": 1000, "is_closed": False, "close": 100}
        closed_c = {"timestamp": 1000, "is_closed": True, "close": 105}
        buf.insert(open_c)
        self.assertTrue(buf.insert(closed_c))
        result = buf.get_latest(1)
        self.assertEqual(result[0]["close"], 105)

    def test_sorted_order(self):
        buf = CandleBuffer()
        for ts in [3000, 1000, 2000]:
            buf.insert({"timestamp": ts, "is_closed": True})
        result = buf.get_latest(3)
        self.assertEqual([c["timestamp"] for c in result], [1000, 2000, 3000])

    def test_max_size_enforced(self):
        buf = CandleBuffer(max_size=5)
        for i in range(10):
            buf.insert({"timestamp": i * 1000, "is_closed": True})
        self.assertEqual(len(buf), 5)
        # Should have kept the latest 5
        result = buf.get_latest(5)
        self.assertEqual(result[0]["timestamp"], 5000)

    def test_get_since(self):
        buf = CandleBuffer()
        for i in range(5):
            buf.insert({"timestamp": i * _1M_MS, "is_closed": True})
        result = buf.get_since(2 * _1M_MS)
        self.assertEqual(len(result), 3)  # timestamps 2, 3, 4

    def test_get_range(self):
        buf = CandleBuffer()
        for i in range(10):
            buf.insert({"timestamp": i * _1M_MS, "is_closed": True})
        result = buf.get_range(3 * _1M_MS, 7 * _1M_MS)
        self.assertEqual(len(result), 4)  # 3, 4, 5, 6

    def test_gap_detection(self):
        buf = CandleBuffer()
        # Insert with a gap: 0, 1, 2, [gap], 5, 6
        for i in [0, 1, 2, 5, 6]:
            buf.insert({"timestamp": i * _1M_MS, "is_closed": True})
        gaps = buf.detect_gaps()
        self.assertEqual(len(gaps), 1)
        self.assertEqual(gaps[0][0], 3 * _1M_MS)  # Gap starts at minute 3

    def test_no_gap_in_continuous_series(self):
        buf = CandleBuffer()
        for i in range(10):
            buf.insert({"timestamp": i * _1M_MS, "is_closed": True})
        gaps = buf.detect_gaps()
        self.assertEqual(len(gaps), 0)

    def test_get_last_timestamp(self):
        buf = CandleBuffer()
        self.assertIsNone(buf.get_last_timestamp())
        buf.insert({"timestamp": 5000, "is_closed": True})
        self.assertEqual(buf.get_last_timestamp(), 5000)


class TestDataEngineNormalization(unittest.TestCase):
    """Test raw OHLCV → canonical candle dict normalization."""

    def test_valid_raw_normalizes(self):
        # Use a timestamp far in the past so it's "closed"
        ts = 1704067200000  # 2024-01-01 00:00
        result = DataEngine._normalize("BTC/USDT", "1m", _raw_candle(ts))
        self.assertIsNotNone(result)
        self.assertEqual(result["timestamp"], ts)
        self.assertEqual(result["open"], 100.0)
        self.assertEqual(result["symbol"], "BTC/USDT")
        self.assertEqual(result["timeframe"], "1m")
        self.assertTrue(result["is_closed"])

    def test_empty_raw_returns_none(self):
        self.assertIsNone(DataEngine._normalize("BTC/USDT", "1m", []))

    def test_short_raw_returns_none(self):
        self.assertIsNone(DataEngine._normalize("BTC/USDT", "1m", [1, 2, 3]))

    def test_negative_price_returns_none(self):
        self.assertIsNone(DataEngine._normalize("BTC/USDT", "1m", [1000, -1, 5, 3, 4, 100]))

    def test_high_less_than_low_returns_none(self):
        # h=5, l=10 → invalid
        self.assertIsNone(DataEngine._normalize("BTC/USDT", "1m", [1000, 7, 5, 10, 8, 100]))

    def test_negative_volume_returns_none(self):
        self.assertIsNone(DataEngine._normalize("BTC/USDT", "1m", [1000, 100, 110, 90, 105, -50]))

    def test_zero_timestamp_returns_none(self):
        self.assertIsNone(DataEngine._normalize("BTC/USDT", "1m", [0, 100, 110, 90, 105, 50]))

    def test_zero_volume_accepted(self):
        ts = 1704067200000
        result = DataEngine._normalize("BTC/USDT", "1m", [ts, 100, 110, 90, 105, 0])
        self.assertIsNotNone(result)
        self.assertEqual(result["volume"], 0.0)

    def test_type_coercion(self):
        """String numbers should be coerced to float/int."""
        ts = 1704067200000
        result = DataEngine._normalize("BTC/USDT", "1m", [str(ts), "100", "110", "90", "105", "50"])
        self.assertIsNotNone(result)
        self.assertEqual(result["timestamp"], ts)
        self.assertEqual(result["open"], 100.0)


class TestDataEngineIngestion(unittest.TestCase):
    """Test the full ingestion pipeline."""

    def setUp(self):
        self.closed_candles = []
        self.engine = DataEngine(
            on_candle_closed=lambda sym, c: self.closed_candles.append((sym, c)),
        )
        self.engine.start()

    def tearDown(self):
        self.engine.stop()

    def test_ingest_creates_buffer(self):
        ts = 1704067200000
        self.engine.ingest_candles("BTC/USDT", "1m", [_raw_candle(ts)])
        self.assertIn("BTC/USDT", self.engine.get_active_symbols())

    def test_ingest_returns_new_count(self):
        ts = 1704067200000
        count = self.engine.ingest_candles("BTC/USDT", "1m", [
            _raw_candle(ts),
            _raw_candle(ts + _1M_MS),
        ])
        self.assertEqual(count, 2)

    def test_duplicate_ingest_returns_zero(self):
        ts = 1704067200000
        self.engine.ingest_candles("BTC/USDT", "1m", [_raw_candle(ts)])
        count = self.engine.ingest_candles("BTC/USDT", "1m", [_raw_candle(ts)])
        self.assertEqual(count, 0)

    def test_closed_candle_forwarded(self):
        ts = 1704067200000  # Far past → is_closed=True
        self.engine.ingest_candles("BTC/USDT", "1m", [_raw_candle(ts)])
        self.assertEqual(len(self.closed_candles), 1)
        self.assertEqual(self.closed_candles[0][0], "BTC/USDT")

    def test_closed_candle_not_re_forwarded(self):
        ts = 1704067200000
        self.engine.ingest_candles("BTC/USDT", "1m", [_raw_candle(ts)])
        self.engine.ingest_candles("BTC/USDT", "1m", [_raw_candle(ts)])
        self.assertEqual(len(self.closed_candles), 1)

    def test_invalid_candles_counted(self):
        self.engine.ingest_candles("BTC/USDT", "1m", [
            _raw_candle(1704067200000),
            [],  # Invalid
            [0, 0, 0, 0, 0, 0],  # Invalid (zero ts, zero prices)
        ])
        self.assertGreater(self.engine.metrics.invalid_candles, 0)

    def test_multi_symbol_isolation(self):
        ts = 1704067200000
        self.engine.ingest_candles("BTC/USDT", "1m", [_raw_candle(ts)])
        self.engine.ingest_candles("ETH/USDT", "1m", [_raw_candle(ts, o=3000, h=3100, l=2900, c=3050)])
        self.assertEqual(len(self.engine.get_active_symbols()), 2)
        btc = self.engine.get_latest_candles("BTC/USDT", 1)
        eth = self.engine.get_latest_candles("ETH/USDT", 1)
        self.assertEqual(btc[0]["open"], 100.0)
        self.assertEqual(eth[0]["open"], 3000.0)

    def test_not_started_returns_zero(self):
        engine = DataEngine()
        # Not started
        count = engine.ingest_candles("BTC/USDT", "1m", [_raw_candle(1000)])
        self.assertEqual(count, 0)

    def test_empty_input_returns_zero(self):
        count = self.engine.ingest_candles("BTC/USDT", "1m", [])
        self.assertEqual(count, 0)

    def test_get_candles_since(self):
        ts = 1704067200000
        self.engine.ingest_candles("BTC/USDT", "1m", [
            _raw_candle(ts + i * _1M_MS) for i in range(10)
        ])
        result = self.engine.get_candles_since("BTC/USDT", ts + 5 * _1M_MS)
        self.assertEqual(len(result), 5)

    def test_detect_gaps(self):
        ts = 1704067200000
        raws = [_raw_candle(ts + i * _1M_MS) for i in [0, 1, 2, 5, 6]]
        self.engine.ingest_candles("BTC/USDT", "1m", raws)
        gaps = self.engine.detect_gaps("BTC/USDT")
        self.assertGreater(len(gaps), 0)


class TestDataEngineMetrics(unittest.TestCase):
    """Test metrics tracking."""

    def test_metrics_snapshot(self):
        engine = DataEngine()
        engine.start()
        ts = 1704067200000
        engine.ingest_candles("BTC/USDT", "1m", [_raw_candle(ts)])
        snap = engine.metrics.snapshot()
        self.assertGreater(snap["candles_ingested"], 0)
        self.assertGreater(snap["candles_normalized"], 0)
        engine.stop()


if __name__ == "__main__":
    unittest.main()
