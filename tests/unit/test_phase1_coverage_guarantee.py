"""
Unit tests for CoverageGuarantee — coverage guarantee monitoring and fallback logic.

Coverage:
1. Normal operation: approved_count > 0 resets idle cycles
2. Idle cycle counting: increments per zero-approval scan
3. Level 0 (below LEVEL_INFO): action="none"
4. Level 1 (at LEVEL_INFO=3): action="info"
5. Level 2 (at LEVEL_EXPAND=6): action="expand_models"
6. Level 3 (at LEVEL_ENRICHMENT=12): action="allow_enrichment"
7. Level 4+ (at LEVEL_NOTIFY=24): action="notify_operator" then "notified_waiting"
8. Excluded regimes (crisis, liquidation_cascade, etc): action="none_excluded"
9. Auto-retract on primary signal
10. Level never de-escalates
11. Fallback trade cap (max 2 per 6h window)
12. Fallback cap exhausted → enrichment_capped
13. Fallback window expiry (6h cutoff)
14. get_state() diagnostics
15. Notified flag fires once per gap
"""
import time
import pytest
from core.monitoring.coverage_guarantee import CoverageGuarantee


class TestCoverageGuaranteeNormalOperation:
    """Test normal operation: approved_count > 0 resets state."""

    def test_init_state(self):
        """CoverageGuarantee initializes with zero idle cycles and level 0."""
        cg = CoverageGuarantee()
        assert cg._idle_cycles == 0
        assert cg._current_level == 0
        assert cg._notified == False
        assert cg._fallback_trade_times == []

    def test_approved_count_gt_zero_resets_idle_cycles(self):
        """When approved_count > 0, idle_cycles reset to 0."""
        cg = CoverageGuarantee()
        # Simulate idle state
        cg._idle_cycles = 5
        cg._current_level = 2

        result = cg.on_scan_complete(approved_count=1, regime_distribution={}, dominant_regime="bull_trend")

        assert cg._idle_cycles == 0
        assert cg._current_level == 0
        assert cg._notified == False
        assert result["action"] == "none"
        assert result["level"] == 0

    def test_approved_count_gt_zero_resets_notified(self):
        """When approved_count > 0, _notified flag resets to False."""
        cg = CoverageGuarantee()
        cg._notified = True
        cg._idle_cycles = 10

        result = cg.on_scan_complete(approved_count=2, regime_distribution={}, dominant_regime="bull_trend")

        assert cg._notified == False
        assert result["action"] == "none"

    def test_approved_count_gt_zero_updates_primary_signal_time(self):
        """When approved_count > 0, _last_primary_signal_time updates."""
        cg = CoverageGuarantee()
        old_time = cg._last_primary_signal_time

        # Advance time slightly
        time.sleep(0.01)
        result = cg.on_scan_complete(approved_count=1, regime_distribution={}, dominant_regime="bull_trend")

        assert cg._last_primary_signal_time > old_time


class TestIdleCycleCounting:
    """Test idle cycle counting: approved_count=0 increments idle_cycles."""

    def test_first_idle_scan(self):
        """First scan with approved_count=0 increments idle_cycles to 1."""
        cg = CoverageGuarantee()
        result = cg.on_scan_complete(approved_count=0, regime_distribution={}, dominant_regime="bull_trend")

        assert cg._idle_cycles == 1
        assert result["idle_cycles"] == 1

    def test_idle_cycles_increment_sequentially(self):
        """Multiple idle scans increment idle_cycles sequentially."""
        cg = CoverageGuarantee()
        for i in range(1, 5):
            result = cg.on_scan_complete(approved_count=0, regime_distribution={}, dominant_regime="bull_trend")
            assert cg._idle_cycles == i
            assert result["idle_cycles"] == i

    def test_idle_cycles_reflect_in_result(self):
        """Result dict includes current idle_cycles."""
        cg = CoverageGuarantee()
        cg.on_scan_complete(approved_count=0, regime_distribution={}, dominant_regime="bull_trend")
        cg.on_scan_complete(approved_count=0, regime_distribution={}, dominant_regime="bull_trend")

        result = cg.on_scan_complete(approved_count=0, regime_distribution={}, dominant_regime="bull_trend")

        assert result["idle_cycles"] == 3


class TestLevel0:
    """Test Level 0: below LEVEL_INFO (3 cycles)."""

    def test_level_0_at_idle_cycles_1(self):
        """Level 0 when idle_cycles = 1 (< LEVEL_INFO=3)."""
        cg = CoverageGuarantee()
        result = cg.on_scan_complete(approved_count=0, regime_distribution={}, dominant_regime="bull_trend")

        assert cg._current_level == 0
        assert result["level"] == 0
        assert result["action"] == "none"

    def test_level_0_at_idle_cycles_2(self):
        """Level 0 when idle_cycles = 2 (< LEVEL_INFO=3)."""
        cg = CoverageGuarantee()
        cg.on_scan_complete(approved_count=0, regime_distribution={}, dominant_regime="bull_trend")
        result = cg.on_scan_complete(approved_count=0, regime_distribution={}, dominant_regime="bull_trend")

        assert cg._current_level == 0
        assert result["level"] == 0
        assert result["action"] == "none"

    def test_level_0_has_no_fallback_multiplier(self):
        """Level 0 fallback_size_multiplier defaults to 1.0."""
        cg = CoverageGuarantee()
        result = cg.on_scan_complete(approved_count=0, regime_distribution={}, dominant_regime="bull_trend")

        assert result["fallback_size_multiplier"] == 1.0

    def test_level_0_expand_regimes_empty(self):
        """Level 0 expand_regimes is empty."""
        cg = CoverageGuarantee()
        result = cg.on_scan_complete(approved_count=0, regime_distribution={}, dominant_regime="bull_trend")

        assert result["expand_regimes"] == []

    def test_level_0_allow_enrichment_false(self):
        """Level 0 allow_enrichment_standalone is False."""
        cg = CoverageGuarantee()
        result = cg.on_scan_complete(approved_count=0, regime_distribution={}, dominant_regime="bull_trend")

        assert result["allow_enrichment_standalone"] == False


class TestLevel1:
    """Test Level 1: at LEVEL_INFO (3 cycles)."""

    def test_level_1_at_idle_cycles_3(self):
        """Level 1 when idle_cycles == LEVEL_INFO (3)."""
        cg = CoverageGuarantee()
        for _ in range(3):
            cg.on_scan_complete(approved_count=0, regime_distribution={}, dominant_regime="bull_trend")

        result = cg.on_scan_complete(approved_count=0, regime_distribution={}, dominant_regime="bull_trend")

        assert cg._current_level == 1
        assert result["level"] == 1
        assert result["action"] == "info"

    def test_level_1_properties(self):
        """Level 1 has no expand_regimes, no enrichment, fallback_size_mult=1.0."""
        cg = CoverageGuarantee()
        for _ in range(3):
            cg.on_scan_complete(approved_count=0, regime_distribution={}, dominant_regime="bull_trend")

        result = cg.on_scan_complete(approved_count=0, regime_distribution={}, dominant_regime="bull_trend")

        assert result["expand_regimes"] == []
        assert result["allow_enrichment_standalone"] == False
        assert result["fallback_size_multiplier"] == 1.0


class TestLevel2:
    """Test Level 2: at LEVEL_EXPAND (6 cycles)."""

    def test_level_2_at_idle_cycles_6(self):
        """Level 2 when idle_cycles >= LEVEL_EXPAND (6)."""
        cg = CoverageGuarantee()
        for _ in range(6):
            cg.on_scan_complete(approved_count=0, regime_distribution={}, dominant_regime="bull_trend")

        assert cg._current_level == 2
        assert cg.on_scan_complete(approved_count=0, regime_distribution={}, dominant_regime="bull_trend")["action"] == "expand_models"

    def test_level_2_expand_regimes_populated(self):
        """Level 2 expand_regimes includes dominant_regime."""
        cg = CoverageGuarantee()
        for _ in range(6):
            cg.on_scan_complete(approved_count=0, regime_distribution={}, dominant_regime="bear_trend")

        result = cg.on_scan_complete(approved_count=0, regime_distribution={}, dominant_regime="bear_trend")

        assert "bear_trend" in result["expand_regimes"]

    def test_level_2_fallback_size_multiplier_0_50(self):
        """Level 2 fallback_size_multiplier is 0.50."""
        cg = CoverageGuarantee()
        for _ in range(6):
            cg.on_scan_complete(approved_count=0, regime_distribution={}, dominant_regime="bull_trend")

        result = cg.on_scan_complete(approved_count=0, regime_distribution={}, dominant_regime="bull_trend")

        assert result["fallback_size_multiplier"] == 0.50

    def test_level_2_no_enrichment(self):
        """Level 2 allow_enrichment_standalone is False."""
        cg = CoverageGuarantee()
        for _ in range(6):
            cg.on_scan_complete(approved_count=0, regime_distribution={}, dominant_regime="bull_trend")

        result = cg.on_scan_complete(approved_count=0, regime_distribution={}, dominant_regime="bull_trend")

        assert result["allow_enrichment_standalone"] == False


class TestLevel3:
    """Test Level 3: at LEVEL_ENRICHMENT (12 cycles)."""

    def test_level_3_at_idle_cycles_12(self):
        """Level 3 when idle_cycles >= LEVEL_ENRICHMENT (12)."""
        cg = CoverageGuarantee()
        for _ in range(12):
            cg.on_scan_complete(approved_count=0, regime_distribution={}, dominant_regime="bull_trend")

        assert cg._current_level == 3
        assert cg.on_scan_complete(approved_count=0, regime_distribution={}, dominant_regime="bull_trend")["action"] == "allow_enrichment"

    def test_level_3_allow_enrichment_true(self):
        """Level 3 allow_enrichment_standalone is True when fallback cap available."""
        cg = CoverageGuarantee()
        for _ in range(12):
            cg.on_scan_complete(approved_count=0, regime_distribution={}, dominant_regime="bull_trend")

        result = cg.on_scan_complete(approved_count=0, regime_distribution={}, dominant_regime="bull_trend")

        assert result["allow_enrichment_standalone"] == True

    def test_level_3_fallback_size_multiplier_0_30(self):
        """Level 3 fallback_size_multiplier is 0.30."""
        cg = CoverageGuarantee()
        for _ in range(12):
            cg.on_scan_complete(approved_count=0, regime_distribution={}, dominant_regime="bull_trend")

        result = cg.on_scan_complete(approved_count=0, regime_distribution={}, dominant_regime="bull_trend")

        assert result["fallback_size_multiplier"] == 0.30

    def test_level_3_expand_regimes_populated(self):
        """Level 3 expand_regimes includes dominant_regime."""
        cg = CoverageGuarantee()
        for _ in range(12):
            cg.on_scan_complete(approved_count=0, regime_distribution={}, dominant_regime="ranging")

        result = cg.on_scan_complete(approved_count=0, regime_distribution={}, dominant_regime="ranging")

        assert "ranging" in result["expand_regimes"]


class TestLevel3FallbackCapped:
    """Test Level 3 with fallback cap exhausted."""

    def test_level_3_enrichment_capped_when_2_trades_recorded(self):
        """When fallback cap exhausted (2 trades), action="enrichment_capped"."""
        cg = CoverageGuarantee()
        cg.record_fallback_trade()
        cg.record_fallback_trade()

        for _ in range(12):
            cg.on_scan_complete(approved_count=0, regime_distribution={}, dominant_regime="bull_trend")

        result = cg.on_scan_complete(approved_count=0, regime_distribution={}, dominant_regime="bull_trend")

        assert result["action"] == "enrichment_capped"
        assert result["allow_enrichment_standalone"] == False
        assert result["fallback_trades_remaining"] == 0

    def test_level_3_enrichment_capped_still_has_fallback_multiplier(self):
        """enrichment_capped action still provides fallback_size_multiplier=0.30."""
        cg = CoverageGuarantee()
        cg.record_fallback_trade()
        cg.record_fallback_trade()

        for _ in range(12):
            cg.on_scan_complete(approved_count=0, regime_distribution={}, dominant_regime="bull_trend")

        result = cg.on_scan_complete(approved_count=0, regime_distribution={}, dominant_regime="bull_trend")

        assert result["fallback_size_multiplier"] == 0.30


class TestLevel4:
    """Test Level 4+: at LEVEL_NOTIFY (24 cycles)."""

    def test_level_4_at_idle_cycles_24(self):
        """Level 4 when idle_cycles >= LEVEL_NOTIFY (24)."""
        cg = CoverageGuarantee()
        for _ in range(24):
            cg.on_scan_complete(approved_count=0, regime_distribution={}, dominant_regime="bull_trend")

        assert cg._current_level == 4

    def test_level_4_first_notification(self):
        """First time at Level 4, action="notify_operator" and _notified=True."""
        cg = CoverageGuarantee()
        for i in range(24):
            result = cg.on_scan_complete(approved_count=0, regime_distribution={}, dominant_regime="bull_trend")
            # On iteration 24 (idle_cycles=24), level becomes 4 and notify_operator fires
            if i == 23:  # 0-indexed, so i=23 is the 24th iteration
                assert result["action"] == "notify_operator"
                assert cg._notified == True
                return

        # Should have fired in the loop
        assert False, "notify_operator should have fired at iteration 24"

    def test_level_4_subsequent_notification(self):
        """Subsequent calls at Level 4, action="notified_waiting"."""
        cg = CoverageGuarantee()
        for _ in range(24):
            cg.on_scan_complete(approved_count=0, regime_distribution={}, dominant_regime="bull_trend")

        cg.on_scan_complete(approved_count=0, regime_distribution={}, dominant_regime="bull_trend")  # First: "notify_operator"
        result = cg.on_scan_complete(approved_count=0, regime_distribution={}, dominant_regime="bull_trend")  # Second: "notified_waiting"

        assert result["action"] == "notified_waiting"
        assert cg._notified == True

    def test_level_4_allow_enrichment_true(self):
        """Level 4 allow_enrichment_standalone is True."""
        cg = CoverageGuarantee()
        for _ in range(24):
            cg.on_scan_complete(approved_count=0, regime_distribution={}, dominant_regime="bull_trend")

        result = cg.on_scan_complete(approved_count=0, regime_distribution={}, dominant_regime="bull_trend")

        assert result["allow_enrichment_standalone"] == True

    def test_level_4_fallback_size_multiplier_0_30(self):
        """Level 4 fallback_size_multiplier is 0.30."""
        cg = CoverageGuarantee()
        for _ in range(24):
            cg.on_scan_complete(approved_count=0, regime_distribution={}, dominant_regime="bull_trend")

        result = cg.on_scan_complete(approved_count=0, regime_distribution={}, dominant_regime="bull_trend")

        assert result["fallback_size_multiplier"] == 0.30


class TestExcludedRegimes:
    """Test excluded regimes: crisis, liquidation_cascade, volatility_compression, squeeze."""

    @pytest.mark.parametrize("regime", ["crisis", "liquidation_cascade", "volatility_compression", "squeeze"])
    def test_excluded_regime_at_level_0(self, regime):
        """Excluded regime at Level 0 produces action='none_excluded'."""
        cg = CoverageGuarantee()
        result = cg.on_scan_complete(approved_count=0, regime_distribution={}, dominant_regime=regime)

        assert result["action"] == "none_excluded"
        assert result["level"] == 0

    @pytest.mark.parametrize("regime", ["crisis", "liquidation_cascade", "volatility_compression", "squeeze"])
    def test_excluded_regime_at_level_2(self, regime):
        """Excluded regime at Level 2 produces action='none_excluded'."""
        cg = CoverageGuarantee()
        for _ in range(6):
            cg.on_scan_complete(approved_count=0, regime_distribution={}, dominant_regime=regime)

        # Level should escalate to 2 internally, but action should be none_excluded
        result = cg.on_scan_complete(approved_count=0, regime_distribution={}, dominant_regime=regime)

        assert result["action"] == "none_excluded"

    @pytest.mark.parametrize("regime", ["crisis", "liquidation_cascade", "volatility_compression", "squeeze"])
    def test_excluded_regime_at_level_4(self, regime):
        """Excluded regime at Level 4 produces action='none_excluded'."""
        cg = CoverageGuarantee()
        for _ in range(24):
            cg.on_scan_complete(approved_count=0, regime_distribution={}, dominant_regime=regime)

        result = cg.on_scan_complete(approved_count=0, regime_distribution={}, dominant_regime=regime)

        assert result["action"] == "none_excluded"

    def test_non_excluded_regime_works_normally(self):
        """Non-excluded regime (e.g., bull_trend) escalates normally."""
        cg = CoverageGuarantee()
        for _ in range(6):
            cg.on_scan_complete(approved_count=0, regime_distribution={}, dominant_regime="bull_trend")

        result = cg.on_scan_complete(approved_count=0, regime_distribution={}, dominant_regime="bull_trend")

        assert result["action"] == "expand_models"


class TestAutoRetract:
    """Test auto-retract on primary signal."""

    def test_retract_from_level_2_to_level_0(self):
        """Primary signal (approved_count > 0) resets from Level 2 to Level 0."""
        cg = CoverageGuarantee()
        for _ in range(6):
            cg.on_scan_complete(approved_count=0, regime_distribution={}, dominant_regime="bull_trend")

        assert cg._current_level == 2

        result = cg.on_scan_complete(approved_count=1, regime_distribution={}, dominant_regime="bull_trend")

        assert cg._current_level == 0
        assert result["action"] == "none"

    def test_retract_from_level_4_to_level_0(self):
        """Primary signal resets from Level 4 to Level 0."""
        cg = CoverageGuarantee()
        for _ in range(24):
            cg.on_scan_complete(approved_count=0, regime_distribution={}, dominant_regime="bull_trend")

        assert cg._current_level == 4

        result = cg.on_scan_complete(approved_count=2, regime_distribution={}, dominant_regime="bull_trend")

        assert cg._current_level == 0
        assert result["action"] == "none"

    def test_retract_clears_notified_flag(self):
        """Auto-retract clears _notified flag."""
        cg = CoverageGuarantee()
        for _ in range(24):
            cg.on_scan_complete(approved_count=0, regime_distribution={}, dominant_regime="bull_trend")

        cg.on_scan_complete(approved_count=0, regime_distribution={}, dominant_regime="bull_trend")  # notify
        assert cg._notified == True

        cg.on_scan_complete(approved_count=1, regime_distribution={}, dominant_regime="bull_trend")
        assert cg._notified == False


class TestLevelNeverDeEscalates:
    """Test that level never de-escalates within a gap."""

    def test_level_escalates_monotonically(self):
        """Level increases or stays same, never decreases."""
        cg = CoverageGuarantee()
        levels = []

        for i in range(25):
            result = cg.on_scan_complete(approved_count=0, regime_distribution={}, dominant_regime="bull_trend")
            levels.append(result["level"])

        # Verify monotonic increase: each level >= previous level
        for i in range(1, len(levels)):
            assert levels[i] >= levels[i - 1], f"Level decreased at cycle {i}: {levels[i-1]} -> {levels[i]}"

    def test_level_stays_at_4_after_reaching_24(self):
        """Level stays at 4 after idle_cycles reaches 24+."""
        cg = CoverageGuarantee()
        for _ in range(24):
            cg.on_scan_complete(approved_count=0, regime_distribution={}, dominant_regime="bull_trend")

        for _ in range(10):
            result = cg.on_scan_complete(approved_count=0, regime_distribution={}, dominant_regime="bull_trend")
            assert result["level"] == 4


class TestFallbackTradeCap:
    """Test fallback trade cap: max 2 per 6-hour window."""

    def test_fallback_trades_remaining_initial(self):
        """Initially, fallback_trades_remaining is 2."""
        cg = CoverageGuarantee()
        result = cg.on_scan_complete(approved_count=0, regime_distribution={}, dominant_regime="bull_trend")

        assert result["fallback_trades_remaining"] == 2

    def test_record_one_fallback_trade(self):
        """After recording 1 trade, remaining is 1."""
        cg = CoverageGuarantee()
        cg.record_fallback_trade()
        result = cg.on_scan_complete(approved_count=0, regime_distribution={}, dominant_regime="bull_trend")

        assert result["fallback_trades_remaining"] == 1

    def test_record_two_fallback_trades(self):
        """After recording 2 trades, remaining is 0."""
        cg = CoverageGuarantee()
        cg.record_fallback_trade()
        cg.record_fallback_trade()
        result = cg.on_scan_complete(approved_count=0, regime_distribution={}, dominant_regime="bull_trend")

        assert result["fallback_trades_remaining"] == 0

    def test_record_three_fallback_trades(self):
        """After recording 3 trades, remaining is still 0 (capped)."""
        cg = CoverageGuarantee()
        cg.record_fallback_trade()
        cg.record_fallback_trade()
        cg.record_fallback_trade()
        result = cg.on_scan_complete(approved_count=0, regime_distribution={}, dominant_regime="bull_trend")

        assert result["fallback_trades_remaining"] == 0

    def test_fallback_trades_remaining_in_get_state(self):
        """get_state() includes fallback_trades_remaining."""
        cg = CoverageGuarantee()
        cg.record_fallback_trade()
        state = cg.get_state()

        assert "fallback_trades_remaining" in state
        assert state["fallback_trades_remaining"] == 1


class TestFallbackWindowExpiry:
    """Test fallback window expiry: trades older than 6h are excluded."""

    def test_fallback_trade_within_window_counts(self, monkeypatch):
        """Trade recorded 1 hour ago is counted."""
        cg = CoverageGuarantee()
        now = time.time()
        monkeypatch.setattr("time.time", lambda: now)

        cg.record_fallback_trade()

        # Advance time by 1 hour
        monkeypatch.setattr("time.time", lambda: now + 3600)

        result = cg.on_scan_complete(approved_count=0, regime_distribution={}, dominant_regime="bull_trend")
        assert result["fallback_trades_remaining"] == 1

    def test_fallback_trade_at_window_boundary(self, monkeypatch):
        """Trade recorded exactly 6 hours ago is NOT counted (t > cutoff check fails at boundary)."""
        cg = CoverageGuarantee()
        now = time.time()
        monkeypatch.setattr("time.time", lambda: now)

        cg.record_fallback_trade()

        # Advance time by exactly 6 hours
        monkeypatch.setattr("time.time", lambda: now + 6 * 3600)

        result = cg.on_scan_complete(approved_count=0, regime_distribution={}, dominant_regime="bull_trend")
        # Trade at exactly the cutoff (t == cutoff) is NOT included (filter is t > cutoff, not t >= cutoff)
        assert result["fallback_trades_remaining"] == 2

    def test_fallback_trade_just_beyond_window(self, monkeypatch):
        """Trade recorded just beyond 6 hours is not counted."""
        cg = CoverageGuarantee()
        now = time.time()
        monkeypatch.setattr("time.time", lambda: now)

        cg.record_fallback_trade()

        # Advance time just beyond 6 hours
        monkeypatch.setattr("time.time", lambda: now + 6 * 3600 + 1)

        result = cg.on_scan_complete(approved_count=0, regime_distribution={}, dominant_regime="bull_trend")
        # Trade is now outside window
        assert result["fallback_trades_remaining"] == 2

    def test_mixed_fallback_trades_window_expiry(self, monkeypatch):
        """Mixed old and new trades: only new ones count."""
        cg = CoverageGuarantee()
        now = time.time()
        monkeypatch.setattr("time.time", lambda: now)

        cg.record_fallback_trade()  # Trade 1 at now

        # Advance 7 hours
        monkeypatch.setattr("time.time", lambda: now + 7 * 3600)

        cg.record_fallback_trade()  # Trade 2 at now + 7h

        result = cg.on_scan_complete(approved_count=0, regime_distribution={}, dominant_regime="bull_trend")

        # Trade 1 expired, Trade 2 within window
        assert result["fallback_trades_remaining"] == 1

    def test_two_trades_within_window_then_one_expires(self, monkeypatch):
        """Record 2 trades, then advance time to expire one."""
        cg = CoverageGuarantee()
        now = time.time()
        monkeypatch.setattr("time.time", lambda: now)

        cg.record_fallback_trade()
        cg.record_fallback_trade()

        result1 = cg.on_scan_complete(approved_count=0, regime_distribution={}, dominant_regime="bull_trend")
        assert result1["fallback_trades_remaining"] == 0

        # Advance beyond 6 hours
        monkeypatch.setattr("time.time", lambda: now + 6 * 3600 + 1)

        result2 = cg.on_scan_complete(approved_count=0, regime_distribution={}, dominant_regime="bull_trend")
        # Both trades expired
        assert result2["fallback_trades_remaining"] == 2


class TestGetState:
    """Test get_state() diagnostics."""

    def test_get_state_includes_all_keys(self):
        """get_state() returns dict with all diagnostic keys."""
        cg = CoverageGuarantee()
        state = cg.get_state()

        assert "idle_cycles" in state
        assert "current_level" in state
        assert "notified" in state
        assert "fallback_trades_remaining" in state
        assert "minutes_since_primary" in state

    def test_get_state_idle_cycles_reflects_current(self):
        """get_state() idle_cycles matches _idle_cycles."""
        cg = CoverageGuarantee()
        for _ in range(5):
            cg.on_scan_complete(approved_count=0, regime_distribution={}, dominant_regime="bull_trend")

        state = cg.get_state()
        assert state["idle_cycles"] == 5

    def test_get_state_current_level_reflects_current(self):
        """get_state() current_level matches _current_level."""
        cg = CoverageGuarantee()
        for _ in range(6):
            cg.on_scan_complete(approved_count=0, regime_distribution={}, dominant_regime="bull_trend")

        state = cg.get_state()
        assert state["current_level"] == 2

    def test_get_state_notified_flag(self):
        """get_state() notified matches _notified."""
        cg = CoverageGuarantee()
        state1 = cg.get_state()
        assert state1["notified"] == False

        for _ in range(24):
            cg.on_scan_complete(approved_count=0, regime_distribution={}, dominant_regime="bull_trend")

        cg.on_scan_complete(approved_count=0, regime_distribution={}, dominant_regime="bull_trend")
        state2 = cg.get_state()
        assert state2["notified"] == True

    def test_get_state_minutes_since_primary(self, monkeypatch):
        """get_state() minutes_since_primary is computed correctly."""
        cg = CoverageGuarantee()
        now = time.time()
        monkeypatch.setattr("time.time", lambda: now)

        # Advance 5 minutes
        monkeypatch.setattr("time.time", lambda: now + 300)
        state = cg.get_state()

        assert state["minutes_since_primary"] == 5.0

    def test_get_state_fallback_trades_remaining(self):
        """get_state() includes fallback_trades_remaining."""
        cg = CoverageGuarantee()
        cg.record_fallback_trade()
        state = cg.get_state()

        assert state["fallback_trades_remaining"] == 1


class TestNotifiedFlagBehavior:
    """Test _notified flag fires once per gap."""

    def test_notified_flag_false_initially(self):
        """_notified starts False."""
        cg = CoverageGuarantee()
        assert cg._notified == False

    def test_notified_flag_set_on_first_level_4(self):
        """_notified set True on first Level 4 call."""
        cg = CoverageGuarantee()
        for _ in range(24):
            cg.on_scan_complete(approved_count=0, regime_distribution={}, dominant_regime="bull_trend")

        cg.on_scan_complete(approved_count=0, regime_distribution={}, dominant_regime="bull_trend")
        assert cg._notified == True

    def test_notified_flag_stays_true_at_level_4(self):
        """_notified stays True during subsequent Level 4 calls."""
        cg = CoverageGuarantee()
        for _ in range(24):
            cg.on_scan_complete(approved_count=0, regime_distribution={}, dominant_regime="bull_trend")

        cg.on_scan_complete(approved_count=0, regime_distribution={}, dominant_regime="bull_trend")  # notify
        cg.on_scan_complete(approved_count=0, regime_distribution={}, dominant_regime="bull_trend")  # waiting
        cg.on_scan_complete(approved_count=0, regime_distribution={}, dominant_regime="bull_trend")  # waiting

        assert cg._notified == True

    def test_notified_flag_reset_on_primary_signal(self):
        """_notified reset to False when primary signal fires."""
        cg = CoverageGuarantee()
        for _ in range(24):
            cg.on_scan_complete(approved_count=0, regime_distribution={}, dominant_regime="bull_trend")

        cg.on_scan_complete(approved_count=0, regime_distribution={}, dominant_regime="bull_trend")  # notify
        assert cg._notified == True

        cg.on_scan_complete(approved_count=1, regime_distribution={}, dominant_regime="bull_trend")  # retract
        assert cg._notified == False

    def test_notified_fires_again_after_new_gap(self):
        """After retract + new gap, _notified fires again at Level 4."""
        cg = CoverageGuarantee()

        # Gap 1: reach Level 4
        for _ in range(24):
            cg.on_scan_complete(approved_count=0, regime_distribution={}, dominant_regime="bull_trend")
        cg.on_scan_complete(approved_count=0, regime_distribution={}, dominant_regime="bull_trend")
        assert cg._notified == True

        # Retract
        cg.on_scan_complete(approved_count=1, regime_distribution={}, dominant_regime="bull_trend")
        assert cg._notified == False

        # Gap 2: reach Level 4 again
        for _ in range(24):
            cg.on_scan_complete(approved_count=0, regime_distribution={}, dominant_regime="bull_trend")
        cg.on_scan_complete(approved_count=0, regime_distribution={}, dominant_regime="bull_trend")
        assert cg._notified == True


class TestEdgeCases:
    """Test edge cases and boundary conditions."""

    def test_zero_idle_cycles_on_init(self):
        """Fresh CoverageGuarantee has zero idle_cycles."""
        cg = CoverageGuarantee()
        assert cg._idle_cycles == 0

    def test_multiple_retracts_idempotent(self):
        """Multiple primary signals in a row reset level to 0."""
        cg = CoverageGuarantee()
        for _ in range(6):
            cg.on_scan_complete(approved_count=0, regime_distribution={}, dominant_regime="bull_trend")

        cg.on_scan_complete(approved_count=2, regime_distribution={}, dominant_regime="bull_trend")
        assert cg._idle_cycles == 0
        assert cg._current_level == 0

        cg.on_scan_complete(approved_count=1, regime_distribution={}, dominant_regime="bull_trend")
        assert cg._idle_cycles == 0
        assert cg._current_level == 0

    def test_approved_count_zero_vs_none(self):
        """approved_count=0 is treated as idle (not a primary signal)."""
        cg = CoverageGuarantee()
        result = cg.on_scan_complete(approved_count=0, regime_distribution={}, dominant_regime="bull_trend")

        assert cg._idle_cycles == 1
        assert result["action"] == "none"

    def test_dominant_regime_in_result(self):
        """Result always includes dominant_regime."""
        cg = CoverageGuarantee()
        result = cg.on_scan_complete(approved_count=0, regime_distribution={}, dominant_regime="special_regime")

        assert result["dominant_regime"] == "special_regime"

    def test_multiple_symbols_regime_distribution(self):
        """regime_distribution parameter is accepted (not used in current logic)."""
        cg = CoverageGuarantee()
        dist = {"bull_trend": 3, "bear_trend": 2}
        result = cg.on_scan_complete(approved_count=0, regime_distribution=dist, dominant_regime="bull_trend")

        # Should complete without error
        assert result["dominant_regime"] == "bull_trend"


class TestIntegration:
    """Integration tests: realistic usage patterns."""

    def test_normal_trading_rhythm(self):
        """Normal scanning with occasional approved trades."""
        cg = CoverageGuarantee()

        # Scan 1-2: approved
        cg.on_scan_complete(approved_count=1, regime_distribution={}, dominant_regime="bull_trend")
        cg.on_scan_complete(approved_count=1, regime_distribution={}, dominant_regime="bull_trend")
        assert cg._idle_cycles == 0

        # Scan 3-8: idle
        for _ in range(6):
            cg.on_scan_complete(approved_count=0, regime_distribution={}, dominant_regime="bull_trend")

        # Should be at Level 2 now
        assert cg._current_level == 2

        # Scan 9: approved again
        cg.on_scan_complete(approved_count=2, regime_distribution={}, dominant_regime="bull_trend")
        assert cg._idle_cycles == 0
        assert cg._current_level == 0

    def test_extended_idle_with_excluded_regime(self):
        """Extended idle in excluded regime stays at none_excluded."""
        cg = CoverageGuarantee()

        for i in range(30):
            result = cg.on_scan_complete(approved_count=0, regime_distribution={}, dominant_regime="crisis")
            assert result["action"] == "none_excluded", f"Scan {i} failed"

    def test_fallback_trades_with_level_progression(self):
        """Fallback trades recorded during level progression."""
        cg = CoverageGuarantee()

        # Idle to Level 3
        for _ in range(12):
            cg.on_scan_complete(approved_count=0, regime_distribution={}, dominant_regime="bull_trend")

        # Record fallback trade
        cg.record_fallback_trade()
        result = cg.on_scan_complete(approved_count=0, regime_distribution={}, dominant_regime="bull_trend")
        assert result["level"] == 3
        assert result["fallback_trades_remaining"] == 1

        # Record another fallback trade
        cg.record_fallback_trade()
        result = cg.on_scan_complete(approved_count=0, regime_distribution={}, dominant_regime="bull_trend")
        assert result["fallback_trades_remaining"] == 0
        assert result["action"] == "enrichment_capped"

    def test_full_lifecycle(self):
        """Full lifecycle: idle → escalate → fallback cap → retract."""
        cg = CoverageGuarantee()

        # Record 2 fallback trades
        cg.record_fallback_trade()
        cg.record_fallback_trade()

        # Idle to Level 3
        for _ in range(12):
            cg.on_scan_complete(approved_count=0, regime_distribution={}, dominant_regime="bull_trend")

        result = cg.on_scan_complete(approved_count=0, regime_distribution={}, dominant_regime="bull_trend")
        assert result["action"] == "enrichment_capped"
        assert result["level"] == 3

        # Continue to Level 4 — notify_operator fires inside the loop at iteration 24
        notify_result = None
        for i in range(12):
            result = cg.on_scan_complete(approved_count=0, regime_distribution={}, dominant_regime="bull_trend")
            # At iteration 12 (0-indexed: i=11), idle_cycles becomes 25, level=4, and notify_operator fires
            # Actually iteration should be when idle_cycles reaches 24 (from current 13)
            # Loop iteration i=0: idle_cycles=14; i=11: idle_cycles=25
            # So at i=10 (idle_cycles=24), level becomes 4 and notify fires
            if i == 10:  # When idle_cycles=24 (13+11)
                notify_result = result

        # Verify notify_operator was captured during the loop
        if notify_result is None:
            # It fired at iteration where idle_cycles reached 24
            # Starting from 13, after 11 more: 13+11=24. That's at i=10 (0-indexed).
            # Actually let me recount: before loop idle_cycles=13, then loop i=0 makes it 14, ..., i=10 makes it 24
            # At i=10, idle_cycles=24, level=4, and if _notified was False, it fires notify_operator
            # But we called on_scan_complete 13 times in the first loop (to reach level 3 at idle_cycles=13)
            # Then in second loop we do 12 more calls, reaching idle_cycles=13+12=25
            # So notify should have fired when idle_cycles first hit 24 (during the 12-iteration loop)
            # That would be at call i=11 (0-indexed loop): idle_cycles = 13 + (11+1) = 25? No, at i=11 we've done 11 increments, so idle_cycles = 13+11 = 24
            # Let me just assert it happened
            assert cg._notified == True, "notify_operator should have fired during level 4 progression"
            assert cg._current_level == 4
        else:
            assert notify_result["action"] == "notify_operator"
            assert notify_result["level"] == 4

        # Retract with primary signal
        result = cg.on_scan_complete(approved_count=3, regime_distribution={}, dominant_regime="bull_trend")
        assert cg._idle_cycles == 0
        assert cg._current_level == 0
        assert cg._notified == False
        assert result["action"] == "none"
