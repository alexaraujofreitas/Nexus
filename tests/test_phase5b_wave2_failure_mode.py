"""
tests/test_phase5b_wave2_failure_mode.py — Failure Mode Protection Tests
========================================================================
42 tests covering:
  - Configuration defaults & immutability (3)
  - Event recording & derived state (5)
  - Loss clustering detector (5)
  - Drawdown acceleration detector (5)
  - Consecutive losses detector (5)
  - Regime mismatch detector (4)
  - Progressive tier enforcement (6)
  - Event sourcing: replay consistency (3)
  - Persistence: to_json / from_json / fail-closed (4)
  - Retention / truncation (2)
"""
import json
import pytest

from core.intraday.protection.failure_mode_protection import (
    FailureModeConfig,
    FailureModeEvent,
    FailureModeProtection,
    FailureModeResult,
    FailureModeSnapshot,
    FailureModeStateCorruptError,
    FailureSeverity,
    DetectorID,
    DetectorResult,
    _MS_PER_HOUR,
    _MS_PER_DAY,
    _RETENTION_WINDOW_MS,
)

# ── Constants ───────────────────────────────────────────────────
NOW_MS = 1_000_000_000_000
HOUR = _MS_PER_HOUR
DAY = _MS_PER_DAY


# ── Helpers ─────────────────────────────────────────────────────

def _make_fmp(config=None):
    return FailureModeProtection(config=config)


def _record_wins(fmp, n, start_ms=NOW_MS, strategy="MX", regime="BULL"):
    for i in range(n):
        fmp.record_trade_outcome(
            is_win=True, strategy_class=strategy, regime=regime,
            pnl_pct=0.5, now_ms=start_ms + i * 1000,
        )


def _record_losses(fmp, n, start_ms=NOW_MS, strategy="MX", regime="BULL"):
    for i in range(n):
        fmp.record_trade_outcome(
            is_win=False, strategy_class=strategy, regime=regime,
            pnl_pct=-0.5, now_ms=start_ms + i * 1000,
        )


def _record_dd_samples(fmp, values, start_ms=NOW_MS, interval_ms=HOUR):
    for i, v in enumerate(values):
        fmp.record_drawdown_sample(drawdown_pct=v, now_ms=start_ms + i * interval_ms)


# ── Configuration (3) ──────────────────────────────────────────

class TestFailureModeConfig:
    def test_defaults(self):
        cfg = FailureModeConfig()
        assert cfg.min_trades_warning == 20
        assert cfg.min_trades_degraded == 40
        assert cfg.min_trades_suspended == 75
        assert cfg.degraded_exposure_multiplier == 0.80
        assert cfg.loss_cluster_lookback == 10
        assert cfg.loss_cluster_threshold == 0.70
        assert cfg.consec_loss_warning == 3
        assert cfg.consec_loss_degraded == 5
        assert cfg.consec_loss_suspended == 8

    def test_frozen(self):
        cfg = FailureModeConfig()
        with pytest.raises(AttributeError):
            cfg.min_trades_warning = 99

    def test_custom_config(self):
        cfg = FailureModeConfig(min_trades_warning=5, min_trades_degraded=10, min_trades_suspended=15)
        assert cfg.min_trades_warning == 5
        assert cfg.min_trades_degraded == 10
        assert cfg.min_trades_suspended == 15


# ── Event Recording & Derived State (5) ───────────────────────

class TestEventRecording:
    def test_trade_outcome_appends_event(self):
        fmp = _make_fmp()
        fmp.record_trade_outcome(True, "MX", "BULL", 0.5, NOW_MS)
        assert fmp.event_count == 1
        assert fmp.trade_count == 1
        assert fmp.consecutive_losses == 0

    def test_loss_increments_consecutive(self):
        fmp = _make_fmp()
        _record_losses(fmp, 3)
        assert fmp.consecutive_losses == 3
        assert fmp.trade_count == 3

    def test_win_resets_consecutive(self):
        fmp = _make_fmp()
        _record_losses(fmp, 3)
        fmp.record_trade_outcome(True, "MX", "BULL", 0.5, NOW_MS + 10000)
        assert fmp.consecutive_losses == 0

    def test_drawdown_sample_appends(self):
        fmp = _make_fmp()
        fmp.record_drawdown_sample(5.0, NOW_MS)
        fmp.record_drawdown_sample(6.0, NOW_MS + 1000)
        assert fmp.event_count == 2
        assert fmp.trade_count == 0

    def test_mixed_events(self):
        fmp = _make_fmp()
        fmp.record_trade_outcome(True, "MX", "BULL", 0.5, NOW_MS)
        fmp.record_drawdown_sample(3.0, NOW_MS + 1000)
        fmp.record_trade_outcome(False, "MX", "BULL", -0.5, NOW_MS + 2000)
        assert fmp.event_count == 3
        assert fmp.trade_count == 2
        assert fmp.consecutive_losses == 1


# ── Loss Clustering Detector (5) ──────────────────────────────

class TestLossClustering:
    def test_insufficient_data_no_trigger(self):
        fmp = _make_fmp()
        _record_losses(fmp, 5)  # Below lookback of 10
        result = fmp.evaluate(NOW_MS)
        # Even though all 5 are losses, lookback=10 means the detector
        # says insufficient data — but consecutive_losses=5 fires instead
        # So test the loss_clustering detector specifically
        det = fmp._detect_loss_clustering(NOW_MS)
        assert not det.triggered

    def test_below_threshold_no_trigger(self):
        fmp = _make_fmp()
        # 10 trades: 6 wins, 4 losses => 40% loss rate < 70% threshold
        _record_wins(fmp, 6)
        _record_losses(fmp, 4, start_ms=NOW_MS + 10000)
        det = fmp._detect_loss_clustering(NOW_MS + 20000)
        assert not det.triggered

    def test_at_threshold_triggers_warning(self):
        fmp = _make_fmp()
        # 10 trades: 3 wins + 7 losses => 70% loss rate = threshold
        _record_wins(fmp, 3)
        _record_losses(fmp, 7, start_ms=NOW_MS + 10000)
        det = fmp._detect_loss_clustering(NOW_MS + 20000)
        assert det.triggered
        assert det.severity == FailureSeverity.WARNING.value

    def test_high_loss_rate_degraded(self):
        fmp = _make_fmp()
        # 10 trades: 2 wins + 8 losses => 80% >= threshold+10%
        _record_wins(fmp, 2)
        _record_losses(fmp, 8, start_ms=NOW_MS + 10000)
        det = fmp._detect_loss_clustering(NOW_MS + 20000)
        assert det.triggered
        assert det.severity == FailureSeverity.DEGRADED.value

    def test_extreme_loss_rate_suspended(self):
        fmp = _make_fmp()
        # 10 trades: 1 win + 9 losses => 90% >= 0.90
        _record_wins(fmp, 1)
        _record_losses(fmp, 9, start_ms=NOW_MS + 10000)
        det = fmp._detect_loss_clustering(NOW_MS + 20000)
        assert det.triggered
        assert det.severity == FailureSeverity.SUSPENDED.value


# ── Drawdown Acceleration Detector (5) ─────────────────────────

class TestDrawdownAcceleration:
    def test_insufficient_samples_no_trigger(self):
        fmp = _make_fmp()
        fmp.record_drawdown_sample(5.0, NOW_MS)
        det = fmp._detect_drawdown_acceleration(NOW_MS + 1000)
        assert not det.triggered

    def test_stable_drawdown_no_trigger(self):
        fmp = _make_fmp()
        # Uniform samples over 24h: short avg == long avg → ratio ~1
        base = NOW_MS - 24 * HOUR
        _record_dd_samples(fmp, [3.0] * 25, start_ms=base, interval_ms=HOUR)
        det = fmp._detect_drawdown_acceleration(NOW_MS)
        assert not det.triggered

    def test_accelerating_drawdown_triggers(self):
        fmp = _make_fmp()
        # Long window: low drawdown. Short window: high drawdown
        base = NOW_MS - 24 * HOUR
        # 20h of low DD
        _record_dd_samples(fmp, [1.0] * 20, start_ms=base, interval_ms=HOUR)
        # 4h of high DD (within short window)
        short_start = NOW_MS - 4 * HOUR
        _record_dd_samples(fmp, [5.0] * 4, start_ms=short_start, interval_ms=HOUR)
        det = fmp._detect_drawdown_acceleration(NOW_MS)
        assert det.triggered
        assert det.severity in (FailureSeverity.WARNING.value,
                                FailureSeverity.DEGRADED.value,
                                FailureSeverity.SUSPENDED.value)

    def test_near_zero_baseline_no_trigger(self):
        fmp = _make_fmp()
        base = NOW_MS - 24 * HOUR
        _record_dd_samples(fmp, [0.0005] * 20, start_ms=base, interval_ms=HOUR)
        _record_dd_samples(fmp, [0.001] * 4, start_ms=NOW_MS - 4 * HOUR, interval_ms=HOUR)
        det = fmp._detect_drawdown_acceleration(NOW_MS)
        assert not det.triggered

    def test_extreme_acceleration_suspended(self):
        fmp = _make_fmp()
        base = NOW_MS - 24 * HOUR
        # Need ratio >= threshold*2 = 5.0 for SUSPENDED
        # long_avg includes both old + new samples; old=0.5 for 20h, new=20.0 for 4h
        # long_avg = (0.5*20 + 20.0*4)/24 = (10+80)/24 = 3.75
        # short_avg = 20.0
        # ratio = 20.0/3.75 = 5.33 >= 5.0 → SUSPENDED
        _record_dd_samples(fmp, [0.5] * 20, start_ms=base, interval_ms=HOUR)
        _record_dd_samples(fmp, [20.0] * 4, start_ms=NOW_MS - 4 * HOUR, interval_ms=HOUR)
        det = fmp._detect_drawdown_acceleration(NOW_MS)
        assert det.triggered
        assert det.severity == FailureSeverity.SUSPENDED.value


# ── Consecutive Losses Detector (5) ───────────────────────────

class TestConsecutiveLosses:
    def test_no_losses_no_trigger(self):
        fmp = _make_fmp()
        _record_wins(fmp, 5)
        det = fmp._detect_consecutive_losses(NOW_MS)
        assert not det.triggered

    def test_below_warning_no_trigger(self):
        fmp = _make_fmp()
        _record_losses(fmp, 2)
        det = fmp._detect_consecutive_losses(NOW_MS)
        assert not det.triggered

    def test_warning_at_3(self):
        fmp = _make_fmp()
        _record_losses(fmp, 3)
        det = fmp._detect_consecutive_losses(NOW_MS)
        assert det.triggered
        assert det.severity == FailureSeverity.WARNING.value

    def test_degraded_at_5(self):
        fmp = _make_fmp()
        _record_losses(fmp, 5)
        det = fmp._detect_consecutive_losses(NOW_MS)
        assert det.triggered
        assert det.severity == FailureSeverity.DEGRADED.value

    def test_suspended_at_8(self):
        fmp = _make_fmp()
        _record_losses(fmp, 8)
        det = fmp._detect_consecutive_losses(NOW_MS)
        assert det.triggered
        assert det.severity == FailureSeverity.SUSPENDED.value


# ── Regime Mismatch Detector (4) ──────────────────────────────

class TestRegimeMismatch:
    def test_insufficient_data_no_trigger(self):
        fmp = _make_fmp()
        _record_wins(fmp, 5)
        det = fmp._detect_regime_mismatch(NOW_MS)
        assert not det.triggered

    def test_no_mismatch_no_trigger(self):
        fmp = _make_fmp()
        # All same regime, WR is uniform → no gap
        for i in range(20):
            fmp.record_trade_outcome(
                is_win=(i % 2 == 0), strategy_class="MX", regime="BULL",
                pnl_pct=0.5 if i % 2 == 0 else -0.5, now_ms=NOW_MS + i * 1000,
            )
        det = fmp._detect_regime_mismatch(NOW_MS + 30000)
        assert not det.triggered

    def test_mismatch_triggers(self):
        fmp = _make_fmp()
        # Overall: 15/30 = 50% WR across both regimes
        # BULL regime: 12/15 = 80% WR
        # BEAR regime: 3/15 = 20% WR → gap = 50% - 20% = 30% > 20% threshold
        for i in range(15):
            fmp.record_trade_outcome(
                is_win=(i < 12), strategy_class="MX", regime="BULL",
                pnl_pct=0.5 if i < 12 else -0.5, now_ms=NOW_MS + i * 1000,
            )
        for i in range(15):
            fmp.record_trade_outcome(
                is_win=(i < 3), strategy_class="MX", regime="BEAR",
                pnl_pct=0.5 if i < 3 else -0.5, now_ms=NOW_MS + 20000 + i * 1000,
            )
        det = fmp._detect_regime_mismatch(NOW_MS + 40000)
        assert det.triggered

    def test_regime_with_too_few_trades_ignored(self):
        fmp = _make_fmp()
        # BULL: 20 trades (enough), BEAR: 5 trades (below lookback=15)
        for i in range(20):
            fmp.record_trade_outcome(
                is_win=(i % 2 == 0), strategy_class="MX", regime="BULL",
                pnl_pct=0.5 if i % 2 == 0 else -0.5, now_ms=NOW_MS + i * 1000,
            )
        for i in range(5):
            fmp.record_trade_outcome(
                is_win=False, strategy_class="MX", regime="BEAR",
                pnl_pct=-0.5, now_ms=NOW_MS + 30000 + i * 1000,
            )
        det = fmp._detect_regime_mismatch(NOW_MS + 40000)
        # BEAR regime has only 5 trades < lookback=15, so it's ignored
        assert not det.triggered


# ── Progressive Tier Enforcement (6) ──────────────────────────

class TestProgressiveTiers:
    """Test that tiers are gated by trade count."""

    def test_normal_with_zero_trades(self):
        fmp = _make_fmp()
        result = fmp.evaluate(NOW_MS)
        assert result.passed is True
        assert result.severity == FailureSeverity.NORMAL.value
        assert result.exposure_multiplier == 1.0

    def test_detector_triggered_but_low_trade_count_stays_normal(self):
        """Even if consecutive losses detector fires, <20 trades → NORMAL."""
        fmp = _make_fmp()
        _record_losses(fmp, 5)  # consec=5 → DEGRADED-level detector
        result = fmp.evaluate(NOW_MS)
        # 5 trades < 20 → cannot reach WARNING, stays NORMAL
        assert result.severity == FailureSeverity.NORMAL.value
        assert result.passed is True
        assert result.exposure_multiplier == 1.0

    def test_warning_at_sufficient_trades(self):
        """With >= 20 trades and detector triggered → WARNING."""
        cfg = FailureModeConfig(min_trades_warning=10)
        fmp = _make_fmp(config=cfg)
        # 7 wins + 3 consecutive losses = 10 trades, consec=3 → WARNING
        _record_wins(fmp, 7)
        _record_losses(fmp, 3, start_ms=NOW_MS + 10000)
        result = fmp.evaluate(NOW_MS + 20000)
        assert result.severity == FailureSeverity.WARNING.value
        assert result.passed is True
        assert result.exposure_multiplier == 1.0

    def test_degraded_at_sufficient_trades(self):
        """With >= 40 trades and strong detector → DEGRADED (0.8x)."""
        cfg = FailureModeConfig(min_trades_warning=5, min_trades_degraded=10)
        fmp = _make_fmp(config=cfg)
        # 5 wins + 5 consecutive losses = 10 trades, consec=5 → DEGRADED-level
        _record_wins(fmp, 5)
        _record_losses(fmp, 5, start_ms=NOW_MS + 10000)
        result = fmp.evaluate(NOW_MS + 20000)
        assert result.severity == FailureSeverity.DEGRADED.value
        assert result.passed is True
        assert result.exposure_multiplier == 0.80

    def test_suspended_blocks_trades(self):
        """With >= 75 trades and extreme detector → SUSPENDED (block)."""
        cfg = FailureModeConfig(
            min_trades_warning=5, min_trades_degraded=10, min_trades_suspended=15,
        )
        fmp = _make_fmp(config=cfg)
        # 7 wins + 8 consecutive losses = 15 trades, consec=8 → SUSPENDED-level
        _record_wins(fmp, 7)
        _record_losses(fmp, 8, start_ms=NOW_MS + 10000)
        result = fmp.evaluate(NOW_MS + 20000)
        assert result.severity == FailureSeverity.SUSPENDED.value
        assert result.passed is False
        assert result.exposure_multiplier == 0.0

    def test_suspended_downgraded_to_degraded_when_low_trades(self):
        """If detector says SUSPENDED but trades < min_suspended → DEGRADED."""
        cfg = FailureModeConfig(
            min_trades_warning=5, min_trades_degraded=10, min_trades_suspended=50,
        )
        fmp = _make_fmp(config=cfg)
        # 5 wins + 8 losses = 13 trades; consec=8 → SUSPENDED-level detector
        # But 13 < 50 (min_suspended), so downgrade
        _record_wins(fmp, 5)
        _record_losses(fmp, 8, start_ms=NOW_MS + 10000)
        result = fmp.evaluate(NOW_MS + 20000)
        # 13 >= 10 (min_degraded), so stays DEGRADED
        assert result.severity == FailureSeverity.DEGRADED.value
        assert result.passed is True


# ── Event Sourcing: Replay Consistency (3) ─────────────────────

class TestReplayConsistency:
    def test_replay_produces_identical_state(self):
        fmp1 = _make_fmp()
        _record_wins(fmp1, 10)
        _record_losses(fmp1, 5, start_ms=NOW_MS + 20000)
        fmp1.record_drawdown_sample(3.5, NOW_MS + 30000)

        # Replay from event log
        events = fmp1.event_log
        fmp2 = _make_fmp()
        fmp2.replay(events)

        assert fmp2.trade_count == fmp1.trade_count
        assert fmp2.consecutive_losses == fmp1.consecutive_losses
        assert fmp2.event_count == fmp1.event_count
        r1 = fmp1.evaluate(NOW_MS + 40000)
        r2 = fmp2.evaluate(NOW_MS + 40000)
        assert r1.severity == r2.severity
        assert r1.passed == r2.passed
        assert r1.exposure_multiplier == r2.exposure_multiplier

    def test_replay_clears_previous_state(self):
        fmp = _make_fmp()
        _record_losses(fmp, 10)
        assert fmp.consecutive_losses == 10
        # Replay with only wins
        events = [FailureModeEvent(
            event_type="trade_outcome", timestamp_ms=NOW_MS + i * 1000,
            is_win=True, strategy_class="MX", regime="BULL", pnl_pct=0.5,
        ) for i in range(5)]
        fmp.replay(events)
        assert fmp.consecutive_losses == 0
        assert fmp.trade_count == 5
        assert fmp.event_count == 5

    def test_double_replay_idempotent(self):
        fmp = _make_fmp()
        _record_losses(fmp, 5)
        _record_wins(fmp, 3, start_ms=NOW_MS + 10000)
        events = fmp.event_log
        fmp.replay(events)
        fmp.replay(events)
        assert fmp.trade_count == 8
        assert fmp.consecutive_losses == 0


# ── Persistence: to_json / from_json / fail-closed (4) ─────────

class TestPersistence:
    def test_roundtrip(self):
        fmp = _make_fmp()
        _record_wins(fmp, 5)
        _record_losses(fmp, 3, start_ms=NOW_MS + 10000)
        fmp.record_drawdown_sample(2.5, NOW_MS + 20000)

        data = fmp.to_json()
        restored = FailureModeProtection.from_json(data)

        assert restored.trade_count == 8
        assert restored.consecutive_losses == 3
        assert restored.event_count == 9

    def test_corrupt_json_raises(self):
        with pytest.raises(FailureModeStateCorruptError, match="JSON corrupt"):
            FailureModeProtection.from_json("not json {{{")

    def test_missing_version_raises(self):
        data = json.dumps({"events": []})
        with pytest.raises(FailureModeStateCorruptError, match="Missing 'version'"):
            FailureModeProtection.from_json(data)

    def test_wrong_version_raises(self):
        data = json.dumps({"version": 999, "events": []})
        with pytest.raises(FailureModeStateCorruptError, match="Unknown version"):
            FailureModeProtection.from_json(data)


# ── Retention / Truncation (2) ─────────────────────────────────

class TestRetention:
    def test_truncate_removes_old_events(self):
        fmp = _make_fmp()
        old_ts = NOW_MS - 8 * DAY  # Older than 7-day window
        fmp.record_trade_outcome(True, "MX", "BULL", 0.5, old_ts)
        fmp.record_trade_outcome(False, "MX", "BULL", -0.5, NOW_MS)
        assert fmp.event_count == 2
        archived = fmp.truncate(NOW_MS)
        assert len(archived) == 1
        assert fmp.event_count == 1
        assert fmp.trade_count == 1

    def test_truncate_no_old_events_noop(self):
        fmp = _make_fmp()
        _record_wins(fmp, 5)
        archived = fmp.truncate(NOW_MS + 1000)
        assert len(archived) == 0
        assert fmp.event_count == 5
