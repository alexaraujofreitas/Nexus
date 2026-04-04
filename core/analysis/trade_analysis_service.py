# ============================================================
# NEXUS TRADER — Trade Analysis Service (Phase 2)
#
# Canonical single source of truth for AI-driven trade analysis.
# All output channels consume this service's output — no channel
# implements its own scoring, root-cause, or recommendation logic.
#
# Phase 2 additions:
#   • build_open_trade_analysis(trade, **live_kwargs) → full thesis
#   • build_closed_trade_analysis(trade)             → forensics
#   • build_trade_analysis(trade)                    → auto-dispatches
#   • generate_notification_payload()                → uses canonical_renderer
#   • on_trade_closed()                              → persist + AI enrich
#   • generate_tuning_proposals()                    → adaptive learning
#   • get_pending_proposals()                        → proposal list
#
# Canonical analysis object (Phase 2):
# {
#   setup_score, risk_score, execution_score, decision_score,
#   overall_score, classification, classification_emoji,
#   hard_overrides, penalty_log, rr_ratio, regime_affinity, is_open,
#   regime_confidence_at_entry, htf_confirmed_at_entry,
#   root_causes      : list[dict],
#   recommendations   : list[dict],
#   ai_explanation    : str|None,
#   thesis            : dict|None,    # open trades
#   forensics         : dict|None,    # closed trades
# }
# ============================================================
from __future__ import annotations

import logging
from typing import Optional, Callable

from .scoring_engine         import score_trade
from .root_cause_analyzer    import analyze_root_causes
from .improvement_recommender import generate_recommendations

logger = logging.getLogger(__name__)

_CLASSIFICATION_EMOJI = {
    "GOOD":    "✅",
    "BAD":     "❌",
    "NEUTRAL": "⚖️",
}


class TradeAnalysisService:
    """
    Facade for the full Phase 2 analysis pipeline.
    All output channels must call this service — never bypass it.
    """

    # ─────────────────────────────────────────────────────────
    # Core analysis entry points
    # ─────────────────────────────────────────────────────────

    def build_trade_analysis(
        self,
        trade: dict,
        # Optional live-market fields for open-trade live validity
        current_regime: Optional[str] = None,
        current_confluence: Optional[float] = None,
        current_momentum: Optional[str] = None,
        current_volatility: Optional[str] = None,
        current_liquidity: Optional[str] = None,
    ) -> dict:
        """
        Build full canonical analysis object.
        Auto-dispatches to open or closed path based on exit_price / exit_reason.
        Pure computation — no DB writes, no async calls.
        """
        try:
            is_open = not bool(trade.get("exit_price") or trade.get("exit_reason"))
            if is_open:
                return self.build_open_trade_analysis(
                    trade,
                    current_regime=current_regime,
                    current_confluence=current_confluence,
                    current_momentum=current_momentum,
                    current_volatility=current_volatility,
                    current_liquidity=current_liquidity,
                )
            else:
                return self.build_closed_trade_analysis(trade)
        except Exception as exc:
            logger.error("build_trade_analysis failed: %s", exc, exc_info=True)
            return _empty_analysis()

    def build_open_trade_analysis(
        self,
        trade: dict,
        current_regime: Optional[str] = None,
        current_confluence: Optional[float] = None,
        current_momentum: Optional[str] = None,
        current_volatility: Optional[str] = None,
        current_liquidity: Optional[str] = None,
    ) -> dict:
        """
        Build analysis for an open position, including full thesis and live validity.
        """
        try:
            scoring     = score_trade(trade)
            root_causes = analyze_root_causes(scoring)
            recs        = generate_recommendations(root_causes)
            cls_        = scoring.get("classification", "NEUTRAL")

            from .open_trade_thesis import build_open_trade_thesis
            thesis = build_open_trade_thesis(
                trade,
                current_regime=current_regime,
                current_confluence=current_confluence,
                current_momentum=current_momentum,
                current_volatility=current_volatility,
                current_liquidity=current_liquidity,
            )

            analysis = {
                **scoring,
                "classification_emoji": _CLASSIFICATION_EMOJI.get(cls_, "⚖️"),
                "root_causes":     root_causes,
                "recommendations": recs,
                "ai_explanation":  None,
                "thesis":          thesis,
                "forensics":       None,
            }
            try:
                from core.analysis.analysis_contract import stamp_version, validate_open_analysis
                from core.analysis.analysis_metrics import log_analysis_generated, log_contract_violation
                errs = validate_open_analysis(analysis)
                if errs:
                    log_contract_violation("build_open_trade_analysis", errs)
                else:
                    stamp_version(analysis)
                log_analysis_generated(trade.get("symbol", "?"), True,
                                       float(analysis.get("overall_score", 0)),
                                       analysis.get("classification", "?"))
            except Exception:
                pass
            return analysis
        except Exception as exc:
            logger.error("build_open_trade_analysis failed: %s", exc, exc_info=True)
            return _empty_analysis(is_open=True)

    def build_closed_trade_analysis(self, trade: dict) -> dict:
        """
        Build analysis for a closed trade, including decision forensics.
        """
        try:
            scoring     = score_trade(trade)
            root_causes = analyze_root_causes(scoring)
            recs        = generate_recommendations(root_causes)
            cls_        = scoring.get("classification", "NEUTRAL")

            from .decision_forensics import build_decision_forensics
            forensics = build_decision_forensics(trade, scoring)

            analysis = {
                **scoring,
                "classification_emoji": _CLASSIFICATION_EMOJI.get(cls_, "⚖️"),
                "root_causes":     root_causes,
                "recommendations": recs,
                "ai_explanation":  None,
                "thesis":          None,
                "forensics":       forensics,
            }
            try:
                from core.analysis.analysis_contract import stamp_version, validate_closed_analysis
                from core.analysis.analysis_metrics import log_analysis_generated, log_contract_violation
                errs = validate_closed_analysis(analysis)
                if errs:
                    log_contract_violation("build_closed_trade_analysis", errs)
                else:
                    stamp_version(analysis)
                log_analysis_generated(trade.get("symbol", "?"), False,
                                       float(analysis.get("overall_score", 0)),
                                       analysis.get("classification", "?"))
            except Exception:
                pass
            return analysis
        except Exception as exc:
            logger.error("build_closed_trade_analysis failed: %s", exc, exc_info=True)
            return _empty_analysis(is_open=False)

    # ─────────────────────────────────────────────────────────
    # Canonical notification payload
    # ─────────────────────────────────────────────────────────

    def generate_notification_payload(
        self,
        trade:    dict,
        analysis: Optional[dict] = None,
    ) -> dict:
        """
        Produce a flat dict suitable for embedding in trade_opened / trade_closed
        notification templates.  Uses canonical_renderer — never reimplements logic.

        Returns analysis_* keys for template compatibility.
        """
        if analysis is None:
            analysis = self.build_trade_analysis(trade)

        from .canonical_renderer import render_for_channel, MODE_NOTIF_OPEN, MODE_NOTIF_CLOSED
        is_open = analysis.get("is_open", False)
        mode    = MODE_NOTIF_OPEN if is_open else MODE_NOTIF_CLOSED
        rendered = render_for_channel(analysis, mode=mode, trade=trade)

        root_causes = analysis.get("root_causes") or []
        recs        = analysis.get("recommendations") or []
        rr          = analysis.get("rr_ratio")

        rc_summary = (
            "; ".join(
                f"{rc['category']}({rc['severity']})"
                for rc in root_causes[:3]
            ) or "None identified"
        )
        top_rec = recs[0]["action"][:100] if recs else "No specific recommendations."

        thesis    = analysis.get("thesis")    or {}
        forensics = analysis.get("forensics") or {}

        payload = {
            # Core scores — from canonical analysis, NEVER recomputed
            "analysis_overall":        f"{analysis.get('overall_score', 0.0):.1f}",
            "analysis_setup":          f"{analysis.get('setup_score',    0.0):.1f}",
            "analysis_risk":           f"{analysis.get('risk_score',     0.0):.1f}",
            "analysis_execution":      f"{analysis.get('execution_score',0.0):.1f}",
            "analysis_decision":       f"{analysis.get('decision_score', 0.0):.1f}",
            "analysis_classification": analysis.get("classification", "NEUTRAL"),
            "analysis_emoji":          analysis.get("classification_emoji", "⚖️"),
            "analysis_root_causes":    rc_summary,
            "analysis_recommendation": top_rec,
            "analysis_rr":             f"{rr:.2f}" if rr is not None else "—",
            # Phase 2 fields
            "analysis_matrix":         forensics.get("decision_outcome_matrix_label", "—"),
            "analysis_preventability": f"{forensics.get('preventability_score', 0):.0f}",
            "analysis_avoidable":      str(forensics.get("avoidable_loss_flag", False)),
            "analysis_disposition":    thesis.get("current_disposition", "—"),
            "analysis_live_validity":  f"{thesis.get('live_validity_score', 0):.0f}",
            "analysis_thesis_status":  thesis.get("thesis_status", "—"),
            # Rendered compact notification lines
            "analysis_notification_lines": rendered.get("text_lines", []),
            "analysis_summary_line":       rendered.get("summary_line", ""),
        }
        return payload

    # ─────────────────────────────────────────────────────────
    # Post-close persistence + AI enrichment
    # ─────────────────────────────────────────────────────────

    def on_trade_closed(
        self,
        trade:      dict,
        analysis:   Optional[dict] = None,
        ai_enrich:  bool = True,
        on_ai_done: Optional[Callable[[str], None]] = None,
    ) -> Optional[int]:
        """
        Called when a trade is fully closed.
        1. Computes full analysis (if not already provided).
        2. Persists to TradeFeedback table (including Phase 2 forensics).
        3. Optionally triggers async AI enrichment.
        Returns TradeFeedback row id or None on failure.
        """
        if analysis is None:
            analysis = self.build_trade_analysis(trade)

        root_causes = analysis.get("root_causes")  or []
        recs        = analysis.get("recommendations") or []
        forensics   = analysis.get("forensics")    or {}

        # ── Persist ───────────────────────────────────────────
        row_id = None
        try:
            from .trade_feedback_store import persist_trade_feedback
            scoring_subset = {
                k: analysis[k] for k in (
                    "setup_score", "risk_score", "execution_score",
                    "decision_score", "overall_score", "classification",
                    "hard_overrides", "penalty_log",
                    "regime_confidence_at_entry", "htf_confirmed_at_entry",
                )
                if k in analysis
            }
            row_id = persist_trade_feedback(
                trade          = trade,
                scoring_result = scoring_subset,
                root_causes    = root_causes,
                recommendations= recs,
                forensics      = forensics,
            )
        except Exception as exc:
            logger.error("on_trade_closed persist failed: %s", exc)

        # ── Async AI enrichment ───────────────────────────────
        if ai_enrich:
            def _on_ai_complete(explanation: str):
                try:
                    from core.database.engine import get_session
                    from core.database.models import TradeFeedback
                    from .trade_feedback_store import _make_trade_id
                    trade_id = _make_trade_id(trade)
                    with get_session() as sess:
                        rec = sess.query(TradeFeedback).filter(
                            TradeFeedback.trade_id == trade_id
                        ).first()
                        if rec:
                            rec.ai_explanation = explanation
                            sess.commit()
                except Exception as exc2:
                    logger.debug("ai_enrichment DB update failed: %s", exc2)
                if on_ai_done:
                    try:
                        on_ai_done(explanation)
                    except Exception:
                        pass

            try:
                from .ai_enrichment import enrich_async, MODE_UI_CLOSED
                enrich_async(
                    trade          = trade,
                    scoring_result = analysis,
                    root_causes    = root_causes,
                    recommendations= recs,
                    on_complete    = _on_ai_complete,
                    mode           = MODE_UI_CLOSED,
                )
            except Exception as exc:
                logger.debug("ai_enrichment enrich_async failed: %s", exc)

        return row_id

    # ─────────────────────────────────────────────────────────
    # Load persisted feedback
    # ─────────────────────────────────────────────────────────

    def load_analysis(self, trade: dict) -> Optional[dict]:
        """Load a previously persisted analysis for a trade."""
        try:
            from .trade_feedback_store import load_trade_feedback
            return load_trade_feedback(trade)
        except Exception as exc:
            logger.debug("load_analysis failed: %s", exc)
            return None

    # ─────────────────────────────────────────────────────────
    # Adaptive learning integration
    # ─────────────────────────────────────────────────────────

    def generate_tuning_proposals(
        self,
        min_trade_count: int = 10,
        min_occurrence_pct: float = 20.0,
        persist: bool = True,
    ) -> list[dict]:
        """
        Run the full adaptive learning pipeline:
        1. Aggregate feedback summary
        2. Generate tuning proposals from recurring patterns
        3. Optionally persist to database

        Returns list of proposal dicts.
        """
        try:
            from .adaptive_learning import full_feedback_summary
            from .tuning_proposal_generator import (
                generate_tuning_proposals, persist_proposals
            )
            summary   = full_feedback_summary()
            proposals = generate_tuning_proposals(
                summary,
                min_trade_count=min_trade_count,
                min_occurrence_pct=min_occurrence_pct,
            )
            if persist and proposals:
                n = persist_proposals(proposals)
                logger.info("generate_tuning_proposals: persisted %d new proposals", n)
            return proposals
        except Exception as exc:
            logger.error("generate_tuning_proposals failed: %s", exc)
            return []

    def get_pending_proposals(self) -> list[dict]:
        """Load all pending tuning proposals from the database."""
        try:
            from .tuning_proposal_generator import load_pending_proposals
            return load_pending_proposals()
        except Exception as exc:
            logger.debug("get_pending_proposals failed: %s", exc)
            return []

    # ─────────────────────────────────────────────────────────
    # Canonical rendering
    # ─────────────────────────────────────────────────────────

    def render_for_channel(self, analysis: dict, mode: str, trade: Optional[dict] = None) -> dict:
        """
        Render the canonical analysis object for a specific output channel.
        Delegates to canonical_renderer — all channels must call this.
        """
        from .canonical_renderer import render_for_channel as _render
        return _render(analysis, mode=mode, trade=trade)


# ── Module-level singleton ────────────────────────────────────
trade_analysis_service = TradeAnalysisService()


# ── Helpers ───────────────────────────────────────────────────

def _empty_analysis(is_open: bool = False) -> dict:
    return {
        "setup_score":              0.0,
        "risk_score":               0.0,
        "execution_score":          0.0,
        "decision_score":           0.0,
        "overall_score":            0.0,
        "classification":           "NEUTRAL",
        "classification_emoji":     "⚖️",
        "hard_overrides":           [],
        "penalty_log":              {},
        "rr_ratio":                 None,
        "regime_affinity":          0,
        "is_open":                  is_open,
        "regime_confidence_at_entry": 0.0,
        "htf_confirmed_at_entry":   None,
        "root_causes":              [],
        "recommendations":          [],
        "ai_explanation":           None,
        "thesis":                   None,
        "forensics":                None,
    }
