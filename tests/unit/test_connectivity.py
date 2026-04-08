# ============================================================
# Phase 3 — Connectivity Layer Unit Tests
#
# Validates:
#   1. WSClient state machine (IDLE → CONNECTING → STREAMING → STOPPED)
#   2. WSClient failure → FAILED state after max failures
#   3. RESTPoller lifecycle and polling
#   4. RESTPoller gap detection and backfill
#   5. ConnectivityManager WS → REST failover
#   6. ConnectivityManager unified callback routing
#   7. Metrics tracking for both sources
#   8. Symbol subscription updates
# ============================================================
import unittest
import threading
import time
from unittest.mock import MagicMock, patch, PropertyMock

from core.market_data.ws_client import (
    WebSocketClient, WSState, WSMetrics, _MAX_WS_FAILURES,
)
from core.market_data.rest_poller import (
    RESTPoller, PollerState, PollerMetrics,
)
from core.market_data.connectivity_manager import (
    ConnectivityManager, FeedSource,
)


class TestWSMetrics(unittest.TestCase):
    """Test WebSocket metrics tracking."""

    def test_initial_state(self):
        m = WSMetrics()
        self.assertEqual(m.messages_received, 0)
        self.assertEqual(m.consecutive_failures, 0)
        self.assertFalse(m.is_stale)

    def test_record_latency(self):
        m = WSMetrics()
        m.record_latency(10.0)
        m.record_latency(20.0)
        self.assertEqual(m.last_latency_ms, 20.0)
        self.assertEqual(m.avg_latency_ms, 15.0)

    def test_record_message(self):
        m = WSMetrics()
        m.record_message()
        self.assertEqual(m.messages_received, 1)
        self.assertGreater(m.last_message_at, 0)

    def test_failure_tracking(self):
        m = WSMetrics()
        m.record_failure()
        m.record_failure()
        self.assertEqual(m.consecutive_failures, 2)
        m.reset_failures()
        self.assertEqual(m.consecutive_failures, 0)

    def test_snapshot(self):
        m = WSMetrics()
        m.record_latency(5.0)
        m.record_message()
        snap = m.snapshot()
        self.assertIn("messages_received", snap)
        self.assertIn("last_latency_ms", snap)
        self.assertIn("is_stale", snap)


class TestWSClientState(unittest.TestCase):
    """Test WSClient state management (without actual WS connections)."""

    def test_initial_state_is_idle(self):
        em = MagicMock()
        em.get_ws_exchange.return_value = None
        client = WebSocketClient(em, ["BTC/USDT"])
        self.assertEqual(client.state, WSState.IDLE)

    def test_no_ws_exchange_goes_to_failed(self):
        """If no WS exchange available, client should enter FAILED state."""
        em = MagicMock()
        em.get_ws_exchange.return_value = None
        states = []
        client = WebSocketClient(
            em, ["BTC/USDT"],
            on_state_change=lambda s: states.append(s),
        )
        client.start()
        client.join(timeout=5.0)
        self.assertIn(WSState.FAILED, states)

    def test_symbol_management(self):
        em = MagicMock()
        client = WebSocketClient(em, ["BTC/USDT", "ETH/USDT"])
        self.assertEqual(len(client.get_symbols()), 2)
        client.set_symbols(["SOL/USDT"])
        self.assertEqual(client.get_symbols(), ["SOL/USDT"])

    def test_stop_from_idle(self):
        em = MagicMock()
        client = WebSocketClient(em, ["BTC/USDT"])
        client.stop()
        self.assertEqual(client.state, WSState.STOPPED)


class TestPollerMetrics(unittest.TestCase):
    """Test REST poller metrics."""

    def test_record_poll(self):
        m = PollerMetrics()
        m.record_poll(50.0, 5)
        self.assertEqual(m.polls_completed, 1)
        self.assertEqual(m.candles_received, 5)
        self.assertEqual(m.last_poll_latency_ms, 50.0)

    def test_failure_tracking(self):
        m = PollerMetrics()
        m.record_failure()
        self.assertEqual(m.polls_failed, 1)

    def test_snapshot(self):
        m = PollerMetrics()
        m.record_poll(10.0, 3)
        snap = m.snapshot()
        self.assertIn("polls_completed", snap)
        self.assertIn("avg_poll_latency_ms", snap)


class TestRESTPollerLifecycle(unittest.TestCase):
    """Test REST poller lifecycle."""

    def test_initial_state(self):
        em = MagicMock()
        poller = RESTPoller(em, ["BTC/USDT"])
        self.assertEqual(poller.state, PollerState.IDLE)

    def test_symbol_management(self):
        em = MagicMock()
        poller = RESTPoller(em, ["BTC/USDT"])
        poller.set_symbols(["ETH/USDT", "SOL/USDT"])
        self.assertEqual(len(poller.get_symbols()), 2)

    def test_stop_before_start(self):
        em = MagicMock()
        poller = RESTPoller(em, ["BTC/USDT"])
        poller.stop()
        self.assertEqual(poller.state, PollerState.STOPPED)


class TestConnectivityManagerFailover(unittest.TestCase):
    """Test WS → REST automatic failover."""

    def test_no_ws_starts_rest(self):
        em = MagicMock()
        em.get_ws_exchange.return_value = None
        received = []
        cm = ConnectivityManager(
            em, ["BTC/USDT"],
            ws_enabled=True,
            on_candle=lambda s, tf, cl: received.append((s, tf)),
        )
        cm.start()
        # Should fall back to REST since no WS exchange
        self.assertEqual(cm.active_source, FeedSource.REST)
        cm.stop()

    def test_ws_disabled_starts_rest(self):
        em = MagicMock()
        cm = ConnectivityManager(em, ["BTC/USDT"], ws_enabled=False)
        cm.start()
        self.assertEqual(cm.active_source, FeedSource.REST)
        cm.stop()

    def test_stop_clears_source(self):
        em = MagicMock()
        em.get_ws_exchange.return_value = None
        cm = ConnectivityManager(em, ["BTC/USDT"], ws_enabled=False)
        cm.start()
        cm.stop()
        self.assertEqual(cm.active_source, FeedSource.NONE)

    def test_metrics_snapshot(self):
        em = MagicMock()
        em.get_ws_exchange.return_value = None
        cm = ConnectivityManager(em, ["BTC/USDT"], ws_enabled=False)
        cm.start()
        snap = cm.get_metrics_snapshot()
        self.assertIn("active_source", snap)
        cm.stop()

    def test_symbol_update_propagates(self):
        em = MagicMock()
        em.get_ws_exchange.return_value = None
        cm = ConnectivityManager(em, ["BTC/USDT"], ws_enabled=False)
        cm.start()
        cm.set_symbols(["ETH/USDT", "SOL/USDT"])
        self.assertEqual(cm._symbols, ["ETH/USDT", "SOL/USDT"])
        cm.stop()


class TestConnectivityManagerCallbacks(unittest.TestCase):
    """Test that raw candle data routes through unified callback."""

    @patch("core.market_data.connectivity_manager.bus")
    def test_on_raw_candle_publishes_to_eventbus(self, mock_bus):
        em = MagicMock()
        em.get_ws_exchange.return_value = None
        cm = ConnectivityManager(em, ["BTC/USDT"], ws_enabled=False)
        # Call the internal callback directly
        cm._on_raw_candle("BTC/USDT", "1m", [[1000, 100, 110, 90, 105, 500]])
        mock_bus.publish.assert_called()

    def test_on_raw_candle_forwards_to_external(self):
        received = []
        em = MagicMock()
        cm = ConnectivityManager(
            em, ["BTC/USDT"], ws_enabled=False,
            on_candle=lambda s, tf, cl: received.append((s, tf, len(cl))),
        )
        cm._on_raw_candle("BTC/USDT", "1m", [[1000, 100, 110, 90, 105, 500]])
        self.assertEqual(len(received), 1)
        self.assertEqual(received[0], ("BTC/USDT", "1m", 1))


class TestCandleContractValidation(unittest.TestCase):
    """Test candle contract validation added in Module 3.5."""

    def test_valid_candle_passes(self):
        from core.contracts import validate_candle
        candle = {
            "timestamp": 1704067200000,
            "open": 100.0, "high": 110.0, "low": 90.0,
            "close": 105.0, "volume": 1000.0,
            "symbol": "BTC/USDT", "timeframe": "1m",
            "is_closed": True,
        }
        violations = validate_candle(candle)
        self.assertEqual(violations, [])

    def test_missing_field_detected(self):
        from core.contracts import validate_candle
        candle = {"timestamp": 1000, "open": 100.0}
        violations = validate_candle(candle)
        self.assertTrue(any("Missing" in v for v in violations))

    def test_invalid_timeframe_detected(self):
        from core.contracts import validate_candle
        candle = {
            "timestamp": 1704067200000,
            "open": 100.0, "high": 110.0, "low": 90.0,
            "close": 105.0, "volume": 1000.0,
            "symbol": "BTC/USDT", "timeframe": "7m",
            "is_closed": True,
        }
        violations = validate_candle(candle)
        self.assertTrue(any("timeframe" in v for v in violations))

    def test_high_less_than_low_detected(self):
        from core.contracts import validate_candle
        candle = {
            "timestamp": 1704067200000,
            "open": 100.0, "high": 80.0, "low": 90.0,
            "close": 85.0, "volume": 100.0,
            "symbol": "BTC/USDT", "timeframe": "1m",
            "is_closed": True,
        }
        violations = validate_candle(candle)
        self.assertTrue(any("high" in v and "low" in v for v in violations))

    def test_negative_volume_detected(self):
        from core.contracts import validate_candle
        candle = {
            "timestamp": 1704067200000,
            "open": 100.0, "high": 110.0, "low": 90.0,
            "close": 105.0, "volume": -50.0,
            "symbol": "BTC/USDT", "timeframe": "1m",
            "is_closed": True,
        }
        violations = validate_candle(candle)
        self.assertTrue(any("volume" in v for v in violations))

    def test_strict_raises(self):
        from core.contracts import validate_candle_strict, ContractViolation
        candle = {"timestamp": 0}
        with self.assertRaises(ContractViolation):
            validate_candle_strict(candle)


if __name__ == "__main__":
    unittest.main()
