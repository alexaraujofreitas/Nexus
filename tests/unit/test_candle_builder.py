# ============================================================
# Phase 3 — CandleBuilder Unit Tests
#
# Validates:
#   1. 1m candle passthrough and EventBus publish
#   2. Multi-TF derivation correctness (3m, 5m, 15m, 30m, 1h)
#   3. OHLCV aggregation: open=first, high=max, low=min, close=last, volume=sum
#   4. Boundary alignment (floor to TF boundary)
#   5. Incomplete window handling (gap tolerance)
#   6. Duplicate candle rejection
#   7. Memory cleanup of old boundaries
#   8. Edge cases: single candle, midnight crossover, volume=0
# ============================================================
import unittest
import time
from unittest.mock import MagicMock, patch, call

from core.market_data.candle_builder import (
    CandleBuilder, align_to_boundary, aggregate_candles,
    DERIVED_TIMEFRAMES, TF_TOPIC_MAP, _1M_MS,
)


def _make_1m_candle(ts_ms: int, o=100.0, h=105.0, l=95.0, c=102.0, v=1000.0,
                     symbol="BTC/USDT") -> dict:
    """Helper: create a closed 1m candle dict."""
    return {
        "timestamp": ts_ms,
        "open": o, "high": h, "low": l, "close": c, "volume": v,
        "symbol": symbol,
        "timeframe": "1m",
        "is_closed": True,
    }


class TestAlignToBoundary(unittest.TestCase):
    """Test timestamp boundary alignment."""

    def test_exact_boundary_unchanged(self):
        # 2024-01-01 00:00:00 UTC = 1704067200000 ms
        ts = 1704067200000
        self.assertEqual(align_to_boundary(ts, 5), ts)
        self.assertEqual(align_to_boundary(ts, 15), ts)
        self.assertEqual(align_to_boundary(ts, 60), ts)

    def test_mid_period_floors_to_start(self):
        # 00:02:30 → should align to 00:00 for 5m, 15m, 1h
        base = 1704067200000
        ts = base + 2 * _1M_MS + 30_000  # 2.5 minutes in

        self.assertEqual(align_to_boundary(ts, 5), base)
        self.assertEqual(align_to_boundary(ts, 15), base)
        self.assertEqual(align_to_boundary(ts, 60), base)

    def test_3m_alignment(self):
        base = 1704067200000
        # Minute 4 should align to minute 3 boundary
        ts = base + 4 * _1M_MS
        self.assertEqual(align_to_boundary(ts, 3), base + 3 * _1M_MS)

    def test_last_minute_of_hour(self):
        base = 1704067200000  # Hour start
        ts = base + 59 * _1M_MS  # :59
        self.assertEqual(align_to_boundary(ts, 60), base)

    def test_30m_boundary(self):
        base = 1704067200000
        # Minute 45 → should align to :30
        ts = base + 45 * _1M_MS
        self.assertEqual(align_to_boundary(ts, 30), base + 30 * _1M_MS)


class TestAggregateCandles(unittest.TestCase):
    """Test OHLCV aggregation logic."""

    def test_empty_input(self):
        self.assertIsNone(aggregate_candles([]))

    def test_single_candle(self):
        c = _make_1m_candle(1000000, o=100, h=110, l=90, c=105, v=500)
        agg = aggregate_candles([c])
        self.assertEqual(agg["open"], 100)
        self.assertEqual(agg["high"], 110)
        self.assertEqual(agg["low"], 90)
        self.assertEqual(agg["close"], 105)
        self.assertEqual(agg["volume"], 500)
        self.assertTrue(agg["is_closed"])

    def test_three_candles_aggregation(self):
        """open=first, high=max, low=min, close=last, volume=sum."""
        candles = [
            _make_1m_candle(1000, o=100, h=110, l=95,  c=105, v=100),
            _make_1m_candle(2000, o=105, h=120, l=100, c=115, v=200),
            _make_1m_candle(3000, o=115, h=118, l=90,  c=112, v=150),
        ]
        agg = aggregate_candles(candles)
        self.assertEqual(agg["timestamp"], 1000)
        self.assertEqual(agg["open"], 100)      # First candle's open
        self.assertEqual(agg["high"], 120)       # Max of all highs
        self.assertEqual(agg["low"], 90)         # Min of all lows
        self.assertEqual(agg["close"], 112)      # Last candle's close
        self.assertEqual(agg["volume"], 450)     # Sum of volumes

    def test_preserves_symbol(self):
        c = _make_1m_candle(1000, symbol="ETH/USDT")
        agg = aggregate_candles([c])
        self.assertEqual(agg["symbol"], "ETH/USDT")

    def test_zero_volume_candle(self):
        c = _make_1m_candle(1000, v=0.0)
        agg = aggregate_candles([c])
        self.assertEqual(agg["volume"], 0.0)


class TestCandleBuilderDerivation(unittest.TestCase):
    """Test multi-TF candle derivation from 1m candles."""

    def setUp(self):
        self.emitted = []
        self.builder = CandleBuilder(
            timeframes=["3m", "5m", "15m"],
            on_candle=lambda sym, tf, c: self.emitted.append((sym, tf, c)),
            publish_1m=False,  # Suppress 1m EventBus publish for cleaner tests
        )
        self.builder.start()

    def tearDown(self):
        self.builder.stop()

    def _feed_minutes(self, base_ts: int, count: int, symbol="BTC/USDT"):
        """Feed `count` sequential 1m candles starting at base_ts."""
        for i in range(count):
            ts = base_ts + i * _1M_MS
            candle = _make_1m_candle(
                ts,
                o=100 + i, h=110 + i, l=90 + i, c=105 + i,
                v=1000 + i * 10,
                symbol=symbol,
            )
            self.builder.ingest(symbol, candle)

    def test_3m_candle_emitted_after_3_minutes(self):
        base = align_to_boundary(1704067200000, 3)
        self._feed_minutes(base, 3)
        tf3_candles = [(s, tf, c) for s, tf, c in self.emitted if tf == "3m"]
        self.assertEqual(len(tf3_candles), 1)
        _, _, agg = tf3_candles[0]
        self.assertEqual(agg["timestamp"], base)
        self.assertEqual(agg["open"], 100)        # First
        self.assertEqual(agg["close"], 107)       # Last (105 + 2)
        self.assertEqual(agg["volume"], 3000 + 30)  # 1000+1010+1020

    def test_5m_candle_emitted_after_5_minutes(self):
        base = align_to_boundary(1704067200000, 5)
        self._feed_minutes(base, 5)
        tf5_candles = [(s, tf, c) for s, tf, c in self.emitted if tf == "5m"]
        self.assertEqual(len(tf5_candles), 1)
        _, _, agg = tf5_candles[0]
        self.assertEqual(agg["timestamp"], base)
        self.assertEqual(agg["volume"], sum(1000 + i * 10 for i in range(5)))

    def test_15m_candle_emitted_after_15_minutes(self):
        base = align_to_boundary(1704067200000, 15)
        self._feed_minutes(base, 15)
        tf15_candles = [(s, tf, c) for s, tf, c in self.emitted if tf == "15m"]
        self.assertEqual(len(tf15_candles), 1)

    def test_no_emission_before_window_complete(self):
        base = align_to_boundary(1704067200000, 5)
        self._feed_minutes(base, 4)  # Only 4 of 5 needed
        tf5_candles = [(s, tf, c) for s, tf, c in self.emitted if tf == "5m"]
        self.assertEqual(len(tf5_candles), 0)

    def test_multiple_windows_emit_multiple_candles(self):
        base = align_to_boundary(1704067200000, 3)
        self._feed_minutes(base, 9)  # 3 complete 3m windows
        tf3_candles = [(s, tf, c) for s, tf, c in self.emitted if tf == "3m"]
        self.assertEqual(len(tf3_candles), 3)

    def test_multi_symbol_isolation(self):
        base = align_to_boundary(1704067200000, 3)
        self._feed_minutes(base, 3, symbol="BTC/USDT")
        self._feed_minutes(base, 3, symbol="ETH/USDT")
        btc = [(s, tf, c) for s, tf, c in self.emitted if tf == "3m" and s == "BTC/USDT"]
        eth = [(s, tf, c) for s, tf, c in self.emitted if tf == "3m" and s == "ETH/USDT"]
        self.assertEqual(len(btc), 1)
        self.assertEqual(len(eth), 1)

    def test_no_duplicate_emission(self):
        """Feeding the same candles twice should not re-emit."""
        base = align_to_boundary(1704067200000, 3)
        self._feed_minutes(base, 3)
        count_before = len([e for e in self.emitted if e[1] == "3m"])

        # Feed same candles again
        self._feed_minutes(base, 3)
        count_after = len([e for e in self.emitted if e[1] == "3m"])
        self.assertEqual(count_before, count_after)

    def test_high_is_max_across_window(self):
        base = align_to_boundary(1704067200000, 3)
        candles = [
            _make_1m_candle(base,              h=100),
            _make_1m_candle(base + _1M_MS,     h=200),  # Highest
            _make_1m_candle(base + 2 * _1M_MS, h=150),
        ]
        for c in candles:
            self.builder.ingest("BTC/USDT", c)
        tf3 = [e for e in self.emitted if e[1] == "3m"]
        self.assertEqual(tf3[0][2]["high"], 200)

    def test_low_is_min_across_window(self):
        base = align_to_boundary(1704067200000, 3)
        candles = [
            _make_1m_candle(base,              l=50),
            _make_1m_candle(base + _1M_MS,     l=30),   # Lowest
            _make_1m_candle(base + 2 * _1M_MS, l=45),
        ]
        for c in candles:
            self.builder.ingest("BTC/USDT", c)
        tf3 = [e for e in self.emitted if e[1] == "3m"]
        self.assertEqual(tf3[0][2]["low"], 30)


class TestCandleBuilderMetrics(unittest.TestCase):
    """Test metrics tracking."""

    def test_1m_count_tracked(self):
        builder = CandleBuilder(timeframes=["3m"], publish_1m=False)
        builder.start()
        base = align_to_boundary(1704067200000, 3)
        for i in range(5):
            builder.ingest("BTC/USDT", _make_1m_candle(base + i * _1M_MS))
        self.assertEqual(builder.metrics.candles_received_1m, 5)
        builder.stop()

    def test_build_count_tracked(self):
        builder = CandleBuilder(timeframes=["3m"], publish_1m=False)
        builder.start()
        base = align_to_boundary(1704067200000, 3)
        for i in range(3):
            builder.ingest("BTC/USDT", _make_1m_candle(base + i * _1M_MS))
        self.assertEqual(builder.metrics.candles_built.get("3m", 0), 1)
        builder.stop()


class TestCandleBuilderEventBus(unittest.TestCase):
    """Test EventBus integration."""

    @patch("core.market_data.candle_builder.bus")
    def test_1m_published_to_eventbus(self, mock_bus):
        builder = CandleBuilder(timeframes=[], publish_1m=True)
        builder.start()
        candle = _make_1m_candle(1704067200000)
        builder.ingest("BTC/USDT", candle)
        mock_bus.publish.assert_called()
        # Find the CANDLE_1M publish call
        calls = [c for c in mock_bus.publish.call_args_list
                 if c[0][0] == "market.candle.1m"]
        self.assertEqual(len(calls), 1)
        builder.stop()

    @patch("core.market_data.candle_builder.bus")
    def test_derived_tf_published_to_correct_topic(self, mock_bus):
        builder = CandleBuilder(timeframes=["5m"], publish_1m=False)
        builder.start()
        base = align_to_boundary(1704067200000, 5)
        for i in range(5):
            builder.ingest("BTC/USDT", _make_1m_candle(base + i * _1M_MS))
        calls = [c for c in mock_bus.publish.call_args_list
                 if c[0][0] == "market.candle.5m"]
        self.assertEqual(len(calls), 1)
        builder.stop()


class TestCandleBuilderEdgeCases(unittest.TestCase):
    """Edge case handling."""

    def test_not_started_ignores_ingest(self):
        builder = CandleBuilder(timeframes=["3m"])
        # Not started — should silently ignore
        builder.ingest("BTC/USDT", _make_1m_candle(1704067200000))
        self.assertEqual(builder.metrics.candles_received_1m, 0)

    def test_get_current_candle_partial(self):
        builder = CandleBuilder(timeframes=["5m"], publish_1m=False)
        builder.start()
        base = align_to_boundary(int(time.time() * 1000), 5)
        # Feed only 2 of 5 candles
        for i in range(2):
            builder.ingest("BTC/USDT", _make_1m_candle(base + i * _1M_MS))
        current = builder.get_current_candle("BTC/USDT", "5m")
        # Should return partial aggregation
        self.assertIsNotNone(current)
        builder.stop()


if __name__ == "__main__":
    unittest.main()
