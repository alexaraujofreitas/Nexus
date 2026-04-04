"""
NexusTrader — Paper Trading Monitor (Phase 4)

Milestone tracker, live-trading gating criteria, and adaptive-learning policy
for the paper-trading phase. All logic operates on pre-computed stats dicts
(no direct DB queries here — those live in daily_report.py).

Usage:
    from core.monitoring.paper_trading_monitor import (
        MilestoneTracker, LiveReadinessEvaluator, AdaptiveLearningPolicy,
        MILESTONES, LIVE_TRADING_GATES, evaluate_milestone, evaluate_readiness
    )
"""

from typing import Optional, Tuple, List, Dict, Any


# =============================================================================
# LIVE TRADING GATES — defined first so MILESTONES[200] can reference it
# =============================================================================

LIVE_TRADING_GATES = {
    "min_trades":                    200,
    "min_win_rate_pct":              45.0,
    "min_expectancy_r":              0.10,
    "min_profit_factor":             1.10,
    "max_drawdown_r":                10.0,
    "min_good_trade_pct":            40.0,
    "max_bad_trade_pct":             40.0,
    "min_avg_r":                     0.05,
    "min_notification_reliability":  0.95,
    "min_analysis_reliability":      0.90,
    "min_execution_quality_score":   60.0,
}


# =============================================================================
# MILESTONES DEFINITION
# =============================================================================

MILESTONES = {
    20: {
        "name": "Early Sanity Check",
        "required_metrics": [
            "win_rate_pct",
            "profit_factor",
            "avg_r",
            "analysis_success_rate",
        ],
        "thresholds": {
            "win_rate_pct": 35.0,
            "profit_factor": 0.8,
            "analysis_success_rate": 80.0,
        },
        "advisory_only": True,
    },
    50: {
        "name": "First Meaningful Review",
        "required_metrics": [
            "win_rate_pct",
            "profit_factor",
            "avg_r",
            "expectancy_r",
            "good_trade_pct",
            "bad_trade_pct",
            "notification_reliability",
            "analysis_success_rate",
        ],
        "thresholds": {
            "win_rate_pct": 40.0,
            "profit_factor": 0.9,
            "expectancy_r": -0.05,
            "good_trade_pct": 30.0,
            "notification_reliability": 90.0,
            "analysis_success_rate": 80.0,
        },
        "comparison_ops": {
            "expectancy_r": ">=",
            "notification_reliability": ">=",
            "analysis_success_rate": ">=",
        },
        "advisory_only": False,
    },
    75: {
        "name": "Strategy Family Review",
        "required_metrics": [
            "win_rate_pct",
            "profit_factor",
            "expectancy_r",
            "good_trade_pct",
            "drawdown_r",
            "analysis_success_rate",
        ],
        "thresholds": {
            "win_rate_pct": 43.0,
            "profit_factor": 1.0,
            "expectancy_r": 0.05,
            "good_trade_pct": 35.0,
            "drawdown_r": 12.0,
            "analysis_success_rate": 80.0,
        },
        "comparison_ops": {
            "drawdown_r": "<=",
            "expectancy_r": ">=",
            "analysis_success_rate": ">=",
        },
        "advisory_only": False,
    },
    100: {
        "name": "Confidence Check",
        "required_metrics": [
            "win_rate_pct",
            "profit_factor",
            "expectancy_r",
            "good_trade_pct",
            "bad_trade_pct",
            "drawdown_r",
            "analysis_success_rate",
        ],
        "thresholds": {
            "win_rate_pct": 44.0,
            "profit_factor": 1.05,
            "expectancy_r": 0.08,
            "good_trade_pct": 38.0,
            "bad_trade_pct": 45.0,
            "drawdown_r": 10.0,
            "analysis_success_rate": 80.0,
        },
        "comparison_ops": {
            "bad_trade_pct": "<=",
            "drawdown_r": "<=",
            "expectancy_r": ">=",
            "analysis_success_rate": ">=",
        },
        "advisory_only": False,
    },
    200: {
        "name": "Money Readiness Eval",
        "required_metrics": list(LIVE_TRADING_GATES.keys()),
        "thresholds": LIVE_TRADING_GATES.copy(),
        "comparison_ops": {
            "max_drawdown_r": "<=",
            "min_win_rate_pct": ">=",
            "min_expectancy_r": ">=",
            "min_profit_factor": ">=",
            "min_good_trade_pct": ">=",
            "min_avg_r": ">=",
            "min_notification_reliability": ">=",
            "min_analysis_reliability": ">=",
            "min_execution_quality_score": ">=",
            "min_trades": ">=",
            "max_bad_trade_pct": "<=",
        },
        "advisory_only": False,
    },
}

# =============================================================================
# ADAPTIVE LEARNING POLICY
# =============================================================================


class AdaptiveLearningPolicy:
    """
    Policy for how tuning proposals are handled during paper trading.
    """

    MIN_TRADES_FOR_ANY_PROPOSAL = 20
    MIN_TRADES_FOR_AUTO_APPLY = 100
    MIN_OCCURRENCE_PCT_LOW_SAMPLE = 30.0
    MIN_OCCURRENCE_PCT_NORMAL = 20.0
    REVIEW_FREQUENCY_TRADES = 25
    AUTO_APPLY_CATEGORIES = {"time_of_day_filter"}
    ALWAYS_MANUAL_CATEGORIES = {
        "model_weight",
        "confluence_threshold",
        "risk_pct",
        "stop_loss_multiplier",
    }
    MAX_PROPOSALS_PER_REVIEW = 3

    # Overfitting protection
    # A proposal must be backed by at least this many trades in its specific category
    # (e.g. trades at that time-of-day, or with that volatility profile) before it
    # can be surfaced.  Prevents proposals driven by single-observation anomalies.
    MIN_TRADES_PER_CATEGORY = 10
    # When a proposal spans multiple symbols, require evidence from at least this
    # many distinct symbols to guard against symbol-specific noise.
    MIN_SYMBOLS_FOR_CROSS_SYMBOL_CONSISTENCY = 2

    def get_min_occurrence_pct(self, total_trades: int) -> float:
        """
        Returns the minimum occurrence percentage threshold based on sample size.

        Args:
            total_trades: Total number of trades closed.

        Returns:
            30.0 if fewer than 50 trades, else 20.0
        """
        if total_trades < 50:
            return self.MIN_OCCURRENCE_PCT_LOW_SAMPLE
        return self.MIN_OCCURRENCE_PCT_NORMAL

    def should_suppress_proposals(self, total_trades: int) -> bool:
        """
        Whether to suppress all tuning proposals (too few trades).

        Args:
            total_trades: Total number of trades closed.

        Returns:
            True if trade count is below MIN_TRADES_FOR_ANY_PROPOSAL.
        """
        return total_trades < self.MIN_TRADES_FOR_ANY_PROPOSAL

    def can_auto_apply(
        self, proposal: Dict[str, Any], total_trades: int
    ) -> Tuple[bool, str]:
        """
        Determine if a proposal can be auto-applied without manual review.

        Checks:
        - total_trades >= MIN_TRADES_FOR_AUTO_APPLY
        - category in AUTO_APPLY_CATEGORIES
        - category not in ALWAYS_MANUAL_CATEGORIES
        - proposal confidence >= 0.70
        - proposal risk_level == "low"

        Args:
            proposal: Dict with keys: category, confidence, risk_level, etc.
            total_trades: Total number of trades closed.

        Returns:
            Tuple of (bool can_apply, str reason)
        """
        if total_trades < self.MIN_TRADES_FOR_AUTO_APPLY:
            return False, f"Need {self.MIN_TRADES_FOR_AUTO_APPLY} trades for auto-apply (have {total_trades})"

        category = proposal.get("category", "")

        if category in self.ALWAYS_MANUAL_CATEGORIES:
            return False, f"Category '{category}' always requires manual approval"

        if category not in self.AUTO_APPLY_CATEGORIES:
            return False, f"Category '{category}' not in auto-apply list"

        confidence = proposal.get("confidence", 0.0)
        if confidence < 0.70:
            return False, f"Confidence {confidence:.2f} < 0.70 threshold"

        risk_level = proposal.get("risk_level", "medium")
        if risk_level != "low":
            return False, f"Risk level '{risk_level}' != 'low'"

        return True, "All auto-apply criteria met"

    def next_review_at_trade(self, last_review_trade: int) -> int:
        """
        Compute the trade index at which the next review should occur.

        Args:
            last_review_trade: The trade index of the last review.

        Returns:
            Trade index for next review = last_review_trade + REVIEW_FREQUENCY_TRADES
        """
        return last_review_trade + self.REVIEW_FREQUENCY_TRADES

    def filter_proposals_for_review(
        self, proposals: List[Dict[str, Any]], total_trades: int
    ) -> List[Dict[str, Any]]:
        """
        Filter proposals for review based on policy.

        - Suppresses all proposals if trade count too low
        - Caps output at MAX_PROPOSALS_PER_REVIEW
        - Sorts by confidence descending

        Args:
            proposals: List of proposal dicts.
            total_trades: Total number of trades closed.

        Returns:
            Filtered and sorted list of proposals.
        """
        if self.should_suppress_proposals(total_trades):
            return []

        # Apply overfitting protection before surfacing proposals
        proposals = [p for p in proposals if not self.is_overfit_risk(p, total_trades)]

        sorted_proposals = sorted(
            proposals, key=lambda p: p.get("confidence", 0.0), reverse=True
        )

        return sorted_proposals[: self.MAX_PROPOSALS_PER_REVIEW]

    def is_overfit_risk(self, proposal: Dict[str, Any], total_trades: int) -> bool:
        """
        Returns True if a proposal shows signs of overfitting and should be suppressed.

        Overfitting conditions:
        1. category_trade_count < MIN_TRADES_PER_CATEGORY — too few observations
           in the specific category bucket (e.g. only 3 trades in the time-of-day
           window that drove the proposal).
        2. Cross-symbol proposal backed by fewer than MIN_SYMBOLS_FOR_CROSS_SYMBOL_CONSISTENCY
           distinct symbols — proposal may be driven by a single-symbol anomaly.
        3. Proposal confidence is inflated when sample size is very small:
           confidence > 0.90 with category_trade_count < 15 is suspicious.

        Args:
            proposal: Proposal dict.  Relevant optional keys:
                - category_trade_count (int): trades in this category bucket
                - symbols (list[str]): symbols evidence was drawn from
                - scope (str): "symbol" | "cross_symbol" | "global"
            total_trades: Total closed trades.

        Returns:
            True if the proposal should be suppressed due to overfit risk.
        """
        category_count = proposal.get("category_trade_count", None)

        # Rule 1: insufficient category-level evidence
        if category_count is not None and category_count < self.MIN_TRADES_PER_CATEGORY:
            return True

        # Rule 2: cross-symbol proposals must have multi-symbol support
        scope = proposal.get("scope", "symbol")
        if scope == "cross_symbol":
            symbols = proposal.get("symbols", [])
            if len(symbols) < self.MIN_SYMBOLS_FOR_CROSS_SYMBOL_CONSISTENCY:
                return True

        # Rule 3: suspiciously high confidence on tiny sample
        confidence = proposal.get("confidence", 0.0)
        if (
            category_count is not None
            and category_count < 15
            and confidence > 0.90
        ):
            return True

        return False


# =============================================================================
# MILESTONE TRACKER
# =============================================================================


class MilestoneTracker:
    """
    Tracks paper-trading milestones and evaluates threshold checks.
    """

    def __init__(self):
        """Initialize the milestone tracker."""
        pass

    def evaluate(self, stats: Dict[str, Any]) -> Dict[str, Any]:
        """
        Evaluate stats against all milestones and return the most recent one reached.

        Args:
            stats: Dict with keys: total_trades, win_rate_pct, profit_factor, expectancy_r,
                   avg_r, good_trade_pct, bad_trade_pct, neutral_trade_pct, drawdown_r,
                   analysis_success_rate, notification_reliability, bad_decision_pct,
                   avoidable_loss_pct.

        Returns:
            Dict with keys:
            - milestone_reached: int | None (trade count of most recent milestone)
            - milestone_name: str
            - checks_passed: list of check descriptions that passed
            - checks_failed: list of check descriptions that failed
            - advisory_only: bool
            - next_milestone: int | None (next milestone trade count)
            - trades_to_next: int | None (trades needed to reach next milestone)
            - overall_pass: bool (True if advisory_only OR all checks passed)
        """
        total_trades = stats.get("total_trades", 0)

        # Find the most recent milestone we've reached
        reached_milestone = None
        for trade_count in sorted(MILESTONES.keys(), reverse=True):
            if total_trades >= trade_count:
                reached_milestone = trade_count
                break

        if reached_milestone is None:
            return {
                "milestone_reached": None,
                "milestone_name": "No milestone reached",
                "checks_passed": [],
                "checks_failed": [],
                "advisory_only": True,
                "next_milestone": min(MILESTONES.keys()),
                "trades_to_next": min(MILESTONES.keys()) - total_trades,
                "overall_pass": True,
            }

        # Evaluate the reached milestone
        milestone_data = MILESTONES[reached_milestone]
        checks_result = self._check_milestone(reached_milestone, stats)

        # Find next milestone
        next_milestone_list = [mc for mc in MILESTONES.keys() if mc > reached_milestone]
        next_milestone = next_milestone_list[0] if next_milestone_list else None
        trades_to_next = (next_milestone - total_trades) if next_milestone else None

        # Determine overall pass
        advisory_only = milestone_data["advisory_only"]
        all_passed = len(checks_result["checks_failed"]) == 0
        overall_pass = advisory_only or all_passed

        return {
            "milestone_reached": reached_milestone,
            "milestone_name": milestone_data["name"],
            "checks_passed": checks_result["checks_passed"],
            "checks_failed": checks_result["checks_failed"],
            "advisory_only": advisory_only,
            "next_milestone": next_milestone,
            "trades_to_next": trades_to_next,
            "overall_pass": overall_pass,
        }

    def _check_milestone(self, trade_count: int, stats: Dict[str, Any]) -> Dict[str, Any]:
        """
        Evaluate a specific milestone's thresholds against stats.

        Args:
            trade_count: The milestone trade count (e.g., 20, 50, 75, 100, 200).
            stats: Stats dict.

        Returns:
            Dict with keys: checks_passed (list), checks_failed (list)
        """
        milestone_def = MILESTONES[trade_count]
        thresholds = milestone_def["thresholds"]
        comparison_ops = milestone_def.get("comparison_ops", {})

        # Translation table: gate key → stats dict key (mirrors LiveReadinessEvaluator)
        # Required for milestone 200 which uses LIVE_TRADING_GATES keys directly.
        _gate_to_stats: Dict[str, str] = {
            "min_trades":                   "total_trades",
            "min_win_rate_pct":             "win_rate_pct",
            "min_expectancy_r":             "expectancy_r",
            "min_profit_factor":            "profit_factor",
            "max_drawdown_r":               "drawdown_r",
            "min_good_trade_pct":           "good_trade_pct",
            "max_bad_trade_pct":            "bad_trade_pct",
            "min_avg_r":                    "avg_r",
            "min_notification_reliability": "notification_reliability",
            "min_analysis_reliability":     "analysis_success_rate",
            "min_execution_quality_score":  "execution_quality_score",
        }

        checks_passed = []
        checks_failed = []

        for metric, threshold in thresholds.items():
            # Resolve gate key → stats key if needed
            stats_key = _gate_to_stats.get(metric, metric)
            actual = stats.get(stats_key, None)

            # For fraction-scale fields stored as 0–100 in stats, normalise to 0–1
            # when the gate threshold is expressed as a fraction (0–1).
            if actual is not None and isinstance(actual, float):
                if stats_key in ("notification_reliability", "analysis_success_rate"):
                    if threshold <= 1.0 and actual > 1.0:
                        actual = actual / 100.0

            if actual is None:
                checks_failed.append(f"{metric}: MISSING (threshold: {threshold})")
                continue

            # Determine comparison operator
            if metric in comparison_ops:
                op = comparison_ops[metric]
            elif metric in [
                "max_drawdown_r",
                "bad_trade_pct",
                "max_bad_trade_pct",
                "bad_decision_pct",
                "avoidable_loss_pct",
            ]:
                op = "<="
            else:
                op = ">="

            passed = self._compare(actual, op, threshold)

            if passed:
                checks_passed.append(
                    f"{metric}: {actual:.2f} {op} {threshold:.2f} ✓"
                )
            else:
                checks_failed.append(
                    f"{metric}: {actual:.2f} {op} {threshold:.2f} ✗"
                )

        return {"checks_passed": checks_passed, "checks_failed": checks_failed}

    @staticmethod
    def _compare(actual: float, op: str, threshold: float) -> bool:
        """Perform a comparison."""
        if op == ">=":
            return actual >= threshold
        elif op == "<=":
            return actual <= threshold
        elif op == ">":
            return actual > threshold
        elif op == "<":
            return actual < threshold
        elif op == "==":
            return actual == threshold
        elif op == "!=":
            return actual != threshold
        else:
            return False


# =============================================================================
# LIVE READINESS EVALUATOR
# =============================================================================


class LiveReadinessEvaluator:
    """
    Evaluates readiness to transition to live trading based on LIVE_TRADING_GATES.
    """

    def evaluate(self, stats: Dict[str, Any]) -> Dict[str, Any]:
        """
        Check all LIVE_TRADING_GATES and return recommendation.

        Args:
            stats: Stats dict with keys matching gate names (e.g., min_trades, win_rate_pct, etc.)

        Returns:
            Dict with keys:
            - recommendation: str ("READY", "NOT_READY", "MONITORING")
            - gates_passed: list of dicts {gate, threshold, actual, passed}
            - gates_failed: list of dicts {gate, threshold, actual, passed}
            - readiness_score: float 0-100 (% of gates passed)
            - blocking_issues: list of human-readable strings
            - notes: list of additional context strings
        """
        gates_passed = []
        gates_failed = []
        blocking_issues = []
        notes = []

        total_trades = stats.get("min_trades", 0)

        for gate_name, gate_threshold in LIVE_TRADING_GATES.items():
            # Map gate names to stats keys
            if gate_name == "min_trades":
                actual = stats.get("total_trades", 0)
            elif gate_name == "min_win_rate_pct":
                actual = stats.get("win_rate_pct", 0.0)
            elif gate_name == "min_expectancy_r":
                actual = stats.get("expectancy_r", 0.0)
            elif gate_name == "min_profit_factor":
                actual = stats.get("profit_factor", 0.0)
            elif gate_name == "max_drawdown_r":
                actual = stats.get("drawdown_r", float("inf"))
            elif gate_name == "min_good_trade_pct":
                actual = stats.get("good_trade_pct", 0.0)
            elif gate_name == "max_bad_trade_pct":
                actual = stats.get("bad_trade_pct", 100.0)
            elif gate_name == "min_avg_r":
                actual = stats.get("avg_r", 0.0)
            elif gate_name == "min_notification_reliability":
                actual = stats.get("notification_reliability", 0.0)
                if isinstance(actual, float) and actual > 1.0:
                    actual = actual / 100.0
            elif gate_name == "min_analysis_reliability":
                actual = stats.get("analysis_success_rate", 0.0)
                if isinstance(actual, float) and actual > 1.0:
                    actual = actual / 100.0
            elif gate_name == "min_execution_quality_score":
                actual = stats.get("execution_quality_score", 0.0)
            else:
                actual = stats.get(gate_name, None)

            if actual is None:
                gates_failed.append(
                    {
                        "gate": gate_name,
                        "threshold": gate_threshold,
                        "actual": None,
                        "passed": False,
                    }
                )
                blocking_issues.append(f"{gate_name}: missing from stats")
                continue

            # Determine operator: max_* and _pct_bad use <=, others use >=
            if gate_name.startswith("max_"):
                passed = actual <= gate_threshold
            else:
                passed = actual >= gate_threshold

            gate_result = {
                "gate": gate_name,
                "threshold": gate_threshold,
                "actual": actual,
                "passed": passed,
            }

            if passed:
                gates_passed.append(gate_result)
            else:
                gates_failed.append(gate_result)
                blocking_issues.append(
                    f"{gate_name}: {actual:.2f} (need {gate_threshold:.2f})"
                )

        # Compute readiness score
        total_gates = len(LIVE_TRADING_GATES)
        readiness_score = 100.0 * len(gates_passed) / total_gates if total_gates > 0 else 0.0

        # Determine recommendation
        if len(gates_failed) == 0 and total_trades >= LIVE_TRADING_GATES.get(
            "min_trades", 200
        ):
            recommendation = "READY"
        elif total_trades < LIVE_TRADING_GATES.get("min_trades", 200):
            recommendation = "MONITORING"
            trades_needed = (
                LIVE_TRADING_GATES.get("min_trades", 200) - total_trades
            )
            notes.append(f"Trade count: {total_trades} of {LIVE_TRADING_GATES.get('min_trades', 200)} required. Need {trades_needed} more.")
        else:
            recommendation = "NOT_READY"

        return {
            "recommendation": recommendation,
            "gates_passed": gates_passed,
            "gates_failed": gates_failed,
            "readiness_score": readiness_score,
            "blocking_issues": blocking_issues,
            "notes": notes,
        }


# =============================================================================
# CONVENIENCE FUNCTIONS
# =============================================================================


def evaluate_milestone(stats: Dict[str, Any]) -> Dict[str, Any]:
    """
    Convenience function to create a MilestoneTracker and evaluate.

    Args:
        stats: Stats dict.

    Returns:
        Milestone evaluation result dict.
    """
    tracker = MilestoneTracker()
    return tracker.evaluate(stats)


def evaluate_readiness(stats: Dict[str, Any]) -> Dict[str, Any]:
    """
    Convenience function to create a LiveReadinessEvaluator and evaluate.

    Args:
        stats: Stats dict.

    Returns:
        Readiness evaluation result dict.
    """
    evaluator = LiveReadinessEvaluator()
    return evaluator.evaluate(stats)
