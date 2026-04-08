"""
Phase 8 Test Suite — Order Lifecycle State Machine

Tests:
- All valid transitions
- All invalid transitions (every terminal → non-terminal)
- State machine completeness (every state has defined transitions)
- LiveOrder creation, transition audit trail
- Fill recording (VWAP, multi-fill)
- Serialization round-trip (to_dict / from_dict)
- Deterministic client_order_id generation
- Properties (is_terminal, is_fully_filled, fill_pct, slippage_pct)
"""

import pytest
from core.intraday.live.order_lifecycle import (
    OrderLifecycleState,
    LiveOrder,
    OrderTransitionError,
    TransitionRecord,
    VALID_TRANSITIONS,
    TERMINAL_STATES,
    NON_TERMINAL_STATES,
    make_client_order_id,
)


# ══════════════════════════════════════════════════════════════
# FIXTURES
# ══════════════════════════════════════════════════════════════

def _make_order(**overrides) -> LiveOrder:
    """Factory for LiveOrder with sensible defaults."""
    defaults = dict(
        client_order_id="NT-abc123",
        request_id="req-001",
        decision_id="dec-001",
        trigger_id="trig-001",
        symbol="BTCUSDT",
        side="buy",
        order_type="market",
        requested_price=50000.0,
        requested_quantity=0.01,
        size_usdt=500.0,
        strategy_name="MomentumBreakout",
        regime="bull_trend",
        stop_loss=49000.0,
        take_profit=52000.0,
        created_at_ms=1000000,
    )
    defaults.update(overrides)
    return LiveOrder(**defaults)


# ══════════════════════════════════════════════════════════════
# 1. STATE MACHINE COMPLETENESS
# ══════════════════════════════════════════════════════════════

class TestStateMachineCompleteness:
    """Every OrderLifecycleState must appear in VALID_TRANSITIONS."""

    def test_all_states_in_transition_table(self):
        for state in OrderLifecycleState:
            assert state in VALID_TRANSITIONS, f"{state} missing from VALID_TRANSITIONS"

    def test_terminal_states_have_no_outgoing(self):
        for state in TERMINAL_STATES:
            assert VALID_TRANSITIONS[state] == frozenset(), (
                f"Terminal state {state} should have no outgoing transitions"
            )

    def test_non_terminal_states_have_outgoing(self):
        for state in NON_TERMINAL_STATES:
            assert len(VALID_TRANSITIONS[state]) > 0, (
                f"Non-terminal state {state} should have outgoing transitions"
            )

    def test_terminal_states_count(self):
        assert len(TERMINAL_STATES) == 4

    def test_non_terminal_states_count(self):
        assert len(NON_TERMINAL_STATES) == 6

    def test_total_states_is_10(self):
        assert len(OrderLifecycleState) == 10


# ══════════════════════════════════════════════════════════════
# 2. VALID TRANSITIONS
# ══════════════════════════════════════════════════════════════

class TestValidTransitions:
    """Test all explicitly valid transitions succeed."""

    @pytest.mark.parametrize("from_state,to_state", [
        (OrderLifecycleState.INTENT_CREATED, OrderLifecycleState.SUBMISSION_ATTEMPTED),
        (OrderLifecycleState.INTENT_CREATED, OrderLifecycleState.REJECTED),
        (OrderLifecycleState.INTENT_CREATED, OrderLifecycleState.FAILED),
        (OrderLifecycleState.SUBMISSION_ATTEMPTED, OrderLifecycleState.ACKNOWLEDGED),
        (OrderLifecycleState.SUBMISSION_ATTEMPTED, OrderLifecycleState.REJECTED),
        (OrderLifecycleState.SUBMISSION_ATTEMPTED, OrderLifecycleState.FAILED),
        (OrderLifecycleState.SUBMISSION_ATTEMPTED, OrderLifecycleState.RECOVERY_PENDING),
        (OrderLifecycleState.ACKNOWLEDGED, OrderLifecycleState.LIVE),
        (OrderLifecycleState.ACKNOWLEDGED, OrderLifecycleState.CANCELLED),
        (OrderLifecycleState.ACKNOWLEDGED, OrderLifecycleState.FAILED),
        (OrderLifecycleState.LIVE, OrderLifecycleState.PARTIALLY_FILLED),
        (OrderLifecycleState.LIVE, OrderLifecycleState.FILLED),
        (OrderLifecycleState.LIVE, OrderLifecycleState.CANCELLED),
        (OrderLifecycleState.LIVE, OrderLifecycleState.FAILED),
        (OrderLifecycleState.PARTIALLY_FILLED, OrderLifecycleState.FILLED),
        (OrderLifecycleState.PARTIALLY_FILLED, OrderLifecycleState.CANCELLED),
        (OrderLifecycleState.PARTIALLY_FILLED, OrderLifecycleState.FAILED),
        (OrderLifecycleState.RECOVERY_PENDING, OrderLifecycleState.ACKNOWLEDGED),
        (OrderLifecycleState.RECOVERY_PENDING, OrderLifecycleState.FILLED),
        (OrderLifecycleState.RECOVERY_PENDING, OrderLifecycleState.CANCELLED),
        (OrderLifecycleState.RECOVERY_PENDING, OrderLifecycleState.FAILED),
    ])
    def test_valid_transition(self, from_state, to_state):
        order = _make_order(state=from_state)
        record = order.transition(to_state, reason="test", timestamp_ms=2000000)

        assert order.state == to_state
        assert isinstance(record, TransitionRecord)
        assert record.from_state == from_state
        assert record.to_state == to_state
        assert record.timestamp_ms == 2000000
        assert len(order.history) == 1


# ══════════════════════════════════════════════════════════════
# 3. INVALID TRANSITIONS
# ══════════════════════════════════════════════════════════════

class TestInvalidTransitions:
    """Test that invalid transitions raise OrderTransitionError."""

    @pytest.mark.parametrize("terminal_state", list(TERMINAL_STATES))
    def test_terminal_to_any(self, terminal_state):
        """No transitions out of terminal states."""
        order = _make_order(state=terminal_state)
        for target in OrderLifecycleState:
            if target == terminal_state:
                continue
            with pytest.raises(OrderTransitionError) as exc_info:
                order.transition(target)
            assert exc_info.value.order_id == "NT-abc123"
            assert exc_info.value.from_state == terminal_state

    def test_intent_to_live_invalid(self):
        """Cannot skip directly from INTENT_CREATED to LIVE."""
        order = _make_order()
        with pytest.raises(OrderTransitionError):
            order.transition(OrderLifecycleState.LIVE)

    def test_intent_to_filled_invalid(self):
        order = _make_order()
        with pytest.raises(OrderTransitionError):
            order.transition(OrderLifecycleState.FILLED)

    def test_acknowledged_to_partially_filled_invalid(self):
        """Must go through LIVE first."""
        order = _make_order(state=OrderLifecycleState.ACKNOWLEDGED)
        with pytest.raises(OrderTransitionError):
            order.transition(OrderLifecycleState.PARTIALLY_FILLED)

    def test_live_to_acknowledged_invalid(self):
        """Can't go backwards."""
        order = _make_order(state=OrderLifecycleState.LIVE)
        with pytest.raises(OrderTransitionError):
            order.transition(OrderLifecycleState.ACKNOWLEDGED)

    def test_state_unchanged_after_invalid_transition(self):
        """State must not change if transition is rejected."""
        order = _make_order(state=OrderLifecycleState.INTENT_CREATED)
        original_state = order.state
        with pytest.raises(OrderTransitionError):
            order.transition(OrderLifecycleState.FILLED)
        assert order.state == original_state
        assert len(order.history) == 0


# ══════════════════════════════════════════════════════════════
# 4. LIVE ORDER LIFECYCLE
# ══════════════════════════════════════════════════════════════

class TestLiveOrderLifecycle:
    """Test full happy-path lifecycle."""

    def test_happy_path_market_order(self):
        """INTENT → SUBMITTED → ACKNOWLEDGED → LIVE → FILLED"""
        order = _make_order()

        order.transition(OrderLifecycleState.SUBMISSION_ATTEMPTED, timestamp_ms=100)
        assert order.submitted_at_ms == 100

        order.transition(
            OrderLifecycleState.ACKNOWLEDGED,
            exchange_order_id="BYBIT-123",
            timestamp_ms=200,
        )
        assert order.exchange_order_id == "BYBIT-123"
        assert order.acknowledged_at_ms == 200

        order.transition(OrderLifecycleState.LIVE, timestamp_ms=300)

        order.record_fill(50100.0, 0.01, 0.28, timestamp_ms=400)
        order.transition(OrderLifecycleState.FILLED, timestamp_ms=400)

        assert order.state == OrderLifecycleState.FILLED
        assert order.is_terminal
        assert order.filled_quantity == 0.01
        assert order.filled_price_avg == 50100.0
        assert order.fee_usdt == 0.28
        assert order.completed_at_ms == 400
        assert len(order.history) == 4

    def test_partial_fill_then_complete(self):
        """LIVE → PARTIALLY_FILLED → FILLED with VWAP tracking."""
        order = _make_order(state=OrderLifecycleState.LIVE)

        # First partial fill: 0.004 @ 50000
        order.record_fill(50000.0, 0.004, 0.11, timestamp_ms=100)
        order.transition(OrderLifecycleState.PARTIALLY_FILLED, timestamp_ms=100)

        assert order.filled_quantity == 0.004
        assert order.filled_price_avg == 50000.0
        assert order.fill_pct == pytest.approx(0.4, abs=0.01)
        assert not order.is_fully_filled

        # Second partial fill: 0.006 @ 50200
        order.record_fill(50200.0, 0.006, 0.17, timestamp_ms=200)
        order.transition(OrderLifecycleState.FILLED, timestamp_ms=200)

        # VWAP: (50000*0.004 + 50200*0.006) / 0.01 = 50120
        assert order.filled_quantity == pytest.approx(0.01)
        assert order.filled_price_avg == pytest.approx(50120.0)
        assert order.fee_usdt == pytest.approx(0.28)
        assert order.is_fully_filled
        assert order.is_terminal

    def test_rejection_path(self):
        """INTENT → REJECTED."""
        order = _make_order()
        order.transition(
            OrderLifecycleState.REJECTED,
            reason="insufficient balance",
            timestamp_ms=100,
        )
        assert order.state == OrderLifecycleState.REJECTED
        assert order.failure_reason == "insufficient balance"
        assert order.is_terminal

    def test_crash_after_submit_path(self):
        """SUBMITTED → RECOVERY_PENDING → FILLED."""
        order = _make_order(state=OrderLifecycleState.SUBMISSION_ATTEMPTED)
        order.transition(
            OrderLifecycleState.RECOVERY_PENDING,
            reason="crash detected",
            timestamp_ms=100,
        )
        assert order.state == OrderLifecycleState.RECOVERY_PENDING

        # Recovery finds order was filled on exchange
        order.record_fill(50050.0, 0.01, 0.28, timestamp_ms=200)
        order.transition(
            OrderLifecycleState.FILLED,
            reason="recovered: order filled during downtime",
            timestamp_ms=200,
        )
        assert order.is_terminal
        assert order.filled_quantity == 0.01

    def test_cancel_after_partial_fill(self):
        """LIVE → PARTIALLY_FILLED → CANCELLED."""
        order = _make_order(state=OrderLifecycleState.LIVE)
        order.record_fill(50000.0, 0.005, 0.14, timestamp_ms=100)
        order.transition(OrderLifecycleState.PARTIALLY_FILLED, timestamp_ms=100)
        order.transition(
            OrderLifecycleState.CANCELLED,
            reason="remaining cancelled by user",
            timestamp_ms=200,
        )
        assert order.state == OrderLifecycleState.CANCELLED
        assert order.filled_quantity == 0.005
        assert order.is_terminal


# ══════════════════════════════════════════════════════════════
# 5. FILL RECORDING
# ══════════════════════════════════════════════════════════════

class TestFillRecording:
    """Test fill tracking and VWAP calculation."""

    def test_single_fill(self):
        order = _make_order()
        order.record_fill(50000.0, 0.01, 0.28, timestamp_ms=100)
        assert order.filled_quantity == 0.01
        assert order.filled_price_avg == 50000.0
        assert order.fill_count == 1

    def test_vwap_two_fills(self):
        order = _make_order()
        order.record_fill(50000.0, 0.005, 0.14, timestamp_ms=100)
        order.record_fill(50400.0, 0.005, 0.14, timestamp_ms=200)
        # VWAP: (50000*0.005 + 50400*0.005) / 0.01 = 50200
        assert order.filled_price_avg == pytest.approx(50200.0)
        assert order.filled_quantity == pytest.approx(0.01)
        assert order.fill_count == 2

    def test_vwap_three_unequal_fills(self):
        order = _make_order(requested_quantity=1.0)
        order.record_fill(100.0, 0.5, 0.03, timestamp_ms=100)
        order.record_fill(110.0, 0.3, 0.02, timestamp_ms=200)
        order.record_fill(105.0, 0.2, 0.01, timestamp_ms=300)
        # VWAP: (100*0.5 + 110*0.3 + 105*0.2) / 1.0 = 104.0
        assert order.filled_price_avg == pytest.approx(104.0)
        assert order.filled_quantity == pytest.approx(1.0)
        assert order.fee_usdt == pytest.approx(0.06)
        assert order.fill_count == 3

    def test_zero_quantity_fill(self):
        order = _make_order()
        order.record_fill(50000.0, 0.0, 0.0, timestamp_ms=100)
        assert order.filled_quantity == 0.0
        assert order.fill_count == 1


# ══════════════════════════════════════════════════════════════
# 6. PROPERTIES
# ══════════════════════════════════════════════════════════════

class TestProperties:
    def test_is_terminal(self):
        for state in TERMINAL_STATES:
            order = _make_order(state=state)
            assert order.is_terminal

    def test_is_not_terminal(self):
        for state in NON_TERMINAL_STATES:
            order = _make_order(state=state)
            assert not order.is_terminal

    def test_fill_pct_empty(self):
        order = _make_order()
        assert order.fill_pct == 0.0

    def test_fill_pct_half(self):
        order = _make_order(requested_quantity=1.0)
        order.record_fill(100.0, 0.5, 0.01)
        assert order.fill_pct == pytest.approx(0.5)

    def test_fill_pct_capped_at_one(self):
        order = _make_order(requested_quantity=1.0)
        order.record_fill(100.0, 1.5, 0.01)
        assert order.fill_pct == 1.0

    def test_slippage_pct(self):
        order = _make_order(requested_price=50000.0)
        order.record_fill(50050.0, 0.01, 0.28)
        assert order.slippage_pct == pytest.approx(0.001, abs=0.0001)

    def test_slippage_zero_when_no_fill(self):
        order = _make_order()
        assert order.slippage_pct == 0.0


# ══════════════════════════════════════════════════════════════
# 7. SERIALIZATION
# ══════════════════════════════════════════════════════════════

class TestSerialization:
    def test_round_trip(self):
        order = _make_order()
        order.transition(OrderLifecycleState.SUBMISSION_ATTEMPTED, timestamp_ms=100)
        order.transition(
            OrderLifecycleState.ACKNOWLEDGED,
            exchange_order_id="EX-123",
            timestamp_ms=200,
        )
        order.transition(OrderLifecycleState.LIVE, timestamp_ms=300)
        order.record_fill(50100.0, 0.01, 0.28, timestamp_ms=400)
        order.transition(OrderLifecycleState.FILLED, timestamp_ms=400)

        d = order.to_dict()
        restored = LiveOrder.from_dict(d)

        assert restored.client_order_id == order.client_order_id
        assert restored.state == order.state
        assert restored.exchange_order_id == order.exchange_order_id
        assert restored.filled_quantity == order.filled_quantity
        assert restored.filled_price_avg == order.filled_price_avg
        assert restored.fee_usdt == order.fee_usdt
        assert len(restored.history) == len(order.history)
        assert restored.history[0].from_state == OrderLifecycleState.INTENT_CREATED

    def test_to_dict_keys(self):
        order = _make_order()
        d = order.to_dict()
        required_keys = {
            "client_order_id", "request_id", "decision_id", "trigger_id",
            "symbol", "side", "order_type", "requested_price",
            "requested_quantity", "size_usdt", "strategy_name", "regime",
            "stop_loss", "take_profit", "state", "exchange_order_id",
            "filled_quantity", "filled_price_avg", "fee_usdt", "fill_count",
            "created_at_ms", "submitted_at_ms", "acknowledged_at_ms",
            "last_fill_at_ms", "completed_at_ms", "failure_reason",
            "retry_count", "history",
        }
        assert required_keys.issubset(set(d.keys()))


# ══════════════════════════════════════════════════════════════
# 8. CLIENT ORDER ID GENERATION
# ══════════════════════════════════════════════════════════════

class TestClientOrderId:
    def test_deterministic(self):
        id1 = make_client_order_id("req-1", "BTCUSDT", "buy", 1000)
        id2 = make_client_order_id("req-1", "BTCUSDT", "buy", 1000)
        assert id1 == id2

    def test_prefix(self):
        cid = make_client_order_id("req-1", "BTCUSDT", "buy", 1000)
        assert cid.startswith("NT-")

    def test_different_inputs_different_ids(self):
        id1 = make_client_order_id("req-1", "BTCUSDT", "buy", 1000)
        id2 = make_client_order_id("req-2", "BTCUSDT", "buy", 1000)
        id3 = make_client_order_id("req-1", "ETHUSDT", "buy", 1000)
        id4 = make_client_order_id("req-1", "BTCUSDT", "sell", 1000)
        id5 = make_client_order_id("req-1", "BTCUSDT", "buy", 2000)
        assert len({id1, id2, id3, id4, id5}) == 5

    def test_length(self):
        cid = make_client_order_id("req-1", "BTCUSDT", "buy", 1000)
        # "NT-" + 16 hex chars = 19
        assert len(cid) == 19


# ══════════════════════════════════════════════════════════════
# 9. AUDIT TRAIL
# ══════════════════════════════════════════════════════════════

class TestAuditTrail:
    def test_transition_records_accumulate(self):
        order = _make_order()
        order.transition(OrderLifecycleState.SUBMISSION_ATTEMPTED, timestamp_ms=100)
        order.transition(OrderLifecycleState.ACKNOWLEDGED, timestamp_ms=200)
        order.transition(OrderLifecycleState.LIVE, timestamp_ms=300)
        order.transition(OrderLifecycleState.FILLED, timestamp_ms=400)

        assert len(order.history) == 4
        states = [(r.from_state, r.to_state) for r in order.history]
        assert states == [
            (OrderLifecycleState.INTENT_CREATED, OrderLifecycleState.SUBMISSION_ATTEMPTED),
            (OrderLifecycleState.SUBMISSION_ATTEMPTED, OrderLifecycleState.ACKNOWLEDGED),
            (OrderLifecycleState.ACKNOWLEDGED, OrderLifecycleState.LIVE),
            (OrderLifecycleState.LIVE, OrderLifecycleState.FILLED),
        ]

    def test_transition_record_serialization(self):
        order = _make_order()
        order.transition(
            OrderLifecycleState.SUBMISSION_ATTEMPTED,
            reason="submitting",
            timestamp_ms=100,
            exchange_order_id="EX-1",
            metadata='{"attempt": 1}',
        )
        d = order.history[0].to_dict()
        assert d["from_state"] == "intent_created"
        assert d["to_state"] == "submission_attempted"
        assert d["reason"] == "submitting"
        assert d["timestamp_ms"] == 100
        assert d["exchange_order_id"] == "EX-1"
        assert d["metadata"] == '{"attempt": 1}'

    def test_timing_fields_set_on_transition(self):
        order = _make_order()
        order.transition(OrderLifecycleState.SUBMISSION_ATTEMPTED, timestamp_ms=100)
        assert order.submitted_at_ms == 100

        order.transition(OrderLifecycleState.ACKNOWLEDGED, timestamp_ms=200)
        assert order.acknowledged_at_ms == 200

        order.transition(OrderLifecycleState.LIVE, timestamp_ms=300)
        order.transition(OrderLifecycleState.FILLED, timestamp_ms=400)
        assert order.completed_at_ms == 400
