"""
Phase 8 Test Suite — Exchange Adapter

Tests:
- Error classification (transient, rejected, duplicate, not_found, unknown)
- Retry logic (transient retries, non-transient fails immediately)
- Response parsing
- Fail-closed behavior (unknown errors → no retry)
- Order creation, cancellation, fetch
"""

import pytest
from unittest.mock import MagicMock, patch, PropertyMock
from core.intraday.live.exchange_adapter import (
    ExchangeAdapter,
    ExchangeError,
    ExchangeErrorClass,
    ExchangeResponse,
    RetryConfig,
)


# ══════════════════════════════════════════════════════════════
# FIXTURES
# ══════════════════════════════════════════════════════════════

def _make_adapter(exchange=None, max_retries=2, base_delay_ms=10):
    """Create adapter with fast retry for testing."""
    if exchange is None:
        exchange = MagicMock()
        exchange.id = "bybit"
    config = RetryConfig(
        max_retries=max_retries,
        base_delay_ms=base_delay_ms,
        max_delay_ms=50,
        timeout_ms=5000,
    )
    clock = [1000000]
    adapter = ExchangeAdapter(
        exchange=exchange,
        retry_config=config,
        now_ms_fn=lambda: clock[0],
    )
    return adapter, exchange, clock


def _mock_ccxt():
    """Create mock ccxt module with exception classes."""
    ccxt = MagicMock()

    class NetworkError(Exception): pass
    class RequestTimeout(Exception): pass
    class ExchangeNotAvailable(Exception): pass
    class RateLimitExceeded(Exception): pass
    class InsufficientFunds(Exception): pass
    class InvalidOrder(Exception): pass
    class BadRequest(Exception): pass
    class AuthenticationError(Exception): pass
    class PermissionDenied(Exception): pass
    class OrderNotFound(Exception): pass
    class OnMaintenance(Exception): pass

    ccxt.NetworkError = NetworkError
    ccxt.RequestTimeout = RequestTimeout
    ccxt.ExchangeNotAvailable = ExchangeNotAvailable
    ccxt.RateLimitExceeded = RateLimitExceeded
    ccxt.InsufficientFunds = InsufficientFunds
    ccxt.InvalidOrder = InvalidOrder
    ccxt.BadRequest = BadRequest
    ccxt.AuthenticationError = AuthenticationError
    ccxt.PermissionDenied = PermissionDenied
    ccxt.OrderNotFound = OrderNotFound
    ccxt.OnMaintenance = OnMaintenance

    return ccxt


# ══════════════════════════════════════════════════════════════
# 1. ERROR CLASSIFICATION
# ══════════════════════════════════════════════════════════════

class TestErrorClassification:
    def test_network_error_is_transient(self):
        adapter, _, _ = _make_adapter()
        ccxt = _mock_ccxt()
        with patch.dict("sys.modules", {"ccxt": ccxt}):
            err = adapter._classify_error(ccxt.NetworkError("timeout"))
            assert err.error_class == ExchangeErrorClass.TRANSIENT

    def test_request_timeout_is_transient(self):
        adapter, _, _ = _make_adapter()
        ccxt = _mock_ccxt()
        with patch.dict("sys.modules", {"ccxt": ccxt}):
            err = adapter._classify_error(ccxt.RequestTimeout("timeout"))
            assert err.error_class == ExchangeErrorClass.TRANSIENT

    def test_rate_limit_is_transient(self):
        adapter, _, _ = _make_adapter()
        ccxt = _mock_ccxt()
        with patch.dict("sys.modules", {"ccxt": ccxt}):
            err = adapter._classify_error(ccxt.RateLimitExceeded("too many"))
            assert err.error_class == ExchangeErrorClass.TRANSIENT

    def test_insufficient_funds_is_rejected(self):
        adapter, _, _ = _make_adapter()
        ccxt = _mock_ccxt()
        with patch.dict("sys.modules", {"ccxt": ccxt}):
            err = adapter._classify_error(ccxt.InsufficientFunds("no money"))
            assert err.error_class == ExchangeErrorClass.REJECTED

    def test_invalid_order_is_rejected(self):
        adapter, _, _ = _make_adapter()
        ccxt = _mock_ccxt()
        with patch.dict("sys.modules", {"ccxt": ccxt}):
            err = adapter._classify_error(ccxt.InvalidOrder("bad params"))
            assert err.error_class == ExchangeErrorClass.REJECTED

    def test_duplicate_order_is_duplicate(self):
        adapter, _, _ = _make_adapter()
        ccxt = _mock_ccxt()
        with patch.dict("sys.modules", {"ccxt": ccxt}):
            err = adapter._classify_error(ccxt.InvalidOrder("duplicate order already exists"))
            assert err.error_class == ExchangeErrorClass.DUPLICATE

    def test_order_not_found(self):
        adapter, _, _ = _make_adapter()
        ccxt = _mock_ccxt()
        with patch.dict("sys.modules", {"ccxt": ccxt}):
            err = adapter._classify_error(ccxt.OrderNotFound("not found"))
            assert err.error_class == ExchangeErrorClass.NOT_FOUND

    def test_maintenance_is_exchange_down(self):
        adapter, _, _ = _make_adapter()
        ccxt = _mock_ccxt()
        with patch.dict("sys.modules", {"ccxt": ccxt}):
            err = adapter._classify_error(ccxt.OnMaintenance("upgrading"))
            assert err.error_class == ExchangeErrorClass.EXCHANGE_DOWN

    def test_unknown_error_is_unknown(self):
        adapter, _, _ = _make_adapter()
        ccxt = _mock_ccxt()
        with patch.dict("sys.modules", {"ccxt": ccxt}):
            err = adapter._classify_error(ValueError("something weird"))
            assert err.error_class == ExchangeErrorClass.UNKNOWN


# ══════════════════════════════════════════════════════════════
# 2. RETRY LOGIC
# ══════════════════════════════════════════════════════════════

class TestRetryLogic:
    def test_success_on_first_try(self):
        adapter, exchange, _ = _make_adapter()
        exchange.create_order.return_value = {
            "id": "EX-1", "status": "closed", "filled": 0.01,
            "average": 50000.0, "remaining": 0, "fee": {"cost": 0.28, "currency": "USDT"},
        }
        resp = adapter.create_order("BTC/USDT:USDT", "market", "buy", 0.01)
        assert resp.success
        assert resp.exchange_order_id == "EX-1"
        assert exchange.create_order.call_count == 1

    def test_transient_error_retries(self):
        adapter, exchange, _ = _make_adapter(max_retries=2, base_delay_ms=1)
        ccxt = _mock_ccxt()

        call_count = [0]
        def side_effect(**kwargs):
            call_count[0] += 1
            if call_count[0] <= 2:
                raise ccxt.NetworkError("timeout")
            return {
                "id": "EX-1", "status": "closed", "filled": 0.01,
                "average": 50000.0, "remaining": 0, "fee": {"cost": 0.28, "currency": "USDT"},
            }

        exchange.create_order.side_effect = side_effect
        with patch.dict("sys.modules", {"ccxt": ccxt}):
            resp = adapter.create_order("BTC/USDT:USDT", "market", "buy", 0.01)
        assert resp.success
        assert call_count[0] == 3  # 2 failures + 1 success

    def test_transient_exhausted(self):
        adapter, exchange, _ = _make_adapter(max_retries=1, base_delay_ms=1)
        ccxt = _mock_ccxt()

        exchange.create_order.side_effect = ccxt.NetworkError("timeout")
        with patch.dict("sys.modules", {"ccxt": ccxt}):
            resp = adapter.create_order("BTC/USDT:USDT", "market", "buy", 0.01)
        assert not resp.success
        assert resp.error.error_class == ExchangeErrorClass.TRANSIENT

    def test_non_transient_no_retry(self):
        adapter, exchange, _ = _make_adapter(max_retries=3, base_delay_ms=1)
        ccxt = _mock_ccxt()

        exchange.create_order.side_effect = ccxt.InsufficientFunds("no money")
        with patch.dict("sys.modules", {"ccxt": ccxt}):
            resp = adapter.create_order("BTC/USDT:USDT", "market", "buy", 0.01)
        assert not resp.success
        assert resp.error.error_class == ExchangeErrorClass.REJECTED
        assert exchange.create_order.call_count == 1  # No retry

    def test_unknown_error_no_retry(self):
        adapter, exchange, _ = _make_adapter(max_retries=3, base_delay_ms=1)
        ccxt = _mock_ccxt()

        exchange.create_order.side_effect = RuntimeError("what")
        with patch.dict("sys.modules", {"ccxt": ccxt}):
            resp = adapter.create_order("BTC/USDT:USDT", "market", "buy", 0.01)
        assert not resp.success
        assert exchange.create_order.call_count == 1


# ══════════════════════════════════════════════════════════════
# 3. RESPONSE PARSING
# ══════════════════════════════════════════════════════════════

class TestResponseParsing:
    def test_parse_full_response(self):
        adapter, exchange, _ = _make_adapter()
        exchange.create_order.return_value = {
            "id": "EX-1",
            "status": "closed",
            "filled": 0.01,
            "average": 50100.0,
            "remaining": 0,
            "fee": {"cost": 0.28, "currency": "USDT"},
        }
        resp = adapter.create_order("BTC/USDT:USDT", "market", "buy", 0.01)
        assert resp.success
        assert resp.exchange_order_id == "EX-1"
        assert resp.status == "closed"
        assert resp.filled_quantity == 0.01
        assert resp.avg_price == 50100.0
        assert resp.fee == 0.28
        assert resp.fee_currency == "USDT"
        assert resp.remaining == 0

    def test_parse_open_order(self):
        adapter, exchange, _ = _make_adapter()
        exchange.create_order.return_value = {
            "id": "EX-2",
            "status": "open",
            "filled": 0,
            "average": None,
            "remaining": 0.01,
            "fee": None,
        }
        resp = adapter.create_order("BTC/USDT:USDT", "limit", "buy", 0.01, price=49000)
        assert resp.success
        assert resp.status == "open"
        assert resp.filled_quantity == 0

    def test_parse_none_values(self):
        adapter, exchange, _ = _make_adapter()
        exchange.create_order.return_value = {
            "id": "EX-3",
            "status": "closed",
            "filled": None,
            "average": None,
            "remaining": None,
            "fee": None,
        }
        resp = adapter.create_order("BTC/USDT:USDT", "market", "buy", 0.01)
        assert resp.success
        assert resp.filled_quantity == 0
        assert resp.avg_price == 0
        assert resp.fee == 0

    def test_client_order_id_passed_as_param(self):
        adapter, exchange, _ = _make_adapter()
        exchange.create_order.return_value = {
            "id": "EX-1", "status": "closed", "filled": 0.01,
            "average": 50000.0, "remaining": 0, "fee": None,
        }
        adapter.create_order(
            "BTC/USDT:USDT", "market", "buy", 0.01,
            client_order_id="NT-abc123",
        )
        call_args = exchange.create_order.call_args
        params = call_args.kwargs.get("params", {})
        assert params.get("orderLinkId") == "NT-abc123"


# ══════════════════════════════════════════════════════════════
# 4. CANCEL AND FETCH
# ══════════════════════════════════════════════════════════════

class TestCancelAndFetch:
    def test_cancel_order(self):
        adapter, exchange, _ = _make_adapter()
        exchange.cancel_order.return_value = {
            "id": "EX-1", "status": "canceled", "filled": 0,
            "average": 0, "remaining": 0, "fee": None,
        }
        resp = adapter.cancel_order("EX-1", "BTC/USDT:USDT")
        assert resp.success
        assert resp.status == "canceled"

    def test_fetch_order(self):
        adapter, exchange, _ = _make_adapter()
        exchange.fetch_order.return_value = {
            "id": "EX-1", "status": "closed", "filled": 0.01,
            "average": 50000.0, "remaining": 0, "fee": {"cost": 0.28, "currency": "USDT"},
        }
        resp = adapter.fetch_order("EX-1", "BTC/USDT:USDT")
        assert resp.success
        assert resp.filled_quantity == 0.01

    def test_fetch_open_orders(self):
        adapter, exchange, _ = _make_adapter()
        exchange.fetch_open_orders.return_value = [
            {"id": "EX-1", "status": "open", "filled": 0, "average": 0, "remaining": 0.01, "fee": None},
            {"id": "EX-2", "status": "open", "filled": 0, "average": 0, "remaining": 0.02, "fee": None},
        ]
        orders = adapter.fetch_open_orders("BTC/USDT:USDT")
        assert len(orders) == 2
        assert orders[0].exchange_order_id == "EX-1"

    def test_fetch_positions(self):
        adapter, exchange, _ = _make_adapter()
        exchange.fetch_positions.return_value = [
            {"symbol": "BTC/USDT:USDT", "contracts": 0.01, "side": "long"},
        ]
        positions = adapter.fetch_positions()
        assert len(positions) == 1

    def test_fetch_balance(self):
        adapter, exchange, _ = _make_adapter()
        exchange.fetch_balance.return_value = {
            "USDT": {"total": 10000.0, "free": 8000.0},
        }
        balance = adapter.fetch_balance()
        assert balance["USDT"]["total"] == 10000.0


# ══════════════════════════════════════════════════════════════
# 5. RESPONSE TO_DICT
# ══════════════════════════════════════════════════════════════

class TestResponseSerialization:
    def test_success_response_to_dict(self):
        resp = ExchangeResponse(
            success=True,
            exchange_order_id="EX-1",
            status="closed",
            filled_quantity=0.01,
            avg_price=50000.0,
            fee=0.28,
            timestamp_ms=1000,
        )
        d = resp.to_dict()
        assert d["success"] is True
        assert d["exchange_order_id"] == "EX-1"
        assert d["error"] is None

    def test_error_response_to_dict(self):
        err = ExchangeError("bad", ExchangeErrorClass.REJECTED)
        resp = ExchangeResponse(
            success=False,
            error=err,
            timestamp_ms=1000,
        )
        d = resp.to_dict()
        assert d["success"] is False
        assert "bad" in d["error"]


# ══════════════════════════════════════════════════════════════
# 6. DIAGNOSTICS
# ══════════════════════════════════════════════════════════════

class TestDiagnostics:
    def test_get_state(self):
        adapter, _, _ = _make_adapter()
        state = adapter.get_state()
        assert state["exchange_id"] == "bybit"
        assert "retry_config" in state
