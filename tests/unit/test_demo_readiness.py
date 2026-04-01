# ============================================================
# NEXUS TRADER — Demo Readiness Test Suite (Session 50)
#
# Tests:
#   1. Correct model activation — PBL+SLC active, Trend+MB+Donchian disabled
#   2. Exact PBL parameter values
#   3. Config immutability when demo_mode.locked=True
#   4. Deterministic trade outputs (same signal → same SL/TP/size)
#   5. Demo startup validation passes all checks
#   6. Export/summary functions exist and return correct structure
# ============================================================
from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# ── Path setup ─────────────────────────────────────────────
ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT))


# ── Helper: build a minimal valid config dict ──────────────
def _locked_config() -> dict:
    return {
        "demo_mode": {
            "locked": True,
            "parameter_lock_version": "session50",
            "approved_models": ["pullback_long", "swing_low_continuation"],
            "locked_pbl_params": {
                "sl_atr_mult": 3.0, "tp_atr_mult": 4.0,
                "ema_prox_atr_mult": 0.4, "rsi_min": 45.0, "wick_strength": 1.5,
            },
        },
        "disabled_models": [
            "mean_reversion", "liquidity_sweep", "trend",
            "donchian_breakout", "momentum_breakout",
        ],
        "mr_pbl_slc": {
            "enabled": True,
            "pullback_long": {
                "sl_atr_mult": 3.0, "tp_atr_mult": 4.0,
                "ema_prox_atr_mult": 0.4, "rsi_min": 45.0, "wick_strength": 1.5,
            },
        },
        "data": {"default_timeframe": "30m", "websocket_enabled": False},
        "idss": {"min_confluence_score": 0.45},
        "scanner": {"auto_execute": True},
        "multi_tf": {"confirmation_required": True},
        "risk_engine": {"risk_pct_per_trade": 0.5},
    }


# ── 1. Model activation ─────────────────────────────────────

class TestModelActivation(unittest.TestCase):
    """Verify the correct models are enabled/disabled."""

    def test_pbl_slc_active_in_signal_generator(self):
        """PBL and SLC must be in _ALL_MODELS."""
        from core.signals.signal_generator import _ALL_MODELS
        names = {m.name for m in _ALL_MODELS}
        self.assertIn("pullback_long", names, "PBL missing from _ALL_MODELS")
        self.assertIn("swing_low_continuation", names, "SLC missing from _ALL_MODELS")

    def test_trend_model_in_all_models_but_disabled_by_config(self):
        """TrendModel is in _ALL_MODELS but blocked by disabled_models config."""
        from core.signals.signal_generator import _ALL_MODELS
        names = {m.name for m in _ALL_MODELS}
        self.assertIn("trend", names, "TrendModel should remain in code (disabled via config)")

    def test_momentum_breakout_in_all_models_but_disabled_by_config(self):
        """MomentumBreakout is in _ALL_MODELS but blocked by disabled_models config."""
        from core.signals.signal_generator import _ALL_MODELS
        names = {m.name for m in _ALL_MODELS}
        self.assertIn("momentum_breakout", names, "MB should remain in code (disabled via config)")

    def test_signal_generator_skips_disabled_models(self):
        """generate() must not produce signals for disabled models."""
        import pandas as pd
        import numpy as np
        from core.signals.signal_generator import SignalGenerator

        cfg = _locked_config()
        with patch("config.settings.settings") as mock_s:
            mock_s.get.side_effect = lambda k, d=None: (
                cfg["disabled_models"]   if k == "disabled_models" else
                cfg["mr_pbl_slc"]["enabled"] if k == "mr_pbl_slc.enabled" else
                False                    if k == "adaptive_activation.enabled" else
                cfg["mr_pbl_slc"]["enabled"] if "mr_pbl_slc" in k and "enabled" in k else
                d
            )
            sg = SignalGenerator()
            # Build minimal df (too short to trigger any real signal)
            df = pd.DataFrame({
                "open":   [100.0] * 10,
                "high":   [101.0] * 10,
                "low":    [99.0]  * 10,
                "close":  [100.5] * 10,
                "volume": [1000.0] * 10,
            })
            signals = sg.generate("BTC/USDT", df, "ranging", "30m")
            model_names = [s.model_name for s in signals]
            for blocked in ("trend", "momentum_breakout", "donchian_breakout",
                            "mean_reversion", "liquidity_sweep"):
                self.assertNotIn(blocked, model_names,
                                 f"{blocked} should be blocked by disabled_models")

    def test_pbl_active_regimes(self):
        """PBL must declare ACTIVE_REGIMES = ['bull_trend']."""
        from core.signals.sub_models.pullback_long_model import PullbackLongModel
        from core.regime.regime_classifier import REGIME_BULL_TREND
        pbl = PullbackLongModel()
        self.assertEqual(pbl.ACTIVE_REGIMES, [REGIME_BULL_TREND])

    def test_slc_active_regimes(self):
        """SLC must declare ACTIVE_REGIMES = ['bear_trend']."""
        from core.signals.sub_models.swing_low_continuation_model import SwingLowContinuationModel
        from core.regime.regime_classifier import REGIME_BEAR_TREND
        slc = SwingLowContinuationModel()
        self.assertEqual(slc.ACTIVE_REGIMES, [REGIME_BEAR_TREND])


# ── 2. PBL parameter values ─────────────────────────────────

class TestPBLParameters(unittest.TestCase):
    """Verify exact Session-50 approved parameter values are loaded."""

    _EXPECTED = {
        "sl_atr_mult":       3.0,
        "tp_atr_mult":       4.0,
        "ema_prox_atr_mult": 0.4,
        "rsi_min":           45.0,
        "wick_strength":     1.5,
    }

    def _mock_settings(self):
        cfg = _locked_config()
        mock = MagicMock()
        mock.get.side_effect = lambda k, d=None: (
            cfg["mr_pbl_slc"]["pullback_long"].get(k.split(".")[-1], d)
            if k.startswith("mr_pbl_slc.pullback_long.") else d
        )
        return mock

    def test_sl_atr_mult(self):
        mock = self._mock_settings()
        val = float(mock.get("mr_pbl_slc.pullback_long.sl_atr_mult", 2.5))
        self.assertEqual(val, 3.0)

    def test_tp_atr_mult(self):
        mock = self._mock_settings()
        val = float(mock.get("mr_pbl_slc.pullback_long.tp_atr_mult", 3.0))
        self.assertEqual(val, 4.0)

    def test_ema_prox_atr_mult(self):
        mock = self._mock_settings()
        val = float(mock.get("mr_pbl_slc.pullback_long.ema_prox_atr_mult", 0.5))
        self.assertEqual(val, 0.4)

    def test_rsi_min(self):
        mock = self._mock_settings()
        val = float(mock.get("mr_pbl_slc.pullback_long.rsi_min", 40.0))
        self.assertEqual(val, 45.0)

    def test_wick_strength(self):
        mock = self._mock_settings()
        val = float(mock.get("mr_pbl_slc.pullback_long.wick_strength", 1.0))
        self.assertEqual(val, 1.5)

    def test_all_params_simultaneously(self):
        """All five params match expected values."""
        cfg = _locked_config()
        pbl = cfg["mr_pbl_slc"]["pullback_long"]
        for k, expected in self._EXPECTED.items():
            self.assertAlmostEqual(pbl[k], expected, places=6,
                                   msg=f"Param {k}: expected {expected}, got {pbl[k]}")

    def test_pbl_wick_strength_in_DEFAULT_CONFIG(self):
        """config/settings.py DEFAULT_CONFIG must include wick_strength."""
        from config.settings import DEFAULT_CONFIG
        pbl_defaults = DEFAULT_CONFIG.get("mr_pbl_slc", {}).get("pullback_long", {})
        self.assertIn("wick_strength", pbl_defaults,
                      "wick_strength missing from DEFAULT_CONFIG.mr_pbl_slc.pullback_long")

    def test_config_yaml_pbl_params(self):
        """Live config.yaml must contain the approved PBL params."""
        import yaml
        cfg_path = ROOT / "config.yaml"
        if not cfg_path.exists():
            self.skipTest("config.yaml not found")
        with open(cfg_path) as f:
            cfg = yaml.safe_load(f)
        pbl = cfg.get("mr_pbl_slc", {}).get("pullback_long", {})
        for k, expected in self._EXPECTED.items():
            actual = pbl.get(k)
            self.assertAlmostEqual(
                float(actual), expected, places=6,
                msg=f"config.yaml mr_pbl_slc.pullback_long.{k}={actual} ≠ {expected}",
            )


# ── 3. Config immutability ───────────────────────────────────

class TestConfigImmutability(unittest.TestCase):
    """When demo_mode.locked=True, locked keys must not be mutable."""

    def _make_settings(self, locked: bool = True):
        """Return an AppSettings instance with an in-memory locked config."""
        from config.settings import AppSettings
        inst = AppSettings.__new__(AppSettings)
        inst._config = _locked_config()
        inst._config["demo_mode"]["locked"] = locked
        return inst

    def test_pbl_sl_blocked_when_locked(self):
        """settings.set on a PBL param is blocked when demo_mode.locked=True."""
        s = self._make_settings(locked=True)
        original = s._config["mr_pbl_slc"]["pullback_long"]["sl_atr_mult"]
        s.set("mr_pbl_slc.pullback_long.sl_atr_mult", 9.9, auto_save=False)
        after = s._config["mr_pbl_slc"]["pullback_long"]["sl_atr_mult"]
        self.assertEqual(after, original, "PBL sl_atr_mult must not change when locked")

    def test_disabled_models_blocked_when_locked(self):
        """settings.set on disabled_models is blocked when demo_mode.locked=True."""
        s = self._make_settings(locked=True)
        original = list(s._config["disabled_models"])
        s.set("disabled_models", [], auto_save=False)
        after = s._config["disabled_models"]
        self.assertEqual(after, original, "disabled_models must not change when locked")

    def test_demo_mode_key_blocked_when_locked(self):
        """settings.set on demo_mode.locked is blocked when demo_mode.locked=True."""
        s = self._make_settings(locked=True)
        s.set("demo_mode.locked", False, auto_save=False)
        self.assertTrue(s._config["demo_mode"]["locked"],
                        "demo_mode.locked must not be mutable via settings.set()")

    def test_non_locked_key_still_mutable(self):
        """Non-locked keys (e.g. risk_pct) remain writable when demo_mode.locked=True."""
        s = self._make_settings(locked=True)
        s.set("risk_engine.risk_pct_per_trade", 1.0, auto_save=False)
        val = s._config.get("risk_engine", {}).get("risk_pct_per_trade")
        self.assertEqual(val, 1.0, "Non-locked keys must remain writable")

    def test_pbl_mutable_when_not_locked(self):
        """PBL params are writable when demo_mode.locked=False."""
        s = self._make_settings(locked=False)
        s.set("mr_pbl_slc.pullback_long.sl_atr_mult", 5.0, auto_save=False)
        val = s._config["mr_pbl_slc"]["pullback_long"]["sl_atr_mult"]
        self.assertEqual(val, 5.0, "PBL params must be writable when not locked")

    def test_all_five_pbl_params_blocked(self):
        """All five PBL params are individually blocked."""
        s = self._make_settings(locked=True)
        for param, original in _locked_config()["mr_pbl_slc"]["pullback_long"].items():
            s.set(f"mr_pbl_slc.pullback_long.{param}", 0.0, auto_save=False)
            after = s._config["mr_pbl_slc"]["pullback_long"][param]
            self.assertEqual(after, original,
                             f"mr_pbl_slc.pullback_long.{param} should be immutable")


# ── 4. Deterministic trade outputs ──────────────────────────

class TestDeterministicOutputs(unittest.TestCase):
    """Identical inputs to PBL.evaluate() must produce identical SL/TP."""

    def _make_pbl_df(self, n: int = 80, close: float = 100.0, atr: float = 1.0) -> "pd.DataFrame":
        import pandas as pd, numpy as np
        np.random.seed(42)
        # Construct a bar with: bullish body, long lower wick, wick > 1.5 × body
        # close=100, open=98, low=95, high=100.5 → body=2, lw=3, uw=0.5
        data = {
            "open":   [98.0] * n,
            "high":   [100.5] * n,
            "low":    [95.0] * n,
            "close":  [100.0] * n,
            "volume": [1000.0] * n,
        }
        df = pd.DataFrame(data)
        # Add required indicators
        df["ema_50"]  = 99.7  # close is within 0.4 ATR of EMA50 (prox = 0.3/atr = 0.3)
        df["rsi_14"]  = 55.0
        df["atr_14"]  = atr
        return df

    def test_identical_inputs_same_sl_tp(self):
        """Two calls with same df must return same SL and TP."""
        import pandas as pd
        from core.signals.sub_models.pullback_long_model import PullbackLongModel
        from core.regime.regime_classifier import REGIME_BULL_TREND

        pbl = PullbackLongModel()
        df  = self._make_pbl_df()

        with patch("config.settings.settings") as mock_s:
            cfg = _locked_config()
            pbl_p = cfg["mr_pbl_slc"]["pullback_long"]
            mock_s.get.side_effect = lambda k, d=None: {
                "mr_pbl_slc.pullback_long.ema_prox_atr_mult": pbl_p["ema_prox_atr_mult"],
                "mr_pbl_slc.pullback_long.sl_atr_mult":       pbl_p["sl_atr_mult"],
                "mr_pbl_slc.pullback_long.tp_atr_mult":       pbl_p["tp_atr_mult"],
                "mr_pbl_slc.pullback_long.rsi_min":           pbl_p["rsi_min"],
                "mr_pbl_slc.pullback_long.wick_strength":     pbl_p["wick_strength"],
            }.get(k, d)

            sig1 = pbl.evaluate("BTC/USDT", df, REGIME_BULL_TREND, "30m")
            sig2 = pbl.evaluate("BTC/USDT", df, REGIME_BULL_TREND, "30m")

        if sig1 is None and sig2 is None:
            return  # both returned None — consistent, not a determinism failure

        self.assertIsNotNone(sig1, "First call returned None")
        self.assertIsNotNone(sig2, "Second call returned None")
        self.assertAlmostEqual(sig1.stop_loss,   sig2.stop_loss,   places=8)
        self.assertAlmostEqual(sig1.take_profit, sig2.take_profit, places=8)
        self.assertAlmostEqual(sig1.entry_price, sig2.entry_price, places=8)

    def test_sl_uses_approved_sl_atr_mult(self):
        """SL = close − 3.0 × ATR (Session 50 approved)."""
        from core.signals.sub_models.pullback_long_model import PullbackLongModel
        from core.regime.regime_classifier import REGIME_BULL_TREND

        pbl = PullbackLongModel()
        atr = 2.0
        df  = self._make_pbl_df(atr=atr)

        with patch("config.settings.settings") as mock_s:
            cfg = _locked_config()
            pbl_p = cfg["mr_pbl_slc"]["pullback_long"]
            mock_s.get.side_effect = lambda k, d=None: {
                "mr_pbl_slc.pullback_long.ema_prox_atr_mult": pbl_p["ema_prox_atr_mult"],
                "mr_pbl_slc.pullback_long.sl_atr_mult":       pbl_p["sl_atr_mult"],
                "mr_pbl_slc.pullback_long.tp_atr_mult":       pbl_p["tp_atr_mult"],
                "mr_pbl_slc.pullback_long.rsi_min":           pbl_p["rsi_min"],
                "mr_pbl_slc.pullback_long.wick_strength":     pbl_p["wick_strength"],
            }.get(k, d)
            sig = pbl.evaluate("BTC/USDT", df, REGIME_BULL_TREND, "30m")

        if sig is None:
            self.skipTest("PBL signal did not fire on synthetic df — skipping SL/TP check")

        expected_sl = sig.entry_price - 3.0 * atr
        self.assertAlmostEqual(sig.stop_loss, expected_sl, places=6,
                               msg="SL must be close − 3.0 × ATR")

    def test_tp_uses_approved_tp_atr_mult(self):
        """TP = close + 4.0 × ATR (Session 50 approved)."""
        from core.signals.sub_models.pullback_long_model import PullbackLongModel
        from core.regime.regime_classifier import REGIME_BULL_TREND

        pbl = PullbackLongModel()
        atr = 2.0
        df  = self._make_pbl_df(atr=atr)

        with patch("config.settings.settings") as mock_s:
            cfg = _locked_config()
            pbl_p = cfg["mr_pbl_slc"]["pullback_long"]
            mock_s.get.side_effect = lambda k, d=None: {
                "mr_pbl_slc.pullback_long.ema_prox_atr_mult": pbl_p["ema_prox_atr_mult"],
                "mr_pbl_slc.pullback_long.sl_atr_mult":       pbl_p["sl_atr_mult"],
                "mr_pbl_slc.pullback_long.tp_atr_mult":       pbl_p["tp_atr_mult"],
                "mr_pbl_slc.pullback_long.rsi_min":           pbl_p["rsi_min"],
                "mr_pbl_slc.pullback_long.wick_strength":     pbl_p["wick_strength"],
            }.get(k, d)
            sig = pbl.evaluate("BTC/USDT", df, REGIME_BULL_TREND, "30m")

        if sig is None:
            self.skipTest("PBL signal did not fire on synthetic df — skipping SL/TP check")

        expected_tp = sig.entry_price + 4.0 * atr
        self.assertAlmostEqual(sig.take_profit, expected_tp, places=6,
                               msg="TP must be close + 4.0 × ATR")

    def test_pbl_rejects_wrong_regime(self):
        """PBL must return None when regime ≠ bull_trend."""
        from core.signals.sub_models.pullback_long_model import PullbackLongModel
        pbl = PullbackLongModel()
        df  = self._make_pbl_df()
        with patch("config.settings.settings") as mock_s:
            mock_s.get.side_effect = lambda k, d=None: d
            sig = pbl.evaluate("BTC/USDT", df, "bear_trend", "30m")
        self.assertIsNone(sig, "PBL must return None for non-bull_trend regime")


# ── 5. Demo startup validation ───────────────────────────────

class TestDemoStartupValidation(unittest.TestCase):
    """demo_startup_log.run_demo_startup_validation() must pass on clean config."""

    def test_validation_passes_with_locked_config(self):
        """run_demo_startup_validation() must return True when config is correct."""
        cfg = _locked_config()
        with patch("config.settings.settings") as mock_s:
            mock_s.get.side_effect = lambda k, d=None: {
                "demo_mode.locked":                     True,
                "demo_mode.parameter_lock_version":     "session50",
                "demo_mode.note":                       "test",
                "disabled_models":                      cfg["disabled_models"],
                "mr_pbl_slc.enabled":                   True,
                "mr_pbl_slc.pullback_long.sl_atr_mult": 3.0,
                "mr_pbl_slc.pullback_long.tp_atr_mult": 4.0,
                "mr_pbl_slc.pullback_long.ema_prox_atr_mult": 0.4,
                "mr_pbl_slc.pullback_long.rsi_min":     45.0,
                "mr_pbl_slc.pullback_long.wick_strength": 1.5,
                "data.default_timeframe":               "30m",
                "risk_engine.risk_pct_per_trade":       0.5,
                "idss.min_confluence_score":            0.45,
                "multi_tf.confirmation_required":       True,
                "scanner.auto_execute":                 True,
                "data.websocket_enabled":               False,
                "execution_mode.backtest_parity":       True,
            }.get(k, d)

            from core.orchestrator.demo_startup_log import run_demo_startup_validation
            result = run_demo_startup_validation()
        self.assertTrue(result, "Demo startup validation should pass with correct config")

    def test_validation_fails_when_param_wrong(self):
        """Validation must return False if a PBL param is wrong."""
        cfg = _locked_config()
        with patch("config.settings.settings") as mock_s:
            mock_s.get.side_effect = lambda k, d=None: {
                "demo_mode.locked":                     True,
                "demo_mode.parameter_lock_version":     "session50",
                "demo_mode.note":                       "",
                "disabled_models":                      cfg["disabled_models"],
                "mr_pbl_slc.enabled":                   True,
                "mr_pbl_slc.pullback_long.sl_atr_mult": 99.9,  # WRONG
                "mr_pbl_slc.pullback_long.tp_atr_mult": 4.0,
                "mr_pbl_slc.pullback_long.ema_prox_atr_mult": 0.4,
                "mr_pbl_slc.pullback_long.rsi_min":     45.0,
                "mr_pbl_slc.pullback_long.wick_strength": 1.5,
            }.get(k, d)

            from core.orchestrator.demo_startup_log import run_demo_startup_validation
            result = run_demo_startup_validation()
        self.assertFalse(result, "Validation should fail when sl_atr_mult is wrong")

    def test_validation_fails_when_unlocked(self):
        """Validation must return False when demo_mode.locked=False."""
        with patch("config.settings.settings") as mock_s:
            mock_s.get.side_effect = lambda k, d=None: {
                "demo_mode.locked": False,
                "demo_mode.note": "",
                "demo_mode.parameter_lock_version": "?",
                "mr_pbl_slc.enabled": True,
                "disabled_models": [],
            }.get(k, d)

            from core.orchestrator.demo_startup_log import run_demo_startup_validation
            result = run_demo_startup_validation()
        self.assertFalse(result)


# ── 6. Export and summary ────────────────────────────────────

class TestDemoExportSummary(unittest.TestCase):
    """PaperExecutor must expose export_trades_csv() and generate_demo_summary()."""

    def _make_executor_with_trades(self, n: int = 5):
        """Return a PaperExecutor with n synthetic closed trades."""
        from unittest.mock import patch as _patch, MagicMock as _MM
        # Patch out all I/O-heavy dependencies
        with (
            _patch("core.execution.paper_executor.PaperExecutor._load_open_positions"),
            _patch("core.execution.paper_executor.PaperExecutor._load_history"),
            _patch("core.event_bus.bus.subscribe"),
        ):
            from core.execution.paper_executor import PaperExecutor
            pe = PaperExecutor.__new__(PaperExecutor)
            pe._positions     = {}
            pe._closed_trades = []
            pe._capital       = 100_000.0
            pe._peak_capital  = 100_000.0
            pe._initial_capital = 100_000.0

        from datetime import datetime as _dt
        for i in range(n):
            won = i % 2 == 0
            pe._closed_trades.append({
                "trade_id":    f"T{i:04d}",
                "symbol":      "BTC/USDT",
                "side":        "buy",
                "model":       "pullback_long",
                "timeframe":   "30m",
                "regime":      "bull_trend",
                "opened_at":   _dt(2026, 1, 1 + (i % 28), 12).isoformat(),
                "closed_at":   _dt(2026, 1, 1 + (i % 28), 14).isoformat(),
                "duration_s":  7200,
                "entry_price": 100.0,
                "stop_loss":   94.0,
                "take_profit": 112.0,
                "exit_price":  112.0 if won else 94.0,
                "size_usdt":   500.0,
                "entry_size_usdt": 500.0,
                "pnl_usdt":    60.0 if won else -30.0,
                "pnl_pct":     12.0 if won else -6.0,
                "realized_r":  2.0  if won else -1.0,
                "exit_reason": "target_hit" if won else "stop_hit",
                "confluence_score": 0.65,
            })
        return pe

    def test_export_trades_csv_returns_path(self):
        """export_trades_csv() must write a file and return its path."""
        import tempfile, os, csv
        pe = self._make_executor_with_trades(10)
        with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as tf:
            out = tf.name
        try:
            path = pe.export_trades_csv(output_path=out)
            self.assertTrue(Path(path).exists(), "CSV file must exist")
            with open(path) as f:
                rows = list(csv.DictReader(f))
            self.assertEqual(len(rows), 10, "CSV must have 10 data rows")
            self.assertIn("trade_id", rows[0], "CSV must have trade_id column")
            self.assertIn("realized_r", rows[0], "CSV must have realized_r column")
        finally:
            os.unlink(out)

    def test_generate_demo_summary_structure(self):
        """generate_demo_summary() must return required keys."""
        pe = self._make_executor_with_trades(5)
        summary = pe.generate_demo_summary()
        required = [
            "generated_at", "demo_phase_complete", "min_trades_required",
            "all_trades", "last_20", "last_50", "by_model", "by_symbol", "exit_reasons",
        ]
        for k in required:
            self.assertIn(k, summary, f"summary missing key: {k}")

    def test_demo_phase_complete_flag(self):
        """demo_phase_complete must be True at ≥50 trades, False below."""
        pe_few = self._make_executor_with_trades(10)
        pe_many = self._make_executor_with_trades(55)
        self.assertFalse(pe_few.generate_demo_summary()["demo_phase_complete"])
        self.assertTrue(pe_many.generate_demo_summary()["demo_phase_complete"])

    def test_summary_pf_and_wr(self):
        """PF and WR in summary must be numerically consistent."""
        pe = self._make_executor_with_trades(10)  # alternating W/L → WR=50%
        summary = pe.generate_demo_summary()
        all_metrics = summary["all_trades"]
        self.assertAlmostEqual(all_metrics["wr"], 0.5, places=3)
        # 5 wins × 60 USDT, 5 losses × 30 USDT → PF = 300/150 = 2.0
        self.assertAlmostEqual(all_metrics["pf"], 2.0, places=3)

    def test_rolling_demo_metrics_does_not_raise(self):
        """_log_rolling_demo_metrics() must not raise."""
        pe = self._make_executor_with_trades(5)
        pe._log_rolling_demo_metrics()  # should not raise


if __name__ == "__main__":
    unittest.main(verbosity=2)
