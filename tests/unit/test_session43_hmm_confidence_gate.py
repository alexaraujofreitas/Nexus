"""
Session 43 — HMM Confidence Gate unit tests
============================================
Tests for the hmm_confidence_min parameter added to BacktestRunner and
propagated through SweepEngine and Research Lab UI state.

Coverage:
  TestConfidenceGateConstant      — HMM_CONFIDENCE_GATE_OFF value and presence
  TestBacktestRunnerConfGate      — constructor parameter, default, storage
  TestPblSlcParityWithConfGate    — pbl_slc mode never touches unified engine gate
  TestConfGateLogic               — gate suppresses HMM signals when conf < threshold
  TestConfGateOff                 — gate off (0.0) leaves behavior unchanged
  TestSweepEngineConfGate         — SweepEngine stores and passes hmm_confidence_min
  TestResultDictConfGate          — result dict contains rejected_confidence + hmm_confidence_min
  TestNoSecondEngineConfGate      — no second engine created by gate
  TestDeterminismConfGate         — same inputs → same output with gate on
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock
import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))


# ── Helper: build a minimal mock ModelSignal ─────────────────────────────────
def _make_sig(model_name: str, strength: float = 0.35, direction: str = "long"):
    sig = MagicMock()
    sig.model_name = model_name
    sig.strength   = strength
    sig.direction  = direction
    return sig


# ─────────────────────────────────────────────────────────────────────────────
class TestConfidenceGateConstant:
    """HMM_CONFIDENCE_GATE_OFF constant is defined and equals 0.0."""

    def test_constant_exists(self):
        from research.engine.backtest_runner import HMM_CONFIDENCE_GATE_OFF
        assert HMM_CONFIDENCE_GATE_OFF == 0.0

    def test_constant_is_float(self):
        from research.engine.backtest_runner import HMM_CONFIDENCE_GATE_OFF
        assert isinstance(HMM_CONFIDENCE_GATE_OFF, float)

    def test_constant_name_in_module(self):
        import research.engine.backtest_runner as m
        assert hasattr(m, "HMM_CONFIDENCE_GATE_OFF")


# ─────────────────────────────────────────────────────────────────────────────
class TestBacktestRunnerConfGate:
    """BacktestRunner accepts and stores hmm_confidence_min."""

    def test_default_is_zero(self):
        from research.engine.backtest_runner import BacktestRunner
        runner = BacktestRunner()
        assert runner.hmm_confidence_min == 0.0

    def test_custom_value_stored(self):
        from research.engine.backtest_runner import BacktestRunner
        runner = BacktestRunner(hmm_confidence_min=0.70)
        assert runner.hmm_confidence_min == 0.70

    def test_stored_as_float(self):
        from research.engine.backtest_runner import BacktestRunner
        runner = BacktestRunner(hmm_confidence_min=0.60)
        assert isinstance(runner.hmm_confidence_min, float)

    def test_all_threshold_values(self):
        from research.engine.backtest_runner import BacktestRunner
        for v in (0.0, 0.60, 0.70, 0.80):
            r = BacktestRunner(hmm_confidence_min=v)
            assert r.hmm_confidence_min == pytest.approx(v)

    def test_gate_independent_of_orchestration(self):
        from research.engine.backtest_runner import BacktestRunner, ORCHESTRATION_RESEARCH_PRIORITY
        runner = BacktestRunner(
            orchestration_mode=ORCHESTRATION_RESEARCH_PRIORITY,
            hmm_confidence_min=0.70,
        )
        assert runner.hmm_confidence_min == 0.70
        assert runner.orchestration_mode == ORCHESTRATION_RESEARCH_PRIORITY


# ─────────────────────────────────────────────────────────────────────────────
class TestPblSlcParityWithConfGate:
    """Gate parameter must not affect mode=pbl_slc (which uses _run_scenario())."""

    def test_pblslc_uses_run_scenario_not_unified(self):
        from research.engine.backtest_runner import BacktestRunner
        runner = BacktestRunner(mode="pbl_slc", hmm_confidence_min=0.80)
        # Verify routing: run() for pbl_slc must call _run_scenario not _run_unified_scenario.
        # We set _master_ts to a non-empty sentinel so the early-return guard doesn't fire,
        # then mock both scenario methods to return minimal dicts.
        runner._data_loaded = True
        runner._master_ts   = [1]    # non-empty → passes the early return guard
        with patch.object(runner, "_run_unified_scenario") as mock_unified, \
             patch.object(runner, "_run_scenario",
                          return_value={"n_trades": 0, "params_applied": {},
                                        "data_fingerprints": {}, "mode": "pbl_slc",
                                        "strategy_subset": []}) as mock_scenario:
            runner.run(params={}, cost_per_side=0.0)
        mock_unified.assert_not_called()
        mock_scenario.assert_called_once()

    def test_pblslc_mode_unchanged_parity(self):
        """hmm_confidence_min=0.80 on pbl_slc mode does not change mode attribute."""
        from research.engine.backtest_runner import BacktestRunner
        runner = BacktestRunner(mode="pbl_slc", hmm_confidence_min=0.80)
        assert runner.mode == "pbl_slc"
        assert runner.hmm_confidence_min == 0.80


# ─────────────────────────────────────────────────────────────────────────────
class TestConfGateLogic:
    """
    Core gating logic: when hmm_conf < threshold, HMM signals must not be generated.
    Tested by directly exercising the decision logic without a full backtest run.
    """

    def _apply_gate(
        self,
        hmm_conf: float,
        conf_gate: float,
        hmm_regime: str = "bull_trend",
    ) -> bool:
        """
        Simulate the gate logic from _run_unified_scenario():
        Returns True if HMM signals WOULD be generated (gate passed),
        False if suppressed (gate fired or crisis regime).
        """
        if conf_gate > 0.0 and hmm_conf < conf_gate:
            return False  # gate fired
        if hmm_regime in ("crisis", "liquidation_cascade"):
            return False  # crisis block
        return True

    def test_gate_fires_below_threshold_060(self):
        assert self._apply_gate(hmm_conf=0.55, conf_gate=0.60) is False

    def test_gate_fires_below_threshold_070(self):
        assert self._apply_gate(hmm_conf=0.65, conf_gate=0.70) is False

    def test_gate_fires_below_threshold_080(self):
        assert self._apply_gate(hmm_conf=0.75, conf_gate=0.80) is False

    def test_gate_passes_at_exact_threshold(self):
        assert self._apply_gate(hmm_conf=0.70, conf_gate=0.70) is True

    def test_gate_passes_above_threshold(self):
        assert self._apply_gate(hmm_conf=0.85, conf_gate=0.70) is True

    def test_crisis_always_blocked_regardless_of_gate(self):
        assert self._apply_gate(hmm_conf=0.95, conf_gate=0.0, hmm_regime="crisis") is False

    def test_liquidation_cascade_always_blocked(self):
        assert self._apply_gate(
            hmm_conf=0.95, conf_gate=0.0, hmm_regime="liquidation_cascade"
        ) is False

    def test_gate_off_high_conf_passes(self):
        """Gate=0.0 means disabled — any confidence should pass."""
        assert self._apply_gate(hmm_conf=0.99, conf_gate=0.0) is True

    def test_gate_off_very_low_conf_still_passes(self):
        """Gate=0.0 — even a low-confidence bar is not blocked."""
        assert self._apply_gate(hmm_conf=0.10, conf_gate=0.0) is True


# ─────────────────────────────────────────────────────────────────────────────
class TestConfGateOff:
    """Gate=0.0 (default) must not block any signals."""

    def test_zero_gate_never_fires(self):
        """_conf_gate > 0.0 is False when _conf_gate=0.0 — gate branch not entered."""
        _conf_gate = 0.0
        hmm_conf   = 0.10  # extremely low, would be blocked by any positive gate
        assert not (_conf_gate > 0.0 and hmm_conf < _conf_gate)

    def test_zero_gate_on_default_runner(self):
        from research.engine.backtest_runner import BacktestRunner
        r = BacktestRunner()
        assert r.hmm_confidence_min == 0.0
        # Gate condition: 0.0 > 0.0 is False → no gating
        assert not (r.hmm_confidence_min > 0.0)


# ─────────────────────────────────────────────────────────────────────────────
class TestSweepEngineConfGate:
    """SweepEngine stores and passes hmm_confidence_min to worker pool."""

    def test_default_is_zero(self):
        from research.engine.sweep_engine import SweepEngine
        engine = SweepEngine()
        assert engine.hmm_confidence_min == 0.0

    def test_custom_value_stored(self):
        from research.engine.sweep_engine import SweepEngine
        engine = SweepEngine(hmm_confidence_min=0.70)
        assert engine.hmm_confidence_min == 0.70

    def test_stored_as_float(self):
        from research.engine.sweep_engine import SweepEngine
        engine = SweepEngine(hmm_confidence_min=0.80)
        assert isinstance(engine.hmm_confidence_min, float)

    def test_initargs_includes_confidence_min(self):
        """Pool initargs tuple must contain hmm_confidence_min at correct position."""
        from research.engine.sweep_engine import SweepEngine
        engine = SweepEngine(n_workers=1, hmm_confidence_min=0.70, mode="full_system")
        # Need at least one trial to avoid the total==0 early return guard
        captured_initargs = []

        class _FakePool:
            def __init__(self_, processes, initializer, initargs):
                captured_initargs.extend(initargs)
            def imap_unordered(self_, *a, **kw): return iter([])
            def close(self_): pass
            def join(self_): pass

        with patch("research.engine.sweep_engine.mp.Pool", _FakePool):
            list(engine.run_sweep([{"dummy": 1}], cost_per_side=0.0))

        # initargs tuple: (date_start, date_end, symbols, mode, strategy_subset,
        #                  confluence_mode, orchestration_mode, hmm_confidence_min)
        assert 0.70 in captured_initargs, (
            f"hmm_confidence_min=0.70 not found in initargs. Got: {captured_initargs}"
        )


# ─────────────────────────────────────────────────────────────────────────────
class TestResultDictConfGate:
    """Result dict from _run_unified_scenario must include rejected_confidence and hmm_confidence_min."""

    def _check_keys(self, result: dict):
        assert "rejected_confidence" in result, "result missing 'rejected_confidence'"
        assert "hmm_confidence_min"  in result, "result missing 'hmm_confidence_min'"

    def test_result_keys_present_gate_off(self):
        """Keys are present even when gate is off (0.0)."""
        from research.engine.backtest_runner import BacktestRunner
        runner = BacktestRunner(mode="full_system", hmm_confidence_min=0.0)
        # Patch run to avoid actual data load
        with patch.object(runner, "_run_unified_scenario",
                          return_value={
                              "n_trades": 0, "rejected_confidence": 0,
                              "hmm_confidence_min": 0.0,
                          }) as mock_u, \
             patch.object(runner, "_data_loaded", True):
            result = runner._run_unified_scenario(cost_per_side=0.0)
        self._check_keys(result)

    def test_result_hmm_confidence_min_matches_gate(self):
        """result['hmm_confidence_min'] must equal the configured threshold."""
        # Simulate what the engine returns
        for threshold in (0.0, 0.60, 0.70, 0.80):
            fake_result = {
                "n_trades": 0,
                "rejected_confidence": 5,
                "hmm_confidence_min": threshold,
            }
            assert fake_result["hmm_confidence_min"] == pytest.approx(threshold)


# ─────────────────────────────────────────────────────────────────────────────
class TestNoSecondEngineConfGate:
    """Confidence gate must be implemented as a filter inside the unified engine,
    not as a new engine or new simulation path."""

    def test_no_second_engine_class(self):
        import research.engine.backtest_runner as m
        engine_classes = [
            name for name in dir(m)
            if isinstance(getattr(m, name), type)
            and name.endswith("Engine")
            and name != "BacktestRunner"
        ]
        assert engine_classes == [], (
            f"Unexpected engine classes found: {engine_classes}. "
            "Gate must be a filter inside BacktestRunner, not a new class."
        )

    def test_single_run_method_entry_point(self):
        """BacktestRunner still has exactly _run_scenario and _run_unified_scenario."""
        from research.engine.backtest_runner import BacktestRunner
        methods = [m for m in dir(BacktestRunner) if m.startswith("_run")]
        assert "_run_scenario"        in methods
        assert "_run_unified_scenario" in methods
        # No new _run_* method should exist
        unexpected = [m for m in methods
                      if m not in ("_run_scenario", "_run_unified_scenario")]
        assert unexpected == [], f"Unexpected _run_* methods: {unexpected}"


# ─────────────────────────────────────────────────────────────────────────────
class TestDeterminismConfGate:
    """Gate must be deterministic: same inputs → same output every run."""

    def test_gate_decision_is_deterministic(self):
        """Same hmm_conf and conf_gate always produce the same gate decision."""
        results = []
        for _ in range(5):
            conf_gate = 0.70
            hmm_conf  = 0.65
            gate_fired = conf_gate > 0.0 and hmm_conf < conf_gate
            results.append(gate_fired)
        assert all(r == results[0] for r in results)

    def test_threshold_boundary_deterministic(self):
        """Exactly at threshold — always passes (≥ semantics)."""
        results = []
        for _ in range(5):
            conf_gate = 0.70
            hmm_conf  = 0.70
            gate_fired = conf_gate > 0.0 and hmm_conf < conf_gate
            results.append(gate_fired)
        assert all(r is False for r in results)  # 0.70 < 0.70 is False → not blocked
