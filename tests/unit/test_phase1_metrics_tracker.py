"""
Tests for Phase1MetricsTracker — centralized metrics collection for Phase 1 components.

Covers:
- TransitionDetector recording (signals, counts by type/symbol, active transitions)
- CoverageGuarantee recording (level history, escalation, retraction, fallback trades)
- RegimeCapitalAllocator recording (adjustments, regime distribution)
- Global regime distribution from scan cycles
- Snapshot generation for dashboard
- Summary line generation
- Thread safety (concurrent recording)
- Singleton pattern
"""
import threading
import time

import pytest


# ── Fixtures ─────────────────────────────────────────────────

@pytest.fixture
def tracker():
    """Fresh Phase1MetricsTracker instance (not singleton)."""
    from core.monitoring.phase1_metrics import Phase1MetricsTracker
    return Phase1MetricsTracker()


# ════════════════════════════════════════════════════════════════
# TransitionDetector metrics
# ════════════════════════════════════════════════════════════════

class TestTransitionMetrics:

    def test_record_transition_increments_counts(self, tracker):
        tracker.record_transition("BTC/USDT", "transition_breakout", "long", 0.75, "accumulation", "bull_trend")
        assert tracker._transition_counts["transition_breakout"] == 1
        assert tracker._transition_counts_by_symbol["BTC/USDT"] == 1
        assert len(tracker._transition_signals) == 1

    def test_record_multiple_transitions(self, tracker):
        tracker.record_transition("BTC/USDT", "transition_breakout", "long", 0.75, "accumulation", "bull_trend")
        tracker.record_transition("ETH/USDT", "transition_expansion", "short", 0.60, "ranging", "volatility_expansion")
        tracker.record_transition("BTC/USDT", "transition_breakout", "long", 0.80, "accumulation", "bull_trend")

        assert tracker._transition_counts["transition_breakout"] == 2
        assert tracker._transition_counts["transition_expansion"] == 1
        assert tracker._transition_counts_by_symbol["BTC/USDT"] == 2
        assert tracker._transition_counts_by_symbol["ETH/USDT"] == 1
        assert len(tracker._transition_signals) == 3

    def test_active_transitions_tracked(self, tracker):
        tracker.record_transition("BTC/USDT", "transition_breakout", "long", 0.75, "accumulation", "bull_trend")
        assert "BTC/USDT" in tracker._active_transitions
        assert tracker._active_transitions["BTC/USDT"]["type"] == "transition_breakout"

    def test_active_transition_updated_by_latest(self, tracker):
        tracker.record_transition("BTC/USDT", "transition_breakout", "long", 0.75, "accumulation", "bull_trend")
        tracker.record_transition("BTC/USDT", "transition_expansion", "short", 0.60, "ranging", "vol_expansion")
        assert tracker._active_transitions["BTC/USDT"]["type"] == "transition_expansion"

    def test_clear_active_transition(self, tracker):
        tracker.record_transition("BTC/USDT", "transition_breakout", "long", 0.75, "accumulation", "bull_trend")
        tracker.clear_active_transition("BTC/USDT")
        assert "BTC/USDT" not in tracker._active_transitions

    def test_clear_nonexistent_symbol_no_error(self, tracker):
        tracker.clear_active_transition("FAKE/USDT")  # should not raise

    def test_signals_capped_at_200(self, tracker):
        for i in range(250):
            tracker.record_transition(f"SYM{i}", "transition_breakout", "long", 0.50, "acc", "bull")
        assert len(tracker._transition_signals) == 200

    def test_last_transition_timestamp(self, tracker):
        assert tracker._last_transition_at is None
        tracker.record_transition("BTC/USDT", "transition_breakout", "long", 0.75, "acc", "bull")
        assert tracker._last_transition_at is not None
        assert time.time() - tracker._last_transition_at < 1.0


# ════════════════════════════════════════════════════════════════
# CoverageGuarantee metrics
# ════════════════════════════════════════════════════════════════

class TestCoverageMetrics:

    def test_record_coverage_normal(self, tracker):
        tracker.record_coverage_event(0, "none", 0, "bull_trend")
        assert tracker._cg_current_level == 0
        assert tracker._cg_gap_episodes == 0

    def test_record_coverage_escalation(self, tracker):
        tracker.record_coverage_event(0, "none", 0, "ranging")
        tracker.record_coverage_event(1, "info", 3, "ranging")
        assert tracker._cg_current_level == 1
        assert tracker._cg_gap_episodes == 1

    def test_record_coverage_multi_escalation(self, tracker):
        tracker.record_coverage_event(0, "none", 0, "ranging")
        tracker.record_coverage_event(1, "info", 3, "ranging")
        tracker.record_coverage_event(2, "expand_models", 6, "ranging")
        tracker.record_coverage_event(3, "allow_enrichment", 12, "ranging")
        assert tracker._cg_current_level == 3
        assert tracker._cg_max_level_reached == 3
        assert tracker._cg_gap_episodes == 1  # still one episode

    def test_record_coverage_retraction(self, tracker):
        tracker.record_coverage_event(1, "info", 3, "ranging")
        tracker.record_coverage_event(0, "none", 0, "bull_trend")
        assert tracker._cg_current_level == 0
        assert tracker._cg_gap_episodes == 1

    def test_record_coverage_new_episode_after_retraction(self, tracker):
        tracker.record_coverage_event(1, "info", 3, "ranging")
        tracker.record_coverage_event(0, "none", 0, "bull_trend")
        tracker.record_coverage_event(2, "expand_models", 6, "accumulation")
        assert tracker._cg_gap_episodes == 2

    def test_record_fallback_trade(self, tracker):
        assert tracker._cg_fallback_trade_count == 0
        tracker.record_fallback_trade()
        assert tracker._cg_fallback_trade_count == 1
        tracker.record_fallback_trade()
        assert tracker._cg_fallback_trade_count == 2

    def test_idle_cycles_tracked(self, tracker):
        tracker.record_coverage_event(1, "info", 5, "ranging")
        assert tracker._cg_idle_cycles == 5

    def test_max_level_persists(self, tracker):
        tracker.record_coverage_event(3, "allow_enrichment", 12, "ranging")
        tracker.record_coverage_event(0, "none", 0, "bull_trend")
        assert tracker._cg_max_level_reached == 3

    def test_level_history_capped(self, tracker):
        for i in range(250):
            tracker.record_coverage_event(1, "info", i, "ranging")
        assert len(tracker._cg_level_history) == 200


# ════════════════════════════════════════════════════════════════
# RegimeCapitalAllocator metrics
# ════════════════════════════════════════════════════════════════

class TestRCAMetrics:

    def test_record_rca_adjustment(self, tracker):
        tracker.record_rca_adjustment("bull_trend", 1.20, 100.0, 120.0)
        assert tracker._rca_adjustment_count == 1
        assert tracker._rca_regime_distribution["bull_trend"] == 1

    def test_record_multiple_adjustments(self, tracker):
        tracker.record_rca_adjustment("bull_trend", 1.20, 100.0, 120.0)
        tracker.record_rca_adjustment("bear_trend", 1.10, 100.0, 110.0)
        tracker.record_rca_adjustment("bull_trend", 1.20, 200.0, 240.0)
        assert tracker._rca_adjustment_count == 3
        assert tracker._rca_regime_distribution["bull_trend"] == 2
        assert tracker._rca_regime_distribution["bear_trend"] == 1

    def test_transition_and_fallback_flags(self, tracker):
        tracker.record_rca_adjustment("ranging", 0.60, 100.0, 60.0, is_transition=True)
        assert tracker._rca_adjustments[-1]["is_transition"] is True
        assert tracker._rca_adjustments[-1]["is_fallback"] is False

        tracker.record_rca_adjustment("ranging", 0.30, 100.0, 30.0, is_fallback=True)
        assert tracker._rca_adjustments[-1]["is_fallback"] is True

    def test_adjustments_capped_at_200(self, tracker):
        for i in range(250):
            tracker.record_rca_adjustment("bull_trend", 1.20, float(i), float(i) * 1.2)
        assert len(tracker._rca_adjustments) == 200

    def test_last_adjustment_timestamp(self, tracker):
        assert tracker._last_rca_adjustment_at is None
        tracker.record_rca_adjustment("bull_trend", 1.20, 100.0, 120.0)
        assert tracker._last_rca_adjustment_at is not None


# ════════════════════════════════════════════════════════════════
# Global regime distribution
# ════════════════════════════════════════════════════════════════

class TestGlobalMetrics:

    def test_record_scan_cycle(self, tracker):
        tracker.record_scan_cycle({"bull_trend": 3, "ranging": 2})
        assert tracker._total_scan_cycles == 1
        assert tracker._regime_distribution["bull_trend"] == 3
        assert tracker._regime_distribution["ranging"] == 2

    def test_accumulates_across_cycles(self, tracker):
        tracker.record_scan_cycle({"bull_trend": 3, "ranging": 2})
        tracker.record_scan_cycle({"bull_trend": 1, "bear_trend": 4})
        assert tracker._total_scan_cycles == 2
        assert tracker._regime_distribution["bull_trend"] == 4
        assert tracker._regime_distribution["bear_trend"] == 4
        assert tracker._regime_distribution["ranging"] == 2


# ════════════════════════════════════════════════════════════════
# Snapshot
# ════════════════════════════════════════════════════════════════

class TestSnapshot:

    def test_empty_snapshot(self, tracker):
        snap = tracker.get_snapshot()
        assert snap["transition_total"] == 0
        assert snap["cg_current_level"] == 0
        assert snap["rca_adjustment_count"] == 0
        assert snap["total_scan_cycles"] == 0
        assert snap["last_transition_ago_min"] is None

    def test_populated_snapshot(self, tracker):
        tracker.record_transition("BTC/USDT", "transition_breakout", "long", 0.75, "acc", "bull")
        tracker.record_coverage_event(2, "expand_models", 6, "ranging")
        tracker.record_rca_adjustment("bull_trend", 1.20, 100.0, 120.0)
        tracker.record_scan_cycle({"bull_trend": 5})

        snap = tracker.get_snapshot()
        assert snap["transition_total"] == 1
        assert snap["cg_current_level"] == 2
        assert snap["rca_adjustment_count"] == 1
        assert snap["total_scan_cycles"] == 1
        assert snap["last_transition_ago_min"] is not None
        assert snap["last_transition_ago_min"] < 1.0

    def test_recent_events_limited_to_5(self, tracker):
        for i in range(10):
            tracker.record_transition(f"SYM{i}", "transition_breakout", "long", 0.5, "acc", "bull")
        snap = tracker.get_snapshot()
        assert len(snap["recent_transitions"]) == 5

    def test_snapshot_includes_active_transitions(self, tracker):
        tracker.record_transition("BTC/USDT", "transition_breakout", "long", 0.75, "acc", "bull")
        snap = tracker.get_snapshot()
        assert "BTC/USDT" in snap["active_transitions"]

    def test_snapshot_uptime(self, tracker):
        snap = tracker.get_snapshot()
        assert snap["uptime_min"] >= 0


# ════════════════════════════════════════════════════════════════
# Summary line
# ════════════════════════════════════════════════════════════════

class TestSummaryLine:

    def test_empty_summary(self, tracker):
        line = tracker.get_summary_line()
        assert "TD: idle" in line
        assert "CG: normal" in line
        assert "RCA: idle" in line

    def test_populated_summary(self, tracker):
        tracker.record_transition("BTC/USDT", "transition_breakout", "long", 0.75, "acc", "bull")
        tracker.record_coverage_event(2, "expand_models", 6, "ranging")
        tracker.record_rca_adjustment("bull_trend", 1.20, 100.0, 120.0)

        line = tracker.get_summary_line()
        assert "TD: 1 signals" in line
        assert "CG: L2 EXPAND" in line
        assert "RCA: 1 adj" in line

    def test_summary_with_high_cg_level(self, tracker):
        tracker.record_coverage_event(4, "notify_operator", 24, "ranging")
        line = tracker.get_summary_line()
        assert "CG: L4 NOTIFY" in line


# ════════════════════════════════════════════════════════════════
# Thread safety
# ════════════════════════════════════════════════════════════════

class TestThreadSafety:

    def test_concurrent_recording(self, tracker):
        """Multiple threads recording simultaneously should not raise."""
        errors = []

        def record_transitions():
            try:
                for i in range(50):
                    tracker.record_transition(f"SYM{i}", "transition_breakout", "long", 0.5, "acc", "bull")
            except Exception as e:
                errors.append(e)

        def record_coverage():
            try:
                for i in range(50):
                    tracker.record_coverage_event(i % 5, "info", i, "ranging")
            except Exception as e:
                errors.append(e)

        def record_rca():
            try:
                for i in range(50):
                    tracker.record_rca_adjustment("bull_trend", 1.20, float(i), float(i) * 1.2)
            except Exception as e:
                errors.append(e)

        threads = [
            threading.Thread(target=record_transitions),
            threading.Thread(target=record_coverage),
            threading.Thread(target=record_rca),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Thread safety errors: {errors}"
        # All records should be present
        assert sum(tracker._transition_counts.values()) == 50
        assert tracker._rca_adjustment_count == 50


# ════════════════════════════════════════════════════════════════
# Singleton
# ════════════════════════════════════════════════════════════════

class TestSingleton:

    def test_get_phase1_metrics_returns_same_instance(self):
        import core.monitoring.phase1_metrics as mod
        # Reset singleton
        mod._tracker = None
        m1 = mod.get_phase1_metrics()
        m2 = mod.get_phase1_metrics()
        assert m1 is m2
        mod._tracker = None  # cleanup
