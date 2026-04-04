"""
tests/unit/test_phase5_hardening.py
====================================
Phase 5 hardening tests:
  1. partial_close() FilterStatsTracker tracking
  2. Milestone key-mapping correctness (milestone 200 gate keys)
  3. analysis_success_rate scale (percentage, not fraction)
  4. AdaptiveLearningPolicy overfitting protection
  5. AdaptiveLearningPolicy tier thresholds (20/50/100 trade tiers)
"""

import math
import pytest
from unittest.mock import MagicMock, patch, call

from core.monitoring.paper_trading_monitor import (
    AdaptiveLearningPolicy,
    MilestoneTracker,
    LiveReadinessEvaluator,
    LIVE_TRADING_GATES,
    MILESTONES,
)


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def policy():
    return AdaptiveLearningPolicy()


@pytest.fixture
def tracker():
    return MilestoneTracker()


@pytest.fixture
def readiness():
    return LiveReadinessEvaluator()


def _make_stats(**overrides):
    """Return a stats dict with sensible defaults; override any key via kwargs."""
    defaults = {
        "total_trades": 0,
        "win_rate_pct": 50.0,
        "profit_factor": 1.3,
        "expectancy_r": 0.20,
        "avg_r": 0.20,
        "good_trade_pct": 45.0,
        "bad_trade_pct": 30.0,
        "neutral_trade_pct": 25.0,
        "drawdown_r": 3.0,
        "analysis_success_rate": 85.0,   # percentage scale (0-100)
        "notification_reliability": 0.97,  # fraction scale (0-1)
        "bad_decision_pct": 30.0,
        "avoidable_loss_pct": 10.0,
        "execution_quality_score": 70.0,
    }
    defaults.update(overrides)
    return defaults


# ─────────────────────────────────────────────────────────────────────────────
# 1. partial_close() FilterStatsTracker tracking
# ─────────────────────────────────────────────────────────────────────────────

class TestPartialCloseFilterStats:
    """Verify partial_close() correctly records realized R in FilterStatsTracker."""

    def _make_position(self, entry_price=100.0, stop_loss=90.0, quantity=10.0,
                        size_usdt=1000.0, side="buy"):
        """Build a minimal PaperPosition-like mock."""
        pos = MagicMock()
        pos.side = side
        pos.entry_price = entry_price
        pos.stop_loss = stop_loss
        pos.quantity = quantity
        pos.size_usdt = size_usdt
        pos.current_price = entry_price
        pos._initial_risk = abs(entry_price - stop_loss)  # 10.0
        pos.score = 0.6
        pos.rationale = "test"
        pos.regime = "trending_up"
        pos.models_fired = ["TrendModel"]
        pos.timeframe = "1h"
        pos.opened_at = None
        pos.take_profit = entry_price * 1.2
        pos.to_dict.return_value = {}
        pos.entry_expected = None
        pos.expected_value = None
        pos.symbol_weight = 1.0
        pos.adjusted_score = 0.6
        return pos

    def test_partial_close_records_realized_r_for_win(self):
        """
        A profitable partial close (exit > entry for long) should record
        a positive R via record_trade_outcome for both time_of_day and volatility.
        """
        from core.execution.paper_executor import PaperExecutor
        executor = PaperExecutor.__new__(PaperExecutor)
        executor._capital = 100_000.0
        executor._peak_capital = 100_000.0
        executor._closed_trades = []
        executor._positions = {}
        executor._save_trade_to_db = MagicMock()
        executor._save_open_positions = MagicMock()

        pos = self._make_position(entry_price=100.0, stop_loss=90.0,
                                   quantity=10.0, side="buy")
        pos.current_price = 115.0  # exit above entry → win
        executor._positions["BTC/USDT"] = [pos]

        mock_fst = MagicMock()
        with patch("core.analytics.filter_stats.get_filter_stats_tracker",
                   return_value=mock_fst):
            # Patch bus.publish to avoid event bus dependency
            with patch("core.execution.paper_executor.bus") as mock_bus:
                result = executor.partial_close("BTC/USDT", 0.5)

        assert result is True
        # R for 50% close: pnl = (115-100)*5 = 75, risk = 10*5 = 50, R = 1.5
        calls = mock_fst.record_trade_outcome.call_args_list
        assert len(calls) == 2
        for c in calls:
            filter_name, r_val = c[0]
            assert filter_name in ("time_of_day", "volatility")
            assert pytest.approx(r_val, abs=0.01) == 1.5

    def test_partial_close_records_negative_r_for_loss(self):
        """A losing partial close should record a negative R."""
        from core.execution.paper_executor import PaperExecutor
        executor = PaperExecutor.__new__(PaperExecutor)
        executor._capital = 100_000.0
        executor._peak_capital = 100_000.0
        executor._closed_trades = []
        executor._positions = {}
        executor._save_trade_to_db = MagicMock()
        executor._save_open_positions = MagicMock()

        pos = self._make_position(entry_price=100.0, stop_loss=90.0,
                                   quantity=10.0, side="buy")
        pos.current_price = 95.0  # exit below entry → loss
        executor._positions["ETH/USDT"] = [pos]

        mock_fst = MagicMock()
        with patch("core.analytics.filter_stats.get_filter_stats_tracker",
                   return_value=mock_fst):
            with patch("core.execution.paper_executor.bus"):
                result = executor.partial_close("ETH/USDT", 0.5)

        assert result is True
        # pnl = (95-100)*5 = -25, risk = 10*5 = 50, R = -0.5
        for c in mock_fst.record_trade_outcome.call_args_list:
            _, r_val = c[0]
            assert pytest.approx(r_val, abs=0.01) == -0.5

    def test_multiple_partial_closes_no_double_counting(self):
        """
        Two partial closes of 25% each should record 2 outcomes each,
        not 4.  Each call to partial_close is independent.
        """
        from core.execution.paper_executor import PaperExecutor
        executor = PaperExecutor.__new__(PaperExecutor)
        executor._capital = 100_000.0
        executor._peak_capital = 100_000.0
        executor._closed_trades = []
        executor._positions = {}
        executor._save_trade_to_db = MagicMock()
        executor._save_open_positions = MagicMock()

        pos = self._make_position(entry_price=100.0, stop_loss=90.0,
                                   quantity=10.0, side="buy")
        pos.current_price = 110.0
        executor._positions["SOL/USDT"] = [pos]

        mock_fst = MagicMock()
        with patch("core.analytics.filter_stats.get_filter_stats_tracker",
                   return_value=mock_fst):
            with patch("core.execution.paper_executor.bus"):
                executor.partial_close("SOL/USDT", 0.25)  # first partial
                executor.partial_close("SOL/USDT", 0.25)  # second partial

        # 2 calls per partial × 2 partials = 4 total record_trade_outcome calls
        assert mock_fst.record_trade_outcome.call_count == 4

    def test_partial_close_fst_exception_non_fatal(self):
        """FilterStatsTracker failure must not raise — partial close still returns True."""
        from core.execution.paper_executor import PaperExecutor
        executor = PaperExecutor.__new__(PaperExecutor)
        executor._capital = 100_000.0
        executor._peak_capital = 100_000.0
        executor._closed_trades = []
        executor._positions = {}
        executor._save_trade_to_db = MagicMock()
        executor._save_open_positions = MagicMock()

        pos = self._make_position(entry_price=100.0, stop_loss=90.0,
                                   quantity=10.0, side="buy")
        pos.current_price = 110.0
        executor._positions["BNB/USDT"] = [pos]

        with patch("core.analytics.filter_stats.get_filter_stats_tracker",
                   side_effect=RuntimeError("DB gone")):
            with patch("core.execution.paper_executor.bus"):
                result = executor.partial_close("BNB/USDT", 0.5)

        assert result is True  # must not propagate the exception

    def test_r_formula_zero_initial_risk(self):
        """If _initial_risk == 0 (stop == entry), R is 0.0, not a division error."""
        from core.execution.paper_executor import PaperExecutor
        executor = PaperExecutor.__new__(PaperExecutor)
        executor._capital = 100_000.0
        executor._peak_capital = 100_000.0
        executor._closed_trades = []
        executor._positions = {}
        executor._save_trade_to_db = MagicMock()
        executor._save_open_positions = MagicMock()

        pos = self._make_position(entry_price=100.0, stop_loss=100.0,
                                   quantity=10.0, side="buy")
        pos._initial_risk = 0.0  # degenerate case
        pos.current_price = 110.0
        executor._positions["XRP/USDT"] = [pos]

        mock_fst = MagicMock()
        with patch("core.analytics.filter_stats.get_filter_stats_tracker",
                   return_value=mock_fst):
            with patch("core.execution.paper_executor.bus"):
                result = executor.partial_close("XRP/USDT", 0.5)

        assert result is True
        for c in mock_fst.record_trade_outcome.call_args_list:
            _, r_val = c[0]
            assert r_val == 0.0


# ─────────────────────────────────────────────────────────────────────────────
# 2. Milestone key-mapping correctness
# ─────────────────────────────────────────────────────────────────────────────

class TestMilestoneKeyMapping:
    """Gate keys like min_win_rate_pct must resolve against stats keys like win_rate_pct."""

    def test_milestone_200_no_missing_keys(self, tracker):
        """
        With a stats dict using standard keys (win_rate_pct, etc.) milestone 200
        should produce zero MISSING checks — all gate keys must resolve.
        """
        stats = _make_stats(total_trades=200)
        result = tracker._check_milestone(200, stats)
        missing = [c for c in result["checks_failed"] if "MISSING" in c]
        assert missing == [], f"Unexpected MISSING keys: {missing}"

    def test_milestone_200_win_rate_passes_when_above_threshold(self, tracker):
        stats = _make_stats(
            total_trades=200,
            win_rate_pct=50.0,       # > min_win_rate_pct 45.0
            profit_factor=1.2,       # > 1.10
            notification_reliability=0.97,  # > 0.95
            analysis_success_rate=92.0,     # percentage, corresponds to 0.92 > 0.90
        )
        result = tracker._check_milestone(200, stats)
        win_failures = [c for c in result["checks_failed"] if "win_rate" in c]
        assert win_failures == []

    def test_milestone_200_win_rate_fails_when_below_threshold(self, tracker):
        stats = _make_stats(total_trades=200, win_rate_pct=40.0)
        result = tracker._check_milestone(200, stats)
        win_failures = [c for c in result["checks_failed"] if "win_rate" in c]
        assert len(win_failures) == 1

    def test_milestone_20_uses_percentage_scale_analysis_success(self, tracker):
        """
        analysis_success_rate in stats is percentage (85.0).
        MILESTONES[20] threshold is 80.0 (also percentage).
        Should pass.
        """
        stats = _make_stats(total_trades=20, analysis_success_rate=85.0)
        result = tracker._check_milestone(20, stats)
        analysis_failures = [c for c in result["checks_failed"]
                              if "analysis_success_rate" in c]
        assert analysis_failures == [], (
            "analysis_success_rate=85.0 should pass threshold 80.0 "
            f"but got: {analysis_failures}"
        )

    def test_milestone_20_analysis_success_fails_below_threshold(self, tracker):
        stats = _make_stats(total_trades=20, analysis_success_rate=70.0)
        result = tracker._check_milestone(20, stats)
        analysis_failures = [c for c in result["checks_failed"]
                              if "analysis_success_rate" in c]
        assert len(analysis_failures) == 1

    def test_analysis_success_rate_fraction_in_stats_still_handled(self, tracker):
        """
        If a caller accidentally passes analysis_success_rate as a fraction (0.85)
        but the gate threshold is expressed as a fraction (<=1), the normalisation
        path should not double-divide.  Only applies when gate threshold is <= 1.0.
        """
        # For milestone 20 threshold is 80.0 (percentage); passing 0.85 fraction
        # would be incorrect usage, but the comparison should just fail (0.85 < 80.0),
        # not cause an exception.
        stats = _make_stats(total_trades=20, analysis_success_rate=0.85)
        result = tracker._check_milestone(20, stats)
        # Should not raise; result should contain a failure (0.85 < 80.0)
        analysis_failures = [c for c in result["checks_failed"]
                              if "analysis_success_rate" in c]
        assert len(analysis_failures) == 1

    def test_evaluate_readiness_notification_reliability_fraction(self, readiness):
        """
        LiveReadinessEvaluator already had mapping; verify it still works correctly.
        notification_reliability passed as fraction (0.97) vs gate threshold 0.95.
        """
        stats = _make_stats(total_trades=200, notification_reliability=0.97)
        result = readiness.evaluate(stats)
        notif_failures = [g for g in result["gates_failed"]
                          if g["gate"] == "min_notification_reliability"]
        assert notif_failures == [], f"notification_reliability=0.97 should pass 0.95"


# ─────────────────────────────────────────────────────────────────────────────
# 3. AdaptiveLearningPolicy — tier thresholds
# ─────────────────────────────────────────────────────────────────────────────

class TestAdaptiveLearningPolicyTiers:
    """Verify the four-tier proposal/auto-apply behaviour."""

    def test_tier_below_20_suppresses_all(self, policy):
        for n in (0, 1, 10, 19):
            assert policy.should_suppress_proposals(n) is True, f"failed at n={n}"

    def test_tier_at_20_allows_proposals(self, policy):
        assert policy.should_suppress_proposals(20) is False

    def test_tier_20_to_99_no_auto_apply(self, policy):
        """20–99 trades: proposals generated but auto-apply always blocked."""
        proposal = {
            "category": "time_of_day_filter",
            "confidence": 0.80,
            "risk_level": "low",
        }
        for n in (20, 25, 50, 75, 99):
            can, reason = policy.can_auto_apply(proposal, n)
            assert can is False, f"should not auto-apply at {n} trades"
            assert str(policy.MIN_TRADES_FOR_AUTO_APPLY) in reason

    def test_tier_100_allows_auto_apply_for_low_risk(self, policy):
        proposal = {
            "category": "time_of_day_filter",
            "confidence": 0.80,
            "risk_level": "low",
        }
        can, reason = policy.can_auto_apply(proposal, 110)
        assert can is True

    def test_always_manual_blocked_even_at_200_trades(self, policy):
        for cat in policy.ALWAYS_MANUAL_CATEGORIES:
            proposal = {"category": cat, "confidence": 0.95, "risk_level": "low"}
            can, _ = policy.can_auto_apply(proposal, 200)
            assert can is False, f"{cat} should always be manual"


# ─────────────────────────────────────────────────────────────────────────────
# 4. AdaptiveLearningPolicy — overfitting protection
# ─────────────────────────────────────────────────────────────────────────────

class TestOverfitProtection:
    """Verify is_overfit_risk() and filter_proposals_for_review() drop bad proposals."""

    def _good_proposal(self, **overrides):
        base = {
            "category": "time_of_day_filter",
            "confidence": 0.75,
            "risk_level": "low",
            "category_trade_count": 20,
            "scope": "symbol",
            "symbols": ["BTC/USDT"],
        }
        base.update(overrides)
        return base

    def test_too_few_category_trades_is_overfit(self, policy):
        p = self._good_proposal(category_trade_count=5)
        assert policy.is_overfit_risk(p, 50) is True

    def test_sufficient_category_trades_not_overfit(self, policy):
        p = self._good_proposal(category_trade_count=15)
        assert policy.is_overfit_risk(p, 50) is False

    def test_cross_symbol_single_symbol_is_overfit(self, policy):
        p = self._good_proposal(
            scope="cross_symbol",
            symbols=["BTC/USDT"],  # only 1 symbol
        )
        assert policy.is_overfit_risk(p, 50) is True

    def test_cross_symbol_two_symbols_not_overfit(self, policy):
        p = self._good_proposal(
            scope="cross_symbol",
            symbols=["BTC/USDT", "ETH/USDT"],
        )
        assert policy.is_overfit_risk(p, 50) is False

    def test_high_confidence_tiny_sample_is_overfit(self, policy):
        p = self._good_proposal(category_trade_count=8, confidence=0.95)
        assert policy.is_overfit_risk(p, 50) is True

    def test_high_confidence_adequate_sample_not_overfit(self, policy):
        p = self._good_proposal(category_trade_count=20, confidence=0.95)
        assert policy.is_overfit_risk(p, 50) is False

    def test_no_category_count_passes_through(self, policy):
        """Proposals without category_trade_count are not subject to rule 1."""
        p = {"category": "time_of_day_filter", "confidence": 0.70,
             "risk_level": "low", "scope": "symbol"}
        assert policy.is_overfit_risk(p, 50) is False

    def test_filter_removes_overfit_proposals(self, policy):
        good = self._good_proposal(category_trade_count=15)
        bad1 = self._good_proposal(category_trade_count=3)
        bad2 = self._good_proposal(scope="cross_symbol", symbols=["BTC/USDT"])
        result = policy.filter_proposals_for_review([good, bad1, bad2], total_trades=30)
        assert good in result
        assert bad1 not in result
        assert bad2 not in result

    def test_filter_still_suppresses_below_20_trades(self, policy):
        good = self._good_proposal(category_trade_count=15)
        result = policy.filter_proposals_for_review([good], total_trades=15)
        assert result == []


# ─────────────────────────────────────────────────────────────────────────────
# 5. Milestone evaluate() integration — correct milestone triggered
# ─────────────────────────────────────────────────────────────────────────────

class TestMilestoneEvaluateIntegration:
    """End-to-end milestone evaluation with realistic stats."""

    def test_no_milestone_below_20_trades(self, tracker):
        stats = _make_stats(total_trades=10)
        result = tracker.evaluate(stats)
        assert result["milestone_reached"] is None
        assert result["overall_pass"] is True  # advisory_only when no milestone

    def test_milestone_20_reached_and_advisory(self, tracker):
        stats = _make_stats(total_trades=20)
        result = tracker.evaluate(stats)
        assert result["milestone_reached"] == 20
        assert result["advisory_only"] is True

    def test_milestone_50_reached(self, tracker):
        stats = _make_stats(total_trades=55)
        result = tracker.evaluate(stats)
        assert result["milestone_reached"] == 50

    def test_milestone_200_all_gates_pass(self, tracker):
        stats = _make_stats(
            total_trades=205,
            win_rate_pct=50.0,
            profit_factor=1.20,
            expectancy_r=0.20,
            avg_r=0.15,
            good_trade_pct=50.0,
            bad_trade_pct=25.0,
            drawdown_r=5.0,
            notification_reliability=0.97,
            analysis_success_rate=92.0,
            execution_quality_score=70.0,
        )
        result = tracker.evaluate(stats)
        assert result["milestone_reached"] == 200
        assert len(result["checks_failed"]) == 0, (
            f"Unexpected failures: {result['checks_failed']}"
        )
        assert result["overall_pass"] is True
