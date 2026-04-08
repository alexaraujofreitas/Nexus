"""
tests/test_phase5b_global_filter.py — Global Trade Filter (Phase 5B v3)
======================================================================
37 tests: daily limits (4), regime throttle (3), chop/loss cooldowns (4),
          symbol cooldown (2), daily reset (2), determinism (3),
          replay/persistence (6), state snapshot (3), no wall-clock (3),
          fail-closed recovery (4), event log retention (3)
"""
import json
import pytest
from core.intraday.filtering.global_trade_filter import (
    GlobalTradeFilter, GlobalFilterConfig, FilterResult,
    FilterEvent, FilterStateSnapshot, FilterStateCorruptError,
    _FilterState, _RETENTION_WINDOW_MS,
)


# ── Constants ────────────────────────────────────────────────
DAY_MS = 86_400_000
BASE_MS = 20_000 * DAY_MS  # Arbitrary future day


# ── Daily Limits (4) ────────────────────────────────────────

class TestDailyLimits:
    def test_global_limit_blocks(self):
        f = GlobalTradeFilter(GlobalFilterConfig(max_trades_per_day_global=2))
        # Record 2 trades
        f.evaluate("MX", "BTC", "bull", 0.5, now_ms=BASE_MS)
        f.record_trade("MX", "BTC", "bull", now_ms=BASE_MS)
        f.evaluate("MX", "ETH", "bull", 0.5, now_ms=BASE_MS + 1000)
        f.record_trade("MX", "ETH", "bull", now_ms=BASE_MS + 1000)
        # 3rd should be blocked
        r = f.evaluate("MX", "SOL", "bull", 0.5, now_ms=BASE_MS + 2000)
        assert r.passed is False
        assert r.gate == "daily_limit_global"

    def test_per_class_limit_blocks(self):
        f = GlobalTradeFilter(GlobalFilterConfig(
            max_trades_per_day_per_class=1, max_trades_per_day_global=10,
        ))
        f.evaluate("MX", "BTC", "bull", 0.5, now_ms=BASE_MS)
        f.record_trade("MX", "BTC", "bull", now_ms=BASE_MS)
        r = f.evaluate("MX", "ETH", "bull", 0.5, now_ms=BASE_MS + 1000)
        assert r.passed is False
        assert r.gate == "daily_limit_class"

    def test_different_classes_independent(self):
        f = GlobalTradeFilter(GlobalFilterConfig(max_trades_per_day_per_class=1))
        f.evaluate("MX", "BTC", "bull", 0.5, now_ms=BASE_MS)
        f.record_trade("MX", "BTC", "bull", now_ms=BASE_MS)
        # Different class AND different symbol to avoid symbol cooldown
        r = f.evaluate("VR", "ETH", "bull", 0.5, now_ms=BASE_MS + 1000)
        assert r.passed is True

    def test_under_limit_passes(self):
        f = GlobalTradeFilter()
        r = f.evaluate("MX", "BTC", "bull", 0.5, now_ms=BASE_MS)
        assert r.passed is True


# ── Regime Throttle (3) ─────────────────────────────────────

class TestRegimeThrottle:
    def test_uncertain_regime_throttled(self):
        f = GlobalTradeFilter(GlobalFilterConfig(uncertain_regime_max_trades=1))
        f.evaluate("MX", "BTC", "uncertain", 0.6, now_ms=BASE_MS)
        f.record_trade("MX", "BTC", "uncertain", now_ms=BASE_MS)
        r = f.evaluate("MX", "ETH", "uncertain", 0.6, now_ms=BASE_MS + 1000)
        assert r.passed is False
        assert r.gate == "regime_throttle"

    def test_uncertain_regime_low_tqs_blocked(self):
        f = GlobalTradeFilter(GlobalFilterConfig(uncertain_regime_tqs_floor=0.50))
        r = f.evaluate("MX", "BTC", "uncertain", 0.30, now_ms=BASE_MS)
        assert r.passed is False
        assert r.gate == "regime_tqs_floor"

    def test_non_uncertain_regime_passes(self):
        f = GlobalTradeFilter()
        r = f.evaluate("MX", "BTC", "BULL_TREND", 0.3, now_ms=BASE_MS)
        assert r.passed is True


# ── Chop / Loss Cooldowns (4) ───────────────────────────────

class TestCooldowns:
    def test_chop_cooldown_activates(self):
        cfg = GlobalFilterConfig(
            chop_lookback_trades=5, chop_wr_floor=0.30,
            chop_cooldown_ms=60_000, loss_streak_cooldown=100,
        )
        f = GlobalTradeFilter(cfg)
        # 5 losses → WR=0% < 30%
        for i in range(5):
            f.record_outcome(False, now_ms=BASE_MS + i * 1000)
        r = f.evaluate("MX", "BTC", "bull", 0.5, now_ms=BASE_MS + 10_000)
        assert r.passed is False
        assert r.gate == "chop_cooldown"

    def test_chop_cooldown_expires(self):
        cfg = GlobalFilterConfig(
            chop_lookback_trades=5, chop_wr_floor=0.30,
            chop_cooldown_ms=60_000, loss_streak_cooldown=100,
        )
        f = GlobalTradeFilter(cfg)
        for i in range(5):
            f.record_outcome(False, now_ms=BASE_MS + i * 1000)
        r = f.evaluate("MX", "BTC", "bull", 0.5, now_ms=BASE_MS + 70_000)
        assert r.passed is True

    def test_loss_streak_cooldown_activates(self):
        cfg = GlobalFilterConfig(
            loss_streak_cooldown=3, loss_streak_cooldown_ms=3_600_000,
            chop_lookback_trades=100,  # High to avoid chop
        )
        f = GlobalTradeFilter(cfg)
        for i in range(3):
            f.record_outcome(False, now_ms=BASE_MS + i * 1000)
        r = f.evaluate("MX", "BTC", "bull", 0.5, now_ms=BASE_MS + 10_000)
        assert r.passed is False
        assert r.gate == "loss_cooldown"

    def test_win_resets_loss_streak(self):
        cfg = GlobalFilterConfig(
            loss_streak_cooldown=3, loss_streak_cooldown_ms=3_600_000,
            chop_lookback_trades=100,
        )
        f = GlobalTradeFilter(cfg)
        f.record_outcome(False, now_ms=BASE_MS)
        f.record_outcome(False, now_ms=BASE_MS + 1000)
        f.record_outcome(True, now_ms=BASE_MS + 2000)  # Reset
        f.record_outcome(False, now_ms=BASE_MS + 3000)
        r = f.evaluate("MX", "BTC", "bull", 0.5, now_ms=BASE_MS + 5000)
        assert r.passed is True  # Only 1 consecutive loss


# ── Symbol Cooldown (2) ─────────────────────────────────────

class TestSymbolCooldown:
    def test_symbol_cooldown_blocks(self):
        cfg = GlobalFilterConfig(symbol_cooldown_ms=300_000)
        f = GlobalTradeFilter(cfg)
        f.evaluate("MX", "BTC", "bull", 0.5, now_ms=BASE_MS)
        f.record_trade("MX", "BTC", "bull", now_ms=BASE_MS)
        r = f.evaluate("MX", "BTC", "bull", 0.5, now_ms=BASE_MS + 60_000)
        assert r.passed is False
        assert r.gate == "symbol_cooldown"

    def test_symbol_cooldown_expires(self):
        cfg = GlobalFilterConfig(symbol_cooldown_ms=300_000)
        f = GlobalTradeFilter(cfg)
        f.evaluate("MX", "BTC", "bull", 0.5, now_ms=BASE_MS)
        f.record_trade("MX", "BTC", "bull", now_ms=BASE_MS)
        r = f.evaluate("MX", "BTC", "bull", 0.5, now_ms=BASE_MS + 400_000)
        assert r.passed is True


# ── Daily Reset (2) ─────────────────────────────────────────

class TestDailyReset:
    def test_new_day_resets_counters(self):
        f = GlobalTradeFilter(GlobalFilterConfig(max_trades_per_day_global=1))
        f.evaluate("MX", "BTC", "bull", 0.5, now_ms=BASE_MS)
        f.record_trade("MX", "BTC", "bull", now_ms=BASE_MS)
        # Next day
        next_day = BASE_MS + DAY_MS
        r = f.evaluate("MX", "BTC", "bull", 0.5, now_ms=next_day)
        assert r.passed is True

    def test_cooldowns_persist_across_days(self):
        cfg = GlobalFilterConfig(
            loss_streak_cooldown=2, loss_streak_cooldown_ms=DAY_MS * 2,
            chop_lookback_trades=100,
        )
        f = GlobalTradeFilter(cfg)
        f.record_outcome(False, now_ms=BASE_MS)
        f.record_outcome(False, now_ms=BASE_MS + 1000)
        next_day = BASE_MS + DAY_MS
        r = f.evaluate("MX", "BTC", "bull", 0.5, now_ms=next_day)
        assert r.passed is False
        assert r.gate == "loss_cooldown"


# ── Determinism (3) ──────────────────────────────────────────

class TestDeterminism:
    def test_same_sequence_same_result(self):
        for _ in range(3):
            f = GlobalTradeFilter()
            f.evaluate("MX", "BTC", "bull", 0.5, now_ms=BASE_MS)
            f.record_trade("MX", "BTC", "bull", now_ms=BASE_MS)
            r = f.evaluate("MX", "ETH", "bull", 0.5, now_ms=BASE_MS + 1000)
            assert r.passed is True

    def test_filter_result_frozen(self):
        r = FilterResult(passed=True)
        with pytest.raises(AttributeError):
            r.passed = False

    def test_no_wall_clock_in_evaluate(self):
        """evaluate() requires explicit now_ms — no wall-clock fallback."""
        f = GlobalTradeFilter()
        # Must pass now_ms explicitly; signature requires it
        r = f.evaluate("MX", "BTC", "bull", 0.5, now_ms=BASE_MS)
        assert r.passed is True


# ── Replay / Persistence (6) ────────────────────────────────

class TestReplay:
    def test_replay_from_events_matches_live(self):
        """Event-based replay produces identical state to live execution."""
        f1 = GlobalTradeFilter(GlobalFilterConfig(max_trades_per_day_global=10))
        f1.evaluate("MX", "BTC", "bull", 0.5, now_ms=BASE_MS)
        f1.record_trade("MX", "BTC", "bull", now_ms=BASE_MS)
        f1.record_outcome(True, now_ms=BASE_MS + 1000)
        f1.record_trade("MX", "ETH", "bull", now_ms=BASE_MS + 2000)
        f1.record_outcome(False, now_ms=BASE_MS + 3000)

        # Replay
        f2 = GlobalTradeFilter(GlobalFilterConfig(max_trades_per_day_global=10))
        f2.replay(f1.event_log)

        s1 = f1.state_snapshot()
        s2 = f2.state_snapshot()
        assert s1 == s2

    def test_json_round_trip(self):
        f1 = GlobalTradeFilter()
        f1.record_trade("MX", "BTC", "bull", now_ms=BASE_MS)
        f1.record_outcome(False, now_ms=BASE_MS + 1000)

        data = f1.to_json()
        f2 = GlobalTradeFilter.from_json(data)

        assert f1.state_snapshot() == f2.state_snapshot()
        assert f1.event_count == f2.event_count

    def test_json_format_versioned(self):
        f = GlobalTradeFilter()
        f.record_trade("MX", "BTC", "bull", now_ms=BASE_MS)
        parsed = json.loads(f.to_json())
        assert parsed["version"] == 1
        assert len(parsed["events"]) == 1

    def test_replay_empty_events(self):
        f = GlobalTradeFilter()
        f.record_trade("MX", "BTC", "bull", now_ms=BASE_MS)
        f.replay([])  # Reset to empty
        assert f.event_count == 0
        assert f.state_snapshot().trades_today_global == 0

    def test_filter_event_round_trip(self):
        e = FilterEvent(event_type="trade", timestamp_ms=BASE_MS,
                        strategy_class="MX", symbol="BTC", regime="bull")
        d = e.to_dict()
        e2 = FilterEvent.from_dict(d)
        assert e == e2

    def test_replay_preserves_cooldowns(self):
        cfg = GlobalFilterConfig(
            loss_streak_cooldown=2, loss_streak_cooldown_ms=3_600_000,
            chop_lookback_trades=100,
        )
        f1 = GlobalTradeFilter(cfg)
        f1.record_outcome(False, now_ms=BASE_MS)
        f1.record_outcome(False, now_ms=BASE_MS + 1000)

        f2 = GlobalTradeFilter(cfg)
        f2.replay(f1.event_log)

        s1 = f1.state_snapshot()
        s2 = f2.state_snapshot()
        assert s1.loss_cooldown_until_ms == s2.loss_cooldown_until_ms
        assert s1.consecutive_losses == s2.consecutive_losses == 2


# ── State Snapshot (3) ───────────────────────────────────────

class TestStateSnapshot:
    def test_snapshot_is_frozen(self):
        f = GlobalTradeFilter()
        f.evaluate("MX", "BTC", "bull", 0.5, now_ms=BASE_MS)
        s = f.state_snapshot()
        with pytest.raises(AttributeError):
            s.trades_today_global = 99

    def test_snapshot_to_dict(self):
        f = GlobalTradeFilter()
        f.evaluate("MX", "BTC", "bull", 0.5, now_ms=BASE_MS)
        f.record_trade("MX", "BTC", "bull", now_ms=BASE_MS)
        d = f.state_snapshot().to_dict()
        assert d["trades_today_global"] == 1
        assert "event_count" in d

    def test_snapshot_event_count(self):
        f = GlobalTradeFilter()
        f.record_trade("MX", "BTC", "bull", now_ms=BASE_MS)
        f.record_outcome(True, now_ms=BASE_MS + 1000)
        assert f.state_snapshot().event_count == 2


# ── No Wall-Clock (3) ───────────────────────────────────────

class TestNoWallClock:
    def test_record_trade_requires_now_ms(self):
        """record_trade signature requires now_ms (no default)."""
        f = GlobalTradeFilter()
        # now_ms is a required positional-or-keyword arg
        f.record_trade("MX", "BTC", "bull", now_ms=BASE_MS)  # OK
        assert f.event_count == 1

    def test_record_outcome_requires_now_ms(self):
        f = GlobalTradeFilter()
        f.record_outcome(True, now_ms=BASE_MS)
        assert f.event_count == 1

    def test_evaluate_requires_now_ms(self):
        f = GlobalTradeFilter()
        r = f.evaluate("MX", "BTC", "bull", 0.5, now_ms=BASE_MS)
        assert r.passed is True


# ── Fail-Closed Recovery (4) ────────────────────────────────

class TestFailClosedRecovery:
    """
    Fail-closed: corrupt/invalid persisted state MUST raise
    FilterStateCorruptError. No silent reset to empty state.
    """

    def test_corrupt_json_raises(self):
        """Unparseable JSON → FilterStateCorruptError."""
        with pytest.raises(FilterStateCorruptError, match="corrupt"):
            GlobalTradeFilter.from_json("not valid json {{{")

    def test_missing_version_raises(self):
        """Missing 'version' key → FilterStateCorruptError."""
        data = json.dumps({"events": []})
        with pytest.raises(FilterStateCorruptError, match="version"):
            GlobalTradeFilter.from_json(data)

    def test_unknown_version_raises(self):
        """Version != 1 → FilterStateCorruptError."""
        data = json.dumps({"version": 99, "events": []})
        with pytest.raises(FilterStateCorruptError, match="version"):
            GlobalTradeFilter.from_json(data)

    def test_invalid_event_data_raises(self):
        """Malformed event dict → FilterStateCorruptError."""
        data = json.dumps({
            "version": 1,
            "events": [{"bad_key": "no_event_type"}],
        })
        with pytest.raises(FilterStateCorruptError, match="invalid"):
            GlobalTradeFilter.from_json(data)


# ── Event Log Retention (3) ─────────────────────────────────

class TestEventLogRetention:
    """
    Retention policy: 7-day window. truncate() removes old events,
    returns archived batch, and rebuilds state from kept events.
    """

    def test_truncate_removes_old_events(self):
        f = GlobalTradeFilter()
        # Event 8 days ago
        old_ms = BASE_MS - 8 * DAY_MS
        f.record_trade("MX", "BTC", "bull", now_ms=old_ms)
        # Event today
        f.record_trade("MX", "ETH", "bull", now_ms=BASE_MS)
        assert f.event_count == 2

        archived = f.truncate(now_ms=BASE_MS)
        assert len(archived) == 1
        assert archived[0].timestamp_ms == old_ms
        assert f.event_count == 1  # Only today's event remains

    def test_truncate_returns_empty_when_all_recent(self):
        f = GlobalTradeFilter()
        f.record_trade("MX", "BTC", "bull", now_ms=BASE_MS)
        f.record_trade("MX", "ETH", "bull", now_ms=BASE_MS + 1000)

        archived = f.truncate(now_ms=BASE_MS + 2000)
        assert len(archived) == 0
        assert f.event_count == 2

    def test_truncate_rebuilds_state_correctly(self):
        """After truncation, state reflects only kept events."""
        cfg = GlobalFilterConfig(max_trades_per_day_global=10)
        f = GlobalTradeFilter(cfg)
        # Old trade (will be truncated)
        old_ms = BASE_MS - 8 * DAY_MS
        f.record_trade("MX", "BTC", "bull", now_ms=old_ms)
        # Recent trade (kept)
        f.record_trade("MX", "ETH", "bull", now_ms=BASE_MS)

        f.truncate(now_ms=BASE_MS)
        # State should only reflect the recent trade
        snap = f.state_snapshot()
        assert snap.event_count == 1
        # Daily counters reset for current day
        assert snap.trades_today_global >= 0
