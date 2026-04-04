# ============================================================
# NEXUS TRADER — Adaptive Learning Aggregator
#
# Aggregates TradeFeedback records to surface:
#   • Quality distribution by symbol
#   • Quality distribution by regime
#   • Quality distribution by model
#   • Root-cause frequency (most common failure modes)
#   • Recommendation recurrence (highest-priority improvements)
#
# Results are read-only summaries used by the UI and report.
# ============================================================
from __future__ import annotations

import logging
from collections import Counter, defaultdict
from typing import Optional

logger = logging.getLogger(__name__)


def _get_session():
    from core.database.engine import get_session
    return get_session()


# ─────────────────────────────────────────────────────────────
# Public aggregation functions
# ─────────────────────────────────────────────────────────────

def aggregate_by_symbol(limit: int = 20) -> list[dict]:
    """
    Returns per-symbol quality summary, sorted by trade count descending.

    Each row:
    {
        symbol:           str,
        total:            int,
        good_count:       int,
        bad_count:        int,
        neutral_count:    int,
        avg_overall:      float,
        avg_setup:        float,
        avg_risk:         float,
        avg_execution:    float,
        avg_decision:     float,
        win_rate:         float,   # pnl_usdt > 0
        avg_pnl_usdt:     float,
    }
    """
    try:
        from core.database.models import TradeFeedback
        with _get_session() as session:
            records = session.query(TradeFeedback).all()
        return _group_records(records, key_fn=lambda r: r.symbol, limit=limit)
    except Exception as exc:
        logger.debug("aggregate_by_symbol: %s", exc)
        return []


def aggregate_by_regime(limit: int = 20) -> list[dict]:
    """Returns per-regime quality summary."""
    try:
        from core.database.models import TradeFeedback
        with _get_session() as session:
            records = session.query(TradeFeedback).all()
        return _group_records(records, key_fn=lambda r: r.regime or "unknown", limit=limit)
    except Exception as exc:
        logger.debug("aggregate_by_regime: %s", exc)
        return []


def aggregate_by_model(limit: int = 20) -> list[dict]:
    """
    Returns per-model quality summary.
    Each trade can contribute to multiple models (models_fired list).
    """
    try:
        from core.database.models import TradeFeedback
        with _get_session() as session:
            records = session.query(TradeFeedback).all()

        # Expand: one record → multiple model entries
        model_records: dict[str, list] = defaultdict(list)
        for rec in records:
            for model in (rec.models_fired or []):
                if model:
                    model_records[model].append(rec)

        result = []
        for model, recs in model_records.items():
            if recs:
                result.append(_summarise_group(model, recs))

        result.sort(key=lambda x: x["total"], reverse=True)
        return result[:limit]
    except Exception as exc:
        logger.debug("aggregate_by_model: %s", exc)
        return []


def most_common_root_causes(top_n: int = 10) -> list[dict]:
    """
    Returns the most frequently occurring root-cause categories
    across all TradeFeedback records.

    Each row:
    {
        category:    str,
        count:       int,
        pct:         float,  # % of trades with this root cause
        severity:    str,    # most severe encountered
        description: str,
    }
    """
    try:
        from core.database.models import TradeFeedback
        with _get_session() as session:
            records = session.query(TradeFeedback).all()

        if not records:
            return []

        total_trades = len(records)
        cat_counter: Counter = Counter()
        cat_severity: dict[str, str] = {}
        cat_description: dict[str, str] = {}

        _sev_rank = {"critical": 0, "major": 1, "minor": 2}

        for rec in records:
            for rc in (rec.root_causes or []):
                cat = rc.get("category", "")
                if not cat:
                    continue
                cat_counter[cat] += 1
                sev = rc.get("severity", "minor")
                existing = cat_severity.get(cat, "minor")
                if _sev_rank.get(sev, 2) < _sev_rank.get(existing, 2):
                    cat_severity[cat] = sev
                if cat not in cat_description:
                    cat_description[cat] = rc.get("description", "")

        result = []
        for cat, count in cat_counter.most_common(top_n):
            result.append({
                "category":    cat,
                "count":       count,
                "pct":         round(count / total_trades * 100, 1),
                "severity":    cat_severity.get(cat, "minor"),
                "description": cat_description.get(cat, ""),
            })

        return result
    except Exception as exc:
        logger.debug("most_common_root_causes: %s", exc)
        return []


def most_recurring_recommendations(top_n: int = 5) -> list[dict]:
    """
    Returns the most frequently recommended improvements across all feedback.

    Each row: {category, action, auto_tune_safe, priority, count, pct}
    """
    try:
        from core.database.models import TradeFeedback
        with _get_session() as session:
            records = session.query(TradeFeedback).all()

        if not records:
            return []

        total = len(records)
        cat_counter: Counter = Counter()
        cat_template: dict[str, dict] = {}

        for rec in records:
            for r in (rec.recommendations or []):
                cat = r.get("category", "")
                if not cat:
                    continue
                cat_counter[cat] += 1
                if cat not in cat_template:
                    cat_template[cat] = r

        result = []
        for cat, count in cat_counter.most_common(top_n):
            tmpl = cat_template.get(cat, {})
            result.append({
                "category":       cat,
                "action":         tmpl.get("action", ""),
                "auto_tune_safe": tmpl.get("auto_tune_safe", False),
                "priority":       tmpl.get("priority", "medium"),
                "count":          count,
                "pct":            round(count / total * 100, 1),
            })

        return result
    except Exception as exc:
        logger.debug("most_recurring_recommendations: %s", exc)
        return []


def full_feedback_summary() -> dict:
    """
    Returns a comprehensive summary dict for use in reports.
    """
    try:
        from core.database.models import TradeFeedback
        with _get_session() as session:
            records = session.query(TradeFeedback).all()

        total = len(records)
        if total == 0:
            return {"total": 0}

        good    = sum(1 for r in records if r.classification == "GOOD")
        bad     = sum(1 for r in records if r.classification == "BAD")
        neutral = total - good - bad

        avg_overall   = sum(r.overall_score   for r in records) / total
        avg_setup     = sum(r.setup_score     for r in records) / total
        avg_risk      = sum(r.risk_score      for r in records) / total
        avg_execution = sum(r.execution_score for r in records) / total
        avg_decision  = sum(r.decision_score  for r in records) / total
        wins          = sum(1 for r in records if r.pnl_usdt > 0)

        return {
            "total":           total,
            "good_count":      good,
            "bad_count":       bad,
            "neutral_count":   neutral,
            "good_pct":        round(good  / total * 100, 1),
            "bad_pct":         round(bad   / total * 100, 1),
            "avg_overall":     round(avg_overall,   1),
            "avg_setup":       round(avg_setup,     1),
            "avg_risk":        round(avg_risk,      1),
            "avg_execution":   round(avg_execution, 1),
            "avg_decision":    round(avg_decision,  1),
            "win_rate":        round(wins / total * 100, 1),
            "by_symbol":       aggregate_by_symbol(),
            "by_regime":       aggregate_by_regime(),
            "by_model":        aggregate_by_model(),
            "top_root_causes": most_common_root_causes(),
            "top_recommendations": most_recurring_recommendations(),
        }
    except Exception as exc:
        logger.error("full_feedback_summary: %s", exc)
        return {"total": 0, "error": str(exc)}


# ─────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────

def _group_records(records, key_fn, limit: int) -> list[dict]:
    groups: dict[str, list] = defaultdict(list)
    for rec in records:
        k = key_fn(rec)
        groups[k].append(rec)

    result = [_summarise_group(k, recs) for k, recs in groups.items()]
    result.sort(key=lambda x: x["total"], reverse=True)
    return result[:limit]


def _summarise_group(name: str, records: list) -> dict:
    n = len(records)
    if n == 0:
        return {"name": name, "total": 0}

    good    = sum(1 for r in records if r.classification == "GOOD")
    bad     = sum(1 for r in records if r.classification == "BAD")
    neutral = n - good - bad
    wins    = sum(1 for r in records if r.pnl_usdt > 0)

    return {
        "name":           name,
        "total":          n,
        "good_count":     good,
        "bad_count":      bad,
        "neutral_count":  neutral,
        "avg_overall":    round(sum(r.overall_score   for r in records) / n, 1),
        "avg_setup":      round(sum(r.setup_score     for r in records) / n, 1),
        "avg_risk":       round(sum(r.risk_score      for r in records) / n, 1),
        "avg_execution":  round(sum(r.execution_score for r in records) / n, 1),
        "avg_decision":   round(sum(r.decision_score  for r in records) / n, 1),
        "win_rate":       round(wins / n * 100, 1),
        "avg_pnl_usdt":   round(sum(r.pnl_usdt for r in records) / n, 2),
    }
