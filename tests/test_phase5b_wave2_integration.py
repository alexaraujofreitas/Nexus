"""
tests/test_phase5b_wave2_integration.py — Wave 2 Integration Tests
==================================================================
38 tests covering:
  - ProcessingEngine Wave 2 construction (3)
  - FailureMode rejection in pipeline (3)
  - EdgeValidity rejection in pipeline (3)
  - Exposure multiplier stacking (4)
  - Rejection precedence (4)
  - PipelineContext Wave 2 fields (4)
  - Backward compatibility: Wave 2 absent (3)
  - Wave 2 + Wave 1 combined (4)
  - Decision hashing with Wave 2 (3)
  - No PySide6 imports in Wave 2 modules (3)
  - Wave 2 does not mutate Wave 1 pipeline order (2)
  - Safety proof: Wave 2 never bypasses risk engine (2)
"""
import importlib
import inspect
import pytest
from unittest.mock import MagicMock

from core.intraday.execution_contracts import (
    CapitalSnapshot, DecisionStatus, Direction, ExecutionDecision,
    ExposureSnapshot, PortfolioSnapshot, RejectionSource, StrategyClass,
    _make_id,
)
from core.intraday.signal_contracts import TriggerSignal, TriggerLifecycle
from core.intraday.processing.processing_engine import (
    ProcessingEngine, REJECTION_TQS_FLOOR, REJECTION_GLOBAL_FILTER,
    REJECTION_FAILURE_MODE, REJECTION_EDGE_VALIDITY,
)
from core.intraday.pipeline_context import PipelineContext, EMPTY_CONTEXT
from core.intraday.protection.failure_mode_protection import (
    FailureModeProtection, FailureModeConfig, FailureModeResult,
    FailureSeverity,
)
from core.intraday.monitoring.edge_validity_monitor import (
    EdgeValidityMonitor, EdgeValidityConfig, EdgeValidityResult,
    EdgeState,
)
from core.intraday.scoring.trade_quality_scorer import TQSResult


# ── Constants ───────────────────────────────────────────────────
NOW_MS = 1_000_000_000_000


# ── Helpers ─────────────────────────────────────────────────────

def _trigger(symbol="BTC", direction=Direction.LONG, strength=0.7,
             trigger_quality=0.6, regime_confidence=0.5, regime="BULL_TREND",
             entry_price=50000.0, stop_loss=49000.0, take_profit=52000.0,
             strategy_class=StrategyClass.MOMENTUM_EXPANSION):
    return TriggerSignal(
        trigger_id="t001", setup_id="s001", symbol=symbol,
        direction=direction, strategy_name="Test",
        strategy_class=strategy_class,
        entry_price=entry_price, stop_loss=stop_loss, take_profit=take_profit,
        atr_value=500.0, strength=strength, trigger_quality=trigger_quality,
        regime=regime, regime_confidence=regime_confidence,
        setup_timeframe="30m", trigger_timeframe="5m",
        trigger_candle_ts=NOW_MS, setup_candle_ts=NOW_MS,
        lifecycle=TriggerLifecycle.EVALUATED,
        created_at_ms=NOW_MS, max_age_ms=300_000,
        candle_trace_ids=("c1",), setup_trace_ids=("s1",),
    )


def _snap(available=50000.0, total=100000.0, heat=0.02):
    return PortfolioSnapshot(
        capital=CapitalSnapshot(
            total_capital=total, available_capital=available,
            reserved_capital=total - available, equity=total,
            peak_equity=total, drawdown_pct=0.0,
            realized_pnl_today=0.0, total_realized_pnl=0.0,
            total_fees=0.0, trade_count_today=0, consecutive_losses=0,
        ),
        exposure=ExposureSnapshot(
            per_symbol={}, portfolio_heat=heat,
            long_exposure=0.0, short_exposure=0.0, net_exposure=0.0,
        ),
        open_positions=(), open_position_count=0,
    )


def _mock_sizer(size=500.0, qty=0.01, risk=50.0):
    sizer = MagicMock()
    sizer.calculate.return_value = {
        "size_usdt": size, "quantity": qty, "risk_usdt": risk,
    }
    return sizer


def _mock_risk_engine(approve=True):
    engine = MagicMock()

    def validate(intent, snapshot, cb):
        status = DecisionStatus.APPROVED if approve else DecisionStatus.REJECTED
        return ExecutionDecision(
            decision_id=_make_id(intent.trigger_id, "risk"),
            intent_id=intent.intent_id,
            trigger_id=intent.trigger_id,
            setup_id=intent.setup_id,
            symbol=intent.symbol,
            direction=intent.direction,
            strategy_name=intent.strategy_name,
            strategy_class=intent.strategy_class,
            entry_price=intent.entry_price,
            stop_loss=intent.stop_loss,
            take_profit=intent.take_profit,
            final_size_usdt=intent.size_usdt,
            final_quantity=intent.quantity,
            risk_usdt=intent.risk_usdt,
            risk_reward_ratio=intent.risk_reward_ratio,
            regime=intent.regime,
            status=status,
            rejection_reason="" if approve else "risk_gate_reject",
            rejection_source="" if approve else "portfolio_heat",
            created_at_ms=NOW_MS,
            candle_trace_ids=intent.candle_trace_ids,
        )

    engine.validate.side_effect = validate
    return engine


def _mock_cb():
    return MagicMock()


def _mock_ks(halted=False):
    ks = MagicMock()
    ks.is_halted.return_value = halted
    return ks


def _engine(**kwargs):
    defaults = dict(
        position_sizer=_mock_sizer(),
        risk_engine=_mock_risk_engine(),
        circuit_breaker=_mock_cb(),
        kill_switch=_mock_ks(),
        now_ms_fn=lambda: NOW_MS,
    )
    defaults.update(kwargs)
    return ProcessingEngine(**defaults)


def _make_fm_suspended():
    """Create a FailureModeProtection that evaluates to SUSPENDED."""
    cfg = FailureModeConfig(
        min_trades_warning=5, min_trades_degraded=10, min_trades_suspended=15,
    )
    fmp = FailureModeProtection(config=cfg)
    for i in range(7):
        fmp.record_trade_outcome(True, "MX", "BULL", 0.5, NOW_MS + i * 1000)
    for i in range(8):
        fmp.record_trade_outcome(False, "MX", "BULL", -0.5, NOW_MS + 10000 + i * 1000)
    result = fmp.evaluate(NOW_MS + 20000)
    assert not result.passed, f"Expected SUSPENDED, got {result.severity}"
    return fmp


def _make_fm_degraded():
    """Create a FailureModeProtection that evaluates to DEGRADED."""
    cfg = FailureModeConfig(
        min_trades_warning=5, min_trades_degraded=10, min_trades_suspended=100,
    )
    fmp = FailureModeProtection(config=cfg)
    for i in range(5):
        fmp.record_trade_outcome(True, "MX", "BULL", 0.5, NOW_MS + i * 1000)
    for i in range(5):
        fmp.record_trade_outcome(False, "MX", "BULL", -0.5, NOW_MS + 10000 + i * 1000)
    result = fmp.evaluate(NOW_MS + 20000)
    assert result.severity == FailureSeverity.DEGRADED.value
    return fmp


def _make_ev_suspended(strategy_class="MX"):
    """Create an EdgeValidityMonitor with strategy_class SUSPENDED."""
    cfg = EdgeValidityConfig(lookback_trades=5, degrade_pf=0.80, degrade_wr=0.40,
                              suspend_pf=0.60, suspend_wr=0.30,
                              dwell_degraded_ms=0,
                              probe_entry_lookback=5, probe_entry_expectancy=-0.10)
    evm = EdgeValidityMonitor(config=cfg)
    # Force ACTIVE → DEGRADED: 5 trades, 1 win + 4 losses
    ts = NOW_MS
    evm.record_trade_outcome(strategy_class, True, 0.3, ts); ts += 1000
    for i in range(4):
        evm.record_trade_outcome(strategy_class, False, -0.5, ts); ts += 1000
    assert evm.get_state(strategy_class) == EdgeState.DEGRADED.value
    # Force DEGRADED → SUSPENDED: more losses
    for i in range(5):
        evm.record_trade_outcome(strategy_class, False, -0.5, ts); ts += 1000
    assert evm.get_state(strategy_class) == EdgeState.SUSPENDED.value
    return evm


def _make_ev_degraded(strategy_class="MX"):
    """Create an EdgeValidityMonitor with strategy_class DEGRADED."""
    cfg = EdgeValidityConfig(lookback_trades=5, degrade_pf=0.80, degrade_wr=0.40)
    evm = EdgeValidityMonitor(config=cfg)
    ts = NOW_MS
    evm.record_trade_outcome(strategy_class, True, 0.3, ts); ts += 1000
    for i in range(4):
        evm.record_trade_outcome(strategy_class, False, -0.5, ts); ts += 1000
    assert evm.get_state(strategy_class) == EdgeState.DEGRADED.value
    return evm


# ── Construction (3) ────────────────────────────────────────────

class TestWave2Construction:
    def test_engine_with_no_wave2(self):
        """Engine works with no Wave 2 modules."""
        eng = _engine()
        assert eng._failure_mode is None
        assert eng._edge_validity is None

    def test_engine_with_failure_mode_only(self):
        fmp = FailureModeProtection()
        eng = _engine(failure_mode_protection=fmp)
        assert eng._failure_mode is fmp
        assert eng._edge_validity is None

    def test_engine_with_both_wave2(self):
        fmp = FailureModeProtection()
        evm = EdgeValidityMonitor()
        eng = _engine(failure_mode_protection=fmp, edge_validity_monitor=evm)
        assert eng._failure_mode is fmp
        assert eng._edge_validity is evm


# ── FailureMode Rejection (3) ──────────────────────────────────

class TestFailureModeRejection:
    def test_suspended_rejects_trade(self):
        fmp = _make_fm_suspended()
        eng = _engine(failure_mode_protection=fmp)
        decision = eng.process(_trigger(), _snap(), 50000.0, now_ms=NOW_MS + 20000)
        assert decision.status == DecisionStatus.REJECTED
        assert decision.rejection_source == REJECTION_FAILURE_MODE

    def test_normal_passes_through(self):
        fmp = FailureModeProtection()
        eng = _engine(failure_mode_protection=fmp)
        decision = eng.process(_trigger(), _snap(), 50000.0, now_ms=NOW_MS)
        assert decision.status == DecisionStatus.APPROVED

    def test_degraded_passes_with_reduced_exposure(self):
        fmp = _make_fm_degraded()
        eng = _engine(failure_mode_protection=fmp)
        decision = eng.process(_trigger(), _snap(), 50000.0, now_ms=NOW_MS + 20000)
        assert decision.status == DecisionStatus.APPROVED
        # Verify sizing was reduced
        assert decision.final_size_usdt == pytest.approx(500.0 * 0.80, rel=0.01)


# ── EdgeValidity Rejection (3) ────────────────────────────────

class TestEdgeValidityRejection:
    def test_suspended_rejects_trade(self):
        evm = _make_ev_suspended()
        eng = _engine(edge_validity_monitor=evm)
        decision = eng.process(_trigger(), _snap(), 50000.0, now_ms=NOW_MS + 50000)
        assert decision.status == DecisionStatus.REJECTED
        assert decision.rejection_source == REJECTION_EDGE_VALIDITY

    def test_active_passes_through(self):
        evm = EdgeValidityMonitor()
        eng = _engine(edge_validity_monitor=evm)
        decision = eng.process(_trigger(), _snap(), 50000.0, now_ms=NOW_MS)
        assert decision.status == DecisionStatus.APPROVED

    def test_degraded_passes_with_reduced_exposure(self):
        evm = _make_ev_degraded()
        eng = _engine(edge_validity_monitor=evm)
        decision = eng.process(_trigger(), _snap(), 50000.0, now_ms=NOW_MS + 50000)
        assert decision.status == DecisionStatus.APPROVED
        assert decision.final_size_usdt == pytest.approx(500.0 * 0.70, rel=0.01)


# ── Exposure Multiplier Stacking (4) ──────────────────────────

class TestExposureStacking:
    def test_both_degraded_stacks(self):
        """FM DEGRADED (0.8) × EV DEGRADED (0.7) = 0.56."""
        fmp = _make_fm_degraded()
        evm = _make_ev_degraded()
        eng = _engine(failure_mode_protection=fmp, edge_validity_monitor=evm)
        decision = eng.process(_trigger(), _snap(), 50000.0, now_ms=NOW_MS + 50000)
        assert decision.status == DecisionStatus.APPROVED
        expected = 500.0 * 0.80 * 0.70
        assert decision.final_size_usdt == pytest.approx(expected, rel=0.01)

    def test_fm_normal_ev_degraded(self):
        """FM normal (1.0) × EV DEGRADED (0.7) = 0.70."""
        fmp = FailureModeProtection()
        evm = _make_ev_degraded()
        eng = _engine(failure_mode_protection=fmp, edge_validity_monitor=evm)
        decision = eng.process(_trigger(), _snap(), 50000.0, now_ms=NOW_MS + 50000)
        assert decision.status == DecisionStatus.APPROVED
        assert decision.final_size_usdt == pytest.approx(500.0 * 0.70, rel=0.01)

    def test_fm_degraded_ev_normal(self):
        """FM DEGRADED (0.8) × EV normal (1.0) = 0.80."""
        fmp = _make_fm_degraded()
        evm = EdgeValidityMonitor()
        eng = _engine(failure_mode_protection=fmp, edge_validity_monitor=evm)
        decision = eng.process(_trigger(), _snap(), 50000.0, now_ms=NOW_MS + 20000)
        assert decision.status == DecisionStatus.APPROVED
        assert decision.final_size_usdt == pytest.approx(500.0 * 0.80, rel=0.01)

    def test_both_normal_no_reduction(self):
        """FM normal (1.0) × EV normal (1.0) = 1.0."""
        fmp = FailureModeProtection()
        evm = EdgeValidityMonitor()
        eng = _engine(failure_mode_protection=fmp, edge_validity_monitor=evm)
        decision = eng.process(_trigger(), _snap(), 50000.0, now_ms=NOW_MS)
        assert decision.status == DecisionStatus.APPROVED
        assert decision.final_size_usdt == pytest.approx(500.0, rel=0.01)


# ── Rejection Precedence (4) ──────────────────────────────────

class TestRejectionPrecedence:
    def test_kill_switch_before_failure_mode(self):
        fmp = _make_fm_suspended()
        eng = _engine(failure_mode_protection=fmp, kill_switch=_mock_ks(halted=True))
        decision = eng.process(_trigger(), _snap(), 50000.0, now_ms=NOW_MS + 20000)
        assert decision.rejection_source == RejectionSource.KILL_SWITCH.value

    def test_failure_mode_before_edge_validity(self):
        """FM rejects before EV is even evaluated."""
        fmp = _make_fm_suspended()
        evm = _make_ev_suspended()
        eng = _engine(failure_mode_protection=fmp, edge_validity_monitor=evm)
        decision = eng.process(_trigger(), _snap(), 50000.0, now_ms=NOW_MS + 50000)
        assert decision.rejection_source == REJECTION_FAILURE_MODE

    def test_edge_validity_before_sizing(self):
        """EV rejection happens before position sizer is called."""
        evm = _make_ev_suspended()
        sizer = _mock_sizer()
        eng = _engine(edge_validity_monitor=evm, position_sizer=sizer)
        decision = eng.process(_trigger(), _snap(), 50000.0, now_ms=NOW_MS + 50000)
        assert decision.rejection_source == REJECTION_EDGE_VALIDITY
        sizer.calculate.assert_not_called()

    def test_failure_mode_before_sizing(self):
        """FM rejection happens before position sizer is called."""
        fmp = _make_fm_suspended()
        sizer = _mock_sizer()
        eng = _engine(failure_mode_protection=fmp, position_sizer=sizer)
        decision = eng.process(_trigger(), _snap(), 50000.0, now_ms=NOW_MS + 20000)
        assert decision.rejection_source == REJECTION_FAILURE_MODE
        sizer.calculate.assert_not_called()


# ── PipelineContext Wave 2 Fields (4) ──────────────────────────

class TestPipelineContextWave2:
    def test_failure_mode_in_context(self):
        fmp = FailureModeProtection()
        eng = _engine(failure_mode_protection=fmp)
        eng.process(_trigger(), _snap(), 50000.0, now_ms=NOW_MS)
        ctx = eng.last_pipeline_context
        assert ctx.failure_mode is not None
        assert isinstance(ctx.failure_mode, FailureModeResult)

    def test_edge_validity_in_context(self):
        evm = EdgeValidityMonitor()
        eng = _engine(edge_validity_monitor=evm)
        eng.process(_trigger(), _snap(), 50000.0, now_ms=NOW_MS)
        ctx = eng.last_pipeline_context
        assert ctx.edge_validity is not None
        assert isinstance(ctx.edge_validity, EdgeValidityResult)

    def test_both_wave2_in_context(self):
        fmp = FailureModeProtection()
        evm = EdgeValidityMonitor()
        eng = _engine(failure_mode_protection=fmp, edge_validity_monitor=evm)
        eng.process(_trigger(), _snap(), 50000.0, now_ms=NOW_MS)
        ctx = eng.last_pipeline_context
        assert ctx.failure_mode is not None
        assert ctx.edge_validity is not None

    def test_wave2_absent_context_is_none(self):
        eng = _engine()
        eng.process(_trigger(), _snap(), 50000.0, now_ms=NOW_MS)
        ctx = eng.last_pipeline_context
        assert ctx.failure_mode is None
        assert ctx.edge_validity is None


# ── Backward Compatibility: Wave 2 absent (3) ──────────────────

class TestBackwardCompatibility:
    def test_no_wave2_approved(self):
        eng = _engine()
        decision = eng.process(_trigger(), _snap(), 50000.0, now_ms=NOW_MS)
        assert decision.status == DecisionStatus.APPROVED
        assert decision.final_size_usdt == pytest.approx(500.0, rel=0.01)

    def test_no_wave2_context_has_no_wave2_fields(self):
        eng = _engine()
        eng.process(_trigger(), _snap(), 50000.0, now_ms=NOW_MS)
        ctx_dict = eng.last_pipeline_context.to_dict()
        assert "failure_mode" not in ctx_dict
        assert "edge_validity" not in ctx_dict

    def test_wave1_pipeline_unchanged_with_wave2(self):
        """Adding Wave 2 modules doesn't change Wave 1 behavior."""
        eng_v1 = _engine()
        eng_v2 = _engine(
            failure_mode_protection=FailureModeProtection(),
            edge_validity_monitor=EdgeValidityMonitor(),
        )
        t = _trigger()
        s = _snap()
        d1 = eng_v1.process(t, s, 50000.0, now_ms=NOW_MS)
        d2 = eng_v2.process(t, s, 50000.0, now_ms=NOW_MS)
        # Both should approve with same sizing
        assert d1.status == d2.status
        assert d1.final_size_usdt == d2.final_size_usdt


# ── Wave 2 + Wave 1 Combined (4) ──────────────────────────────

class TestWave1Wave2Combined:
    def _make_tqs(self, score=0.7, passed=True):
        tqs = MagicMock()
        result = TQSResult(
            score=score, setup_quality=0.5, trigger_strength=0.5,
            market_context=0.5, execution_context=0.5, passed=passed,
            reason="" if passed else "TQS too low",
        )
        tqs.evaluate.return_value = result
        return tqs

    def test_tqs_rejection_before_wave2(self):
        """TQS (Step 4) rejects before FM (W2a) is evaluated."""
        tqs = self._make_tqs(score=0.1, passed=False)
        fmp = FailureModeProtection()
        eng = _engine(tqs_scorer=tqs, failure_mode_protection=fmp)
        decision = eng.process(_trigger(), _snap(), 50000.0, now_ms=NOW_MS)
        assert decision.rejection_source == REJECTION_TQS_FLOOR
        ctx = eng.last_pipeline_context
        assert ctx.failure_mode is None  # FM never ran

    def test_wave2_runs_after_tqs_pass(self):
        tqs = self._make_tqs(score=0.7, passed=True)
        fmp = FailureModeProtection()
        eng = _engine(tqs_scorer=tqs, failure_mode_protection=fmp)
        eng.process(_trigger(), _snap(), 50000.0, now_ms=NOW_MS)
        ctx = eng.last_pipeline_context
        assert ctx.tqs is not None
        assert ctx.failure_mode is not None

    def test_fm_suspended_with_tqs_pass(self):
        """TQS passes, but FM blocks."""
        tqs = self._make_tqs(score=0.7, passed=True)
        fmp = _make_fm_suspended()
        eng = _engine(tqs_scorer=tqs, failure_mode_protection=fmp)
        decision = eng.process(_trigger(), _snap(), 50000.0, now_ms=NOW_MS + 20000)
        assert decision.rejection_source == REJECTION_FAILURE_MODE

    def test_full_pipeline_with_wave1_and_wave2(self):
        """All modules present, all passing."""
        tqs = self._make_tqs(score=0.7, passed=True)
        fmp = FailureModeProtection()
        evm = EdgeValidityMonitor()
        eng = _engine(
            tqs_scorer=tqs,
            failure_mode_protection=fmp,
            edge_validity_monitor=evm,
        )
        decision = eng.process(_trigger(), _snap(), 50000.0, now_ms=NOW_MS)
        assert decision.status == DecisionStatus.APPROVED
        ctx = eng.last_pipeline_context
        assert ctx.tqs is not None
        assert ctx.failure_mode is not None
        assert ctx.edge_validity is not None


# ── Decision Hashing (3) ──────────────────────────────────────

class TestDecisionHashingWave2:
    def test_hash_includes_wave2(self):
        """Hash changes when Wave 2 modules are added."""
        eng1 = _engine()
        eng2 = _engine(
            failure_mode_protection=FailureModeProtection(),
            edge_validity_monitor=EdgeValidityMonitor(),
        )
        t = _trigger()
        s = _snap()
        eng1.process(t, s, 50000.0, now_ms=NOW_MS)
        eng2.process(t, s, 50000.0, now_ms=NOW_MS)
        # Hash should differ because ctx includes Wave 2 data
        assert eng1.last_decision_hash != eng2.last_decision_hash

    def test_hash_deterministic(self):
        """Same inputs → same hash."""
        fmp = FailureModeProtection()
        evm = EdgeValidityMonitor()
        eng = _engine(failure_mode_protection=fmp, edge_validity_monitor=evm)
        t = _trigger()
        s = _snap()
        eng.process(t, s, 50000.0, now_ms=NOW_MS)
        h1 = eng.last_decision_hash
        # Re-process with fresh modules (same state)
        fmp2 = FailureModeProtection()
        evm2 = EdgeValidityMonitor()
        eng2 = _engine(failure_mode_protection=fmp2, edge_validity_monitor=evm2)
        eng2.process(t, s, 50000.0, now_ms=NOW_MS)
        h2 = eng2.last_decision_hash
        assert h1 == h2

    def test_hash_changes_with_different_fm_state(self):
        """Different FM state → different hash."""
        fmp1 = FailureModeProtection()
        fmp2 = FailureModeProtection()
        fmp2.record_trade_outcome(False, "MX", "BULL", -0.5, NOW_MS - 1000)
        eng1 = _engine(failure_mode_protection=fmp1)
        eng2 = _engine(failure_mode_protection=fmp2)
        t = _trigger()
        s = _snap()
        eng1.process(t, s, 50000.0, now_ms=NOW_MS)
        eng2.process(t, s, 50000.0, now_ms=NOW_MS)
        assert eng1.last_decision_hash != eng2.last_decision_hash


# ── No PySide6 Imports (3) ────────────────────────────────────

class TestNoPySide6:
    def _check_module_for_pyside6(self, module_path):
        with open(module_path) as f:
            for line in f:
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                if "PySide6" in stripped:
                    return True
        return False

    def test_failure_mode_no_pyside6(self):
        import core.intraday.protection.failure_mode_protection as mod
        path = inspect.getfile(mod)
        assert not self._check_module_for_pyside6(path)

    def test_edge_validity_no_pyside6(self):
        import core.intraday.monitoring.edge_validity_monitor as mod
        path = inspect.getfile(mod)
        assert not self._check_module_for_pyside6(path)

    def test_pipeline_context_no_pyside6(self):
        import core.intraday.pipeline_context as mod
        path = inspect.getfile(mod)
        assert not self._check_module_for_pyside6(path)


# ── Wave 2 Does Not Mutate Wave 1 Pipeline Order (2) ──────────

class TestWave1PipelineOrder:
    def test_wave1_rejection_constants_unchanged(self):
        assert REJECTION_TQS_FLOOR == "tqs_floor"
        assert REJECTION_GLOBAL_FILTER == "global_filter"

    def test_wave2_rejection_constants_exist(self):
        assert REJECTION_FAILURE_MODE == "failure_mode_suspended"
        assert REJECTION_EDGE_VALIDITY == "edge_validity_suspended"


# ── Safety Proof: Wave 2 Never Bypasses Risk Engine (2) ────────

class TestSafetyProof:
    def test_risk_engine_always_called_when_approved(self):
        """Even with Wave 2 passing, risk engine still validates."""
        fmp = FailureModeProtection()
        evm = EdgeValidityMonitor()
        risk = _mock_risk_engine(approve=True)
        eng = _engine(
            failure_mode_protection=fmp,
            edge_validity_monitor=evm,
            risk_engine=risk,
        )
        eng.process(_trigger(), _snap(), 50000.0, now_ms=NOW_MS)
        risk.validate.assert_called_once()

    def test_risk_engine_not_called_on_wave2_rejection(self):
        """When Wave 2 rejects, risk engine is not reached."""
        fmp = _make_fm_suspended()
        risk = _mock_risk_engine()
        eng = _engine(failure_mode_protection=fmp, risk_engine=risk)
        decision = eng.process(_trigger(), _snap(), 50000.0, now_ms=NOW_MS + 20000)
        assert decision.status == DecisionStatus.REJECTED
        risk.validate.assert_not_called()
