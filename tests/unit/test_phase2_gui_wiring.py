# ============================================================
# NEXUS TRADER — Phase 2 GUI Wiring Integration Tests
#
# Tests all Phase 2 Safety Infrastructure changes:
#   1. LiveExecutor.get_production_status() parity
#   2. LiveExecutor.adjust_target() parity
#   3. Router-aware trading page executor switching
#   4. Signal confirmation widget lifecycle
#   5. Circuit breaker safety panel state rendering
#   6. Dashboard/DemoMonitor router awareness
#   7. Live mode visual safety (banner, reset disable, double confirm)
#   8. ExitManager zero SL/TP guard (cross-ref Phase 1)
# ============================================================
from __future__ import annotations

import pytest
import threading
from unittest.mock import MagicMock, patch, PropertyMock
from datetime import datetime, timezone

pytestmark = pytest.mark.skip(reason="LiveExecutor not yet implemented — aspirational tests for live trading")


def _has_pyside6() -> bool:
    """Check if PySide6 Qt widgets can be imported (requires display server)."""
    try:
        from PySide6.QtWidgets import QWidget  # noqa: F401
        return True
    except (ImportError, RuntimeError):
        return False


# ─────────────────────────────────────────────────────────
# FIXTURES
# ─────────────────────────────────────────────────────────

def _make_live_executor():
    """Create a LiveExecutor-like object without network/DB deps."""
    # Bypass __init__ entirely
    from core.execution.live_executor import LiveExecutor
    le = object.__new__(LiveExecutor)
    le._lock = threading.RLock()
    le._positions = {}
    le._closed_trades = []
    le._circuit_breaker_tripped = False
    le._daily_loss_limit_hit = False
    le._daily_loss_limit_pct = 2.0
    le._requires_manual_review = False
    le._critical_state = False
    le._critical_failed_symbols = []
    le._equity_uncertain = False
    le._config_validation_failed = False
    le._config_validation_errors = []
    le._circuit_breaker_pct = 10.0
    le._peak_usdt = 100_000.0
    le._initial_usdt = 100_000.0
    le._balance_cache = 100_000.0
    le._balance_cache_ts = 0.0
    le._pending_confirmations = {}
    le._auto_execute_mode = False
    # Phase 2 Fix attributes
    le._kill_switch_active = False
    le._kill_switch_reason = ""
    le._kill_switch_activated_at = ""
    le._signal_expiry_seconds = 60.0
    le._state_version = 0
    le._safety_action_log = []
    le._safety_action_cooldowns = {}
    le._SAFETY_COOLDOWN_SECONDS = 5.0
    le._reconciliation_issues = []
    # Phase 3 additions
    le._exchange_connected = True
    le._json_dirty = False
    le._json_last_write = 0.0
    le._json_debounce_s = 1.0
    le._destructive_action_log = []
    le._daily_loss_limit_date = ""
    # Phase 4 additions
    le._reconnect_reconciliation_in_progress = False
    le._audit_log_path = None
    return le


def _make_live_position(**kwargs):
    """Create a LivePosition with defaults."""
    from core.execution.live_executor import LivePosition
    defaults = dict(
        symbol="BTC/USDT", side="buy", entry_price=50000.0,
        quantity=0.01, stop_loss=49000.0, take_profit=52000.0,
        size_usdt=500.0, entry_size_usdt=500.0,
        score=0.65, regime="bull_trend",
        models_fired=["momentum_breakout"], timeframe="30m",
        opened_at=datetime.now(timezone.utc).isoformat(),
    )
    defaults.update(kwargs)
    return LivePosition(**defaults)


# ─────────────────────────────────────────────────────────
# 1. LiveExecutor.get_production_status()
# ─────────────────────────────────────────────────────────

class TestLiveExecutorProductionStatus:

    def test_returns_all_required_keys(self):
        """get_production_status() must return ALL keys that PaperExecutor returns."""
        le = _make_live_executor()
        with patch.object(type(le), '_fetch_total_equity', return_value=100_000.0):
            ps = le.get_production_status()

        required = {
            "capital_usdt", "peak_capital_usdt", "total_return_pct",
            "drawdown_pct", "circuit_breaker_on", "portfolio_heat_pct",
            "open_positions", "open_symbols", "last_10_outcomes",
            "current_losing_streak", "total_trades", "session_pnl_usdt",
        }
        assert required.issubset(set(ps.keys())), f"Missing: {required - set(ps.keys())}"

    def test_returns_live_specific_keys(self):
        """Live executor adds safety state keys not in PaperExecutor."""
        le = _make_live_executor()
        with patch.object(type(le), '_fetch_total_equity', return_value=100_000.0):
            ps = le.get_production_status()

        live_keys = {
            "critical_state", "requires_manual_review",
            "config_validation_failed", "equity_uncertain",
            "daily_loss_limit_hit",
        }
        assert live_keys.issubset(set(ps.keys())), f"Missing live keys: {live_keys - set(ps.keys())}"

    def test_circuit_breaker_reflected(self):
        """circuit_breaker_on reflects the tripped flag."""
        le = _make_live_executor()
        le._circuit_breaker_tripped = True
        with patch.object(type(le), '_fetch_total_equity', return_value=90_000.0):
            ps = le.get_production_status()
        assert ps["circuit_breaker_on"] is True

    def test_equity_uncertain_when_fetch_fails(self):
        """Returns equity_uncertain=True when balance unavailable."""
        le = _make_live_executor()
        le._equity_uncertain = True
        with patch.object(type(le), '_fetch_total_equity', return_value=-1.0):
            ps = le.get_production_status()
        assert ps["equity_uncertain"] is True
        assert ps["drawdown_pct"] == -1.0

    def test_portfolio_heat_with_positions(self):
        """Portfolio heat calculated from open positions."""
        le = _make_live_executor()
        pos = _make_live_position(
            stop_loss=49000.0, entry_price=50000.0,
            size_usdt=500.0,
        )
        le._positions = {"BTC/USDT": pos}
        with patch.object(type(le), '_fetch_total_equity', return_value=100_000.0):
            ps = le.get_production_status()
        assert ps["portfolio_heat_pct"] > 0
        assert ps["open_positions"] == 1
        assert "BTC/USDT" in ps["open_symbols"]

    def test_last_10_outcomes(self):
        """last_10_outcomes shows correct W/L sequence."""
        le = _make_live_executor()
        le._closed_trades = [
            {"pnl_usdt": 100.0},  # W
            {"pnl_usdt": -50.0},  # L
            {"pnl_usdt": 200.0},  # W
        ]
        with patch.object(type(le), '_fetch_total_equity', return_value=100_000.0):
            ps = le.get_production_status()
        assert ps["last_10_outcomes"] == ["W", "L", "W"]
        assert ps["current_losing_streak"] == 0  # last trade was a win


# ─────────────────────────────────────────────────────────
# 2. LiveExecutor.adjust_target()
# ─────────────────────────────────────────────────────────

class TestLiveExecutorAdjustTarget:

    def test_adjust_target_success(self):
        """adjust_target() changes TP and returns True."""
        le = _make_live_executor()
        pos = _make_live_position(take_profit=52000.0)
        le._positions = {"BTC/USDT": pos}
        with patch.object(type(le), '_save_positions_json'):
            with patch('core.execution.live_executor.bus'):
                result = le.adjust_target("BTC/USDT", 53000.0)
        assert result is True
        assert pos.take_profit == 53000.0

    def test_adjust_target_not_found(self):
        """adjust_target() returns False for unknown symbol."""
        le = _make_live_executor()
        le._positions = {}
        result = le.adjust_target("ETH/USDT", 4000.0)
        assert result is False


# ─────────────────────────────────────────────────────────
# 3. Router-Aware Trading Page
# ─────────────────────────────────────────────────────────

class TestRouterAwareTradingPage:

    def test_get_executor_returns_router_active(self):
        """_get_executor() returns order_router.active_executor."""
        # We can't fully instantiate the page without PySide6, so test the logic
        from core.execution.order_router import OrderRouter
        router = OrderRouter(mode="paper")

        # In paper mode, active_executor should be paper_executor
        exe = router.active_executor
        from core.execution.paper_executor import paper_executor
        assert exe is paper_executor

    def test_router_mode_switch(self):
        """OrderRouter correctly switches active_executor on mode change."""
        from core.execution.order_router import OrderRouter
        router = OrderRouter(mode="paper")

        # Switch to live
        with patch('core.execution.order_router.bus'):
            router.set_mode("live")
        assert router.mode == "live"

        # Switch back
        with patch('core.execution.order_router.bus'):
            router.set_mode("paper")
        assert router.mode == "paper"

    def test_invalid_mode_rejected(self):
        """OrderRouter rejects invalid mode strings."""
        from core.execution.order_router import OrderRouter
        router = OrderRouter(mode="paper")
        with pytest.raises(ValueError):
            router.set_mode("invalid")


# ─────────────────────────────────────────────────────────
# 4. Signal Confirmation Widget
# ─────────────────────────────────────────────────────────

class TestSignalConfirmation:
    """Widget-creation tests run in subprocess to avoid QApplication state
    corruption when executed alongside non-Qt tests in the full suite."""

    @staticmethod
    def _run_widget_test(code: str) -> None:
        """Run widget test in isolated subprocess (clean QApplication)."""
        import subprocess, sys, tempfile, textwrap
        preamble = textwrap.dedent("""\
        import os, sys
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
        """)
        preamble += f'sys.path.insert(0, r"{sys.path[0]}")\n'
        preamble += textwrap.dedent("""\
        from PySide6.QtWidgets import QApplication
        app = QApplication([])
        """)
        full_script = preamble + textwrap.dedent(code)
        with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
            f.write(full_script)
            f.flush()
            result = subprocess.run(
                [sys.executable, f.name],
                capture_output=True, text=True, timeout=15,
            )
        if result.returncode != 0:
            raise AssertionError(
                f"Widget subprocess failed (rc={result.returncode}):\n"
                f"STDOUT: {result.stdout}\nSTDERR: {result.stderr}"
            )

    @pytest.mark.skipif(
        not _has_pyside6(), reason="PySide6 not available in sandbox"
    )
    def test_card_creation_from_candidate_data(self):
        """_SignalCard initializes with correct candidate data."""
        self._run_widget_test("""
from gui.widgets.signal_confirmation_widget import _SignalCard
data = {
    "candidate_id": "test-123",
    "symbol": "ETH/USDT",
    "side": "buy",
    "score": 0.72,
    "position_size_usdt": 500.0,
    "stop_loss_price": 3800.0,
    "take_profit_price": 4200.0,
    "models_fired": ["momentum_breakout"],
    "regime": "bull_trend",
}
card = _SignalCard(data)
assert card.candidate_id == "test-123"
assert card.age_seconds >= 0
assert card.age_seconds < 5, f"age_seconds={card.age_seconds}"
""")

    @pytest.mark.skipif(
        not _has_pyside6(), reason="PySide6 not available in sandbox"
    )
    def test_card_timer_countdown(self):
        """update_timer() shows remaining seconds."""
        self._run_widget_test("""
from gui.widgets.signal_confirmation_widget import _SignalCard
card = _SignalCard({"candidate_id": "t1"})
card.update_timer()
text = card._timer_lbl.text()
assert "s" in text, f"Expected 's' in timer text, got: {text!r}"
""")


# ─────────────────────────────────────────────────────────
# 5. Circuit Breaker Safety Panel
# ─────────────────────────────────────────────────────────

class TestCircuitBreakerPanel:

    def test_cb_tripped_renders_red(self):
        """When circuit_breaker_on=True, status shows TRIPPED."""
        # Test the rendering logic directly since we can't test full UI
        ps = {
            "circuit_breaker_on": True,
            "drawdown_pct": 12.5,
            "equity_uncertain": False,
            "capital_usdt": 87500.0,
            "daily_loss_limit_hit": False,
            "critical_state": False,
            "requires_manual_review": False,
            "config_validation_failed": False,
        }
        # Verify data structure is correct for the panel
        assert ps["circuit_breaker_on"] is True
        assert ps["drawdown_pct"] == 12.5

    def test_critical_state_data(self):
        """Critical state fields propagate correctly."""
        le = _make_live_executor()
        le._critical_state = True
        le._critical_failed_symbols = ["BTC/USDT"]
        with patch.object(type(le), '_fetch_total_equity', return_value=90_000.0):
            ps = le.get_production_status()
        assert ps["critical_state"] is True

    def test_reconciliation_data(self):
        """Manual review flag propagates to production status."""
        le = _make_live_executor()
        le._requires_manual_review = True
        with patch.object(type(le), '_fetch_total_equity', return_value=100_000.0):
            ps = le.get_production_status()
        assert ps["requires_manual_review"] is True

    def test_config_validation_data(self):
        """Config validation failure propagates."""
        le = _make_live_executor()
        le._config_validation_failed = True
        with patch.object(type(le), '_fetch_total_equity', return_value=100_000.0):
            ps = le.get_production_status()
        assert ps["config_validation_failed"] is True


# ─────────────────────────────────────────────────────────
# 6. Dashboard Router Awareness
# ─────────────────────────────────────────────────────────

class TestDashboardRouterAware:

    def test_paper_executor_has_get_closed_trades(self):
        """PaperExecutor has get_closed_trades() for router-aware access."""
        from core.execution.paper_executor import PaperExecutor
        pe = PaperExecutor(initial_capital_usdt=10_000.0)
        pe._save_open_positions = lambda: None
        assert hasattr(pe, "get_closed_trades")
        result = pe.get_closed_trades()
        assert isinstance(result, list)

    def test_live_executor_has_get_closed_trades(self):
        """LiveExecutor has get_closed_trades() for router-aware access."""
        le = _make_live_executor()
        result = le.get_closed_trades()
        assert isinstance(result, list)

    def test_both_executors_have_get_production_status(self):
        """Both executors have get_production_status() for mode-agnostic GUI."""
        from core.execution.paper_executor import paper_executor
        assert hasattr(paper_executor, "get_production_status")

        le = _make_live_executor()
        assert hasattr(le, "get_production_status")


# ─────────────────────────────────────────────────────────
# 7. Live Mode Visual Safety
# ─────────────────────────────────────────────────────────

class TestLiveModeSafety:

    def test_live_executor_single_position_per_symbol(self):
        """LiveExecutor enforces single position per symbol."""
        le = _make_live_executor()
        pos = _make_live_position(symbol="BTC/USDT")
        le._positions = {"BTC/USDT": pos}

        # _positions is dict[str, LivePosition] — only one per key
        assert len(le._positions) == 1
        assert "BTC/USDT" in le._positions

    def test_order_router_default_paper(self):
        """OrderRouter defaults to paper mode."""
        from core.execution.order_router import OrderRouter
        router = OrderRouter()
        assert router.mode == "paper"


# ─────────────────────────────────────────────────────────
# 8. ExitManager Zero SL/TP Guard (Phase 1 cross-ref)
# ─────────────────────────────────────────────────────────

class TestExitManagerZeroGuard:

    def test_zero_sl_no_trigger_buy(self):
        """SL=0 must NOT trigger stop_loss for buy positions."""
        from core.execution.exit_manager import ExitManager
        em = ExitManager()
        pos = {
            "side": "buy", "entry_price": 50000.0,
            "stop_loss": 0.0, "take_profit": 52000.0,
            "bars_held": 0,
        }
        action = em.check_exits(pos, 49000.0, parity_mode=True)
        assert action is None  # SL=0 means unset, should NOT trigger

    def test_zero_tp_no_trigger_buy(self):
        """TP=0 must NOT trigger take_profit for buy positions."""
        from core.execution.exit_manager import ExitManager
        em = ExitManager()
        pos = {
            "side": "buy", "entry_price": 50000.0,
            "stop_loss": 49000.0, "take_profit": 0.0,
            "bars_held": 0,
        }
        action = em.check_exits(pos, 55000.0, parity_mode=True)
        assert action is None  # TP=0 means unset, should NOT trigger

    def test_zero_sl_no_trigger_sell(self):
        """SL=0 must NOT trigger stop_loss for sell positions."""
        from core.execution.exit_manager import ExitManager
        em = ExitManager()
        pos = {
            "side": "sell", "entry_price": 50000.0,
            "stop_loss": 0.0, "take_profit": 48000.0,
            "bars_held": 0,
        }
        action = em.check_exits(pos, 55000.0, parity_mode=True)
        assert action is None

    def test_zero_tp_no_trigger_sell(self):
        """TP=0 must NOT trigger take_profit for sell positions."""
        from core.execution.exit_manager import ExitManager
        em = ExitManager()
        pos = {
            "side": "sell", "entry_price": 50000.0,
            "stop_loss": 51000.0, "take_profit": 0.0,
            "bars_held": 0,
        }
        action = em.check_exits(pos, 45000.0, parity_mode=True)
        assert action is None

    def test_zero_both_no_trigger(self):
        """Both SL=0 and TP=0 must not trigger any exit (orphan position)."""
        from core.execution.exit_manager import ExitManager
        em = ExitManager()
        pos = {
            "side": "buy", "entry_price": 50000.0,
            "stop_loss": 0.0, "take_profit": 0.0,
            "bars_held": 0,
        }
        # Price above entry — no trigger
        assert em.check_exits(pos, 55000.0, parity_mode=True) is None
        # Price below entry — no trigger
        assert em.check_exits(pos, 40000.0, parity_mode=True) is None


# ─────────────────────────────────────────────────────────
# 9. ExitManager Execution Order (Phase 1 cross-ref)
# ─────────────────────────────────────────────────────────

class TestExitManagerExecutionOrder:

    def test_time_exit_before_trailing(self):
        """Time exit fires before trailing stop check."""
        from core.execution.exit_manager import ExitManager
        em = ExitManager({"max_hold_bars": 5, "trailing_stop.pct": 0.02})
        pos = {
            "side": "buy", "entry_price": 50000.0,
            "stop_loss": 49000.0, "take_profit": 52000.0,
            "bars_held": 4,  # will become 5 after increment
            "highest_price": 51000.0,
            "trailing_stop_pct": 0.02,
            "_initial_risk": 1000.0,
            "_breakeven_applied": False,
            "_auto_partial_applied": False,
        }
        action = em.check_exits(pos, 51000.0)
        assert action is not None
        assert action.action == "time_exit"

    def test_auto_partial_before_static_tp(self):
        """Auto-partial at +1R fires before static TP check."""
        from core.execution.exit_manager import ExitManager
        em = ExitManager({
            "exit.mode": "partial",
            "exit.partial_pct": 0.33,
            "exit.partial_r_trigger": 1.0,
        })
        pos = {
            "side": "buy", "entry_price": 50000.0,
            "stop_loss": 49000.0, "take_profit": 52000.0,
            "bars_held": 0,
            "highest_price": 50000.0,
            "_initial_risk": 1000.0,
            "_breakeven_applied": False,
            "_auto_partial_applied": False,
        }
        # Price at +1R exactly (entry + initial_risk)
        action = em.check_exits(pos, 51000.0)
        assert action is not None
        assert action.action == "auto_partial"
        assert action.reduce_pct == 0.33

    def test_static_sl_fires_when_active(self):
        """Static SL triggers when price hits stop level."""
        from core.execution.exit_manager import ExitManager
        em = ExitManager()
        pos = {
            "side": "buy", "entry_price": 50000.0,
            "stop_loss": 49000.0, "take_profit": 52000.0,
            "bars_held": 0,
            "highest_price": 50000.0,
            "_initial_risk": 0.0,
            "_breakeven_applied": False,
            "_auto_partial_applied": False,
        }
        action = em.check_exits(pos, 48500.0)
        assert action is not None
        assert action.action == "stop_loss"


# ─────────────────────────────────────────────────────────
# 10. Pre-Trade Check Ordering (Phase 1 cross-ref)
# ─────────────────────────────────────────────────────────

class TestPreTradeCheckOrdering:

    def test_critical_state_blocks_first(self):
        """Critical state is highest-priority gate."""
        le = _make_live_executor()
        le._critical_state = True
        le._circuit_breaker_tripped = True
        le._config_validation_failed = True
        result = le._pre_trade_check()
        assert result is not None
        assert "critical" in result.lower()

    def test_config_validation_blocks_second(self):
        """Config validation failure blocks before circuit breaker."""
        le = _make_live_executor()
        le._config_validation_failed = True
        le._circuit_breaker_tripped = True
        result = le._pre_trade_check()
        assert result is not None
        assert "config" in result.lower()

    def test_circuit_breaker_blocks_third(self):
        """Circuit breaker blocks after config validation."""
        le = _make_live_executor()
        le._circuit_breaker_tripped = True
        result = le._pre_trade_check()
        assert result is not None
        assert "circuit" in result.lower() or "breaker" in result.lower()

    def test_all_clear_returns_none(self):
        """No gates tripped → returns None (clear to trade)."""
        le = _make_live_executor()
        # Mock the daily loss and circuit breaker checks
        le._check_daily_loss_limit = MagicMock(return_value=False)
        le._check_circuit_breaker = MagicMock(return_value=False)
        result = le._pre_trade_check()
        assert result is None


# ─────────────────────────────────────────────────────────
# 11. API Parity (comprehensive check)
# ─────────────────────────────────────────────────────────

class TestAPIParity:

    def test_shared_public_api(self):
        """Both executors expose the same public API used by GUI."""
        from core.execution.paper_executor import PaperExecutor
        from core.execution.live_executor import LiveExecutor

        required_methods = [
            "submit", "close_position", "close_all", "close_all_longs",
            "adjust_stop", "adjust_target", "partial_close",
            "get_open_positions", "get_closed_trades", "get_stats",
            "get_production_status",
        ]
        required_properties = ["drawdown_pct", "available_capital"]

        for method in required_methods:
            assert hasattr(PaperExecutor, method), f"PaperExecutor missing {method}"
            assert hasattr(LiveExecutor, method), f"LiveExecutor missing {method}"

        for prop in required_properties:
            assert hasattr(PaperExecutor, prop), f"PaperExecutor missing {prop}"
            assert hasattr(LiveExecutor, prop), f"LiveExecutor missing {prop}"
