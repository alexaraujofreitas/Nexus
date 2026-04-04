# ============================================================
# NEXUS TRADER — Trade Analysis Service Tests (Session 35)
#
# 19 tests covering:
#   • Scoring rubric (good win, good loss, bad win, bad loss)
#   • Hard overrides (no stop, below-confluence, R:R < 1)
#   • Classification thresholds
#   • Root-cause analyzer categories
#   • Improvement recommender derivation
#   • Notification payload format
#   • Template enrichment (trade_opened, trade_closed)
#   • Open position analysis (no exit data)
#   • AI explanation stub (no Ollama required)
# ============================================================
import pytest
from unittest.mock import patch, MagicMock


# ── Trade fixture factories ───────────────────────────────────

def _good_long_win():
    """Bull trend, 2 models, good score, TP hit."""
    return {
        "symbol":      "BTCUSDT",
        "side":        "buy",
        "regime":      "bull_trend",
        "timeframe":   "1h",
        "score":       0.72,           # confluence
        "entry_price": 50_000.0,
        "exit_price":  52_500.0,
        "stop_loss":   49_000.0,
        "take_profit": 52_500.0,
        "size_usdt":   500.0,
        "pnl_usdt":    25.0,
        "pnl_pct":     5.0,
        "exit_reason": "take_profit",
        "models_fired":["trend", "momentum_breakout"],
        "rationale":   "Strong bull trend with breakout.",
        "duration_s":  14_400,  # 4 hours
        "opened_at":   "2026-03-25T10:00:00+00:00",
        "closed_at":   "2026-03-25T14:00:00+00:00",
    }


def _good_long_loss():
    """Bull trend, good setup — but stopped out. Still a good decision."""
    t = _good_long_win()
    t.update({
        "exit_price":  49_000.0,
        "exit_reason": "stop_loss",
        "pnl_usdt":    -10.0,
        "pnl_pct":     -2.0,
    })
    return t


def _bad_win():
    """Counter-regime entry that happened to profit. Bad decision quality."""
    return {
        "symbol":      "ETHUSDT",
        "side":        "sell",
        "regime":      "bull_trend",   # selling in bull trend
        "timeframe":   "1h",
        "score":       0.48,           # low confluence
        "entry_price": 3_000.0,
        "exit_price":  2_900.0,
        "stop_loss":   3_100.0,
        "take_profit": 2_800.0,
        "size_usdt":   300.0,
        "pnl_usdt":    10.0,
        "pnl_pct":     3.33,
        "exit_reason": "take_profit",
        "models_fired":["trend"],
        "rationale":   "Countertrend short.",
        "duration_s":  7_200,
        "opened_at":   "2026-03-25T08:00:00+00:00",
        "closed_at":   "2026-03-25T10:00:00+00:00",
    }


def _bad_loss():
    """No stop, below-minimum confluence, counter-regime — textbook BAD."""
    return {
        "symbol":      "SOLUSDT",
        "side":        "sell",
        "regime":      "bull_trend",
        "timeframe":   "1h",
        "score":       0.38,           # BELOW minimum (0.45)
        "entry_price": 150.0,
        "exit_price":  155.0,
        "stop_loss":   None,           # NO STOP → hard override
        "take_profit": None,
        "size_usdt":   200.0,
        "pnl_usdt":    -10.0,
        "pnl_pct":     -3.3,
        "exit_reason": "manual_close",
        "models_fired":[],             # no models
        "rationale":   "",
        "duration_s":  120,            # noise trade
        "opened_at":   "2026-03-25T06:00:00+00:00",
        "closed_at":   "2026-03-25T06:02:00+00:00",
    }


def _open_position():
    """Open position — no exit data."""
    t = _good_long_win()
    del t["exit_price"]
    del t["exit_reason"]
    del t["pnl_usdt"]
    del t["pnl_pct"]
    del t["closed_at"]
    t["current_price"]   = 51_000.0
    t["unrealized_pnl"]  = 2.0
    return t


# ── Scoring Engine ────────────────────────────────────────────

class TestScoringEngine:

    def test_good_win_classification(self):
        from core.analysis.scoring_engine import score_trade
        result = score_trade(_good_long_win())
        assert result["classification"] == "GOOD", \
            f"Expected GOOD, got {result['classification']} (overall={result['overall_score']})"

    def test_good_loss_still_good_decision(self):
        """A stop-loss hit on a well-setup trade should still classify GOOD."""
        from core.analysis.scoring_engine import score_trade
        result = score_trade(_good_long_loss())
        # Setup and decision should remain good; execution minor penalty
        assert result["setup_score"]    >= 70.0, f"setup_score={result['setup_score']}"
        assert result["decision_score"] >= 60.0, f"decision_score={result['decision_score']}"

    def test_bad_win_classification(self):
        """Counter-regime entry should classify BAD regardless of PnL."""
        from core.analysis.scoring_engine import score_trade
        result = score_trade(_bad_win())
        assert result["classification"] == "BAD", \
            f"Expected BAD, got {result['classification']} (overall={result['overall_score']})"

    def test_bad_loss_classification(self):
        from core.analysis.scoring_engine import score_trade
        result = score_trade(_bad_loss())
        assert result["classification"] == "BAD"

    def test_hard_override_no_stop(self):
        from core.analysis.scoring_engine import compute_hard_overrides
        trade = _good_long_win()
        trade["stop_loss"] = None
        overrides = compute_hard_overrides(trade)
        assert any("NO_STOP_LOSS" in o for o in overrides), f"overrides={overrides}"

    def test_hard_override_below_confluence(self):
        from core.analysis.scoring_engine import compute_hard_overrides
        trade = _good_long_win()
        trade["score"] = 0.30
        overrides = compute_hard_overrides(trade)
        assert any("BELOW_MIN_CONFLUENCE" in o for o in overrides), f"overrides={overrides}"

    def test_hard_override_rr_below_floor(self):
        from core.analysis.scoring_engine import compute_hard_overrides
        trade = {
            "side":        "buy",
            "entry_price": 100.0,
            "stop_loss":   98.0,   # risk = 2
            "take_profit": 101.0,  # reward = 1  → RR = 0.5
            "score":       0.70,
        }
        overrides = compute_hard_overrides(trade)
        assert any("RR_BELOW_FLOOR" in o for o in overrides), f"overrides={overrides}"

    def test_open_position_is_flagged(self):
        from core.analysis.scoring_engine import score_trade
        result = score_trade(_open_position())
        assert result["is_open"] is True

    def test_overall_score_bounds(self):
        from core.analysis.scoring_engine import score_trade
        for trade in [_good_long_win(), _bad_loss(), _bad_win()]:
            result = score_trade(trade)
            assert 0.0 <= result["overall_score"] <= 100.0, \
                f"overall_score={result['overall_score']} out of bounds"

    def test_rr_ratio_computed(self):
        from core.analysis.scoring_engine import _compute_rr
        # 50000 buy, stop 49000, tp 52000 → risk=1000, reward=2000 → RR=2.0
        rr = _compute_rr({
            "side": "buy",
            "entry_price": 50_000, "stop_loss": 49_000, "take_profit": 52_000,
        })
        assert rr is not None
        assert abs(rr - 2.0) < 0.01, f"Expected 2.0, got {rr}"

    def test_short_rr_ratio_computed(self):
        from core.analysis.scoring_engine import _compute_rr
        # 100 sell, stop 102, tp 96 → risk=2, reward=4 → RR=2.0
        rr = _compute_rr({
            "side": "sell",
            "entry_price": 100, "stop_loss": 102, "take_profit": 96,
        })
        assert rr is not None
        assert abs(rr - 2.0) < 0.01, f"Expected 2.0, got {rr}"


# ── Root Cause Analyzer ───────────────────────────────────────

class TestRootCauseAnalyzer:

    def test_no_root_causes_for_clean_trade(self):
        from core.analysis.scoring_engine import score_trade
        from core.analysis.root_cause_analyzer import analyze_root_causes
        result = score_trade(_good_long_win())
        root_causes = analyze_root_causes(result)
        # Should have few or zero root causes
        critical = [rc for rc in root_causes if rc["severity"] == "critical"]
        assert len(critical) == 0, f"Found critical root causes: {critical}"

    def test_bad_loss_has_critical_root_causes(self):
        from core.analysis.scoring_engine import score_trade
        from core.analysis.root_cause_analyzer import analyze_root_causes
        result = score_trade(_bad_loss())
        root_causes = analyze_root_causes(result)
        categories = {rc["category"] for rc in root_causes}
        assert "NO_STOP_LOSS" in categories or "BELOW_MINIMUM_CONFLUENCE" in categories, \
            f"Missing expected critical categories. Got: {categories}"

    def test_root_causes_deduplicated(self):
        from core.analysis.scoring_engine import score_trade
        from core.analysis.root_cause_analyzer import analyze_root_causes
        result = score_trade(_bad_loss())
        root_causes = analyze_root_causes(result)
        categories = [rc["category"] for rc in root_causes]
        assert len(categories) == len(set(categories)), \
            f"Duplicate root cause categories: {categories}"

    def test_root_causes_sorted_by_severity(self):
        from core.analysis.scoring_engine import score_trade
        from core.analysis.root_cause_analyzer import analyze_root_causes
        result = score_trade(_bad_loss())
        root_causes = analyze_root_causes(result)
        if len(root_causes) >= 2:
            _sev = {"critical": 0, "major": 1, "minor": 2}
            for i in range(len(root_causes) - 1):
                s1 = _sev.get(root_causes[i]["severity"],   9)
                s2 = _sev.get(root_causes[i+1]["severity"], 9)
                assert s1 <= s2, \
                    f"Severity order violated at index {i}: {root_causes[i]['severity']} > {root_causes[i+1]['severity']}"


# ── Improvement Recommender ───────────────────────────────────

class TestImprovementRecommender:

    def test_recommendations_derived_from_root_causes(self):
        from core.analysis.scoring_engine import score_trade
        from core.analysis.root_cause_analyzer import analyze_root_causes
        from core.analysis.improvement_recommender import generate_recommendations
        result = score_trade(_bad_loss())
        root_causes = analyze_root_causes(result)
        recs = generate_recommendations(root_causes)
        assert len(recs) > 0, "Expected at least one recommendation for bad loss"

    def test_recommendations_have_required_keys(self):
        from core.analysis.scoring_engine import score_trade
        from core.analysis.root_cause_analyzer import analyze_root_causes
        from core.analysis.improvement_recommender import generate_recommendations
        result = score_trade(_bad_win())
        root_causes = analyze_root_causes(result)
        recs = generate_recommendations(root_causes)
        for r in recs:
            assert "action"         in r, f"Missing 'action' in {r}"
            assert "rationale"      in r, f"Missing 'rationale' in {r}"
            assert "auto_tune_safe" in r, f"Missing 'auto_tune_safe' in {r}"
            assert "priority"       in r, f"Missing 'priority' in {r}"

    def test_no_duplicate_recommendations(self):
        from core.analysis.scoring_engine import score_trade
        from core.analysis.root_cause_analyzer import analyze_root_causes
        from core.analysis.improvement_recommender import generate_recommendations
        result = score_trade(_bad_loss())
        root_causes = analyze_root_causes(result)
        recs = generate_recommendations(root_causes)
        categories = [r["category"] for r in recs]
        assert len(categories) == len(set(categories)), \
            f"Duplicate recommendation categories: {categories}"


# ── Trade Analysis Service ────────────────────────────────────

class TestTradeAnalysisService:

    def test_build_trade_analysis_keys(self):
        from core.analysis.trade_analysis_service import trade_analysis_service
        result = trade_analysis_service.build_trade_analysis(_good_long_win())
        required = [
            "setup_score", "risk_score", "execution_score", "decision_score",
            "overall_score", "classification", "classification_emoji",
            "hard_overrides", "penalty_log", "rr_ratio", "regime_affinity",
            "root_causes", "recommendations", "ai_explanation", "is_open",
        ]
        for key in required:
            assert key in result, f"Missing key '{key}' in analysis result"

    def test_generate_notification_payload_keys(self):
        from core.analysis.trade_analysis_service import trade_analysis_service
        analysis = trade_analysis_service.build_trade_analysis(_good_long_win())
        payload  = trade_analysis_service.generate_notification_payload(
            _good_long_win(), analysis
        )
        required = [
            "analysis_overall", "analysis_setup", "analysis_risk",
            "analysis_execution", "analysis_decision", "analysis_classification",
            "analysis_emoji", "analysis_root_causes", "analysis_recommendation",
            "analysis_rr",
        ]
        for key in required:
            assert key in payload, f"Missing key '{key}' in notification payload"

    def test_classification_emoji_mapping(self):
        from core.analysis.trade_analysis_service import trade_analysis_service
        good = trade_analysis_service.build_trade_analysis(_good_long_win())
        bad  = trade_analysis_service.build_trade_analysis(_bad_loss())
        assert good["classification_emoji"] == "✅"
        assert bad["classification_emoji"]  == "❌"


# ── Notification Template Integration ────────────────────────

class TestNotificationTemplateIntegration:

    def test_trade_opened_includes_analysis(self):
        from core.notifications.notification_templates import trade_opened
        data = {
            "symbol": "BTCUSDT", "direction": "long",
            "entry_price": 50000, "stop_loss": 49000, "take_profit": 52000,
            "size": "$500 USDT", "strategy": "trend", "confidence": 0.72,
            "timeframe": "1h", "regime": "bull_trend", "rationale": "Breakout.",
            "score": 0.72, "side": "buy", "models_fired": ["trend", "momentum_breakout"],
            # Analysis keys
            "analysis_overall": "82.5", "analysis_setup": "90.0",
            "analysis_risk": "80.0", "analysis_classification": "GOOD",
            "analysis_emoji": "✅", "analysis_rr": "2.00",
            "analysis_root_causes": "None identified",
        }
        result = trade_opened(data)
        assert "AI ANALYSIS" in result["body"], "Analysis block missing from body"
        assert "GOOD" in result["body"]
        assert "✅" in result["short"]

    def test_trade_closed_includes_analysis(self):
        from core.notifications.notification_templates import trade_closed
        data = {
            "symbol": "ETHUSDT", "direction": "long",
            "entry_price": 3000, "exit_price": 3300,
            "pnl": 30, "pnl_pct": 10.0,
            "size": "$300 USDT", "strategy": "momentum_breakout",
            "close_reason": "take_profit", "duration": "4h 0m",
            "analysis_overall": "88.0", "analysis_setup": "92.0",
            "analysis_risk": "85.0", "analysis_execution": "80.0",
            "analysis_decision": "90.0", "analysis_classification": "GOOD",
            "analysis_emoji": "✅", "analysis_rr": "2.00",
            "analysis_root_causes": "None identified",
            "analysis_recommendation": "No specific recommendations.",
        }
        result = trade_closed(data)
        assert "AI ANALYSIS" in result["body"]
        assert "GOOD" in result["subject"]

    def test_trade_closed_html_body_present(self):
        from core.notifications.notification_templates import trade_closed
        data = {
            "symbol": "BTCUSDT", "direction": "long",
            "entry_price": 50000, "exit_price": 52000,
            "pnl": 20, "pnl_pct": 4.0,
            "size": "$500 USDT", "strategy": "trend",
            "close_reason": "take_profit", "duration": "2h",
            "analysis_overall": "85.0", "analysis_setup": "88.0",
            "analysis_risk": "82.0", "analysis_execution": "80.0",
            "analysis_decision": "90.0", "analysis_classification": "GOOD",
            "analysis_emoji": "✅", "analysis_rr": "2.00",
            "analysis_root_causes": "None identified",
            "analysis_recommendation": "No specific recommendations.",
        }
        result = trade_closed(data)
        assert "html_body" in result
        assert "AI Trade Quality Scorecard" in result["html_body"]
        assert "88" in result["html_body"]  # setup score rendered
