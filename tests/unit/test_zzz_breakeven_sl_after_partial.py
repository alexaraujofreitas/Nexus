"""
Section-1 regression tests: breakeven SL after partial_close().

Tests verify:
  1. partial_close() sets pos.stop_loss == pos.entry_price immediately
  2. partial_close() sets pos._breakeven_applied == True immediately
  3. PaperPosition.to_dict() serialises _breakeven_applied
  4. _load_open_positions() restores _breakeven_applied from JSON
  5. Restored position does NOT re-trigger SL move on next update() call
  6. pos.stop_loss in partial_trade dict equals pos.entry_price (notification correctness)
  7. Edge case: partial_close on a sell (short) position also sets breakeven SL
  8. _breakeven_applied is NOT present in pre-v1.2 JSON (backward compat — defaults False)
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Path setup so tests can import core modules without a full app boot
# ---------------------------------------------------------------------------
ROOT = Path(__file__).parents[2]          # …/NexusTrader
sys.path.insert(0, str(ROOT))


# ---------------------------------------------------------------------------
# Minimal stubs to satisfy paper_executor imports without real infra
# ---------------------------------------------------------------------------

def _patch_imports():
    """Patch heavy/unavailable imports before importing paper_executor."""
    # config.settings stub
    settings_mock = MagicMock()
    settings_mock.get = lambda key, default=None: default
    sys.modules.setdefault("config", MagicMock())
    sys.modules["config.settings"] = MagicMock(settings=settings_mock)

    # event bus stub — silently absorbs publish / subscribe
    bus_mock = MagicMock()
    bus_mock.subscribe = MagicMock()
    bus_mock.publish   = MagicMock()
    topics_mock = MagicMock()
    topics_mock.POSITION_MONITOR_UPDATED = "position.monitor.updated"
    topics_mock.TRADE_CLOSED             = "trade.closed"
    topics_mock.POSITION_UPDATED         = "position.updated"
    sys.modules["core.event_bus"] = MagicMock(bus=bus_mock, Topics=topics_mock)

    # database / analytics stubs
    # NOTE: core.meta_decision.confluence_scorer is intentionally excluded —
    # paper_executor only imports it lazily (inside functions), so stubbing it
    # at module level contaminates tests/unit/test_confluence.py which needs
    # the real ConfluenceScorer class.
    for mod in (
        "core.database.engine",
        "core.analytics.filter_stats",
        "core.monitoring.trade_monitor",
        "core.learning.trade_outcome_store",
        "core.learning.level2_tracker",
    ):
        sys.modules.setdefault(mod, MagicMock())

    return bus_mock, topics_mock, settings_mock


_bus_mock, _topics_mock, _settings_mock = _patch_imports()

# Now safe to import
from core.execution.paper_executor import PaperExecutor, PaperPosition  # noqa: E402

# Capture the REAL _load_open_positions at import time — before conftest.py's
# autouse fixture replaces it with a no-op.  The persistence tests call this
# directly so they can test the restore path despite disk-IO isolation.
_REAL_LOAD_OPEN_POSITIONS = PaperExecutor._load_open_positions


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_long_pos(entry=100.0, stop=90.0, qty=1.0, size=100.0) -> PaperPosition:
    return PaperPosition(
        symbol="BTC/USDT", side="buy",
        entry_price=entry, quantity=qty,
        stop_loss=stop, take_profit=130.0,
        size_usdt=size,
        score=0.7, rationale="test", regime="bull_trend",
        models_fired=["TrendModel"], timeframe="30m",
        opened_at=datetime.utcnow(),
    )


def _make_short_pos(entry=100.0, stop=110.0, qty=1.0, size=100.0) -> PaperPosition:
    return PaperPosition(
        symbol="ETH/USDT", side="sell",
        entry_price=entry, quantity=qty,
        stop_loss=stop, take_profit=70.0,
        size_usdt=size,
        score=0.65, rationale="test short", regime="bear_trend",
        models_fired=["MomentumBreakout"], timeframe="30m",
        opened_at=datetime.utcnow(),
    )


def _make_executor(initial_capital: float = 10_000.0) -> PaperExecutor:
    """Create a PaperExecutor with an in-memory (non-existent) positions file."""
    with patch("core.execution.paper_executor._OPEN_POSITIONS_FILE",
               Path(tempfile.mktemp(suffix=".json"))):
        exe = PaperExecutor(initial_capital_usdt=initial_capital)
    return exe


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestPartialCloseBreakevenSL:

    def setup_method(self):
        self.exe = _make_executor()
        # Inject a long position at 100, current price 110 (at +1R from 10-pt stop)
        pos = _make_long_pos(entry=100.0, stop=90.0, qty=1.0, size=100.0)
        pos.current_price = 110.0          # +1R (price - entry == stop_distance)
        self.exe._positions["BTC/USDT"] = [pos]
        self.pos = pos

    # ------------------------------------------------------------------
    # T1 — stop_loss set to entry_price immediately inside partial_close()
    # ------------------------------------------------------------------
    def test_t1_stop_loss_moves_to_entry_after_partial(self):
        assert self.pos.stop_loss == 90.0, "pre-condition: SL should start at 90"
        self.exe.partial_close("BTC/USDT", 0.33)
        assert self.pos.stop_loss == self.pos.entry_price, (
            f"stop_loss should be entry_price ({self.pos.entry_price}) after partial close, "
            f"got {self.pos.stop_loss}"
        )

    # ------------------------------------------------------------------
    # T2 — _breakeven_applied flag set to True immediately
    # ------------------------------------------------------------------
    def test_t2_breakeven_applied_flag_set(self):
        assert not self.pos._breakeven_applied, "pre-condition: flag should be False"
        self.exe.partial_close("BTC/USDT", 0.33)
        assert self.pos._breakeven_applied is True, (
            "_breakeven_applied must be True immediately after partial_close()"
        )

    # ------------------------------------------------------------------
    # T3 — to_dict() serialises _breakeven_applied
    # ------------------------------------------------------------------
    def test_t3_to_dict_includes_breakeven_applied(self):
        self.exe.partial_close("BTC/USDT", 0.33)
        d = self.pos.to_dict()
        assert "_breakeven_applied" in d, "to_dict() must include '_breakeven_applied'"
        assert d["_breakeven_applied"] is True

    # ------------------------------------------------------------------
    # T4 — to_dict() before partial returns False for _breakeven_applied
    # ------------------------------------------------------------------
    def test_t4_to_dict_false_before_partial(self):
        d = self.pos.to_dict()
        assert "_breakeven_applied" in d
        assert d["_breakeven_applied"] is False

    # ------------------------------------------------------------------
    # T5 — stop_loss in partial_trade dict equals entry_price
    #       (notification receives correct breakeven value)
    # ------------------------------------------------------------------
    def test_t5_partial_trade_dict_stop_loss_equals_entry(self):
        # paper_executor.bus may point to the real bus (not _bus_mock) when
        # this file is collected LAST (test_zzz_ prefix).  Patch the module-
        # level 'bus' variable in paper_executor so publish calls are captured
        # by _bus_mock regardless of import order.
        from unittest.mock import patch as _patch
        import core.execution.paper_executor as _pe_mod
        _bus_mock.reset_mock()
        with _patch.object(_pe_mod, "bus", _bus_mock):
            self.exe.partial_close("BTC/USDT", 0.33)
        # The last TRADE_CLOSED publish carries partial_trade
        calls = _bus_mock.publish.call_args_list
        trade_closed_calls = [
            c for c in calls
            if c.args and c.args[0] == _topics_mock.TRADE_CLOSED
        ]
        assert trade_closed_calls, "TRADE_CLOSED must be published"
        trade_data = trade_closed_calls[-1].kwargs.get("data", {})
        assert trade_data.get("stop_loss") == self.pos.entry_price, (
            f"partial_trade['stop_loss'] should equal entry_price={self.pos.entry_price}, "
            f"got {trade_data.get('stop_loss')}"
        )
        assert trade_data.get("exit_reason") == "partial_close"


class TestBreakevenRestorePersistence:
    """
    These tests call _load_open_positions() directly on a fresh executor to
    isolate the restore path without needing a real SQLite DB.
    """

    def _restore_from_dict(self, pos_dict: dict, capital: float = 10_000.0) -> PaperExecutor:
        """
        Build a minimal PaperExecutor via __new__ (skips __init__ / DB calls)
        then call _load_open_positions() directly with a temp JSON file.
        This isolates the restore logic from all external dependencies.
        """
        import tempfile
        pf = Path(tempfile.mktemp(suffix="_test_positions.json"))
        pf.write_text(json.dumps({"capital": capital, "peak_capital": capital,
                                   "positions": [pos_dict]}))
        # Build the minimal executor state that _load_open_positions() needs
        exe = PaperExecutor.__new__(PaperExecutor)
        exe._initial_capital          = capital
        exe._capital                  = capital
        exe._peak_capital             = capital
        exe._positions                = {}
        exe._closed_trades            = []
        exe._max_positions_per_symbol = 10
        # Call the REAL method directly (conftest autouse replaces it with a no-op)
        with patch("core.execution.paper_executor._OPEN_POSITIONS_FILE", pf):
            _REAL_LOAD_OPEN_POSITIONS(exe)
        pf.unlink(missing_ok=True)
        return exe

    def test_t6_breakeven_applied_restored_from_json(self):
        """Positions saved with _breakeven_applied=True restore with True."""
        pos_dict = {
            "symbol":                "BTC/USDT",
            "side":                  "buy",
            "entry_price":           100.0,
            "current_price":         110.0,
            "quantity":              0.67,
            "stop_loss":             100.0,     # already at breakeven
            "take_profit":           130.0,
            "size_usdt":             67.0,
            "entry_size_usdt":       100.0,
            "unrealized_pnl":        6.7,
            "score":                 0.7,
            "rationale":             "test",
            "regime":                "bull_trend",
            "models_fired":          ["TrendModel"],
            "timeframe":             "30m",
            "opened_at":             datetime.utcnow().isoformat(),
            "_auto_partial_applied": True,
            "_breakeven_applied":    True,         # serialised flag
        }
        exe = self._restore_from_dict(pos_dict)

        pos_list = exe._positions.get("BTC/USDT", [])
        assert pos_list, "Position should have been restored"
        pos = pos_list[0]
        assert pos._breakeven_applied is True, (
            "_breakeven_applied must be restored as True from JSON"
        )
        assert pos.stop_loss == 100.0, "stop_loss must be restored to breakeven (entry_price)"

    def test_t7_breakeven_applied_absent_in_legacy_json_defaults_false(self):
        """Pre-v1.2 JSON without _breakeven_applied restores with False (backward compat)."""
        pos_dict = {
            "symbol":                "SOL/USDT",
            "side":                  "buy",
            "entry_price":           50.0,
            "current_price":         50.0,
            "quantity":              2.0,
            "stop_loss":             46.0,
            "take_profit":           60.0,
            "size_usdt":             100.0,
            "entry_size_usdt":       100.0,
            "unrealized_pnl":        0.0,
            "score":                 0.65,
            "rationale":             "legacy",
            "regime":                "bull_trend",
            "models_fired":          [],
            "timeframe":             "1h",
            "opened_at":             datetime.utcnow().isoformat(),
            "_auto_partial_applied": False,
            # NOTE: _breakeven_applied intentionally absent
        }
        exe = self._restore_from_dict(pos_dict)

        pos = exe._positions.get("SOL/USDT", [None])[0]
        assert pos is not None
        assert pos._breakeven_applied is False, (
            "Legacy positions without _breakeven_applied must default to False"
        )

    def test_t8_restored_position_does_not_re_trigger_sl_move(self):
        """
        A restored position with _breakeven_applied=True should NOT move the SL
        again when update() is called (no double-application).
        """
        entry = 100.0
        pos_dict = {
            "symbol":                "ETH/USDT",
            "side":                  "buy",
            "entry_price":           entry,
            "current_price":         112.0,
            "quantity":              0.67,
            "stop_loss":             entry,    # breakeven
            "take_profit":           130.0,
            "size_usdt":             67.0,
            "entry_size_usdt":       100.0,
            "unrealized_pnl":        8.04,
            "score":                 0.7,
            "rationale":             "partial_already_done",
            "regime":                "bull_trend",
            "models_fired":          ["TrendModel"],
            "timeframe":             "30m",
            "opened_at":             datetime.utcnow().isoformat(),
            "_auto_partial_applied": True,
            "_breakeven_applied":    True,
        }
        exe = self._restore_from_dict(pos_dict)

        pos = exe._positions["ETH/USDT"][0]
        sl_before = pos.stop_loss

        # Simulate next tick — price still above entry
        pos.update(112.0)

        assert pos.stop_loss == sl_before, (
            "stop_loss should not change on update() when _breakeven_applied is already True"
        )
        assert pos._breakeven_applied is True


class TestBreakevenSLShortPosition:

    def test_t9_short_position_breakeven_sl_set_correctly(self):
        """partial_close() on a short must also set stop_loss to entry_price."""
        exe = _make_executor()
        pos = _make_short_pos(entry=100.0, stop=110.0, qty=1.0, size=100.0)
        pos.current_price = 90.0     # +1R on a short
        exe._positions["ETH/USDT"] = [pos]

        assert pos.stop_loss == 110.0, "pre-condition: SL above entry for short"
        exe.partial_close("ETH/USDT", 0.33)

        assert pos.stop_loss == pos.entry_price, (
            f"Short breakeven: stop_loss should be entry_price={pos.entry_price}, "
            f"got {pos.stop_loss}"
        )
        assert pos._breakeven_applied is True
