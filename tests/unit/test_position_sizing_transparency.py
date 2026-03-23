"""
tests/unit/test_position_sizing_transparency.py
================================================
Regression tests for the Trade History position-sizing transparency feature
(Session 30 — Entry Size / Exit Size columns).

Covers:
  PST-01  PaperPosition stores entry_size_usdt at init
  PST-02  entry_size_usdt is immutable even after partial_close reduces size_usdt
  PST-03  PaperPosition.to_dict() includes entry_size_usdt
  PST-04  _close_position() trade dict contains entry_size_usdt == exit_size_usdt (full close)
  PST-05  _close_position() trade dict: entry_size_usdt > exit_size_usdt after prior partial close
  PST-06  partial_close() creates a trade record in _closed_trades
  PST-07  partial_close trade record: entry_size_usdt = original, exit_size_usdt = closed fraction
  PST-08  partial_close trade record: exit_reason == "partial_close"
  PST-09  partial_close trade record: pnl_usdt computed correctly (long profit)
  PST-10  partial_close trade record: pnl_usdt computed correctly (short profit)
  PST-11  PaperTrade.to_dict() returns entry_size_usdt / exit_size_usdt
  PST-12  PaperTrade.to_dict() falls back to size_usdt when new fields are NULL
  PST-13  _save_trade_to_db passes entry/exit size fields
  PST-14  Full close: entry_size_usdt and exit_size_usdt are equal
  PST-15  stop_loss close: entry_size_usdt and exit_size_usdt in trade dict
  PST-16  take_profit close: entry_size_usdt and exit_size_usdt in trade dict
  PST-17  _exit_reason_label includes partial_close mapping
"""
from __future__ import annotations

import json, os, sys, tempfile, time
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ── Make sure the project root is on sys.path ──────────────────────────────
_ROOT = Path(__file__).parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


# ── Helpers: build a minimal PaperPosition ────────────────────────────────

def _make_pos(
    symbol="BTC/USDT", side="buy",
    entry_price=80_000.0, size_usdt=500.0,
    stop_loss=78_000.0, take_profit=84_000.0,
    score=0.80,
):
    from core.execution.paper_executor import PaperPosition
    quantity = size_usdt / entry_price
    return PaperPosition(
        symbol=symbol, side=side,
        entry_price=entry_price, quantity=quantity,
        stop_loss=stop_loss, take_profit=take_profit,
        size_usdt=size_usdt, score=score,
        rationale="test", regime="bull_trend",
        models_fired=["trend"], timeframe="1h",
    )


# ── Helpers: build a minimal PaperExecutor with mocked IO ─────────────────

def _make_executor():
    """
    Build a PaperExecutor with all file/DB I/O patched so no real files
    are touched during tests.
    """
    # Patch at class-level so _load_open_positions and _save_* are no-ops
    with patch("core.execution.paper_executor.PaperExecutor._load_open_positions",
               return_value=None), \
         patch("core.execution.paper_executor.PaperExecutor._load_history",
               return_value=None), \
         patch("core.execution.paper_executor.PaperExecutor._save_open_positions",
               return_value=None), \
         patch("core.execution.paper_executor.PaperExecutor._save_trade_to_db",
               return_value=None), \
         patch("core.event_bus.bus.publish", return_value=None), \
         patch("core.event_bus.bus.subscribe", return_value=None):
        from core.execution.paper_executor import PaperExecutor
        pe = PaperExecutor(initial_capital_usdt=100_000.0)
    # Keep save mocks active on the instance
    pe._save_open_positions = MagicMock()
    pe._save_trade_to_db    = MagicMock()
    return pe


# ════════════════════════════════════════════════════════════════════════════
# PST-01 to PST-03 — PaperPosition attribute
# ════════════════════════════════════════════════════════════════════════════

class TestPaperPositionAttribute:

    def test_pst01_entry_size_stored_at_init(self):
        """PST-01: PaperPosition sets entry_size_usdt = size_usdt at creation."""
        pos = _make_pos(size_usdt=500.0)
        assert pos.entry_size_usdt == 500.0

    def test_pst02_entry_size_immutable_after_size_reduction(self):
        """PST-02: Reducing size_usdt (simulating partial close) does NOT change entry_size_usdt."""
        pos = _make_pos(size_usdt=500.0)
        pos.size_usdt = 250.0   # simulate partial_close halving the position
        assert pos.entry_size_usdt == 500.0, (
            "entry_size_usdt must remain 500.0 after size_usdt is reduced"
        )

    def test_pst03_to_dict_includes_entry_size(self):
        """PST-03: PaperPosition.to_dict() includes 'entry_size_usdt' key."""
        pos = _make_pos(size_usdt=500.0)
        d = pos.to_dict()
        assert "entry_size_usdt" in d
        assert d["entry_size_usdt"] == 500.0


# ════════════════════════════════════════════════════════════════════════════
# PST-04 to PST-05 — _close_position() trade dict
# ════════════════════════════════════════════════════════════════════════════

class TestClosePositionTradeDict:

    def _do_close(self, size_usdt=500.0, reduced_to=None):
        """Helper: open a position, optionally reduce its size, then close it."""
        pe  = _make_executor()
        pos = _make_pos(size_usdt=size_usdt)
        if reduced_to is not None:
            pos.size_usdt = reduced_to
        symbol = pos.symbol
        pe._positions[symbol] = [pos]

        with patch("core.event_bus.bus.publish"):
            pe._close_position(symbol, 82_000.0, "manual_close", pos)

        return pe._closed_trades[-1]

    def test_pst04_full_close_entry_equals_exit_size(self):
        """PST-04: Full close → entry_size_usdt == exit_size_usdt == 500."""
        t = self._do_close(size_usdt=500.0)
        assert "entry_size_usdt" in t
        assert "exit_size_usdt"  in t
        assert t["entry_size_usdt"] == 500.0
        assert t["exit_size_usdt"]  == 500.0
        assert t["entry_size_usdt"] == t["exit_size_usdt"]

    def test_pst05_after_partial_close_entry_larger_than_exit(self):
        """PST-05: After a prior partial close, entry_size_usdt > exit_size_usdt."""
        # Simulate: opened at 500, partial close reduced to 250
        t = self._do_close(size_usdt=500.0, reduced_to=250.0)
        assert t["entry_size_usdt"] == 500.0, "Original entry size must be 500"
        assert t["exit_size_usdt"]  == 250.0, "Remaining (closed) size must be 250"
        assert t["entry_size_usdt"] > t["exit_size_usdt"]


# ════════════════════════════════════════════════════════════════════════════
# PST-06 to PST-10 — partial_close() trade record
# ════════════════════════════════════════════════════════════════════════════

class TestPartialCloseTrade:

    def _do_partial(self, side="buy", entry=80_000.0, current=82_000.0,
                    size_usdt=500.0, reduce_pct=0.50):
        pe  = _make_executor()
        pos = _make_pos(side=side, entry_price=entry, size_usdt=size_usdt)
        pos.current_price = current
        symbol = pos.symbol
        pe._positions[symbol] = [pos]
        initial_capital = pe._capital

        with patch("core.event_bus.bus.publish"):
            ok = pe.partial_close(symbol, reduce_pct)

        return ok, pe, pos, pe._closed_trades[-1] if pe._closed_trades else None

    def test_pst06_partial_creates_closed_trade(self):
        """PST-06: partial_close() produces exactly one entry in _closed_trades."""
        ok, pe, pos, trade = self._do_partial()
        assert ok is True
        assert len(pe._closed_trades) == 1
        assert trade is not None

    def test_pst07_partial_entry_and_exit_sizes(self):
        """PST-07: Partial close record: entry_size = 500, exit_size = 250 (50%)."""
        ok, pe, pos, trade = self._do_partial(size_usdt=500.0, reduce_pct=0.50)
        assert trade["entry_size_usdt"] == 500.0, (
            f"Expected entry_size_usdt=500, got {trade['entry_size_usdt']}"
        )
        assert abs(trade["exit_size_usdt"] - 250.0) < 0.01, (
            f"Expected exit_size_usdt=250, got {trade['exit_size_usdt']}"
        )

    def test_pst08_partial_exit_reason(self):
        """PST-08: partial_close record has exit_reason == 'partial_close'."""
        ok, pe, pos, trade = self._do_partial()
        assert trade["exit_reason"] == "partial_close"

    def test_pst09_partial_pnl_long(self):
        """PST-09: partial_close long profit: entry 80k, exit 82k, 50% of 500 USDT position."""
        # Qty = 500/80000 = 0.00625 BTC; close 50% = 0.003125 BTC
        # Rough P&L = (82000-80000) * 0.003125 = 6.25 USDT (before slippage)
        ok, pe, pos, trade = self._do_partial(
            side="buy", entry=80_000.0, current=82_000.0,
            size_usdt=500.0, reduce_pct=0.50,
        )
        assert trade["pnl_usdt"] > 0, (
            f"Expected positive P&L for long profit, got {trade['pnl_usdt']}"
        )

    def test_pst10_partial_pnl_short(self):
        """PST-10: partial_close short profit: entry 80k, exit 78k, 50% position."""
        # Qty = 500/80000; close 50%
        # Short profit: (80000-78000) * qty * 0.5 > 0
        ok, pe, pos, trade = self._do_partial(
            side="sell", entry=80_000.0, current=78_000.0,
            size_usdt=500.0, reduce_pct=0.50,
        )
        assert trade["pnl_usdt"] > 0, (
            f"Expected positive P&L for short profit, got {trade['pnl_usdt']}"
        )


# ════════════════════════════════════════════════════════════════════════════
# PST-11 to PST-12 — PaperTrade DB model
# ════════════════════════════════════════════════════════════════════════════

class TestPaperTradeModel:

    @staticmethod
    def _make_db_engine():
        """Create a throw-away in-memory SQLAlchemy engine with all tables."""
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from sqlalchemy.pool import StaticPool
        import core.database.models  # ensure models are registered against Base
        from core.database.engine import Base

        eng = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(eng)
        return eng, sessionmaker(bind=eng)

    def test_pst11_to_dict_includes_size_fields(self):
        """PST-11: PaperTrade.to_dict() returns entry_size_usdt and exit_size_usdt."""
        from core.database.models import PaperTrade

        eng, Session = self._make_db_engine()
        with Session() as s:
            pt = PaperTrade(
                symbol="BTC/USDT", side="buy",
                regime="bull_trend", timeframe="1h",
                entry_price=80_000.0, exit_price=82_000.0,
                stop_loss=78_000.0, take_profit=84_000.0,
                size_usdt=250.0,
                entry_size_usdt=500.0,
                exit_size_usdt=250.0,
                pnl_usdt=6.25, pnl_pct=1.25,
                score=0.8, exit_reason="manual_close",
                models_fired=["trend"], rationale="",
                duration_s=600,
                opened_at="2026-03-23T10:00:00",
                closed_at="2026-03-23T10:10:00",
            )
            s.add(pt)
            s.flush()
            d = pt.to_dict()

        assert d["entry_size_usdt"] == 500.0, (
            f"Expected entry_size_usdt=500, got {d['entry_size_usdt']}"
        )
        assert d["exit_size_usdt"] == 250.0, (
            f"Expected exit_size_usdt=250, got {d['exit_size_usdt']}"
        )

    def test_pst12_null_fallback_to_size_usdt(self):
        """PST-12: When entry/exit_size_usdt are NULL, to_dict() falls back to size_usdt."""
        from core.database.models import PaperTrade

        eng, Session = self._make_db_engine()
        with Session() as s:
            pt = PaperTrade(
                symbol="ETH/USDT", side="buy",
                regime="", timeframe="1h",
                entry_price=3000.0, exit_price=3100.0,
                stop_loss=None, take_profit=None,
                size_usdt=500.0,
                entry_size_usdt=None,   # NULL — pre-Session-30 row
                exit_size_usdt=None,    # NULL — pre-Session-30 row
                pnl_usdt=16.67, pnl_pct=3.33,
                score=0.0, exit_reason="",
                models_fired=None, rationale=None,
                duration_s=0,
                opened_at="2026-03-23T10:00:00",
                closed_at="2026-03-23T10:10:00",
            )
            s.add(pt)
            s.flush()
            d = pt.to_dict()

        assert d["entry_size_usdt"] == 500.0, (
            f"Expected fallback to size_usdt=500, got {d['entry_size_usdt']}"
        )
        assert d["exit_size_usdt"] == 500.0, (
            f"Expected fallback to size_usdt=500, got {d['exit_size_usdt']}"
        )


# ════════════════════════════════════════════════════════════════════════════
# PST-13 — _save_trade_to_db fields
# ════════════════════════════════════════════════════════════════════════════

class TestSaveTradeToDB:

    def test_pst13_save_passes_size_fields(self):
        """PST-13: _save_trade_to_db() passes entry_size_usdt and exit_size_usdt to PaperTrade()."""
        captured_kwargs = {}

        class FakePaperTrade:
            def __init__(self, **kw):
                captured_kwargs.update(kw)

        class _FakeSession:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def add(self, obj): pass
            def commit(self): pass
            def rollback(self): pass

        from contextlib import contextmanager

        @contextmanager
        def _fake_get_session():
            yield _FakeSession()

        trade = {
            "symbol": "BTC/USDT", "side": "buy",
            "regime": "bull_trend", "timeframe": "1h",
            "entry_price": 80_000.0, "exit_price": 82_000.0,
            "stop_loss": 78_000.0, "take_profit": 84_000.0,
            "size_usdt": 250.0,
            "entry_size_usdt": 500.0,
            "exit_size_usdt":  250.0,
            "pnl_usdt": 6.25, "pnl_pct": 1.25,
            "score": 0.8, "exit_reason": "manual_close",
            "models_fired": ["trend"], "rationale": "",
            "duration_s": 600,
            "opened_at": "2026-03-23T10:00:00",
            "closed_at": "2026-03-23T10:10:00",
        }

        pe = _make_executor()

        # Patch the lazy imports inside _save_trade_to_db directly on the source modules.
        # When the method does `from core.database.engine import get_session`, Python looks
        # up sys.modules["core.database.engine"].get_session — patching the attribute there
        # intercepts the lazy import correctly.
        import core.execution.paper_executor as _pe_mod
        with patch("core.database.engine.get_session", _fake_get_session), \
             patch("core.database.models.PaperTrade", FakePaperTrade):
            # Call the unbound real method directly (bypasses the MagicMock on the instance)
            _pe_mod.PaperExecutor._save_trade_to_db(pe, trade)

        assert captured_kwargs.get("entry_size_usdt") == 500.0, (
            f"entry_size_usdt not passed, got {captured_kwargs}"
        )
        assert captured_kwargs.get("exit_size_usdt") == 250.0, (
            f"exit_size_usdt not passed, got {captured_kwargs}"
        )


# ════════════════════════════════════════════════════════════════════════════
# PST-14 to PST-16 — Scenario tests: full close, stop-loss, take-profit
# ════════════════════════════════════════════════════════════════════════════

class TestScenarios:

    def _close_with_reason(self, reason_price, reason):
        pe  = _make_executor()
        pos = _make_pos(size_usdt=500.0, entry_price=80_000.0,
                        stop_loss=78_000.0, take_profit=84_000.0)
        pe._positions[pos.symbol] = [pos]

        with patch("core.event_bus.bus.publish"):
            pe._close_position(pos.symbol, reason_price, reason, pos)

        return pe._closed_trades[-1]

    def test_pst14_full_close_sizes_equal(self):
        """PST-14: Manual full close: entry_size == exit_size == 500."""
        t = self._close_with_reason(82_000.0, "manual_close")
        assert t["entry_size_usdt"] == t["exit_size_usdt"] == 500.0

    def test_pst15_stop_loss_sizes(self):
        """PST-15: Stop-loss close: entry_size and exit_size present and equal."""
        t = self._close_with_reason(78_000.0, "stop_loss")
        assert t["entry_size_usdt"] == 500.0
        assert t["exit_size_usdt"]  == 500.0

    def test_pst16_take_profit_sizes(self):
        """PST-16: Take-profit close: entry_size and exit_size present and equal."""
        t = self._close_with_reason(84_000.0, "take_profit")
        assert t["entry_size_usdt"] == 500.0
        assert t["exit_size_usdt"]  == 500.0


# ════════════════════════════════════════════════════════════════════════════
# PST-17 — UI exit_reason_label
# ════════════════════════════════════════════════════════════════════════════

class TestExitReasonLabel:

    def test_pst17_partial_close_label(self):
        """PST-17: paper_trading_page._exit_reason_label has amber partial_close mapping.

        We verify via source inspection rather than Qt import to avoid libEGL
        dependency in headless unit-test runs.  The UI checks (scripts/run_ui_checks.py)
        cover the full Qt import path with the EGL stub in place.
        """
        page_file = _ROOT / "gui" / "pages" / "paper_trading" / "paper_trading_page.py"
        source = page_file.read_text(encoding="utf-8")

        # The function must define the partial_close mapping
        assert '"partial_close"' in source, (
            "_exit_reason_label must contain a 'partial_close' key"
        )
        assert '"Partial Close"' in source, (
            "_exit_reason_label must label partial_close as 'Partial Close'"
        )
        # Amber color must be present and near the partial_close key
        assert '"#FFB300"' in source, (
            "_exit_reason_label must use amber #FFB300 for partial_close"
        )
        # Verify proximity: amber color appears within 120 chars of the partial_close key
        idx_partial = source.index('"partial_close"')
        idx_amber   = source.index('"#FFB300"', idx_partial)
        assert idx_amber - idx_partial < 120, (
            "The #FFB300 color must be on the same line as the partial_close entry"
        )
