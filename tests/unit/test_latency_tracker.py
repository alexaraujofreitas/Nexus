# ============================================================
# Phase 3 — Latency Tracker Unit Tests
#
# Validates:
#   1. Latency bucket recording and percentiles
#   2. Multi-stage tracking
#   3. End-to-end latency tracking (arrival → publish)
#   4. Throughput counting
#   5. Snapshot format
# ============================================================
import unittest
import time

from core.market_data.latency_tracker import (
    LatencyTracker, LatencyBucket,
)


class TestLatencyBucket(unittest.TestCase):

    def test_empty_bucket(self):
        b = LatencyBucket(name="test")
        self.assertEqual(b.avg_ms, 0.0)
        self.assertEqual(b.p50_ms, 0.0)
        self.assertEqual(b.max_ms, 0.0)

    def test_single_sample(self):
        b = LatencyBucket(name="test")
        b.record(10.0)
        self.assertEqual(b.last_ms, 10.0)
        self.assertEqual(b.avg_ms, 10.0)
        self.assertEqual(b.p50_ms, 10.0)
        self.assertEqual(b.total_count, 1)

    def test_multiple_samples(self):
        b = LatencyBucket(name="test")
        for v in [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]:
            b.record(float(v))
        self.assertEqual(b.total_count, 10)
        self.assertEqual(b.avg_ms, 5.5)
        self.assertGreater(b.p95_ms, 9.0)
        self.assertEqual(b.max_ms, 10.0)

    def test_snapshot_format(self):
        b = LatencyBucket(name="ws")
        b.record(5.0)
        snap = b.snapshot()
        self.assertEqual(snap["name"], "ws")
        self.assertIn("count", snap)
        self.assertIn("p50_ms", snap)
        self.assertIn("p95_ms", snap)


class TestLatencyTracker(unittest.TestCase):

    def test_record_and_snapshot(self):
        tracker = LatencyTracker(log_interval_s=9999)
        tracker.start()
        tracker.record("ws_message", 12.5)
        tracker.record("ws_message", 15.0)
        tracker.record("candle_normalize", 0.5)
        snap = tracker.snapshot()
        self.assertIn("ws_message", snap["stages"])
        self.assertIn("candle_normalize", snap["stages"])
        self.assertEqual(snap["stages"]["ws_message"]["count"], 2)
        tracker.stop()

    def test_end_to_end_tracking(self):
        tracker = LatencyTracker(log_interval_s=9999)
        tracker.start()
        tracker.mark_arrival("BTC/USDT")
        time.sleep(0.01)  # 10ms
        tracker.mark_published("BTC/USDT")
        stage = tracker.get_stage(LatencyTracker.STAGE_END_TO_END)
        self.assertIsNotNone(stage)
        self.assertGreater(stage["last_ms"], 5.0)  # At least 5ms
        tracker.stop()

    def test_get_stage_nonexistent(self):
        tracker = LatencyTracker()
        tracker.start()
        self.assertIsNone(tracker.get_stage("nonexistent"))
        tracker.stop()

    def test_not_started_ignores(self):
        tracker = LatencyTracker()
        tracker.record("test", 10.0)
        snap = tracker.snapshot()
        self.assertEqual(len(snap["stages"]), 0)


if __name__ == "__main__":
    unittest.main()
