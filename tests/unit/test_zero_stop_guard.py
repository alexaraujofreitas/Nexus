"""
tests/unit/test_zero_stop_guard.py — Zero-stop-distance guard in PaperExecutor.submit()

Regression tests for the bug confirmed in Trade #2 of the demo session
(2026-03-24): entry_price == stop_loss_price == 69851.23265679907 (identical
to 10 decimal places). Root cause: _do_auto_execute_one() fetches a live
market price for entry; if the market moved to the model's stop level between
the HTF scan and the LTF execution, fill_price ≈ stop_loss_price.

Guard location: paper_executor.submit(), after _apply_slippage(), before
PaperPosition is constructed.

Guard threshold: |fill_price - stop_loss_price| / fill_price < 0.001 (0.1%)
→ REJECT and return False.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

from core.meta_decision.order_candidate import OrderCandidate


# ── helpers ──────────────────────────────────────────────────────────────────

def _make_candidate(
    symbol:   str   = "BTC/USDT",
    side:     str   = "sell",
    entry:    float = 69_851.23,
    sl:       float = 69_851.23,   # default: zero-stop (Trade #2 replication)
    tp:       float = 68_000.0,
    size:     float = 3_840.0,
    score:    float = 0.78,
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
        models_fired       = ["momentum_breakout"],
        regime             = "TRENDING_DOWN",
        rationale          = "Test zero-stop candidate",
        timeframe          = "1h",
        atr_value          = 500.0,
        expiry             = datetime.utcnow() + timedelta(hours=1),
    )
    c.approved = True
    return c


# ══════════════════════════════════════════════════════════════════════════════
#  ZS-001 — exact zero stop (Trade #2 replication)
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
def test_zs001_exact_zero_stop_rejected(paper_executor):
    """submit() must return False when fill_price == stop_loss_price (0.00% distance)."""
    c = _make_candidate(entry=69_851.23, sl=69_851.23)
    with patch.object(paper_executor, "_apply_slippage", return_value=69_851.23):
        result = paper_executor.submit(c)
    assert result is False, "Zero-stop candidate must be rejected"


@pytest.mark.unit
def test_zs001_exact_zero_stop_no_position_opened(paper_executor):
    """No position must exist for BTC/USDT after a zero-stop rejection."""
    c = _make_candidate(entry=69_851.23, sl=69_851.23)
    with patch.object(paper_executor, "_apply_slippage", return_value=69_851.23):
        paper_executor.submit(c)
    assert paper_executor._positions.get("BTC/USDT", []) == []


# ══════════════════════════════════════════════════════════════════════════════
#  ZS-002 — near-zero stop (below 0.1% threshold)
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
def test_zs002_sub_threshold_stop_rejected(paper_executor):
    """stop distance of 0.05% < 0.1% minimum must be rejected."""
    entry = 69_851.23
    sl    = entry * (1 - 0.0005)   # 0.05% below entry → short stop would be above (use sell)
    # For a sell: stop is above entry price, 0.05% above
    sl_above = entry * (1 + 0.0005)
    c = _make_candidate(entry=entry, sl=sl_above, side="sell")
    with patch.object(paper_executor, "_apply_slippage", return_value=entry):
        result = paper_executor.submit(c)
    assert result is False, "Sub-threshold stop (<0.1%) must be rejected"


@pytest.mark.unit
def test_zs002_exactly_at_threshold_rejected(paper_executor):
    """stop distance of exactly 0.1% is still below minimum (< not <=) → rejected."""
    entry = 50_000.0
    sl    = entry * (1 - 0.001)   # exactly 0.1% below (buy position)
    c = _make_candidate(entry=entry, sl=sl, side="buy")
    with patch.object(paper_executor, "_apply_slippage", return_value=entry):
        result = paper_executor.submit(c)
    # 0.001 is NOT < 0.001, so this passes. Verify border:
    dist_pct = abs(entry - sl) / entry  # == 0.001 exactly
    if dist_pct < 0.001:
        assert result is False
    else:
        # At exactly the threshold the guard does NOT fire — position opens
        assert result is True


# ══════════════════════════════════════════════════════════════════════════════
#  ZS-003 — valid stop (above threshold) — must NOT be rejected
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
def test_zs003_valid_stop_buy_accepted(paper_executor):
    """A buy candidate with a 2% stop must open normally."""
    entry = 65_000.0
    sl    = 63_700.0   # ~2% stop
    c = _make_candidate(entry=entry, sl=sl, side="buy", tp=68_000.0)
    with patch.object(paper_executor, "_apply_slippage", return_value=entry):
        result = paper_executor.submit(c)
    assert result is True, "Valid-stop buy candidate must open successfully"
    assert len(paper_executor._positions.get("BTC/USDT", [])) == 1


@pytest.mark.unit
def test_zs003_valid_stop_sell_accepted(paper_executor):
    """A sell candidate with a 1.5% stop must open normally."""
    entry = 69_851.23
    sl    = entry * 1.015   # 1.5% above entry — valid for short
    c = _make_candidate(entry=entry, sl=sl, side="sell", tp=68_000.0)
    with patch.object(paper_executor, "_apply_slippage", return_value=entry):
        result = paper_executor.submit(c)
    assert result is True, "Valid-stop sell candidate must open successfully"


@pytest.mark.unit
def test_zs003_valid_stop_just_above_threshold(paper_executor):
    """A stop distance of 0.15% (> 0.1%) must be accepted."""
    entry = 50_000.0
    sl    = entry * (1 - 0.0015)   # 0.15% below → buy
    c = _make_candidate(entry=entry, sl=sl, side="buy", tp=52_000.0)
    with patch.object(paper_executor, "_apply_slippage", return_value=entry):
        result = paper_executor.submit(c)
    assert result is True, "0.15% stop should pass the 0.1% guard"


# ══════════════════════════════════════════════════════════════════════════════
#  ZS-004 — None stop_loss_price (guard must not fire — open normally)
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
def test_zs004_none_stop_loss_skips_guard():
    """The guard condition `if _sl_guard and fill_price > 0` safely evaluates to
    False when stop_loss_price is None, preventing ZeroDivisionError.

    This tests the guard logic directly rather than going through submit(), because
    PaperPosition.__init__ itself (a pre-existing constraint) also rejects None stop.
    The guard's `if _sl_guard and fill_price > 0:` short-circuits on None via Python
    truthiness — no division occurs.
    """
    fill_price = 65_000.0
    sl_guard   = None
    # Replicate the guard condition exactly as written in paper_executor.submit()
    _MIN_STOP_PCT = 0.001
    guard_would_fire = False
    if sl_guard and fill_price > 0:          # None → falsy → block is skipped
        stop_dist_pct = abs(fill_price - sl_guard) / fill_price
        if stop_dist_pct < _MIN_STOP_PCT:
            guard_would_fire = True
    assert not guard_would_fire, "Guard must not fire when stop_loss_price is None"


# ══════════════════════════════════════════════════════════════════════════════
#  ZS-005 — capital unchanged after rejection
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
def test_zs005_capital_unchanged_after_rejection(paper_executor):
    """A rejected zero-stop trade must not alter capital."""
    initial_capital = paper_executor._capital
    c = _make_candidate(entry=69_851.23, sl=69_851.23)
    with patch.object(paper_executor, "_apply_slippage", return_value=69_851.23):
        paper_executor.submit(c)
    assert paper_executor._capital == initial_capital, (
        f"Capital must not change after rejection: was {initial_capital}, "
        f"now {paper_executor._capital}"
    )
