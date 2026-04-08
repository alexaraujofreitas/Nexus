"""
tests/test_phase5b_wave2_edge_validity.py — Edge Validity Monitor Tests
=======================================================================
45 tests covering:
  - Configuration defaults & immutability (3)
  - Metric computation (4)
  - State machine: ACTIVE → DEGRADED (3)
  - State machine: DEGRADED → SUSPENDED (3)
  - State machine: DEGRADED → ACTIVE (recovery / hysteresis) (3)
  - State machine: SUSPENDED → PROBE (3)
  - State machine: PROBE → ACTIVE (3)
  - State machine: PROBE → SUSPENDED (3)
  - Dwell time enforcement (4)
  - Probe trade limit (3)
  - Global win tracking (2)
  - Evaluate read-only (2)
  - Event sourcing: replay consistency (3)
  - Persistence: to_json / from_json / fail-closed (4)
  - Retention / truncation (2)
"""
import json
import pytest

from core.intraday.monitoring.edge_validity_monitor import (
    EdgeEvent,
    EdgeState,
    EdgeValidityConfig,
    EdgeValidityMonitor,
    EdgeValidityResult,
    EdgeValiditySnapshot,
    EdgeValidityStateCorruptError,
    _MS_PER_HOUR,
    _MS_PER_DAY,
)

# ── Constants ───────────────────────────────────────────────────
NOW_MS = 1_000_000_000_000
HOUR = _MS_PER_HOUR
DAY = _MS_PER_DAY
SC = "MX"  # Strategy class shorthand


# ── Helpers ─────────────────────────────────────────────────────

def _make_evm(config=None):
    return EdgeValidityMonitor(config=config)


def _record_outcomes(evm, strategy, wins, losses, start_ms=NOW_MS,
                     win_pnl=1.0, loss_pnl=-1.0):
    """Record a mix of wins then losses."""
    ts = start_ms
    for _ in range(wins):
        evm.record_trade_outcome(strategy, True, win_pnl, ts)
        ts += 1000
    for _ in range(losses):
        evm.record_trade_outcome(strategy, False, loss_pnl, ts)
        ts += 1000
    return ts


def _force_degraded(evm, sc, start_ms=NOW_MS):
    """Drive a strategy to DEGRADED state."""
    cfg = evm.config
    # Need lookback_trades outcomes with PF < 0.80 AND WR < 40%
    # 20 trades: 6 wins + 14 losses → WR=30%, PF depends on pnl
    ts = start_ms
    for i in range(6):
        evm.record_trade_outcome(sc, True, 0.5, ts)
        ts += 1000
    for i in range(14):
        evm.record_trade_outcome(sc, False, -0.5, ts)
        ts += 1000
    assert evm.get_state(sc) == EdgeState.DEGRADED.value
    return ts


def _force_suspended(evm, sc, start_ms=NOW_MS):
    """Drive a strategy to SUSPENDED state via DEGRADED."""
    ts = _force_degraded(evm, sc, start_ms)
    # Wait past dwell time for DEGRADED
    ts += evm.config.dwell_degraded_ms + 1
    # Add more terrible outcomes to push PF < 0.60 AND WR < 30%
    for i in range(20):
        evm.record_trade_outcome(sc, False, -0.5, ts)
        ts += 1000
    assert evm.get_state(sc) == EdgeState.SUSPENDED.value
    return ts


def _seed_recovery_outcomes(evm, sc, ts):
    """Add class-local outcomes with improving expectancy for probe entry.
    probe_entry_lookback=5, probe_entry_expectancy=-0.10.

    Critical: the threshold must cross on the LAST (5th) trade, not earlier.
    If it crosses earlier, remaining trades leak into probe_outcomes and
    contaminate probe evaluation counts.

    4 small losses(-0.1) then 1 win(1.0):
      After trade 4: last-5 avg = (-0.5 -0.1 -0.1 -0.1 -0.1)/5 = -0.18 < -0.10 → no
      After trade 5: last-5 avg = (-0.1 -0.1 -0.1 -0.1 +1.0)/5 = +0.12 >= -0.10 → PROBE
    """
    for _ in range(4):
        evm.record_trade_outcome(sc, False, -0.1, ts)
        ts += 1000
    evm.record_trade_outcome(sc, True, 1.0, ts)
    ts += 1000
    return ts


# ── Configuration (3) ──────────────────────────────────────────

class TestEdgeValidityConfig:
    def test_defaults(self):
        cfg = EdgeValidityConfig()
        assert cfg.lookback_trades == 20
        assert cfg.probe_lookback_trades == 5
        assert cfg.degrade_pf == 0.80
        assert cfg.degrade_wr == 0.40
        assert cfg.recover_pf == 1.10
        assert cfg.recover_wr == 0.48
        assert cfg.degraded_multiplier == 0.70
        assert cfg.probe_multiplier == 0.50
        assert cfg.probe_max_trades == 5
        assert cfg.dwell_degraded_ms == 30 * 60 * 1000
        assert cfg.dwell_suspended_ms == 2 * HOUR
        assert cfg.probe_entry_lookback == 5
        assert cfg.probe_entry_expectancy == -0.10

    def test_frozen(self):
        cfg = EdgeValidityConfig()
        with pytest.raises(AttributeError):
            cfg.lookback_trades = 99

    def test_custom_config(self):
        cfg = EdgeValidityConfig(lookback_trades=10, degrade_pf=0.50)
        assert cfg.lookback_trades == 10
        assert cfg.degrade_pf == 0.50


# ── Metric Computation (4) ─────────────────────────────────────

class TestMetricComputation:
    def test_empty_outcomes_neutral(self):
        pf, wr, exp = EdgeValidityMonitor._compute_metrics([], 20)
        assert pf == 1.0
        assert wr == 0.5
        assert exp == 0.0

    def test_all_wins(self):
        outcomes = [{"is_win": True, "pnl_pct": 1.0, "ts": i} for i in range(5)]
        pf, wr, exp = EdgeValidityMonitor._compute_metrics(outcomes, 20)
        assert wr == 1.0
        assert pf == 10.0  # Capped at 10 when no losses
        assert exp == 1.0

    def test_mixed_outcomes(self):
        outcomes = []
        for i in range(6):
            outcomes.append({"is_win": True, "pnl_pct": 2.0, "ts": i})
        for i in range(4):
            outcomes.append({"is_win": False, "pnl_pct": -1.0, "ts": 10 + i})
        pf, wr, exp = EdgeValidityMonitor._compute_metrics(outcomes, 20)
        assert wr == 0.6
        assert pf == pytest.approx(12.0 / 4.0)  # 3.0
        assert exp == pytest.approx((12.0 - 4.0) / 10)  # 0.8

    def test_lookback_window_respected(self):
        # 30 outcomes, lookback=10 → only last 10 used
        outcomes = [{"is_win": True, "pnl_pct": 1.0, "ts": i} for i in range(20)]
        outcomes += [{"is_win": False, "pnl_pct": -1.0, "ts": 20 + i} for i in range(10)]
        pf, wr, exp = EdgeValidityMonitor._compute_metrics(outcomes, 10)
        assert wr == 0.0  # Last 10 are all losses
        assert exp == -1.0


# ── ACTIVE → DEGRADED (3) ────────────────────────────────────

class TestActiveToDegraded:
    def test_no_transition_under_lookback(self):
        evm = _make_evm()
        # 10 trades (below lookback=20): all losses
        _record_outcomes(evm, SC, 0, 10)
        assert evm.get_state(SC) == EdgeState.ACTIVE.value

    def test_transition_at_lookback(self):
        evm = _make_evm()
        # 20 trades: 6 wins + 14 losses → WR=30% < 40%, PF low
        _record_outcomes(evm, SC, 6, 14, win_pnl=0.5, loss_pnl=-0.5)
        assert evm.get_state(SC) == EdgeState.DEGRADED.value

    def test_no_transition_when_wr_above_threshold(self):
        evm = _make_evm()
        # 20 trades: 10 wins + 10 losses → WR=50% > 40%
        _record_outcomes(evm, SC, 10, 10)
        assert evm.get_state(SC) == EdgeState.ACTIVE.value


# ── DEGRADED → SUSPENDED (3) ─────────────────────────────────

class TestDegradedToSuspended:
    def test_transition_after_dwell(self):
        evm = _make_evm()
        ts = _force_degraded(evm, SC)
        # Wait past DEGRADED dwell time
        ts += evm.config.dwell_degraded_ms + 1
        # Add more losses to push PF < 0.60 AND WR < 30%
        for i in range(20):
            evm.record_trade_outcome(SC, False, -0.5, ts)
            ts += 1000
        assert evm.get_state(SC) == EdgeState.SUSPENDED.value

    def test_no_transition_during_dwell(self):
        evm = _make_evm()
        ts = _force_degraded(evm, SC)
        # Within dwell time — add terrible trades
        for i in range(20):
            evm.record_trade_outcome(SC, False, -0.5, ts + i * 100)
        assert evm.get_state(SC) == EdgeState.DEGRADED.value  # Dwell not met

    def test_no_transition_when_pf_above_threshold(self):
        evm = _make_evm()
        ts = _force_degraded(evm, SC)
        ts += evm.config.dwell_degraded_ms + 1
        # Add some wins to keep PF above 0.60
        for i in range(10):
            evm.record_trade_outcome(SC, True, 2.0, ts)
            ts += 1000
        for i in range(5):
            evm.record_trade_outcome(SC, False, -0.5, ts)
            ts += 1000
        # PF should be decent, should recover to ACTIVE instead
        state = evm.get_state(SC)
        assert state != EdgeState.SUSPENDED.value


# ── DEGRADED → ACTIVE Recovery / Hysteresis (3) ────────────────

class TestDegradedToActive:
    def test_recovery_when_metrics_meet_threshold(self):
        evm = _make_evm()
        ts = _force_degraded(evm, SC)
        ts += evm.config.dwell_degraded_ms + 1
        # Add strong winning trades to push PF >= 1.10 AND WR >= 48%
        for i in range(20):
            win = i < 12  # 12/20 = 60% WR > 48%
            evm.record_trade_outcome(SC, win, 2.0 if win else -0.5, ts)
            ts += 1000
        assert evm.get_state(SC) == EdgeState.ACTIVE.value

    def test_no_recovery_during_dwell(self):
        evm = _make_evm()
        ts = _force_degraded(evm, SC)
        # Within dwell time — add strong trades
        for i in range(20):
            evm.record_trade_outcome(SC, True, 2.0, ts + i * 100)
        assert evm.get_state(SC) == EdgeState.DEGRADED.value  # Dwell not met

    def test_hysteresis_prevents_oscillation(self):
        """Degrade threshold (PF<0.80) is lower than recovery (PF>=1.10).
        A PF between 0.80 and 1.10 stays DEGRADED — that's the hysteresis band."""
        evm = _make_evm()
        ts = _force_degraded(evm, SC)
        ts += evm.config.dwell_degraded_ms + 1
        # Add trades that bring PF to ~0.95 (above degrade=0.80, below recover=1.10)
        # and WR to ~45% (below recover_wr=48%)
        for i in range(20):
            win = i < 9  # 9/20 = 45% WR — below recover_wr=48%
            evm.record_trade_outcome(SC, win, 1.0 if win else -1.0, ts)
            ts += 1000
        # PF = 9.0/11.0 ≈ 0.818, WR = 45% — PF above degrade but WR below recover
        assert evm.get_state(SC) == EdgeState.DEGRADED.value


# ── SUSPENDED → PROBE (3) ─────────────────────────────────────

class TestSuspendedToProbe:
    def test_probe_entry_with_class_local_recovery(self):
        """Class-pure: SUSPENDED → PROBE uses only this class's recent outcomes."""
        evm = _make_evm()
        ts = _force_suspended(evm, SC)
        ts += evm.config.dwell_suspended_ms + 1
        # Add class-local outcomes with improving expectancy
        ts = _seed_recovery_outcomes(evm, SC, ts)
        assert evm.get_state(SC) == EdgeState.PROBE.value

    def test_no_probe_without_improving_expectancy(self):
        """Class stays SUSPENDED if recent expectancy is still bad."""
        evm = _make_evm()
        ts = _force_suspended(evm, SC)
        ts += evm.config.dwell_suspended_ms + 1
        # Add losses — expectancy stays negative
        for _ in range(5):
            evm.record_trade_outcome(SC, False, -0.5, ts)
            ts += 1000
        assert evm.get_state(SC) == EdgeState.SUSPENDED.value

    def test_no_probe_during_dwell(self):
        evm = _make_evm()
        ts = _force_suspended(evm, SC)
        # Within dwell time — add recovery outcomes but dwell blocks
        for _ in range(5):
            evm.record_trade_outcome(SC, True, 0.5, ts)
            ts += 100
        assert evm.get_state(SC) == EdgeState.SUSPENDED.value


# ── PROBE → ACTIVE (3) ────────────────────────────────────────

class TestProbeToActive:
    def _enter_probe(self, evm, sc):
        ts = _force_suspended(evm, sc)
        ts += evm.config.dwell_suspended_ms + 1
        ts = _seed_recovery_outcomes(evm, sc, ts)
        assert evm.get_state(sc) == EdgeState.PROBE.value
        return ts

    def test_probe_success_to_active(self):
        evm = _make_evm()
        ts = self._enter_probe(evm, SC)
        # Need 5 probe trades. The recovery outcomes that triggered PROBE
        # were applied BEFORE the transition — they don't count as probe.
        for i in range(5):
            evm.record_trade_outcome(SC, True, 2.0, ts)
            ts += 1000
        assert evm.get_state(SC) == EdgeState.ACTIVE.value

    def test_probe_needs_enough_trades(self):
        evm = _make_evm()
        ts = self._enter_probe(evm, SC)
        for i in range(3):
            evm.record_trade_outcome(SC, True, 2.0, ts + i * 1000)
        assert evm.get_state(SC) == EdgeState.PROBE.value

    def test_probe_recovery_threshold_higher_than_degrade(self):
        """probe_recover_pf=1.20 > degrade_pf=0.80 — intentional hysteresis."""
        cfg = EdgeValidityConfig()
        assert cfg.probe_recover_pf > cfg.degrade_pf
        assert cfg.probe_recover_wr > cfg.degrade_wr


# ── PROBE → SUSPENDED (3) ─────────────────────────────────────

class TestProbeToSuspended:
    def _enter_probe(self, evm, sc):
        ts = _force_suspended(evm, sc)
        ts += evm.config.dwell_suspended_ms + 1
        ts = _seed_recovery_outcomes(evm, sc, ts)
        assert evm.get_state(sc) == EdgeState.PROBE.value
        return ts

    def test_probe_failure_back_to_suspended(self):
        evm = _make_evm()
        ts = self._enter_probe(evm, SC)
        # 5 probe trades: all losses → PF=0, WR=0 → fail
        for i in range(5):
            evm.record_trade_outcome(SC, False, -1.0, ts)
            ts += 1000
        assert evm.get_state(SC) == EdgeState.SUSPENDED.value

    def test_probe_mixed_failure(self):
        evm = _make_evm()
        ts = self._enter_probe(evm, SC)
        # 5 probe trades: 1 win + 4 losses → WR=20%<35% → SUSPENDED
        evm.record_trade_outcome(SC, True, 0.3, ts); ts += 1000
        evm.record_trade_outcome(SC, False, -1.0, ts); ts += 1000
        evm.record_trade_outcome(SC, False, -1.0, ts); ts += 1000
        evm.record_trade_outcome(SC, False, -1.0, ts); ts += 1000
        evm.record_trade_outcome(SC, False, -1.0, ts); ts += 1000
        assert evm.get_state(SC) == EdgeState.SUSPENDED.value

    def test_class_pure_no_cross_class_coupling(self):
        """Probe entry for class A must NOT depend on class B outcomes."""
        evm = _make_evm()
        ts = _force_suspended(evm, SC)
        ts += evm.config.dwell_suspended_ms + 1
        # Add great outcomes for a DIFFERENT class — should not help SC
        for _ in range(10):
            evm.record_trade_outcome("VR", True, 2.0, ts)
            ts += 1000
        # SC still has terrible expectancy (no new outcomes for SC)
        assert evm.get_state(SC) == EdgeState.SUSPENDED.value


# ── Dwell Time Enforcement (4) ─────────────────────────────────

class TestDwellTimes:
    def test_degraded_dwell_30min(self):
        cfg = EdgeValidityConfig()
        assert cfg.dwell_degraded_ms == 30 * 60 * 1000

    def test_suspended_dwell_2h(self):
        cfg = EdgeValidityConfig()
        assert cfg.dwell_suspended_ms == 2 * HOUR

    def test_probe_dwell_1h(self):
        cfg = EdgeValidityConfig()
        assert cfg.dwell_probe_ms == 1 * HOUR

    def test_dwell_prevents_premature_transition(self):
        evm = _make_evm()
        ts = _force_degraded(evm, SC)
        # Immediately add recovery trades (within dwell)
        for i in range(20):
            evm.record_trade_outcome(SC, True, 2.0, ts + i * 100)
        assert evm.get_state(SC) == EdgeState.DEGRADED.value


# ── Probe Trade Limit (3) ─────────────────────────────────────

class TestProbeTradeLimit:
    def _enter_probe(self, evm, sc):
        ts = _force_suspended(evm, sc)
        ts += evm.config.dwell_suspended_ms + 1
        ts = _seed_recovery_outcomes(evm, sc, ts)
        assert evm.get_state(sc) == EdgeState.PROBE.value
        return ts

    def test_probe_limit_default_5(self):
        cfg = EdgeValidityConfig()
        assert cfg.probe_max_trades == 5

    def test_probe_limit_blocks_after_max(self):
        evm = _make_evm()
        ts = self._enter_probe(evm, SC)
        # Record 5 trades that keep PF in the middle zone (not recover, not fail)
        # 3 wins + 2 losses → WR=60%>=50%, but PF depends on amounts
        # Use win_pnl=0.5, loss_pnl=-0.5 → PF = 1.5/1.0 = 1.5 ≥ 1.20 → ACTIVE
        # Instead make them ambiguous: 2 wins + 3 losses → WR=40%<50% but PF?
        # wins=0.5*2=1.0, losses=0.5*3=1.5 → PF=0.67<0.80 → SUSPENDED
        # We need to stay in PROBE. Use exactly 5 trades with WR≥35% and PF≥0.80
        # but WR<50% or PF<1.20 so we don't recover
        # 3 wins + 2 losses: WR=60% PF depends
        # win_pnl=0.4, loss_pnl=-0.5: PF=1.2/1.0=1.2 → PF≥1.20 AND WR≥50% → ACTIVE
        # Need to NOT trigger either ACTIVE or SUSPENDED after 5 trades.
        # But the code evaluates after each trade — at trade 5, it will decide.
        # The only way to stay in PROBE with 5 trades is if neither condition met.
        # probe_recover: PF >= 1.20 AND WR >= 50%
        # probe_fail: PF < 0.80 OR WR < 35%
        # So we need: PF in [0.80, 1.20) AND WR in [35%, 50%)
        # 2 wins + 3 losses: WR=40% (in range). win=1.0 loss=-1.0: PF=2/3=0.67<0.80 FAIL
        # 2 wins + 3 losses: win=2.0 loss=-1.0: PF=4/3=1.33≥1.20 but WR=40%<50% → neither
        # Actually probe_recover requires BOTH, probe_fail requires OR
        # PF=1.33≥1.20 but WR=40%<50%: not recover. PF=1.33≥0.80 and WR=40%≥35%: not fail.
        # → stays PROBE! After 5 trades, probe_trade_count=5 → evaluate blocks.
        evm.record_trade_outcome(SC, True, 2.0, ts); ts += 1000
        evm.record_trade_outcome(SC, True, 2.0, ts); ts += 1000
        evm.record_trade_outcome(SC, False, -1.0, ts); ts += 1000
        evm.record_trade_outcome(SC, False, -1.0, ts); ts += 1000
        evm.record_trade_outcome(SC, False, -1.0, ts); ts += 1000
        assert evm.get_state(SC) == EdgeState.PROBE.value
        result = evm.evaluate(SC, ts)
        assert not result.passed

    def test_probe_exposure_multiplier(self):
        evm = _make_evm()
        ts = self._enter_probe(evm, SC)
        result = evm.evaluate(SC, ts)
        assert result.exposure_multiplier == 0.50


# ── Class-Pure Recovery (2) ───────────────────────────────────

class TestClassPureRecovery:
    def test_class_local_expectancy_drives_probe_entry(self):
        """Probe entry depends only on this class's recent outcomes."""
        evm = _make_evm()
        ts = _force_suspended(evm, SC)
        ts += evm.config.dwell_suspended_ms + 1
        # Add improving class-local outcomes
        ts = _seed_recovery_outcomes(evm, SC, ts)
        assert evm.get_state(SC) == EdgeState.PROBE.value

    def test_bad_class_outcomes_block_probe(self):
        """Class stays SUSPENDED if its own expectancy remains bad."""
        evm = _make_evm()
        ts = _force_suspended(evm, SC)
        ts += evm.config.dwell_suspended_ms + 1
        # Class outcomes still bad
        for _ in range(5):
            evm.record_trade_outcome(SC, False, -0.5, ts)
            ts += 1000
        assert evm.get_state(SC) == EdgeState.SUSPENDED.value


# ── Evaluate Read-Only (2) ────────────────────────────────────

class TestEvaluateReadOnly:
    def test_evaluate_does_not_mutate_state(self):
        evm = _make_evm()
        _record_outcomes(evm, SC, 5, 5)
        state_before = evm.get_state(SC)
        count_before = evm.event_count
        evm.evaluate(SC, NOW_MS + 100000)
        assert evm.get_state(SC) == state_before
        assert evm.event_count == count_before

    def test_evaluate_active_full_exposure(self):
        evm = _make_evm()
        result = evm.evaluate(SC, NOW_MS)
        assert result.passed is True
        assert result.state == EdgeState.ACTIVE.value
        assert result.exposure_multiplier == 1.0


# ── Replay Consistency (3) ────────────────────────────────────

class TestEdgeReplay:
    def test_replay_produces_identical_state(self):
        evm1 = _make_evm()
        _record_outcomes(evm1, SC, 5, 15)  # Force degraded
        events = evm1.event_log
        evm2 = _make_evm()
        evm2.replay(events)
        assert evm1.get_state(SC) == evm2.get_state(SC)
        r1 = evm1.evaluate(SC, NOW_MS + 50000)
        r2 = evm2.evaluate(SC, NOW_MS + 50000)
        assert r1.state == r2.state
        assert r1.exposure_multiplier == r2.exposure_multiplier

    def test_replay_clears_previous_state(self):
        evm = _make_evm()
        _record_outcomes(evm, SC, 0, 20)
        # Replay with only wins
        events = [EdgeEvent(
            event_type="trade_outcome", timestamp_ms=NOW_MS + i * 1000,
            strategy_class=SC, is_win=True, pnl_pct=1.0,
        ) for i in range(10)]
        evm.replay(events)
        assert evm.get_state(SC) == EdgeState.ACTIVE.value

    def test_double_replay_idempotent(self):
        evm = _make_evm()
        _record_outcomes(evm, SC, 8, 12)
        events = evm.event_log
        evm.replay(events)
        evm.replay(events)
        assert evm.event_count == len([e for e in events])


# ── Persistence (4) ────────────────────────────────────────────

class TestEdgePersistence:
    def test_roundtrip(self):
        evm = _make_evm()
        _record_outcomes(evm, SC, 10, 5)
        data = evm.to_json()
        restored = EdgeValidityMonitor.from_json(data)
        assert restored.get_state(SC) == evm.get_state(SC)
        assert restored.event_count > 0

    def test_corrupt_json_raises(self):
        with pytest.raises(EdgeValidityStateCorruptError, match="JSON corrupt"):
            EdgeValidityMonitor.from_json("broken {{{")

    def test_missing_version_raises(self):
        data = json.dumps({"events": []})
        with pytest.raises(EdgeValidityStateCorruptError, match="Missing 'version'"):
            EdgeValidityMonitor.from_json(data)

    def test_wrong_version_raises(self):
        data = json.dumps({"version": 999, "events": []})
        with pytest.raises(EdgeValidityStateCorruptError, match="Unknown version"):
            EdgeValidityMonitor.from_json(data)


# ── Retention / Truncation (2) ─────────────────────────────────

class TestEdgeRetention:
    def test_truncate_removes_old_events(self):
        evm = _make_evm()
        old_ts = NOW_MS - 8 * DAY
        evm.record_trade_outcome(SC, True, 1.0, old_ts)
        evm.record_trade_outcome(SC, False, -1.0, NOW_MS)
        archived = evm.truncate(NOW_MS)
        assert len(archived) >= 1

    def test_truncate_no_old_events_noop(self):
        evm = _make_evm()
        _record_outcomes(evm, SC, 5, 0)
        count_before = evm.event_count
        archived = evm.truncate(NOW_MS + 1000)
        assert len(archived) == 0
        assert evm.event_count == count_before
