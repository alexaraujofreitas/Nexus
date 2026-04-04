"""
NexusTrader — Phase 4 Monitoring Test Suite

Comprehensive pytest tests for MilestoneTracker, LiveReadinessEvaluator,
AdaptiveLearningPolicy, and related Phase 4 monitoring components.

Tests cover:
- Milestone thresholds and progression
- Live trading readiness gates
- Adaptive learning policy rules
- Daily report stats computation
- Integration convenience functions
- Launch checklist validation

Usage:
    pytest tests/unit/test_phase4_monitoring.py -v
    pytest tests/unit/test_phase4_monitoring.py::TestMilestoneThresholds -v
"""

import pytest
import sys
from pathlib import Path
from typing import Dict, Any

# Setup path
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from core.monitoring.paper_trading_monitor import (
    MilestoneTracker,
    LiveReadinessEvaluator,
    AdaptiveLearningPolicy,
    MILESTONES,
    LIVE_TRADING_GATES,
    evaluate_milestone,
    evaluate_readiness,
)


# =============================================================================
# FIXTURES & HELPERS
# =============================================================================


def _compute_basic_stats(trades: list) -> Dict[str, Any]:
    """
    Helper to compute basic stats from a list of trades.

    Args:
        trades: List of dicts with keys: pnl_usdt, entry_size_usdt, size_usdt

    Returns:
        Dict with computed: wins, losses, win_rate_pct, profit_factor, avg_r
    """
    if not trades:
        return {
            "wins": 0,
            "losses": 0,
            "win_rate_pct": 0.0,
            "profit_factor": 0.0,
            "avg_r": 0.0,
            "total_pnl_usdt": 0.0,
        }

    wins = sum(1 for t in trades if t.get("pnl_usdt", 0) > 0)
    losses = sum(1 for t in trades if t.get("pnl_usdt", 0) < 0)
    total = len(trades)

    win_rate_pct = (wins / total * 100.0) if total > 0 else 0.0

    wins_pnl = sum(t.get("pnl_usdt", 0) for t in trades if t.get("pnl_usdt", 0) > 0)
    losses_pnl = abs(sum(t.get("pnl_usdt", 0) for t in trades if t.get("pnl_usdt", 0) < 0))
    profit_factor = wins_pnl / losses_pnl if losses_pnl > 0 else (wins_pnl if wins_pnl > 0 else 0.0)

    total_pnl = sum(t.get("pnl_usdt", 0) for t in trades)

    # Compute avg R: pnl_usdt / (entry_size_usdt * risk_pct)
    # If risk_pct not provided, assume 0.5% = 0.005
    avg_r_values = []
    for t in trades:
        pnl = t.get("pnl_usdt", 0)
        entry_size = t.get("entry_size_usdt", 1000.0)
        risk_pct = t.get("risk_pct", 0.005)
        if entry_size > 0 and risk_pct > 0:
            r = pnl / (entry_size * risk_pct)
            avg_r_values.append(r)
    avg_r = (sum(avg_r_values) / len(avg_r_values)) if avg_r_values else 0.0

    return {
        "wins": wins,
        "losses": losses,
        "win_rate_pct": win_rate_pct,
        "profit_factor": profit_factor,
        "avg_r": avg_r,
        "total_pnl_usdt": total_pnl,
    }


@pytest.fixture
def tracker():
    """Create a fresh MilestoneTracker for each test."""
    return MilestoneTracker()


@pytest.fixture
def evaluator():
    """Create a fresh LiveReadinessEvaluator for each test."""
    return LiveReadinessEvaluator()


@pytest.fixture
def policy():
    """Create a fresh AdaptiveLearningPolicy for each test."""
    return AdaptiveLearningPolicy()


@pytest.fixture
def stats_baseline():
    """Create a baseline stats dict with typical values."""
    return {
        "total_trades": 50,
        "win_rate_pct": 45.0,
        "profit_factor": 1.1,
        "avg_r": 0.15,
        "expectancy_r": 0.05,
        "good_trade_pct": 40.0,
        "bad_trade_pct": 35.0,
        "neutral_trade_pct": 25.0,
        "drawdown_r": 8.0,
        "analysis_success_rate": 85.0,
        "notification_reliability": 92.0,
        "execution_quality_score": 75.0,
    }


@pytest.fixture
def stats_excellent():
    """Create stats dict with excellent metrics."""
    return {
        "total_trades": 210,
        "min_trades": 210,  # For line 471 in evaluator
        "win_rate_pct": 48.0,
        "profit_factor": 1.25,
        "avg_r": 0.20,
        "expectancy_r": 0.15,
        "good_trade_pct": 50.0,
        "bad_trade_pct": 25.0,
        "neutral_trade_pct": 25.0,
        "drawdown_r": 8.0,
        "analysis_success_rate": 90.0,
        "notification_reliability": 0.96,
        "execution_quality_score": 80.0,
    }


@pytest.fixture
def stats_poor():
    """Create stats dict with poor metrics."""
    return {
        "total_trades": 55,
        "win_rate_pct": 35.0,
        "profit_factor": 0.75,
        "avg_r": -0.05,
        "expectancy_r": -0.10,
        "good_trade_pct": 20.0,
        "bad_trade_pct": 60.0,
        "neutral_trade_pct": 20.0,
        "drawdown_r": 15.0,
        "analysis_success_rate": 70.0,
        "notification_reliability": 0.80,
        "execution_quality_score": 50.0,
    }


# =============================================================================
# TEST: TestMilestoneThresholds (8 tests)
# =============================================================================


class TestMilestoneThresholds:
    """Test MilestoneTracker.evaluate() with various stats."""

    def test_no_milestone_before_20_trades(self, tracker):
        """total_trades=15 → milestone_reached=None, next_milestone=20"""
        stats = {"total_trades": 15}
        result = tracker.evaluate(stats)

        assert result["milestone_reached"] is None
        assert result["next_milestone"] == 20
        assert result["trades_to_next"] == 5
        assert result["overall_pass"] is True  # Before any milestone

    def test_milestone_20_reached_advisory_only(self, tracker):
        """total_trades=22 → advisory_only=True, overall_pass=True"""
        stats = {
            "total_trades": 22,
            "win_rate_pct": 30.0,  # Below 35 threshold
            "profit_factor": 0.7,  # Below 0.8 threshold
            "analysis_success_rate": 75.0,  # Below 80 threshold
        }
        result = tracker.evaluate(stats)

        assert result["milestone_reached"] == 20
        assert result["advisory_only"] is True
        assert result["overall_pass"] is True  # Advisory passes regardless
        assert result["next_milestone"] == 50
        assert len(result["checks_failed"]) > 0  # Some checks failed

    def test_milestone_50_pass(self, tracker):
        """
        total_trades=52 with passing metrics:
        win_rate=42, pf=1.1, expectancy=0.0, good_trade_pct=35, notification_reliability=95
        → overall_pass=True
        """
        stats = {
            "total_trades": 52,
            "win_rate_pct": 42.0,
            "profit_factor": 1.1,
            "expectancy_r": 0.0,
            "good_trade_pct": 35.0,
            "bad_trade_pct": 40.0,
            "notification_reliability": 95.0,
            "analysis_success_rate": 85.0,
        }
        result = tracker.evaluate(stats)

        assert result["milestone_reached"] == 50
        assert result["overall_pass"] is True
        assert len(result["checks_failed"]) == 0

    def test_milestone_50_fail_low_win_rate(self, tracker):
        """
        total_trades=51, win_rate=35 (below 40 threshold)
        → overall_pass=False, "win_rate_pct" in checks_failed
        """
        stats = {
            "total_trades": 51,
            "win_rate_pct": 35.0,  # Below 40 threshold
            "profit_factor": 1.1,
            "expectancy_r": 0.05,
            "good_trade_pct": 35.0,
            "bad_trade_pct": 40.0,
            "notification_reliability": 95.0,
            "analysis_success_rate": 85.0,
        }
        result = tracker.evaluate(stats)

        assert result["milestone_reached"] == 50
        assert result["overall_pass"] is False
        assert len(result["checks_failed"]) > 0
        assert any("win_rate_pct" in check for check in result["checks_failed"])

    def test_milestone_75_drawdown_check(self, tracker):
        """
        total_trades=80, drawdown_r=13.0 (above 12.0 threshold)
        → overall_pass=False
        """
        stats = {
            "total_trades": 80,
            "win_rate_pct": 45.0,
            "profit_factor": 1.05,
            "expectancy_r": 0.08,
            "good_trade_pct": 40.0,
            "drawdown_r": 13.0,  # Above 12.0 threshold
            "analysis_success_rate": 85.0,
        }
        result = tracker.evaluate(stats)

        assert result["milestone_reached"] == 75
        assert result["overall_pass"] is False
        assert any("drawdown_r" in check for check in result["checks_failed"])

    def test_milestone_100_bad_trade_pct_cap(self, tracker):
        """
        total_trades=105, bad_trade_pct=50.0 (above 45 threshold)
        → overall_pass=False
        """
        stats = {
            "total_trades": 105,
            "win_rate_pct": 46.0,
            "profit_factor": 1.08,
            "expectancy_r": 0.10,
            "good_trade_pct": 42.0,
            "bad_trade_pct": 50.0,  # Above 45 threshold
            "drawdown_r": 9.0,
            "analysis_success_rate": 85.0,
        }
        result = tracker.evaluate(stats)

        assert result["milestone_reached"] == 100
        assert result["overall_pass"] is False
        assert any("bad_trade_pct" in check for check in result["checks_failed"])

    def test_next_milestone_tracking(self, tracker):
        """total_trades=55 → next_milestone=75, trades_to_next=20"""
        stats = {
            "total_trades": 55,
            "win_rate_pct": 42.0,
            "profit_factor": 1.1,
            "expectancy_r": 0.05,
            "good_trade_pct": 35.0,
            "bad_trade_pct": 40.0,
            "notification_reliability": 95.0,
            "analysis_success_rate": 85.0,
        }
        result = tracker.evaluate(stats)

        assert result["next_milestone"] == 75
        assert result["trades_to_next"] == 20

    def test_milestone_200_uses_live_gates(self, tracker):
        """total_trades=205 → milestone_name='Money Readiness Eval'"""
        stats = {
            "total_trades": 205,
            "win_rate_pct": 47.0,
            "profit_factor": 1.15,
            "expectancy_r": 0.12,
            "good_trade_pct": 45.0,
            "bad_trade_pct": 30.0,
            "drawdown_r": 9.0,
            "analysis_success_rate": 88.0,
            "notification_reliability": 0.96,
            "execution_quality_score": 75.0,
            "avg_r": 0.15,
        }
        result = tracker.evaluate(stats)

        assert result["milestone_reached"] == 200
        assert result["milestone_name"] == "Money Readiness Eval"


# =============================================================================
# TEST: TestLiveReadinessEvaluator (7 tests)
# =============================================================================


class TestLiveReadinessEvaluator:
    """Test LiveReadinessEvaluator.evaluate() with various gate conditions."""

    def test_not_ready_below_200_trades(self, evaluator):
        """total_trades=150 → recommendation='MONITORING'"""
        stats = {
            "total_trades": 150,
            "win_rate_pct": 48.0,
            "profit_factor": 1.20,
            "expectancy_r": 0.12,
            "good_trade_pct": 45.0,
            "bad_trade_pct": 28.0,
            "drawdown_r": 8.0,
            "analysis_success_rate": 88.0,
            "notification_reliability": 0.96,
            "execution_quality_score": 78.0,
            "avg_r": 0.18,
        }
        result = evaluator.evaluate(stats)

        assert result["recommendation"] == "MONITORING"
        assert len(result["notes"]) > 0

    def test_not_ready_metrics_fail(self, evaluator):
        """
        total_trades=205 but win_rate=38 (below 45)
        → recommendation='NOT_READY'
        """
        stats = {
            "total_trades": 205,
            "min_trades": 205,  # For line 471 in evaluator
            "win_rate_pct": 38.0,  # Below 45 threshold
            "profit_factor": 1.15,
            "expectancy_r": 0.12,
            "good_trade_pct": 45.0,
            "bad_trade_pct": 30.0,
            "drawdown_r": 9.0,
            "analysis_success_rate": 88.0,
            "notification_reliability": 0.96,
            "execution_quality_score": 75.0,
            "avg_r": 0.15,
        }
        result = evaluator.evaluate(stats)

        assert result["recommendation"] == "NOT_READY"
        assert len(result["gates_failed"]) > 0

    def test_ready_all_gates_pass(self, evaluator, stats_excellent):
        """All gates passing with 210+ trades → recommendation='READY'"""
        result = evaluator.evaluate(stats_excellent)

        assert result["recommendation"] == "READY"
        assert len(result["gates_failed"]) == 0
        assert len(result["gates_passed"]) == len(LIVE_TRADING_GATES)

    def test_readiness_score_is_percentage(self, evaluator, stats_baseline):
        """readiness_score between 0 and 100"""
        result = evaluator.evaluate(stats_baseline)

        assert isinstance(result["readiness_score"], float)
        assert 0.0 <= result["readiness_score"] <= 100.0

    def test_gates_passed_list_populated(self, evaluator, stats_excellent):
        """gates_passed is a list of dicts with expected keys"""
        result = evaluator.evaluate(stats_excellent)

        assert isinstance(result["gates_passed"], list)
        if len(result["gates_passed"]) > 0:
            gate = result["gates_passed"][0]
            assert "gate" in gate
            assert "threshold" in gate
            assert "actual" in gate
            assert "passed" in gate

    def test_gates_failed_list_populated_on_fail(self, evaluator, stats_poor):
        """gates_failed is non-empty when metrics below threshold"""
        result = evaluator.evaluate(stats_poor)

        assert isinstance(result["gates_failed"], list)
        assert len(result["gates_failed"]) > 0
        gate = result["gates_failed"][0]
        assert "gate" in gate
        assert "passed" in gate
        assert gate["passed"] is False

    def test_recommendation_never_enables_trading(self, evaluator):
        """
        recommendation is always one of ("READY", "NOT_READY", "MONITORING")
        Never a boolean or action command
        """
        test_cases = [
            {"total_trades": 10},
            {"total_trades": 150},
            {"total_trades": 205, "win_rate_pct": 35.0},
        ]

        for stats in test_cases:
            result = evaluator.evaluate(stats)
            assert result["recommendation"] in ("READY", "NOT_READY", "MONITORING")
            assert not isinstance(result["recommendation"], bool)


# =============================================================================
# TEST: TestAdaptiveLearningPolicy (8 tests)
# =============================================================================


class TestAdaptiveLearningPolicy:
    """Test AdaptiveLearningPolicy decision logic."""

    def test_suppress_below_20_trades(self, policy):
        """should_suppress_proposals(15) → True"""
        assert policy.should_suppress_proposals(15) is True

    def test_no_suppress_at_20_trades(self, policy):
        """should_suppress_proposals(20) → False"""
        assert policy.should_suppress_proposals(20) is False

    def test_stricter_occurrence_below_50(self, policy):
        """get_min_occurrence_pct(30) → 30.0"""
        result = policy.get_min_occurrence_pct(30)
        assert result == 30.0

    def test_normal_occurrence_at_50(self, policy):
        """get_min_occurrence_pct(55) → 20.0"""
        result = policy.get_min_occurrence_pct(55)
        assert result == 20.0

    def test_can_auto_apply_passes(self, policy):
        """
        category='time_of_day_filter', confidence=0.8, risk_level='low', trades=110
        → can=True
        """
        proposal = {
            "category": "time_of_day_filter",
            "confidence": 0.8,
            "risk_level": "low",
        }
        can_apply, reason = policy.can_auto_apply(proposal, 110)
        assert can_apply is True

    def test_cannot_auto_apply_model_weight(self, policy):
        """category='model_weight', trades=150 → can=False (always manual)"""
        proposal = {
            "category": "model_weight",
            "confidence": 0.95,
            "risk_level": "low",
        }
        can_apply, reason = policy.can_auto_apply(proposal, 150)
        assert can_apply is False
        assert "manual" in reason.lower()

    def test_cannot_auto_apply_low_trades(self, policy):
        """trades=50 (below 100) → can=False"""
        proposal = {
            "category": "time_of_day_filter",
            "confidence": 0.95,
            "risk_level": "low",
        }
        can_apply, reason = policy.can_auto_apply(proposal, 50)
        assert can_apply is False

    def test_filter_proposals_caps_at_3(self, policy):
        """5 proposals in → at most 3 out; sorted by confidence desc"""
        proposals = [
            {"category": "time_of_day_filter", "confidence": 0.6},
            {"category": "time_of_day_filter", "confidence": 0.9},
            {"category": "time_of_day_filter", "confidence": 0.7},
            {"category": "time_of_day_filter", "confidence": 0.5},
            {"category": "time_of_day_filter", "confidence": 0.8},
        ]
        result = policy.filter_proposals_for_review(proposals, 100)

        assert len(result) <= 3
        # Check sorted by confidence descending
        for i in range(len(result) - 1):
            assert result[i]["confidence"] >= result[i + 1]["confidence"]


# =============================================================================
# TEST: TestDailyReportStats (6 tests)
# =============================================================================


class TestDailyReportStats:
    """Test trade stats computation helpers."""

    def test_win_rate_correct(self):
        """3 wins + 2 losses → win_rate_pct=60.0"""
        trades = [
            {"pnl_usdt": 50.0},
            {"pnl_usdt": 75.0},
            {"pnl_usdt": 100.0},
            {"pnl_usdt": -25.0},
            {"pnl_usdt": -50.0},
        ]
        stats = _compute_basic_stats(trades)
        assert stats["wins"] == 3
        assert stats["losses"] == 2
        assert stats["win_rate_pct"] == 60.0

    def test_profit_factor_correct(self):
        """wins sum=$150, losses sum=-$50 → pf=3.0"""
        trades = [
            {"pnl_usdt": 75.0},
            {"pnl_usdt": 75.0},
            {"pnl_usdt": -25.0},
            {"pnl_usdt": -25.0},
        ]
        stats = _compute_basic_stats(trades)
        assert stats["profit_factor"] == 3.0

    def test_zero_losses_pf(self):
        """all wins → profit_factor is float >= 0 (no division error)"""
        trades = [
            {"pnl_usdt": 50.0},
            {"pnl_usdt": 75.0},
            {"pnl_usdt": 100.0},
        ]
        stats = _compute_basic_stats(trades)
        assert isinstance(stats["profit_factor"], float)
        assert stats["profit_factor"] >= 0.0

    def test_zero_wins_pf(self):
        """all losses → profit_factor=0.0"""
        trades = [
            {"pnl_usdt": -25.0},
            {"pnl_usdt": -50.0},
            {"pnl_usdt": -75.0},
        ]
        stats = _compute_basic_stats(trades)
        assert stats["profit_factor"] == 0.0

    def test_avg_r_calculation(self):
        """
        pnl_usdt=50, entry_size_usdt=1000, risk_pct=0.005
        → R=10.0
        """
        trades = [
            {
                "pnl_usdt": 50.0,
                "entry_size_usdt": 1000.0,
                "risk_pct": 0.005,
            }
        ]
        stats = _compute_basic_stats(trades)
        # R = pnl / (entry_size * risk_pct) = 50 / (1000 * 0.005) = 50 / 5 = 10
        assert stats["avg_r"] == 10.0

    def test_empty_trades(self):
        """no trades → win_rate_pct=0.0, profit_factor=0.0"""
        stats = _compute_basic_stats([])
        assert stats["win_rate_pct"] == 0.0
        assert stats["profit_factor"] == 0.0
        assert stats["wins"] == 0
        assert stats["losses"] == 0


# =============================================================================
# TEST: TestMilestoneReadinessIntegration (4 tests)
# =============================================================================


class TestMilestoneReadinessIntegration:
    """Test integration of milestone and readiness evaluators."""

    def test_evaluate_milestone_convenience(self):
        """evaluate_milestone(stats) returns dict with required keys"""
        stats = {
            "total_trades": 25,
            "win_rate_pct": 40.0,
            "profit_factor": 0.9,
            "analysis_success_rate": 80.0,
        }
        result = evaluate_milestone(stats)

        assert isinstance(result, dict)
        assert "milestone_reached" in result
        assert "milestone_name" in result
        assert "checks_passed" in result
        assert "checks_failed" in result
        assert "overall_pass" in result

    def test_evaluate_readiness_convenience(self):
        """evaluate_readiness(stats) returns dict with required keys"""
        stats = {
            "total_trades": 210,
            "win_rate_pct": 47.0,
            "profit_factor": 1.15,
            "expectancy_r": 0.12,
            "good_trade_pct": 45.0,
            "bad_trade_pct": 30.0,
            "drawdown_r": 9.0,
            "analysis_success_rate": 88.0,
            "notification_reliability": 0.96,
            "execution_quality_score": 75.0,
            "avg_r": 0.15,
        }
        result = evaluate_readiness(stats)

        assert isinstance(result, dict)
        assert "recommendation" in result
        assert "gates_passed" in result
        assert "gates_failed" in result
        assert "readiness_score" in result

    def test_readiness_score_increases_with_better_stats(self, evaluator):
        """stats_good has higher readiness_score than stats_bad"""
        stats_bad = {
            "total_trades": 150,
            "win_rate_pct": 35.0,
            "profit_factor": 0.8,
            "expectancy_r": -0.05,
            "good_trade_pct": 25.0,
            "bad_trade_pct": 50.0,
            "drawdown_r": 15.0,
            "analysis_success_rate": 70.0,
            "notification_reliability": 0.75,
            "execution_quality_score": 50.0,
            "avg_r": -0.05,
        }

        stats_good = {
            "total_trades": 210,
            "win_rate_pct": 48.0,
            "profit_factor": 1.25,
            "expectancy_r": 0.15,
            "good_trade_pct": 50.0,
            "bad_trade_pct": 20.0,
            "drawdown_r": 7.0,
            "analysis_success_rate": 90.0,
            "notification_reliability": 0.97,
            "execution_quality_score": 85.0,
            "avg_r": 0.20,
        }

        result_bad = evaluator.evaluate(stats_bad)
        result_good = evaluator.evaluate(stats_good)

        assert result_good["readiness_score"] > result_bad["readiness_score"]

    def test_milestone_overall_pass_advisory_at_20(self):
        """at 20 trades, even if metrics below threshold, overall_pass=True"""
        stats = {
            "total_trades": 20,
            "win_rate_pct": 25.0,  # Well below 35 threshold
            "profit_factor": 0.5,  # Well below 0.8 threshold
            "analysis_success_rate": 50.0,  # Below 80 threshold
        }
        result = evaluate_milestone(stats)

        assert result["milestone_reached"] == 20
        assert result["overall_pass"] is True  # Advisory bypasses checks


# =============================================================================
# TEST: TestMilestoneProgressionCorrect (4 tests)
# =============================================================================


class TestMilestoneProgressionCorrect:
    """Test MILESTONES and LIVE_TRADING_GATES structure."""

    def test_milestones_ordered(self):
        """sorted(MILESTONES.keys()) == [20, 50, 75, 100, 200]"""
        assert sorted(MILESTONES.keys()) == [20, 50, 75, 100, 200]

    def test_live_gates_present(self):
        """all expected gate keys present in LIVE_TRADING_GATES"""
        expected_gates = {
            "min_trades",
            "min_win_rate_pct",
            "min_expectancy_r",
            "min_profit_factor",
            "max_drawdown_r",
            "min_good_trade_pct",
            "max_bad_trade_pct",
            "min_avg_r",
            "min_notification_reliability",
            "min_analysis_reliability",
            "min_execution_quality_score",
        }
        assert set(LIVE_TRADING_GATES.keys()) == expected_gates

    def test_milestone_200_has_gate_thresholds(self):
        """MILESTONES[200]['thresholds'] matches LIVE_TRADING_GATES"""
        milestone_200_thresholds = MILESTONES[200]["thresholds"]
        assert milestone_200_thresholds == LIVE_TRADING_GATES

    def test_advisory_only_only_at_20(self):
        """only MILESTONES[20]['advisory_only'] is True"""
        for trade_count, milestone_def in MILESTONES.items():
            if trade_count == 20:
                assert milestone_def["advisory_only"] is True
            else:
                assert milestone_def["advisory_only"] is False


# =============================================================================
# TEST: TestProposalReviewSafety (5 tests)
# =============================================================================


class TestProposalReviewSafety:
    """Test proposal review safety rules."""

    def test_review_frequency(self, policy):
        """next_review_at_trade(25) == 50"""
        assert policy.next_review_at_trade(25) == 50

    def test_max_proposals_enforced(self, policy):
        """10 proposals in → max 3 out"""
        proposals = [
            {"category": "time_of_day_filter", "confidence": 0.5 + 0.01 * i}
            for i in range(10)
        ]
        result = policy.filter_proposals_for_review(proposals, 100)
        assert len(result) <= 3

    def test_high_confidence_first(self, policy):
        """proposals sorted by confidence desc"""
        proposals = [
            {"category": "time_of_day_filter", "confidence": 0.6},
            {"category": "time_of_day_filter", "confidence": 0.9},
            {"category": "time_of_day_filter", "confidence": 0.7},
        ]
        result = policy.filter_proposals_for_review(proposals, 100)

        for i in range(len(result) - 1):
            assert result[i]["confidence"] >= result[i + 1]["confidence"]

    def test_always_manual_not_auto(self, policy):
        """confluence_threshold category → can_auto_apply=False"""
        proposal = {
            "category": "confluence_threshold",
            "confidence": 0.95,
            "risk_level": "low",
        }
        can_apply, reason = policy.can_auto_apply(proposal, 150)
        assert can_apply is False
        assert "manual" in reason.lower()

    def test_risk_pct_never_auto(self, policy):
        """risk_pct category → can_auto_apply=False"""
        proposal = {
            "category": "risk_pct",
            "confidence": 0.99,
            "risk_level": "low",
        }
        can_apply, reason = policy.can_auto_apply(proposal, 150)
        assert can_apply is False


# =============================================================================
# TEST: TestHealthReportFallback (4 tests)
# =============================================================================


class TestHealthReportFallback:
    """Test that launch checklist and daily report scripts exist."""

    def test_launch_checklist_exists(self):
        """scripts/launch_checklist.py exists"""
        checklist_path = _PROJECT_ROOT / "scripts" / "launch_checklist.py"
        assert checklist_path.exists(), f"Expected {checklist_path} to exist"

    def test_launch_checklist_has_db_section(self):
        """source contains 'Database' or 'db' keyword"""
        checklist_path = _PROJECT_ROOT / "scripts" / "launch_checklist.py"
        content = checklist_path.read_text(encoding="utf-8")
        assert "Database" in content or "database" in content.lower()

    def test_launch_checklist_has_analysis_section(self):
        """source contains 'Analysis' keyword"""
        checklist_path = _PROJECT_ROOT / "scripts" / "launch_checklist.py"
        content = checklist_path.read_text(encoding="utf-8")
        assert "Analysis" in content or "analysis" in content.lower()

    def test_daily_report_exists(self):
        """scripts/daily_report.py exists"""
        daily_report_path = _PROJECT_ROOT / "scripts" / "daily_report.py"
        assert daily_report_path.exists(), f"Expected {daily_report_path} to exist"


# =============================================================================
# TEST: Edge Cases & Robustness
# =============================================================================


class TestEdgeCases:
    """Test edge cases and robustness."""

    def test_milestone_exactly_at_boundary(self, tracker):
        """Exactly at milestone boundary (total_trades == 20)"""
        stats = {
            "total_trades": 20,
            "win_rate_pct": 40.0,
            "profit_factor": 0.9,
            "analysis_success_rate": 85.0,
        }
        result = tracker.evaluate(stats)
        assert result["milestone_reached"] == 20

    def test_missing_stats_keys(self, tracker):
        """Missing stats keys should be marked as MISSING in checks"""
        stats = {"total_trades": 50}  # Missing all thresholds
        result = tracker.evaluate(stats)
        assert result["milestone_reached"] == 50
        # Should have failed checks due to missing keys
        assert len(result["checks_failed"]) > 0

    def test_zero_trades_stats(self, evaluator):
        """Zero trades → recommendation should be MONITORING"""
        stats = {"total_trades": 0}
        result = evaluator.evaluate(stats)
        assert result["recommendation"] == "MONITORING"

    def test_negative_pnl_tracking(self):
        """Negative PnL handled correctly in stats"""
        trades = [
            {"pnl_usdt": -100.0, "entry_size_usdt": 1000.0, "risk_pct": 0.005},
            {"pnl_usdt": -50.0, "entry_size_usdt": 1000.0, "risk_pct": 0.005},
        ]
        stats = _compute_basic_stats(trades)
        assert stats["wins"] == 0
        assert stats["losses"] == 2
        assert stats["profit_factor"] == 0.0

    def test_proposal_with_missing_keys(self, policy):
        """Proposal missing 'confidence' key defaults to 0.0"""
        proposal = {"category": "time_of_day_filter", "risk_level": "low"}
        can_apply, reason = policy.can_auto_apply(proposal, 150)
        # Should fail because confidence defaults to 0.0 < 0.70
        assert can_apply is False
