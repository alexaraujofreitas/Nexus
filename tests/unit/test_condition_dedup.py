# ============================================================
# NEXUS TRADER — Unit tests for condition-based deduplication
#
# Tests that PaperExecutor.submit() rejects duplicate conditions
# (same side + models_fired set + regime) for the same symbol,
# while allowing different conditions for the same symbol.
#
# CD-01  Same condition → rejected
# CD-02  Different side → allowed
# CD-03  Different models → allowed
# CD-04  Different regime → allowed
# CD-05  Different symbol, same condition → allowed
# CD-06  Model order doesn't matter (set equality)
# CD-07  Regime comparison is case-insensitive
# CD-08  Multiple open positions, one matches → rejected
# CD-09  Multiple open positions, none match → allowed
# CD-10  has_duplicate_condition() standalone test
# CD-11  _condition_fingerprint() is stable
# CD-12  After closing a position, same condition is allowed again
# CD-13  Empty models_fired treated correctly
# ============================================================
from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

from core.meta_decision.order_candidate import OrderCandidate


# ── helpers ──────────────────────────────────────────────────

def _cand(
    symbol:  str   = "BTC/USDT",
    side:    str   = "buy",
    entry:   float = 65_000.0,
    sl:      float = 63_700.0,
    tp:      float = 68_000.0,
    size:    float = 100.0,
    score:   float = 0.78,
    models:  list  = None,
    regime:  str   = "bull_trend",
) -> OrderCandidate:
    c = OrderCandidate(
        symbol             = symbol,
        side               = side,
        entry_type         = "limit",
        entry_price        = entry,
        stop_loss_price    = sl,
        take_profit_price  = tp,
        position_size_usdt = size,
        score              = score,
        models_fired       = models if models is not None else ["trend"],
        regime             = regime,
        rationale          = "dedup test",
        timeframe          = "1h",
        atr_value          = 650.0,
        expiry             = datetime.utcnow() + timedelta(hours=1),
    )
    c.approved = True
    return c


# ── Tests ────────────────────────────────────────────────────

class TestConditionDedup:
    """CD-01 through CD-13: Condition-based deduplication in PaperExecutor."""

    def test_cd01_same_condition_rejected(self, paper_executor):
        """CD-01: Second submit with identical (side, models, regime) → False."""
        pe = paper_executor
        c1 = _cand(models=["trend"], regime="bull_trend", side="buy")
        c2 = _cand(models=["trend"], regime="bull_trend", side="buy")
        assert pe.submit(c1) is True
        assert pe.submit(c2) is False
        # Only 1 position should exist
        assert len(pe._positions.get("BTC/USDT", [])) == 1

    def test_cd02_different_side_allowed(self, paper_executor):
        """CD-02: Same models+regime but opposite side → allowed."""
        pe = paper_executor
        c1 = _cand(side="buy",  models=["trend"], regime="bull_trend")
        c2 = _cand(side="sell", models=["trend"], regime="bull_trend")
        assert pe.submit(c1) is True
        assert pe.submit(c2) is True
        assert len(pe._positions.get("BTC/USDT", [])) == 2

    def test_cd03_different_models_allowed(self, paper_executor):
        """CD-03: Same side+regime but different models → allowed."""
        pe = paper_executor
        c1 = _cand(models=["trend"],           regime="bull_trend")
        c2 = _cand(models=["mean_reversion"],  regime="bull_trend")
        assert pe.submit(c1) is True
        assert pe.submit(c2) is True
        assert len(pe._positions.get("BTC/USDT", [])) == 2

    def test_cd04_different_regime_allowed(self, paper_executor):
        """CD-04: Same models+side but different regime → allowed."""
        pe = paper_executor
        c1 = _cand(models=["trend"], regime="bull_trend")
        c2 = _cand(models=["trend"], regime="ranging")
        assert pe.submit(c1) is True
        assert pe.submit(c2) is True
        assert len(pe._positions.get("BTC/USDT", [])) == 2

    def test_cd05_different_symbol_not_affected(self, paper_executor):
        """CD-05: Same condition but different symbol → allowed."""
        pe = paper_executor
        c1 = _cand(symbol="BTC/USDT", models=["trend"], regime="bull_trend")
        c2 = _cand(symbol="ETH/USDT", models=["trend"], regime="bull_trend",
                    entry=2000.0, sl=1950.0, tp=2100.0)
        assert pe.submit(c1) is True
        assert pe.submit(c2) is True

    def test_cd06_model_order_invariant(self, paper_executor):
        """CD-06: Models in different order = same set → rejected."""
        pe = paper_executor
        c1 = _cand(models=["trend", "momentum_breakout"], regime="bull_trend")
        c2 = _cand(models=["momentum_breakout", "trend"], regime="bull_trend")
        assert pe.submit(c1) is True
        assert pe.submit(c2) is False

    def test_cd07_regime_case_insensitive(self, paper_executor):
        """CD-07: Regime comparison is case-insensitive."""
        pe = paper_executor
        c1 = _cand(models=["trend"], regime="Bull_Trend")
        c2 = _cand(models=["trend"], regime="bull_trend")
        assert pe.submit(c1) is True
        assert pe.submit(c2) is False

    def test_cd08_multiple_open_one_matches(self, paper_executor):
        """CD-08: Multiple positions open, new candidate matches one → rejected."""
        pe = paper_executor
        c1 = _cand(models=["mean_reversion"], regime="ranging")
        c2 = _cand(models=["trend"],          regime="bull_trend")
        c3 = _cand(models=["trend"],          regime="bull_trend")  # matches c2
        assert pe.submit(c1) is True
        assert pe.submit(c2) is True
        assert pe.submit(c3) is False
        assert len(pe._positions.get("BTC/USDT", [])) == 2

    def test_cd09_multiple_open_none_match(self, paper_executor):
        """CD-09: Multiple positions open, new candidate doesn't match any → allowed."""
        pe = paper_executor
        c1 = _cand(models=["mean_reversion"], regime="ranging")
        c2 = _cand(models=["trend"],          regime="bull_trend")
        c3 = _cand(models=["momentum_breakout"], regime="vol_expansion")
        assert pe.submit(c1) is True
        assert pe.submit(c2) is True
        assert pe.submit(c3) is True
        assert len(pe._positions.get("BTC/USDT", [])) == 3

    def test_cd10_has_duplicate_condition_standalone(self, paper_executor):
        """CD-10: has_duplicate_condition() returns correct bool."""
        pe = paper_executor
        c1 = _cand(models=["trend"], regime="bull_trend", side="buy")
        pe.submit(c1)
        # Same condition → True
        assert pe.has_duplicate_condition("BTC/USDT", "buy", ["trend"], "bull_trend") is True
        # Different side → False
        assert pe.has_duplicate_condition("BTC/USDT", "sell", ["trend"], "bull_trend") is False
        # Different models → False
        assert pe.has_duplicate_condition("BTC/USDT", "buy", ["mean_reversion"], "bull_trend") is False
        # Different regime → False
        assert pe.has_duplicate_condition("BTC/USDT", "buy", ["trend"], "ranging") is False
        # Different symbol → False (no positions for ETH)
        assert pe.has_duplicate_condition("ETH/USDT", "buy", ["trend"], "bull_trend") is False

    def test_cd11_fingerprint_is_stable(self, paper_executor):
        """CD-11: _condition_fingerprint produces consistent output."""
        from core.execution.paper_executor import PaperExecutor
        fp1 = PaperExecutor._condition_fingerprint("buy", ["trend", "momentum"], "bull_trend")
        fp2 = PaperExecutor._condition_fingerprint("buy", ["momentum", "trend"], "bull_trend")
        assert fp1 == fp2
        fp3 = PaperExecutor._condition_fingerprint("buy", ["trend", "momentum"], "Bull_Trend")
        assert fp1 == fp3

    def test_cd12_after_close_same_condition_allowed(self, paper_executor):
        """CD-12: After closing a position, the same condition can be opened again."""
        pe = paper_executor
        c1 = _cand(models=["trend"], regime="bull_trend")
        assert pe.submit(c1) is True
        # Close the position
        pe.close_position("BTC/USDT", price=66_000.0)
        assert len(pe._positions.get("BTC/USDT", [])) == 0
        # Same condition should now be allowed
        c2 = _cand(models=["trend"], regime="bull_trend")
        assert pe.submit(c2) is True

    def test_cd13_empty_models_treated_correctly(self, paper_executor):
        """CD-13: Empty models_fired list → fingerprint still works."""
        pe = paper_executor
        c1 = _cand(models=[], regime="bull_trend")
        c2 = _cand(models=[], regime="bull_trend")
        assert pe.submit(c1) is True
        assert pe.submit(c2) is False  # same empty set = duplicate
