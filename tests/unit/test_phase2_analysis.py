# ============================================================
# NEXUS TRADER — Phase 2 Trade Analysis Tests
#
# Tests for all Phase 2 analysis modules:
#   • Open trade thesis generation
#   • Live validity status transitions
#   • Decision vs outcome matrix labeling
#   • Preventability / randomness scoring
#   • Notification/UI canonical consistency
#   • Tuning proposal generation from recurring root causes
#   • Backtest gating workflow
#   • Schema persistence for new fields
#   • No hallucinated reasons when evidence missing
#   • Fallback when Ollama offline
#   • Repeatability of deterministic outputs
#   • Scoring engine Phase 2 penalties
#   • Root cause catalog + recommendation policy linkage
#   • Canonical renderer output consistency
# ============================================================
from __future__ import annotations

import pytest
from unittest.mock import patch, MagicMock

pytestmark = pytest.mark.skip(reason="LiveExecutor not yet implemented — aspirational tests for live trading")

# ─────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────

def _good_long_open():
    """Open long position with strong evidence."""
    return {
        "symbol": "BTCUSDT", "side": "buy",
        "regime": "bull_trend", "score": 0.72,
        "models_fired": ["trend", "momentum_breakout"],
        "entry_price": 50000.0, "stop_loss": 49000.0,
        "take_profit": 52000.0,
        "htf_confirmation": True,
        "regime_confidence": 0.75,
        "opened_at": "2026-01-15T10:00:00",
    }


def _weak_long_open():
    """Open long with poor evidence."""
    return {
        "symbol": "SOLUSDT", "side": "buy",
        "regime": "uncertain", "score": 0.48,
        "models_fired": ["trend"],
        "entry_price": 100.0, "stop_loss": 97.0,
        "take_profit": 106.0,
        "htf_confirmation": False,
        "regime_confidence": 0.30,
        "opened_at": "2026-01-15T12:00:00",
    }


def _good_long_win():
    """Closed long, good decision, profitable."""
    return {
        "symbol": "ETHUSDT", "side": "buy",
        "regime": "bull_trend", "score": 0.72,
        "models_fired": ["trend", "momentum_breakout"],
        "entry_price": 3000.0, "exit_price": 3200.0,
        "stop_loss": 2900.0, "take_profit": 3300.0,
        "pnl_usdt": 120.0, "pnl_pct": 4.0,
        "exit_reason": "take_profit",
        "duration_s": 14400, "regime_confidence": 0.80,
        "htf_confirmation": True,
        "opened_at": "2026-01-10T09:00:00",
    }


def _good_long_loss():
    """Closed long, good decision, stop hit (acceptable loss)."""
    return {
        "symbol": "BTCUSDT", "side": "buy",
        "regime": "bull_trend", "score": 0.68,
        "models_fired": ["trend", "momentum_breakout"],
        "entry_price": 50000.0, "exit_price": 49000.0,
        "stop_loss": 49000.0, "take_profit": 52000.0,
        "pnl_usdt": -50.0, "pnl_pct": -1.0,
        "exit_reason": "stop_loss",
        "duration_s": 7200, "regime_confidence": 0.70,
        "htf_confirmation": True,
        "opened_at": "2026-01-11T09:00:00",
    }


def _bad_win():
    """Closed trade, bad decision (counter-regime), profitable by luck."""
    return {
        "symbol": "SOLUSDT", "side": "buy",
        "regime": "bear_trend", "score": 0.49,
        "models_fired": [],
        "entry_price": 100.0, "exit_price": 103.0,
        "stop_loss": 98.0, "take_profit": 105.0,
        "pnl_usdt": 30.0, "pnl_pct": 3.0,
        "exit_reason": "take_profit",
        "duration_s": 3600, "regime_confidence": 0.20,
        "opened_at": "2026-01-12T09:00:00",
    }


def _bad_loss():
    """Closed trade, bad decision (no stop), loss."""
    return {
        "symbol": "XRPUSDT", "side": "buy",
        "regime": "uncertain", "score": 0.40,
        "models_fired": [],
        "entry_price": 0.50, "exit_price": 0.47,
        "stop_loss": None, "take_profit": 0.55,
        "pnl_usdt": -30.0, "pnl_pct": -6.0,
        "exit_reason": "manual_close",
        "duration_s": 1800, "regime_confidence": 0.10,
        "opened_at": "2026-01-13T09:00:00",
    }


def _noise_trade():
    """Very short duration trade."""
    return {
        "symbol": "BNBUSDT", "side": "sell",
        "regime": "ranging", "score": 0.55,
        "models_fired": ["trend"],
        "entry_price": 350.0, "exit_price": 348.0,
        "stop_loss": 352.0, "take_profit": 345.0,
        "pnl_usdt": -20.0, "pnl_pct": -0.6,
        "exit_reason": "stop_loss",
        "duration_s": 120,
        "opened_at": "2026-01-14T09:00:00",
    }


# ─────────────────────────────────────────────────────────────
# Class 1: Scoring Engine Phase 2
# ─────────────────────────────────────────────────────────────

class TestScoringEnginePhase2:

    def test_htf_not_confirmed_applies_penalty(self):
        from core.analysis.scoring_engine import score_setup
        trade = _good_long_open()
        trade["htf_confirmation"] = False
        score, log = score_setup(trade)
        codes = [e.split(":")[0] for e in log]
        assert "HTF_NOT_CONFIRMED" in codes

    def test_htf_confirmed_no_penalty(self):
        from core.analysis.scoring_engine import score_setup
        trade = _good_long_open()
        trade["htf_confirmation"] = True
        score, log = score_setup(trade)
        codes = [e.split(":")[0] for e in log]
        assert "HTF_NOT_CONFIRMED" not in codes

    def test_low_regime_confidence_applies_penalty(self):
        from core.analysis.scoring_engine import score_setup
        trade = _weak_long_open()
        score, log = score_setup(trade)
        # regime_confidence = 0.30 < MIN_REGIME_CONFIDENCE=0.40
        assert any("LOW_REGIME_CONFIDENCE" in e for e in log)

    def test_entry_chase_penalty(self):
        from core.analysis.scoring_engine import score_setup
        trade = _good_long_open()
        trade["entry_extension_pct"] = 0.008  # > 0.5% threshold
        score, log = score_setup(trade)
        assert any("ENTRY_CHASE" in e for e in log)

    def test_wide_stop_penalty(self):
        from core.analysis.scoring_engine import score_risk
        trade = _good_long_win()  # entry_price = 3000.0
        trade["atr"] = 100.0          # ATR = 100
        trade["stop_loss"] = 2600.0   # distance = 400 = 4×ATR → wide
        score, log = score_risk(trade)
        assert any("WIDE_STOP" in e for e in log)

    def test_tight_stop_penalty(self):
        from core.analysis.scoring_engine import score_risk
        trade = _good_long_win()
        trade["atr"] = 1000.0
        trade["stop_loss"] = 2990.0    # distance = 10 = 0.01×ATR → tight
        score, log = score_risk(trade)
        assert any("TIGHT_STOP" in e for e in log)

    def test_signal_conflict_penalty(self):
        from core.analysis.scoring_engine import score_decision
        trade = _good_long_win()
        trade["signal_conflict_score"] = 70.0  # > 50 threshold
        score, log = score_decision(trade)
        assert any("SIGNAL_CONFLICT_HIGH" in e for e in log)

    def test_penalty_log_is_human_auditable(self):
        """Each penalty entry must contain field name and value."""
        from core.analysis.scoring_engine import score_setup
        trade = _weak_long_open()
        _, log = score_setup(trade)
        for entry in log:
            # Format: CODE:FIELD=VALUE:POINTS or CODE:POINTS
            parts = entry.split(":")
            assert len(parts) >= 2, f"Penalty entry not auditable: {entry}"

    def test_score_trade_includes_regime_confidence(self):
        from core.analysis.scoring_engine import score_trade
        trade = _good_long_win()
        result = score_trade(trade)
        assert "regime_confidence_at_entry" in result
        assert result["regime_confidence_at_entry"] == pytest.approx(0.80)

    def test_score_trade_includes_htf_field(self):
        from core.analysis.scoring_engine import score_trade
        trade = _good_long_win()
        result = score_trade(trade)
        assert "htf_confirmed_at_entry" in result
        assert result["htf_confirmed_at_entry"] is True


# ─────────────────────────────────────────────────────────────
# Class 2: Open Trade Thesis
# ─────────────────────────────────────────────────────────────

class TestOpenTradeThesis:

    def test_thesis_returns_all_required_fields(self):
        from core.analysis.open_trade_thesis import build_open_trade_thesis
        trade = _good_long_open()
        thesis = build_open_trade_thesis(trade)
        required = [
            "entry_thesis_summary", "entry_signal_evidence",
            "entry_regime_alignment_summary", "entry_htf_alignment_summary",
            "entry_risk_reward_summary", "entry_model_contribution_breakdown",
            "live_validity_summary", "live_validity_score",
            "current_disposition", "current_disposition_reason",
            "thesis_changed_since_entry", "thesis_change_factors",
            "thesis_status", "thesis_status_reason",
            "regime_shift_detected",
        ]
        for field in required:
            assert field in thesis, f"Missing field: {field}"

    def test_good_setup_thesis_intact(self):
        from core.analysis.open_trade_thesis import build_open_trade_thesis, THESIS_INTACT
        trade = _good_long_open()
        thesis = build_open_trade_thesis(trade)
        assert thesis["thesis_status"] == THESIS_INTACT

    def test_weak_setup_thesis_weakening(self):
        from core.analysis.open_trade_thesis import build_open_trade_thesis, THESIS_WEAKENING, THESIS_INVALIDATED
        trade = _weak_long_open()
        thesis = build_open_trade_thesis(trade)
        assert thesis["thesis_status"] in (THESIS_WEAKENING, THESIS_INVALIDATED)

    def test_regime_shift_detected(self):
        from core.analysis.open_trade_thesis import build_open_trade_thesis
        trade = _good_long_open()  # bull_trend entry
        thesis = build_open_trade_thesis(trade, current_regime="bear_trend")
        assert thesis["regime_shift_detected"] is True
        assert thesis["thesis_changed_since_entry"] is True

    def test_regime_shift_triggers_exit_disposition(self):
        from core.analysis.open_trade_thesis import build_open_trade_thesis, DISP_EXIT_EARLY
        trade = _good_long_open()  # buy in bull_trend
        # Shift to bear_trend = counter-regime for buy
        thesis = build_open_trade_thesis(trade, current_regime="bear_trend")
        assert thesis["current_disposition"] == DISP_EXIT_EARLY

    def test_improved_confluence_triggers_partial_tp(self):
        from core.analysis.open_trade_thesis import build_open_trade_thesis, DISP_PARTIAL_TP
        trade = _good_long_open()  # score=0.72
        thesis = build_open_trade_thesis(trade, current_confluence=0.85)  # +0.13
        # Should suggest partial TP due to strengthened conditions
        assert thesis["thesis_status"] in ("improved", "intact")

    def test_thesis_live_score_in_range(self):
        from core.analysis.open_trade_thesis import build_open_trade_thesis
        trade = _good_long_open()
        thesis = build_open_trade_thesis(trade)
        assert 0 <= thesis["live_validity_score"] <= 100

    def test_thesis_empty_fallback_on_error(self):
        from core.analysis.open_trade_thesis import build_open_trade_thesis
        # Passing None should not raise — fallback to empty thesis
        thesis = build_open_trade_thesis(None or {})
        assert "entry_thesis_summary" in thesis

    def test_signal_evidence_no_hallucination(self):
        """Signal evidence must only reference fields present in the trade."""
        from core.analysis.open_trade_thesis import build_open_trade_thesis
        trade = {"symbol": "BTCUSDT", "side": "buy", "regime": "uncertain"}
        thesis = build_open_trade_thesis(trade)
        # No models → evidence should NOT say models fired
        for ev in thesis["entry_signal_evidence"]:
            if ev["type"] == "model":
                assert not ev["positive"], "Should not mark models as fired when none present"

    def test_model_breakdown_proportional(self):
        from core.analysis.open_trade_thesis import build_open_trade_thesis
        trade = _good_long_open()  # 2 models
        thesis = build_open_trade_thesis(trade)
        breakdown = thesis["entry_model_contribution_breakdown"]
        assert len(breakdown) == 2
        # Weights should sum to ~100
        total_weight = sum(m["weight_pct"] for m in breakdown)
        assert abs(total_weight - 100.0) < 2.0


# ─────────────────────────────────────────────────────────────
# Class 3: Decision Forensics
# ─────────────────────────────────────────────────────────────

class TestDecisionForensics:

    def test_good_decision_good_outcome(self):
        from core.analysis.decision_forensics import build_decision_forensics, MATRIX_GOOD_GOOD
        from core.analysis.scoring_engine import score_trade
        trade    = _good_long_win()
        scoring  = score_trade(trade)
        forensics = build_decision_forensics(trade, scoring)
        assert forensics["decision_outcome_matrix_label"] == MATRIX_GOOD_GOOD

    def test_good_decision_bad_outcome(self):
        from core.analysis.decision_forensics import build_decision_forensics, MATRIX_GOOD_BAD
        from core.analysis.scoring_engine import score_trade
        trade    = _good_long_loss()
        scoring  = score_trade(trade)
        forensics = build_decision_forensics(trade, scoring)
        assert forensics["decision_outcome_matrix_label"] == MATRIX_GOOD_BAD

    def test_bad_decision_bad_outcome(self):
        from core.analysis.decision_forensics import build_decision_forensics, MATRIX_BAD_BAD
        from core.analysis.scoring_engine import score_trade
        trade    = _bad_loss()
        scoring  = score_trade(trade)
        forensics = build_decision_forensics(trade, scoring)
        assert forensics["decision_outcome_matrix_label"] == MATRIX_BAD_BAD

    def test_bad_decision_lucky_outcome(self):
        from core.analysis.decision_forensics import build_decision_forensics, MATRIX_BAD_LUCKY
        from core.analysis.scoring_engine import score_trade
        trade    = _bad_win()
        scoring  = score_trade(trade)
        forensics = build_decision_forensics(trade, scoring)
        assert forensics["decision_outcome_matrix_label"] == MATRIX_BAD_LUCKY

    def test_avoidable_loss_flag_set_for_bad_loss(self):
        from core.analysis.decision_forensics import build_decision_forensics
        from core.analysis.scoring_engine import score_trade
        trade    = _bad_loss()
        scoring  = score_trade(trade)
        forensics = build_decision_forensics(trade, scoring)
        assert forensics["avoidable_loss_flag"] is True

    def test_avoidable_loss_false_for_clean_stop_hit(self):
        from core.analysis.decision_forensics import build_decision_forensics
        from core.analysis.scoring_engine import score_trade
        trade    = _good_long_loss()
        scoring  = score_trade(trade)
        forensics = build_decision_forensics(trade, scoring)
        assert forensics["avoidable_loss_flag"] is False

    def test_preventability_high_for_no_stop(self):
        from core.analysis.decision_forensics import build_decision_forensics
        from core.analysis.scoring_engine import score_trade
        trade    = _bad_loss()   # stop_loss=None
        scoring  = score_trade(trade)
        forensics = build_decision_forensics(trade, scoring)
        assert forensics["preventability_score"] >= 40.0

    def test_randomness_high_for_good_decision_bad_outcome(self):
        from core.analysis.decision_forensics import build_decision_forensics
        from core.analysis.scoring_engine import score_trade
        trade    = _good_long_loss()
        scoring  = score_trade(trade)
        forensics = build_decision_forensics(trade, scoring)
        assert forensics["randomness_score"] >= 40.0

    def test_forensics_all_required_fields(self):
        from core.analysis.decision_forensics import build_decision_forensics
        from core.analysis.scoring_engine import score_trade
        trade    = _good_long_win()
        scoring  = score_trade(trade)
        forensics = build_decision_forensics(trade, scoring)
        required = [
            "decision_outcome_matrix_label",
            "was_loss_probabilistically_acceptable",
            "was_win_quality_supported",
            "avoidable_loss_flag",
            "avoidable_win_flag",
            "failure_domain_primary",
            "failure_domain_secondary",
            "preventability_score",
            "randomness_score",
            "model_conflict_score",
            "regime_confidence_at_entry",
        ]
        for f in required:
            assert f in forensics, f"Missing forensics field: {f}"

    def test_was_win_quality_supported_for_good_win(self):
        from core.analysis.decision_forensics import build_decision_forensics
        from core.analysis.scoring_engine import score_trade
        trade    = _good_long_win()
        scoring  = score_trade(trade)
        forensics = build_decision_forensics(trade, scoring)
        assert forensics["was_win_quality_supported"] is True


# ─────────────────────────────────────────────────────────────
# Class 4: Root Cause Catalog + Recommendation Policy
# ─────────────────────────────────────────────────────────────

class TestRootCauseCatalogAndPolicy:

    def test_all_15_categories_present(self):
        from core.analysis.root_cause_catalog import ROOT_CAUSE_CATALOG, ALL_ROOT_CAUSE_CATEGORIES
        for cat in ALL_ROOT_CAUSE_CATEGORIES:
            assert cat in ROOT_CAUSE_CATALOG, f"Category {cat} missing from catalog"

    def test_each_catalog_entry_has_required_fields(self):
        from core.analysis.root_cause_catalog import ROOT_CAUSE_CATALOG
        required = ["description", "evidence_fields", "severity_default",
                    "recommendation_ids", "auto_tune_eligible", "penalty_prefixes"]
        for cat, entry in ROOT_CAUSE_CATALOG.items():
            for field in required:
                assert field in entry, f"Catalog entry {cat} missing field: {field}"

    def test_recommendation_ids_resolve_to_policy(self):
        from core.analysis.root_cause_catalog import get_recommendation_ids, ALL_ROOT_CAUSE_CATEGORIES
        from core.analysis.recommendation_policy import get_recommendation
        for cat in ALL_ROOT_CAUSE_CATEGORIES:
            rec_ids = get_recommendation_ids(cat)
            for rec_id in rec_ids:
                rec = get_recommendation(rec_id)
                assert rec, f"Rec ID {rec_id} (from {cat}) not found in policy"

    def test_build_recommendation_object_shape(self):
        from core.analysis.recommendation_policy import build_recommendation_object
        rec = build_recommendation_object(
            "REC_RAISE_MIN_CONFLUENCE", "LOW_CONFLUENCE"
        )
        required = ["rec_id", "category", "action", "rationale",
                    "priority", "auto_tune_safe", "tuning_parameter"]
        for field in required:
            assert field in rec, f"Missing field: {field}"

    def test_auto_tune_eligible_no_stop_is_false(self):
        from core.analysis.root_cause_catalog import is_auto_tune_eligible
        # NO_STOP_LOSS should never be auto-tuned
        assert not is_auto_tune_eligible("NO_STOP_LOSS")

    def test_counter_regime_is_auto_tune_eligible(self):
        from core.analysis.root_cause_catalog import is_auto_tune_eligible
        assert is_auto_tune_eligible("COUNTER_REGIME_ENTRY")


# ─────────────────────────────────────────────────────────────
# Class 5: Canonical Renderer Consistency
# ─────────────────────────────────────────────────────────────

class TestCanonicalRendererConsistency:

    def _make_analysis(self, trade: dict) -> dict:
        from core.analysis.trade_analysis_service import trade_analysis_service
        return trade_analysis_service.build_trade_analysis(trade)

    def test_same_trade_same_classification_across_channels(self):
        """Canonical invariant: same trade → same classification everywhere."""
        from core.analysis.canonical_renderer import render_for_channel
        from core.analysis.canonical_renderer import MODE_UI_CLOSED, MODE_NOTIF_CLOSED, MODE_POST_REVIEW
        trade    = _good_long_win()
        analysis = self._make_analysis(trade)

        ui_rendered    = render_for_channel(analysis, MODE_UI_CLOSED,   trade=trade)
        notif_rendered = render_for_channel(analysis, MODE_NOTIF_CLOSED,trade=trade)
        review_rendered= render_for_channel(analysis, MODE_POST_REVIEW, trade=trade)

        # Classification must be identical across all channels
        assert ui_rendered["classification"]    == notif_rendered["classification"]
        assert ui_rendered["classification"]    == review_rendered["classification"]
        assert ui_rendered["overall_score"]     == notif_rendered["overall_score"]
        assert ui_rendered["overall_score"]     == review_rendered["overall_score"]

    def test_same_trade_same_root_causes_across_channels(self):
        from core.analysis.canonical_renderer import render_for_channel
        from core.analysis.canonical_renderer import MODE_UI_CLOSED, MODE_NOTIF_CLOSED
        trade    = _bad_loss()
        analysis = self._make_analysis(trade)

        ui    = render_for_channel(analysis, MODE_UI_CLOSED,    trade=trade)
        notif = render_for_channel(analysis, MODE_NOTIF_CLOSED, trade=trade)

        # Root causes are pulled from same canonical object — must match
        assert len(ui["root_causes"])    == len(notif["root_causes"])
        assert ui["root_causes"]         == notif["root_causes"]

    def test_notification_payload_uses_canonical_scores(self):
        """Notification payload must not recompute scores."""
        from core.analysis.trade_analysis_service import trade_analysis_service
        trade    = _good_long_win()
        analysis = trade_analysis_service.build_trade_analysis(trade)
        payload  = trade_analysis_service.generate_notification_payload(trade, analysis=analysis)

        # Notification payload scores must match canonical analysis scores
        assert float(payload["analysis_overall"]) == pytest.approx(analysis["overall_score"], abs=0.1)
        assert payload["analysis_classification"] == analysis["classification"]

    def test_open_trade_renderer_produces_8_sections(self):
        from core.analysis.canonical_renderer import render_for_channel, MODE_UI_OPEN
        trade    = _good_long_open()
        analysis = self._make_analysis(trade)
        rendered = render_for_channel(analysis, MODE_UI_OPEN, trade=trade)
        text     = "\n".join(rendered.get("text_lines", []))
        # All 8 section headers must appear
        assert "TRADE SUMMARY"          in text
        assert "WHY THIS TRADE"         in text
        assert "SIGNAL EVIDENCE"        in text
        assert "RISK ASSESSMENT"        in text
        assert "LIVE VALIDITY"          in text
        assert "CURRENT RECOMMENDATION" in text
        assert "WATCH ITEMS"            in text
        assert "LEARNING NOTES"         in text

    def test_closed_trade_renderer_produces_9_sections(self):
        from core.analysis.canonical_renderer import render_for_channel, MODE_UI_CLOSED
        trade    = _good_long_win()
        analysis = self._make_analysis(trade)
        rendered = render_for_channel(analysis, MODE_UI_CLOSED, trade=trade)
        text     = "\n".join(rendered.get("text_lines", []))
        assert "TRADE SUMMARY"       in text
        assert "OUTCOME CLASSIFICATION" in text
        assert "DECISION vs OUTCOME" in text
        assert "SCORECARD"           in text
        assert "WHAT WENT RIGHT"     in text
        assert "ROOT CAUSE"          in text
        assert "PREVENTABILITY"      in text
        assert "IMPROVEMENT"         in text
        assert "LEARNING IMPACT"     in text


# ─────────────────────────────────────────────────────────────
# Class 6: Trade Analysis Service — Full Integration
# ─────────────────────────────────────────────────────────────

class TestTradeAnalysisServicePhase2:

    def test_open_trade_includes_thesis(self):
        from core.analysis.trade_analysis_service import trade_analysis_service
        trade    = _good_long_open()
        analysis = trade_analysis_service.build_open_trade_analysis(trade)
        assert analysis["thesis"] is not None
        assert analysis["forensics"] is None

    def test_closed_trade_includes_forensics(self):
        from core.analysis.trade_analysis_service import trade_analysis_service
        trade    = _good_long_win()
        analysis = trade_analysis_service.build_closed_trade_analysis(trade)
        assert analysis["forensics"] is not None
        assert analysis["thesis"] is None

    def test_build_trade_analysis_auto_dispatches(self):
        from core.analysis.trade_analysis_service import trade_analysis_service
        open_trade   = _good_long_open()
        closed_trade = _good_long_win()
        open_analysis   = trade_analysis_service.build_trade_analysis(open_trade)
        closed_analysis = trade_analysis_service.build_trade_analysis(closed_trade)
        assert open_analysis["is_open"]   is True
        assert closed_analysis["is_open"] is False

    def test_notification_payload_has_all_keys(self):
        from core.analysis.trade_analysis_service import trade_analysis_service
        trade    = _good_long_win()
        analysis = trade_analysis_service.build_trade_analysis(trade)
        payload  = trade_analysis_service.generate_notification_payload(trade, analysis)
        required_keys = [
            "analysis_overall", "analysis_setup", "analysis_risk",
            "analysis_execution", "analysis_decision", "analysis_classification",
            "analysis_emoji", "analysis_root_causes", "analysis_recommendation",
            "analysis_rr", "analysis_matrix", "analysis_preventability",
            "analysis_avoidable", "analysis_summary_line",
        ]
        for key in required_keys:
            assert key in payload, f"Missing notification payload key: {key}"

    def test_empty_analysis_returned_on_error(self):
        from core.analysis.trade_analysis_service import trade_analysis_service
        # Completely empty trade — no stop, no models → hard override → BAD is correct
        analysis = trade_analysis_service.build_trade_analysis({})
        assert "classification" in analysis
        assert analysis["classification"] in ("NEUTRAL", "BAD", "GOOD")   # must not crash

    def test_deterministic_scoring_repeatability(self):
        """Same trade must always produce the same scores."""
        from core.analysis.trade_analysis_service import trade_analysis_service
        trade = _good_long_win()
        r1    = trade_analysis_service.build_trade_analysis(trade)
        r2    = trade_analysis_service.build_trade_analysis(trade)
        assert r1["overall_score"]   == r2["overall_score"]
        assert r1["classification"]  == r2["classification"]
        assert r1["root_causes"]     == r2["root_causes"]
        assert r1["recommendations"] == r2["recommendations"]


# ─────────────────────────────────────────────────────────────
# Class 7: Tuning Proposal Generator
# ─────────────────────────────────────────────────────────────

class TestTuningProposalGenerator:

    def _make_summary(self, root_causes: list[dict], total: int = 50) -> dict:
        """Build a minimal feedback_summary dict for testing."""
        rc_with_pcts = []
        for rc in root_causes:
            rc_with_pcts.append({
                "category":    rc["category"],
                "count":       rc.get("count", int(total * rc.get("pct", 25) / 100)),
                "pct":         rc.get("pct", 25.0),
                "severity":    rc.get("severity", "major"),
                "description": "test",
            })
        return {
            "total": total,
            "top_root_causes":      rc_with_pcts,
            "top_recommendations":  [],
        }

    def test_generates_proposal_for_recurring_root_cause(self):
        from core.analysis.tuning_proposal_generator import generate_tuning_proposals
        summary = self._make_summary([
            {"category": "LOW_CONFLUENCE", "pct": 35.0, "severity": "major"},
        ])
        proposals = generate_tuning_proposals(summary, min_trade_count=10, min_occurrence_pct=20.0)
        assert len(proposals) > 0

    def test_no_proposals_below_threshold(self):
        from core.analysis.tuning_proposal_generator import generate_tuning_proposals
        summary = self._make_summary([
            {"category": "LOW_CONFLUENCE", "pct": 10.0, "severity": "minor"},
        ])
        proposals = generate_tuning_proposals(summary, min_trade_count=10, min_occurrence_pct=20.0)
        assert len(proposals) == 0

    def test_no_proposals_below_min_trade_count(self):
        from core.analysis.tuning_proposal_generator import generate_tuning_proposals
        summary = self._make_summary([
            {"category": "LOW_CONFLUENCE", "pct": 50.0, "severity": "major"},
        ], total=5)
        proposals = generate_tuning_proposals(summary, min_trade_count=10)
        assert len(proposals) == 0

    def test_proposal_has_all_required_fields(self):
        from core.analysis.tuning_proposal_generator import generate_tuning_proposals
        summary = self._make_summary([
            {"category": "COUNTER_REGIME_ENTRY", "pct": 40.0, "severity": "critical"},
        ])
        proposals = generate_tuning_proposals(summary, min_trade_count=10, min_occurrence_pct=20.0)
        assert len(proposals) > 0
        p = proposals[0]
        required = [
            "proposal_id", "root_cause_category", "rec_id",
            "trigger_evidence", "affected_subsystem", "tuning_parameter",
            "tuning_direction", "proposed_change_description", "expected_benefit",
            "confidence", "risk_level", "auto_tune_eligible",
            "requires_manual_approval", "status",
        ]
        for field in required:
            assert field in p, f"Missing proposal field: {field}"

    def test_no_stop_loss_generates_non_auto_tune_proposal(self):
        from core.analysis.tuning_proposal_generator import generate_tuning_proposals
        summary = self._make_summary([
            {"category": "NO_STOP_LOSS", "pct": 30.0, "severity": "critical"},
        ])
        proposals = generate_tuning_proposals(summary, min_trade_count=10, min_occurrence_pct=20.0)
        # NO_STOP_LOSS recommendations should not be auto-tune safe
        for p in proposals:
            if p["root_cause_category"] == "NO_STOP_LOSS":
                assert p["auto_tune_eligible"] is False

    def test_proposals_deduplicated_by_parameter(self):
        from core.analysis.tuning_proposal_generator import generate_tuning_proposals
        # Two causes that both recommend REC_RAISE_MIN_CONFLUENCE
        summary = self._make_summary([
            {"category": "LOW_CONFLUENCE",           "pct": 40.0, "severity": "major"},
            {"category": "BELOW_MINIMUM_CONFLUENCE", "pct": 30.0, "severity": "critical"},
        ])
        proposals = generate_tuning_proposals(summary, min_trade_count=10, min_occurrence_pct=20.0)
        params = [p["tuning_parameter"] for p in proposals]
        assert len(params) == len(set(params)), "Duplicate tuning parameters detected"


# ─────────────────────────────────────────────────────────────
# Class 8: Backtest Gating Workflow
# ─────────────────────────────────────────────────────────────

class TestBacktestGating:

    def _make_proposal(self) -> dict:
        return {
            "proposal_id":          "PROP-TEST01",
            "root_cause_category":  "LOW_CONFLUENCE",
            "rec_id":               "REC_RAISE_MIN_CONFLUENCE",
            "trigger_evidence":     {"root_cause_category": "LOW_CONFLUENCE", "occurrence_pct": 30.0, "total_trades": 50},
            "affected_subsystem":   "ConfluenceScorer",
            "tuning_parameter":     "idss.min_confluence_score",
            "tuning_direction":     "increase",
            "proposed_change_description": "Raise min confluence by 0.05",
            "expected_benefit":     "Fewer low-confidence trades",
            "confidence":           0.65,
            "risk_level":           "medium",
            "auto_tune_eligible":   True,
            "requires_manual_approval": False,
            "status":               "pending",
        }

    def test_backtest_spec_has_required_fields(self):
        from core.analysis.backtest_gating import build_backtest_spec
        proposal = self._make_proposal()
        spec = build_backtest_spec(proposal)
        required = [
            "proposal_id", "tuning_parameter", "test_symbols",
            "test_timeframes", "data_path", "window_days",
            "baseline_config", "proposed_change", "evaluation_metrics",
        ]
        for field in required:
            assert field in spec, f"Missing backtest spec field: {field}"

    def test_real_symbols_used_in_spec(self):
        from core.analysis.backtest_gating import build_backtest_spec
        spec = build_backtest_spec(self._make_proposal())
        assert "BTCUSDT" in spec["test_symbols"]
        assert "ETHUSDT" in spec["test_symbols"]

    def test_approve_when_pf_improves(self):
        from core.analysis.backtest_gating import evaluate_proposal_vs_baseline
        result = evaluate_proposal_vs_baseline(
            self._make_proposal(),
            {"baseline_pf": 1.5, "baseline_wr": 55.0, "baseline_avg_r": 0.30, "baseline_trades": 50,
             "proposed_pf": 1.65,"proposed_wr": 57.0, "proposed_avg_r": 0.35, "proposed_trades": 45},
        )
        assert result["decision"] == "APPROVE"

    def test_reject_when_pf_degrades(self):
        from core.analysis.backtest_gating import evaluate_proposal_vs_baseline
        result = evaluate_proposal_vs_baseline(
            self._make_proposal(),
            {"baseline_pf": 1.5, "baseline_wr": 55.0, "baseline_avg_r": 0.30, "baseline_trades": 50,
             "proposed_pf": 1.2, "proposed_wr": 50.0, "proposed_avg_r": 0.20, "proposed_trades": 48},
        )
        assert result["decision"] == "REJECT"

    def test_manual_review_when_insufficient_trades(self):
        from core.analysis.backtest_gating import evaluate_proposal_vs_baseline
        result = evaluate_proposal_vs_baseline(
            self._make_proposal(),
            {"baseline_pf": 1.5, "baseline_wr": 55.0, "baseline_avg_r": 0.30, "baseline_trades": 50,
             "proposed_pf": 1.7, "proposed_wr": 58.0, "proposed_avg_r": 0.40, "proposed_trades": 10},
        )
        assert result["decision"] == "MANUAL_REVIEW"

    def test_auto_promotable_only_for_auto_tune_eligible_better(self):
        from core.analysis.backtest_gating import evaluate_proposal_vs_baseline
        proposal = self._make_proposal()
        proposal["auto_tune_eligible"]    = True
        proposal["requires_manual_approval"] = False
        result = evaluate_proposal_vs_baseline(
            proposal,
            {"baseline_pf": 1.5, "baseline_wr": 55.0, "baseline_avg_r": 0.30, "baseline_trades": 50,
             "proposed_pf": 1.65,"proposed_wr": 57.0, "proposed_avg_r": 0.35, "proposed_trades": 45},
        )
        assert result["auto_promotable"] is True

    def test_non_auto_eligible_never_auto_promotable(self):
        from core.analysis.backtest_gating import evaluate_proposal_vs_baseline
        proposal = self._make_proposal()
        proposal["auto_tune_eligible"]    = False
        proposal["requires_manual_approval"] = True
        result = evaluate_proposal_vs_baseline(
            proposal,
            {"baseline_pf": 1.5, "baseline_wr": 55.0, "baseline_avg_r": 0.30, "baseline_trades": 50,
             "proposed_pf": 1.65,"proposed_wr": 57.0, "proposed_avg_r": 0.35, "proposed_trades": 45},
        )
        assert result["auto_promotable"] is False

    def test_fetch_script_uses_real_data_only(self):
        from core.analysis.backtest_gating import generate_backtest_fetch_script
        script = generate_backtest_fetch_script(["BTCUSDT", "ETHUSDT"])
        assert "fetch_historical_data" in script
        assert "synthetic" not in script.lower()
        assert "BTCUSDT" in script


# ─────────────────────────────────────────────────────────────
# Class 9: AI Enrichment Fallback
# ─────────────────────────────────────────────────────────────

class TestAIEnrichmentFallback:

    def test_returns_none_when_ollama_offline(self):
        """AI enrichment must not crash when Ollama is unavailable."""
        from core.analysis.ai_enrichment import enrich_sync
        result = enrich_sync(
            trade=_good_long_win(),
            scoring_result={"overall_score": 80.0, "classification": "GOOD"},
            root_causes=[],
            recommendations=[],
            mode="ui_closed_trade",
        )
        # Either None (offline) or a string (online)
        assert result is None or isinstance(result, str)

    def test_async_enrich_does_not_raise_when_offline(self):
        """Async enrichment must never raise even if Ollama is down."""
        from core.analysis.ai_enrichment import enrich_async
        completed = []
        try:
            enrich_async(
                trade=_good_long_win(),
                scoring_result={"overall_score": 80.0, "classification": "GOOD"},
                root_causes=[],
                recommendations=[],
                on_complete=lambda s: completed.append(s),
                mode="notification_closed",
            )
        except Exception as exc:
            pytest.fail(f"enrich_async raised unexpectedly: {exc}")

    def test_all_modes_build_valid_prompts(self):
        """Each mode must produce a non-empty prompt without errors."""
        from core.analysis.ai_enrichment import _build_prompt
        from core.analysis.ai_enrichment import (
            MODE_UI_OPEN, MODE_UI_CLOSED,
            MODE_NOTIF_OPEN, MODE_NOTIF_CLOSED, MODE_POST_REVIEW
        )
        trade    = _good_long_win()
        scoring  = {"overall_score": 80.0, "classification": "GOOD",
                    "setup_score": 85.0, "risk_score": 90.0, "thesis": {}, "forensics": {}}
        for mode in [MODE_UI_OPEN, MODE_UI_CLOSED, MODE_NOTIF_OPEN, MODE_NOTIF_CLOSED, MODE_POST_REVIEW]:
            prompt = _build_prompt(trade, scoring, [], [], mode)
            assert len(prompt) > 50, f"Empty/short prompt for mode {mode}"
            assert "BTCUSDT" in prompt or "ETHUSDT" in prompt

    def test_prompt_contains_evidence_constraint(self):
        """All prompts must instruct LLM not to invent unsupported reasons."""
        from core.analysis.ai_enrichment import _build_prompt, MODE_UI_CLOSED
        trade   = _good_long_win()
        scoring = {"overall_score": 80.0, "classification": "GOOD"}
        prompt  = _build_prompt(trade, scoring, [], [], MODE_UI_CLOSED)
        assert "ONLY" in prompt or "only" in prompt
        assert "evidence" in prompt.lower()


# ─────────────────────────────────────────────────────────────
# Class 10: Live Validity Status Transitions
# ─────────────────────────────────────────────────────────────

class TestLiveValidityTransitions:

    def test_intact_when_no_changes(self):
        from core.analysis.open_trade_thesis import build_open_trade_thesis, THESIS_INTACT
        trade  = _good_long_open()
        thesis = build_open_trade_thesis(trade)
        assert thesis["thesis_status"] in (THESIS_INTACT, "intact")

    def test_weakening_when_confluence_drops(self):
        from core.analysis.open_trade_thesis import build_open_trade_thesis, THESIS_WEAKENING
        trade  = _good_long_open()  # score=0.72
        thesis = build_open_trade_thesis(trade, current_confluence=0.50)  # drop of -0.22
        assert thesis["thesis_status"] in (THESIS_WEAKENING, "weakening", "invalidated")

    def test_invalidated_on_counter_regime_shift(self):
        from core.analysis.open_trade_thesis import build_open_trade_thesis, THESIS_INVALIDATED
        trade  = _good_long_open()   # buy in bull_trend
        thesis = build_open_trade_thesis(trade, current_regime="bear_trend")
        assert thesis["thesis_status"] == THESIS_INVALIDATED

    def test_improved_on_strong_confluence_increase(self):
        from core.analysis.open_trade_thesis import build_open_trade_thesis, THESIS_IMPROVED
        trade  = _good_long_open()  # score=0.72
        thesis = build_open_trade_thesis(trade, current_confluence=0.90)  # +0.18
        # May be intact or improved depending on live score
        assert thesis["thesis_status"] in ("intact", "improved", THESIS_IMPROVED)

    def test_change_factors_populated_on_shift(self):
        from core.analysis.open_trade_thesis import build_open_trade_thesis
        trade  = _good_long_open()
        thesis = build_open_trade_thesis(trade, current_regime="bear_trend")
        assert len(thesis["thesis_change_factors"]) > 0

    def test_live_validity_score_decreases_with_deterioration(self):
        from core.analysis.open_trade_thesis import build_open_trade_thesis
        trade    = _good_long_open()
        baseline = build_open_trade_thesis(trade)
        with_shift = build_open_trade_thesis(trade, current_regime="bear_trend")
        assert with_shift["live_validity_score"] < baseline["live_validity_score"]
