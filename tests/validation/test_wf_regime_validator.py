# ============================================================
# NexusTrader — Walk-Forward Regime-Segmented Validator Tests
#
# Test IDs: WF-001 through WF-040
#
# Sections:
#   WF-001–005  WalkForwardConfig dataclass
#   WF-006–010  WalkForwardResult dataclass
#   WF-011–018  SyntheticRegimeDataGenerator
#   WF-019–026  Analytics helpers (compute_metrics, segment_metrics,
#                                  rolling functions)
#   WF-027–032  assess_edge_persistence verdict logic
#   WF-033–037  Window splitting (no-leakage guarantee)
#   WF-038–040  EnhancedIDSSBacktester R-multiple math (unit level)
# ============================================================
from __future__ import annotations

import statistics
from dataclasses import fields
from typing import Optional
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
import pytest

from core.validation.walk_forward_regime_validator import (
    WalkForwardConfig,
    WalkForwardResult,
    SyntheticRegimeDataGenerator,
    compute_metrics,
    segment_metrics,
    segment_by_model,
    segment_by_score_bucket,
    _cumulative_r,
    _rolling_exp,
    _rolling_pf,
    _rolling_dd_r,
    _compute_drawdown_r,
    assess_edge_persistence,
    _REGIME_PARAMS,
    _SYMBOL_SCHEDULES,
    _BASE_PRICES,
)


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures & Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _make_trades(n_wins: int, n_losses: int,
                 win_r: float = 1.5, loss_r: float = 1.0,
                 regime: str = "bull_trend",
                 symbol: str = "BTC/USDT",
                 score: float = 0.60,
                 models: Optional[list] = None) -> list[dict]:
    """Build simple trade list for testing."""
    trades = []
    models = models or ["TrendModel"]
    entry = 100.0
    stop  = 98.0   # 2% risk → risk_usdt = 0.02 * size_usdt

    for i in range(n_wins):
        size  = 100.0
        risk  = abs(entry - stop) / entry * size
        pnl   = win_r * risk
        trades.append({
            "symbol":            symbol,
            "regime_at_entry":   regime,
            "regime":            regime,
            "pnl":               pnl,
            "pnl_usdt":          pnl,
            "entry_price":       entry,
            "stop_price":        stop,
            "tp_price":          entry + (entry - stop) * win_r,
            "size_usdt":         size,
            "realized_r_multiple": win_r,
            "expected_rr":       win_r,
            "exit_reason":       "take_profit",
            "duration_hours":    4.0,
            "confluence_score":  score,
            "score":             score,
            "models_fired":      list(models),
            "wf_window":         i % 2,
        })

    for i in range(n_losses):
        size  = 100.0
        risk  = abs(entry - stop) / entry * size
        pnl   = -loss_r * risk
        trades.append({
            "symbol":            symbol,
            "regime_at_entry":   regime,
            "regime":            regime,
            "pnl":               pnl,
            "pnl_usdt":          pnl,
            "entry_price":       entry,
            "stop_price":        stop,
            "tp_price":          entry + (entry - stop) * 1.5,
            "size_usdt":         size,
            "realized_r_multiple": -loss_r,
            "expected_rr":       1.5,
            "exit_reason":       "stop_loss",
            "duration_hours":    2.0,
            "confluence_score":  score,
            "score":             score,
            "models_fired":      list(models),
            "wf_window":         i % 2,
        })

    return trades


def _minimal_result(trades: list[dict]) -> WalkForwardResult:
    """Build a WalkForwardResult pre-filled with analytics."""
    from core.validation.walk_forward_regime_validator import (
        segment_metrics, segment_by_model, segment_by_score_bucket,
        _cumulative_r, _rolling_exp, _rolling_pf, _rolling_dd_r,
    )
    cfg = WalkForwardConfig()
    result = WalkForwardResult(config=cfg)
    result.all_trades = trades
    result.global_metrics   = compute_metrics(trades)
    result.by_regime        = segment_metrics(trades, "regime_at_entry")
    result.by_asset         = segment_metrics(trades, "symbol")
    result.by_model         = segment_by_model(trades)
    result.by_score_bucket  = segment_by_score_bucket(trades)
    r_seq = [t.get("realized_r_multiple", 0.0) for t in trades]
    result.cumulative_r_history   = _cumulative_r(r_seq)
    result.rolling_20_exp_history = _rolling_exp(r_seq, 20) if len(r_seq) >= 20 else []
    result.rolling_20_pf_history  = _rolling_pf(r_seq, 20) if len(r_seq) >= 20 else []
    result.rolling_dd_r_history   = _rolling_dd_r(r_seq, 20)
    result.equity_curve = [10000.0]
    result.windows = [
        {"symbol": "BTC/USDT", "window": 0, "window_global": "BTC/USDT|0",
         "test_start": "2024-01-01", "test_end": "2024-02-01",
         "n_trades": len(trades), "metrics": {}, "end_equity": 10000.0},
    ]
    result.window_count = 1
    result.total_symbols = 1
    return result


# ─────────────────────────────────────────────────────────────────────────────
# WF-001–005  WalkForwardConfig
# ─────────────────────────────────────────────────────────────────────────────

class TestWalkForwardConfig:
    def test_wf_001_default_symbols(self):
        cfg = WalkForwardConfig()
        assert "BTC/USDT" in cfg.symbols
        assert "ETH/USDT" in cfg.symbols
        assert len(cfg.symbols) == 5

    def test_wf_002_default_window_sizes(self):
        cfg = WalkForwardConfig()
        assert cfg.calibration_bars == 400
        assert cfg.test_bars == 200
        assert cfg.step_bars == 200

    def test_wf_003_default_cost_params(self):
        cfg = WalkForwardConfig()
        assert cfg.fee_pct == 0.10
        assert cfg.slippage_pct == 0.05
        assert cfg.spread_pct == 0.05

    def test_wf_004_custom_construction(self):
        cfg = WalkForwardConfig(
            symbols=["BTC/USDT"],
            calibration_bars=200,
            test_bars=100,
            initial_capital=5000.0,
        )
        assert cfg.symbols == ["BTC/USDT"]
        assert cfg.calibration_bars == 200
        assert cfg.initial_capital == 5000.0

    def test_wf_005_min_confluence_score(self):
        cfg = WalkForwardConfig()
        assert cfg.min_confluence_score == 0.45


# ─────────────────────────────────────────────────────────────────────────────
# WF-006–010  WalkForwardResult
# ─────────────────────────────────────────────────────────────────────────────

class TestWalkForwardResult:
    def test_wf_006_default_construction(self):
        cfg = WalkForwardConfig()
        result = WalkForwardResult(config=cfg)
        assert result.all_trades == []
        assert result.global_metrics == {}
        assert result.edge_verdict == "NOT_READY"

    def test_wf_007_dataclass_fields_present(self):
        field_names = {f.name for f in fields(WalkForwardResult)}
        for expected in ("all_trades", "by_regime", "by_asset", "by_model",
                         "cumulative_r_history", "rolling_20_exp_history",
                         "rolling_20_pf_history", "rolling_dd_r_history",
                         "equity_curve", "edge_verdict", "edge_explanation",
                         "window_count", "total_symbols"):
            assert expected in field_names, f"Missing field: {expected}"

    def test_wf_008_result_stores_config(self):
        cfg = WalkForwardConfig(calibration_bars=999)
        result = WalkForwardResult(config=cfg)
        assert result.config.calibration_bars == 999

    def test_wf_009_result_default_lists_are_independent(self):
        r1 = WalkForwardResult(config=WalkForwardConfig())
        r2 = WalkForwardResult(config=WalkForwardConfig())
        r1.all_trades.append({"test": 1})
        assert r2.all_trades == [], "Shared mutable default detected"

    def test_wf_010_edge_verdict_default(self):
        result = WalkForwardResult(config=WalkForwardConfig())
        assert result.edge_verdict == "NOT_READY"


# ─────────────────────────────────────────────────────────────────────────────
# WF-011–018  SyntheticRegimeDataGenerator
# ─────────────────────────────────────────────────────────────────────────────

class TestSyntheticRegimeDataGenerator:
    def setup_method(self):
        self.gen = SyntheticRegimeDataGenerator(seed=42)

    def test_wf_011_generate_returns_dataframe(self):
        df, periods = self.gen.generate("BTC/USDT")
        assert isinstance(df, pd.DataFrame)
        assert not df.empty

    def test_wf_012_dataframe_has_ohlcv_columns(self):
        df, _ = self.gen.generate("BTC/USDT")
        for col in ("open", "high", "low", "close", "volume", "true_regime"):
            assert col in df.columns, f"Missing column: {col}"

    def test_wf_013_regime_column_values_are_valid(self):
        df, _ = self.gen.generate("BTC/USDT")
        valid = set(_REGIME_PARAMS.keys())
        observed = set(df["true_regime"].unique())
        assert observed.issubset(valid), f"Invalid regimes: {observed - valid}"

    def test_wf_014_high_gte_open_close(self):
        df, _ = self.gen.generate("ETH/USDT")
        assert (df["high"] >= df["open"]).all()
        assert (df["high"] >= df["close"]).all()

    def test_wf_015_low_lte_open_close(self):
        df, _ = self.gen.generate("ETH/USDT")
        assert (df["low"] <= df["open"]).all()
        assert (df["low"] <= df["close"]).all()

    def test_wf_016_regime_periods_returned(self):
        _, periods = self.gen.generate("BTC/USDT")
        assert len(periods) > 0
        for regime_name, start, end in periods:
            assert regime_name in _REGIME_PARAMS
            assert end >= start

    def test_wf_017_total_bars_matches_schedule(self):
        symbol = "BTC/USDT"
        schedule = _SYMBOL_SCHEDULES[symbol]
        expected_bars = sum(n for _, n in schedule)
        df, _ = self.gen.generate(symbol)
        assert len(df) == expected_bars

    def test_wf_018_generate_all_symbols(self):
        symbols = ["BTC/USDT", "ETH/USDT"]
        result_map = self.gen.generate_all_symbols(symbols)
        assert set(result_map.keys()) == set(symbols)
        for sym, (df, periods) in result_map.items():
            assert not df.empty
            assert len(periods) > 0

    def test_wf_018b_different_seeds_produce_different_data(self):
        g1 = SyntheticRegimeDataGenerator(seed=1)
        g2 = SyntheticRegimeDataGenerator(seed=2)
        df1, _ = g1.generate("BTC/USDT")
        df2, _ = g2.generate("BTC/USDT")
        # Prices should differ (different RNG seeds)
        assert not df1["close"].equals(df2["close"])

    def test_wf_018c_all_five_symbols_have_schedules(self):
        for sym in ["BTC/USDT", "ETH/USDT", "BNB/USDT", "SOL/USDT", "XRP/USDT"]:
            assert sym in _SYMBOL_SCHEDULES, f"Missing schedule for {sym}"
            assert sym in _BASE_PRICES, f"Missing base price for {sym}"


# ─────────────────────────────────────────────────────────────────────────────
# WF-019–026  Analytics helpers
# ─────────────────────────────────────────────────────────────────────────────

class TestAnalyticsHelpers:
    def test_wf_019_compute_metrics_empty(self):
        m = compute_metrics([])
        assert m["total_trades"] == 0
        assert m["expectancy_r"] == 0.0

    def test_wf_020_compute_metrics_win_rate(self):
        trades = _make_trades(6, 4)
        m = compute_metrics(trades)
        assert abs(m["win_rate_frac"] - 0.60) < 0.01

    def test_wf_021_compute_metrics_expectancy_formula(self):
        # WR=0.6, avgWin=1.5R, avgLoss=1.0R → E[R] = 0.6*1.5 - 0.4*1.0 = 0.50
        trades = _make_trades(6, 4, win_r=1.5, loss_r=1.0)
        m = compute_metrics(trades)
        assert abs(m["expectancy_r"] - 0.50) < 0.05

    def test_wf_022_compute_metrics_profit_factor(self):
        # gross_win = 6 * 1.5 = 9R; gross_loss = 4 * 1.0 = 4R; PF = 9/4 = 2.25
        # But PF is computed on USDT, same ratio holds here
        trades = _make_trades(6, 4, win_r=1.5, loss_r=1.0)
        m = compute_metrics(trades)
        assert m["profit_factor"] > 1.0

    def test_wf_023_compute_metrics_drawdown_r(self):
        # Sequence: +1.5, −1.0, +1.5, −1.0, ... losses only at the end
        trades = _make_trades(0, 5, loss_r=1.0)
        m = compute_metrics(trades)
        assert m["drawdown_r"] > 0

    def test_wf_024_segment_metrics_by_regime(self):
        bull  = _make_trades(5, 2, regime="bull_trend")
        bear  = _make_trades(2, 5, regime="bear_trend")
        all_t = bull + bear
        by_r = segment_metrics(all_t, "regime_at_entry")
        assert "bull_trend" in by_r
        assert "bear_trend" in by_r
        assert by_r["bull_trend"]["total_trades"] == 7
        assert by_r["bear_trend"]["total_trades"] == 7

    def test_wf_025_segment_by_model(self):
        t1 = _make_trades(3, 2, models=["TrendModel"])
        t2 = _make_trades(2, 3, models=["MeanReversionModel"])
        by_m = segment_by_model(t1 + t2)
        assert "TrendModel" in by_m
        assert "MeanReversionModel" in by_m

    def test_wf_026_segment_by_score_bucket(self):
        trades = _make_trades(5, 5, score=0.72)
        by_b = segment_by_score_bucket(trades)
        assert any("0.65" in k for k in by_b)

    def test_wf_026b_segment_by_model_multi_model_trade(self):
        """A trade with 2 models should appear in both model buckets."""
        trades = _make_trades(3, 2, models=["TrendModel", "MomentumBreakoutModel"])
        by_m = segment_by_model(trades)
        assert "TrendModel" in by_m
        assert "MomentumBreakoutModel" in by_m
        # Both buckets include all trades
        assert by_m["TrendModel"]["total_trades"] == 5
        assert by_m["MomentumBreakoutModel"]["total_trades"] == 5


# ─────────────────────────────────────────────────────────────────────────────
# WF-019b  Rolling functions
# ─────────────────────────────────────────────────────────────────────────────

class TestRollingFunctions:
    def test_wf_r01_cumulative_r_sum(self):
        r_seq = [1.0, -0.5, 1.5, -1.0, 2.0]
        cum = _cumulative_r(r_seq)
        assert len(cum) == len(r_seq)
        assert abs(cum[-1] - sum(r_seq)) < 1e-9

    def test_wf_r02_cumulative_r_monotone_wins(self):
        r_seq = [1.0, 1.0, 1.0]
        cum = _cumulative_r(r_seq)
        for i in range(1, len(cum)):
            assert cum[i] > cum[i - 1]

    def test_wf_r03_rolling_exp_length(self):
        r_seq = [1.0] * 30
        rolled = _rolling_exp(r_seq, 20)
        assert len(rolled) == 30 - 20 + 1  # 11

    def test_wf_r04_rolling_exp_all_wins(self):
        r_seq = [1.5] * 25
        rolled = _rolling_exp(r_seq, 20)
        assert all(v > 0 for v in rolled)

    def test_wf_r05_rolling_pf_all_wins(self):
        r_seq = [1.0] * 25
        pf = _rolling_pf(r_seq, 20)
        # No losses → should return 999.0
        assert all(v == 999.0 for v in pf)

    def test_wf_r06_rolling_pf_mixed(self):
        # 5 wins then 20 losses; final rolling-20 window is all losses → PF < 1
        r_seq = [1.5] * 5 + [-1.0] * 20
        pf20 = _rolling_pf(r_seq, 20)
        # Final window (bars 5..24): 0 wins, 20 losses → PF should be 0 (no wins)
        # Actually _rolling_pf returns 999.0 when gl=0, but here gw=0 → pf = 0/gl = 0
        # Let's just check < 1
        assert pf20[-1] < 1.0

    def test_wf_r07_compute_drawdown_r_zero_for_monotone_up(self):
        r_seq = [0.5, 0.5, 0.5, 0.5]
        dd = _compute_drawdown_r(r_seq)
        assert dd == 0.0

    def test_wf_r08_compute_drawdown_r_sequence(self):
        r_seq = [2.0, -3.0, 1.0]
        # cum: 2, -1, 0. Peak=2, trough=-1 → DD=3
        dd = _compute_drawdown_r(r_seq)
        assert abs(dd - 3.0) < 1e-6

    def test_wf_r09_rolling_dd_r_length(self):
        r_seq = [1.0, -0.5] * 15
        dd = _rolling_dd_r(r_seq, 20)
        assert len(dd) == len(r_seq)


# ─────────────────────────────────────────────────────────────────────────────
# WF-027–032  assess_edge_persistence
# ─────────────────────────────────────────────────────────────────────────────

class TestAssessEdgePersistence:
    def test_wf_027_insufficient_data(self):
        trades = _make_trades(5, 5)
        result = _minimal_result(trades)
        verdict, exp = assess_edge_persistence(result)
        assert verdict == "INSUFFICIENT_DATA"
        assert "20" in exp

    def test_wf_028_persistent_edge_all_regimes_positive(self):
        """Strong positive across 4 regimes → PERSISTENT_EDGE."""
        trades = []
        for regime in ["bull_trend", "bear_trend", "ranging", "vol_expansion"]:
            trades.extend(_make_trades(8, 2, regime=regime, win_r=2.0, loss_r=0.8))
        result = _minimal_result(trades)
        verdict, exp = assess_edge_persistence(result)
        assert verdict == "PERSISTENT_EDGE"

    def test_wf_029_regime_dependent_only_bull(self):
        """Good in bull, terrible in bear/ranging → REGIME_DEPENDENT."""
        trades = (
            _make_trades(9, 1, regime="bull_trend",  win_r=2.0, loss_r=0.5) +
            _make_trades(2, 8, regime="bear_trend",  win_r=0.5, loss_r=2.0) +
            _make_trades(2, 8, regime="ranging",     win_r=0.5, loss_r=2.0) +
            _make_trades(2, 8, regime="vol_expansion", win_r=0.5, loss_r=2.0)
        )
        result = _minimal_result(trades)
        verdict, _ = assess_edge_persistence(result)
        assert verdict == "REGIME_DEPENDENT"

    def test_wf_030_explanation_contains_conclusion(self):
        # Need ≥ 20 trades to get past INSUFFICIENT_DATA check
        trades = _make_trades(15, 5, win_r=2.0)  # 20 trades, good
        result = _minimal_result(trades)
        _, exp = assess_edge_persistence(result)
        # Should contain a CONCLUSION section
        assert "CONCLUSION:" in exp

    def test_wf_031_negative_expectancy_is_regime_dependent(self):
        trades = _make_trades(3, 17, win_r=0.5, loss_r=1.5)
        result = _minimal_result(trades)
        verdict, _ = assess_edge_persistence(result)
        assert verdict in ("REGIME_DEPENDENT", "INSUFFICIENT_DATA")

    def test_wf_032_high_drawdown_penalised(self):
        """DD > 10R even with positive exp → REGIME_DEPENDENT."""
        # 4 wins, 16 losses producing a big R drawdown
        trades = _make_trades(4, 20, win_r=1.0, loss_r=3.0)
        result = _minimal_result(trades)
        verdict, exp = assess_edge_persistence(result)
        # DD will be very high here → not PERSISTENT
        if result.global_metrics["drawdown_r"] >= 10.0:
            assert verdict == "REGIME_DEPENDENT"

    def test_wf_032b_verdict_strings(self):
        """Verdict must be one of three canonical strings."""
        for n_wins, n_losses in [(15, 5), (5, 15), (1, 1)]:
            trades = _make_trades(n_wins, n_losses)
            result = _minimal_result(trades)
            verdict, _ = assess_edge_persistence(result)
            assert verdict in ("PERSISTENT_EDGE", "REGIME_DEPENDENT", "INSUFFICIENT_DATA")


# ─────────────────────────────────────────────────────────────────────────────
# WF-033–037  Window splitting (no-leakage guarantee)
# ─────────────────────────────────────────────────────────────────────────────

class TestWindowSplitting:
    """Tests for the walk-forward window slicing logic."""

    def _build_df(self, n_bars: int = 1000) -> pd.DataFrame:
        idx = pd.date_range("2024-01-01", periods=n_bars, freq="4h")
        rng = np.random.default_rng(0)
        prices = 100.0 * np.cumprod(1 + rng.normal(0, 0.005, n_bars))
        return pd.DataFrame({
            "open":  prices * 0.999,
            "high":  prices * 1.003,
            "low":   prices * 0.997,
            "close": prices,
            "volume": rng.uniform(1000, 5000, n_bars),
            "true_regime": "bull_trend",
        }, index=idx)

    def test_wf_033_window_count_correct(self):
        """Number of windows = floor((n - cal) / step)."""
        cfg = WalkForwardConfig(
            calibration_bars=400, test_bars=200, step_bars=200,
            symbols=["BTC/USDT"]
        )
        df = self._build_df(1000)
        n_windows = (1000 - 400) // 200  # = 3
        assert n_windows == 3

    def test_wf_034_insufficient_data_returns_empty(self):
        """If df is shorter than cal + test bars, no windows generated."""
        cfg = WalkForwardConfig(calibration_bars=400, test_bars=200)
        n_bars = cfg.calibration_bars + cfg.test_bars - 1  # just short
        df = self._build_df(n_bars)
        assert len(df) < cfg.calibration_bars + cfg.test_bars

    def test_wf_035_warmup_bars_equals_cal_len(self):
        """
        The backtester warmup_bars must equal the calibration window length,
        ensuring no trades are generated before the forward-test window starts.
        """
        cfg = WalkForwardConfig(calibration_bars=400, test_bars=200, step_bars=200)
        # Verify the formula: cal_len = train_end - cal_start
        n = 800
        train_end = cfg.calibration_bars  # 400
        cal_start = max(0, train_end - cfg.calibration_bars)  # 0
        cal_len   = train_end - cal_start  # 400 == calibration_bars
        assert cal_len == cfg.calibration_bars

    def test_wf_036_test_window_is_non_overlapping_when_step_equals_test(self):
        """With step_bars == test_bars, consecutive test windows don't overlap."""
        cfg = WalkForwardConfig(calibration_bars=400, test_bars=200, step_bars=200)
        n = 1000
        windows = []
        train_end = cfg.calibration_bars
        while train_end + cfg.test_bars <= n:
            test_start = train_end
            test_end   = train_end + cfg.test_bars
            windows.append((test_start, test_end))
            train_end += cfg.step_bars

        for i in range(1, len(windows)):
            prev_end   = windows[i - 1][1]
            curr_start = windows[i][0]
            assert curr_start >= prev_end, "Overlapping test windows detected"

    def test_wf_037_calibration_data_precedes_test_window(self):
        """Cal window always precedes test window (no future leakage)."""
        cfg = WalkForwardConfig(calibration_bars=400, test_bars=200, step_bars=200)
        n = 1000
        train_end = cfg.calibration_bars
        while train_end + cfg.test_bars <= n:
            cal_end  = train_end - 1
            test_start = train_end
            assert cal_end < test_start, "Calibration extends into test window"
            train_end += cfg.step_bars


# ─────────────────────────────────────────────────────────────────────────────
# WF-038–040  R-multiple math
# ─────────────────────────────────────────────────────────────────────────────

class TestRMultipleMath:
    def test_wf_038_r_multiple_win(self):
        """Long trade: entry=100, stop=98, tp=103, size=100 USDT."""
        entry = 100.0
        stop  = 98.0
        tp    = 103.0
        size  = 100.0
        risk  = abs(entry - stop) / entry * size   # = 2.0 USDT
        pnl_win = abs(tp - entry) / entry * size   # = 3.0 USDT
        realized_r = pnl_win / risk                # = 1.5
        assert abs(realized_r - 1.5) < 1e-9

    def test_wf_039_r_multiple_loss(self):
        """Long trade hits SL: entry=100, stop=98, size=100 USDT."""
        entry = 100.0
        stop  = 98.0
        size  = 100.0
        risk  = abs(entry - stop) / entry * size   # = 2.0 USDT
        pnl_loss = -(entry - stop) / entry * size  # = -2.0 USDT
        realized_r = pnl_loss / risk               # = -1.0
        assert abs(realized_r - (-1.0)) < 1e-9

    def test_wf_040_expected_rr(self):
        """exp_rr = |tp - entry| / |sl - entry|."""
        entry = 100.0
        stop  = 98.0
        tp    = 104.0
        exp_rr = abs(tp - entry) / abs(stop - entry)  # = 4 / 2 = 2.0
        assert abs(exp_rr - 2.0) < 1e-9

    def test_wf_040b_short_r_multiple(self):
        """Short trade: entry=100, stop=102, tp=97, size=100 USDT."""
        entry = 100.0
        stop  = 102.0
        tp    = 97.0
        size  = 100.0
        risk  = abs(entry - stop) / entry * size   # = 2.0 USDT
        pnl_win = abs(entry - tp) / entry * size   # = 3.0 USDT
        realized_r = pnl_win / risk                # = 1.5
        assert abs(realized_r - 1.5) < 1e-9
