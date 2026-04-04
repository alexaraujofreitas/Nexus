# ============================================================
# NEXUS TRADER — Trade Feedback Store (Phase 2)
#
# Persists trade analysis results to the TradeFeedback SQLite
# table.  Phase 2 adds forensics fields to the persisted record.
#
# Lookup key: symbol + "_" + opened_at (ISO string)
# ============================================================
from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)


def _get_session():
    from core.database.engine import get_session
    return get_session()


def persist_trade_feedback(
    trade:          dict,
    scoring_result: dict,
    root_causes:    list[dict],
    recommendations:list[dict],
    ai_explanation: Optional[str] = None,
    forensics:      Optional[dict] = None,
) -> Optional[int]:
    """
    Persist or update a TradeFeedback record including Phase 2 forensics.
    Returns the primary-key id of the inserted/updated row, or None on failure.
    """
    try:
        from core.database.models import TradeFeedback

        trade_id = _make_trade_id(trade)
        f = forensics or {}

        with _get_session() as session:
            existing = (
                session.query(TradeFeedback)
                .filter(TradeFeedback.trade_id == trade_id)
                .first()
            )
            if existing:
                session.delete(existing)
                session.flush()

            record = TradeFeedback(
                trade_id          = trade_id,
                symbol            = trade.get("symbol", ""),
                side              = trade.get("side", ""),
                regime            = trade.get("regime", ""),
                models_fired      = trade.get("models_fired") or [],
                # Core scores
                setup_score       = scoring_result.get("setup_score", 0.0),
                risk_score        = scoring_result.get("risk_score", 0.0),
                execution_score   = scoring_result.get("execution_score", 0.0),
                decision_score    = scoring_result.get("decision_score", 0.0),
                overall_score     = scoring_result.get("overall_score", 0.0),
                classification    = scoring_result.get("classification", "NEUTRAL"),
                hard_overrides    = scoring_result.get("hard_overrides", []),
                root_causes       = root_causes,
                recommendations   = recommendations,
                penalty_log       = scoring_result.get("penalty_log", {}),
                ai_explanation    = ai_explanation,
                # Outcome
                pnl_usdt          = float(trade.get("pnl_usdt") or 0.0),
                pnl_pct           = float(trade.get("pnl_pct")  or 0.0),
                exit_reason       = trade.get("exit_reason", ""),
                duration_s        = int(trade.get("duration_s") or 0),
                # Phase 2: scoring extras
                # regime_confidence_at_entry: prefer forensics dict (has fallback to scoring_result)
                # so do NOT set it here — set it once below in the forensics block
                htf_confirmed_at_entry     = scoring_result.get("htf_confirmed_at_entry"),
                signal_conflict_score      = float(trade.get("signal_conflict_score", 0.0)),
                # Phase 2: forensics
                decision_outcome_matrix    = f.get("decision_outcome_matrix_label", ""),
                avoidable_loss_flag        = f.get("avoidable_loss_flag"),
                avoidable_win_flag         = f.get("avoidable_win_flag"),
                was_loss_acceptable        = f.get("was_loss_probabilistically_acceptable"),
                failure_domain_primary     = f.get("failure_domain_primary", ""),
                failure_domain_secondary   = f.get("failure_domain_secondary", ""),
                preventability_score       = float(f.get("preventability_score", 0.0)),
                randomness_score           = float(f.get("randomness_score", 0.0)),
                model_conflict_score       = float(f.get("model_conflict_score", 0.0)),
                regime_confidence_at_entry = float(
                    f.get("regime_confidence_at_entry")
                    or scoring_result.get("regime_confidence_at_entry", 0.0)
                ),
            )
            session.add(record)
            session.commit()
            logger.debug(
                "TradeFeedback saved: %s  cls=%s  overall=%.1f  matrix=%s",
                trade_id, record.classification, record.overall_score,
                f.get("decision_outcome_matrix_label", "—")
            )
            return record.id

    except Exception as exc:
        logger.error("persist_trade_feedback failed: %s", exc)
        return None


def load_trade_feedback(trade: dict) -> Optional[dict]:
    """
    Load an existing TradeFeedback record for a trade dict.
    Returns dict or None if not found.
    """
    try:
        from core.database.models import TradeFeedback

        trade_id = _make_trade_id(trade)
        with _get_session() as session:
            rec = (
                session.query(TradeFeedback)
                .filter(TradeFeedback.trade_id == trade_id)
                .first()
            )
            if rec is None:
                return None
            return _record_to_dict(rec)
    except Exception as exc:
        logger.debug("load_trade_feedback failed: %s", exc)
        return None


def _record_to_dict(rec) -> dict:
    return {
        "trade_id":         rec.trade_id,
        "symbol":           rec.symbol,
        "side":             rec.side,
        "regime":           rec.regime,
        "models_fired":     rec.models_fired or [],
        "setup_score":      rec.setup_score,
        "risk_score":       rec.risk_score,
        "execution_score":  rec.execution_score,
        "decision_score":   rec.decision_score,
        "overall_score":    rec.overall_score,
        "classification":   rec.classification,
        "classification_emoji": {"GOOD":"✅","BAD":"❌","NEUTRAL":"⚖️"}.get(rec.classification,"⚖️"),
        "hard_overrides":   rec.hard_overrides or [],
        "root_causes":      rec.root_causes or [],
        "recommendations":  rec.recommendations or [],
        "penalty_log":      rec.penalty_log or {},
        "ai_explanation":   rec.ai_explanation,
        "pnl_usdt":         rec.pnl_usdt,
        "pnl_pct":          rec.pnl_pct,
        "exit_reason":      rec.exit_reason,
        "duration_s":       rec.duration_s,
        "created_at":       rec.created_at.isoformat() if rec.created_at else "",
        # Phase 2 forensics
        "forensics": {
            "decision_outcome_matrix_label":      getattr(rec, "decision_outcome_matrix", ""),
            "avoidable_loss_flag":                getattr(rec, "avoidable_loss_flag", None),
            "avoidable_win_flag":                 getattr(rec, "avoidable_win_flag", None),
            "was_loss_probabilistically_acceptable": getattr(rec, "was_loss_acceptable", None),
            "failure_domain_primary":             getattr(rec, "failure_domain_primary", ""),
            "failure_domain_secondary":           getattr(rec, "failure_domain_secondary", ""),
            "preventability_score":               getattr(rec, "preventability_score", 0.0),
            "randomness_score":                   getattr(rec, "randomness_score", 0.0),
            "model_conflict_score":               getattr(rec, "model_conflict_score", 0.0),
            "regime_confidence_at_entry":         getattr(rec, "regime_confidence_at_entry", 0.0),
        },
    }


def _make_trade_id(trade: dict) -> str:
    """Stable unique ID for a trade dict."""
    sym       = trade.get("symbol", "UNKNOWN")
    opened_at = trade.get("opened_at", "")
    return f"{sym}_{opened_at}"
