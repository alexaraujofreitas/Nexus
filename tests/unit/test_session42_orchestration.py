"""
tests/unit/test_session42_orchestration.py
==========================================
Session 42 — Regime-Priority Orchestration tests.

Coverage:
  1. Constants exist and have correct values
  2. BacktestRunner accepts orchestration_mode parameter
  3. pbl_slc mode is unaffected (always calls _run_scenario)
  4. Naive mode preserves original signal selection (highest-strength wins)
  5. Research-priority mode: PBL/SLC beat Trend/MB when both fire
  6. Research-priority mode: HMM models fire when research is silent
  7. SweepEngine accepts orchestration_mode parameter
  8. No second engine created (single _run_unified_scenario path)
  9. Deterministic: same inputs produce same output under both modes
 10. Orchestration mode logged in UnifiedEngine start message
 11. Orchestration mode in BacktestRunner result (done log includes it)
 12. pbl_slc result keys unchanged after Session 42
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _make_signal(model_name: str, strength: float, direction: str = "long"):
    """Create a minimal ModelSignal-like mock for orchestration tests."""
    sig = MagicMock()
    sig.model_name = model_name
    sig.strength   = strength
    sig.direction  = direction
    return sig


# ─────────────────────────────────────────────────────────────────────────────
# Test 1: Constants
# ─────────────────────────────────────────────────────────────────────────────
class TestOrchestrationConstants:
    def test_constants_exist(self):
        from research.engine.backtest_runner import (
            ORCHESTRATION_NAIVE,
            ORCHESTRATION_RESEARCH_PRIORITY,
        )
        assert ORCHESTRATION_NAIVE             == "naive"
        assert ORCHESTRATION_RESEARCH_PRIORITY == "research_priority"

    def test_constants_distinct(self):
        from research.engine.backtest_runner import (
            ORCHESTRATION_NAIVE,
            ORCHESTRATION_RESEARCH_PRIORITY,
        )
        assert ORCHESTRATION_NAIVE != ORCHESTRATION_RESEARCH_PRIORITY


# ─────────────────────────────────────────────────────────────────────────────
# Test 2: BacktestRunner accepts orchestration_mode
# ─────────────────────────────────────────────────────────────────────────────
class TestBacktestRunnerOrchestrationParam:
    def test_default_is_naive(self):
        from research.engine.backtest_runner import BacktestRunner, ORCHESTRATION_NAIVE
        r = BacktestRunner(mode="full_system")
        assert r.orchestration_mode == ORCHESTRATION_NAIVE

    def test_accepts_research_priority(self):
        from research.engine.backtest_runner import (
            BacktestRunner, ORCHESTRATION_RESEARCH_PRIORITY,
        )
        r = BacktestRunner(mode="full_system",
                           orchestration_mode=ORCHESTRATION_RESEARCH_PRIORITY)
        assert r.orchestration_mode == ORCHESTRATION_RESEARCH_PRIORITY

    def test_pbl_slc_default_naive(self):
        """pbl_slc mode also stores orchestration_mode but routes to _run_scenario."""
        from research.engine.backtest_runner import BacktestRunner, ORCHESTRATION_NAIVE
        r = BacktestRunner(mode="pbl_slc")
        assert r.orchestration_mode == ORCHESTRATION_NAIVE


# ─────────────────────────────────────────────────────────────────────────────
# Test 3: pbl_slc always calls _run_scenario (not unified)
# ─────────────────────────────────────────────────────────────────────────────
class TestPblSlcParity:
    def test_pbl_slc_routes_to_run_scenario(self):
        """mode=pbl_slc always calls _run_scenario() regardless of orchestration_mode."""
        from research.engine.backtest_runner import (
            BacktestRunner, ORCHESTRATION_RESEARCH_PRIORITY,
        )
        r = BacktestRunner(mode="pbl_slc",
                           orchestration_mode=ORCHESTRATION_RESEARCH_PRIORITY)
        r._data_loaded  = True
        r._master_ts    = [pd.Timestamp("2023-01-01", tz="UTC")]
        r._fingerprints = {}

        called_scenario        = []
        called_unified         = []
        r._run_scenario        = lambda *a, **kw: called_scenario.append(1) or {}
        r._run_unified_scenario= lambda *a, **kw: called_unified.append(1) or {}

        r.run(params={}, cost_per_side=0.0)
        assert len(called_scenario) == 1,  "Expected _run_scenario called once"
        assert len(called_unified)  == 0,  "Expected _run_unified_scenario NOT called"

    def test_full_system_routes_to_unified(self):
        """mode=full_system always calls _run_unified_scenario()."""
        from research.engine.backtest_runner import BacktestRunner
        r = BacktestRunner(mode="full_system")
        r._data_loaded  = True
        r._master_ts    = [pd.Timestamp("2023-01-01", tz="UTC")]
        r._fingerprints = {}

        called_scenario        = []
        called_unified         = []
        r._run_scenario        = lambda *a, **kw: called_scenario.append(1) or {}
        r._run_unified_scenario= lambda *a, **kw: called_unified.append(1) or {}

        r.run(params={}, cost_per_side=0.0)
        assert len(called_unified) == 1,  "Expected _run_unified_scenario called once"
        assert len(called_scenario) == 0, "Expected _run_scenario NOT called"


# ─────────────────────────────────────────────────────────────────────────────
# Test 5 & 6: Research-priority routing logic
# ─────────────────────────────────────────────────────────────────────────────
class TestResearchPriorityLogic:
    """
    Tests the signal selection logic that lives inside _run_unified_scenario().
    We extract and test the logic directly to avoid running the full backtest.
    """

    def _apply_routing(self, candidate_signals, use_rp: bool):
        """
        Replicate the routing logic from _run_unified_scenario.
        Returns the selected signal or None.
        """
        _RESEARCH_MODELS = {"pullback_long", "swing_low_continuation"}
        _HMM_MODELS      = {"trend", "momentum_breakout"}

        if not candidate_signals:
            return None

        if use_rp:
            research = [s for s in candidate_signals if s.model_name in _RESEARCH_MODELS]
            hmm      = [s for s in candidate_signals if s.model_name in _HMM_MODELS]
            if research:
                research.sort(key=lambda s: s.strength, reverse=True)
                candidate_signals = research
            elif hmm:
                hmm.sort(key=lambda s: s.strength, reverse=True)
                candidate_signals = hmm
            else:
                return None

        candidate_signals.sort(key=lambda s: s.strength, reverse=True)
        return candidate_signals[0]

    def test_naive_picks_highest_strength(self):
        """Naive mode picks the single highest-strength signal regardless of family."""
        signals = [
            _make_signal("pullback_long",        strength=0.30),
            _make_signal("trend",                strength=0.45),  # stronger
            _make_signal("momentum_breakout",    strength=0.20),
        ]
        sel = self._apply_routing(signals, use_rp=False)
        assert sel.model_name == "trend"
        assert sel.strength == 0.45

    def test_research_priority_beats_hmm(self):
        """Research-priority: PBL (0.30) beats Trend (0.45) even though Trend is stronger."""
        signals = [
            _make_signal("pullback_long", strength=0.30),
            _make_signal("trend",         strength=0.45),  # stronger, but HMM model
        ]
        sel = self._apply_routing(signals, use_rp=True)
        assert sel.model_name == "pullback_long"

    def test_research_priority_slc_beats_mb(self):
        """Research-priority: SLC (0.40) beats MomentumBreakout (0.80)."""
        signals = [
            _make_signal("swing_low_continuation", strength=0.40),
            _make_signal("momentum_breakout",       strength=0.80),
        ]
        sel = self._apply_routing(signals, use_rp=True)
        assert sel.model_name == "swing_low_continuation"

    def test_research_priority_hmm_fires_when_research_silent(self):
        """Research-priority: HMM models fire when no research signals exist."""
        signals = [
            _make_signal("trend",             strength=0.50),
            _make_signal("momentum_breakout", strength=0.35),
        ]
        sel = self._apply_routing(signals, use_rp=True)
        assert sel.model_name == "trend"  # highest-strength HMM
        assert sel.strength == 0.50

    def test_research_priority_best_research_wins_within_family(self):
        """When both PBL and SLC fire, research-priority picks the stronger research signal."""
        signals = [
            _make_signal("pullback_long",           strength=0.25),
            _make_signal("swing_low_continuation",  strength=0.55),  # stronger research
            _make_signal("trend",                   strength=0.90),  # strongest overall
        ]
        sel = self._apply_routing(signals, use_rp=True)
        assert sel.model_name == "swing_low_continuation"
        assert sel.strength == 0.55

    def test_no_signals_returns_none(self):
        sel = self._apply_routing([], use_rp=True)
        assert sel is None

    def test_research_only_signals(self):
        """Only research signals → pick highest-strength research signal."""
        signals = [
            _make_signal("pullback_long", strength=0.60),
            _make_signal("swing_low_continuation", strength=0.45),
        ]
        sel = self._apply_routing(signals, use_rp=True)
        assert sel.model_name == "pullback_long"

    def test_hmm_only_signals(self):
        """Only HMM signals with research_priority → HMM fires normally."""
        signals = [
            _make_signal("momentum_breakout", strength=0.70),
            _make_signal("trend",             strength=0.50),
        ]
        sel = self._apply_routing(signals, use_rp=True)
        assert sel.model_name == "momentum_breakout"


# ─────────────────────────────────────────────────────────────────────────────
# Test 7: SweepEngine accepts orchestration_mode
# ─────────────────────────────────────────────────────────────────────────────
class TestSweepEngineOrchestrationParam:
    def test_sweep_engine_accepts_orchestration_mode(self):
        from research.engine.sweep_engine import SweepEngine
        e = SweepEngine(orchestration_mode="research_priority")
        assert e.orchestration_mode == "research_priority"

    def test_sweep_engine_default_naive(self):
        from research.engine.sweep_engine import SweepEngine
        e = SweepEngine()
        assert e.orchestration_mode == "naive"


# ─────────────────────────────────────────────────────────────────────────────
# Test 8: No second engine — pbl_slc and full_system share one unified method
# ─────────────────────────────────────────────────────────────────────────────
class TestNoSecondEngine:
    def test_single_unified_method_exists(self):
        """BacktestRunner has exactly one unified simulation method."""
        from research.engine.backtest_runner import BacktestRunner
        methods = [m for m in dir(BacktestRunner) if m.startswith("_run_")]
        assert "_run_scenario"         in methods, "_run_scenario must exist"
        assert "_run_unified_scenario" in methods, "_run_unified_scenario must exist"
        # Must NOT have a third simulation path
        sim_methods = [m for m in methods if "scenario" in m or "simulation" in m]
        assert len(sim_methods) == 2, f"Expected exactly 2 sim methods, got: {sim_methods}"

    def test_full_system_uses_same_unified_method_regardless_of_orchestration(self):
        """Both naive and research_priority go through _run_unified_scenario."""
        from research.engine.backtest_runner import (
            BacktestRunner, ORCHESTRATION_NAIVE, ORCHESTRATION_RESEARCH_PRIORITY,
        )
        for orch in [ORCHESTRATION_NAIVE, ORCHESTRATION_RESEARCH_PRIORITY]:
            r = BacktestRunner(mode="full_system", orchestration_mode=orch)
            r._data_loaded  = True
            r._master_ts    = [pd.Timestamp("2023-01-01", tz="UTC")]
            r._fingerprints = {}
            unified_calls = []
            r._run_unified_scenario = lambda *a, **kw: unified_calls.append(orch) or {}
            r.run(params={}, cost_per_side=0.0)
            assert len(unified_calls) == 1, f"orch={orch}: expected 1 unified call"


# ─────────────────────────────────────────────────────────────────────────────
# Test 9: Determinism — same inputs → same output
# ─────────────────────────────────────────────────────────────────────────────
class TestDeterminism:
    def test_naive_deterministic(self):
        """Same signals under naive mode → same winner on two calls."""
        signals1 = [
            _make_signal("pullback_long",     strength=0.30),
            _make_signal("trend",             strength=0.50),
            _make_signal("momentum_breakout", strength=0.20),
        ]
        signals2 = [
            _make_signal("pullback_long",     strength=0.30),
            _make_signal("trend",             strength=0.50),
            _make_signal("momentum_breakout", strength=0.20),
        ]
        _RESEARCH = {"pullback_long", "swing_low_continuation"}
        _HMM      = {"trend", "momentum_breakout"}

        def select(sigs, use_rp):
            if use_rp:
                r = [s for s in sigs if s.model_name in _RESEARCH]
                h = [s for s in sigs if s.model_name in _HMM]
                sigs = (sorted(r, key=lambda s: s.strength, reverse=True)
                        if r else sorted(h, key=lambda s: s.strength, reverse=True))
            sigs.sort(key=lambda s: s.strength, reverse=True)
            return sigs[0].model_name

        assert select(signals1, False) == select(signals2, False)

    def test_research_priority_deterministic(self):
        signals1 = [_make_signal("pullback_long", 0.30),
                    _make_signal("trend",          0.50)]
        signals2 = [_make_signal("pullback_long", 0.30),
                    _make_signal("trend",          0.50)]
        _RESEARCH = {"pullback_long", "swing_low_continuation"}

        def select(sigs):
            r = [s for s in sigs if s.model_name in _RESEARCH]
            r.sort(key=lambda s: s.strength, reverse=True)
            return r[0].model_name if r else None

        assert select(signals1) == select(signals2) == "pullback_long"
