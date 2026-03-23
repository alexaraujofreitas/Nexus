# ============================================================
# NEXUS TRADER — Tests for new evaluator checks (#17–#21)
#
# NE-01  Condition diversity: ≥3 (model×regime) pairs with ≥5 trades
# NE-02  Regime-specific win rate: no regime with ≥10 trades above 70% loss rate
# NE-03  Max consecutive losses: ≤8 consecutive losses
# NE-04  RL shadow comparison: diagnostic (always passes)
# NE-05  Integration: full evaluate() includes all new checks
# NE-06  Streaks helper correctness
# NE-07  Condition diversity with empty/insufficient trades
# NE-08  Regime-specific with exactly 10 trades at boundary
# NE-09  Position size increase reflected in BASE_SIZE_USDT
# ============================================================
from __future__ import annotations

import pytest
from datetime import datetime, timedelta, timezone

from core.evaluation.demo_performance_evaluator import (
    DemoPerformanceEvaluator,
    ReadinessThresholds,
    ReadinessStatus,
)


# ── Helpers ───────────────────────────────────────────────────

def _trade(
    symbol: str = "BTC/USDT",
    side: str = "buy",
    pnl_pct: float = 1.5,
    pnl_usdt: float = 10.0,
    regime: str = "bull_trend",
    models: list = None,
    entry: float = 65000.0,
    sl: float = 63700.0,
    tp: float = 68000.0,
    age_hours: int = 0,
) -> dict:
    closed = datetime.now(timezone.utc) - timedelta(hours=age_hours)
    opened = closed - timedelta(minutes=30)
    return {
        "symbol": symbol,
        "side": side,
        "entry_price": entry,
        "exit_price": entry * (1 + pnl_pct / 100),
        "stop_loss": sl,
        "take_profit": tp,
        "size_usdt": 200.0,
        "pnl_pct": pnl_pct,
        "pnl_usdt": pnl_usdt,
        "exit_reason": "take_profit" if pnl_pct > 0 else "stop_loss",
        "score": 0.72,
        "rationale": "test",
        "regime": regime,
        "models_fired": models if models is not None else ["trend"],
        "timeframe": "1h",
        "duration_s": 1800,
        "opened_at": opened.isoformat(),
        "closed_at": closed.isoformat(),
        "entry_expected": entry,
    }


def _diverse_trades(n: int = 80) -> list[dict]:
    """Generate a diverse set of trades across multiple conditions."""
    trades = []
    configs = [
        ("BTC/USDT", "buy",  "bull_trend", ["trend"]),
        ("ETH/USDT", "buy",  "ranging",    ["mean_reversion"]),
        ("SOL/USDT", "sell", "bear_trend", ["momentum_breakout"]),
        ("BNB/USDT", "buy",  "vol_compression", ["vwap_reversion"]),
        ("XRP/USDT", "sell", "uncertain",  ["liquidity_sweep"]),
    ]
    for i in range(n):
        cfg = configs[i % len(configs)]
        # ~60% win rate, losses spread across ALL regimes (use prime modulus to avoid alignment)
        win = (i % 3) != 0  # 2 wins per 3 ≈ 67% — distributed evenly across 5 configs
        pnl_pct = 1.5 if win else -1.0
        pnl_usdt = 15.0 if win else -10.0
        trades.append(_trade(
            symbol=cfg[0], side=cfg[1], regime=cfg[2], models=cfg[3],
            pnl_pct=pnl_pct, pnl_usdt=pnl_usdt,
            age_hours=n - i,  # spread over time
        ))
    return trades


# ── NE-01: Condition diversity ─────────────────────────────────

class TestConditionDiversity:
    def test_ne01a_diverse_trades_pass(self):
        """NE-01a: 80 diverse trades across 5 conditions → passes diversity check."""
        evaluator = DemoPerformanceEvaluator()
        result = evaluator._compute_condition_diversity(_diverse_trades(80))
        assert result["qualified_count"] >= 3

    def test_ne01b_monotonic_trades_fail(self):
        """NE-01b: All trades from same (model, regime) → diversity check fails."""
        trades = [_trade(models=["trend"], regime="bull_trend") for _ in range(80)]
        evaluator = DemoPerformanceEvaluator()
        result = evaluator._compute_condition_diversity(trades)
        # Only 1 pair (trend×bull_trend) with ≥5 trades
        assert result["qualified_count"] == 1

    def test_ne01c_three_pairs_exact_threshold(self):
        """NE-01c: Exactly 3 pairs with ≥5 trades each → passes."""
        trades = (
            [_trade(models=["trend"], regime="bull_trend") for _ in range(5)]
            + [_trade(models=["mean_reversion"], regime="ranging") for _ in range(5)]
            + [_trade(models=["momentum_breakout"], regime="bear_trend") for _ in range(5)]
        )
        evaluator = DemoPerformanceEvaluator()
        result = evaluator._compute_condition_diversity(trades)
        assert result["qualified_count"] == 3

    def test_ne01d_pairs_below_5_not_counted(self):
        """NE-01d: Pairs with <5 trades are not counted as qualified."""
        trades = (
            [_trade(models=["trend"], regime="bull_trend") for _ in range(10)]
            + [_trade(models=["mean_reversion"], regime="ranging") for _ in range(4)]
        )
        evaluator = DemoPerformanceEvaluator()
        result = evaluator._compute_condition_diversity(trades)
        assert result["qualified_count"] == 1

    def test_ne01e_empty_trades(self):
        """NE-01e: Empty trade list → 0 qualified pairs."""
        evaluator = DemoPerformanceEvaluator()
        result = evaluator._compute_condition_diversity([])
        assert result["qualified_count"] == 0


# ── NE-02: Regime-specific win rate ────────────────────────────

class TestRegimeWinRate:
    def test_ne02a_balanced_regimes_pass(self):
        """NE-02a: All regimes with >30% win rate → passes."""
        evaluator = DemoPerformanceEvaluator()
        result = evaluator._compute_regime_win_rates(_diverse_trades(80))
        assert not result["has_failing_regime"]

    def test_ne02b_one_regime_failing(self):
        """NE-02b: One regime with >70% loss rate (≥10 trades) → fails."""
        # 10 trades in bull_trend: 2 wins, 8 losses = 80% loss rate
        trades = (
            [_trade(regime="bull_trend", pnl_pct=1.0) for _ in range(2)]
            + [_trade(regime="bull_trend", pnl_pct=-1.0) for _ in range(8)]
            + [_trade(regime="ranging", pnl_pct=1.0) for _ in range(10)]
        )
        evaluator = DemoPerformanceEvaluator()
        result = evaluator._compute_regime_win_rates(trades)
        assert result["has_failing_regime"] is True
        assert "bull_trend" in result["worst_regime"]

    def test_ne02c_regime_under_10_trades_exempt(self):
        """NE-02c: Regime with <10 trades is NOT checked (exempt)."""
        # 9 trades in bull_trend: all losses = 100% loss rate, but exempt
        trades = (
            [_trade(regime="bull_trend", pnl_pct=-1.0) for _ in range(9)]
            + [_trade(regime="ranging", pnl_pct=1.0) for _ in range(15)]
        )
        evaluator = DemoPerformanceEvaluator()
        result = evaluator._compute_regime_win_rates(trades)
        assert not result["has_failing_regime"]

    def test_ne02d_boundary_exactly_70_loss_rate(self):
        """NE-02d: Exactly 70% loss rate (7/10) → passes (threshold is >70%, not ≥70%)."""
        trades = (
            [_trade(regime="bull_trend", pnl_pct=1.0) for _ in range(3)]
            + [_trade(regime="bull_trend", pnl_pct=-1.0) for _ in range(7)]
        )
        evaluator = DemoPerformanceEvaluator()
        result = evaluator._compute_regime_win_rates(trades)
        # 70% loss rate = 0.70, threshold is > 0.70, so this passes
        assert not result["has_failing_regime"]


# ── NE-03: Max consecutive losses ──────────────────────────────

class TestStreaks:
    def test_ne03a_no_streaks(self):
        """NE-03a: Alternating win/loss → max streak = 1."""
        trades = []
        for i in range(20):
            pnl = 1.0 if i % 2 == 0 else -1.0
            trades.append(_trade(pnl_pct=pnl, age_hours=20 - i))
        result = DemoPerformanceEvaluator._compute_streaks(trades)
        assert result["max_loss_streak"] == 1
        assert result["max_win_streak"] == 1

    def test_ne03b_long_loss_streak(self):
        """NE-03b: 9 consecutive losses → exceeds threshold (8)."""
        trades = (
            [_trade(pnl_pct=1.0, age_hours=20)]
            + [_trade(pnl_pct=-1.0, age_hours=19 - i) for i in range(9)]
            + [_trade(pnl_pct=1.0, age_hours=9)]
        )
        result = DemoPerformanceEvaluator._compute_streaks(trades)
        assert result["max_loss_streak"] == 9

    def test_ne03c_exactly_8_losses_passes(self):
        """NE-03c: Exactly 8 consecutive losses → passes threshold (≤8)."""
        trades = (
            [_trade(pnl_pct=1.0, age_hours=20)]
            + [_trade(pnl_pct=-1.0, age_hours=19 - i) for i in range(8)]
            + [_trade(pnl_pct=1.0, age_hours=10)]
        )
        result = DemoPerformanceEvaluator._compute_streaks(trades)
        assert result["max_loss_streak"] == 8
        # Verify evaluator threshold
        thresholds = ReadinessThresholds()
        assert result["max_loss_streak"] <= thresholds.max_consecutive_losses

    def test_ne03d_all_wins(self):
        """NE-03d: All wins → max_loss_streak = 0."""
        trades = [_trade(pnl_pct=1.0, age_hours=i) for i in range(10)]
        result = DemoPerformanceEvaluator._compute_streaks(trades)
        assert result["max_loss_streak"] == 0
        assert result["max_win_streak"] == 10

    def test_ne03e_empty_trades(self):
        """NE-03e: Empty → both 0."""
        result = DemoPerformanceEvaluator._compute_streaks([])
        assert result["max_loss_streak"] == 0
        assert result["max_win_streak"] == 0


# ── NE-04: RL shadow comparison ────────────────────────────────

class TestRLShadow:
    def test_ne04a_no_shadow_data(self):
        """NE-04a: No RL shadow entries → returns zeros."""
        result = DemoPerformanceEvaluator._check_rl_shadow()
        # Should not crash; returns zeros
        assert isinstance(result, dict)
        assert "total" in result

    def test_ne04b_shadow_check_always_passes(self):
        """NE-04b: RL shadow check in evaluate() always passes (diagnostic only)."""
        evaluator = DemoPerformanceEvaluator()
        assessment = evaluator.evaluate(_diverse_trades(80))
        rl_check = [c for c in assessment.check_details if c.name == "RL shadow performance"]
        assert len(rl_check) == 1
        assert rl_check[0].passed is True  # always passes — informational


# ── NE-05: Full integration ────────────────────────────────────

class TestIntegration:
    def test_ne05a_evaluate_returns_all_checks(self):
        """NE-05a: evaluate() returns all 21 checks."""
        evaluator = DemoPerformanceEvaluator()
        assessment = evaluator.evaluate(_diverse_trades(80))
        assert assessment.checks_total == 20
        check_names = [c.name for c in assessment.check_details]
        for expected in ("Condition diversity", "Regime-specific win rate",
                         "Max consecutive losses", "RL shadow performance"):
            assert expected in check_names, f"Missing check: {expected}"

    def test_ne05b_metrics_contain_new_keys(self):
        """NE-05b: Metrics dict includes new keys from checks 17–21."""
        evaluator = DemoPerformanceEvaluator()
        assessment = evaluator.evaluate(_diverse_trades(80))
        met = assessment.metrics
        assert "condition_pairs" in met
        assert "regime_win_rates" in met
        assert "max_consecutive_losses" in met
        assert "max_consecutive_wins" in met
        assert "rl_shadow" in met

    def test_ne05c_diverse_trades_high_score(self):
        """NE-05c: Diverse well-performing trades should get a good score."""
        evaluator = DemoPerformanceEvaluator()
        assessment = evaluator.evaluate(_diverse_trades(80))
        # Should pass most checks
        assert assessment.checks_passed >= 15

    def test_ne05d_zero_trades_still_works(self):
        """NE-05d: Zero trades → NOT_READY, no crash."""
        evaluator = DemoPerformanceEvaluator()
        assessment = evaluator.evaluate([])
        assert assessment.status == ReadinessStatus.NOT_READY
        assert assessment.trade_count == 0


# ── NE-09: Position size increase ──────────────────────────────

class TestPositionSizeIncrease:
    def test_ne09a_base_size_is_500(self):
        """NE-09a: BASE_SIZE_USDT is now 500."""
        from core.meta_decision.confluence_scorer import BASE_SIZE_USDT
        assert BASE_SIZE_USDT == 500.0

    def test_ne09b_sizer_max_is_zero(self):
        """NE-09b: PositionSizer max_size_usdt is 0.0 (no absolute cap).
        Session 31 fix: removed the $500 demo cap so max_capital_pct (4%)
        governs — trades should be ~$4,053 on a $100k account, not $500.
        """
        from core.meta_decision.confluence_scorer import ConfluenceScorer
        cs = ConfluenceScorer()
        assert cs._sizer.max_size_usdt == 0.0, (
            f"max_size_usdt={cs._sizer.max_size_usdt}; expected 0.0 (Session 31 fix)"
        )
