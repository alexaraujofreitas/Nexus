"""
tests/unit/test_notification_audit.py
======================================
Regression tests for notification audit fixes (Session 30).

Covers:
  NA-01  trade_closed direction: short trade shows "short" not "long"
  NA-02  trade_closed pnl: pnl_usdt mapped to template pnl key
  NA-03  trade_closed size: size_usdt formatted as $X,XXX.XX USDT
  NA-04  trade_closed strategy: models_fired list joined to string
  NA-05  trade_closed close_reason: exit_reason mapped correctly
  NA-06  trade_closed duration: duration_s formatted as Xm Ys
  NA-07  trade_closed long direction preserved when side=buy
  NA-08  trade_stopped direction: "sell" → "short"
  NA-09  trade_stopped direction: "buy" → "long"
  NA-10  daily_summary collects actual trading stats
  NA-11  strategy_signal normalises score → confidence
  NA-12  strategy_signal normalises models_fired → strategy
  NA-13  trade_rejected normalises score → confidence
  NA-14  candidate_approved normalises score → confidence
  NA-15  trade_closed pnl_pct preserved for short trade (negative price move is profit)
  NA-16  trade_closed duration_s < 60s formatted as Xs
  NA-17  trade_closed duration_s > 3600s formatted as Xh Ym
  NA-18  trade_opened template keys already normalised (regression)
"""
from __future__ import annotations

import sys, types, threading
from unittest.mock import MagicMock, patch, PropertyMock

import pytest


# ── Lightweight bus stub so notification_manager can be imported ───────────
class _FakeBus:
    def subscribe(self, *a, **kw): pass
    def unsubscribe(self, *a, **kw): pass
    def publish(self, *a, **kw): pass

class _FakeTopics:
    TRADE_OPENED     = "trade_opened"
    TRADE_CLOSED     = "trade_closed"
    ORDER_FILLED     = "order_filled"
    SIGNAL_REJECTED  = "signal_rejected"
    SIGNAL_CONFIRMED = "signal_confirmed"
    DRAWDOWN_ALERT   = "drawdown_alert"
    RISK_LIMIT_HIT   = "risk_limit_hit"
    EMERGENCY_STOP   = "emergency_stop"
    REGIME_CHANGED   = "regime_changed"
    EXCHANGE_ERROR   = "exchange_error"
    SYSTEM_ALERT     = "system_alert"
    CANDIDATE_APPROVED = "candidate_approved"
    FEED_STATUS      = "feed_status"
    POSITION_UPDATED = "position_updated"
    CANDLE_CLOSED    = "candle_closed"
    SYSTEM_WARNING   = "system_warning"

class _FakeEvent:
    def __init__(self, data):
        self.data = data

_fake_event_bus_mod = types.ModuleType("core.event_bus")
_fake_event_bus_mod.bus    = _FakeBus()
_fake_event_bus_mod.Topics = _FakeTopics()
_fake_event_bus_mod.Event  = _FakeEvent
sys.modules.setdefault("core.event_bus", _fake_event_bus_mod)

# Stub notification_templates with real module so we can test rendering
from core.notifications import notification_templates as tpl

from core.notifications.notification_manager import NotificationManager


# ── Helpers ────────────────────────────────────────────────────────────────

def _make_manager() -> NotificationManager:
    """Return a manager with no channels (no sends) but all handlers wired."""
    mgr = NotificationManager()
    # Don't call start() — we call handlers directly
    return mgr


def _captured_notify(mgr):
    """
    Patch mgr.notify to capture calls instead of sending them.
    Returns a list that receives (template_name, data) tuples.
    """
    calls = []
    original = mgr.notify

    def _fake_notify(template_name, data, dedup_key=None, channels=None):
        calls.append((template_name, dict(data)))
        return True

    mgr.notify = _fake_notify
    return calls


# ════════════════════════════════════════════════════════════════════════════
# NA-01 through NA-07  trade_closed key normalisation
# ════════════════════════════════════════════════════════════════════════════

class TestTradeClosedNormalisation:
    """Verify _on_trade_closed() maps executor keys → template keys correctly."""

    def _run(self, executor_data: dict) -> dict:
        mgr   = _make_manager()
        calls = _captured_notify(mgr)
        mgr._on_trade_closed(_FakeEvent(executor_data))
        assert len(calls) == 1, "Expected exactly one notify call"
        return calls[0][1]

    def test_na01_short_direction_mapped(self):
        """NA-01: side='sell' → direction='short' (not 'long')."""
        result = self._run({
            "symbol": "ETH/USDT", "side": "sell",
            "entry_price": 3000.0, "exit_price": 2900.0,
            "pnl_usdt": 100.0, "pnl_pct": 3.33,
            "size_usdt": 500.0, "exit_reason": "take_profit",
            "duration_s": 1800,
        })
        assert result["direction"] == "short", (
            f"Expected 'short', got '{result.get('direction')}'"
        )

    def test_na02_pnl_usdt_mapped_to_pnl(self):
        """NA-02: pnl_usdt value appears as 'pnl' key for template."""
        result = self._run({
            "symbol": "BTC/USDT", "side": "buy",
            "entry_price": 80000.0, "exit_price": 81000.0,
            "pnl_usdt": 62.50, "pnl_pct": 1.25,
            "size_usdt": 500.0, "exit_reason": "manual_close",
            "duration_s": 3600,
        })
        assert result.get("pnl") == 62.50, (
            f"Expected pnl=62.50, got {result.get('pnl')}"
        )

    def test_na03_size_usdt_formatted(self):
        """NA-03: size_usdt → size formatted as '$500.00 USDT'."""
        result = self._run({
            "symbol": "SOL/USDT", "side": "buy",
            "entry_price": 150.0, "exit_price": 155.0,
            "pnl_usdt": 16.67, "pnl_pct": 3.33,
            "size_usdt": 500.0, "exit_reason": "take_profit",
            "duration_s": 900,
        })
        size = result.get("size", "")
        assert "500" in size and "USDT" in size, (
            f"Expected formatted size string, got '{size}'"
        )

    def test_na04_models_fired_joined(self):
        """NA-04: models_fired list → strategy comma-separated string."""
        result = self._run({
            "symbol": "XRP/USDT", "side": "buy",
            "entry_price": 0.55, "exit_price": 0.57,
            "pnl_usdt": 18.18, "pnl_pct": 3.64,
            "size_usdt": 500.0, "models_fired": ["trend", "momentum_breakout"],
            "exit_reason": "take_profit", "duration_s": 600,
        })
        strategy = result.get("strategy", "")
        assert "trend" in strategy and "momentum_breakout" in strategy, (
            f"Expected joined strategy, got '{strategy}'"
        )

    def test_na05_exit_reason_mapped_to_close_reason(self):
        """NA-05: exit_reason → close_reason."""
        result = self._run({
            "symbol": "BNB/USDT", "side": "sell",
            "entry_price": 600.0, "exit_price": 580.0,
            "pnl_usdt": 16.67, "pnl_pct": 3.33,
            "size_usdt": 500.0, "exit_reason": "stop_loss",
            "duration_s": 300,
        })
        assert result.get("close_reason") == "stop_loss", (
            f"Expected 'stop_loss', got '{result.get('close_reason')}'"
        )

    def test_na06_duration_s_formatted_minutes(self):
        """NA-06: duration_s=1830 → '30m 30s'."""
        result = self._run({
            "symbol": "BTC/USDT", "side": "buy",
            "entry_price": 80000.0, "exit_price": 81000.0,
            "pnl_usdt": 62.5, "pnl_pct": 1.25,
            "size_usdt": 500.0, "exit_reason": "take_profit",
            "duration_s": 1830,
        })
        duration = result.get("duration", "")
        assert "30m" in duration and "30s" in duration, (
            f"Expected '30m 30s' in duration, got '{duration}'"
        )

    def test_na07_long_direction_preserved(self):
        """NA-07: side='buy' → direction='long'."""
        result = self._run({
            "symbol": "BTC/USDT", "side": "buy",
            "entry_price": 80000.0, "exit_price": 81000.0,
            "pnl_usdt": 62.5, "pnl_pct": 1.25,
            "size_usdt": 500.0, "exit_reason": "take_profit",
            "duration_s": 3600,
        })
        assert result["direction"] == "long", (
            f"Expected 'long', got '{result.get('direction')}'"
        )


# ════════════════════════════════════════════════════════════════════════════
# NA-08, NA-09  trade_stopped direction
# ════════════════════════════════════════════════════════════════════════════

class TestTradeStoppedDirection:

    def _run(self, side: str) -> dict:
        mgr   = _make_manager()
        calls = _captured_notify(mgr)
        mgr._on_order_filled(_FakeEvent({
            "symbol": "ETH/USDT", "order_type": "stop_market",
            "side": side, "fill_price": 2800.0,
            "entry_price": 3000.0, "realized_pnl": -100.0,
            "pnl_pct": -3.33,
        }))
        assert len(calls) == 1
        return calls[0][1]

    def test_na08_sell_maps_to_short(self):
        """NA-08: side='sell' → direction='short'."""
        result = self._run("sell")
        assert result["direction"] == "short", (
            f"Expected 'short', got '{result.get('direction')}'"
        )

    def test_na09_buy_maps_to_long(self):
        """NA-09: side='buy' → direction='long'."""
        result = self._run("buy")
        assert result["direction"] == "long", (
            f"Expected 'long', got '{result.get('direction')}'"
        )


# ════════════════════════════════════════════════════════════════════════════
# NA-10  daily_summary collects actual trading stats
# ════════════════════════════════════════════════════════════════════════════

class TestDailySummary:

    def test_na10_collects_trading_data(self):
        """NA-10: _send_daily_summary sends total_trades, wins, losses, etc."""
        from datetime import datetime, timezone, timedelta

        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        # Fake paper_executor
        fake_pe = MagicMock()
        fake_pe._capital = 100_300.0
        fake_pe._initial_capital = 100_000.0
        fake_pe._peak_capital = 100_300.0
        fake_pe._positions = {}
        fake_pe._closed_trades = [
            {"closed_at": f"{today_str}T10:00:00", "pnl_usdt": 150.0, "pnl_pct": 1.5},
            {"closed_at": f"{today_str}T11:00:00", "pnl_usdt": -50.0, "pnl_pct": -0.5},
            {"closed_at": "2020-01-01T00:00:00",   "pnl_usdt": 999.0, "pnl_pct": 9.9},  # not today
        ]
        fake_pe.get_stats.return_value = {
            "win_rate": 60.0, "total_trades": 3, "open_positions": 0,
        }
        fake_pe.available_capital = 100_300.0

        mgr   = _make_manager()
        calls = _captured_notify(mgr)

        with patch.dict("sys.modules", {"core.execution.paper_executor": types.ModuleType("x")}):
            import core.execution.paper_executor as _fake_mod
            _fake_mod.paper_executor = fake_pe
            sys.modules["core.execution.paper_executor"] = _fake_mod
            try:
                mgr._send_daily_summary()
            finally:
                # Restore original module
                del sys.modules["core.execution.paper_executor"]

        assert len(calls) == 1, "Expected one notify call"
        data = calls[0][1]
        assert data.get("total_trades") == 2, (
            f"Expected 2 today trades, got {data.get('total_trades')}"
        )
        assert data.get("wins") == 1
        assert data.get("losses") == 1
        # daily_pnl should be 150 + (-50) = 100
        assert abs(data.get("daily_pnl", 0) - 100.0) < 0.5, (
            f"Expected daily_pnl ~100.0, got {data.get('daily_pnl')}"
        )


# ════════════════════════════════════════════════════════════════════════════
# NA-11, NA-12  strategy_signal normalisation
# ════════════════════════════════════════════════════════════════════════════

class TestStrategySignalNormalisation:

    def _run_confirmed(self, event_data: dict) -> dict:
        mgr   = _make_manager()
        calls = _captured_notify(mgr)
        mgr._on_signal_confirmed(_FakeEvent(event_data))
        assert len(calls) == 1
        return calls[0][1]

    def test_na11_score_mapped_to_confidence(self):
        """NA-11: score → confidence for strategy_signal."""
        result = self._run_confirmed({
            "symbol": "BTC/USDT", "score": 0.82,
            "direction": "long", "regime": "bull_trend",
        })
        assert abs(result.get("confidence", -1) - 0.82) < 0.001

    def test_na12_models_fired_mapped_to_strategy(self):
        """NA-12: models_fired → strategy for strategy_signal."""
        result = self._run_confirmed({
            "symbol": "ETH/USDT", "score": 0.75,
            "direction": "long", "models_fired": ["trend", "order_book"],
        })
        strategy = result.get("strategy", "")
        assert "trend" in strategy and "order_book" in strategy


# ════════════════════════════════════════════════════════════════════════════
# NA-13  trade_rejected normalisation
# ════════════════════════════════════════════════════════════════════════════

class TestTradeRejectedNormalisation:

    def test_na13_score_mapped_to_confidence(self):
        """NA-13: score → confidence for trade_rejected."""
        mgr   = _make_manager()
        calls = _captured_notify(mgr)
        mgr._on_signal_rejected(_FakeEvent({
            "symbol": "SOL/USDT", "score": 0.38,
            "reason": "EV gate", "regime": "ranging",
        }))
        assert len(calls) == 1
        data = calls[0][1]
        assert abs(data.get("confidence", -1) - 0.38) < 0.001


# ════════════════════════════════════════════════════════════════════════════
# NA-14  candidate_approved normalisation
# ════════════════════════════════════════════════════════════════════════════

class TestCandidateApprovedNormalisation:

    def test_na14_score_mapped_to_confidence(self):
        """NA-14: score → confidence for candidate_approved."""
        mgr   = _make_manager()
        calls = _captured_notify(mgr)
        mgr._on_candidate_approved(_FakeEvent({
            "symbol": "XRP/USDT", "score": 0.91,
            "direction": "long", "models_fired": ["trend"],
        }))
        assert len(calls) == 1
        data = calls[0][1]
        assert abs(data.get("confidence", -1) - 0.91) < 0.001


# ════════════════════════════════════════════════════════════════════════════
# NA-15  short P&L sign correctness
# ════════════════════════════════════════════════════════════════════════════

class TestShortPnLCorrectness:

    def test_na15_short_pnl_pct_positive_when_price_falls(self):
        """NA-15: For a short that profits (price falls), pnl_pct must be positive."""
        mgr   = _make_manager()
        calls = _captured_notify(mgr)
        mgr._on_trade_closed(_FakeEvent({
            "symbol": "ETH/USDT", "side": "sell",
            "entry_price": 3000.0, "exit_price": 2850.0,
            # pnl_pct computed externally: (3000-2850)/3000 * 100 = 5.0%
            "pnl_pct": 5.0, "pnl_usdt": 25.0,
            "size_usdt": 500.0, "exit_reason": "take_profit",
            "duration_s": 900,
        }))
        assert len(calls) == 1
        data = calls[0][1]
        assert data.get("direction") == "short"
        assert data.get("pnl") == 25.0    # pnl_usdt correctly mapped
        assert data.get("pnl_pct") == 5.0  # unchanged (already correct key)


# ════════════════════════════════════════════════════════════════════════════
# NA-16, NA-17  duration formatting edge cases
# ════════════════════════════════════════════════════════════════════════════

class TestDurationFormatting:

    def _get_duration(self, duration_s: int) -> str:
        mgr   = _make_manager()
        calls = _captured_notify(mgr)
        mgr._on_trade_closed(_FakeEvent({
            "symbol": "BTC/USDT", "side": "buy",
            "entry_price": 80000.0, "exit_price": 81000.0,
            "pnl_usdt": 62.5, "pnl_pct": 1.25,
            "size_usdt": 500.0, "exit_reason": "take_profit",
            "duration_s": duration_s,
        }))
        return calls[0][1].get("duration", "")

    def test_na16_under_60s(self):
        """NA-16: duration_s=45 → '45s'."""
        assert self._get_duration(45) == "45s"

    def test_na17_over_3600s(self):
        """NA-17: duration_s=5400 → '1h 30m'."""
        assert self._get_duration(5400) == "1h 30m"


# ════════════════════════════════════════════════════════════════════════════
# NA-18  trade_opened regression (already worked — ensure it still does)
# ════════════════════════════════════════════════════════════════════════════

class TestTradeOpenedRegression:

    def test_na18_trade_opened_normalization(self):
        """NA-18: trade_opened handler normalises side/size_usdt/score/models_fired."""
        mgr   = _make_manager()
        calls = _captured_notify(mgr)
        mgr._on_trade_opened(_FakeEvent({
            "symbol": "BTC/USDT", "side": "sell",
            "entry_price": 80000.0, "size_usdt": 500.0,
            "stop_loss": 82000.0, "take_profit": 76000.0,
            "score": 0.80, "models_fired": ["trend"],
            "timeframe": "1h", "regime": "bear_trend",
            "rationale": "Strong sell signal",
        }))
        assert len(calls) == 1
        data = calls[0][1]
        assert data["direction"] == "short"
        assert "500" in data["size"]
        assert abs(data["confidence"] - 0.80) < 0.001
        assert "trend" in data["strategy"]
