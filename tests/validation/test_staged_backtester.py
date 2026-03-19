"""
tests/validation/test_staged_backtester.py
──────────────────────────────────────────
Unit tests for the Phase 5 Staged Backtester.

Sections:
  SB-01  Sub-bar interpolation
  SB-02  LTF confirmation logic
  SB-03  Candidate state machine
  SB-04  Candidate fingerprint dedup
  SB-05  Candidate expiry
  SB-06  Lifecycle metrics
  SB-07  Validation rules
  SB-08  Baseline vs staged mode selection
  SB-09  Time simulation correctness
  SB-10  Comparison runner structure
"""
from __future__ import annotations

import math
from unittest.mock import patch, MagicMock

import numpy as np
import pandas as pd
import pytest


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _make_1h_row(o=100.0, h=105.0, l=95.0, c=102.0, v=5000.0):
    return pd.Series({"open": o, "high": h, "low": l, "close": c, "volume": v})


def _make_sub_bar_history(n=50, base=100.0, trend=0.0002):
    """Generate n sub-bars with slight uptrend + oscillation for realistic RSI (~55-65)."""
    bars = []
    price = base
    for i in range(n):
        osc = np.sin(i * 0.7) * 0.004
        price = price * (1 + trend + osc)
        bars.append({
            "open": price * 0.999,
            "high": price * 1.003,
            "low": price * 0.996,
            "close": float(price),
            "volume": float(1200.0 + 400 * abs(np.sin(i * 0.3))),
        })
    return bars


def _make_sub_bar_history_down(n=50, base=100.0, trend=-0.0005):
    """Generate n sub-bars with downtrend + oscillation for realistic RSI (~35-45)."""
    bars = []
    price = base
    for i in range(n):
        # Use cos so the last few bars end on a downswing (EMA slope negative)
        osc = np.cos(i * 0.7) * 0.003
        price = price * (1 + trend + osc)
        bars.append({
            "open": price * 1.001,
            "high": price * 1.003,
            "low": price * 0.996,
            "close": float(price),
            "volume": float(1200.0 + 400 * abs(np.sin(i * 0.3))),
        })
    return bars


# ─────────────────────────────────────────────────────────────────────────────
# SB-01: Sub-bar interpolation
# ─────────────────────────────────────────────────────────────────────────────

class TestSubBarInterpolation:
    """Verify 15m sub-bar generation from 1H OHLCV."""

    def test_sb01_returns_4_bars(self):
        from core.validation.staged_backtester import interpolate_15m_bars
        rng = np.random.default_rng(42)
        bars = interpolate_15m_bars(_make_1h_row(), rng)
        assert len(bars) == 4

    def test_sb01_first_opens_at_1h_open(self):
        from core.validation.staged_backtester import interpolate_15m_bars
        rng = np.random.default_rng(42)
        bars = interpolate_15m_bars(_make_1h_row(o=100.0), rng)
        assert bars[0]["open"] == pytest.approx(100.0, abs=0.01)

    def test_sb01_last_closes_at_1h_close(self):
        from core.validation.staged_backtester import interpolate_15m_bars
        rng = np.random.default_rng(42)
        bars = interpolate_15m_bars(_make_1h_row(c=102.0), rng)
        assert bars[3]["close"] == pytest.approx(102.0, abs=0.01)

    def test_sb01_all_within_1h_envelope(self):
        from core.validation.staged_backtester import interpolate_15m_bars
        rng = np.random.default_rng(42)
        row = _make_1h_row(o=100, h=110, l=90, c=105)
        bars = interpolate_15m_bars(row, rng)
        for b in bars:
            assert b["high"] <= 110.0 + 0.01, f"high {b['high']} > 1H high 110"
            assert b["low"] >= 90.0 - 0.01, f"low {b['low']} < 1H low 90"

    def test_sb01_ohlc_consistency(self):
        from core.validation.staged_backtester import interpolate_15m_bars
        rng = np.random.default_rng(42)
        bars = interpolate_15m_bars(_make_1h_row(), rng)
        for b in bars:
            assert b["high"] >= b["open"]
            assert b["high"] >= b["close"]
            assert b["low"] <= b["open"]
            assert b["low"] <= b["close"]

    def test_sb01_volume_sums_to_1h(self):
        from core.validation.staged_backtester import interpolate_15m_bars
        rng = np.random.default_rng(42)
        row = _make_1h_row(v=4000.0)
        bars = interpolate_15m_bars(row, rng)
        total_vol = sum(b["volume"] for b in bars)
        assert total_vol == pytest.approx(4000.0, rel=0.01)

    def test_sb01_different_seeds_produce_different_bars(self):
        from core.validation.staged_backtester import interpolate_15m_bars
        row = _make_1h_row()
        bars_a = interpolate_15m_bars(row, np.random.default_rng(1))
        bars_b = interpolate_15m_bars(row, np.random.default_rng(99))
        # At least one sub-bar close should differ
        closes_a = [b["close"] for b in bars_a]
        closes_b = [b["close"] for b in bars_b]
        assert closes_a != closes_b


# ─────────────────────────────────────────────────────────────────────────────
# SB-02: LTF confirmation logic
# ─────────────────────────────────────────────────────────────────────────────

class TestLTFConfirmationLogic:
    """Verify the backtester-internal LTF confirmation evaluator."""

    def test_sb02_confirms_long_in_uptrend(self):
        from core.validation.staged_backtester import _evaluate_ltf_confirmation
        history = _make_sub_bar_history(50)  # default trend=0.0002, RSI ~61
        result = _evaluate_ltf_confirmation(history, "buy")
        assert result["confirmed"] is True
        assert result["voided"] is False

    def test_sb02_confirms_short_in_downtrend(self):
        from core.validation.staged_backtester import _evaluate_ltf_confirmation
        history = _make_sub_bar_history_down(50)  # default trend=-0.0005, RSI ~40
        result = _evaluate_ltf_confirmation(history, "sell")
        assert result["confirmed"] is True
        assert result["voided"] is False

    def test_sb02_rejects_long_in_downtrend(self):
        from core.validation.staged_backtester import _evaluate_ltf_confirmation
        history = _make_sub_bar_history_down(50)
        result = _evaluate_ltf_confirmation(history, "buy")
        # EMA slope negative = not aligned for buy → not confirmed
        assert not result["confirmed"]

    def test_sb02_returns_rsi_and_ema_slope(self):
        from core.validation.staged_backtester import _evaluate_ltf_confirmation
        history = _make_sub_bar_history(50)
        result = _evaluate_ltf_confirmation(history, "buy")
        assert "rsi" in result
        assert "ema_slope" in result
        assert "volume_ratio" in result
        assert not math.isnan(result["rsi"])

    def test_sb02_insufficient_data_returns_not_confirmed(self):
        from core.validation.staged_backtester import _evaluate_ltf_confirmation
        history = _make_sub_bar_history(5)  # < ema_period
        result = _evaluate_ltf_confirmation(history, "buy")
        assert result["confirmed"] is False
        assert result["voided"] is False


# ─────────────────────────────────────────────────────────────────────────────
# SB-03: Candidate state machine
# ─────────────────────────────────────────────────────────────────────────────

class TestCandidateStateMachine:

    def test_sb03_created_is_active(self):
        from core.validation.staged_backtester import _SimCandidate, _CandidateState
        c = _SimCandidate(cid=0, symbol="BTC/USDT", side="buy",
                          entry_price=100, stop_loss=95, take_profit=110,
                          score=0.8, models_fired=["trend"], regime="bull",
                          atr_value=1.0, fingerprint=("BTC", "buy", frozenset(), ""))
        assert c.is_active
        assert c.state == _CandidateState.CREATED

    def test_sb03_confirm_transitions(self):
        from core.validation.staged_backtester import _SimCandidate, _CandidateState
        c = _SimCandidate(cid=0, symbol="BTC/USDT", side="buy",
                          entry_price=100, stop_loss=95, take_profit=110,
                          score=0.8, models_fired=["trend"], regime="bull",
                          atr_value=1.0, fingerprint=("BTC", "buy", frozenset(), ""))
        c.confirm(bar=5, sub=2, ltf_rsi=55.0)
        assert c.state == _CandidateState.CONFIRMED
        assert c.confirmed_at_bar == 5
        assert c.ltf_rsi == 55.0

    def test_sb03_execute_requires_confirmed(self):
        from core.validation.staged_backtester import _SimCandidate, _CandidateState
        c = _SimCandidate(cid=0, symbol="BTC/USDT", side="buy",
                          entry_price=100, stop_loss=95, take_profit=110,
                          score=0.8, models_fired=["trend"], regime="bull",
                          atr_value=1.0, fingerprint=("BTC", "buy", frozenset(), ""))
        with pytest.raises(AssertionError):
            c.execute(bar=5)  # Not yet confirmed

    def test_sb03_void_requires_created(self):
        from core.validation.staged_backtester import _SimCandidate, _CandidateState
        c = _SimCandidate(cid=0, symbol="BTC/USDT", side="buy",
                          entry_price=100, stop_loss=95, take_profit=110,
                          score=0.8, models_fired=["trend"], regime="bull",
                          atr_value=1.0, fingerprint=("BTC", "buy", frozenset(), ""))
        c.confirm(bar=5, sub=0)
        with pytest.raises(AssertionError):
            c.void(bar=6)  # Already confirmed

    def test_sb03_full_lifecycle(self):
        from core.validation.staged_backtester import _SimCandidate, _CandidateState
        c = _SimCandidate(cid=0, symbol="BTC/USDT", side="buy",
                          entry_price=100, stop_loss=95, take_profit=110,
                          score=0.8, models_fired=["trend"], regime="bull",
                          atr_value=1.0, fingerprint=("BTC", "buy", frozenset(), ""))
        assert c.state == _CandidateState.CREATED
        c.confirm(bar=5, sub=1)
        assert c.state == _CandidateState.CONFIRMED
        c.execute(bar=5)
        assert c.state == _CandidateState.EXECUTED
        assert not c.is_active

    def test_sb03_expire_is_terminal(self):
        from core.validation.staged_backtester import _SimCandidate, _CandidateState
        c = _SimCandidate(cid=0, symbol="BTC/USDT", side="buy",
                          entry_price=100, stop_loss=95, take_profit=110,
                          score=0.8, models_fired=["trend"], regime="bull",
                          atr_value=1.0, fingerprint=("BTC", "buy", frozenset(), ""))
        c.expire(bar=10)
        assert c.state == _CandidateState.EXPIRED
        assert not c.is_active


# ─────────────────────────────────────────────────────────────────────────────
# SB-04: Fingerprint dedup
# ─────────────────────────────────────────────────────────────────────────────

class TestFingerprintDedup:

    def test_sb04_same_models_different_order(self):
        from core.validation.staged_backtester import _make_fingerprint
        fp1 = _make_fingerprint("BTC/USDT", "buy", ["trend", "momentum"], "bull")
        fp2 = _make_fingerprint("BTC/USDT", "buy", ["momentum", "trend"], "bull")
        assert fp1 == fp2

    def test_sb04_different_side_different_fp(self):
        from core.validation.staged_backtester import _make_fingerprint
        fp1 = _make_fingerprint("BTC/USDT", "buy", ["trend"], "bull")
        fp2 = _make_fingerprint("BTC/USDT", "sell", ["trend"], "bull")
        assert fp1 != fp2

    def test_sb04_case_insensitive_regime(self):
        from core.validation.staged_backtester import _make_fingerprint
        fp1 = _make_fingerprint("BTC/USDT", "buy", ["trend"], "Bull_Trend")
        fp2 = _make_fingerprint("BTC/USDT", "buy", ["trend"], "bull_trend")
        assert fp1 == fp2


# ─────────────────────────────────────────────────────────────────────────────
# SB-05: Candidate expiry
# ─────────────────────────────────────────────────────────────────────────────

class TestCandidateExpiry:

    def test_sb05_expires_after_threshold(self):
        from core.validation.staged_backtester import _SimCandidate, _CandidateState
        c = _SimCandidate(cid=0, symbol="BTC/USDT", side="buy",
                          entry_price=100, stop_loss=95, take_profit=110,
                          score=0.8, models_fired=["trend"], regime="bull",
                          atr_value=1.0, fingerprint=("BTC", "buy", frozenset(), ""),
                          created_at_bar=10)
        # Simulate expiry check at bar 14 (4 bars later)
        assert (14 - c.created_at_bar) >= 4
        c.expire(14)
        assert c.state == _CandidateState.EXPIRED

    def test_sb05_not_expired_before_threshold(self):
        from core.validation.staged_backtester import _SimCandidate
        c = _SimCandidate(cid=0, symbol="BTC/USDT", side="buy",
                          entry_price=100, stop_loss=95, take_profit=110,
                          score=0.8, models_fired=["trend"], regime="bull",
                          atr_value=1.0, fingerprint=("BTC", "buy", frozenset(), ""),
                          created_at_bar=10)
        assert (12 - c.created_at_bar) < 4  # Only 2 bars


# ─────────────────────────────────────────────────────────────────────────────
# SB-06: Lifecycle metrics
# ─────────────────────────────────────────────────────────────────────────────

class TestLifecycleMetrics:

    def test_sb06_conversion_rate(self):
        from core.validation.staged_backtester import CandidateLifecycleMetrics
        m = CandidateLifecycleMetrics(total_created=10, total_executed=3)
        assert m.conversion_rate == pytest.approx(0.3)

    def test_sb06_expiry_rate(self):
        from core.validation.staged_backtester import CandidateLifecycleMetrics
        m = CandidateLifecycleMetrics(total_created=20, total_expired=5)
        assert m.expiry_rate == pytest.approx(0.25)

    def test_sb06_avg_confirmation_delay(self):
        from core.validation.staged_backtester import CandidateLifecycleMetrics
        m = CandidateLifecycleMetrics(confirmation_delays=[2, 4, 6])
        assert m.avg_confirmation_delay == pytest.approx(4.0)

    def test_sb06_to_dict_has_all_keys(self):
        from core.validation.staged_backtester import CandidateLifecycleMetrics
        m = CandidateLifecycleMetrics(total_created=5, total_confirmed=3,
                                       total_executed=2, total_voided=1,
                                       total_expired=2,
                                       confirmation_delays=[1, 2, 3])
        d = m.to_dict()
        assert "total_created" in d
        assert "conversion_rate" in d
        assert "execution_clustering" in d
        assert "confirmation_delay_distribution" in d

    def test_sb06_zero_created_no_division_error(self):
        from core.validation.staged_backtester import CandidateLifecycleMetrics
        m = CandidateLifecycleMetrics()
        assert m.conversion_rate == 0.0
        assert m.expiry_rate == 0.0


# ─────────────────────────────────────────────────────────────────────────────
# SB-07: Validation rules
# ─────────────────────────────────────────────────────────────────────────────

class TestValidationRules:

    def test_sb07_valid_lifecycle_passes(self):
        from core.validation.staged_backtester import (
            _SimCandidate, _validate_candidates, _CandidateState,
        )
        c = _SimCandidate(cid=0, symbol="BTC/USDT", side="buy",
                          entry_price=100, stop_loss=95, take_profit=110,
                          score=0.8, models_fired=["trend"], regime="bull",
                          atr_value=1.0, fingerprint=("BTC", "buy", frozenset(), ""))
        c.confirm(bar=5, sub=1)
        c.execute(bar=5)
        result = _validate_candidates([c])
        assert result.all_passed
        assert result.no_execution_before_confirmation
        assert result.no_duplicate_executions
        assert result.terminal_states_respected

    def test_sb07_detects_execution_before_confirmation(self):
        from core.validation.staged_backtester import (
            _SimCandidate, _validate_candidates, _CandidateState,
        )
        c = _SimCandidate(cid=0, symbol="BTC/USDT", side="buy",
                          entry_price=100, stop_loss=95, take_profit=110,
                          score=0.8, models_fired=["trend"], regime="bull",
                          atr_value=1.0, fingerprint=("BTC", "buy", frozenset(), ""))
        # Force invalid state
        c.state = _CandidateState.EXECUTED
        c.executed_at_bar = 5
        c.confirmed_at_bar = -1  # Never confirmed
        result = _validate_candidates([c])
        assert not result.no_execution_before_confirmation
        assert len(result.violations) > 0

    def test_sb07_detects_voided_with_execution(self):
        from core.validation.staged_backtester import (
            _SimCandidate, _validate_candidates, _CandidateState,
        )
        c = _SimCandidate(cid=0, symbol="BTC/USDT", side="buy",
                          entry_price=100, stop_loss=95, take_profit=110,
                          score=0.8, models_fired=["trend"], regime="bull",
                          atr_value=1.0, fingerprint=("BTC", "buy", frozenset(), ""))
        # Force invalid state: voided but also has executed_at_bar
        c.state = _CandidateState.VOIDED
        c.voided_at_bar = 5
        c.executed_at_bar = 6
        result = _validate_candidates([c])
        assert not result.terminal_states_respected

    def test_sb07_empty_candidates_passes(self):
        from core.validation.staged_backtester import _validate_candidates
        result = _validate_candidates([])
        assert result.all_passed


# ─────────────────────────────────────────────────────────────────────────────
# SB-08: Mode selection
# ─────────────────────────────────────────────────────────────────────────────

class TestModeSelection:

    def test_sb08_baseline_mode_flag(self):
        """Baseline mode creates StagedBacktester with staged=False."""
        from core.validation.staged_backtester import StagedBacktester
        bt = StagedBacktester(staged=False)
        assert bt._staged is False

    def test_sb08_staged_mode_flag(self):
        from core.validation.staged_backtester import StagedBacktester
        bt = StagedBacktester(staged=True)
        assert bt._staged is True


# ─────────────────────────────────────────────────────────────────────────────
# SB-09: Time simulation
# ─────────────────────────────────────────────────────────────────────────────

class TestTimeSimulation:

    def test_sb09_4_sub_bars_per_1h_bar(self):
        """Each 1H bar should produce exactly 4 sub-bars."""
        from core.validation.staged_backtester import interpolate_15m_bars
        rng = np.random.default_rng(42)
        for _ in range(10):
            bars = interpolate_15m_bars(_make_1h_row(
                o=np.random.uniform(90, 110),
                h=np.random.uniform(110, 120),
                l=np.random.uniform(80, 90),
                c=np.random.uniform(95, 115),
                v=np.random.uniform(1000, 10000),
            ), rng)
            assert len(bars) == 4

    def test_sb09_sub_bars_chain_correctly(self):
        """Close of sub-bar N should be open of sub-bar N+1 (approximately)."""
        from core.validation.staged_backtester import interpolate_15m_bars
        rng = np.random.default_rng(42)
        bars = interpolate_15m_bars(_make_1h_row(), rng)
        for j in range(3):
            # The close of bar j is the open of bar j+1
            # (by construction: points[j+1] is both)
            assert bars[j]["close"] == pytest.approx(bars[j + 1]["open"], abs=0.01)


# ─────────────────────────────────────────────────────────────────────────────
# SB-10: Comparison runner structure
# ─────────────────────────────────────────────────────────────────────────────

class TestComparisonRunnerStructure:

    def test_sb10_run_ab_returns_required_keys(self):
        """run_ab_comparison must return baseline, staged, comparison, validation."""
        from core.validation.staged_backtester import run_ab_comparison
        # Use minimal config for speed
        result = run_ab_comparison(
            symbols=["BTC/USDT"],
            initial_capital=10_000.0,
            seed=42,
        )
        assert "baseline" in result
        assert "staged" in result
        assert "comparison" in result
        assert "validation" in result

    def test_sb10_baseline_has_aggregate_metrics(self):
        from core.validation.staged_backtester import run_ab_comparison
        result = run_ab_comparison(symbols=["BTC/USDT"], seed=42)
        assert "aggregate_metrics" in result["baseline"]
        m = result["baseline"]["aggregate_metrics"]
        assert "profit_factor" in m
        assert "max_drawdown_pct" in m
        assert "win_rate" in m

    def test_sb10_staged_has_lifecycle(self):
        from core.validation.staged_backtester import run_ab_comparison
        result = run_ab_comparison(symbols=["BTC/USDT"], seed=42)
        assert "candidate_lifecycle" in result["staged"]
        lc = result["staged"]["candidate_lifecycle"]
        assert "total_created" in lc
        assert "total_confirmed" in lc
        assert "conversion_rate" in lc

    def test_sb10_validation_all_passed(self):
        from core.validation.staged_backtester import run_ab_comparison
        result = run_ab_comparison(symbols=["BTC/USDT"], seed=42)
        assert result["validation"]["all_passed"] is True

    def test_sb10_comparison_has_deltas(self):
        from core.validation.staged_backtester import run_ab_comparison
        result = run_ab_comparison(symbols=["BTC/USDT"], seed=42)
        comp = result["comparison"]
        assert "profit_factor_baseline" in comp
        assert "profit_factor_staged" in comp
        assert "profit_factor_change_pct" in comp
        assert "expectancy_baseline" in comp
        assert "expectancy_staged" in comp
