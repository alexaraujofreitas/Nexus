# ============================================================
# Phase 2 Addendum — Interface Contract Tests
#
# Validates:
#   1. Signal schema validation (strategy → processing boundary)
#   2. Trade decision schema validation (processing → execution boundary)
#   3. Execution request validation (must be approved)
#   4. Invalid payloads are rejected with clear error messages
#   5. EventBus topic layer ownership enforcement
#   6. Layer boundary violation detection
#   7. Contract enums constrain values correctly
#   8. to_execution_request() conversion with validation
# ============================================================
import unittest
from datetime import datetime, timedelta
from dataclasses import dataclass
from typing import Optional

from core.contracts import (
    Direction, Side, SignalLayer, TradeDecisionStatus,
    VALID_MODEL_NAMES, VALID_REGIMES, VALID_TIMEFRAMES,
    validate_signal, validate_signal_strict,
    validate_trade_decision, validate_trade_decision_strict,
    validate_execution_request, validate_execution_request_strict,
    ContractViolation,
    get_topic_owner, check_topic_boundary,
    TOPIC_LAYER_OWNERSHIP,
    to_execution_request, ExecutionRequestContract,
)
from core.meta_decision.order_candidate import ModelSignal, OrderCandidate


class TestSignalValidation(unittest.TestCase):
    """Test strategy → processing boundary: ModelSignal validation."""

    def _make_valid_signal(self, **overrides):
        defaults = dict(
            symbol="BTC/USDT",
            model_name="momentum_breakout",
            direction="long",
            strength=0.75,
            entry_price=50000.0,
            stop_loss=49000.0,
            take_profit=52000.0,
            timeframe="30m",
            regime="bull_trend",
            rationale="Test signal",
            atr_value=400.0,
        )
        defaults.update(overrides)
        return ModelSignal(**defaults)

    def test_valid_signal_passes(self):
        sig = self._make_valid_signal()
        violations = validate_signal(sig)
        self.assertEqual(violations, [])

    def test_invalid_symbol_format(self):
        sig = self._make_valid_signal(symbol="BTCUSDT")
        violations = validate_signal(sig)
        self.assertTrue(any("symbol" in v.lower() for v in violations))

    def test_invalid_model_name(self):
        sig = self._make_valid_signal(model_name="nonexistent_model")
        violations = validate_signal(sig)
        self.assertTrue(any("model_name" in v for v in violations))

    def test_invalid_direction(self):
        sig = self._make_valid_signal(direction="sideways")
        violations = validate_signal(sig)
        self.assertTrue(any("direction" in v for v in violations))

    def test_strength_out_of_range(self):
        sig = self._make_valid_signal(strength=1.5)
        violations = validate_signal(sig)
        self.assertTrue(any("strength" in v for v in violations))

    def test_negative_entry_price(self):
        sig = self._make_valid_signal(entry_price=-100.0)
        violations = validate_signal(sig)
        self.assertTrue(any("entry_price" in v for v in violations))

    def test_long_sl_above_entry_rejected(self):
        """Long signal: SL must be below entry."""
        sig = self._make_valid_signal(
            direction="long", entry_price=50000.0, stop_loss=51000.0
        )
        violations = validate_signal(sig)
        self.assertTrue(any("stop_loss" in v for v in violations))

    def test_long_tp_below_entry_rejected(self):
        """Long signal: TP must be above entry."""
        sig = self._make_valid_signal(
            direction="long", entry_price=50000.0, take_profit=49000.0
        )
        violations = validate_signal(sig)
        self.assertTrue(any("take_profit" in v for v in violations))

    def test_short_sl_below_entry_rejected(self):
        """Short signal: SL must be above entry."""
        sig = self._make_valid_signal(
            direction="short", entry_price=50000.0,
            stop_loss=49000.0, take_profit=48000.0
        )
        violations = validate_signal(sig)
        self.assertTrue(any("stop_loss" in v for v in violations))

    def test_short_tp_above_entry_rejected(self):
        """Short signal: TP must be below entry."""
        sig = self._make_valid_signal(
            direction="short", entry_price=50000.0,
            stop_loss=51000.0, take_profit=52000.0
        )
        violations = validate_signal(sig)
        self.assertTrue(any("take_profit" in v for v in violations))

    def test_invalid_timeframe(self):
        sig = self._make_valid_signal(timeframe="7m")
        violations = validate_signal(sig)
        self.assertTrue(any("timeframe" in v for v in violations))

    def test_strict_raises_on_invalid(self):
        sig = self._make_valid_signal(direction="invalid")
        with self.assertRaises(ContractViolation):
            validate_signal_strict(sig)

    def test_strict_passes_on_valid(self):
        sig = self._make_valid_signal()
        validate_signal_strict(sig)  # Should not raise

    def test_missing_field_detected(self):
        """Object missing required field is caught."""
        @dataclass
        class PartialSignal:
            symbol: str = "BTC/USDT"
            model_name: str = "trend"
            # direction is missing
        violations = validate_signal(PartialSignal())
        self.assertTrue(any("Missing" in v for v in violations))

    def test_multiple_violations_reported(self):
        """Multiple issues are all reported, not just the first."""
        sig = self._make_valid_signal(
            symbol="BAD", direction="invalid", strength=5.0, entry_price=-1
        )
        violations = validate_signal(sig)
        self.assertGreater(len(violations), 2, "Should report multiple violations")


class TestTradeDecisionValidation(unittest.TestCase):
    """Test processing → execution boundary: OrderCandidate validation."""

    def _make_valid_candidate(self, **overrides):
        defaults = dict(
            symbol="ETH/USDT",
            side="buy",
            entry_type="market",
            entry_price=3000.0,
            stop_loss_price=2900.0,
            take_profit_price=3200.0,
            position_size_usdt=500.0,
            score=0.72,
            models_fired=["momentum_breakout"],
            regime="bull_trend",
            rationale="Test candidate",
            timeframe="30m",
            atr_value=50.0,
        )
        defaults.update(overrides)
        return OrderCandidate(**defaults)

    def test_valid_candidate_passes(self):
        c = self._make_valid_candidate()
        violations = validate_trade_decision(c)
        self.assertEqual(violations, [])

    def test_invalid_side(self):
        c = self._make_valid_candidate(side="hold")
        violations = validate_trade_decision(c)
        self.assertTrue(any("side" in v for v in violations))

    def test_negative_size_rejected(self):
        c = self._make_valid_candidate(position_size_usdt=-100.0)
        violations = validate_trade_decision(c)
        self.assertTrue(any("position_size_usdt" in v for v in violations))

    def test_score_above_1_rejected(self):
        c = self._make_valid_candidate(score=1.5)
        violations = validate_trade_decision(c)
        self.assertTrue(any("score" in v for v in violations))

    def test_zero_stop_loss_rejected(self):
        c = self._make_valid_candidate(stop_loss_price=0.0)
        violations = validate_trade_decision(c)
        self.assertTrue(any("stop_loss_price" in v for v in violations))

    def test_strict_raises_on_invalid(self):
        c = self._make_valid_candidate(side="invalid")
        with self.assertRaises(ContractViolation):
            validate_trade_decision_strict(c)


class TestExecutionRequestValidation(unittest.TestCase):
    """Test that execution layer only accepts approved candidates."""

    def _make_approved_candidate(self, **overrides):
        defaults = dict(
            symbol="SOL/USDT",
            side="buy",
            entry_type="market",
            entry_price=150.0,
            stop_loss_price=145.0,
            take_profit_price=160.0,
            position_size_usdt=200.0,
            score=0.65,
            models_fired=["momentum_breakout"],
            regime="bull_trend",
            rationale="Approved test",
            timeframe="30m",
            atr_value=3.0,
        )
        defaults.update(overrides)
        c = OrderCandidate(**defaults)
        c.approved = True
        return c

    def test_approved_candidate_passes(self):
        c = self._make_approved_candidate()
        violations = validate_execution_request(c)
        self.assertEqual(violations, [])

    def test_unapproved_candidate_rejected(self):
        """Execution layer MUST reject unapproved candidates."""
        c = self._make_approved_candidate()
        c.approved = False
        violations = validate_execution_request(c)
        self.assertTrue(any("approved" in v for v in violations))

    def test_low_rr_ratio_rejected(self):
        """Execution requires R:R >= 1.0."""
        c = self._make_approved_candidate(
            entry_price=150.0, stop_loss_price=140.0, take_profit_price=155.0
        )
        c.approved = True
        # R:R = 5/10 = 0.5 (auto-computed by __post_init__)
        violations = validate_execution_request(c)
        self.assertTrue(any("risk_reward_ratio" in v for v in violations))

    def test_strict_raises_on_unapproved(self):
        c = self._make_approved_candidate()
        c.approved = False
        with self.assertRaises(ContractViolation):
            validate_execution_request_strict(c)

    def test_to_execution_request_succeeds(self):
        """Valid approved candidate converts to ExecutionRequestContract."""
        c = self._make_approved_candidate()
        req = to_execution_request(c)
        self.assertIsInstance(req, ExecutionRequestContract)
        self.assertEqual(req.symbol, "SOL/USDT")
        self.assertEqual(req.side, "buy")
        self.assertGreater(req.size_usdt, 0)

    def test_to_execution_request_rejects_unapproved(self):
        """to_execution_request raises if candidate not approved."""
        c = self._make_approved_candidate()
        c.approved = False
        with self.assertRaises(ContractViolation):
            to_execution_request(c)


class TestEventBusTopicOwnership(unittest.TestCase):
    """Test that EventBus topics are correctly assigned to layers."""

    def test_raw_transport_owned_by_connectivity(self):
        self.assertEqual(
            get_topic_owner("market.tick"), SignalLayer.CONNECTIVITY
        )
        self.assertEqual(
            get_topic_owner("market.orderbook"), SignalLayer.CONNECTIVITY
        )

    def test_candle_topics_owned_by_data(self):
        self.assertEqual(
            get_topic_owner("market.candle.1m"), SignalLayer.DATA
        )

    def test_strategy_topics_owned_by_strategy(self):
        self.assertEqual(
            get_topic_owner("strategy.signal"), SignalLayer.STRATEGY
        )
        self.assertEqual(
            get_topic_owner("strategy.setup_qualified"), SignalLayer.STRATEGY
        )

    def test_agent_topics_owned_by_processing(self):
        self.assertEqual(
            get_topic_owner("agent.funding_rate"), SignalLayer.PROCESSING
        )
        self.assertEqual(
            get_topic_owner("orchestrator.signal"), SignalLayer.PROCESSING
        )

    def test_trade_topics_owned_by_execution(self):
        self.assertEqual(
            get_topic_owner("trade.opened"), SignalLayer.EXECUTION
        )
        self.assertEqual(
            get_topic_owner("order.placed"), SignalLayer.EXECUTION
        )

    def test_ui_topics_owned_by_ui(self):
        self.assertEqual(
            get_topic_owner("ui.page_changed"), SignalLayer.UI
        )

    def test_unknown_topic_returns_none(self):
        self.assertIsNone(get_topic_owner("unknown.topic"))

    def test_boundary_violation_detected(self):
        """Strategy layer cannot publish to execution topics."""
        violation = check_topic_boundary("trade.opened", SignalLayer.STRATEGY)
        self.assertIsNotNone(violation)
        self.assertIn("boundary violation", violation.lower())

    def test_valid_publish_allowed(self):
        """DATA layer can publish candle topics; CONNECTIVITY can publish raw transport."""
        violation = check_topic_boundary("market.candle.1m", SignalLayer.DATA)
        self.assertIsNone(violation)
        violation2 = check_topic_boundary("market.tick", SignalLayer.CONNECTIVITY)
        self.assertIsNone(violation2)

    def test_execution_cannot_publish_strategy(self):
        violation = check_topic_boundary("strategy.signal", SignalLayer.EXECUTION)
        self.assertIsNotNone(violation)

    def test_processing_can_publish_agent(self):
        violation = check_topic_boundary("agent.funding_rate", SignalLayer.PROCESSING)
        self.assertIsNone(violation)


class TestContractEnums(unittest.TestCase):
    """Test enum correctness and conversions."""

    def test_direction_values(self):
        self.assertEqual(Direction.LONG.value, "long")
        self.assertEqual(Direction.SHORT.value, "short")

    def test_side_from_direction(self):
        self.assertEqual(Side.from_direction(Direction.LONG), Side.BUY)
        self.assertEqual(Side.from_direction(Direction.SHORT), Side.SELL)

    def test_trade_decision_status_lifecycle(self):
        """All lifecycle states exist."""
        states = {s.value for s in TradeDecisionStatus}
        self.assertIn("proposed", states)
        self.assertIn("approved", states)
        self.assertIn("rejected", states)
        self.assertIn("executed", states)
        self.assertIn("expired", states)

    def test_valid_model_names_includes_active_models(self):
        """All currently active models are in the valid set."""
        active = {"momentum_breakout", "funding_rate", "sentiment",
                  "pullback_long", "swing_low_continuation"}
        self.assertTrue(active.issubset(VALID_MODEL_NAMES))

    def test_valid_timeframes_includes_intraday(self):
        """Intraday timeframes are in the valid set."""
        intraday = {"1m", "3m", "5m", "15m", "30m"}
        self.assertTrue(intraday.issubset(VALID_TIMEFRAMES))


class TestStrategyCannotCallExecution(unittest.TestCase):
    """
    Prove that strategy layer (signal models) cannot directly call
    execution layer. Strategies produce ModelSignal objects only.
    """

    def test_model_signal_has_no_execute_method(self):
        """ModelSignal is a data object — no execution methods."""
        sig = ModelSignal(
            symbol="BTC/USDT", model_name="momentum_breakout",
            direction="long", strength=0.8, entry_price=50000.0,
            stop_loss=49000.0, take_profit=52000.0, timeframe="30m",
            regime="bull_trend", rationale="test", atr_value=400.0,
        )
        self.assertFalse(hasattr(sig, "execute"))
        self.assertFalse(hasattr(sig, "submit"))
        self.assertFalse(hasattr(sig, "place_order"))

    def test_order_candidate_is_not_executable_until_approved(self):
        """OrderCandidate starts unapproved — cannot pass execution validation."""
        c = OrderCandidate(
            symbol="BTC/USDT", side="buy", entry_type="market",
            entry_price=50000.0, stop_loss_price=49000.0,
            take_profit_price=52000.0, position_size_usdt=500.0,
            score=0.7, models_fired=["momentum_breakout"],
            regime="bull_trend", rationale="test", timeframe="30m",
            atr_value=400.0,
        )
        self.assertFalse(c.approved)
        violations = validate_execution_request(c)
        self.assertTrue(len(violations) > 0, "Unapproved candidate must not pass execution validation")


if __name__ == "__main__":
    unittest.main()
