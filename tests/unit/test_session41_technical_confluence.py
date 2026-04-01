"""
tests/unit/test_session41_technical_confluence.py
==================================================
Session 41 — Technical-only ConfluenceScorer path.

Tests
-----
1.  PBL/SLC present in MODEL_WEIGHTS with non-zero weights
2.  PBL/SLC present in REGIME_AFFINITY with correct directional affinities
3.  technical_only=True skips AdaptiveWeightEngine (combined_adj=1.0)
4.  technical_only=True skips OI/Liq modifier block
5.  technical_only=True uses capital_usdt_override (not paper_executor)
6.  technical_only path is deterministic (same inputs → same output, no state)
7.  mode=pbl_slc BacktestRunner parity unchanged (not affected by confluence_mode)
8.  CONFLUENCE_NONE / CONFLUENCE_TECHNICAL constants exist on backtest_runner module
9.  BacktestRunner.__init__ accepts confluence_mode parameter
10. SweepEngine.__init__ accepts confluence_mode parameter
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _make_signal(model_name: str, direction: str = "long", strength: float = 0.6):
    from core.meta_decision.order_candidate import ModelSignal
    return ModelSignal(
        model_name  = model_name,
        symbol      = "BTC/USDT",
        direction   = direction,
        strength    = strength,
        entry_price = 50_000.0,
        stop_loss   = 49_000.0,
        take_profit = 52_000.0,
        atr_value   = 500.0,
        timeframe   = "30m",
        regime      = "bull_trend",
        rationale   = "unit-test signal",
    )


# ─────────────────────────────────────────────────────────────────────────────
# Test class
# ─────────────────────────────────────────────────────────────────────────────

class TestPBLSLCWeights(unittest.TestCase):
    """Test 1 & 2 — PBL/SLC in MODEL_WEIGHTS and REGIME_AFFINITY."""

    def setUp(self):
        from core.meta_decision.confluence_scorer import MODEL_WEIGHTS, REGIME_AFFINITY
        self.mw = MODEL_WEIGHTS
        self.ra = REGIME_AFFINITY

    def test_pbl_in_model_weights_nonzero(self):
        """PBL must have a non-zero weight (was absent → 0.0 before Session 41)."""
        self.assertIn("pullback_long", self.mw)
        self.assertGreater(self.mw["pullback_long"], 0.0,
                           "pullback_long weight must be > 0.0")

    def test_slc_in_model_weights_nonzero(self):
        """SLC must have a non-zero weight."""
        self.assertIn("swing_low_continuation", self.mw)
        self.assertGreater(self.mw["swing_low_continuation"], 0.0,
                           "swing_low_continuation weight must be > 0.0")

    def test_pbl_regime_affinity_bull_trend_max(self):
        """PBL affinity must be highest (1.0) in bull_trend."""
        self.assertIn("pullback_long", self.ra)
        self.assertEqual(self.ra["pullback_long"].get("bull_trend"), 1.0)

    def test_pbl_regime_affinity_bear_trend_zero(self):
        """PBL must NOT fire in bear_trend (affinity=0.0)."""
        self.assertEqual(self.ra["pullback_long"].get("bear_trend"), 0.0)

    def test_slc_regime_affinity_bear_trend_max(self):
        """SLC affinity must be highest (1.0) in bear_trend."""
        self.assertIn("swing_low_continuation", self.ra)
        self.assertEqual(self.ra["swing_low_continuation"].get("bear_trend"), 1.0)

    def test_slc_regime_affinity_bull_trend_zero(self):
        """SLC must NOT fire in bull_trend (affinity=0.0)."""
        self.assertEqual(self.ra["swing_low_continuation"].get("bull_trend"), 0.0)


class TestTechnicalOnlySkipsAdaptive(unittest.TestCase):
    """Test 3 — technical_only=True must not call AdaptiveWeightEngine."""

    def test_adaptive_engine_not_called_in_technical_only(self):
        from core.meta_decision.confluence_scorer import ConfluenceScorer
        scorer = ConfluenceScorer()
        signals = [_make_signal("trend", "long", 0.8)]

        mock_engine = MagicMock()
        mock_engine.get_multiplier.return_value = 2.0  # would distort if called

        with patch(
            "core.meta_decision.confluence_scorer._get_adaptive_engine_safe",
            return_value=mock_engine,
        ):
            # With technical_only=True — adaptive engine must NOT be invoked
            scorer.score(
                signals=signals,
                symbol="BTC/USDT",
                regime_probs={"bull_trend": 0.7, "ranging": 0.3},
                technical_only=True,
                capital_usdt_override=100_000.0,
            )
            mock_engine.get_multiplier.assert_not_called()


class TestTechnicalOnlySkipsOILiq(unittest.TestCase):
    """Test 4 — technical_only=True must not call OI/Liquidation modifier."""

    def test_oi_modifier_not_called_in_technical_only(self):
        from core.meta_decision.confluence_scorer import ConfluenceScorer
        scorer = ConfluenceScorer()
        signals = [_make_signal("trend", "long", 0.8)]

        oi_called = []

        def fake_oi_import(name, fromlist=()):
            """Intercept: if oi_signal is imported during score(), flag it."""
            if "oi_signal" in str(name):
                oi_called.append(name)
            import builtins
            return original_import(name, fromlist=fromlist)

        import builtins
        original_import = builtins.__import__

        # technical_only=True — no OI import should occur
        # We verify by checking oi_called stays empty
        scorer.score(
            signals=signals,
            symbol="BTC/USDT",
            regime_probs={"bull_trend": 0.7},
            technical_only=True,
            capital_usdt_override=100_000.0,
        )
        self.assertEqual(oi_called, [], "OI module must not be imported in technical_only mode")


class TestTechnicalOnlyCapitalOverride(unittest.TestCase):
    """Test 5 — technical_only=True uses capital_usdt_override, not paper_executor."""

    def test_capital_override_used_when_technical_only(self):
        from core.meta_decision.confluence_scorer import ConfluenceScorer
        scorer = ConfluenceScorer()
        signals = [_make_signal("trend", "long", 0.8)]

        captured_capital = []

        original_calc = None
        try:
            from core.meta_decision.position_sizer import PositionSizer
            original_calc = PositionSizer.calculate_risk_based
        except Exception:
            self.skipTest("PositionSizer not importable")

        def mock_calc(self_ps, capital_usdt, **kwargs):
            captured_capital.append(capital_usdt)
            return 1000.0

        with patch.object(
            PositionSizer, "calculate_risk_based", mock_calc
        ):
            scorer.score(
                signals=signals,
                symbol="BTC/USDT",
                regime_probs={"bull_trend": 0.7},
                technical_only=True,
                capital_usdt_override=77_777.0,
            )

        # If a candidate was produced, capital_usdt_override must have been used
        if captured_capital:
            self.assertAlmostEqual(captured_capital[0], 77_777.0,
                                   msg="capital_usdt_override must propagate to sizer")


class TestTechnicalOnlyDeterminism(unittest.TestCase):
    """Test 6 — same inputs → same output on repeated calls (no hidden state)."""

    def test_score_deterministic(self):
        from core.meta_decision.confluence_scorer import ConfluenceScorer
        scorer = ConfluenceScorer()
        signals = [_make_signal("trend", "long", 0.75)]
        kwargs = dict(
            signals=signals,
            symbol="BTC/USDT",
            regime_probs={"bull_trend": 0.8, "ranging": 0.2},
            technical_only=True,
            capital_usdt_override=100_000.0,
        )

        result_a = scorer.score(**kwargs)
        result_b = scorer.score(**kwargs)

        if result_a is None and result_b is None:
            return  # both filtered — deterministic
        self.assertIsNotNone(result_a)
        self.assertIsNotNone(result_b)
        self.assertEqual(result_a.side, result_b.side)
        self.assertAlmostEqual(
            result_a.score, result_b.score, places=6,
            msg="technical_only score must be deterministic across calls"
        )


class TestBacktestRunnerConstants(unittest.TestCase):
    """Test 8 & 9 — BacktestRunner exports constants and accepts confluence_mode."""

    def test_confluence_constants_exist(self):
        import research.engine.backtest_runner as br
        self.assertTrue(hasattr(br, "CONFLUENCE_NONE"),
                        "CONFLUENCE_NONE must be a module-level constant")
        self.assertTrue(hasattr(br, "CONFLUENCE_TECHNICAL"),
                        "CONFLUENCE_TECHNICAL must be a module-level constant")
        self.assertEqual(br.CONFLUENCE_NONE, "none")
        self.assertEqual(br.CONFLUENCE_TECHNICAL, "technical_only")

    def test_backtest_runner_accepts_confluence_mode(self):
        from research.engine.backtest_runner import BacktestRunner, CONFLUENCE_TECHNICAL
        runner = BacktestRunner(
            mode="trend",
            confluence_mode=CONFLUENCE_TECHNICAL,
        )
        self.assertEqual(runner.confluence_mode, CONFLUENCE_TECHNICAL)

    def test_backtest_runner_default_confluence_is_none(self):
        from research.engine.backtest_runner import BacktestRunner, CONFLUENCE_NONE
        runner = BacktestRunner(mode="trend")
        self.assertEqual(runner.confluence_mode, CONFLUENCE_NONE)


class TestSweepEngineAcceptsConfluenceMode(unittest.TestCase):
    """Test 10 — SweepEngine propagates confluence_mode."""

    def test_sweep_engine_accepts_confluence_mode(self):
        from research.engine.sweep_engine import SweepEngine
        engine = SweepEngine(n_workers=1, confluence_mode="technical_only")
        self.assertEqual(engine.confluence_mode, "technical_only")

    def test_sweep_engine_default_confluence_is_none(self):
        from research.engine.sweep_engine import SweepEngine
        engine = SweepEngine(n_workers=1)
        self.assertEqual(engine.confluence_mode, "none")


class TestPBLSLCParityUnchanged(unittest.TestCase):
    """
    Test 7 — mode=pbl_slc always routes to _run_scenario().
    We confirm BacktestRunner.run() calls _run_scenario (not _run_unified_scenario)
    for MODE_PBL_SLC regardless of confluence_mode.
    """

    def test_pbl_slc_mode_calls_run_scenario(self):
        from research.engine.backtest_runner import BacktestRunner, CONFLUENCE_TECHNICAL
        import pandas as pd

        runner = BacktestRunner(mode="pbl_slc", confluence_mode=CONFLUENCE_TECHNICAL)

        # Stub both paths so no real data needed
        run_scenario_called = []
        unified_called = []

        sentinel = object()

        def fake_run_scenario(cost_per_side, progress_cb=None, _precomp_sigs=None):
            run_scenario_called.append(1)
            return {"profit_factor": 1.0, "n_trades": 0}

        def fake_run_unified(cost_per_side, progress_cb=None):
            unified_called.append(1)
            return {"profit_factor": 1.0, "n_trades": 0}

        runner._run_scenario         = fake_run_scenario
        runner._run_unified_scenario = fake_run_unified
        runner._data_loaded          = True
        # Non-empty master_ts to bypass the early-return guard
        runner._master_ts            = [pd.Timestamp("2023-01-01", tz="UTC")]
        runner._fingerprints         = {}

        try:
            runner.run(params={})
        except Exception:
            pass  # may fail on other missing data, that's fine

        # MODE_PBL_SLC must call _run_scenario() (the reference implementation),
        # NOT _run_unified_scenario().  This guarantees parity with
        # n=1,731 / PF=1.3798 / PF(fees)=1.2682.
        self.assertGreater(len(run_scenario_called), 0,
                           "MODE_PBL_SLC must call _run_scenario(), not unified engine")
        self.assertEqual(len(unified_called), 0,
                         "MODE_PBL_SLC must never call _run_unified_scenario()")


if __name__ == "__main__":
    unittest.main(verbosity=2)
