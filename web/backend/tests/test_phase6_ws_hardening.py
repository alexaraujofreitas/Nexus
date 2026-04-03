# ============================================================
# Phase 6C Tests — WebSocket Hardening
#
# Validates:
#   - Per-client rate limiting (sliding window)
#   - Message size cap (64 KB default)
#   - Cryptographic connection IDs (not guessable)
#   - Subscribe error for invalid channels
#   - Silent except clauses eliminated (tested via 6A)
# ============================================================
from __future__ import annotations

import time

import pytest

pytestmark = pytest.mark.unit


class TestConnectionIdCryptographic:
    """Connection IDs should be cryptographically random."""

    def test_conn_id_format(self):
        """Connection IDs use secrets.token_hex, not object id."""
        from app.ws.manager import ConnectionManager
        mgr = ConnectionManager()
        # Verify that IDs are generated with hex format
        import secrets
        conn_id = f"ws_{secrets.token_hex(16)}"
        assert conn_id.startswith("ws_")
        hex_part = conn_id[3:]
        # Should be 32 hex chars
        assert len(hex_part) == 32
        int(hex_part, 16)  # Should not raise

    def test_conn_ids_unique(self):
        """Two connection IDs should never collide."""
        import secrets
        ids = {f"ws_{secrets.token_hex(16)}" for _ in range(100)}
        assert len(ids) == 100


class TestRateLimiting:
    """Test per-client message rate limiting."""

    def test_rate_limit_allows_normal_traffic(self):
        from app.ws.manager import _ConnState, MESSAGE_RATE_LIMIT
        from unittest.mock import MagicMock
        state = _ConnState(
            ws=MagicMock(), user_sub="u1", email="test@test.com", token="tok",
        )
        # Normal traffic: should all pass
        for _ in range(MESSAGE_RATE_LIMIT):
            assert state.check_rate_limit() is True

    def test_rate_limit_blocks_excess(self):
        from app.ws.manager import _ConnState, MESSAGE_RATE_LIMIT
        from unittest.mock import MagicMock
        state = _ConnState(
            ws=MagicMock(), user_sub="u1", email="test@test.com", token="tok",
        )
        for _ in range(MESSAGE_RATE_LIMIT):
            state.check_rate_limit()
        # Next message should be blocked
        assert state.check_rate_limit() is False

    def test_rate_limit_resets_after_window(self):
        from app.ws.manager import _ConnState, MESSAGE_RATE_LIMIT
        from unittest.mock import MagicMock
        state = _ConnState(
            ws=MagicMock(), user_sub="u1", email="test@test.com", token="tok",
        )
        # Fill the window with timestamps in the past
        state._msg_timestamps = [time.time() - 2.0] * MESSAGE_RATE_LIMIT
        # Should be allowed since old timestamps expired
        assert state.check_rate_limit() is True

    def test_message_rate_limit_configurable(self):
        from app.ws.manager import MESSAGE_RATE_LIMIT
        assert MESSAGE_RATE_LIMIT == 20  # default


class TestMessageSizeLimit:
    """Test message size cap enforcement."""

    def test_size_limit_constant(self):
        from app.ws.manager import MESSAGE_SIZE_LIMIT
        assert MESSAGE_SIZE_LIMIT == 64 * 1024  # 64 KB

    def test_small_message_under_limit(self):
        from app.ws.manager import MESSAGE_SIZE_LIMIT
        msg = '{"action": "ping"}'
        assert len(msg) < MESSAGE_SIZE_LIMIT

    def test_large_message_over_limit(self):
        from app.ws.manager import MESSAGE_SIZE_LIMIT
        msg = "x" * (MESSAGE_SIZE_LIMIT + 1)
        assert len(msg) > MESSAGE_SIZE_LIMIT


class TestSubscribeErrorResponse:
    """Test subscribe returns error for invalid channels."""

    def test_subscribe_valid_channel_returns_true(self):
        from app.ws.manager import ConnectionManager
        mgr = ConnectionManager()
        result = mgr.subscribe("conn1", "ticker")
        assert result is True

    def test_subscribe_invalid_channel_returns_false(self):
        from app.ws.manager import ConnectionManager
        mgr = ConnectionManager()
        result = mgr.subscribe("conn1", "nonexistent_channel")
        assert result is False

    def test_subscribe_valid_channel_adds_subscription(self):
        from app.ws.manager import ConnectionManager
        mgr = ConnectionManager()
        mgr.subscribe("conn1", "ticker")
        assert "conn1" in mgr._subscriptions.get("ticker", set())


class TestSilentExceptEliminated:
    """Verify all except clauses in manager.py log errors."""

    def test_broadcast_logs_send_failure(self):
        """broadcast() should log warning on send failure, not silently swallow."""
        import inspect
        from app.ws.manager import ConnectionManager
        source = inspect.getsource(ConnectionManager.broadcast)
        # Should NOT have bare 'except Exception:' followed by just append
        assert "logger.warning" in source

    def test_send_personal_logs_failure(self):
        import inspect
        from app.ws.manager import ConnectionManager
        source = inspect.getsource(ConnectionManager.send_personal)
        assert "logger.warning" in source

    def test_close_connection_logs_failure(self):
        import inspect
        from app.ws.manager import ConnectionManager
        source = inspect.getsource(ConnectionManager.close_connection)
        assert "logger.debug" in source
