# ============================================================
# NEXUS TRADER — Tuning Proposal Generator (Phase 2)
#
# Converts recurring root-cause patterns from TradeFeedback
# aggregation into concrete StrategyTuningProposal objects.
#
# Pipeline:
#   1. Read aggregated root-cause frequency from TradeFeedback
#   2. Match against recommendation_policy tuning_parameter fields
#   3. Generate a proposal with trigger evidence, affected system,
#      proposed change, confidence, risk level, and gating flags
#   4. Persist to StrategyTuningProposal table (status=pending)
#
# Each proposal must be backtest-gated before promotion.
# ============================================================
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from .recommendation_policy import RECOMMENDATION_POLICY, get_recommendation

logger = logging.getLogger(__name__)

# ── Proposal status constants ─────────────────────────────────
STATUS_PENDING  = "pending"
STATUS_BACKTESTING = "backtesting"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"
STATUS_APPLIED  = "applied"

# ── Risk level constants ──────────────────────────────────────
RISK_LOW    = "low"
RISK_MEDIUM = "medium"
RISK_HIGH   = "high"


def generate_tuning_proposals(
    feedback_summary: dict,
    min_trade_count: int = 10,
    min_occurrence_pct: float = 20.0,
) -> list[dict]:
    """
    Analyse aggregated feedback and generate a list of tuning proposals
    for root-cause patterns that exceed the occurrence threshold.

    Parameters
    ----------
    feedback_summary : dict
        Output of adaptive_learning.full_feedback_summary()
    min_trade_count : int
        Minimum number of total trades required before generating proposals.
    min_occurrence_pct : float
        Minimum percentage of trades exhibiting a root cause to trigger proposal.

    Returns
    -------
    list[dict]
        List of proposal dicts ready for persistence.
    """
    total = feedback_summary.get("total", 0)
    if total < min_trade_count:
        logger.debug(
            "tuning_proposal_generator: only %d trades — need %d before generating proposals",
            total, min_trade_count
        )
        return []

    top_causes = feedback_summary.get("top_root_causes") or []
    top_recs   = feedback_summary.get("top_recommendations") or []

    proposals: list[dict] = []

    for rc_entry in top_causes:
        cat  = rc_entry.get("category", "")
        pct  = rc_entry.get("pct", 0.0)
        cnt  = rc_entry.get("count", 0)
        sev  = rc_entry.get("severity", "minor")

        if pct < min_occurrence_pct:
            continue

        # Map to recommendation IDs via policy
        from .root_cause_catalog import get_recommendation_ids, is_auto_tune_eligible
        rec_ids = get_recommendation_ids(cat)

        for rec_id in rec_ids:
            rec = get_recommendation(rec_id)
            if not rec:
                continue

            tp = rec.get("tuning_parameter")
            if not tp:
                continue  # manual-only recommendations don't generate proposals

            min_required = rec.get("min_trades_required", 10)
            if total < min_required:
                continue

            # Compute confidence based on occurrence rate and trade count
            confidence = _compute_confidence(pct, cnt, total, sev)

            # Risk level
            risk_level = _assess_risk_level(rec_id, cat)

            proposal = _build_proposal(
                root_cause_category=cat,
                rec_id=rec_id,
                rec=rec,
                trigger_evidence={
                    "root_cause_category": cat,
                    "occurrence_count":    cnt,
                    "occurrence_pct":      pct,
                    "total_trades":        total,
                    "severity":            sev,
                },
                confidence=confidence,
                risk_level=risk_level,
                auto_tune_eligible=is_auto_tune_eligible(cat) and rec.get("auto_tune_safe", False),
            )
            proposals.append(proposal)

    # Deduplicate by tuning_parameter (keep highest confidence)
    seen_params: dict[str, dict] = {}
    for p in proposals:
        key = p.get("tuning_parameter") or p.get("proposal_id")
        existing = seen_params.get(key)
        if existing is None or p["confidence"] > existing["confidence"]:
            seen_params[key] = p

    deduplicated = list(seen_params.values())
    deduplicated.sort(key=lambda p: p["confidence"], reverse=True)

    logger.info(
        "tuning_proposal_generator: generated %d proposals from %d trades",
        len(deduplicated), total
    )
    return deduplicated


def _build_proposal(
    root_cause_category: str,
    rec_id: str,
    rec: dict,
    trigger_evidence: dict,
    confidence: float,
    risk_level: str,
    auto_tune_eligible: bool,
) -> dict:
    return {
        "proposal_id":          f"PROP-{uuid.uuid4().hex[:8].upper()}",
        "root_cause_category":  root_cause_category,
        "rec_id":               rec_id,
        "trigger_evidence":     trigger_evidence,
        "affected_subsystem":   rec.get("affected_subsystem", "unknown"),
        "tuning_parameter":     rec.get("tuning_parameter", ""),
        "tuning_direction":     rec.get("tuning_direction", "manual"),
        "proposed_change_description": _build_change_description(rec_id, rec),
        "expected_benefit":     rec.get("rationale", "")[:200],
        "confidence":           round(confidence, 2),
        "risk_level":           risk_level,
        "auto_tune_eligible":   auto_tune_eligible,
        "requires_manual_approval": not auto_tune_eligible or risk_level == RISK_HIGH,
        "status":               STATUS_PENDING,
        "created_at":           datetime.now(timezone.utc).isoformat(),
        "backtest_result":      None,
        "promoted_at":          None,
        "rejected_at":          None,
        "rejection_reason":     None,
    }


def _build_change_description(rec_id: str, rec: dict) -> str:
    param     = rec.get("tuning_parameter", "")
    direction = rec.get("tuning_direction", "manual")
    action    = rec.get("action", "")[:100]

    if direction == "increase":
        return f"Increase {param}. {action}"
    elif direction == "decrease":
        return f"Decrease {param}. {action}"
    elif direction == "add_filter":
        return f"Add filter on {param}. {action}"
    else:
        return f"Manual review required for {param}. {action}"


def _compute_confidence(pct: float, count: int, total: int, severity: str) -> float:
    """
    Compute a confidence score (0–1) for the proposal.
    Factors: occurrence rate, absolute count, severity.
    """
    base = min(0.5, pct / 100)               # 50% occurrence → 0.5 base
    count_bonus = min(0.30, count / 100)     # 30 trades → +0.30
    sev_bonus = {"critical": 0.20, "major": 0.10, "minor": 0.0}.get(severity, 0.0)
    return min(0.99, base + count_bonus + sev_bonus)


def _assess_risk_level(rec_id: str, cat: str) -> str:
    """Assess risk level of applying this proposal."""
    # Hard-gate changes are high risk (might block trades)
    if any(x in rec_id for x in ("BLOCK", "ENFORCE", "MANDATORY")):
        return RISK_HIGH
    # Threshold changes are medium risk
    if any(x in rec_id for x in ("RAISE", "LOWER", "REQUIRE")):
        return RISK_MEDIUM
    # ATR multiplier and filter additions are low risk
    return RISK_LOW


def persist_proposals(proposals: list[dict]) -> int:
    """
    Persist generated proposals to the StrategyTuningProposal table.
    Returns count of newly inserted proposals.
    """
    if not proposals:
        return 0

    inserted = 0
    try:
        from core.database.engine import get_session
        from core.database.models import StrategyTuningProposal

        with get_session() as session:
            for p in proposals:
                # Check for existing pending proposal for same parameter
                existing = session.query(StrategyTuningProposal).filter(
                    StrategyTuningProposal.tuning_parameter == p.get("tuning_parameter", ""),
                    StrategyTuningProposal.status == STATUS_PENDING,
                ).first()

                if existing:
                    # Update confidence if higher
                    if p["confidence"] > (existing.confidence or 0):
                        existing.confidence  = p["confidence"]
                        existing.trigger_evidence = p.get("trigger_evidence")
                    continue

                row = StrategyTuningProposal(
                    proposal_id             = p["proposal_id"],
                    root_cause_category     = p["root_cause_category"],
                    rec_id                  = p["rec_id"],
                    trigger_evidence        = p.get("trigger_evidence"),
                    affected_subsystem      = p.get("affected_subsystem", ""),
                    tuning_parameter        = p.get("tuning_parameter", ""),
                    tuning_direction        = p.get("tuning_direction", "manual"),
                    proposed_change_description = p.get("proposed_change_description", ""),
                    expected_benefit        = p.get("expected_benefit", ""),
                    confidence              = p.get("confidence", 0.0),
                    risk_level              = p.get("risk_level", RISK_MEDIUM),
                    auto_tune_eligible      = p.get("auto_tune_eligible", False),
                    requires_manual_approval= p.get("requires_manual_approval", True),
                    status                  = STATUS_PENDING,
                )
                session.add(row)
                inserted += 1

            session.commit()

    except Exception as exc:
        logger.error("persist_proposals failed: %s", exc)

    return inserted


def load_pending_proposals() -> list[dict]:
    """Load all pending tuning proposals from the database."""
    try:
        from core.database.engine import get_session
        from core.database.models import StrategyTuningProposal

        with get_session() as session:
            rows = session.query(StrategyTuningProposal).filter(
                StrategyTuningProposal.status == STATUS_PENDING
            ).all()
            return [_row_to_dict(r) for r in rows]
    except Exception as exc:
        logger.debug("load_pending_proposals: %s", exc)
        return []


def _row_to_dict(row) -> dict:
    return {
        "id":                        row.id,
        "proposal_id":               row.proposal_id,
        "root_cause_category":       row.root_cause_category,
        "rec_id":                    row.rec_id,
        "trigger_evidence":          row.trigger_evidence,
        "affected_subsystem":        row.affected_subsystem,
        "tuning_parameter":          row.tuning_parameter,
        "tuning_direction":          row.tuning_direction,
        "proposed_change_description": row.proposed_change_description,
        "expected_benefit":          row.expected_benefit,
        "confidence":                row.confidence,
        "risk_level":                row.risk_level,
        "auto_tune_eligible":        row.auto_tune_eligible,
        "requires_manual_approval":  row.requires_manual_approval,
        "status":                    row.status,
        "backtest_result":           row.backtest_result,
        "created_at":                str(row.created_at) if row.created_at else "",
    }
