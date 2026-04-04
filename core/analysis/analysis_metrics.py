# ============================================================
# NEXUS TRADER — Analysis Metrics (Phase 3)
#
# Lightweight counters and structured logging for the
# trade-analysis subsystem. Provides operational observability
# without requiring an external metrics framework.
# ============================================================
from __future__ import annotations

import logging
import threading
from collections import defaultdict
from typing import Any

logger = logging.getLogger(__name__)

_lock = threading.Lock()

# ── Counters ─────────────────────────────────────────────────
_counters: dict[str, int] = defaultdict(int)

# Counter names (public constants for callers)
C_ANALYSIS_OK              = "analysis.generation.ok"
C_ANALYSIS_ERROR           = "analysis.generation.error"
C_ANALYSIS_FALLBACK        = "analysis.generation.fallback_empty"
C_AI_ENRICH_OK             = "ai.enrichment.ok"
C_AI_ENRICH_ERROR          = "ai.enrichment.error"
C_AI_ENRICH_FALLBACK       = "ai.enrichment.fallback_none"
C_NOTIF_RENDER_OK          = "notification.render.ok"
C_NOTIF_RENDER_ERROR       = "notification.render.error"
C_FEEDBACK_PERSIST_OK      = "feedback.persist.ok"
C_FEEDBACK_PERSIST_ERROR   = "feedback.persist.error"
C_PROPOSAL_GENERATED       = "proposal.generated"
C_PROPOSAL_PERSISTED       = "proposal.persisted"
C_PROPOSAL_APPROVED        = "proposal.approved"
C_PROPOSAL_REJECTED        = "proposal.rejected"
C_PROPOSAL_MANUAL_REVIEW   = "proposal.manual_review"
C_BACKTEST_RUN_OK          = "backtest.run.ok"
C_BACKTEST_RUN_ERROR       = "backtest.run.error"
C_APPLIED_CHANGE           = "applied_change.recorded"
C_CONTRACT_VIOLATION       = "contract.violation"
C_FILTER_STATS_OK          = "filter_stats.record.ok"
C_FILTER_STATS_ERROR       = "filter_stats.record.error"
C_DUPLICATE_PROPOSAL       = "proposal.duplicate_skipped"
C_DUPLICATE_FEEDBACK       = "feedback.duplicate_skipped"


def inc(counter: str, n: int = 1) -> None:
    """Increment a named counter."""
    with _lock:
        _counters[counter] += n


def get(counter: str) -> int:
    """Get current value of a named counter."""
    with _lock:
        return _counters.get(counter, 0)


def snapshot() -> dict[str, int]:
    """Return a copy of all non-zero counters."""
    with _lock:
        return {k: v for k, v in _counters.items() if v > 0}


def reset_all() -> None:
    """Reset all counters (for testing)."""
    with _lock:
        _counters.clear()


def log_snapshot(prefix: str = "AnalysisMetrics") -> None:
    """Log all non-zero counters at INFO level."""
    snap = snapshot()
    if not snap:
        logger.info("%s: no activity recorded yet", prefix)
        return
    for k, v in sorted(snap.items()):
        logger.info("%s | %s = %d", prefix, k, v)


# ── Structured event log helpers ─────────────────────────────

def log_analysis_generated(trade_symbol: str, is_open: bool, score: float,
                             classification: str) -> None:
    inc(C_ANALYSIS_OK)
    logger.debug(
        "AnalysisMetrics: generated %s analysis | symbol=%s score=%.1f class=%s",
        "open" if is_open else "closed", trade_symbol, score, classification,
    )


def log_analysis_error(trade_symbol: str, exc: Exception) -> None:
    inc(C_ANALYSIS_ERROR)
    logger.warning(
        "AnalysisMetrics: analysis generation failed | symbol=%s error=%s",
        trade_symbol, exc,
    )


def log_feedback_persisted(trade_symbol: str, feedback_id: Any) -> None:
    inc(C_FEEDBACK_PERSIST_OK)
    logger.debug(
        "AnalysisMetrics: feedback persisted | symbol=%s id=%s",
        trade_symbol, feedback_id,
    )


def log_feedback_error(trade_symbol: str, exc: Exception) -> None:
    inc(C_FEEDBACK_PERSIST_ERROR)
    logger.warning(
        "AnalysisMetrics: feedback persist failed | symbol=%s error=%s",
        trade_symbol, exc,
    )


def log_proposal_generated(category: str, param: str, confidence: float) -> None:
    inc(C_PROPOSAL_GENERATED)
    logger.info(
        "AnalysisMetrics: proposal generated | category=%s param=%s confidence=%.2f",
        category, param, confidence,
    )


def log_proposal_decision(proposal_id: str, decision: str) -> None:
    if decision == "APPROVE":
        inc(C_PROPOSAL_APPROVED)
    elif decision == "REJECT":
        inc(C_PROPOSAL_REJECTED)
    else:
        inc(C_PROPOSAL_MANUAL_REVIEW)
    logger.info(
        "AnalysisMetrics: proposal decision | id=%s decision=%s",
        proposal_id, decision,
    )


def log_backtest_result(proposal_id: str, ok: bool, pf_delta: float = 0.0) -> None:
    if ok:
        inc(C_BACKTEST_RUN_OK)
        logger.info(
            "AnalysisMetrics: backtest complete | proposal=%s pf_delta=%.2f%%",
            proposal_id, pf_delta,
        )
    else:
        inc(C_BACKTEST_RUN_ERROR)
        logger.warning(
            "AnalysisMetrics: backtest failed | proposal=%s", proposal_id,
        )


def log_contract_violation(context: str, errors: list[str]) -> None:
    inc(C_CONTRACT_VIOLATION)
    logger.warning(
        "AnalysisMetrics: contract violation | context=%s errors=%s",
        context, errors,
    )


def log_applied_change(proposal_id: str, param: str, applied_by: str) -> None:
    inc(C_APPLIED_CHANGE)
    logger.info(
        "AnalysisMetrics: applied change | proposal=%s param=%s by=%s",
        proposal_id, param, applied_by,
    )
