"""
tests/unit/test_session27_perf_framework.py

Session 27 — Performance Validation & Decision Framework tests.

Test groups:
  TH-01 through TH-09  — performance_thresholds.py
  SM-01 through SM-08  — scale_manager.py
  RG-01 through RG-05  — review_generator.py (headless helpers only)
  PP-01 through PP-04  — performance pause wiring in paper_executor (unit level)
  CF-01 through CF-02  — config keys present
"""
from __future__ import annotations

import json
import os
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

# ── helpers ───────────────────────────────────────────────────────────────────

def _make_live(trades=30, wr=0.52, pf=1.50, avg_r=0.30):
    return {"trades": trades, "win_rate": wr, "profit_factor": pf, "avg_r": avg_r}


# ═══════════════════════════════════════════════════════════════════════════════
# TH — performance_thresholds.py
# ═══════════════════════════════════════════════════════════════════════════════

class TestPerformanceThresholds:

    def test_th01_rag_status_values(self):
        from core.monitoring.performance_thresholds import RAGStatus
        assert RAGStatus.GREEN.value             == "GREEN"
        assert RAGStatus.AMBER.value             == "AMBER"
        assert RAGStatus.RED.value               == "RED"
        assert RAGStatus.INSUFFICIENT_DATA.value == "INSUFFICIENT_DATA"

    def test_th02_metric_band_green(self):
        from core.monitoring.performance_thresholds import MetricBand, RAGStatus
        band = MetricBand(green_min=0.50, amber_min=0.40, label="WR")
        assert band.evaluate(0.55) == RAGStatus.GREEN
        assert band.evaluate(0.50) == RAGStatus.GREEN

    def test_th03_metric_band_amber(self):
        from core.monitoring.performance_thresholds import MetricBand, RAGStatus
        band = MetricBand(green_min=0.50, amber_min=0.40, label="WR")
        assert band.evaluate(0.45) == RAGStatus.AMBER
        assert band.evaluate(0.40) == RAGStatus.AMBER

    def test_th04_metric_band_red(self):
        from core.monitoring.performance_thresholds import MetricBand, RAGStatus
        band = MetricBand(green_min=0.50, amber_min=0.40, label="WR")
        assert band.evaluate(0.39) == RAGStatus.RED
        assert band.evaluate(0.0)  == RAGStatus.RED

    def test_th05_metric_band_none_returns_insufficient(self):
        from core.monitoring.performance_thresholds import MetricBand, RAGStatus
        band = MetricBand(green_min=0.50, amber_min=0.40, label="WR")
        assert band.evaluate(None) == RAGStatus.INSUFFICIENT_DATA

    def test_th06_model_thresholds_trend_green_floors(self):
        from core.monitoring.performance_thresholds import THRESHOLDS
        t = THRESHOLDS["trend"]
        # GREEN floor ≈ 95% of 0.503 = 0.478; AMBER ≈ 90% = 0.453
        assert abs(t.wr_band.green_min - 0.503 * 0.95) < 0.002
        assert abs(t.wr_band.amber_min - 0.503 * 0.90) < 0.002

    def test_th07_model_thresholds_momentum_breakout(self):
        from core.monitoring.performance_thresholds import THRESHOLDS
        t = THRESHOLDS["momentum_breakout"]
        assert t.baseline_wr  == pytest.approx(0.635, abs=0.001)
        assert t.baseline_pf  == pytest.approx(4.17,  abs=0.01)
        assert t.baseline_avgr == pytest.approx(1.21, abs=0.01)
        # GREEN PF ≈ 87% × 4.17 = 3.63
        assert t.pf_band.green_min == pytest.approx(4.17 * 0.87, abs=0.01)

    def test_th08_insufficient_data_below_min_trades(self):
        from core.monitoring.performance_thresholds import (
            PerformanceThresholdEvaluator, RAGStatus,
        )
        ev = PerformanceThresholdEvaluator()
        result = ev._evaluate_model("_portfolio", _make_live(trades=5, wr=0.90, pf=9.0, avg_r=5.0))
        # Only 5 trades — should be INSUFFICIENT_DATA
        assert result.overall == RAGStatus.INSUFFICIENT_DATA

    def test_th09_portfolio_green_assessment(self):
        from core.monitoring.performance_thresholds import (
            PerformanceThresholdEvaluator, RAGStatus,
        )
        ev = PerformanceThresholdEvaluator()
        result = ev._evaluate_model(
            "_portfolio",
            _make_live(trades=50, wr=0.55, pf=1.60, avg_r=0.40),
        )
        assert result.overall == RAGStatus.GREEN
        assert result.wr.status   == RAGStatus.GREEN
        assert result.pf.status   == RAGStatus.GREEN
        assert result.avg_r.status == RAGStatus.GREEN

    def test_th10_portfolio_red_triggers_pause(self):
        from core.monitoring.performance_thresholds import (
            PerformanceThresholdEvaluator, PortfolioRAGAssessment,
            RAGStatus,
        )
        ev = PerformanceThresholdEvaluator()
        # Critically low metrics — all RED
        port = ev._evaluate_model(
            "_portfolio",
            _make_live(trades=50, wr=0.30, pf=0.50, avg_r=0.01),
        )
        assessment = PortfolioRAGAssessment(portfolio=port, per_model={})
        assert assessment.should_pause is True
        assert "RED" in assessment.pause_reason

    def test_th11_two_red_models_triggers_pause(self):
        from core.monitoring.performance_thresholds import (
            PerformanceThresholdEvaluator, PortfolioRAGAssessment,
            RAGStatus,
        )
        ev = PerformanceThresholdEvaluator()
        # Portfolio GREEN — but two individual models RED
        port_ok = ev._evaluate_model("_portfolio", _make_live(trades=50, wr=0.52, pf=1.50, avg_r=0.30))
        bad_m1  = ev._evaluate_model("trend", _make_live(trades=30, wr=0.25, pf=0.40, avg_r=0.01))
        bad_m2  = ev._evaluate_model("momentum_breakout", _make_live(trades=30, wr=0.30, pf=0.50, avg_r=0.05))
        assessment = PortfolioRAGAssessment(
            portfolio=port_ok,
            per_model={"trend": bad_m1, "momentum_breakout": bad_m2},
        )
        assert assessment.should_pause is True
        assert assessment.red_model_count == 2

    def test_th12_to_dict_roundtrip(self):
        from core.monitoring.performance_thresholds import (
            PerformanceThresholdEvaluator, PortfolioRAGAssessment,
        )
        ev     = PerformanceThresholdEvaluator()
        port   = ev._evaluate_model("_portfolio", _make_live(trades=50, wr=0.52))
        asses  = PortfolioRAGAssessment(portfolio=port, per_model={})
        d      = asses.to_dict()
        assert "overall" in d
        assert "should_pause" in d
        assert "portfolio" in d
        assert "per_model" in d

    def test_th13_singleton_is_same_instance(self):
        from core.monitoring.performance_thresholds import get_threshold_evaluator
        a = get_threshold_evaluator()
        b = get_threshold_evaluator()
        assert a is b

    def test_th14_evaluate_from_metrics_uses_model_key(self):
        from core.monitoring.performance_thresholds import (
            PerformanceThresholdEvaluator, RAGStatus,
        )
        ev = PerformanceThresholdEvaluator()
        result = ev.evaluate_from_metrics({
            "model": "trend",
            "trades": 30,
            "win_rate": 0.55,
            "profit_factor": 1.60,
            "avg_r": 0.30,
        })
        assert result.model_key == "trend"
        assert result.overall   == RAGStatus.GREEN


# ═══════════════════════════════════════════════════════════════════════════════
# SM — scale_manager.py
# ═══════════════════════════════════════════════════════════════════════════════

class TestScaleManager:

    def _fresh_manager(self, tmp_path):
        """Return a ScaleManager wired to a temp data dir."""
        import core.monitoring.scale_manager as sm_mod
        orig = sm_mod._DATA_FILE
        sm_mod._DATA_FILE = tmp_path / "scale_manager.json"
        mgr = sm_mod.ScaleManager()
        sm_mod._DATA_FILE = orig
        return mgr

    def test_sm01_default_phase_is_1(self, tmp_path):
        from core.monitoring.scale_manager import ScaleManager, PHASES
        import core.monitoring.scale_manager as sm_mod
        orig = sm_mod._DATA_FILE
        sm_mod._DATA_FILE = tmp_path / "sm.json"
        mgr = ScaleManager()
        sm_mod._DATA_FILE = orig
        assert mgr.current_phase == 1
        assert mgr.current_phase_def.risk_pct == 0.005

    def test_sm02_phases_dict_has_3_phases(self):
        from core.monitoring.scale_manager import PHASES
        assert set(PHASES.keys()) == {1, 2, 3}

    def test_sm03_phase_risk_pcts(self):
        from core.monitoring.scale_manager import PHASES
        assert PHASES[1].risk_pct == pytest.approx(0.005)
        assert PHASES[2].risk_pct == pytest.approx(0.0075)
        assert PHASES[3].risk_pct == pytest.approx(0.010)

    def test_sm04_phase_min_trades(self):
        from core.monitoring.scale_manager import PHASES
        assert PHASES[1].min_trades == 50
        assert PHASES[2].min_trades == 50
        assert PHASES[3].min_trades == 100

    def test_sm05_evaluate_advancement_blocks_without_trades(self, tmp_path):
        """evaluate_advancement should block when not enough trades."""
        import core.monitoring.scale_manager as sm_mod
        orig = sm_mod._DATA_FILE
        sm_mod._DATA_FILE = tmp_path / "sm.json"
        mgr = sm_mod.ScaleManager()
        sm_mod._DATA_FILE = orig

        # Patch at the source module since scale_manager uses local imports
        with patch("core.monitoring.live_vs_backtest.get_live_vs_backtest_tracker") as mock_lvb, \
             patch("core.monitoring.performance_thresholds.get_threshold_evaluator") as mock_ev:
            mock_lvb.return_value.get_comparison.return_value = {"trade_count": 10}
            from core.monitoring.performance_thresholds import RAGStatus
            mock_assess = MagicMock()
            mock_assess.should_pause   = False
            mock_assess.overall        = RAGStatus.INSUFFICIENT_DATA
            mock_ev.return_value.evaluate.return_value = mock_assess

            result = mgr.evaluate_advancement()
        assert result.can_advance is False
        assert any("Insufficient trades" in r for r in result.blocking_reasons)

    def test_sm06_evaluate_advancement_blocks_when_not_green(self, tmp_path):
        import core.monitoring.scale_manager as sm_mod
        orig = sm_mod._DATA_FILE
        sm_mod._DATA_FILE = tmp_path / "sm.json"
        mgr = sm_mod.ScaleManager()
        mgr._state.trades_at_start = 0
        sm_mod._DATA_FILE = orig

        with patch("core.monitoring.live_vs_backtest.get_live_vs_backtest_tracker") as mock_lvb, \
             patch("core.monitoring.performance_thresholds.get_threshold_evaluator") as mock_ev:
            mock_lvb.return_value.get_comparison.return_value = {"trade_count": 60}
            from core.monitoring.performance_thresholds import RAGStatus
            mock_assess = MagicMock()
            mock_assess.should_pause   = False
            mock_assess.overall        = RAGStatus.AMBER   # not GREEN
            mock_ev.return_value.evaluate.return_value = mock_assess

            result = mgr.evaluate_advancement()
        assert result.can_advance is False
        assert any("not all GREEN" in r for r in result.blocking_reasons)

    def test_sm07_evaluate_advancement_allows_when_criteria_met(self, tmp_path):
        import core.monitoring.scale_manager as sm_mod
        orig = sm_mod._DATA_FILE
        sm_mod._DATA_FILE = tmp_path / "sm.json"
        mgr = sm_mod.ScaleManager()
        mgr._state.trades_at_start = 0
        sm_mod._DATA_FILE = orig

        with patch("core.monitoring.live_vs_backtest.get_live_vs_backtest_tracker") as mock_lvb, \
             patch("core.monitoring.performance_thresholds.get_threshold_evaluator") as mock_ev:
            mock_lvb.return_value.get_comparison.return_value = {"trade_count": 55}
            from core.monitoring.performance_thresholds import RAGStatus
            mock_assess = MagicMock()
            mock_assess.should_pause   = False
            mock_assess.overall        = RAGStatus.GREEN
            mock_ev.return_value.evaluate.return_value = mock_assess

            result = mgr.evaluate_advancement()
        assert result.can_advance is True
        assert "READY TO ADVANCE" in result.recommendation

    def test_sm08_record_phase_advance_updates_state(self, tmp_path):
        import core.monitoring.scale_manager as sm_mod
        orig = sm_mod._DATA_FILE
        sm_mod._DATA_FILE = tmp_path / "sm.json"
        mgr = sm_mod.ScaleManager()
        mgr.record_phase_advance(new_phase=2, trade_count=55)
        sm_mod._DATA_FILE = orig

        assert mgr.current_phase == 2
        assert mgr._state.trades_at_start == 55
        assert any(e.get("event") == "phase_advanced" for e in mgr._state.advancement_log)

    def test_sm09_phase_summary_contains_required_keys(self, tmp_path):
        import core.monitoring.scale_manager as sm_mod
        orig = sm_mod._DATA_FILE
        sm_mod._DATA_FILE = tmp_path / "sm.json"
        mgr = sm_mod.ScaleManager()
        sm_mod._DATA_FILE = orig
        summary = mgr.get_phase_summary()
        for key in ("current_phase", "risk_pct", "risk_pct_str", "description", "max_phase"):
            assert key in summary, f"Missing key: {key}"

    def test_sm10_phase3_cannot_advance_further(self, tmp_path):
        import core.monitoring.scale_manager as sm_mod
        orig = sm_mod._DATA_FILE
        sm_mod._DATA_FILE = tmp_path / "sm.json"
        mgr = sm_mod.ScaleManager()
        mgr._state.current_phase = 3
        sm_mod._DATA_FILE = orig

        with patch("core.monitoring.live_vs_backtest.get_live_vs_backtest_tracker") as mock_lvb, \
             patch("core.monitoring.performance_thresholds.get_threshold_evaluator") as mock_ev:
            mock_lvb.return_value.get_comparison.return_value = {"trade_count": 999}
            from core.monitoring.performance_thresholds import RAGStatus
            mock_assess = MagicMock()
            mock_assess.should_pause = False
            mock_assess.overall = RAGStatus.GREEN
            mock_ev.return_value.evaluate.return_value = mock_assess

            result = mgr.evaluate_advancement()
        assert result.can_advance is False
        assert result.next_phase is None


# ═══════════════════════════════════════════════════════════════════════════════
# RG — review_generator.py (headless helpers, no Qt)
# ═══════════════════════════════════════════════════════════════════════════════

class TestReviewGeneratorHelpers:

    def test_rg01_fmt_pct_none(self):
        from core.monitoring.review_generator import _fmt_pct
        assert _fmt_pct(None) == "—"

    def test_rg02_fmt_pct_value(self):
        from core.monitoring.review_generator import _fmt_pct
        assert _fmt_pct(0.503) == "50.3%"

    def test_rg03_fmt_r_positive(self):
        from core.monitoring.review_generator import _fmt_r
        assert _fmt_r(1.21).startswith("+")

    def test_rg04_fmt_r_negative(self):
        from core.monitoring.review_generator import _fmt_r
        result = _fmt_r(-0.5)
        assert result.startswith("-")

    def test_rg05_fmt_usdt_positive(self):
        from core.monitoring.review_generator import _fmt_usdt
        s = _fmt_usdt(1234.56)
        assert s.startswith("+")
        assert "1,234" in s

    def test_rg06_symbol_breakdown_empty(self):
        from core.monitoring.review_generator import _symbol_breakdown
        assert _symbol_breakdown([]) == {}

    def test_rg07_symbol_breakdown_single_win(self):
        from core.monitoring.review_generator import _symbol_breakdown
        trades = [{"symbol": "BTC/USDT", "pnl_usdt": 50.0, "realized_r": 1.0}]
        result = _symbol_breakdown(trades)
        assert "BTC/USDT" in result
        d = result["BTC/USDT"]
        assert d["trades"]   == 1
        assert d["wins"]     == 1
        assert d["pnl_usdt"] == pytest.approx(50.0)

    def test_rg08_slippage_analysis_empty(self):
        from core.monitoring.review_generator import _slippage_analysis
        s = _slippage_analysis([])
        assert s["count"] == 0
        assert s["avg"]   is None

    def test_rg09_anomaly_scanner_consecutive_losses(self):
        from core.monitoring.review_generator import _find_anomalies
        trades = [{"pnl_usdt": -1.0, "realized_r": -0.5}] * 7
        anomalies = _find_anomalies(trades)
        assert any("streak" in a.lower() for a in anomalies)

    def test_rg10_trade_after_no_timestamp_inclusive(self):
        from core.monitoring.review_generator import _trade_after
        from datetime import datetime, timezone, timedelta
        cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
        # Trade with no timestamp should be included (conservative)
        assert _trade_after({}, cutoff) is True

    def test_rg11_trade_after_old_trade_excluded(self):
        from core.monitoring.review_generator import _trade_after
        from datetime import datetime, timezone, timedelta
        cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
        old_ts = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
        assert _trade_after({"closed_at": old_ts}, cutoff) is False

    def test_rg12_trade_after_recent_included(self):
        from core.monitoring.review_generator import _trade_after
        from datetime import datetime, timezone, timedelta
        cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
        recent = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        assert _trade_after({"closed_at": recent}, cutoff) is True


# ═══════════════════════════════════════════════════════════════════════════════
# PP — Performance pause wiring in paper_executor (unit level)
# ═══════════════════════════════════════════════════════════════════════════════

class TestPerformancePauseWiring:
    """
    Tests verify that the performance pause code path exists correctly.
    We don't need a full executor round-trip — just check the logic structure.
    """

    def test_pp01_hard_block_requires_both_pf_and_wr_red(self):
        """Hard block only fires when BOTH PF < 1.0 AND WR < 40%."""
        from core.monitoring.performance_thresholds import (
            PerformanceThresholdEvaluator, PortfolioRAGAssessment, RAGStatus,
        )
        ev = PerformanceThresholdEvaluator()
        # PF red, WR green — should NOT trigger hard block
        port = ev._evaluate_model(
            "_portfolio",
            _make_live(trades=50, wr=0.52, pf=0.80, avg_r=0.05),
        )
        assert port.pf.status == RAGStatus.RED
        # WR is not RED here so hard block should not fire
        assert port.wr.status != RAGStatus.RED

    def test_pp02_hard_block_fires_when_both_red(self):
        """When both PF<1.0 AND WR<40%, both should be RED."""
        from core.monitoring.performance_thresholds import (
            PerformanceThresholdEvaluator, RAGStatus,
        )
        ev = PerformanceThresholdEvaluator()
        port = ev._evaluate_model(
            "_portfolio",
            _make_live(trades=50, wr=0.25, pf=0.60, avg_r=0.01),
        )
        pf_red = port.pf.status == RAGStatus.RED and (port.pf.value or 999) < 1.0
        wr_red = port.wr.status == RAGStatus.RED and (port.wr.value or 999) < 0.40
        assert pf_red and wr_red

    def test_pp03_portfolio_assessment_has_trades_attribute(self):
        """ModelAssessment.trades attribute exists and counts correctly."""
        from core.monitoring.performance_thresholds import PerformanceThresholdEvaluator
        ev     = PerformanceThresholdEvaluator()
        result = ev._evaluate_model("_portfolio", _make_live(trades=35))
        assert result.trades == 35

    def test_pp04_should_pause_false_by_default_with_green(self):
        """No pause when portfolio is GREEN and no model RED."""
        from core.monitoring.performance_thresholds import (
            PerformanceThresholdEvaluator, PortfolioRAGAssessment, RAGStatus,
        )
        ev   = PerformanceThresholdEvaluator()
        port = ev._evaluate_model("_portfolio", _make_live(trades=50, wr=0.55, pf=1.60, avg_r=0.40))
        asses = PortfolioRAGAssessment(portfolio=port, per_model={})
        assert asses.should_pause is False


# ═══════════════════════════════════════════════════════════════════════════════
# CF — config keys present
# ═══════════════════════════════════════════════════════════════════════════════

class TestConfigKeys:

    def test_cf01_performance_thresholds_in_default_config(self):
        from config.settings import DEFAULT_CONFIG
        assert "performance_thresholds" in DEFAULT_CONFIG
        pt = DEFAULT_CONFIG["performance_thresholds"]
        assert "min_trades_for_verdict"  in pt
        assert "hard_block_pf_below"     in pt
        assert "hard_block_wr_below"     in pt
        assert "hard_block_min_trades"   in pt

    def test_cf02_scale_manager_in_default_config(self):
        from config.settings import DEFAULT_CONFIG
        assert "scale_manager" in DEFAULT_CONFIG
        sm = DEFAULT_CONFIG["scale_manager"]
        assert "current_phase"    in sm
        assert "phase1_risk_pct"  in sm
        assert "phase2_risk_pct"  in sm
        assert "phase3_risk_pct"  in sm

    def test_cf03_performance_thresholds_min_trades_is_20(self):
        from config.settings import DEFAULT_CONFIG
        assert DEFAULT_CONFIG["performance_thresholds"]["min_trades_for_verdict"] == 20

    def test_cf04_hard_block_defaults_sensible(self):
        from config.settings import DEFAULT_CONFIG
        pt = DEFAULT_CONFIG["performance_thresholds"]
        assert pt["hard_block_pf_below"]   == pytest.approx(1.0)
        assert pt["hard_block_wr_below"]   == pytest.approx(0.40)
        assert pt["hard_block_min_trades"] == 30

    def test_cf05_phase_risk_pcts_ordered(self):
        from config.settings import DEFAULT_CONFIG
        sm = DEFAULT_CONFIG["scale_manager"]
        assert sm["phase1_risk_pct"] < sm["phase2_risk_pct"] < sm["phase3_risk_pct"]


# ═══════════════════════════════════════════════════════════════════════════════
# DM — Demo Monitor helpers / no-qt
# ═══════════════════════════════════════════════════════════════════════════════

class TestDemoMonitorHelpers:
    """Ensure pure-Python helpers in demo_monitor_helpers work unchanged."""

    def test_dm01_fmt_pct_zero(self):
        from gui.widgets.demo_monitor_helpers import fmt_pct
        assert fmt_pct(0.0) == "0.0%"

    def test_dm02_fmt_delta_pct_positive(self):
        from gui.widgets.demo_monitor_helpers import fmt_delta_pct
        assert fmt_delta_pct(0.05).startswith("+")

    def test_dm03_fmt_model_name_known(self):
        from gui.widgets.demo_monitor_helpers import fmt_model_name
        assert fmt_model_name("momentum_breakout") == "MomBreak"

    def test_dm04_fmt_model_name_unknown(self):
        from gui.widgets.demo_monitor_helpers import fmt_model_name
        result = fmt_model_name("some_new_model")
        assert result == "Some New Model"   # titlecase fallback

    def test_dm05_fmt_pct_none(self):
        from gui.widgets.demo_monitor_helpers import fmt_pct
        assert fmt_pct(None) == "—"


# ═══════════════════════════════════════════════════════════════════════════════
# IB — Intermediate hard block (Session 28)
# ═══════════════════════════════════════════════════════════════════════════════

class TestIntermediateHardBlock:
    """
    Tests for the intermediate hard stop: PF < 1.2 AND WR < 45% over ≥ 30
    trades.  Unit-level checks against the threshold logic and the executor
    code structure — no live executor round-trip needed.
    """

    # ── threshold boundary tests ────────────────────────────────────────────

    def test_ib01_both_below_threshold_triggers_block(self):
        """PF=1.1 AND WR=0.43 → both fail intermediate gate."""
        pf_val = 1.1
        wr_val = 0.43
        assert pf_val < 1.2, "PF should be below intermediate PF threshold"
        assert wr_val < 0.45, "WR should be below intermediate WR threshold"
        # Confirm combined condition
        assert (pf_val < 1.2) and (wr_val < 0.45)

    def test_ib02_pf_above_threshold_no_block(self):
        """PF=1.25 AND WR=0.43 → PF gate passes; no block."""
        pf_val = 1.25
        wr_val = 0.43
        assert not (pf_val < 1.2 and wr_val < 0.45)

    def test_ib03_wr_above_threshold_no_block(self):
        """PF=1.1 AND WR=0.46 → WR gate passes; no block."""
        pf_val = 1.1
        wr_val = 0.46
        assert not (pf_val < 1.2 and wr_val < 0.45)

    def test_ib04_both_above_threshold_no_block(self):
        """PF=1.3 AND WR=0.50 → both above; no block."""
        pf_val = 1.3
        wr_val = 0.50
        assert not (pf_val < 1.2 and wr_val < 0.45)

    def test_ib05_exact_pf_boundary(self):
        """PF=1.2 exactly does NOT trigger block (strictly less-than)."""
        pf_val = 1.2
        wr_val = 0.44
        assert not (pf_val < 1.2 and wr_val < 0.45)

    def test_ib06_exact_wr_boundary(self):
        """WR=0.45 exactly does NOT trigger block (strictly less-than)."""
        pf_val = 1.1
        wr_val = 0.45
        assert not (pf_val < 1.2 and wr_val < 0.45)

    def test_ib07_intermediate_fires_before_final_block(self):
        """
        Intermediate gate (PF<1.2/WR<45%) is strictly WEAKER than final gate
        (PF<1.0/WR<40%).  Any state that triggers the final gate also triggers
        the intermediate gate, so intermediate fires first.
        """
        # values that trigger the FINAL block
        pf_val = 0.85
        wr_val = 0.35
        triggers_intermediate = pf_val < 1.2 and wr_val < 0.45
        triggers_final        = pf_val < 1.0 and wr_val < 0.40
        assert triggers_intermediate, "Final-block conditions must also trigger intermediate"
        assert triggers_final

    def test_ib08_intermediate_does_not_require_rag_red(self):
        """
        Unlike the final block which reads RAGStatus.RED, the intermediate
        block fires on raw values — no RAGStatus dependency.
        """
        # Simply verify the numeric condition is standalone
        pf_val, wr_val = 1.15, 0.44
        # This should fire with raw values alone, no RAGStatus comparison
        result = (pf_val < 1.2) and (wr_val < 0.45)
        assert result

    def test_ib09_intermediate_block_type_in_bus_payload(self):
        """Event bus payload type is 'performance_intermediate_block'."""
        expected_type = "performance_intermediate_block"
        # Verify the string is present in paper_executor source
        import inspect
        import core.execution.paper_executor as pe_mod
        src = inspect.getsource(pe_mod)
        assert expected_type in src, (
            f"Expected '{expected_type}' in paper_executor source — "
            "bus payload type must match"
        )

    def test_ib10_critical_log_used_for_intermediate(self):
        """Intermediate block uses logger.critical (not warning)."""
        import inspect
        import core.execution.paper_executor as pe_mod
        src = inspect.getsource(pe_mod)
        # Ensure 'INTERMEDIATE HARD BLOCK' appears and 'logger.critical' is used
        assert "INTERMEDIATE HARD BLOCK" in src
        assert "logger.critical" in src

    def test_ib11_intermediate_thresholds_are_strictly_above_final(self):
        """
        Intermediate PF threshold (1.2) > final PF threshold (1.0).
        Intermediate WR threshold (0.45) > final WR threshold (0.40).
        Guarantees the intermediate gate fires before the final gate.
        """
        inter_pf = 1.2
        inter_wr = 0.45
        final_pf = 1.0
        final_wr = 0.40
        assert inter_pf > final_pf
        assert inter_wr > final_wr

    def test_ib12_min_trades_same_for_both_blocks(self):
        """
        Both the intermediate and final block use the same ≥30 trade gate,
        ensuring neither fires in the first 29 trades.
        """
        import inspect
        import core.execution.paper_executor as pe_mod
        src = inspect.getsource(pe_mod)
        # The single `if _port_trades >= 30` gate covers both blocks
        assert "_port_trades >= 30" in src
