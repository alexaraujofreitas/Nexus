# ============================================================
# NEXUS TRADER — Backtest-Gated Proposal Validation (Phase 2)
#
# Safe pipeline for strategy self-improvement:
#
#   1. Detect recurring weakness  (tuning_proposal_generator)
#   2. Generate proposal          (tuning_proposal_generator)
#   3. Build backtest spec        (this module)
#   4. Run backtest               (operator or scheduled task)
#   5. Evaluate vs baseline       (this module)
#   6. Approve / reject           (this module)
#   7. Record in applied_change_log (this module)
#
# Invariants:
#   • Never uses synthetic data
#   • Requires real historical parquet data in data/validation/
#   • Final promotion always requires_manual_approval unless
#     auto_tune_eligible=True AND risk_level != "high"
#   • All decisions are versioned and auditable
# ============================================================
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

# ── Backtest result outcome constants ─────────────────────────
BT_RESULT_BETTER   = "BETTER_THAN_BASELINE"
BT_RESULT_NEUTRAL  = "NEUTRAL"
BT_RESULT_WORSE    = "WORSE_THAN_BASELINE"
BT_RESULT_INCONCLUSIVE = "INCONCLUSIVE"

# ── Promotion thresholds ──────────────────────────────────────
MIN_IMPROVEMENT_PCT    = 2.0   # minimum % improvement in PF or WR to approve
MAX_DEGRADATION_PCT    = 1.0   # maximum acceptable % degradation


def build_backtest_spec(proposal: dict) -> dict:
    """
    Build a backtest specification dict from a tuning proposal.
    The spec describes exactly what to test, against what baseline,
    using which historical data.

    Returns
    -------
    dict
        Backtest spec suitable for a backtest runner or operator instructions.
    """
    param     = proposal.get("tuning_parameter", "")
    direction = proposal.get("tuning_direction", "manual")
    risk      = proposal.get("risk_level", "medium")
    evidence  = proposal.get("trigger_evidence") or {}

    # Suggest parameter delta based on direction and risk
    if direction == "increase":
        delta = {"low": 0.02, "medium": 0.05, "high": 0.10}.get(risk, 0.05)
    elif direction == "decrease":
        delta = {"low": 0.02, "medium": 0.05, "high": 0.10}.get(risk, 0.05)
    else:
        delta = None

    # Build test window based on available data (use 90-day window by default)
    symbols = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT"]

    return {
        "proposal_id":    proposal.get("proposal_id", "?"),
        "tuning_parameter": param,
        "tuning_direction": direction,
        "delta":          delta,
        "test_symbols":   symbols,
        "test_timeframes":["1h", "4h"],
        "data_path":      "data/validation/",
        "window_days":    90,
        "baseline_config": "config.yaml",
        "proposed_change": _describe_proposed_change(param, direction, delta),
        "evaluation_metrics": [
            "profit_factor", "win_rate", "avg_r_multiple",
            "max_drawdown", "trade_count", "sharpe_ratio",
        ],
        "minimum_trades_in_window": 20,
        "notes": (
            f"Backtest driven by recurring root-cause pattern: "
            f"{evidence.get('root_cause_category','?')} "
            f"({evidence.get('occurrence_pct',0):.1f}% of {evidence.get('total_trades',0)} trades)"
        ),
    }


def _describe_proposed_change(param: str, direction: str, delta: Optional[float]) -> str:
    if delta is None:
        return f"Manually review and adjust {param}."
    if direction == "increase":
        return f"Increase {param} by +{delta} (e.g., current + {delta})"
    elif direction == "decrease":
        return f"Decrease {param} by -{delta} (e.g., current - {delta})"
    elif direction == "add_filter":
        return f"Add filter controlled by {param}."
    return f"Adjust {param} per operator judgment."


def evaluate_proposal_vs_baseline(
    proposal: dict,
    backtest_results: dict,
) -> dict:
    """
    Compare backtest results against baseline and determine approve/reject.

    Parameters
    ----------
    proposal : dict
        The tuning proposal dict (from tuning_proposal_generator).
    backtest_results : dict
        Must contain:
            baseline_pf, baseline_wr, baseline_avg_r, baseline_trades
            proposed_pf, proposed_wr, proposed_avg_r, proposed_trades

    Returns
    -------
    dict
        Evaluation result with:
            decision         : "APPROVE" | "REJECT" | "MANUAL_REVIEW"
            delta_pf         : float
            delta_wr_pct     : float
            delta_avg_r      : float
            reasoning        : str
            auto_promotable  : bool
    """
    baseline_pf  = float(backtest_results.get("baseline_pf",  0.0))
    baseline_wr  = float(backtest_results.get("baseline_wr",  0.0))
    baseline_r   = float(backtest_results.get("baseline_avg_r",0.0))
    proposed_pf  = float(backtest_results.get("proposed_pf",  0.0))
    proposed_wr  = float(backtest_results.get("proposed_wr",  0.0))
    proposed_r   = float(backtest_results.get("proposed_avg_r",0.0))
    n_baseline   = int(backtest_results.get("baseline_trades", 0))
    n_proposed   = int(backtest_results.get("proposed_trades", 0))

    delta_pf  = round((proposed_pf  - baseline_pf)  / max(baseline_pf, 1e-6) * 100, 2)
    delta_wr  = round(proposed_wr   - baseline_wr,  2)
    delta_r   = round(proposed_r    - baseline_r,   4)

    if n_proposed < 20:
        return _evaluation_result(
            decision="MANUAL_REVIEW",
            delta_pf=delta_pf, delta_wr=delta_wr, delta_r=delta_r,
            reasoning=f"Insufficient trades in proposed backtest ({n_proposed} < 20 minimum).",
            auto_promotable=False,
            outcome=BT_RESULT_INCONCLUSIVE,
        )

    # Approve if PF improved meaningfully with no significant WR degradation
    pf_improved  = delta_pf >= MIN_IMPROVEMENT_PCT
    wr_not_worse = delta_wr >= -MAX_DEGRADATION_PCT
    pf_not_worse = delta_pf >= -MAX_DEGRADATION_PCT

    if pf_improved and wr_not_worse:
        decision = "APPROVE"
        outcome  = BT_RESULT_BETTER
        reasoning = (
            f"PF improved {delta_pf:+.1f}% (baseline {baseline_pf:.2f} → proposed {proposed_pf:.2f}). "
            f"WR change: {delta_wr:+.1f}pp. Proposal is effective."
        )
    elif pf_not_worse and wr_not_worse:
        decision = "APPROVE"
        outcome  = BT_RESULT_NEUTRAL
        reasoning = (
            f"No meaningful improvement (PF Δ{delta_pf:+.1f}%, WR Δ{delta_wr:+.1f}pp) "
            f"but no degradation either. Marginal approval."
        )
    elif not pf_not_worse:
        decision = "REJECT"
        outcome  = BT_RESULT_WORSE
        reasoning = (
            f"PF degraded {delta_pf:.1f}% (baseline {baseline_pf:.2f} → proposed {proposed_pf:.2f}). "
            f"Proposal rejected — worse than baseline."
        )
    else:
        decision = "MANUAL_REVIEW"
        outcome  = BT_RESULT_INCONCLUSIVE
        reasoning = (
            f"Mixed results: PF {delta_pf:+.1f}%, WR {delta_wr:+.1f}pp. "
            f"Manual operator review required."
        )

    # Auto-promotable only if proposal is auto_tune_eligible AND result is BETTER
    auto_promotable = (
        proposal.get("auto_tune_eligible", False)
        and not proposal.get("requires_manual_approval", True)
        and decision == "APPROVE"
        and outcome  == BT_RESULT_BETTER
    )

    return _evaluation_result(
        decision=decision,
        delta_pf=delta_pf, delta_wr=delta_wr, delta_r=delta_r,
        reasoning=reasoning,
        auto_promotable=auto_promotable,
        outcome=outcome,
    )


def _evaluation_result(
    decision: str, delta_pf: float, delta_wr: float, delta_r: float,
    reasoning: str, auto_promotable: bool, outcome: str,
) -> dict:
    return {
        "decision":        decision,
        "outcome":         outcome,
        "delta_pf_pct":    delta_pf,
        "delta_wr_pp":     delta_wr,
        "delta_avg_r":     delta_r,
        "reasoning":       reasoning,
        "auto_promotable": auto_promotable,
        "evaluated_at":    datetime.now(timezone.utc).isoformat(),
    }


def update_proposal_after_evaluation(
    proposal_id: str,
    evaluation: dict,
    backtest_results: dict,
) -> bool:
    """
    Persist the backtest evaluation result to the StrategyTuningProposal record.
    Returns True on success.
    """
    try:
        from core.database.engine import get_session
        from core.database.models import StrategyTuningProposal

        decision = evaluation.get("decision", "MANUAL_REVIEW")
        now      = datetime.now(timezone.utc)

        new_status = {
            "APPROVE":       "approved",
            "REJECT":        "rejected",
            "MANUAL_REVIEW": "pending",
        }.get(decision, "pending")

        with get_session() as session:
            row = session.query(StrategyTuningProposal).filter(
                StrategyTuningProposal.proposal_id == proposal_id
            ).first()

            if not row:
                logger.warning("update_proposal_after_evaluation: proposal %s not found", proposal_id)
                return False

            row.status         = new_status
            row.backtest_result = {
                "evaluation":       evaluation,
                "raw_results":      backtest_results,
                "evaluated_at":     evaluation.get("evaluated_at"),
            }
            if decision == "APPROVE":
                row.promoted_at = now
            elif decision == "REJECT":
                row.rejected_at     = now
                row.rejection_reason= evaluation.get("reasoning", "")

            session.commit()
        return True

    except Exception as exc:
        logger.error("update_proposal_after_evaluation: %s", exc)
        return False


def record_applied_change(
    proposal: dict,
    applied_value: object,
    applied_by: str = "auto",
    notes: str = "",
) -> bool:
    """
    Record an applied strategy change in the AppliedStrategyChange audit log.
    Returns True on success.
    """
    try:
        from core.database.engine import get_session
        from core.database.models import AppliedStrategyChange, StrategyTuningProposal

        with get_session() as session:
            row = AppliedStrategyChange(
                proposal_id             = proposal.get("proposal_id", ""),
                root_cause_category     = proposal.get("root_cause_category", ""),
                tuning_parameter        = proposal.get("tuning_parameter", ""),
                tuning_direction        = proposal.get("tuning_direction", ""),
                applied_value           = str(applied_value),
                applied_by              = applied_by,
                notes                   = notes,
                backtest_delta_pf_pct   = (proposal.get("backtest_result") or {}).get(
                                          "evaluation", {}).get("delta_pf_pct", 0.0),
                applied_at              = datetime.now(timezone.utc),
            )
            session.add(row)

            # Update proposal status → applied
            proposal_row = session.query(StrategyTuningProposal).filter(
                StrategyTuningProposal.proposal_id == proposal.get("proposal_id", "")
            ).first()
            if proposal_row:
                proposal_row.status = "applied"

            session.commit()
        return True

    except Exception as exc:
        logger.error("record_applied_change: %s", exc)
        return False


def generate_backtest_fetch_script(symbols: list[str], window_days: int = 90) -> str:
    """
    Return a shell command string to fetch additional historical data
    needed for the backtest window.
    Only real exchange data is used — never synthetic.
    """
    sym_list = " ".join(symbols)
    return (
        f"# Fetch {window_days}-day backtest window from Bybit Demo\n"
        f"# Symbols: {sym_list}\n"
        f"python scripts/fetch_historical_data_v2.py "
        f"--symbols {sym_list} "
        f"--timeframes 1h 4h "
        f"--days {window_days} "
        f"--output data/validation/"
    )


def load_pending_proposals() -> list[dict]:
    """Load all proposals with status='pending' from the DB."""
    try:
        from core.database.engine import get_session
        from core.database.models import StrategyTuningProposal
        with get_session() as session:
            rows = session.query(StrategyTuningProposal).filter_by(status="pending").all()
            return [_proposal_to_dict(row) for row in rows]
    except Exception as e:
        logger.warning("backtest_gating: load_pending_proposals failed: %s", e)
        return []


def _proposal_to_dict(row) -> dict:
    """Convert a StrategyTuningProposal ORM row to a plain dict."""
    return {
        "proposal_id":              row.proposal_id,
        "root_cause_category":      row.root_cause_category,
        "rec_id":                   row.rec_id,
        "trigger_evidence":         row.trigger_evidence or {},
        "affected_subsystem":       row.affected_subsystem,
        "tuning_parameter":         row.tuning_parameter,
        "tuning_direction":         row.tuning_direction,
        "proposed_change_description": str(row.proposed_change_description or ""),
        "expected_benefit":         str(row.expected_benefit or ""),
        "confidence":               float(row.confidence or 0),
        "risk_level":               row.risk_level,
        "auto_tune_eligible":       bool(row.auto_tune_eligible),
        "requires_manual_approval": bool(row.requires_manual_approval),
        "status":                   row.status,
        "backtest_result":          row.backtest_result or {},
        "created_at":               str(row.created_at or ""),
    }


def load_applied_changes() -> list[dict]:
    """Load the immutable applied-change audit trail."""
    try:
        from core.database.engine import get_session
        from core.database.models import AppliedStrategyChange
        with get_session() as session:
            rows = (session.query(AppliedStrategyChange)
                    .order_by(AppliedStrategyChange.applied_at.desc())
                    .limit(100)
                    .all())
            return [{
                "proposal_id":         r.proposal_id,
                "tuning_parameter":    r.tuning_parameter,
                "tuning_direction":    r.tuning_direction,
                "applied_value":       str(r.applied_value or ""),
                "applied_by":          r.applied_by,
                "notes":               str(r.notes or ""),
                "backtest_delta_pf_pct": float(r.backtest_delta_pf_pct or 0),
                "applied_at":          str(r.applied_at or ""),
            } for r in rows]
    except Exception as e:
        logger.warning("backtest_gating: load_applied_changes failed: %s", e)
        return []
