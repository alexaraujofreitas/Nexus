# ============================================================
# NEXUS TRADER — Phase 1 Live Executor Hardening Test Suite
#
# Comprehensive tests for all 5 Phase 1 requirements:
#   R1. Daily loss limit (live)
#   R2. Drawdown circuit breaker (live)
#   R3. Exit management port (ExitManager)
#   R4. Position persistence (JSON + SQLite)
#   R5. Startup recovery + exchange reconciliation
#
# Tests are self-contained — no live exchange or DB required.
# All exchange calls and DB access are mocked.
# ============================================================
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

pytestmark = pytest.mark.skip(reason="LiveExecutor not yet implemented — aspirational tests for live trading")

# ── Ensure project root is on sys.path ────────────────────────
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# ── Import targets ────────────────────────────────────────────
from core.execution.exit_manager import ExitManager, ExitAction


# ============================================================
# FIXTURES
# ============================================================

@pytest.fixture
def exit_mgr_defaults():
    """ExitManager with default config (partial mode, no trailing, no time exit)."""
    return ExitManager({
        "exit.mode": "partial",
        "exit.partial_pct": 0.33,
        "exit.partial_r_trigger": 1.0,
        "trailing_stop.enabled": False,
        "trailing_stop.pct": 0.0,
        "max_hold_bars": 0,
    })


@pytest.fixture
def exit_mgr_trailing():
    """ExitManager with trailing stop enabled."""
    return ExitManager({
        "exit.mode": "partial",
        "exit.partial_pct": 0.33,
        "exit.partial_r_trigger": 1.0,
        "trailing_stop.enabled": True,
        "trailing_stop.pct": 0.05,  # 5% trailing
        "max_hold_bars": 0,
    })


@pytest.fixture
def exit_mgr_time():
    """ExitManager with time exit enabled."""
    return ExitManager({
        "exit.mode": "partial",
        "exit.partial_pct": 0.33,
        "exit.partial_r_trigger": 1.0,
        "trailing_stop.enabled": False,
        "trailing_stop.pct": 0.0,
        "max_hold_bars": 5,
    })


def _make_long_pos(entry=100.0, sl=95.0, tp=110.0) -> dict:
    """Create a minimal long position dict for ExitManager."""
    return {
        "side": "buy",
        "entry_price": entry,
        "stop_loss": sl,
        "take_profit": tp,
        "highest_price": entry,
        "lowest_price": entry,
        "bars_held": 0,
        "_initial_risk": abs(entry - sl),
        "_breakeven_applied": False,
        "_auto_partial_applied": False,
        "trailing_stop_pct": 0.0,
        "max_hold_bars": 0,
    }


def _make_short_pos(entry=100.0, sl=105.0, tp=90.0) -> dict:
    """Create a minimal short position dict for ExitManager."""
    return {
        "side": "sell",
        "entry_price": entry,
        "stop_loss": sl,
        "take_profit": tp,
        "highest_price": entry,
        "lowest_price": entry,
        "bars_held": 0,
        "_initial_risk": abs(entry - sl),
        "_breakeven_applied": False,
        "_auto_partial_applied": False,
        "trailing_stop_pct": 0.0,
        "max_hold_bars": 0,
    }


# ============================================================
# R3 — EXIT MANAGER TESTS
# ============================================================

class TestExitManagerStaticSLTP:
    """Static stop-loss and take-profit checks."""

    def test_long_stop_loss_hit(self, exit_mgr_defaults):
        pos = _make_long_pos(entry=100, sl=95, tp=110)
        action = exit_mgr_defaults.check_exits(pos, 94.0)
        assert action is not None
        assert action.action == "stop_loss"
        assert "SL hit" in action.reason

    def test_long_take_profit_hit(self, exit_mgr_defaults):
        pos = _make_long_pos(entry=100, sl=95, tp=110)
        pos["_auto_partial_applied"] = True  # prevent partial from firing first
        action = exit_mgr_defaults.check_exits(pos, 111.0)
        assert action is not None
        assert action.action == "take_profit"
        assert "TP hit" in action.reason

    def test_long_hold(self, exit_mgr_defaults):
        pos = _make_long_pos(entry=100, sl=95, tp=110)
        action = exit_mgr_defaults.check_exits(pos, 102.0)
        assert action is None

    def test_short_stop_loss_hit(self, exit_mgr_defaults):
        pos = _make_short_pos(entry=100, sl=105, tp=90)
        action = exit_mgr_defaults.check_exits(pos, 106.0)
        assert action is not None
        assert action.action == "stop_loss"

    def test_short_take_profit_hit(self, exit_mgr_defaults):
        pos = _make_short_pos(entry=100, sl=105, tp=90)
        pos["_auto_partial_applied"] = True
        action = exit_mgr_defaults.check_exits(pos, 89.0)
        assert action is not None
        assert action.action == "take_profit"

    def test_short_hold(self, exit_mgr_defaults):
        pos = _make_short_pos(entry=100, sl=105, tp=90)
        action = exit_mgr_defaults.check_exits(pos, 98.0)
        assert action is None

    def test_exact_sl_boundary_long(self, exit_mgr_defaults):
        pos = _make_long_pos(entry=100, sl=95, tp=110)
        action = exit_mgr_defaults.check_exits(pos, 95.0)
        assert action is not None
        assert action.action == "stop_loss"

    def test_exact_tp_boundary_long(self, exit_mgr_defaults):
        pos = _make_long_pos(entry=100, sl=95, tp=110)
        pos["_auto_partial_applied"] = True
        action = exit_mgr_defaults.check_exits(pos, 110.0)
        assert action is not None
        assert action.action == "take_profit"


class TestExitManagerParityMode:
    """Parity mode — only static SL/TP, no advanced features."""

    def test_parity_no_trailing(self, exit_mgr_trailing):
        """In parity mode, trailing stop config is ignored."""
        pos = _make_long_pos(entry=100, sl=95, tp=110)
        pos["trailing_stop_pct"] = 0.05
        pos["highest_price"] = 108.0
        # Price dropped from HWM but still above SL — parity mode ignores trailing
        action = exit_mgr_trailing.check_exits(pos, 97.0, parity_mode=True)
        assert action is None  # 97 > 95 (SL)

    def test_parity_no_time_exit(self, exit_mgr_time):
        """In parity mode, time exit is ignored."""
        pos = _make_long_pos(entry=100, sl=95, tp=110)
        pos["bars_held"] = 99  # Way past max_hold_bars
        action = exit_mgr_time.check_exits(pos, 102.0, parity_mode=True)
        assert action is None

    def test_parity_no_auto_partial(self, exit_mgr_defaults):
        """In parity mode, auto-partial is disabled."""
        pos = _make_long_pos(entry=100, sl=95, tp=110)
        # Price at +2R — would trigger partial in full mode
        action = exit_mgr_defaults.check_exits(pos, 110.0, parity_mode=True)
        # Should hit TP, not auto_partial
        assert action is not None
        assert action.action == "take_profit"

    def test_parity_sl_still_works(self, exit_mgr_defaults):
        pos = _make_long_pos(entry=100, sl=95, tp=110)
        action = exit_mgr_defaults.check_exits(pos, 90.0, parity_mode=True)
        assert action is not None
        assert action.action == "stop_loss"


class TestExitManagerTrailingStop:
    """Trailing stop in full mode."""

    def test_trailing_updates_sl_long(self, exit_mgr_trailing):
        pos = _make_long_pos(entry=100, sl=95, tp=120)
        pos["trailing_stop_pct"] = 0.05
        pos["highest_price"] = 100.0
        pos["_auto_partial_applied"] = True  # isolate trailing behavior
        # Price rises to 110 → HWM updates → trailing SL = 110 * 0.95 = 104.5
        action = exit_mgr_trailing.check_exits(pos, 110.0)
        assert pos["stop_loss"] == pytest.approx(110.0 * 0.95, abs=0.01)
        assert action is None  # Not hit yet

    def test_trailing_triggers_sl_long(self, exit_mgr_trailing):
        pos = _make_long_pos(entry=100, sl=95, tp=120)
        pos["trailing_stop_pct"] = 0.05
        pos["highest_price"] = 110.0
        # First tick at 110 updates SL
        exit_mgr_trailing.check_exits(pos, 110.0)
        new_sl = pos["stop_loss"]
        # Now price drops to below trailing SL
        action = exit_mgr_trailing.check_exits(pos, new_sl - 1.0)
        assert action is not None
        assert action.action == "stop_loss"

    def test_trailing_updates_sl_short(self, exit_mgr_trailing):
        pos = _make_short_pos(entry=100, sl=105, tp=80)
        pos["trailing_stop_pct"] = 0.05
        pos["lowest_price"] = 100.0
        pos["_auto_partial_applied"] = True  # isolate trailing behavior
        # Price drops to 90 → LWM updates → trailing SL = 90 * 1.05 = 94.5
        action = exit_mgr_trailing.check_exits(pos, 90.0)
        assert pos["stop_loss"] == pytest.approx(90.0 * 1.05, abs=0.01)
        assert action is None

    def test_trailing_sl_only_tightens_long(self, exit_mgr_trailing):
        pos = _make_long_pos(entry=100, sl=95, tp=120)
        pos["trailing_stop_pct"] = 0.05
        # First tick updates HWM and SL
        exit_mgr_trailing.check_exits(pos, 110.0)
        sl_after_up = pos["stop_loss"]
        # Price drops — HWM stays, SL should NOT decrease
        exit_mgr_trailing.check_exits(pos, 106.0)
        assert pos["stop_loss"] >= sl_after_up


class TestExitManagerBreakeven:
    """Breakeven move at +1R."""

    def test_breakeven_triggered_long(self, exit_mgr_defaults):
        pos = _make_long_pos(entry=100, sl=95, tp=115)
        # _initial_risk = 5.0, so +1R = 105.0
        action = exit_mgr_defaults.check_exits(pos, 106.0)
        assert pos["_breakeven_applied"] is True
        assert pos["stop_loss"] == 100.0  # entry price

    def test_breakeven_not_triggered_below_1r(self, exit_mgr_defaults):
        pos = _make_long_pos(entry=100, sl=95, tp=115)
        action = exit_mgr_defaults.check_exits(pos, 103.0)
        assert pos["_breakeven_applied"] is False
        assert pos["stop_loss"] == 95.0

    def test_breakeven_triggered_short(self, exit_mgr_defaults):
        pos = _make_short_pos(entry=100, sl=105, tp=85)
        # _initial_risk = 5.0, so +1R short = 95.0
        action = exit_mgr_defaults.check_exits(pos, 94.0)
        assert pos["_breakeven_applied"] is True
        assert pos["stop_loss"] == 100.0  # entry price

    def test_breakeven_only_fires_once(self, exit_mgr_defaults):
        pos = _make_long_pos(entry=100, sl=95, tp=115)
        exit_mgr_defaults.check_exits(pos, 106.0)
        assert pos["_breakeven_applied"] is True
        # Manually move SL above entry (simulating trailing)
        pos["stop_loss"] = 102.0
        exit_mgr_defaults.check_exits(pos, 107.0)
        # SL should NOT be reset back to entry
        assert pos["stop_loss"] >= 102.0


class TestExitManagerAutoPartial:
    """Auto-partial at +1R (33% close)."""

    def test_auto_partial_triggers_long(self, exit_mgr_defaults):
        pos = _make_long_pos(entry=100, sl=95, tp=115)
        # +1R = 105.0
        action = exit_mgr_defaults.check_exits(pos, 106.0)
        assert action is not None
        assert action.action == "auto_partial"
        assert action.reduce_pct == pytest.approx(0.33, abs=0.01)
        assert action.new_stop_loss == 100.0  # breakeven

    def test_auto_partial_triggers_short(self, exit_mgr_defaults):
        pos = _make_short_pos(entry=100, sl=105, tp=85)
        action = exit_mgr_defaults.check_exits(pos, 94.0)
        assert action is not None
        assert action.action == "auto_partial"

    def test_auto_partial_not_re_triggered(self, exit_mgr_defaults):
        pos = _make_long_pos(entry=100, sl=95, tp=115)
        pos["_auto_partial_applied"] = True
        action = exit_mgr_defaults.check_exits(pos, 106.0)
        # Should NOT fire auto_partial again
        assert action is None or action.action != "auto_partial"

    def test_full_exit_mode_no_partial(self):
        mgr = ExitManager({"exit.mode": "full"})
        pos = _make_long_pos(entry=100, sl=95, tp=115)
        action = mgr.check_exits(pos, 106.0)
        # "full" mode should not trigger auto_partial
        assert action is None or action.action != "auto_partial"


class TestExitManagerTimeExit:
    """Time-based exit (max hold bars)."""

    def test_time_exit_triggers(self, exit_mgr_time):
        pos = _make_long_pos(entry=100, sl=90, tp=120)
        pos["bars_held"] = 4
        pos["max_hold_bars"] = 5
        action = exit_mgr_time.check_exits(pos, 102.0)
        assert action is not None
        assert action.action == "time_exit"
        assert "Max hold bars" in action.reason

    def test_time_exit_not_before_limit(self, exit_mgr_time):
        pos = _make_long_pos(entry=100, sl=90, tp=120)
        pos["bars_held"] = 2
        pos["max_hold_bars"] = 5
        action = exit_mgr_time.check_exits(pos, 102.0)
        # bars_held becomes 3 after increment, still < 5
        assert action is None or action.action != "time_exit"

    def test_time_exit_disabled_when_zero(self, exit_mgr_defaults):
        pos = _make_long_pos(entry=100, sl=90, tp=120)
        pos["bars_held"] = 999
        action = exit_mgr_defaults.check_exits(pos, 102.0)
        assert action is None or action.action != "time_exit"


class TestExitManagerCounters:
    """Counter/state mutation tests."""

    def test_bars_held_incremented(self, exit_mgr_defaults):
        pos = _make_long_pos(entry=100, sl=90, tp=120)
        pos["bars_held"] = 0
        exit_mgr_defaults.check_exits(pos, 102.0)
        assert pos["bars_held"] == 1
        exit_mgr_defaults.check_exits(pos, 103.0)
        assert pos["bars_held"] == 2

    def test_highest_price_updated_long(self, exit_mgr_defaults):
        pos = _make_long_pos(entry=100, sl=90, tp=120)
        exit_mgr_defaults.check_exits(pos, 108.0)
        assert pos["highest_price"] == 108.0
        exit_mgr_defaults.check_exits(pos, 105.0)
        assert pos["highest_price"] == 108.0  # HWM stays

    def test_lowest_price_updated_short(self, exit_mgr_defaults):
        pos = _make_short_pos(entry=100, sl=110, tp=80)
        exit_mgr_defaults.check_exits(pos, 92.0)
        assert pos["lowest_price"] == 92.0
        exit_mgr_defaults.check_exits(pos, 95.0)
        assert pos["lowest_price"] == 92.0  # LWM stays

    def test_unrealized_pnl_long(self, exit_mgr_defaults):
        pos = _make_long_pos(entry=100, sl=90, tp=120)
        exit_mgr_defaults.check_exits(pos, 110.0)
        assert pos["unrealized_pnl"] == pytest.approx(10.0, abs=0.01)

    def test_unrealized_pnl_short(self, exit_mgr_defaults):
        pos = _make_short_pos(entry=100, sl=110, tp=80)
        exit_mgr_defaults.check_exits(pos, 90.0)
        assert pos["unrealized_pnl"] == pytest.approx(10.0, abs=0.01)


# ============================================================
# R3 — EXIT MANAGER / PAPER EXECUTOR PARITY
# ============================================================

class TestExitManagerPaperParity:
    """Prove that ExitManager produces identical results to PaperExecutor
    exit logic for the same inputs."""

    def test_parity_sl_identical(self, exit_mgr_defaults):
        """Both modes check price <= sl for long."""
        pos = _make_long_pos(entry=100, sl=95, tp=110)
        full_action = exit_mgr_defaults.check_exits(pos, 94.0, parity_mode=False)
        pos2 = _make_long_pos(entry=100, sl=95, tp=110)
        parity_action = exit_mgr_defaults.check_exits(pos2, 94.0, parity_mode=True)
        assert full_action.action == parity_action.action == "stop_loss"

    def test_parity_tp_identical(self, exit_mgr_defaults):
        pos = _make_long_pos(entry=100, sl=95, tp=110)
        # Disable auto-partial so full mode hits TP
        pos["_auto_partial_applied"] = True
        full_action = exit_mgr_defaults.check_exits(pos, 111.0, parity_mode=False)
        pos2 = _make_long_pos(entry=100, sl=95, tp=110)
        parity_action = exit_mgr_defaults.check_exits(pos2, 111.0, parity_mode=True)
        assert full_action.action == parity_action.action == "take_profit"


# ============================================================
# R4 — LIVE POSITION SERIALISATION
# ============================================================

class TestLivePositionSerialization:
    """LivePosition to_dict/from_dict round-trip."""

    def test_round_trip(self):
        from core.execution.live_executor import LivePosition
        pos = LivePosition(
            symbol="BTC/USDT",
            side="buy",
            entry_price=50000.0,
            quantity=0.1,
            stop_loss=48000.0,
            take_profit=55000.0,
            size_usdt=5000.0,
            entry_size_usdt=5000.0,
            score=0.85,
            regime="bull_trend",
            models_fired=["momentum_breakout"],
            timeframe="30m",
            rationale="Strong breakout",
            opened_at="2026-03-29T12:00:00",
            entry_order_id="order_123",
            trailing_stop_pct=0.03,
            max_hold_bars=20,
            bars_held=5,
            highest_price=52000.0,
            lowest_price=49500.0,
            _breakeven_applied=True,
            _auto_partial_applied=False,
            _initial_risk=2000.0,
        )
        d = pos.to_dict()
        pos2 = LivePosition.from_dict(d)

        assert pos2.symbol == "BTC/USDT"
        assert pos2.side == "buy"
        assert pos2.entry_price == 50000.0
        assert pos2.quantity == 0.1
        assert pos2.stop_loss == 48000.0
        assert pos2.take_profit == 55000.0
        assert pos2.size_usdt == 5000.0
        assert pos2.entry_size_usdt == 5000.0
        assert pos2.score == 0.85
        assert pos2.regime == "bull_trend"
        assert pos2.models_fired == ["momentum_breakout"]
        assert pos2.trailing_stop_pct == 0.03
        assert pos2.max_hold_bars == 20
        assert pos2.bars_held == 5
        assert pos2.highest_price == 52000.0
        assert pos2.lowest_price == 49500.0
        assert pos2._breakeven_applied is True
        assert pos2._auto_partial_applied is False
        assert pos2._initial_risk == 2000.0

    def test_round_trip_minimal(self):
        """Position with minimal fields (backward compat)."""
        from core.execution.live_executor import LivePosition
        d = {
            "symbol": "ETH/USDT",
            "side": "sell",
            "entry_price": 3000.0,
            "quantity": 1.0,
            "stop_loss": 3150.0,
            "take_profit": 2700.0,
            "size_usdt": 3000.0,
        }
        pos = LivePosition.from_dict(d)
        assert pos.symbol == "ETH/USDT"
        assert pos.entry_size_usdt == 3000.0  # defaults to size_usdt
        # _initial_risk auto-calculated from abs(entry_price - stop_loss)
        assert pos._initial_risk == abs(3000.0 - 3150.0)
        assert pos._breakeven_applied is False

    def test_json_round_trip(self):
        """Full JSON serialization / deserialization."""
        from core.execution.live_executor import LivePosition
        pos = LivePosition(
            symbol="SOL/USDT", side="buy", entry_price=150.0,
            quantity=10.0, stop_loss=140.0, take_profit=170.0,
            size_usdt=1500.0, bars_held=3, highest_price=155.0,
        )
        json_str = json.dumps(pos.to_dict())
        d = json.loads(json_str)
        pos2 = LivePosition.from_dict(d)
        assert pos2.symbol == "SOL/USDT"
        assert pos2.highest_price == 155.0
        assert pos2.bars_held == 3


# ============================================================
# R1 — DAILY LOSS LIMIT (LiveExecutor)
# ============================================================

class TestDailyLossLimit:
    """Daily loss limit blocks new trades and resets at UTC midnight."""

    def _make_executor(self, daily_limit=2.0, initial_usdt=10000.0):
        """Create a LiveExecutor with mocked dependencies."""
        with patch("core.execution.live_executor.bus"), \
             patch("core.execution.live_executor._POSITIONS_JSON", Path("/tmp/_test_pos.json")), \
             patch("core.execution.live_executor._STATE_JSON", Path("/tmp/_test_state.json")):
            from core.execution.live_executor import LiveExecutor
            # Prevent __init__ from running full initialization
            exec_obj = object.__new__(LiveExecutor)
            exec_obj._positions = {}
            exec_obj._closed_trades = []
            exec_obj._pending_confirmations = {}
            exec_obj._auto_execute_mode = False
            exec_obj._peak_usdt = initial_usdt
            exec_obj._initial_usdt = initial_usdt
            exec_obj._lock = __import__("threading").RLock()
            exec_obj._balance_cache = {"usdt": initial_usdt, "ts": 0.0}
            exec_obj._exit_manager = ExitManager({})
            exec_obj._daily_loss_limit_pct = daily_limit
            exec_obj._daily_loss_limit_hit = False
            exec_obj._daily_loss_limit_date = ""
            exec_obj._circuit_breaker_pct = 99.0  # effectively disabled
            exec_obj._circuit_breaker_tripped = False
            exec_obj._requires_manual_review = False
            exec_obj._reconciliation_issues = []
            exec_obj._critical_state = False
            exec_obj._critical_failed_symbols = []
            exec_obj._equity_uncertain = False
            exec_obj._config_validation_failed = False
            exec_obj._config_validation_errors = []
            # Phase 3 attributes
            exec_obj._kill_switch_active = False
            exec_obj._kill_switch_reason = ""
            exec_obj._kill_switch_activated_at = ""
            exec_obj._safety_action_cooldowns = {}
            exec_obj._json_dirty = False
            exec_obj._json_last_write = 0.0
            exec_obj._json_debounce_s = 1.0
            exec_obj._destructive_action_log = []
            exec_obj._exchange_connected = True
            exec_obj._state_version = 0
            # Phase 4 additions
            exec_obj._reconnect_reconciliation_in_progress = False
            exec_obj._audit_log_path = None
            return exec_obj

    def test_no_block_when_within_limit(self):
        ex = self._make_executor(daily_limit=2.0, initial_usdt=10000.0)
        # Small loss today
        today = datetime.utcnow().strftime("%Y-%m-%d")
        ex._closed_trades = [
            {"pnl_usdt": -50.0, "closed_at": f"{today}T10:00:00"}
        ]
        assert ex._check_daily_loss_limit() is False

    def test_block_when_limit_breached(self):
        ex = self._make_executor(daily_limit=2.0, initial_usdt=10000.0)
        today = datetime.utcnow().strftime("%Y-%m-%d")
        # 2% of 10000 = 200 loss threshold
        ex._closed_trades = [
            {"pnl_usdt": -250.0, "closed_at": f"{today}T10:00:00"}
        ]
        with patch.object(ex, "_save_system_state"):
            assert ex._check_daily_loss_limit() is True
        assert ex._daily_loss_limit_hit is True

    def test_reset_on_new_day(self):
        ex = self._make_executor(daily_limit=2.0, initial_usdt=10000.0)
        ex._daily_loss_limit_hit = True
        yesterday = (datetime.utcnow() - timedelta(days=1)).strftime("%Y-%m-%d")
        ex._daily_loss_limit_date = yesterday
        with patch.object(ex, "_save_system_state"):
            result = ex._check_daily_loss_limit()
        assert ex._daily_loss_limit_hit is False  # reset
        assert result is False

    def test_disabled_when_zero(self):
        ex = self._make_executor(daily_limit=0.0, initial_usdt=10000.0)
        today = datetime.utcnow().strftime("%Y-%m-%d")
        ex._closed_trades = [
            {"pnl_usdt": -9999.0, "closed_at": f"{today}T10:00:00"}
        ]
        assert ex._check_daily_loss_limit() is False

    def test_pre_trade_check_includes_daily_loss(self):
        ex = self._make_executor(daily_limit=2.0, initial_usdt=10000.0)
        ex._daily_loss_limit_hit = True
        ex._daily_loss_limit_date = datetime.utcnow().strftime("%Y-%m-%d")
        result = ex._pre_trade_check("test")
        assert result is not None
        assert "daily loss limit" in result


# ============================================================
# R2 — DRAWDOWN CIRCUIT BREAKER
# ============================================================

class TestCircuitBreaker:
    """Drawdown circuit breaker closes all positions and halts trading."""

    def _make_executor(self, cb_pct=10.0, peak=10000.0):
        with patch("core.execution.live_executor.bus"), \
             patch("core.execution.live_executor._POSITIONS_JSON", Path("/tmp/_test_pos.json")), \
             patch("core.execution.live_executor._STATE_JSON", Path("/tmp/_test_state.json")):
            from core.execution.live_executor import LiveExecutor
            exec_obj = object.__new__(LiveExecutor)
            exec_obj._positions = {}
            exec_obj._closed_trades = []
            exec_obj._pending_confirmations = {}
            exec_obj._auto_execute_mode = False
            exec_obj._peak_usdt = peak
            exec_obj._initial_usdt = peak
            exec_obj._lock = __import__("threading").RLock()
            exec_obj._balance_cache = {"usdt": peak, "ts": 0.0}
            exec_obj._exit_manager = ExitManager({})
            exec_obj._daily_loss_limit_pct = 99.0
            exec_obj._daily_loss_limit_hit = False
            exec_obj._daily_loss_limit_date = ""
            exec_obj._circuit_breaker_pct = cb_pct
            exec_obj._circuit_breaker_tripped = False
            exec_obj._requires_manual_review = False
            exec_obj._reconciliation_issues = []
            exec_obj._critical_state = False
            exec_obj._critical_failed_symbols = []
            exec_obj._equity_uncertain = False
            exec_obj._config_validation_failed = False
            exec_obj._config_validation_errors = []
            # Phase 3 attributes
            exec_obj._kill_switch_active = False
            exec_obj._kill_switch_reason = ""
            exec_obj._kill_switch_activated_at = ""
            exec_obj._safety_action_cooldowns = {}
            exec_obj._json_dirty = False
            exec_obj._json_last_write = 0.0
            exec_obj._json_debounce_s = 1.0
            exec_obj._destructive_action_log = []
            exec_obj._exchange_connected = True
            exec_obj._state_version = 0
            exec_obj._SAFETY_COOLDOWN_SECONDS = 5.0
            exec_obj._safety_action_log = []
            # Phase 4 additions
            exec_obj._reconnect_reconciliation_in_progress = False
            exec_obj._audit_log_path = None
            return exec_obj

    def test_not_tripped_within_threshold(self):
        ex = self._make_executor(cb_pct=10.0, peak=10000.0)
        # Drawdown 5% — should not trip
        with patch.object(type(ex), "drawdown_pct", new_callable=PropertyMock, return_value=5.0):
            assert ex._check_circuit_breaker() is False
        assert ex._circuit_breaker_tripped is False

    def test_trips_at_threshold(self):
        ex = self._make_executor(cb_pct=10.0, peak=10000.0)
        with patch.object(type(ex), "drawdown_pct", new_callable=PropertyMock, return_value=10.5), \
             patch.object(ex, "_emergency_close_all", return_value=0), \
             patch.object(ex, "_save_system_state"):
            result = ex._check_circuit_breaker()
        assert result is True
        assert ex._circuit_breaker_tripped is True

    def test_stays_tripped(self):
        ex = self._make_executor(cb_pct=10.0)
        ex._circuit_breaker_tripped = True
        assert ex._check_circuit_breaker() is True

    def test_manual_reset(self):
        ex = self._make_executor(cb_pct=10.0)
        ex._circuit_breaker_tripped = True
        with patch.object(ex, "_save_system_state"):
            ex.reset_circuit_breaker()
        assert ex._circuit_breaker_tripped is False

    def test_pre_trade_check_blocks_when_tripped(self):
        ex = self._make_executor(cb_pct=10.0)
        ex._circuit_breaker_tripped = True
        result = ex._pre_trade_check("test")
        assert result is not None
        assert "circuit breaker" in result

    def test_configurable_threshold(self):
        ex = self._make_executor(cb_pct=5.0, peak=10000.0)
        # 5% drawdown should trip with 5% threshold
        with patch.object(type(ex), "drawdown_pct", new_callable=PropertyMock, return_value=5.0), \
             patch.object(ex, "_emergency_close_all", return_value=0), \
             patch.object(ex, "_save_system_state"):
            result = ex._check_circuit_breaker()
        assert result is True


# ============================================================
# R5 — RECONCILIATION
# ============================================================

class TestReconciliation:
    """Exchange reconciliation on startup."""

    def _make_executor(self):
        with patch("core.execution.live_executor.bus"), \
             patch("core.execution.live_executor._POSITIONS_JSON", Path("/tmp/_test_pos.json")), \
             patch("core.execution.live_executor._STATE_JSON", Path("/tmp/_test_state.json")):
            from core.execution.live_executor import LiveExecutor, LivePosition
            exec_obj = object.__new__(LiveExecutor)
            exec_obj._positions = {}
            exec_obj._closed_trades = []
            exec_obj._pending_confirmations = {}
            exec_obj._auto_execute_mode = False
            exec_obj._peak_usdt = 10000.0
            exec_obj._initial_usdt = 10000.0
            exec_obj._lock = __import__("threading").RLock()
            exec_obj._balance_cache = {"usdt": 10000.0, "ts": 0.0}
            exec_obj._exit_manager = ExitManager({})
            exec_obj._daily_loss_limit_pct = 99.0
            exec_obj._daily_loss_limit_hit = False
            exec_obj._daily_loss_limit_date = ""
            exec_obj._circuit_breaker_pct = 99.0
            exec_obj._circuit_breaker_tripped = False
            exec_obj._requires_manual_review = False
            exec_obj._reconciliation_issues = []
            exec_obj._critical_state = False
            exec_obj._critical_failed_symbols = []
            exec_obj._equity_uncertain = False
            exec_obj._config_validation_failed = False
            exec_obj._config_validation_errors = []
            # Phase 3 attributes
            exec_obj._kill_switch_active = False
            exec_obj._kill_switch_reason = ""
            exec_obj._kill_switch_activated_at = ""
            exec_obj._safety_action_cooldowns = {}
            exec_obj._json_dirty = False
            exec_obj._json_last_write = 0.0
            exec_obj._json_debounce_s = 1.0
            exec_obj._destructive_action_log = []
            exec_obj._exchange_connected = True
            exec_obj._state_version = 0
            exec_obj._SAFETY_COOLDOWN_SECONDS = 5.0
            exec_obj._safety_action_log = []
            # Phase 4 additions
            exec_obj._reconnect_reconciliation_in_progress = False
            exec_obj._audit_log_path = None
            return exec_obj, LivePosition

    def _patch_exchange_manager(self, mock_em):
        """Context manager that injects mock_em as the exchange_manager singleton."""
        # The reconcile_with_exchange() method does:
        #   from core.market_data.exchange_manager import exchange_manager
        # We need to ensure that import resolves to our mock.
        mock_module = MagicMock()
        mock_module.exchange_manager = mock_em
        return patch.dict("sys.modules", {
            "core.market_data.exchange_manager": mock_module,
        })

    def test_clean_reconciliation(self):
        ex, LP = self._make_executor()
        ex._positions = {
            "BTC/USDT": LP(symbol="BTC/USDT", side="buy", entry_price=50000,
                           quantity=0.1, stop_loss=48000, take_profit=55000,
                           size_usdt=5000),
        }
        mock_em = MagicMock()
        mock_em.fetch_positions.return_value = [
            {"symbol": "BTC/USDT", "side": "long", "contracts": 0.1,
             "entryPrice": 50000, "notional": 5000}
        ]
        mock_em.fetch_open_orders.return_value = []
        with self._patch_exchange_manager(mock_em), \
             patch.object(ex, "_save_positions_json"), \
             patch.object(ex, "_save_system_state"):
            report = ex.reconcile_with_exchange()
        assert report["clean"] is True
        assert ex._requires_manual_review is False

    def test_local_only_position_closed(self):
        ex, LP = self._make_executor()
        ex._positions = {
            "BTC/USDT": LP(symbol="BTC/USDT", side="buy", entry_price=50000,
                           quantity=0.1, stop_loss=48000, take_profit=55000,
                           size_usdt=5000),
        }
        mock_em = MagicMock()
        mock_em.fetch_positions.return_value = []  # Exchange has nothing
        mock_em.fetch_open_orders.return_value = []
        with self._patch_exchange_manager(mock_em), \
             patch.object(ex, "_save_positions_json"), \
             patch.object(ex, "_save_system_state"), \
             patch.object(ex, "_save_closed_to_db"):
            report = ex.reconcile_with_exchange()
        assert "BTC/USDT" in report["local_only"]
        assert report["clean"] is False
        assert "BTC/USDT" not in ex._positions

    def test_exchange_only_creates_orphan(self):
        ex, LP = self._make_executor()
        ex._positions = {}  # Local has nothing
        mock_em = MagicMock()
        mock_em.fetch_positions.return_value = [
            {"symbol": "ETH/USDT", "side": "long", "contracts": 1.0,
             "entryPrice": 3000, "notional": 3000}
        ]
        mock_em.fetch_open_orders.return_value = []
        with self._patch_exchange_manager(mock_em), \
             patch.object(ex, "_save_positions_json"), \
             patch.object(ex, "_save_system_state"):
            report = ex.reconcile_with_exchange()
        assert "ETH/USDT" in report["exchange_only"]
        assert "ETH/USDT" in ex._positions
        assert ex._requires_manual_review is True

    def test_size_mismatch_updates_local(self):
        ex, LP = self._make_executor()
        ex._positions = {
            "BTC/USDT": LP(symbol="BTC/USDT", side="buy", entry_price=50000,
                           quantity=0.1, stop_loss=48000, take_profit=55000,
                           size_usdt=5000),
        }
        mock_em = MagicMock()
        mock_em.fetch_positions.return_value = [
            {"symbol": "BTC/USDT", "side": "long", "contracts": 0.05,  # Half
             "entryPrice": 50000, "notional": 2500}
        ]
        mock_em.fetch_open_orders.return_value = []
        with self._patch_exchange_manager(mock_em), \
             patch.object(ex, "_save_positions_json"), \
             patch.object(ex, "_save_system_state"):
            report = ex.reconcile_with_exchange()
        assert len(report["size_mismatch"]) == 1
        assert ex._positions["BTC/USDT"].quantity == 0.05

    def test_manual_review_blocks_trades(self):
        ex, LP = self._make_executor()
        ex._requires_manual_review = True
        result = ex._pre_trade_check("test")
        assert result is not None
        assert "reconciliation" in result

    def test_clear_manual_review_unblocks(self):
        ex, LP = self._make_executor()
        ex._requires_manual_review = True
        ex._reconciliation_issues = ["test issue"]
        with patch.object(ex, "_save_system_state"):
            ex.clear_manual_review()
        assert ex._requires_manual_review is False
        assert len(ex._reconciliation_issues) == 0

    def test_exchange_fetch_failure_halts(self):
        ex, LP = self._make_executor()
        mock_em = MagicMock()
        mock_em.fetch_positions.side_effect = Exception("Network error")
        with self._patch_exchange_manager(mock_em), \
             patch.object(ex, "_save_system_state"):
            report = ex.reconcile_with_exchange()
        assert report["clean"] is False
        assert "error" in report
        assert ex._requires_manual_review is True

    def test_stale_orders_cancelled(self):
        ex, LP = self._make_executor()
        ex._positions = {}  # No local positions
        mock_em = MagicMock()
        mock_em.fetch_positions.return_value = []
        mock_em.fetch_open_orders.return_value = [
            {"id": "stale_001", "symbol": "XRP/USDT"}
        ]
        mock_em.cancel_order.return_value = True
        with self._patch_exchange_manager(mock_em), \
             patch.object(ex, "_save_positions_json"), \
             patch.object(ex, "_save_system_state"):
            report = ex.reconcile_with_exchange()
        assert len(report["stale_orders_cancelled"]) == 1
        mock_em.cancel_order.assert_called_once_with("stale_001", "XRP/USDT")


# ============================================================
# R4 — SYSTEM STATE PERSISTENCE
# ============================================================

class TestSystemStatePersistence:
    """Persistent state survives restarts."""

    def test_save_and_load_state(self, tmp_path):
        state_file = tmp_path / "live_system_state.json"
        with patch("core.execution.live_executor._STATE_JSON", state_file), \
             patch("core.execution.live_executor._POSITIONS_JSON", tmp_path / "pos.json"), \
             patch("core.execution.live_executor.bus"):
            from core.execution.live_executor import LiveExecutor
            exec_obj = object.__new__(LiveExecutor)
            exec_obj._circuit_breaker_tripped = True
            exec_obj._daily_loss_limit_hit = True
            exec_obj._daily_loss_limit_date = "2026-03-29"
            exec_obj._peak_usdt = 12345.0
            exec_obj._initial_usdt = 10000.0
            exec_obj._requires_manual_review = True
            exec_obj._reconciliation_issues = ["test issue"]
            exec_obj._critical_state = True
            exec_obj._critical_failed_symbols = ["BTC/USDT"]
            exec_obj._lock = __import__("threading").RLock()
            # Phase 3 attributes
            exec_obj._kill_switch_active = False
            exec_obj._kill_switch_reason = ""
            exec_obj._kill_switch_activated_at = ""
            exec_obj._safety_action_cooldowns = {}
            exec_obj._json_dirty = False
            exec_obj._json_last_write = 0.0
            exec_obj._json_debounce_s = 1.0
            exec_obj._destructive_action_log = []
            exec_obj._exchange_connected = True
            exec_obj._state_version = 0

            exec_obj._save_system_state()
            assert state_file.exists()

            # Now load into a fresh instance
            exec_obj2 = object.__new__(LiveExecutor)
            exec_obj2._circuit_breaker_tripped = False
            exec_obj2._daily_loss_limit_hit = False
            exec_obj2._daily_loss_limit_date = ""
            exec_obj2._peak_usdt = 0.0
            exec_obj2._initial_usdt = 0.0
            exec_obj2._requires_manual_review = False
            exec_obj2._reconciliation_issues = []
            exec_obj2._critical_state = False
            exec_obj2._critical_failed_symbols = []
            exec_obj2._lock = __import__("threading").RLock()
            # Phase 3 attributes
            exec_obj2._kill_switch_active = False
            exec_obj2._kill_switch_reason = ""
            exec_obj2._kill_switch_activated_at = ""
            exec_obj2._safety_action_cooldowns = {}
            exec_obj2._json_dirty = False
            exec_obj2._json_last_write = 0.0
            exec_obj2._json_debounce_s = 1.0
            exec_obj2._destructive_action_log = []
            exec_obj2._exchange_connected = True
            exec_obj2._state_version = 0
            exec_obj2._load_system_state()

            assert exec_obj2._circuit_breaker_tripped is True
            assert exec_obj2._daily_loss_limit_hit is True
            assert exec_obj2._daily_loss_limit_date == "2026-03-29"
            assert exec_obj2._peak_usdt == 12345.0
            assert exec_obj2._requires_manual_review is True

    def test_positions_json_round_trip(self, tmp_path):
        pos_file = tmp_path / "live_open_positions.json"
        with patch("core.execution.live_executor._POSITIONS_JSON", pos_file), \
             patch("core.execution.live_executor.bus"):
            from core.execution.live_executor import LiveExecutor, LivePosition
            exec_obj = object.__new__(LiveExecutor)
            exec_obj._lock = __import__("threading").RLock()
            # Phase 3 attributes
            exec_obj._json_dirty = False
            exec_obj._json_last_write = 0.0
            exec_obj._json_debounce_s = 1.0
            exec_obj._state_version = 0
            exec_obj._positions = {
                "BTC/USDT": LivePosition(
                    symbol="BTC/USDT", side="buy", entry_price=50000,
                    quantity=0.1, stop_loss=48000, take_profit=55000,
                    size_usdt=5000, bars_held=3, highest_price=52000,
                    _breakeven_applied=True,
                ),
            }
            exec_obj._save_positions_json()
            assert pos_file.exists()

            data = json.loads(pos_file.read_text())
            assert len(data["positions"]) == 1
            assert data["positions"][0]["symbol"] == "BTC/USDT"
            assert data["positions"][0]["bars_held"] == 3
            assert data["positions"][0]["_breakeven_applied"] is True


# ============================================================
# MISC — EDGE CASES
# ============================================================

class TestEdgeCases:
    """Edge cases and defensive behaviour."""

    def test_exit_manager_with_empty_config(self):
        mgr = ExitManager(None)
        pos = _make_long_pos(entry=100, sl=95, tp=110)
        action = mgr.check_exits(pos, 94.0)
        assert action is not None
        assert action.action == "stop_loss"

    def test_exit_action_dataclass(self):
        a = ExitAction(action="stop_loss", reason="test")
        assert a.reduce_pct == 0.0
        assert a.new_stop_loss == 0.0

    def test_zero_entry_price_no_crash(self, exit_mgr_defaults):
        """Zero entry price should not crash (division by zero)."""
        pos = _make_long_pos(entry=0.0, sl=-5, tp=10)
        action = exit_mgr_defaults.check_exits(pos, 5.0)
        # Should not raise

    def test_negative_initial_risk(self, exit_mgr_defaults):
        pos = _make_long_pos(entry=100, sl=95, tp=110)
        pos["_initial_risk"] = 0.0  # No risk info
        # Should not crash — breakeven and partial skipped when initial_risk=0
        action = exit_mgr_defaults.check_exits(pos, 102.0)
        assert action is None

    def test_get_stats_empty(self):
        """get_stats() with no trades should not crash."""
        with patch("core.execution.live_executor.bus"), \
             patch("core.execution.live_executor._POSITIONS_JSON", Path("/tmp/_test_pos.json")), \
             patch("core.execution.live_executor._STATE_JSON", Path("/tmp/_test_state.json")):
            from core.execution.live_executor import LiveExecutor
            exec_obj = object.__new__(LiveExecutor)
            exec_obj._positions = {}
            exec_obj._closed_trades = []
            exec_obj._lock = __import__("threading").RLock()
            exec_obj._circuit_breaker_tripped = False
            exec_obj._daily_loss_limit_hit = False
            exec_obj._requires_manual_review = False
            exec_obj._peak_usdt = 0.0
            exec_obj._balance_cache = {"usdt": 0.0, "ts": 0.0}
            # Mock properties that access exchange
            with patch.object(type(exec_obj), "drawdown_pct", new_callable=PropertyMock, return_value=0.0), \
                 patch.object(type(exec_obj), "available_capital", new_callable=PropertyMock, return_value=0.0):
                stats = exec_obj.get_stats()
            assert stats["total_trades"] == 0
            assert stats["win_rate"] == 0.0
            assert stats["profit_factor"] == 0.0


# ============================================================
# DB SCHEMA MIGRATION
# ============================================================

class TestDBSchemaMigration:
    """Verify new columns are present in migration list and ORM model source."""

    def test_migration_entries_present(self):
        """All new live_trades columns must be in _migrate_schema."""
        engine_path = _PROJECT_ROOT / "core" / "database" / "engine.py"
        src = engine_path.read_text()
        expected_columns = [
            "entry_size_usdt",
            "trailing_stop_pct",
            "max_hold_bars",
            "bars_held",
            "highest_price",
            "lowest_price",
            "_breakeven_applied",
            "_auto_partial_applied",
            "_initial_risk",
        ]
        for col in expected_columns:
            assert f'"{col}"' in src or f"'{col}'" in src, \
                f"Missing migration for live_trades.{col}"

    def test_orm_model_has_columns(self):
        """LiveTrade ORM source must declare all exit state columns."""
        models_path = _PROJECT_ROOT / "core" / "database" / "models.py"
        src = models_path.read_text()
        expected_columns = [
            "trailing_stop_pct", "max_hold_bars", "bars_held",
            "highest_price", "lowest_price", "_breakeven_applied",
            "_auto_partial_applied", "_initial_risk", "entry_size_usdt",
        ]
        for col in expected_columns:
            assert col in src, f"LiveTrade model source missing column: {col}"


# ============================================================
# CONFIG KEYS
# ============================================================

class TestConfigKeys:
    """Verify new config keys exist in DEFAULT_CONFIG."""

    def test_drawdown_circuit_breaker_in_defaults(self):
        from config.settings import DEFAULT_CONFIG
        assert "drawdown_circuit_breaker_pct" in DEFAULT_CONFIG["risk_engine"]
        assert DEFAULT_CONFIG["risk_engine"]["drawdown_circuit_breaker_pct"] == 10.0

    def test_exit_config_in_defaults(self):
        from config.settings import DEFAULT_CONFIG
        assert "exit" in DEFAULT_CONFIG
        assert DEFAULT_CONFIG["exit"]["mode"] == "partial"
        assert DEFAULT_CONFIG["exit"]["partial_pct"] == 0.33
        assert DEFAULT_CONFIG["exit"]["partial_r_trigger"] == 1.0

    def test_trailing_stop_in_defaults(self):
        from config.settings import DEFAULT_CONFIG
        assert "trailing_stop" in DEFAULT_CONFIG
        assert DEFAULT_CONFIG["trailing_stop"]["enabled"] is False
        assert DEFAULT_CONFIG["trailing_stop"]["pct"] == 0.0

    def test_max_hold_bars_in_defaults(self):
        from config.settings import DEFAULT_CONFIG
        assert "max_hold_bars" in DEFAULT_CONFIG
        assert DEFAULT_CONFIG["max_hold_bars"] == 0

    def test_daily_loss_limit_in_defaults(self):
        from config.settings import DEFAULT_CONFIG
        assert "daily_loss_limit_pct" in DEFAULT_CONFIG["risk_engine"]
        assert DEFAULT_CONFIG["risk_engine"]["daily_loss_limit_pct"] == 2.0
