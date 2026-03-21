"""
tests/unit/test_session26_demo_validation.py
============================================
Tests for Session 26 — Live Demo Validation:
  - Trade execution verification fields (S26-01 through S26-06)
  - LiveVsBacktestTracker (S26-07 through S26-16)
  - DemoMonitorWidget construction & formatters (S26-17 through S26-20)
"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock, patch

import pytest


# ─────────────────────────────────────────────────────────────────────────────
# S26-01 through S26-06 — Trade execution fields in paper_executor
# ─────────────────────────────────────────────────────────────────────────────

class TestTradeExecutionFields:
    """Verify risk_amount_usdt, expected_rr, symbol_weight, adjusted_score wiring."""

    def _make_candidate(self, **overrides):
        from core.meta_decision.order_candidate import OrderCandidate
        defaults = dict(
            symbol="SOL/USDT", side="buy",
            entry_type="market", entry_price=100.0,
            stop_loss_price=95.0, take_profit_price=110.0,
            position_size_usdt=500.0, score=0.72,
            models_fired=["trend"], regime="bull_trend",
            rationale="test", timeframe="1h", atr_value=2.0,
            approved=True,
        )
        defaults.update(overrides)
        return OrderCandidate(**defaults)

    def test_s26_01_risk_amount_usdt_computed_on_submit(self, paper_executor):
        """risk_amount_usdt = |entry - stop| / entry × size_usdt."""
        cand = self._make_candidate(
            entry_price=100.0, stop_loss_price=95.0,
            take_profit_price=110.0, position_size_usdt=500.0,
        )
        cand.symbol_weight  = 1.3
        cand.adjusted_score = 0.72 * 1.3
        ok = paper_executor.submit(cand)
        assert ok
        pos_list = paper_executor._positions.get("SOL/USDT", [])
        assert pos_list, "Position was not created"
        pos = pos_list[-1]
        # risk_amount_usdt = |fill - stop| / fill × size; fill is slippage-adjusted
        assert pos.risk_amount_usdt > 0
        # for a buy with entry≈100, stop=95, size=500 → risk ≈ 5/100 × 500 ≈ 25 USDT
        assert 20.0 <= pos.risk_amount_usdt <= 30.0

    def test_s26_02_expected_rr_computed_on_submit(self, paper_executor):
        """expected_rr = (tp - entry) / (entry - sl) for buy."""
        cand = self._make_candidate(
            entry_price=100.0, stop_loss_price=95.0,
            take_profit_price=110.0, position_size_usdt=500.0,
        )
        cand.symbol_weight  = 1.0
        cand.adjusted_score = cand.score
        paper_executor.submit(cand)
        pos = paper_executor._positions.get("SOL/USDT", [])[-1]
        # (110 - fill) / (fill - 95); fill ≈ 100 → RR ≈ 10/5 = 2.0
        assert 1.5 <= pos.expected_rr <= 2.5

    def test_s26_03_symbol_weight_stored_on_position(self, paper_executor):
        cand = self._make_candidate()
        cand.symbol_weight  = 1.3
        cand.adjusted_score = round(cand.score * 1.3, 4)
        paper_executor.submit(cand)
        pos = paper_executor._positions.get("SOL/USDT", [])[-1]
        assert pos.symbol_weight == pytest.approx(1.3, abs=0.01)

    def test_s26_04_adjusted_score_stored_on_position(self, paper_executor):
        cand = self._make_candidate(score=0.72)
        cand.symbol_weight  = 1.3
        cand.adjusted_score = round(0.72 * 1.3, 4)
        paper_executor.submit(cand)
        pos = paper_executor._positions.get("SOL/USDT", [])[-1]
        assert pos.adjusted_score == pytest.approx(0.72 * 1.3, abs=0.01)

    def test_s26_05_trade_dict_has_all_new_fields(self, paper_executor):
        """After close, trade dict includes all Session 26 fields."""
        cand = self._make_candidate(
            entry_price=100.0, stop_loss_price=95.0,
            take_profit_price=110.0, position_size_usdt=500.0,
        )
        cand.symbol_weight  = 1.0
        cand.adjusted_score = cand.score
        paper_executor.submit(cand)
        pos_list = paper_executor._positions.get("SOL/USDT", [])
        assert pos_list
        pos = pos_list[-1]
        paper_executor._close_position("SOL/USDT", 108.0, "take_profit", pos)
        assert paper_executor._closed_trades, "No closed trade recorded"
        trade = paper_executor._closed_trades[-1]
        for field in ("risk_amount_usdt", "expected_rr", "symbol_weight",
                      "adjusted_score", "realized_r"):
            assert field in trade, f"Missing field: {field}"

    def test_s26_06_realized_r_sign_matches_pnl(self, paper_executor):
        """realized_r > 0 when trade is profitable."""
        cand = self._make_candidate(
            entry_price=100.0, stop_loss_price=95.0,
            take_profit_price=110.0, position_size_usdt=500.0,
        )
        cand.symbol_weight  = 1.0
        cand.adjusted_score = cand.score
        paper_executor.submit(cand)
        pos_list = paper_executor._positions.get("SOL/USDT", [])
        assert pos_list
        pos = pos_list[-1]
        paper_executor._close_position("SOL/USDT", 110.0, "take_profit", pos)
        trade = paper_executor._closed_trades[-1]
        assert trade["realized_r"] is not None
        assert trade["realized_r"] > 0, "realized_r should be positive on a winning trade"
        assert trade["pnl_usdt"] > 0, "pnl_usdt should be positive too"


# ─────────────────────────────────────────────────────────────────────────────
# S26-07 through S26-16 — LiveVsBacktestTracker
# ─────────────────────────────────────────────────────────────────────────────

class TestLiveVsBacktestTracker:
    """Tests for core/monitoring/live_vs_backtest.py."""

    def _fresh_tracker(self, tmp_path):
        from core.monitoring.live_vs_backtest import LiveVsBacktestTracker
        import core.monitoring.live_vs_backtest as lvb_mod
        original = lvb_mod._DATA_FILE
        lvb_mod._DATA_FILE = tmp_path / "lvb_test.json"
        tracker = LiveVsBacktestTracker()
        yield tracker
        lvb_mod._DATA_FILE = original

    def _make_trade(self, symbol="BTC/USDT", side="buy", pnl=100.0,
                    realized_r=1.5, models=None, slippage_pct=0.03):
        return {
            "symbol":           symbol,
            "side":             side,
            "pnl_usdt":         pnl,
            "pnl_pct":          pnl / 500.0 * 100,
            "realized_r":       realized_r,
            "models_fired":     models or ["trend"],
            "entry_price":      100.0,
            "entry_expected":   100.0 * (1.0 + slippage_pct / 100),
            "exit_price":       100.0 + pnl / 5.0,
            "risk_amount_usdt": pnl / realized_r if realized_r else 0.0,
        }

    def test_s26_07_record_increments_trade_count(self, tmp_path):
        import core.monitoring.live_vs_backtest as lvb_mod
        orig = lvb_mod._DATA_FILE
        lvb_mod._DATA_FILE = tmp_path / "lvb.json"
        try:
            from core.monitoring.live_vs_backtest import LiveVsBacktestTracker
            t = LiveVsBacktestTracker()
            assert t._trade_count == 0
            t.record(self._make_trade())
            assert t._trade_count == 1
            t.record(self._make_trade())
            assert t._trade_count == 2
        finally:
            lvb_mod._DATA_FILE = orig

    def test_s26_08_win_rate_computed_correctly(self, tmp_path):
        import core.monitoring.live_vs_backtest as lvb_mod
        orig = lvb_mod._DATA_FILE
        lvb_mod._DATA_FILE = tmp_path / "lvb.json"
        try:
            from core.monitoring.live_vs_backtest import LiveVsBacktestTracker
            t = LiveVsBacktestTracker()
            for _ in range(3): t.record(self._make_trade(pnl=50, realized_r=1.0))
            for _ in range(2): t.record(self._make_trade(pnl=-30, realized_r=-0.6))
            port = t.get_portfolio_metrics()
            assert port["win_rate"] == pytest.approx(3/5, abs=0.01)
        finally:
            lvb_mod._DATA_FILE = orig

    def test_s26_09_profit_factor_computed(self, tmp_path):
        import core.monitoring.live_vs_backtest as lvb_mod
        orig = lvb_mod._DATA_FILE
        lvb_mod._DATA_FILE = tmp_path / "lvb.json"
        try:
            from core.monitoring.live_vs_backtest import LiveVsBacktestTracker
            t = LiveVsBacktestTracker()
            for _ in range(5): t.record(self._make_trade(pnl=100, realized_r=2.0))
            for _ in range(5): t.record(self._make_trade(pnl=-50, realized_r=-1.0))
            port = t.get_portfolio_metrics()
            # gross_win_r=10, gross_loss_r=5 → PF=2.0
            assert port["profit_factor"] == pytest.approx(2.0, abs=0.1)
        finally:
            lvb_mod._DATA_FILE = orig

    def test_s26_10_per_model_metrics_tracked_separately(self, tmp_path):
        import core.monitoring.live_vs_backtest as lvb_mod
        orig = lvb_mod._DATA_FILE
        lvb_mod._DATA_FILE = tmp_path / "lvb.json"
        try:
            from core.monitoring.live_vs_backtest import LiveVsBacktestTracker
            t = LiveVsBacktestTracker()
            for _ in range(6):
                t.record(self._make_trade(models=["trend"], pnl=100, realized_r=2.0))
            for _ in range(6):
                t.record(self._make_trade(models=["momentum_breakout"], pnl=50, realized_r=1.0))
            mets = t.all_model_metrics()
            assert "trend" in mets
            assert "momentum_breakout" in mets
            assert mets["trend"]["trades"] == 6
            assert mets["momentum_breakout"]["trades"] == 6
        finally:
            lvb_mod._DATA_FILE = orig

    def test_s26_11_get_comparison_includes_baselines(self, tmp_path):
        import core.monitoring.live_vs_backtest as lvb_mod
        orig = lvb_mod._DATA_FILE
        lvb_mod._DATA_FILE = tmp_path / "lvb.json"
        try:
            from core.monitoring.live_vs_backtest import LiveVsBacktestTracker
            t = LiveVsBacktestTracker()
            for _ in range(6): t.record(self._make_trade(models=["trend"], pnl=80, realized_r=1.5))
            comp = t.get_comparison()
            assert "baselines" in comp
            assert "portfolio" in comp
            assert "per_model" in comp
            assert comp["trade_count"] == 6
        finally:
            lvb_mod._DATA_FILE = orig

    def test_s26_12_delta_computed_vs_study4(self, tmp_path):
        """delta.win_rate = live.win_rate - baseline.win_rate for trend model."""
        import core.monitoring.live_vs_backtest as lvb_mod
        orig = lvb_mod._DATA_FILE
        lvb_mod._DATA_FILE = tmp_path / "lvb.json"
        try:
            from core.monitoring.live_vs_backtest import LiveVsBacktestTracker
            t = LiveVsBacktestTracker()
            # 8 wins, 2 losses → 80% WR; baseline is 50.3%
            for _ in range(8): t.record(self._make_trade(models=["trend"], pnl=100, realized_r=2.0))
            for _ in range(2): t.record(self._make_trade(models=["trend"], pnl=-50, realized_r=-1.0))
            comp = t.get_comparison()
            trend_comp = comp["per_model"].get("trend", {})
            delta = trend_comp.get("delta", {})
            assert delta.get("win_rate") is not None
            # 80% - 50.3% = 29.7% ≈ 0.297
            assert delta["win_rate"] == pytest.approx(0.8 - 0.503, abs=0.01)
        finally:
            lvb_mod._DATA_FILE = orig

    def test_s26_13_persists_and_reloads(self, tmp_path):
        """Data survives instantiation cycle."""
        import core.monitoring.live_vs_backtest as lvb_mod
        orig = lvb_mod._DATA_FILE
        lvb_mod._DATA_FILE = tmp_path / "lvb_persist.json"
        try:
            from core.monitoring.live_vs_backtest import LiveVsBacktestTracker
            t1 = LiveVsBacktestTracker()
            for _ in range(5): t1.record(self._make_trade(pnl=100, realized_r=2.0))
            assert t1._trade_count == 5
            t2 = LiveVsBacktestTracker()
            assert t2._trade_count == 5
        finally:
            lvb_mod._DATA_FILE = orig

    def test_s26_14_rolling_window_capped(self, tmp_path):
        """_r_window never exceeds _WINDOW entries."""
        import core.monitoring.live_vs_backtest as lvb_mod
        orig = lvb_mod._DATA_FILE
        lvb_mod._DATA_FILE = tmp_path / "lvb_window.json"
        try:
            from core.monitoring.live_vs_backtest import LiveVsBacktestTracker, _WINDOW
            t = LiveVsBacktestTracker()
            for i in range(_WINDOW + 10):
                t.record(self._make_trade(pnl=100 if i % 2 == 0 else -50, realized_r=1.0 if i % 2 == 0 else -0.5))
            assert len(t._portfolio._r_window) <= _WINDOW
        finally:
            lvb_mod._DATA_FILE = orig

    def test_s26_15_thread_safe_concurrent_records(self, tmp_path):
        """Concurrent record() calls don't corrupt state."""
        import core.monitoring.live_vs_backtest as lvb_mod
        orig = lvb_mod._DATA_FILE
        lvb_mod._DATA_FILE = tmp_path / "lvb_thread.json"
        try:
            from core.monitoring.live_vs_backtest import LiveVsBacktestTracker
            t = LiveVsBacktestTracker()
            def record_many():
                for _ in range(20):
                    t.record(self._make_trade(pnl=50, realized_r=1.0))
            threads = [threading.Thread(target=record_many) for _ in range(5)]
            for th in threads: th.start()
            for th in threads: th.join()
            assert t._trade_count == 100
        finally:
            lvb_mod._DATA_FILE = orig

    def test_s26_16_singleton_returns_same_instance(self, tmp_path, monkeypatch):
        """get_live_vs_backtest_tracker() returns the same singleton."""
        import core.monitoring.live_vs_backtest as lvb_mod
        monkeypatch.setattr(lvb_mod, "_tracker", None)
        from core.monitoring.live_vs_backtest import get_live_vs_backtest_tracker
        a = get_live_vs_backtest_tracker()
        b = get_live_vs_backtest_tracker()
        assert a is b


# ─────────────────────────────────────────────────────────────────────────────
# S26-17 through S26-20 — DemoMonitorWidget helper functions
# ─────────────────────────────────────────────────────────────────────────────

class TestDemoMonitorHelpers:
    """Tests for pure helper functions — import from helpers module (no Qt needed)."""

    def test_s26_17_fmt_pct_formats_correctly(self):
        from gui.widgets.demo_monitor_helpers import fmt_pct as _fmt_pct
        assert _fmt_pct(0.503) == "50.3%"
        assert _fmt_pct(None)  == "—"
        assert _fmt_pct(0.0)   == "0.0%"

    def test_s26_18_fmt_delta_pct_shows_sign(self):
        from gui.widgets.demo_monitor_helpers import fmt_delta_pct as _fmt_delta_pct
        assert _fmt_delta_pct(0.05)  == "+5.0%"
        assert _fmt_delta_pct(-0.03) == "-3.0%"
        assert _fmt_delta_pct(None)  == "—"

    def test_s26_19_fmt_model_name_maps_known_keys(self):
        from gui.widgets.demo_monitor_helpers import fmt_model_name as _fmt_model_name
        assert _fmt_model_name("trend")             == "Trend"
        assert _fmt_model_name("momentum_breakout") == "MomBreak"
        assert _fmt_model_name("rl_ensemble")       == "RL"

    def test_s26_20_fmt_model_name_handles_unknown(self):
        from gui.widgets.demo_monitor_helpers import fmt_model_name as _fmt_model_name
        # Unknown keys should be title-cased and underscores replaced
        result = _fmt_model_name("custom_model")
        assert "Custom" in result or "custom" in result.lower()


# ─────────────────────────────────────────────────────────────────────────────
# S26-21 — demo_monitor_page module importable
# ─────────────────────────────────────────────────────────────────────────────

class TestDemoMonitorPageImport:
    def test_s26_21_helpers_module_importable_without_qt(self):
        """Pure helper module imports successfully without any Qt dependency."""
        from gui.widgets.demo_monitor_helpers import (
            fmt_pct, fmt_delta_pct, fmt_model_name,
            _MODEL_NAME_MAP,
        )
        # Verify all expected model names are in the map
        for key in ("trend", "momentum_breakout", "sentiment", "rl_ensemble"):
            assert key in _MODEL_NAME_MAP
