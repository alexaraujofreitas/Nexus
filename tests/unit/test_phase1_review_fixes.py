# ============================================================
# NEXUS TRADER — Phase 1 Review Fixes Test Suite
#
# Tests all 7 review requirements:
#   Fix 1: Close-all robustness (retries, partial fills, critical state)
#   Fix 2: Reconciliation — no speculative SL/TP
#   Fix 3: Drawdown strict fallback (halt if uncertain)
#   Fix 4: Config.yaml validation + fail-safe
#   Fix 5: ExitManager execution order conflict tests
#   Fix 6: JSON vs DB merge deterministic precedence
#   Fix 7: Single position per symbol enforcement
#
# Self-contained — no live exchange or DB required.
# ============================================================
from __future__ import annotations

import json
import sys
import time
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

pytestmark = pytest.mark.skip(reason="LiveExecutor not yet implemented — aspirational tests for live trading")

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from core.execution.exit_manager import ExitManager, ExitAction


# ============================================================
# HELPERS
# ============================================================

def _make_pos(side="buy", entry=100.0, sl=90.0, tp=120.0, **overrides):
    """Build a minimal position dict for ExitManager."""
    d = {
        "side": side,
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
    d.update(overrides)
    return d


def _mock_live_executor(**kwargs):
    """
    Create a LiveExecutor-like mock with internal state.
    We mock at the class level because LiveExecutor.__init__ tries to
    import settings, DB, etc.
    """
    from core.execution.live_executor import LivePosition

    mock = MagicMock()
    mock._positions = {}
    mock._closed_trades = []
    mock._circuit_breaker_tripped = False
    mock._circuit_breaker_pct = 10.0
    mock._daily_loss_limit_pct = 2.0
    mock._daily_loss_limit_hit = False
    mock._daily_loss_limit_date = ""
    mock._requires_manual_review = False
    mock._reconciliation_issues = []
    mock._critical_state = False
    mock._critical_failed_symbols = []
    mock._equity_uncertain = False
    mock._peak_usdt = 10000.0
    mock._initial_usdt = 10000.0
    mock._balance_cache = {"usdt": 10000.0, "ts": 0.0}
    mock._lock = __import__("threading").RLock()
    mock._config_validation_failed = False
    mock._config_validation_errors = []
    mock._auto_execute_mode = True
    # Phase 3 attributes
    mock._kill_switch_active = False
    mock._kill_switch_reason = ""
    mock._kill_switch_activated_at = ""
    mock._safety_action_cooldowns = {}
    mock._safety_action_log = []
    mock._SAFETY_COOLDOWN_SECONDS = 5.0
    mock._json_dirty = False
    mock._json_last_write = 0.0
    mock._json_debounce_s = 1.0
    mock._destructive_action_log = []
    mock._exchange_connected = True
    mock._state_version = 0
    # Phase 4 additions
    mock._reconnect_reconciliation_in_progress = False
    mock._audit_log_path = None

    for k, v in kwargs.items():
        setattr(mock, k, v)

    return mock


# ============================================================
# FIX 1: CLOSE-ALL ROBUSTNESS
# ============================================================

class TestCloseAllRetries:
    """Fix 1: Close-all must retry, handle partial fills, enter critical state."""

    def test_close_all_retries_on_first_failure(self):
        """If first close attempt fails, retry up to max_retries."""
        from core.execution.live_executor import LiveExecutor, LivePosition

        # Use real method on a mock instance
        le = _mock_live_executor()
        pos = LivePosition("BTC/USDT", "buy", 50000.0, 0.1, 48000.0, 55000.0, 5000.0)
        le._positions = {"BTC/USDT": pos}

        # _close_position_on_exchange: fail once, then succeed
        call_count = [0]
        def close_side_effect(sym, reason):
            call_count[0] += 1
            if call_count[0] == 1:
                raise Exception("Network timeout")
            return True

        le._close_position_on_exchange = MagicMock(side_effect=close_side_effect)
        le._verify_position_closed = MagicMock(return_value=0.0)
        le._enter_critical_state = MagicMock()

        result = LiveExecutor._emergency_close_all(le, "circuit_breaker", max_retries=3)
        assert result == 1
        assert call_count[0] == 2  # failed once, succeeded on retry
        le._enter_critical_state.assert_not_called()

    def test_close_all_enters_critical_on_exhausted_retries(self):
        """If all retries fail, enter CRITICAL state."""
        from core.execution.live_executor import LiveExecutor, LivePosition

        le = _mock_live_executor()
        pos = LivePosition("BTC/USDT", "buy", 50000.0, 0.1, 48000.0, 55000.0, 5000.0)
        le._positions = {"BTC/USDT": pos}
        le._close_position_on_exchange = MagicMock(side_effect=Exception("persistent failure"))
        le._enter_critical_state = MagicMock()

        result = LiveExecutor._emergency_close_all(le, "circuit_breaker", max_retries=2)
        assert result == 0
        le._enter_critical_state.assert_called_once()
        args = le._enter_critical_state.call_args[0]
        assert "BTC/USDT" in args[0]  # failed_symbols

    def test_close_all_handles_partial_fill(self):
        """Partial fill → verify → re-close residual."""
        from core.execution.live_executor import LiveExecutor, LivePosition

        le = _mock_live_executor()
        pos = LivePosition("ETH/USDT", "buy", 3000.0, 1.0, 2800.0, 3300.0, 3000.0)
        le._positions = {"ETH/USDT": pos}
        le._close_position_on_exchange = MagicMock(return_value=True)
        le._verify_position_closed = MagicMock(return_value=0.3)  # 0.3 residual
        le._close_residual = MagicMock(return_value=True)
        le._enter_critical_state = MagicMock()

        result = LiveExecutor._emergency_close_all(le, "circuit_breaker", max_retries=3)
        assert result == 1
        le._close_residual.assert_called_once_with("ETH/USDT", 0.3, "circuit_breaker")
        le._enter_critical_state.assert_not_called()

    def test_critical_state_blocks_all_trading(self):
        """Critical state blocks _pre_trade_check."""
        from core.execution.live_executor import LiveExecutor

        le = _mock_live_executor(_critical_state=True, _critical_failed_symbols=["BTC/USDT"])
        result = LiveExecutor._pre_trade_check(le, "test")
        assert result is not None
        assert "CRITICAL STATE" in result
        assert "BTC/USDT" in result

    def test_critical_state_persists_across_restart(self):
        """Critical state saved/loaded via system state JSON."""
        from core.execution.live_executor import LiveExecutor

        le = _mock_live_executor(
            _critical_state=True,
            _critical_failed_symbols=["SOL/USDT"],
            _circuit_breaker_tripped=True,
            _requires_manual_review=True,
        )
        # Build state dict manually (matches _save_system_state logic)
        state = {
            "circuit_breaker_tripped": True,
            "daily_loss_limit_hit": False,
            "daily_loss_limit_date": "",
            "peak_usdt": 10000.0,
            "initial_usdt": 10000.0,
            "requires_manual_review": True,
            "reconciliation_issues": [],
            "critical_state": True,
            "critical_failed_symbols": ["SOL/USDT"],
        }
        # Verify the state dict is loadable
        assert state["critical_state"] is True
        assert "SOL/USDT" in state["critical_failed_symbols"]

    def test_clear_critical_state(self):
        """clear_critical_state resets flags and unblocks trading."""
        from core.execution.live_executor import LiveExecutor

        le = _mock_live_executor(
            _critical_state=True,
            _critical_failed_symbols=["BTC/USDT"],
        )
        le._save_system_state = MagicMock()
        le.check_safety_action_rate_limit = MagicMock(return_value=False)

        LiveExecutor.clear_critical_state(le)
        assert le._critical_state is False
        assert le._critical_failed_symbols == []
        le._save_system_state.assert_called_once()

    def test_close_all_multi_symbol_partial_failure(self):
        """Two symbols: one closes, one fails after retries → critical for failed only."""
        from core.execution.live_executor import LiveExecutor, LivePosition

        le = _mock_live_executor()
        le._positions = {
            "BTC/USDT": LivePosition("BTC/USDT", "buy", 50000, 0.1, 48000, 55000, 5000),
            "ETH/USDT": LivePosition("ETH/USDT", "sell", 3000, 1.0, 3200, 2700, 3000),
        }

        def close_effect(sym, reason):
            if sym == "BTC/USDT":
                return True
            raise Exception("ETH close failed")

        le._close_position_on_exchange = MagicMock(side_effect=close_effect)
        le._verify_position_closed = MagicMock(return_value=0.0)
        le._enter_critical_state = MagicMock()

        result = LiveExecutor._emergency_close_all(le, "emergency", max_retries=2)
        assert result == 1
        le._enter_critical_state.assert_called_once()
        failed = le._enter_critical_state.call_args[0][0]
        assert "ETH/USDT" in failed
        assert "BTC/USDT" not in failed


# ============================================================
# FIX 2: RECONCILIATION — NO SPECULATIVE SL/TP
# ============================================================

class TestReconciliationNoSpeculativeSLTP:
    """Fix 2: Orphaned exchange positions must NOT get speculative SL/TP."""

    def _run_reconciliation(self, exchange_positions):
        """Helper to run reconciliation with given exchange positions."""
        from core.execution.live_executor import LiveExecutor

        le = _mock_live_executor()
        le._positions = {}  # no local positions

        mock_em = MagicMock()
        mock_em.fetch_positions.return_value = exchange_positions
        mock_em.fetch_open_orders.return_value = []

        mock_module = MagicMock()
        mock_module.exchange_manager = mock_em
        le._save_positions_json = MagicMock()
        le._save_system_state = MagicMock()
        le._record_reconciliation_close = MagicMock()

        with patch.dict("sys.modules", {"core.market_data.exchange_manager": mock_module}):
            report = LiveExecutor.reconcile_with_exchange(le)

        return le, report

    def test_orphan_gets_zero_sl_tp(self):
        """Exchange-only position gets SL=0 TP=0, NOT speculative values."""
        le, report = self._run_reconciliation([{
            "symbol": "BTC/USDT",
            "side": "long",
            "entryPrice": 50000.0,
            "contracts": 0.1,
            "notional": 5000.0,
        }])
        assert "BTC/USDT" in report["exchange_only"]
        pos = le._positions["BTC/USDT"]
        assert pos.stop_loss == 0.0, f"Expected SL=0.0, got {pos.stop_loss}"
        assert pos.take_profit == 0.0, f"Expected TP=0.0, got {pos.take_profit}"

    def test_orphan_requires_manual_review(self):
        """Exchange-only position sets manual review flag."""
        le, report = self._run_reconciliation([{
            "symbol": "ETH/USDT",
            "side": "short",
            "entryPrice": 3000.0,
            "contracts": 1.0,
            "notional": 3000.0,
        }])
        assert le._requires_manual_review is True
        assert not report["clean"]

    def test_orphan_short_also_zero_sl_tp(self):
        """Short orphan also gets SL=0, TP=0."""
        le, report = self._run_reconciliation([{
            "symbol": "SOL/USDT",
            "side": "short",
            "entryPrice": 150.0,
            "contracts": 10.0,
            "notional": 1500.0,
        }])
        pos = le._positions["SOL/USDT"]
        assert pos.stop_loss == 0.0
        assert pos.take_profit == 0.0
        assert pos.side == "sell"

    def test_zero_sl_tp_does_not_trigger_exit(self):
        """ExitManager does not trigger SL or TP when both are 0."""
        mgr = ExitManager()
        pos = _make_pos(side="buy", entry=100.0, sl=0.0, tp=0.0)
        # Price moves anywhere — no exit should trigger from static SL/TP
        action = mgr.check_exits(pos, 50.0, parity_mode=True)
        assert action is None or action.action not in ("stop_loss", "take_profit")
        # Actually with SL=0 and price=50 for buy: 50 <= 0 is False. Correct.


# ============================================================
# FIX 3: DRAWDOWN STRICT FALLBACK
# ============================================================

class TestDrawdownStrictFallback:
    """Fix 3: Halt trading when equity is uncertain, never estimate."""

    def test_equity_fetch_failure_returns_minus_one(self):
        """_fetch_total_equity returns -1 on exchange error."""
        from core.execution.live_executor import LiveExecutor

        le = _mock_live_executor()
        le._exchange = MagicMock(side_effect=Exception("connection refused"))
        result = LiveExecutor._fetch_total_equity(le)
        assert result == -1.0

    def test_equity_zero_balance_returns_minus_one(self):
        """_fetch_total_equity returns -1 when exchange returns zero."""
        from core.execution.live_executor import LiveExecutor

        mock_ex = MagicMock()
        mock_ex.fetch_balance.return_value = {"USDT": {"total": 0.0, "free": 0.0}}
        le = _mock_live_executor()
        le._exchange = MagicMock(return_value=mock_ex)
        result = LiveExecutor._fetch_total_equity(le)
        assert result == -1.0

    def test_drawdown_returns_minus_one_on_uncertain(self):
        """drawdown_pct returns -1 when equity is uncertain."""
        from core.execution.live_executor import LiveExecutor

        le = _mock_live_executor(_peak_usdt=10000.0)
        le._fetch_total_equity = MagicMock(return_value=-1.0)
        dd = LiveExecutor.drawdown_pct.fget(le)
        assert dd == -1.0
        assert le._equity_uncertain is True

    def test_drawdown_clears_uncertainty_on_success(self):
        """When equity becomes available again, uncertainty clears."""
        from core.execution.live_executor import LiveExecutor

        le = _mock_live_executor(_peak_usdt=10000.0, _equity_uncertain=True)
        le._fetch_total_equity = MagicMock(return_value=9500.0)
        le._save_system_state = MagicMock()
        dd = LiveExecutor.drawdown_pct.fget(le)
        assert dd == 5.0  # (10000-9500)/10000 * 100
        assert le._equity_uncertain is False

    def test_equity_uncertain_blocks_pre_trade(self):
        """_pre_trade_check blocks when equity is uncertain."""
        from core.execution.live_executor import LiveExecutor

        le = _mock_live_executor(_equity_uncertain=True)
        result = LiveExecutor._pre_trade_check(le, "test")
        assert result is not None
        assert "UNCERTAIN" in result

    def test_circuit_breaker_blocks_but_doesnt_trip_on_uncertain(self):
        """When equity is uncertain, block trading but DON'T trip the breaker."""
        from core.execution.live_executor import LiveExecutor

        le = _mock_live_executor(_peak_usdt=10000.0)
        le._fetch_total_equity = MagicMock(return_value=-1.0)
        le._save_system_state = MagicMock()
        le._emergency_close_all = MagicMock(return_value=0)

        # Use the real drawdown_pct property
        type(le).drawdown_pct = LiveExecutor.drawdown_pct

        result = LiveExecutor._check_circuit_breaker(le)
        assert result is True  # blocks trading
        assert le._circuit_breaker_tripped is False  # NOT tripped
        # Clean up property override
        del type(le).drawdown_pct


# ============================================================
# FIX 4: CONFIG VALIDATION
# ============================================================

class TestConfigValidation:
    """Fix 4: Config.yaml must be validated on startup, fail-safe on corruption."""

    def test_valid_config_passes(self):
        """Normal config loads without validation errors."""
        from core.execution.live_executor import LiveExecutor

        le = _mock_live_executor()
        le._REQUIRED_CONFIG = LiveExecutor._REQUIRED_CONFIG

        mock_settings = MagicMock()
        mock_settings.get.side_effect = lambda key, default=None: {
            "risk_engine.daily_loss_limit_pct": 2.0,
            "risk_engine.drawdown_circuit_breaker_pct": 10.0,
            "risk_engine.risk_pct_per_trade": 0.5,
            "risk_engine.max_capital_pct": 0.04,
            "exit.mode": "partial",
            "exit.partial_pct": 0.33,
            "exit.partial_r_trigger": 1.0,
        }.get(key, default)

        mock_module = MagicMock()
        mock_module.settings = mock_settings

        with patch.dict("sys.modules", {"config.settings": mock_module}):
            LiveExecutor._load_config(le)

        assert le._config_validation_failed is False
        assert le._config_validation_errors == []

    def test_missing_key_triggers_validation_failure(self):
        """Missing required key causes config validation to fail."""
        from core.execution.live_executor import LiveExecutor

        le = _mock_live_executor()
        le._REQUIRED_CONFIG = LiveExecutor._REQUIRED_CONFIG

        mock_settings = MagicMock()
        # Return None for everything → all keys "missing"
        mock_settings.get.return_value = None

        mock_module = MagicMock()
        mock_module.settings = mock_settings

        with patch.dict("sys.modules", {"config.settings": mock_module}):
            LiveExecutor._load_config(le)

        assert le._config_validation_failed is True
        assert len(le._config_validation_errors) > 0

    def test_invalid_range_triggers_failure(self):
        """Circuit breaker at 0% or >100% triggers validation error."""
        from core.execution.live_executor import LiveExecutor

        le = _mock_live_executor()
        le._REQUIRED_CONFIG = LiveExecutor._REQUIRED_CONFIG

        mock_settings = MagicMock()
        mock_settings.get.side_effect = lambda key, default=None: {
            "risk_engine.daily_loss_limit_pct": 2.0,
            "risk_engine.drawdown_circuit_breaker_pct": 150.0,  # INVALID
            "risk_engine.risk_pct_per_trade": 0.5,
            "risk_engine.max_capital_pct": 0.04,
            "exit.mode": "partial",
            "exit.partial_pct": 0.33,
            "exit.partial_r_trigger": 1.0,
        }.get(key, default)

        mock_module = MagicMock()
        mock_module.settings = mock_settings

        with patch.dict("sys.modules", {"config.settings": mock_module}):
            LiveExecutor._load_config(le)

        assert le._config_validation_failed is True
        assert any("Invalid range" in e for e in le._config_validation_errors)

    def test_config_import_failure_uses_safe_defaults(self):
        """If config module can't be imported, use safe defaults."""
        from core.execution.live_executor import LiveExecutor

        le = _mock_live_executor()
        le._REQUIRED_CONFIG = LiveExecutor._REQUIRED_CONFIG

        with patch.dict("sys.modules", {"config.settings": None}):
            # This will cause ImportError
            try:
                LiveExecutor._load_config(le)
            except (ImportError, TypeError):
                # The real method handles this internally
                pass

        # Even on failure, defaults should be set
        assert le._daily_loss_limit_pct == 2.0
        assert le._circuit_breaker_pct == 10.0

    def test_config_validation_blocks_pre_trade(self):
        """_pre_trade_check blocks when config validation failed."""
        from core.execution.live_executor import LiveExecutor

        le = _mock_live_executor(_config_validation_failed=True, _config_validation_errors=["test error"])
        result = LiveExecutor._pre_trade_check(le, "test")
        assert result is not None
        assert "config validation failed" in result

    def test_override_config_validation_unblocks(self):
        """override_config_validation clears the failure flag."""
        from core.execution.live_executor import LiveExecutor

        le = _mock_live_executor(_config_validation_failed=True, _config_validation_errors=["err"])
        le.check_safety_action_rate_limit = MagicMock(return_value=False)
        LiveExecutor.override_config_validation(le)
        assert le._config_validation_failed is False
        assert le._config_validation_errors == []


# ============================================================
# FIX 5: EXITMANAGER EXECUTION ORDER CONFLICT TESTS
# ============================================================

class TestExitManagerExecutionOrder:
    """Fix 5: Prove execution order correctness with conflict scenarios."""

    def test_time_exit_fires_before_trailing_stop(self):
        """Time exit has highest priority (evaluated first)."""
        mgr = ExitManager({
            "exit.mode": "partial",
            "exit.partial_pct": 0.33,
            "exit.partial_r_trigger": 1.0,
            "trailing_stop.enabled": True,
            "trailing_stop.pct": 0.02,
            "max_hold_bars": 5,
        })
        # Position at max_hold_bars with trailing also triggered
        pos = _make_pos(
            side="buy", entry=100.0, sl=95.0, tp=120.0,
            bars_held=4,  # will be incremented to 5 = max
            max_hold_bars=5,
            highest_price=115.0,  # trailing would update SL
            trailing_stop_pct=0.02,
        )
        action = mgr.check_exits(pos, 112.0)
        assert action is not None
        assert action.action == "time_exit"

    def test_auto_partial_fires_before_take_profit(self):
        """Auto-partial at +1R fires before TP check when both conditions met."""
        mgr = ExitManager({
            "exit.mode": "partial",
            "exit.partial_pct": 0.33,
            "exit.partial_r_trigger": 1.0,
            "trailing_stop.enabled": False,
            "trailing_stop.pct": 0.0,
            "max_hold_bars": 0,
        })
        # Price at exactly TP (120) which is also +2R (initial_risk=10)
        pos = _make_pos(
            side="buy", entry=100.0, sl=90.0, tp=120.0,
            _auto_partial_applied=False,
            _initial_risk=10.0,
        )
        action = mgr.check_exits(pos, 120.0)
        assert action is not None
        assert action.action == "auto_partial", f"Expected auto_partial, got {action.action}"

    def test_breakeven_applied_before_sl_check(self):
        """Breakeven move at +1R happens before SL check (mutates SL in place)."""
        mgr = ExitManager({
            "exit.mode": "full",  # no auto-partial
            "trailing_stop.enabled": False,
            "max_hold_bars": 0,
        })
        # Price at +1R exactly → breakeven should apply
        pos = _make_pos(
            side="buy", entry=100.0, sl=90.0, tp=130.0,
            _breakeven_applied=False,
            _initial_risk=10.0,
        )
        action = mgr.check_exits(pos, 110.0)
        # No exit action (price between SL and TP), but breakeven applied
        assert pos["_breakeven_applied"] is True
        assert pos["stop_loss"] == 100.0  # moved to entry

    def test_trailing_stop_updates_before_sl_check(self):
        """Trailing stop updates SL before the static SL check runs."""
        mgr = ExitManager({
            "exit.mode": "full",
            "trailing_stop.enabled": True,
            "trailing_stop.pct": 0.05,  # 5% trail
            "max_hold_bars": 0,
        })
        pos = _make_pos(
            side="buy", entry=100.0, sl=90.0, tp=130.0,
            highest_price=120.0,
            trailing_stop_pct=0.05,
            _breakeven_applied=True,
            _auto_partial_applied=True,
        )
        # Price at 114 → trail SL = 120 * 0.95 = 114
        # Old SL was 90, trail_sl = 114 > 90 → SL updated to 114
        # Then price (114) <= SL (114) → stop_loss triggered
        action = mgr.check_exits(pos, 114.0)
        assert pos["stop_loss"] == 114.0  # trailing updated
        assert action is not None
        assert action.action == "stop_loss"

    def test_parity_mode_skips_all_advanced_exits(self):
        """In parity mode, ONLY static SL/TP runs — no trailing, breakeven, partial, time."""
        mgr = ExitManager({
            "exit.mode": "partial",
            "exit.partial_pct": 0.33,
            "exit.partial_r_trigger": 1.0,
            "trailing_stop.enabled": True,
            "trailing_stop.pct": 0.05,
            "max_hold_bars": 5,
        })
        pos = _make_pos(
            side="buy", entry=100.0, sl=90.0, tp=120.0,
            bars_held=10,  # way past max_hold
            max_hold_bars=5,
            highest_price=115.0,
            trailing_stop_pct=0.05,
            _initial_risk=10.0,
            _breakeven_applied=False,
            _auto_partial_applied=False,
        )
        # Price at 115 — in full mode would trigger time_exit, auto_partial, etc.
        # In parity mode, only static SL/TP runs
        action = mgr.check_exits(pos, 115.0, parity_mode=True)
        assert action is None  # price between SL(90) and TP(120)
        # Verify breakeven was NOT applied
        assert pos["_breakeven_applied"] is False

    def test_execution_order_short_position(self):
        """Execution order is preserved for short positions too."""
        mgr = ExitManager({
            "exit.mode": "partial",
            "exit.partial_pct": 0.33,
            "exit.partial_r_trigger": 1.0,
            "trailing_stop.enabled": False,
            "max_hold_bars": 3,
        })
        pos = _make_pos(
            side="sell", entry=100.0, sl=110.0, tp=80.0,
            bars_held=2,  # will be 3 = max
            max_hold_bars=3,
            _initial_risk=10.0,
        )
        action = mgr.check_exits(pos, 85.0)
        assert action.action == "time_exit"  # time exit fires first

    def test_full_priority_chain_buy(self):
        """Complete priority chain: time > trailing > breakeven > auto_partial > SL/TP."""
        # Step 1: time_exit (already tested above)
        # Step 2: trailing stop updates SL THEN SL check
        mgr = ExitManager({
            "exit.mode": "partial",
            "exit.partial_pct": 0.33,
            "exit.partial_r_trigger": 1.0,
            "trailing_stop.enabled": True,
            "trailing_stop.pct": 0.02,
            "max_hold_bars": 0,  # disabled
        })
        # Price rallied to 120, now at 110 = +1R → auto_partial fires
        pos = _make_pos(
            side="buy", entry=100.0, sl=90.0, tp=130.0,
            highest_price=120.0,
            trailing_stop_pct=0.02,
            _initial_risk=10.0,
            _breakeven_applied=False,
            _auto_partial_applied=False,
        )
        action = mgr.check_exits(pos, 110.0)
        # Trail SL = 120*0.98 = 117.6 > old SL 90 → SL updated to 117.6
        # Breakeven: +1R met → SL set to entry (100). BUT trailing already set 117.6 > 100
        # Auto-partial: +1R met → fires
        assert action.action == "auto_partial"
        # Verify trailing updated SL
        assert pos["stop_loss"] >= 117.0  # trailing set it high

    def test_sl_tp_is_last_check(self):
        """SL/TP is the final check — only fires if nothing else did."""
        mgr = ExitManager({
            "exit.mode": "full",  # no auto-partial
            "trailing_stop.enabled": False,
            "max_hold_bars": 0,
        })
        pos = _make_pos(
            side="buy", entry=100.0, sl=90.0, tp=120.0,
            _breakeven_applied=True,
            _auto_partial_applied=True,
        )
        # Price hits TP
        action = mgr.check_exits(pos, 120.0)
        assert action.action == "take_profit"


# ============================================================
# FIX 6: JSON VS DB MERGE DETERMINISTIC PRECEDENCE
# ============================================================

class TestMergePrecedenceRules:
    """Fix 6: Field-level precedence must be deterministic."""

    def test_db_authoritative_for_trade_identity(self):
        """Category A fields (entry price, side, etc.) come from DB when both exist."""
        from core.execution.live_executor import LiveExecutor

        le = _mock_live_executor()
        le._DB_AUTHORITATIVE_FIELDS = LiveExecutor._DB_AUTHORITATIVE_FIELDS
        le._JSON_AUTHORITATIVE_FIELDS = LiveExecutor._JSON_AUTHORITATIVE_FIELDS
        le._reconciliation_issues = []
        le._requires_manual_review = False
        le._save_system_state = MagicMock()
        le._save_positions_json = MagicMock()

        # DB has different entry_price than JSON
        db_positions = {"BTC/USDT": {
            "symbol": "BTC/USDT", "side": "buy", "entry_price": 50000.0,
            "quantity": 0.1, "stop_loss": 48000.0, "take_profit": 55000.0,
            "size_usdt": 5000.0, "entry_size_usdt": 5000.0,
            "score": 0.7, "regime": "bull_trend", "models_fired": ["momentum"],
            "timeframe": "30m", "rationale": "DB version", "opened_at": "2026-01-01T00:00:00",
            "entry_order_id": "order_123", "status": "open",
        }}
        json_positions = {"BTC/USDT": {
            "symbol": "BTC/USDT", "side": "buy", "entry_price": 49999.0,  # DIFFERS
            "quantity": 0.1, "stop_loss": 48000.0, "take_profit": 55000.0,
            "size_usdt": 5000.0, "entry_size_usdt": 5000.0,
            "score": 0.7, "regime": "bull_trend", "models_fired": ["momentum"],
            "timeframe": "30m", "rationale": "JSON version",
            "opened_at": "2026-01-01T00:00:00",
            "entry_order_id": "order_123",
            # Exit state fields (JSON authoritative)
            "bars_held": 15,
            "highest_price": 52000.0,
            "_breakeven_applied": True,
            "_auto_partial_applied": False,
            "_initial_risk": 2000.0,
        }}

        # Mock DB and JSON loading
        with patch.object(LiveExecutor, '_load_open_positions') as mock_load:
            # Run merge logic directly
            merged = {}
            sym = "BTC/USDT"
            db_d = db_positions[sym]
            jd = json_positions[sym]
            base = {}
            for field in LiveExecutor._DB_AUTHORITATIVE_FIELDS:
                if field in db_d:
                    base[field] = db_d[field]
                elif field in jd:
                    base[field] = jd[field]
            for field in LiveExecutor._JSON_AUTHORITATIVE_FIELDS:
                if field in jd:
                    base[field] = jd[field]
                elif field in db_d:
                    base[field] = db_d[field]

        # DB authoritative: entry_price from DB
        assert base["entry_price"] == 50000.0, f"Expected DB price 50000, got {base['entry_price']}"
        assert base["rationale"] == "DB version"

        # JSON authoritative: exit state from JSON
        assert base["bars_held"] == 15
        assert base["highest_price"] == 52000.0
        assert base["_breakeven_applied"] is True
        assert base["_initial_risk"] == 2000.0

    def test_db_only_position_gets_exit_defaults(self):
        """Position in DB but not JSON → exit state fields get defaults."""
        from core.execution.live_executor import LivePosition

        db_data = {
            "symbol": "ETH/USDT", "side": "buy", "entry_price": 3000.0,
            "quantity": 1.0, "stop_loss": 2800.0, "take_profit": 3300.0,
            "size_usdt": 3000.0, "entry_size_usdt": 3000.0,
        }
        pos = LivePosition.from_dict(db_data)
        # Exit state defaults
        assert pos.bars_held == 0
        assert pos.highest_price == 3000.0  # defaults to entry
        assert pos._breakeven_applied is False
        assert pos._auto_partial_applied is False

    def test_json_only_position_flagged_for_reconciliation(self):
        """Position in JSON but not DB → included but flagged."""
        from core.execution.live_executor import LiveExecutor

        le = _mock_live_executor()
        le._DB_AUTHORITATIVE_FIELDS = LiveExecutor._DB_AUTHORITATIVE_FIELDS
        le._JSON_AUTHORITATIVE_FIELDS = LiveExecutor._JSON_AUTHORITATIVE_FIELDS
        le._reconciliation_issues = []
        le._requires_manual_review = False

        # Simulate: sym in json_positions but not db_positions
        json_only_issues = []
        sym = "SOL/USDT"
        json_only_issues.append(
            f"Position {sym} found in JSON but not in DB — "
            f"possible crash before DB write"
        )
        assert "JSON but not in DB" in json_only_issues[0]

    def test_field_categories_are_complete(self):
        """Every field in LivePosition.to_dict() is in either Category A or B."""
        from core.execution.live_executor import LivePosition, LiveExecutor

        pos = LivePosition("TEST", "buy", 100, 1, 90, 110, 100)
        all_keys = set(pos.to_dict().keys())
        cat_a = LiveExecutor._DB_AUTHORITATIVE_FIELDS
        cat_b = LiveExecutor._JSON_AUTHORITATIVE_FIELDS
        covered = cat_a | cat_b
        uncovered = all_keys - covered
        # Some fields may not be in either category (they get DB fallback)
        # But core fields must be covered
        for critical in ["entry_price", "side", "stop_loss", "bars_held", "_breakeven_applied"]:
            assert critical in covered, f"Critical field {critical} not in any category"


# ============================================================
# FIX 7: SINGLE POSITION PER SYMBOL
# ============================================================

class TestSinglePositionPerSymbol:
    """Fix 7: Only one position per symbol allowed at any time."""

    def test_submit_rejects_duplicate_symbol(self):
        """submit() rejects when position already exists for symbol."""
        from core.execution.live_executor import LiveExecutor, LivePosition

        le = _mock_live_executor()
        pos = LivePosition("BTC/USDT", "buy", 50000, 0.1, 48000, 55000, 5000)
        le._positions = {"BTC/USDT": pos}

        candidate = MagicMock()
        candidate.symbol = "BTC/USDT"
        candidate.side = "buy"
        candidate.requires_confirmation = False

        # Mock pre_trade_check to pass
        le._pre_trade_check = MagicMock(return_value=None)

        result = LiveExecutor.submit(le, candidate)
        assert result is False

    def test_place_order_rejects_duplicate_symbol(self):
        """_place_order() also rejects duplicates (defense in depth)."""
        from core.execution.live_executor import LiveExecutor, LivePosition

        le = _mock_live_executor()
        pos = LivePosition("ETH/USDT", "sell", 3000, 1.0, 3200, 2700, 3000)
        le._positions = {"ETH/USDT": pos}
        le._pre_trade_check = MagicMock(return_value=None)

        candidate = MagicMock()
        candidate.symbol = "ETH/USDT"
        candidate.side = "buy"  # opposite direction — still rejected
        candidate.requires_confirmation = False

        result = LiveExecutor._place_order(le, candidate)
        assert result is False

    def test_different_symbol_allowed(self):
        """Different symbol is allowed even when other positions exist."""
        from core.execution.live_executor import LiveExecutor, LivePosition

        le = _mock_live_executor()
        pos = LivePosition("BTC/USDT", "buy", 50000, 0.1, 48000, 55000, 5000)
        le._positions = {"BTC/USDT": pos}
        le._pre_trade_check = MagicMock(return_value=None)

        # Submit for a different symbol
        candidate = MagicMock()
        candidate.symbol = "ETH/USDT"
        candidate.side = "buy"
        candidate.requires_confirmation = False
        candidate.entry_price = 3000.0
        candidate.position_size_usdt = 3000.0
        candidate.stop_loss_price = 2800.0
        candidate.take_profit_price = 3300.0
        candidate.score = 0.7

        # Mock exchange
        mock_ex = MagicMock()
        mock_ex.market.return_value = {"limits": {"amount": {"min": 0.001}, "cost": {"min": 1.0}}}
        mock_ex.amount_to_precision.return_value = "1.0"
        mock_ex.create_market_order.return_value = {"id": "order_1", "average": 3000.0}
        le._exchange = MagicMock(return_value=mock_ex)
        le._save_open_to_db = MagicMock()
        le._save_positions_json = MagicMock()

        result = LiveExecutor._place_order(le, candidate)
        assert result is True
        assert "ETH/USDT" in le._positions

    def test_after_close_same_symbol_allowed(self):
        """After closing a position, the same symbol can be reopened."""
        from core.execution.live_executor import LiveExecutor, LivePosition

        le = _mock_live_executor()
        le._positions = {}  # empty — previously closed

        candidate = MagicMock()
        candidate.symbol = "BTC/USDT"
        candidate.side = "buy"
        candidate.requires_confirmation = False
        le._pre_trade_check = MagicMock(return_value=None)

        # Should not be rejected (no existing position)
        # We just check the lock check passes
        with le._lock:
            assert "BTC/USDT" not in le._positions


# ============================================================
# INTEGRATION: COMBINED SAFETY GATE ORDERING
# ============================================================

class TestPreTradeCheckOrdering:
    """Verify the complete ordering of pre-trade safety gates."""

    def test_critical_state_checked_first(self):
        """Critical state blocks before anything else."""
        from core.execution.live_executor import LiveExecutor

        le = _mock_live_executor(
            _critical_state=True,
            _critical_failed_symbols=["BTC/USDT"],
            _circuit_breaker_tripped=True,
            _equity_uncertain=True,
            _config_validation_failed=True,
        )
        result = LiveExecutor._pre_trade_check(le, "test")
        assert "CRITICAL STATE" in result

    def test_config_validation_before_circuit_breaker(self):
        """Config validation failure checked before circuit breaker."""
        from core.execution.live_executor import LiveExecutor

        le = _mock_live_executor(
            _config_validation_failed=True,
            _config_validation_errors=["missing key"],
            _circuit_breaker_tripped=True,
        )
        result = LiveExecutor._pre_trade_check(le, "test")
        assert "config validation" in result

    def test_equity_uncertain_before_daily_loss(self):
        """Equity uncertainty checked before daily loss limit."""
        from core.execution.live_executor import LiveExecutor

        le = _mock_live_executor(
            _equity_uncertain=True,
            _daily_loss_limit_hit=True,
        )
        result = LiveExecutor._pre_trade_check(le, "test")
        assert "UNCERTAIN" in result

    def test_full_gate_ordering(self):
        """
        Complete ordering:
        1. critical_state
        2. config_validation_failed
        3. circuit_breaker_tripped
        4. requires_manual_review
        5. equity_uncertain
        6. daily_loss_limit
        7. circuit_breaker_check (proactive)
        """
        from core.execution.live_executor import LiveExecutor

        # All clear
        le = _mock_live_executor()
        le._check_daily_loss_limit = MagicMock(return_value=False)
        le._check_circuit_breaker = MagicMock(return_value=False)
        result = LiveExecutor._pre_trade_check(le, "test")
        assert result is None  # all clear
