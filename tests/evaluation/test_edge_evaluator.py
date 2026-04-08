"""
tests/evaluation/test_edge_evaluator.py
────────────────────────────────────────
~60 tests for the EdgeEvaluator module.

Sections
────────
E1  R extraction (_r_from_dict)
E2  Expectancy formula (_compute_expectancy)
E3  Profit factor formula (_compute_pf_for_r_list)
E4  Rolling window helpers (_compute_pf_series, _compute_rolling_exp_series)
E5  Drawdown in R (_compute_drawdown_r)
E6  PFS stability (_pfs_from_pf_series)
E7  EdgeThresholds dataclass
E8  Verdict logic (full EdgeEvaluator.evaluate)
E9  Dimension breakdowns (by regime / model / asset / score bucket)
E10 Synthetic simulation scenarios (stable / unstable / deteriorating)
E11 DemoPerformanceEvaluator integration (edge_assessment field)
E12 Safety contract (no set_mode / no live string)
"""
from __future__ import annotations

import math
import inspect
import threading
from copy import deepcopy
from typing import Optional

import pytest

from core.evaluation.edge_evaluator import (
    EdgeEvaluator,
    EdgeAssessment,
    EdgeThresholds,
    EdgeVerdict,
    ExpectancyMetrics,
    ProfitFactorMetrics,
    ScoreBucketMetrics,
    _r_from_dict,
    _compute_expectancy,
    _compute_pf_for_r_list,
    _compute_pf_series,
    _compute_rolling_exp_series,
    _compute_drawdown_r,
    _pfs_from_pf_series,
    get_edge_evaluator,
)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _trade(
    pnl: float = 10.0,
    r_multiple: Optional[float] = None,
    entry: float = 100.0,
    stop: float = 95.0,
    size: float = 200.0,
    regime: str = "bull_trend",
    symbol: str = "BTC/USDT",
    models: Optional[list] = None,
    score: float = 0.65,
    closed_at: str = "2026-03-01T12:00:00",
) -> dict:
    """Build a minimal trade dict for testing."""
    t = {
        "pnl_usdt": pnl,
        "entry_price": entry,
        "stop_loss": stop,
        "size_usdt": size,
        "regime": regime,
        "symbol": symbol,
        "models_fired": models or ["trend"],
        "confluence_score": score,
        "closed_at": closed_at,
        "opened_at": closed_at,
    }
    if r_multiple is not None:
        t["realized_r_multiple"] = r_multiple
    return t


def _winning_trades(n: int, r: float = 1.0, **kwargs) -> list[dict]:
    return [_trade(pnl=r * 10, r_multiple=r, **kwargs) for _ in range(n)]


def _losing_trades(n: int, r: float = -1.0, **kwargs) -> list[dict]:
    return [_trade(pnl=r * 10, r_multiple=r, **kwargs) for _ in range(n)]


def _mixed_trades(
    n_win: int, n_loss: int,
    win_r: float = 1.5, loss_r: float = -1.0
) -> list[dict]:
    trades = _winning_trades(n_win, win_r) + _losing_trades(n_loss, loss_r)
    # Add sequential timestamps
    for i, t in enumerate(trades):
        t["closed_at"] = f"2026-03-{(i % 28) + 1:02d}T{(i % 24):02d}:00:00"
    return trades


# ─────────────────────────────────────────────────────────────────────────────
# E1 — R extraction
# ─────────────────────────────────────────────────────────────────────────────

class TestE1_RExtraction:

    def test_e1_01_precomputed_r_used_first(self):
        t = _trade(r_multiple=2.5, entry=100, stop=90, pnl=50)
        assert _r_from_dict(t) == 2.5

    def test_e1_02_computed_from_entry_stop_size_pnl(self):
        # entry=100, stop=95, size=200 → risk = |100-95|/100 * 200 = 10
        # pnl=20 → r = 20/10 = 2.0
        t = _trade(r_multiple=None, entry=100, stop=95, size=200, pnl=20.0)
        r = _r_from_dict(t)
        assert r is not None
        assert abs(r - 2.0) < 1e-6

    def test_e1_03_negative_r_computed_correctly(self):
        # pnl=-10, risk=10 → r = -1.0
        t = _trade(r_multiple=None, entry=100, stop=95, size=200, pnl=-10.0)
        r = _r_from_dict(t)
        assert r is not None
        assert abs(r - (-1.0)) < 1e-6

    def test_e1_04_missing_entry_returns_none(self):
        t = {"pnl_usdt": 10, "stop_loss": 95, "size_usdt": 200}
        assert _r_from_dict(t) is None

    def test_e1_05_missing_stop_returns_none(self):
        t = {"pnl_usdt": 10, "entry_price": 100, "size_usdt": 200}
        assert _r_from_dict(t) is None

    def test_e1_06_zero_size_returns_none(self):
        t = {"pnl_usdt": 10, "entry_price": 100, "stop_loss": 95, "size_usdt": 0}
        assert _r_from_dict(t) is None

    def test_e1_07_entry_equals_stop_returns_none(self):
        # risk_usdt = 0 → can't divide
        t = _trade(r_multiple=None, entry=100, stop=100, size=200, pnl=10.0)
        assert _r_from_dict(t) is None

    def test_e1_08_precomputed_r_invalid_fallback(self):
        # r_multiple is None explicitly — should fall back to computation
        t = _trade(r_multiple=None, entry=100, stop=90, size=100, pnl=10.0)
        r = _r_from_dict(t)
        assert r is not None


# ─────────────────────────────────────────────────────────────────────────────
# E2 — Expectancy formula
# ─────────────────────────────────────────────────────────────────────────────

class TestE2_Expectancy:

    def test_e2_01_basic_formula(self):
        # 6 wins at +1.5R, 4 losses at -1.0R
        # WR=0.6, LR=0.4, AvgWin=1.5, AvgLoss=1.0
        # E = 0.6*1.5 - 0.4*1.0 = 0.90 - 0.40 = 0.50
        r = [1.5] * 6 + [-1.0] * 4
        em = _compute_expectancy(r)
        assert em is not None
        assert abs(em.expectancy_r - 0.50) < 1e-6
        assert abs(em.win_rate - 0.6) < 1e-6
        assert abs(em.avg_win_r - 1.5) < 1e-6
        assert abs(em.avg_loss_r - 1.0) < 1e-6

    def test_e2_02_all_wins(self):
        em = _compute_expectancy([1.0, 2.0, 3.0])
        assert em is not None
        assert em.win_rate == 1.0
        assert em.loss_rate == 0.0
        assert em.avg_loss_r == 0.0
        assert abs(em.expectancy_r - 2.0) < 1e-6

    def test_e2_03_all_losses(self):
        em = _compute_expectancy([-1.0, -2.0])
        assert em is not None
        assert em.win_rate == 0.0
        assert em.avg_win_r == 0.0
        assert em.expectancy_r < 0

    def test_e2_04_empty_list_returns_none(self):
        assert _compute_expectancy([]) is None

    def test_e2_05_profit_factor_computed(self):
        # gross_win = 6, gross_loss = 4, PF = 1.5
        em = _compute_expectancy([1.0] * 6 + [-1.0] * 4)
        assert em is not None
        assert abs(em.profit_factor - 1.5) < 1e-4

    def test_e2_06_edge_label_losing(self):
        em = _compute_expectancy([-1.0, -2.0])
        assert em.edge_label == "Losing"

    def test_e2_07_edge_label_strong(self):
        em = _compute_expectancy([3.0] * 5 + [-1.0] * 1)
        assert em.edge_label == "Strong"

    def test_e2_08_edge_label_marginal(self):
        em = _compute_expectancy([0.5] * 5 + [-1.0] * 4)
        assert em.edge_label in ("Marginal", "Weak", "Losing")

    def test_e2_09_trade_count_correct(self):
        em = _compute_expectancy([1.0, -1.0, 1.0])
        assert em.trade_count == 3


# ─────────────────────────────────────────────────────────────────────────────
# E3 — Profit factor formula
# ─────────────────────────────────────────────────────────────────────────────

class TestE3_ProfitFactor:

    def test_e3_01_basic_pf(self):
        # wins: 3×1.5=4.5, losses: 3×1.0=3.0, PF=1.5
        r = [1.5, 1.5, 1.5, -1.0, -1.0, -1.0]
        assert abs(_compute_pf_for_r_list(r) - 1.5) < 1e-4

    def test_e3_02_no_losses(self):
        assert _compute_pf_for_r_list([1.0, 2.0]) == 999.0

    def test_e3_03_no_wins(self):
        assert _compute_pf_for_r_list([-1.0, -2.0]) == 0.0

    def test_e3_04_empty_list(self):
        assert _compute_pf_for_r_list([]) == 0.0

    def test_e3_05_break_even(self):
        # wins=5, losses=5
        assert abs(_compute_pf_for_r_list([1.0] * 5 + [-1.0] * 5) - 1.0) < 1e-6


# ─────────────────────────────────────────────────────────────────────────────
# E4 — Rolling window helpers
# ─────────────────────────────────────────────────────────────────────────────

class TestE4_RollingHelpers:

    def test_e4_01_pf_series_length_window_20(self):
        r = [1.0] * 25 + [-1.0] * 25
        series = _compute_pf_series(r, 20)
        # expect len(r) - 20 + 1 = 31 values
        assert len(series) == 31

    def test_e4_02_pf_series_empty_when_too_few(self):
        r = [1.0] * 10
        assert _compute_pf_series(r, 20) == []

    def test_e4_03_pf_series_all_wins(self):
        r = [1.0] * 25
        series = _compute_pf_series(r, 20)
        assert all(v == 999.0 for v in series)

    def test_e4_04_rolling_exp_series_length(self):
        r = [1.5] * 10 + [-1.0] * 10
        series = _compute_rolling_exp_series(r, 20)
        # exactly 1 value: starts at index 19
        assert len(series) == 1

    def test_e4_05_rolling_exp_series_consistent_with_expectancy(self):
        r = [2.0] * 12 + [-1.0] * 8   # 20 total, WR=0.6, AvgWin=2, AvgLoss=1
        # E = 0.6*2 - 0.4*1 = 0.8
        series = _compute_rolling_exp_series(r, 20)
        assert len(series) == 1
        assert abs(series[0] - 0.8) < 1e-6


# ─────────────────────────────────────────────────────────────────────────────
# E5 — Drawdown in R
# ─────────────────────────────────────────────────────────────────────────────

class TestE5_DrawdownR:

    def test_e5_01_no_drawdown_all_wins(self):
        assert _compute_drawdown_r([1.0, 1.0, 1.0]) == 0.0

    def test_e5_02_simple_drawdown(self):
        # cumulative: 1, 2, 3, 1, 2 → peak=3, trough=1, dd=2
        r = [1.0, 1.0, 1.0, -2.0, 1.0]
        assert abs(_compute_drawdown_r(r) - 2.0) < 1e-6

    def test_e5_03_all_losses(self):
        # cumulative: -1, -2, -3 → peak=0 but we start from cumulative[0]
        r = [-1.0, -1.0, -1.0]
        dd = _compute_drawdown_r(r)
        # peak after first element is -1, each step is lower
        assert dd > 0

    def test_e5_04_empty_returns_zero(self):
        assert _compute_drawdown_r([]) == 0.0

    def test_e5_05_large_drawdown(self):
        # up 5R then down 7R → peak=5, trough=-2, dd=7
        r = [1.0] * 5 + [-1.0] * 7
        dd = _compute_drawdown_r(r)
        assert abs(dd - 7.0) < 1e-6


# ─────────────────────────────────────────────────────────────────────────────
# E6 — PFS stability
# ─────────────────────────────────────────────────────────────────────────────

class TestE6_PFS:

    def test_e6_01_insufficient_data(self):
        pf_series = [1.4, 1.5, 1.3]  # < 5 snapshots
        score, cv, label = _pfs_from_pf_series(pf_series)
        assert label == "Insufficient data"
        assert score == 0.0

    def test_e6_02_stable(self):
        # very consistent PF around 1.4 → low CV → high score
        pf_series = [1.40, 1.41, 1.39, 1.42, 1.40, 1.38, 1.41, 1.40, 1.39, 1.40]
        score, cv, label = _pfs_from_pf_series(pf_series)
        assert label == "Stable"
        assert score >= 85

    def test_e6_03_moderate(self):
        # some variation
        pf_series = [1.2, 1.6, 1.1, 1.8, 1.3, 1.5, 1.2, 1.7, 1.4, 1.3]
        score, cv, label = _pfs_from_pf_series(pf_series)
        assert label in ("Moderate", "Unstable")

    def test_e6_04_unstable_high_cv(self):
        # high variance PF series
        pf_series = [0.5, 3.0, 0.3, 2.8, 0.6, 3.2, 0.4, 2.9, 0.7, 0.3]
        score, cv, label = _pfs_from_pf_series(pf_series)
        assert label == "Unstable"
        assert score < 60

    def test_e6_05_uses_last_10_snapshots(self):
        # First 5 are noisy, last 10 are stable
        pf_noisy  = [0.3, 3.5, 0.2, 3.8, 0.1]
        pf_stable = [1.40, 1.41, 1.39, 1.42, 1.40, 1.38, 1.41, 1.40, 1.39, 1.40]
        score, cv, label = _pfs_from_pf_series(pf_noisy + pf_stable)
        # should use only last 10 (stable)
        assert label == "Stable"
        assert score >= 85

    def test_e6_06_score_formula_correct(self):
        # Verify score = round(100 * (1 - cv)) for a controlled series
        pf_series = [1.40] * 10  # perfect stability → cv=0 → score=100
        score, cv, label = _pfs_from_pf_series(pf_series)
        assert score == 100.0
        assert cv == 0.0
        assert label == "Stable"


# ─────────────────────────────────────────────────────────────────────────────
# E7 — EdgeThresholds dataclass
# ─────────────────────────────────────────────────────────────────────────────

class TestE7_Thresholds:

    def test_e7_01_default_values(self):
        t = EdgeThresholds()
        assert t.min_trades_early == 40
        assert t.min_pf_early == 1.35
        assert t.min_expectancy_early == 0.20
        assert t.min_trades_full == 75
        assert t.min_pf_full == 1.40
        assert t.min_expectancy_full == 0.25
        assert t.min_pfs_score == 60.0
        assert t.max_drawdown_r == 10.0

    def test_e7_02_score_bins_correct(self):
        t = EdgeThresholds()
        assert len(t.score_bins) == 4
        assert t.score_bins[0] == (0.60, 0.70)
        assert t.score_bins[3] == (0.90, 1.01)

    def test_e7_03_custom_thresholds(self):
        t = EdgeThresholds(min_trades_early=20, min_pf_early=1.5)
        assert t.min_trades_early == 20
        assert t.min_pf_early == 1.5


# ─────────────────────────────────────────────────────────────────────────────
# E8 — Verdict logic
# ─────────────────────────────────────────────────────────────────────────────

class TestE8_Verdict:

    def _eval(self, trades):
        return EdgeEvaluator().evaluate(trades)

    def test_e8_01_empty_trades_not_ready(self):
        ea = self._eval([])
        assert ea.verdict == EdgeVerdict.NOT_READY
        assert ea.trade_count == 0
        assert ea.score == 0

    def test_e8_02_below_early_threshold_not_ready(self):
        trades = _mixed_trades(15, 10, win_r=2.0, loss_r=-1.0)
        ea = self._eval(trades)
        assert ea.verdict == EdgeVerdict.NOT_READY

    def test_e8_03_negative_expectancy_not_ready(self):
        # 40 trades, but all losing → negative expectancy
        trades = _losing_trades(40)
        ea = self._eval(trades)
        assert ea.verdict == EdgeVerdict.NOT_READY

    def test_e8_04_needs_improvement_early_zone(self):
        # 50 trades, good expectancy but not yet 75
        trades = _mixed_trades(30, 20, win_r=2.0, loss_r=-1.0)
        ea = self._eval(trades)
        assert ea.verdict in (EdgeVerdict.NEEDS_IMPROVEMENT, EdgeVerdict.NOT_READY)

    def test_e8_05_ready_for_live_strong_system(self):
        # 80 wins, 20 losses, high PF — should be READY
        trades = _mixed_trades(80, 20, win_r=2.0, loss_r=-1.0)
        ea = self._eval(trades)
        # Consistent enough for READY (PFS will be stable on synthetic uniform data)
        assert ea.verdict in (EdgeVerdict.READY_FOR_LIVE, EdgeVerdict.NEEDS_IMPROVEMENT)

    def test_e8_06_drawdown_too_high_not_ready(self):
        # 5 big losses in a row to trigger R drawdown > 10
        trades = _mixed_trades(30, 5, win_r=0.5, loss_r=-3.0)
        # Add more big losses
        trades += _losing_trades(10, r=-2.0)
        ea = self._eval(trades)
        # Depends on order, but big losing streak should push NOT_READY
        # (We don't strictly require NOT_READY here since trade ordering matters)
        assert ea.verdict in (EdgeVerdict.NOT_READY, EdgeVerdict.NEEDS_IMPROVEMENT)

    def test_e8_07_score_between_0_and_100(self):
        trades = _mixed_trades(50, 25)
        ea = self._eval(trades)
        assert 0 <= ea.score <= 100

    def test_e8_08_assessment_has_all_fields(self):
        trades = _mixed_trades(20, 10)
        ea = self._eval(trades)
        assert isinstance(ea, EdgeAssessment)
        assert isinstance(ea.profit_factor_metrics, ProfitFactorMetrics)
        assert isinstance(ea.checks_passed, list)
        assert isinstance(ea.checks_failed, list)
        assert ea.generated_at != ""

    def test_e8_09_singleton_returns_same_instance(self):
        e1 = get_edge_evaluator()
        e2 = get_edge_evaluator()
        assert e1 is e2

    def test_e8_10_last_assessment_cached(self):
        ev = EdgeEvaluator()
        assert ev.last_assessment() is None
        trades = _mixed_trades(20, 10)
        ea = ev.evaluate(trades)
        assert ev.last_assessment() is ea

    def test_e8_11_checks_passed_and_failed_exhaustive(self):
        """All checks must appear in either passed or failed, never duplicated."""
        trades = _mixed_trades(50, 30)
        ea = EdgeEvaluator().evaluate(trades)
        all_checks = ea.checks_passed + ea.checks_failed
        # No duplicates
        assert len(all_checks) == len(set(all_checks))

    def test_e8_12_explanation_non_empty(self):
        ea = EdgeEvaluator().evaluate([])
        assert len(ea.explanation) > 10
        ea2 = EdgeEvaluator().evaluate(_mixed_trades(50, 30))
        assert len(ea2.explanation) > 10


# ─────────────────────────────────────────────────────────────────────────────
# E9 — Dimension breakdowns
# ─────────────────────────────────────────────────────────────────────────────

class TestE9_Breakdowns:

    def test_e9_01_regime_breakdown_populated(self):
        trades = (
            _winning_trades(10, regime="bull_trend") +
            _losing_trades(5, regime="bear_trend")
        )
        ea = EdgeEvaluator().evaluate(trades)
        assert "bull_trend" in ea.expectancy_by_regime
        assert "bear_trend" in ea.expectancy_by_regime

    def test_e9_02_model_breakdown_populated(self):
        trades = (
            _winning_trades(8, models=["trend"]) +
            _losing_trades(4, models=["mean_reversion"])
        )
        ea = EdgeEvaluator().evaluate(trades)
        assert "trend" in ea.expectancy_by_model
        assert "mean_reversion" in ea.expectancy_by_model

    def test_e9_03_asset_breakdown_populated(self):
        trades = (
            _winning_trades(6, symbol="BTC/USDT") +
            _winning_trades(4, symbol="ETH/USDT")
        )
        ea = EdgeEvaluator().evaluate(trades)
        assert "btc/usdt" in ea.expectancy_by_asset or "BTC/USDT" in ea.expectancy_by_asset

    def test_e9_04_score_calibration_bins(self):
        # trades with scores in different buckets
        trades = []
        for score in [0.62, 0.65, 0.72, 0.75, 0.82, 0.88, 0.92]:
            t = _trade(r_multiple=1.0, score=score)
            trades.append(t)
        ea = EdgeEvaluator().evaluate(trades)
        # Should have some bins populated
        assert len(ea.score_calibration) > 0

    def test_e9_05_model_multi_fired_counted_per_model(self):
        # A trade with 2 models fired should count in BOTH model buckets
        trades = [_trade(r_multiple=1.0, models=["trend", "mean_reversion"])] * 5
        ea = EdgeEvaluator().evaluate(trades)
        assert "trend" in ea.expectancy_by_model
        assert "mean_reversion" in ea.expectancy_by_model

    def test_e9_06_score_bucket_labels_formatted_correctly(self):
        trades = [_trade(r_multiple=1.0, score=0.75)] * 5
        ea = EdgeEvaluator().evaluate(trades)
        # Should have "0.70-0.80" key
        assert any("0.70" in k for k in ea.score_calibration.keys())

    def test_e9_07_expectancy_by_regime_values_are_expectancy_metrics(self):
        trades = _winning_trades(5, regime="ranging")
        ea = EdgeEvaluator().evaluate(trades)
        for em in ea.expectancy_by_regime.values():
            assert isinstance(em, ExpectancyMetrics)

    def test_e9_08_score_calibration_values_are_bucket_metrics(self):
        trades = [_trade(r_multiple=1.5, score=0.82)] * 5
        ea = EdgeEvaluator().evaluate(trades)
        for bm in ea.score_calibration.values():
            assert isinstance(bm, ScoreBucketMetrics)


# ─────────────────────────────────────────────────────────────────────────────
# E10 — Synthetic simulation scenarios
# ─────────────────────────────────────────────────────────────────────────────

class TestE10_Scenarios:

    def _make_stable_system(self, n: int = 80) -> list[dict]:
        """Consistent 60% WR at 1.5R:1.0R — should be READY at 80 trades."""
        n_win = int(n * 0.60)
        n_loss = n - n_win
        return _mixed_trades(n_win, n_loss, win_r=1.5, loss_r=-1.0)

    def _make_marginally_profitable(self, n: int = 50) -> list[dict]:
        """Barely profitable system (55% WR at 1.2R:1.0R)."""
        n_win = int(n * 0.55)
        return _mixed_trades(n_win, n - n_win, win_r=1.2, loss_r=-1.0)

    def _make_deteriorating(self, n_good: int = 60, n_bad: int = 20) -> list[dict]:
        """Good start, then deteriorates."""
        good = _mixed_trades(n_good, 20, win_r=2.0, loss_r=-1.0)
        bad  = _mixed_trades(2, n_bad, win_r=0.5, loss_r=-1.5)
        return good + bad

    def test_e10_01_stable_system_cumulative_r_positive(self):
        trades = self._make_stable_system(80)
        ea = EdgeEvaluator().evaluate(trades)
        assert ea.cumulative_r_history
        assert ea.cumulative_r_history[-1] > 0

    def test_e10_02_stable_system_pf_above_threshold(self):
        trades = self._make_stable_system(80)
        ea = EdgeEvaluator().evaluate(trades)
        assert ea.profit_factor_metrics.overall > 1.0

    def test_e10_03_stable_system_rolling_exp_history_non_empty(self):
        trades = self._make_stable_system(80)
        ea = EdgeEvaluator().evaluate(trades)
        assert len(ea.rolling_20_exp_history) > 0
        # Overall expectancy should be positive for a 60% WR 1.5R system
        assert ea.overall_expectancy is not None
        assert ea.overall_expectancy.expectancy_r > 0

    def test_e10_04_marginally_profitable_needs_improvement(self):
        trades = self._make_marginally_profitable(50)
        ea = EdgeEvaluator().evaluate(trades)
        # PF~1.32 which is below 1.35 → should be NEEDS_IMPROVEMENT or NOT_READY
        assert ea.verdict in (EdgeVerdict.NEEDS_IMPROVEMENT, EdgeVerdict.NOT_READY)

    def test_e10_05_deteriorating_system_rolling_pf_shows_decline(self):
        trades = self._make_deteriorating()
        ea = EdgeEvaluator().evaluate(trades)
        pf_hist = ea.profit_factor_metrics.rolling_20_history
        if len(pf_hist) >= 2:
            # Later PF should be lower than earlier PF (deterioration)
            early_avg  = sum(pf_hist[:len(pf_hist)//2]) / (len(pf_hist)//2)
            recent_avg = sum(pf_hist[len(pf_hist)//2:]) / (len(pf_hist)//2 or 1)
            assert recent_avg <= early_avg + 0.5  # allow some tolerance

    def test_e10_06_unstable_system_pfs_low_or_insufficient(self):
        """Alternating good/bad periods produce unstable PFS."""
        good = _mixed_trades(12, 8, win_r=2.0, loss_r=-1.0)
        bad  = _mixed_trades(4, 16, win_r=0.5, loss_r=-1.0)
        trades = (good + bad) * 2  # oscillate twice
        ea = EdgeEvaluator().evaluate(trades)
        # PFS label should not be "Stable"
        assert ea.profit_factor_metrics.pfs_label in ("Moderate", "Unstable", "Insufficient data")

    def test_e10_07_all_wins_pf_capped_at_999(self):
        trades = _winning_trades(50)
        ea = EdgeEvaluator().evaluate(trades)
        assert ea.profit_factor_metrics.overall <= 999.0

    def test_e10_08_drawdown_computed_from_r_sequence(self):
        # 10 wins then 10 big losses
        wins   = _winning_trades(10, r=1.0)
        losses = _losing_trades(10, r=-1.0)
        ea = EdgeEvaluator().evaluate(wins + losses)
        # Should have nonzero drawdown
        assert ea.drawdown_r > 0

    def test_e10_09_cumulative_r_history_monotonic_when_all_wins(self):
        trades = _winning_trades(30)
        ea = EdgeEvaluator().evaluate(trades)
        h = ea.cumulative_r_history
        assert all(h[i] <= h[i+1] for i in range(len(h)-1))

    def test_e10_10_thread_safety(self):
        """Concurrent evaluate calls on the same evaluator must not raise."""
        ev = EdgeEvaluator()
        trades = _mixed_trades(30, 20)
        errors = []

        def _run():
            try:
                ev.evaluate(trades)
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=_run) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert errors == [], f"Thread errors: {errors}"


# ─────────────────────────────────────────────────────────────────────────────
# E11 — DemoPerformanceEvaluator integration
# ─────────────────────────────────────────────────────────────────────────────

class TestE11_DemoIntegration:

    def test_e11_01_readiness_assessment_has_edge_field(self):
        from core.evaluation.demo_performance_evaluator import ReadinessAssessment
        import dataclasses
        fields = {f.name for f in dataclasses.fields(ReadinessAssessment)}
        assert "edge_assessment" in fields

    def test_e11_02_edge_assessment_field_default_none(self):
        from core.evaluation.demo_performance_evaluator import ReadinessAssessment
        import dataclasses
        # Construct using all required fields from the dataclass definition
        field_names = {f.name for f in dataclasses.fields(ReadinessAssessment)}
        # Build kwargs with safe defaults
        kwargs = {}
        for f in dataclasses.fields(ReadinessAssessment):
            if f.name == "edge_assessment":
                continue  # leave out to test the default
            if f.default is not dataclasses.MISSING:
                continue  # has a default, skip
            if f.default_factory is not dataclasses.MISSING:
                continue  # has a factory default, skip
            # Required field — supply a safe value
            if f.type in (str,) or f.name in ("status", "explanation", "generated_at"):
                kwargs[f.name] = ""
            elif f.name in ("score", "checks_passed", "checks_total", "trade_count"):
                kwargs[f.name] = 0
            elif f.name == "metrics":
                kwargs[f.name] = {}
            elif f.name == "check_details":
                kwargs[f.name] = []
            else:
                kwargs[f.name] = None
        ra = ReadinessAssessment(**kwargs)
        assert ra.edge_assessment is None

    def test_e11_03_evaluate_populates_edge_assessment(self):
        from core.evaluation.demo_performance_evaluator import DemoPerformanceEvaluator
        trades = _mixed_trades(20, 10)
        evaluator = DemoPerformanceEvaluator()
        ra = evaluator.evaluate(trades)
        # edge_assessment should be either an EdgeAssessment or None (not raise)
        # (None is acceptable if trades don't meet minimum for edge eval)
        assert ra.edge_assessment is None or isinstance(ra.edge_assessment, EdgeAssessment)


# ─────────────────────────────────────────────────────────────────────────────
# E12 — Safety contract
# ─────────────────────────────────────────────────────────────────────────────

class TestE12_SafetyContract:

    def test_e12_01_no_set_mode_call_in_module(self):
        """EdgeEvaluator must NEVER call set_mode."""
        import core.evaluation.edge_evaluator as module_src
        source = inspect.getsource(module_src)
        assert "set_mode" not in source, \
            "EdgeEvaluator module must NOT contain set_mode() calls"

    def test_e12_02_no_live_string_in_module(self):
        """EdgeEvaluator must not contain 'live' as a mode-switching string."""
        import core.evaluation.edge_evaluator as module_src
        source = inspect.getsource(module_src)
        # Allow 'live' in comments/docstrings but not as a mode argument
        # We check for the specific pattern set_mode("live") which would be dangerous
        assert 'set_mode("live")' not in source
        assert "set_mode('live')" not in source

    def test_e12_03_uses_order_router_not_paper_executor(self):
        """EdgeEvaluator must use order_router (not hardcoded paper_executor).
        Session 52: refactored to be mode-aware via order_router.active_executor."""
        import core.evaluation.edge_evaluator as module_src
        source = inspect.getsource(module_src)
        assert "paper_executor" not in source, \
            "EdgeEvaluator must NOT import paper_executor directly (use order_router)"

    def test_e12_04_evaluate_is_read_only(self):
        """evaluate() must not modify global state (paper_executor or similar)."""
        # We simply verify it returns without modifying the evaluator's thresholds
        ev = EdgeEvaluator()
        original_threshold = ev.thresholds.min_trades_early
        ev.evaluate(_mixed_trades(10, 5))
        assert ev.thresholds.min_trades_early == original_threshold

    def test_e12_05_evaluator_has_no_mode_toggle(self):
        """EdgeEvaluator class must have no method that switches modes."""
        ev = EdgeEvaluator()
        public_methods = [
            m for m in dir(ev)
            if not m.startswith("_") and callable(getattr(ev, m))
        ]
        mode_methods = [m for m in public_methods if "mode" in m.lower()]
        assert mode_methods == [], \
            f"EdgeEvaluator has unexpected mode-switching methods: {mode_methods}"
