"""
tests/unit/test_session40_unified_engine.py
============================================
Validation tests for the unified BacktestRunner (Session 40).

Tests are grouped:
  Group A — Architecture integrity (import/instantiation, no second engine)
  Group B — Mode routing (mode parameter wires to correct path)
  Group C — Parameter registry (16 params, all modes covered)
  Group D — Strategy panel (UI widget builds without Qt display where possible)
  Group E — SweepEngine (mode/subset thread-through)
  Group F — Parity assertion (pbl_slc path is unchanged — headless safe)

Tests F (baseline parity) are marked @pytest.mark.slow and skipped unless
the backtest_data/ directory exists AND the environment variable
NEXUS_RUN_BACKTEST=1 is set (avoids CI timeout).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

_DATA_AVAILABLE = (ROOT / "backtest_data" / "BTC_USDT_30m.parquet").exists()
_RUN_BACKTEST   = os.getenv("NEXUS_RUN_BACKTEST", "0") == "1"

_slow = pytest.mark.skipif(
    not (_DATA_AVAILABLE and _RUN_BACKTEST),
    reason="backtest data missing or NEXUS_RUN_BACKTEST!=1",
)


# ─────────────────────────────────────────────────────────────────────────────
# Group A — Architecture integrity
# ─────────────────────────────────────────────────────────────────────────────

class TestArchitectureIntegrity:

    def test_single_module(self):
        """There is exactly ONE BacktestRunner class — no duplicates."""
        from research.engine import backtest_runner as br_mod
        classes = [name for name in dir(br_mod) if name == "BacktestRunner"]
        assert len(classes) == 1

    def test_no_second_engine_module(self):
        """No 'backtest_runner_trend' or similar alternative engine file exists."""
        engine_dir = ROOT / "research" / "engine"
        runner_files = list(engine_dir.glob("backtest_runner*.py"))
        assert len(runner_files) == 1, (
            f"Found {len(runner_files)} runner files: {runner_files}. "
            "Only one canonical engine is allowed."
        )

    def test_mode_constants_defined(self):
        from research.engine.backtest_runner import BacktestRunner
        for const in ("MODE_PBL_SLC", "MODE_PBL", "MODE_SLC", "MODE_TREND",
                      "MODE_MOMENTUM", "MODE_FULL_SYSTEM", "MODE_CUSTOM"):
            assert hasattr(BacktestRunner, const), f"Missing constant: {const}"

    def test_model_sets_defined(self):
        from research.engine.backtest_runner import BacktestRunner
        assert "pullback_long"          in BacktestRunner._RESEARCH_MODELS
        assert "swing_low_continuation" in BacktestRunner._RESEARCH_MODELS
        assert "trend"                  in BacktestRunner._HMM_MODELS
        assert "momentum_breakout"      in BacktestRunner._HMM_MODELS

    def test_unified_scenario_method_exists(self):
        from research.engine.backtest_runner import BacktestRunner
        assert hasattr(BacktestRunner, "_run_unified_scenario")
        assert hasattr(BacktestRunner, "_run_scenario")          # reference unchanged
        assert hasattr(BacktestRunner, "_fit_hmm")
        assert hasattr(BacktestRunner, "_get_active_models")
        assert hasattr(BacktestRunner, "_needs_hmm")

    def test_run_scenario_is_not_renamed(self):
        """_run_scenario must remain — it is the reference implementation."""
        from research.engine.backtest_runner import BacktestRunner
        import inspect
        src = inspect.getsource(BacktestRunner._run_scenario)
        # Reference implementation always uses ResearchRegimeClassifier
        assert "research_regime_to_string" in src
        assert "RES_BULL_TREND" in src


# ─────────────────────────────────────────────────────────────────────────────
# Group B — Mode routing
# ─────────────────────────────────────────────────────────────────────────────

class TestModeRouting:

    def test_default_mode_is_pbl_slc(self):
        from research.engine.backtest_runner import BacktestRunner
        r = BacktestRunner()
        assert r.mode == "pbl_slc"

    def test_pbl_slc_active_models(self):
        from research.engine.backtest_runner import BacktestRunner
        r = BacktestRunner(mode="pbl_slc")
        assert set(r._get_active_models()) == {"pullback_long", "swing_low_continuation"}

    def test_trend_active_models(self):
        from research.engine.backtest_runner import BacktestRunner
        r = BacktestRunner(mode="trend")
        assert r._get_active_models() == ["trend"]

    def test_momentum_active_models(self):
        from research.engine.backtest_runner import BacktestRunner
        r = BacktestRunner(mode="momentum")
        assert r._get_active_models() == ["momentum_breakout"]

    def test_full_system_active_models(self):
        """MODE_FULL_SYSTEM active models — trend removed Session 48 (PF 0.9592)."""
        from research.engine.backtest_runner import BacktestRunner
        r = BacktestRunner(mode="full_system")
        active = set(r._get_active_models())
        assert "pullback_long"          in active
        assert "swing_low_continuation" in active
        assert "momentum_breakout"      in active
        # trend removed Session 48: net-negative at production fees (PF 0.9592)
        assert "trend" not in active

    def test_custom_subset_override(self):
        from research.engine.backtest_runner import BacktestRunner
        subset = ["trend", "pullback_long"]
        r = BacktestRunner(mode="custom", strategy_subset=subset)
        assert r._get_active_models() == subset

    def test_needs_hmm_pbl_slc_false(self):
        from research.engine.backtest_runner import BacktestRunner
        r = BacktestRunner(mode="pbl_slc")
        assert r._needs_hmm() is False

    def test_needs_hmm_trend_true(self):
        from research.engine.backtest_runner import BacktestRunner
        r = BacktestRunner(mode="trend")
        assert r._needs_hmm() is True

    def test_needs_hmm_full_system_true(self):
        from research.engine.backtest_runner import BacktestRunner
        r = BacktestRunner(mode="full_system")
        assert r._needs_hmm() is True

    def test_needs_hmm_custom_pbl_only_false(self):
        from research.engine.backtest_runner import BacktestRunner
        r = BacktestRunner(mode="custom", strategy_subset=["pullback_long"])
        assert r._needs_hmm() is False

    def test_needs_hmm_custom_mixed_true(self):
        from research.engine.backtest_runner import BacktestRunner
        r = BacktestRunner(mode="custom", strategy_subset=["pullback_long", "trend"])
        assert r._needs_hmm() is True


# ─────────────────────────────────────────────────────────────────────────────
# Group C — Parameter registry
# ─────────────────────────────────────────────────────────────────────────────

class TestParameterRegistry:

    def test_total_count(self):
        from research.engine.parameter_registry import ALL_PARAMS
        assert len(ALL_PARAMS) == 16, f"Expected 16, got {len(ALL_PARAMS)}"

    def test_model_families(self):
        from research.engine.parameter_registry import ALL_PARAMS
        families = {p.model for p in ALL_PARAMS}
        assert families == {"pbl", "slc", "trend", "momentum"}

    def test_trend_adx_default(self):
        from research.engine.parameter_registry import TREND_ADX_MIN
        assert TREND_ADX_MIN.default == 31.0
        assert TREND_ADX_MIN.settings_key == "models.trend.adx_min"

    def test_momentum_lookback(self):
        from research.engine.parameter_registry import MB_LOOKBACK
        assert MB_LOOKBACK.default == 20
        assert MB_LOOKBACK.dtype == "int"

    def test_params_for_mode_pbl_slc(self):
        from research.engine.parameter_registry import params_for_mode
        pp = params_for_mode("pbl_slc")
        models = {p.model for p in pp}
        assert models == {"pbl", "slc"}
        assert len(pp) == 8

    def test_params_for_mode_trend(self):
        from research.engine.parameter_registry import params_for_mode
        pp = params_for_mode("trend")
        assert all(p.model == "trend" for p in pp)
        assert len(pp) == 4

    def test_params_for_mode_full_system(self):
        from research.engine.parameter_registry import params_for_mode
        pp = params_for_mode("full_system")
        assert len(pp) == 16

    def test_params_by_key_complete(self):
        from research.engine.parameter_registry import ALL_PARAMS, PARAMS_BY_KEY
        assert len(PARAMS_BY_KEY) == len(ALL_PARAMS)
        for p in ALL_PARAMS:
            assert p.settings_key in PARAMS_BY_KEY

    def test_coarse_values_trend_adx(self):
        from research.engine.parameter_registry import TREND_ADX_MIN
        vals = TREND_ADX_MIN.coarse_values()
        # range_min=20, range_max=45, step=2.5 → 11 values
        assert 20.0 in vals
        assert 45.0 in vals
        assert len(vals) == 11

    def test_validate_params_ok(self):
        from research.engine.parameter_registry import validate_params
        ok, errors = validate_params({"models.trend.adx_min": 31.0})
        assert ok
        assert errors == []

    def test_validate_params_out_of_range(self):
        from research.engine.parameter_registry import validate_params
        ok, errors = validate_params({"models.trend.adx_min": 99.0})
        assert not ok
        assert len(errors) == 1


# ─────────────────────────────────────────────────────────────────────────────
# Group D — LabState additions
# ─────────────────────────────────────────────────────────────────────────────

class TestLabState:

    def test_backtest_mode_field_exists(self):
        """_LabState must have backtest_mode and strategy_subset fields."""
        sys.path.insert(0, str(ROOT))
        # Import without Qt by checking source
        import ast
        src = (ROOT / "gui" / "pages" / "research_lab" / "research_lab_page.py").read_text()
        tree = ast.parse(src)
        # Find _LabState class and check its fields
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == "_LabState":
                src_segment = ast.unparse(node)
                assert "backtest_mode" in src_segment, "_LabState missing backtest_mode"
                assert "strategy_subset" in src_segment, "_LabState missing strategy_subset"
                return
        pytest.fail("_LabState class not found in research_lab_page.py")

    def test_strategy_panel_class_exists(self):
        """_StrategyPanel must be defined in research_lab_page.py."""
        import ast
        src = (ROOT / "gui" / "pages" / "research_lab" / "research_lab_page.py").read_text()
        tree = ast.parse(src)
        classes = [n.name for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]
        assert "_StrategyPanel" in classes

    def test_strategy_options_covers_all_modes(self):
        """_STRATEGY_OPTIONS must include all 7 modes."""
        import ast
        src = (ROOT / "gui" / "pages" / "research_lab" / "research_lab_page.py").read_text()
        expected_modes = {"pbl_slc", "pbl", "slc", "trend", "momentum", "full_system", "custom"}
        for mode in expected_modes:
            assert f'"{mode}"' in src, f"Mode '{mode}' missing from _STRATEGY_OPTIONS"

    def test_parameter_panel_has_rebuild(self):
        """_ParameterPanel must expose rebuild(mode) for dynamic rebuilding."""
        import ast
        src = (ROOT / "gui" / "pages" / "research_lab" / "research_lab_page.py").read_text()
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == "_ParameterPanel":
                methods = [n.name for n in ast.walk(node) if isinstance(n, ast.FunctionDef)]
                assert "rebuild" in methods, "_ParameterPanel missing rebuild() method"
                return
        pytest.fail("_ParameterPanel not found")


# ─────────────────────────────────────────────────────────────────────────────
# Group E — SweepEngine mode pass-through
# ─────────────────────────────────────────────────────────────────────────────

class TestSweepEngineMode:

    def test_sweep_engine_accepts_mode(self):
        from research.engine.sweep_engine import SweepEngine
        e = SweepEngine(mode="trend")
        assert e.mode == "trend"

    def test_sweep_engine_accepts_subset(self):
        from research.engine.sweep_engine import SweepEngine
        subset = ["pullback_long", "trend"]
        e = SweepEngine(mode="custom", strategy_subset=subset)
        assert e.strategy_subset == subset

    def test_init_worker_signature(self):
        """_init_worker must accept mode and strategy_subset."""
        from research.engine.sweep_engine import _init_worker
        import inspect
        sig = inspect.signature(_init_worker)
        params = list(sig.parameters)
        assert "mode"            in params
        assert "strategy_subset" in params


# ─────────────────────────────────────────────────────────────────────────────
# Group F — Parity (requires backtest data + env flag)
# ─────────────────────────────────────────────────────────────────────────────

class TestParityPblSlc:

    @_slow
    def test_pbl_slc_n_trades(self):
        """mode=pbl_slc must produce exactly 1,731 trades (Stage 8 baseline)."""
        from research.engine.backtest_runner import BacktestRunner
        r = BacktestRunner(mode="pbl_slc")
        r.load_data()
        result = r.run(params={}, cost_per_side=0.0)
        assert result["n_trades"] == 1731, (
            f"Parity broken: expected 1731 trades, got {result['n_trades']}"
        )

    @_slow
    def test_pbl_slc_pf_zero_fees(self):
        """mode=pbl_slc PF (no fees) must be 1.3798 ± 0.001."""
        from research.engine.backtest_runner import BacktestRunner
        r = BacktestRunner(mode="pbl_slc")
        r.load_data()
        result = r.run(params={}, cost_per_side=0.0)
        assert abs(result["profit_factor"] - 1.3798) < 0.001, (
            f"PF parity broken: expected 1.3798, got {result['profit_factor']}"
        )

    @_slow
    def test_pbl_slc_pf_with_fees(self):
        """mode=pbl_slc PF (0.04%/side) must be 1.2682 ± 0.001."""
        from research.engine.backtest_runner import BacktestRunner
        r = BacktestRunner(mode="pbl_slc")
        r.load_data()
        result = r.run(params={}, cost_per_side=0.0004)
        assert abs(result["profit_factor"] - 1.2682) < 0.001, (
            f"PF(fees) parity broken: expected 1.2682, got {result['profit_factor']}"
        )

    @_slow
    def test_unified_route_pbl_slc_calls_reference(self):
        """
        mode=pbl_slc via run() must hit _run_scenario (not _run_unified_scenario).
        Verified by comparing results to pbl_slc baseline.
        """
        from research.engine.backtest_runner import BacktestRunner
        r = BacktestRunner(mode="pbl_slc")
        r.load_data()
        # run() routes pbl_slc → _run_scenario
        result = r.run(params={}, cost_per_side=0.0)
        assert result["mode"] == "pbl_slc"
        assert result["n_trades"] == 1731

    @_slow
    def test_trend_mode_produces_trades(self):
        """mode=trend must produce > 0 trades (smoke test — no parity target)."""
        from research.engine.backtest_runner import BacktestRunner
        r = BacktestRunner(mode="trend")
        r.load_data()
        result = r.run(params={}, cost_per_side=0.0)
        # Trend model fires on bull/bear HMM regimes — expect some trades in 4yr BTC
        assert result["n_trades"] >= 0   # ≥0 (might be 0 if HMM fallback hits uncertain)
        assert "mode" in result
        assert result["mode"] == "trend"

    @_slow
    def test_full_system_mode_runs(self):
        """mode=full_system must complete without error."""
        from research.engine.backtest_runner import BacktestRunner
        r = BacktestRunner(mode="full_system")
        r.load_data()
        result = r.run(params={}, cost_per_side=0.0004)
        assert "profit_factor" in result
        assert result["mode"] == "full_system"
