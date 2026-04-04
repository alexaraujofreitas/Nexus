# ============================================================
# NEXUS TRADER — Improvement Recommender (Phase 2)
#
# Converts root-cause objects into ranked recommendation objects.
# Uses recommendation_policy.py as the single source of all
# recommendation templates — no duplicate logic here.
#
# Phase 2 changes:
#   • Uses recommendation_policy.build_recommendation_object()
#   • Preserves backward compat with old callers (same output shape)
# ============================================================
from __future__ import annotations

from .root_cause_catalog    import get_recommendation_ids
from .recommendation_policy import build_recommendation_object

_PRIORITY_ORDER = {"high": 0, "medium": 1, "low": 2}


def generate_recommendations(root_causes: list[dict]) -> list[dict]:
    """
    Generate a deduplicated, priority-sorted list of recommendations
    from a list of root-cause objects.

    Each recommendation dict:
    {
        rec_id:            str,
        category:          str,
        action:            str,
        rationale:         str,
        affected_subsystem:str,
        priority:          str,   # high | medium | low
        auto_tune_safe:    bool,
        tuning_parameter:  str | None,
        tuning_direction:  str,
        min_trades_required: int,
    }
    """
    seen_rec_ids:    set[str] = set()
    seen_categories: set[str] = set()
    recommendations: list[dict] = []

    for rc in root_causes:
        category = rc.get("category", "")
        if not category:
            continue

        # One recommendation entry per root-cause category (same category may
        # appear from multiple scoring dimensions — only process it once).
        if category in seen_categories:
            continue
        seen_categories.add(category)

        rec_ids = get_recommendation_ids(category)
        # Emit only the first (highest-priority) rec_id for this category so
        # the output contains exactly one entry per category.
        for rec_id in rec_ids:
            if rec_id in seen_rec_ids:
                continue
            seen_rec_ids.add(rec_id)

            rec = build_recommendation_object(rec_id, category)
            if rec:
                recommendations.append(rec)
                break   # one recommendation per category

    # Sort: high → medium → low, then auto_tune_safe first
    recommendations.sort(key=lambda r: (
        _PRIORITY_ORDER.get(r.get("priority", "medium"), 1),
        0 if r.get("auto_tune_safe") else 1,
    ))

    return recommendations
