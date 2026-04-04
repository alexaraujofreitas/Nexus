"""
tests/unit/test_daily_loss_limit.py — Daily Loss Limit Kill Switch (DLL-001 to DLL-009)

Tests the daily loss limit introduced in Wave 1 hardening:
  - Blocks new position entries once today's realized P&L breaches the threshold
  - Auto-resets at UTC midnight (simulated by date change)
  - Fires Topics.SYSTEM_ALERT on first breach
  - Never affects already-open positions
  - Disabled when daily_loss_limit_pct = 0.0
  - Survives restart (in-memory history loaded by _load_history mirrors today's trades)

Fixture: paper_executor (10_000 USDT initial capital)
Threshold at default 2.0%: -200 USDT realized today triggers kill switch.

Note on approach:
  _closed_trades is injected directly because _load_history() reads from the DB,
  but the test DB is in-memory and empty. Direct injection simulates what
  _load_history() would have produced after a restart with existing trade history.
  This is the same pattern as how notification_manager computes daily_pnl.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock

import pytest

from core.event_bus import Topics


# ── helpers ───────────────────────────────────────────────────────────────────

def _today_utc() -> str:
    """Return today's UTC date prefix: 'YYYY-MM-DD'."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _yesterday_utc() -> str:
    """Return yesterday's UTC date prefix."""
    return (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")


def _trade(pnl_usdt: float, date_prefix: str | None = None) -> dict:
    """
    Minimal closed-trade dict that _get_today_realized_pnl() can read.
    If date_prefix is None, defaults to today (UTC).
    """
    prefix = date_prefix or _today_utc()
    return {
        "pnl_usdt":  pnl_usdt,
        "closed_at": f"{prefix}T12:00:00",
    }


def _make_candidate():
    """
    Minimal OrderCandidate-like object sufficient for submit() early-exit tests.
    The daily loss limit check fires before any candidate field is read,
    so only the symbol attribute must exist for logging.
    """
    from core.meta_decision.order_candidate import OrderCandidate
    cand = MagicMock(spec=OrderCandidate)
    cand.symbol = "BTC/USDT"
    cand.side   = "buy"
    return cand


# ══════════════════════════════════════════════════════════════════════════════
#  DLL-001 — No limit when no losses
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
def test_no_limit_when_no_losses(paper_executor):
    """Fresh executor with zero closed trades: limit is not hit."""
    pe = paper_executor
    pe._closed_trades.clear()

    assert pe._check_daily_loss_limit() is False
    assert pe.is_daily_limit_hit is False
    assert pe.today_realized_pnl == 0.0


# ══════════════════════════════════════════════════════════════════════════════
#  DLL-002 — Partial loss does not trigger limit
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
def test_partial_loss_no_trigger(paper_executor):
    """
    Today's realized loss is -1.0% (below 2.0% threshold).
    Limit must NOT fire.
    """
    pe = paper_executor
    pe._closed_trades.clear()
    # -$100 on a $10,000 account = -1.0% < threshold of -2.0%
    pe._closed_trades.append(_trade(-100.0))

    assert pe._check_daily_loss_limit() is False
    assert pe.is_daily_limit_hit is False


# ══════════════════════════════════════════════════════════════════════════════
#  DLL-003 — Limit triggers at exactly the threshold
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
def test_limit_triggers_at_threshold(paper_executor):
    """
    Today's realized loss equals exactly -2.0% of initial capital.
    Limit must fire (boundary: pnl <= threshold).
    """
    pe = paper_executor
    pe._closed_trades.clear()
    # $10,000 × 2.0% = $200 threshold
    pe._closed_trades.append(_trade(-200.0))

    assert pe._check_daily_loss_limit() is True
    assert pe.is_daily_limit_hit is True
    assert pe.today_realized_pnl == pytest.approx(-200.0)


# ══════════════════════════════════════════════════════════════════════════════
#  DLL-004 — Limit triggers when loss exceeds threshold
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
def test_limit_triggers_above_threshold(paper_executor):
    """
    Multiple losing trades summing to -$350 today.  Exceeds -$200 threshold.
    """
    pe = paper_executor
    pe._closed_trades.clear()
    pe._closed_trades.extend([_trade(-150.0), _trade(-200.0)])

    assert pe._check_daily_loss_limit() is True
    assert pe.today_realized_pnl == pytest.approx(-350.0)


# ══════════════════════════════════════════════════════════════════════════════
#  DLL-005 — submit() is blocked when limit is active
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
def test_submit_blocked_when_limit_hit(paper_executor):
    """
    Once the daily limit fires, submit() must return False immediately.
    The candidate is never evaluated further.
    """
    pe = paper_executor
    pe._closed_trades.clear()
    pe._closed_trades.append(_trade(-200.0))  # hit threshold

    # Verify limit is active before testing submit
    assert pe._check_daily_loss_limit() is True

    result = pe.submit(_make_candidate())
    assert result is False


# ══════════════════════════════════════════════════════════════════════════════
#  DLL-006 — Limit disabled when pct = 0.0
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
def test_limit_disabled_when_zero(paper_executor):
    """
    Setting daily_loss_limit_pct = 0.0 turns the feature off entirely.
    Even catastrophic losses today do not trigger the kill switch.
    """
    pe = paper_executor
    pe._daily_loss_limit_pct = 0.0
    pe._closed_trades.clear()
    pe._closed_trades.append(_trade(-9_000.0))  # -90% loss

    assert pe._check_daily_loss_limit() is False
    assert pe.is_daily_limit_hit is False


# ══════════════════════════════════════════════════════════════════════════════
#  DLL-007 — SYSTEM_ALERT published on first breach
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
def test_system_alert_published_on_breach(paper_executor):
    """
    When the limit fires for the first time today, a Topics.SYSTEM_ALERT
    event must be published with the correct payload keys.
    """
    from core.event_bus import bus

    published_events = []

    def capture(event):
        published_events.append(event)

    bus.subscribe(Topics.SYSTEM_ALERT, capture)
    try:
        pe = paper_executor
        pe._closed_trades.clear()
        pe._closed_trades.append(_trade(-200.0))  # hit threshold
        pe._daily_loss_limit_hit = False           # ensure it fires fresh

        pe._check_daily_loss_limit()

        # Must have at least one alert
        assert len(published_events) >= 1
        # Find our daily loss limit event (may be mixed with other alerts)
        dll_events = [
            e for e in published_events
            if (hasattr(e, "data") and isinstance(e.data, dict) and
                e.data.get("type") == "daily_loss_limit_hit")
        ]
        assert len(dll_events) >= 1, "Expected 'daily_loss_limit_hit' alert not found"

        payload = dll_events[0].data
        assert "today_pnl_usdt"   in payload
        assert "threshold_usdt"   in payload
        assert "limit_pct"        in payload
        assert "date"             in payload
        assert payload["today_pnl_usdt"] == pytest.approx(-200.0)
        assert payload["limit_pct"]      == pytest.approx(2.0)
    finally:
        bus.unsubscribe(Topics.SYSTEM_ALERT, capture)


# ══════════════════════════════════════════════════════════════════════════════
#  DLL-008 — Auto-reset when UTC date changes
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
def test_auto_reset_on_date_change(paper_executor):
    """
    Simulate the limit firing 'yesterday'. Today is a new UTC date,
    so the flag must auto-reset and the limit must not be active.
    """
    pe = paper_executor
    pe._closed_trades.clear()

    # Inject yesterday's losing trades only — today is clean
    pe._closed_trades.append(_trade(-200.0, date_prefix=_yesterday_utc()))

    # Manually set the flag as if it fired yesterday
    pe._daily_loss_limit_hit = True
    pe._daily_loss_limit_date = _yesterday_utc()

    # Calling _check_daily_loss_limit() should auto-reset because date changed
    result = pe._check_daily_loss_limit()

    # Today has no losses, so after reset it must be False
    assert result is False
    assert pe.is_daily_limit_hit is False
    assert pe._daily_loss_limit_date == ""


# ══════════════════════════════════════════════════════════════════════════════
#  DLL-009 — Restart recovery: today's history already in _closed_trades
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
def test_restart_recovery(paper_executor):
    """
    Simulates a restart where _load_history() has already populated
    _closed_trades with today's losing trades before the first submit().
    The kill switch must detect the breach from the in-memory history,
    exactly as notification_manager does.
    """
    pe = paper_executor
    # Simulate history loaded from DB (as _load_history would do)
    pe._closed_trades.clear()
    pe._closed_trades.extend([
        _trade(-80.0),   # loss 1 today
        _trade(-60.0),   # loss 2 today
        _trade(-70.0),   # loss 3 today
        # total: -210 USDT = -2.1% > 2.0% threshold
    ])

    assert pe._check_daily_loss_limit() is True
    # And a new submit attempt is also blocked
    assert pe.submit(_make_candidate()) is False


# ══════════════════════════════════════════════════════════════════════════════
#  DLL-010 — Open positions not closed when limit fires
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
def test_open_positions_unaffected_by_daily_limit(paper_executor):
    """
    Daily loss limit blocks NEW entries only.
    Positions already open must remain open after the limit fires.
    """
    from core.execution.paper_executor import PaperPosition

    pe = paper_executor

    # Manually plant an open position
    pos = PaperPosition(
        symbol      = "BTC/USDT",
        side        = "buy",
        entry_price = 90_000.0,
        quantity    = 0.001,
        stop_loss   = 89_000.0,
        take_profit = 92_000.0,
        size_usdt   = 90.0,
        score       = 0.65,
        rationale   = "test",
    )
    pe._positions["BTC/USDT"] = [pos]

    # Now trigger the daily limit via closed trade losses
    pe._closed_trades.append(_trade(-200.0))
    assert pe._check_daily_loss_limit() is True

    # Existing position must still be there
    assert len(pe._positions.get("BTC/USDT", [])) == 1
    assert pe._positions["BTC/USDT"][0].symbol == "BTC/USDT"

    # New submit must be rejected
    assert pe.submit(_make_candidate()) is False

    # But existing position is unaffected
    assert len(pe._positions.get("BTC/USDT", [])) == 1


# ══════════════════════════════════════════════════════════════════════════════
#  DLL-011 — get_stats() exposes daily limit state
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
def test_get_stats_exposes_daily_limit(paper_executor):
    """
    get_stats() must include daily loss limit keys so UI / monitoring can display them.
    """
    pe = paper_executor
    pe._closed_trades.clear()
    stats = pe.get_stats()

    assert "daily_loss_limit_pct"  in stats
    assert "daily_loss_limit_hit"  in stats
    assert "today_realized_pnl"    in stats

    assert stats["daily_loss_limit_pct"]  == pytest.approx(2.0)
    assert stats["daily_loss_limit_hit"]  is False
    assert stats["today_realized_pnl"]    == pytest.approx(0.0)


# ══════════════════════════════════════════════════════════════════════════════
#  DLL-012 — Alert fires only once, not on every subsequent check
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
def test_alert_fires_only_once_per_day(paper_executor):
    """
    Calling _check_daily_loss_limit() multiple times after the limit fires
    must NOT publish additional SYSTEM_ALERT events on the second+ call.
    """
    from core.event_bus import bus

    published_events = []

    def capture(event):
        if (hasattr(event, "data") and isinstance(event.data, dict) and
                event.data.get("type") == "daily_loss_limit_hit"):
            published_events.append(event)

    bus.subscribe(Topics.SYSTEM_ALERT, capture)
    try:
        pe = paper_executor
        pe._closed_trades.clear()
        pe._closed_trades.append(_trade(-200.0))
        pe._daily_loss_limit_hit = False

        # First call fires the alert
        pe._check_daily_loss_limit()
        count_after_first = len(published_events)

        # Subsequent calls — flag is already set, no new alert
        pe._check_daily_loss_limit()
        pe._check_daily_loss_limit()

        assert len(published_events) == count_after_first == 1
    finally:
        bus.unsubscribe(Topics.SYSTEM_ALERT, capture)


# ══════════════════════════════════════════════════════════════════════════════
#  Capital Utilization Monitor — basic integration
# ══════════════════════════════════════════════════════════════════════════════

class TestCapitalUtilizationMonitor:
    """CMU-001 to CMU-003 — basic snapshot correctness."""

    @pytest.mark.unit
    def test_zero_positions_utilization(self, paper_executor):
        """With no open positions, utilization must be 0%."""
        from core.monitoring.capital_utilization_monitor import capital_utilization_monitor

        pe = paper_executor
        pe._positions.clear()
        pe._closed_trades.clear()

        snap = capital_utilization_monitor.get_snapshot(pe)

        assert snap["utilization_pct"] == pytest.approx(0.0)
        assert snap["locked_usdt"]     == pytest.approx(0.0)
        assert snap["open_positions"]  == 0

    @pytest.mark.unit
    def test_snapshot_keys_present(self, paper_executor):
        """All required keys must be present in every snapshot."""
        from core.monitoring.capital_utilization_monitor import capital_utilization_monitor

        required_keys = {
            "capital_usdt", "initial_capital_usdt", "peak_capital_usdt",
            "locked_usdt", "available_usdt", "utilization_pct", "deployable_usdt",
            "open_positions", "max_concurrent", "open_symbols",
            "portfolio_heat_pct", "max_heat_pct", "drawdown_pct",
            "drawdown_circuit_on", "daily_loss_limit_hit", "daily_loss_limit_pct",
            "today_realized_pnl", "today_pnl_pct", "limiting_factor",
        }
        snap = capital_utilization_monitor.get_snapshot(paper_executor)
        missing = required_keys - set(snap.keys())
        assert not missing, f"Missing keys in snapshot: {missing}"

    @pytest.mark.unit
    def test_daily_limit_reflected_in_snapshot(self, paper_executor):
        """When daily limit fires, snapshot must show daily_loss_limit_hit = True."""
        from core.monitoring.capital_utilization_monitor import capital_utilization_monitor

        pe = paper_executor
        pe._closed_trades.clear()
        pe._closed_trades.append(_trade(-200.0))  # trigger limit
        pe._check_daily_loss_limit()              # prime the flag

        snap = capital_utilization_monitor.get_snapshot(pe)
        assert snap["daily_loss_limit_hit"] is True
        assert snap["limiting_factor"] == "daily_loss_limit"
