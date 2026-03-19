"""
tests/unit/test_riskgate.py — RiskGate validation tests (RG-001 to RG-010)

RiskGate is stateless: callers supply the portfolio state on every call.
All tests use the make_candidate fixture and a fresh RiskGate instance so
there is no shared state between tests.

Note on DB side-effects
───────────────────────
validate() calls _persist_signal_log() on the APPROVED path.  That method
is wrapped in a broad try/except, so a missing table never raises.  We
include test_db in every test so the write actually succeeds and we exercise
the full code path without touching the real nexus_trader.db.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from core.risk.risk_gate import (
    RiskGate,
    DEFAULT_MAX_POSITIONS,
    DEFAULT_MAX_DRAWDOWN_PCT,
    DEFAULT_MAX_SPREAD_PCT,
    DEFAULT_MIN_RISK_REWARD,
)
from core.meta_decision.order_candidate import OrderCandidate


# ── shared fixture: default gate ─────────────────────────────────────────────

@pytest.fixture
def gate(test_db):
    """
    RiskGate with default limits (matches production defaults):
      max_concurrent_positions   = 3
      max_portfolio_drawdown_pct = 15.0 %
      max_position_capital_pct   = 0.25  (25 %)
      max_spread_pct             = 0.30 %
      min_risk_reward            = 1.3
    test_db is injected so _persist_signal_log() writes to an in-memory DB
    rather than touching data/nexus_trader.db.
    """
    return RiskGate()


# ── helpers ───────────────────────────────────────────────────────────────────

def _pos(symbol: str = "BTC/USDT", side: str = "buy") -> dict:
    """Minimal open-position dict accepted by RiskGate."""
    return {"symbol": symbol, "side": side, "size_usdt": 100.0}


# ══════════════════════════════════════════════════════════════════════════════
#  RG-001 — Signal expiry
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
def test_rg001_expired_signal_rejected(gate, make_candidate):
    """
    A candidate whose expiry timestamp is in the past must be rejected
    before any other check runs.
    """
    candidate = make_candidate(expiry_seconds=-10)   # 10 seconds in the past

    result = gate.validate(
        candidate,
        open_positions=[],
        available_capital_usdt=10_000.0,
        portfolio_drawdown_pct=0.0,
    )

    assert result.approved is False
    assert result.rejection_reason is not None
    assert "expired" in result.rejection_reason.lower()


@pytest.mark.unit
def test_rg001_no_expiry_not_rejected_for_expiry(gate, make_candidate):
    """
    A candidate with expiry=None must not be rejected on expiry grounds.
    (It may still pass or fail other checks.)
    """
    candidate = make_candidate()
    candidate.expiry = None   # explicitly no expiry

    result = gate.validate(
        candidate,
        open_positions=[],
        available_capital_usdt=10_000.0,
        portfolio_drawdown_pct=0.0,
    )

    # Expiry is not the cause of any rejection
    if not result.approved:
        assert "expired" not in (result.rejection_reason or "").lower()


@pytest.mark.unit
def test_rg001_fresh_signal_not_expired(gate, make_candidate):
    """
    A candidate with a 1-hour expiry (the default) must not trigger expiry rejection.
    """
    candidate = make_candidate(expiry_seconds=3_600)   # 1 hour ahead

    result = gate.validate(
        candidate,
        open_positions=[],
        available_capital_usdt=10_000.0,
        portfolio_drawdown_pct=0.0,
    )

    assert result.rejection_reason != "Candidate expired before risk check"


# ══════════════════════════════════════════════════════════════════════════════
#  RG-002 — Duplicate position (already in symbol)
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
def test_rg002_duplicate_position_rejected(gate, make_candidate):
    """
    Multiple positions per symbol are allowed (up to 10).
    This test is now updated to verify max positions per symbol rejection.
    """
    candidate = make_candidate(symbol="BTC/USDT")

    # Create 10 open positions for BTC/USDT (at the max)
    open_positions = [_pos("BTC/USDT", "buy") for _ in range(10)]

    result = gate.validate(
        candidate,
        open_positions=open_positions,  # 10 positions already in BTC/USDT
        available_capital_usdt=10_000.0,
        portfolio_drawdown_pct=0.0,
    )

    assert result.approved is False
    assert "Max positions per symbol reached" in (result.rejection_reason or "")


@pytest.mark.unit
def test_rg002_different_symbol_not_blocked(gate, make_candidate):
    """
    An open position in a different symbol must not block a new candidate.
    """
    candidate = make_candidate(symbol="ETH/USDT")

    result = gate.validate(
        candidate,
        open_positions=[_pos("BTC/USDT")],   # ETH/USDT is free
        available_capital_usdt=10_000.0,
        portfolio_drawdown_pct=0.0,
    )

    # ETH/USDT is not already open — should not be rejected for duplicate
    assert "Already in open position for ETH/USDT" not in (result.rejection_reason or "")


# ══════════════════════════════════════════════════════════════════════════════
#  RG-003 — Max concurrent positions reached
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
def test_rg003_max_positions_reached_rejected(test_db, make_candidate):
    """
    When the number of open positions equals max_concurrent_positions,
    any new candidate must be rejected.

    We use fictional symbols (AAA, BBB, CCC) that are not in the
    CorrelationController's pre-computed matrix, so they default to 0.50
    correlation — safely below the 0.75 cap — ensuring only the
    max-positions check fires.
    """
    gate = RiskGate(max_concurrent_positions=2)
    candidate = make_candidate(symbol="AAA/USDT")

    result = gate.validate(
        candidate,
        open_positions=[_pos("BBB/USDT"), _pos("CCC/USDT")],   # 2 of 2 slots used
        available_capital_usdt=10_000.0,
        portfolio_drawdown_pct=0.0,
    )

    assert result.approved is False
    assert "Max concurrent positions reached" in (result.rejection_reason or "")


@pytest.mark.unit
def test_rg003_one_slot_free_not_rejected(test_db, make_candidate):
    """
    When there is still a free slot, the max-positions check must not block.
    """
    gate = RiskGate(max_concurrent_positions=3)
    candidate = make_candidate(symbol="SOL/USDT")

    result = gate.validate(
        candidate,
        open_positions=[_pos("BTC/USDT"), _pos("ETH/USDT")],   # 2 of 3 slots used
        available_capital_usdt=10_000.0,
        portfolio_drawdown_pct=0.0,
    )

    assert "Max concurrent positions reached" not in (result.rejection_reason or "")


# ══════════════════════════════════════════════════════════════════════════════
#  RG-004 — Portfolio drawdown limit
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
def test_rg004_drawdown_above_limit_rejected(gate, make_candidate):
    """
    When portfolio_drawdown_pct exceeds max_portfolio_drawdown_pct (15 %)
    the candidate must be rejected.
    """
    candidate = make_candidate()

    result = gate.validate(
        candidate,
        open_positions=[],
        available_capital_usdt=10_000.0,
        portfolio_drawdown_pct=20.0,   # 20 % > 15 % limit
    )

    assert result.approved is False
    assert "drawdown" in (result.rejection_reason or "").lower()


@pytest.mark.unit
def test_rg004_drawdown_below_limit_passes(gate, make_candidate):
    """
    When portfolio_drawdown_pct is safely below the limit it must not
    cause a rejection on drawdown grounds.
    """
    candidate = make_candidate()

    result = gate.validate(
        candidate,
        open_positions=[],
        available_capital_usdt=10_000.0,
        portfolio_drawdown_pct=5.0,   # 5 % < 15 % limit
    )

    assert "drawdown" not in (result.rejection_reason or "").lower()


# ══════════════════════════════════════════════════════════════════════════════
#  RG-005 — Capital allocation
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
def test_rg005_zero_capital_rejected(gate, make_candidate):
    """
    When available_capital_usdt is 0 the gate must reject with
    an 'Insufficient capital' reason.
    """
    candidate = make_candidate(size_usdt=100.0)

    result = gate.validate(
        candidate,
        open_positions=[],
        available_capital_usdt=0.0,
        portfolio_drawdown_pct=0.0,
    )

    assert result.approved is False
    assert "capital" in (result.rejection_reason or "").lower()


@pytest.mark.unit
def test_rg005_oversized_position_capped_not_rejected(gate, make_candidate):
    """
    When position_size_usdt exceeds 25 % of available capital, the gate
    REDUCES the size rather than rejecting the candidate outright.

    Default max_position_capital_pct = 0.25, so with 1000 USDT available
    the cap is 250 USDT.  A 500 USDT request should be trimmed to 250 USDT.
    """
    candidate = make_candidate(size_usdt=500.0)   # 50 % of 1000 → over 25 % cap

    result = gate.validate(
        candidate,
        open_positions=[],
        available_capital_usdt=1_000.0,
        portfolio_drawdown_pct=0.0,
    )

    # Should be approved, but size reduced to 250 USDT (25 % of 1000)
    assert result.approved is True
    assert result.position_size_usdt <= 250.0 + 0.01   # allow rounding tolerance
    assert result.position_size_usdt > 0.0


@pytest.mark.unit
def test_rg005_ample_capital_not_capped(gate, make_candidate):
    """
    A normally-sized position with plenty of capital must not be
    reduced or rejected on capital grounds.
    """
    candidate = make_candidate(size_usdt=100.0)

    result = gate.validate(
        candidate,
        open_positions=[],
        available_capital_usdt=10_000.0,
        portfolio_drawdown_pct=0.0,
    )

    # 100 USDT out of 10 000 is 1 % — well within the 25 % cap
    assert result.position_size_usdt == pytest.approx(100.0, abs=0.01)
    assert "capital" not in (result.rejection_reason or "").lower()


# ══════════════════════════════════════════════════════════════════════════════
#  RG-006 — Risk:reward ratio
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
def test_rg006_poor_rr_rejected(gate, make_candidate):
    """
    Validates the R:R sanity floor (1.0) in the new EV-based gate.

    The 2026-03-14 IDSS architecture upgrade replaced the old hard R:R
    threshold (1.3) with an Expected-Value gate.  The R:R floor was lowered
    to 1.0 as a minimum sanity check — a candidate with R:R < 1.0 (taking
    on more risk than potential reward) must still be rejected.

    Candidate: R:R = 0.77 (risk=1300, reward=1000 → ratio < 1.0).
    """
    candidate = make_candidate(
        entry_price  = 65_000.0,
        stop_loss    = 63_700.0,    # risk   = 1300
        take_profit  = 66_000.0,    # reward = 1000 → R:R ≈ 0.77 (below floor)
    )

    result = gate.validate(
        candidate,
        open_positions=[],
        available_capital_usdt=10_000.0,
        portfolio_drawdown_pct=0.0,
    )

    assert result.approved is False
    assert "R:R" in (result.rejection_reason or "") or "ratio" in (result.rejection_reason or "").lower()


@pytest.mark.unit
def test_rg006_acceptable_rr_passes(gate, make_candidate):
    """
    A candidate with R:R >= 1.3 must not be rejected on R:R grounds.

    Default make_candidate uses 2.0 R:R which comfortably exceeds 1.3.
    """
    candidate = make_candidate()   # default R:R = 2.0

    assert candidate.risk_reward_ratio >= 1.3, (
        f"Fixture has unexpected R:R {candidate.risk_reward_ratio}"
    )

    result = gate.validate(
        candidate,
        open_positions=[],
        available_capital_usdt=10_000.0,
        portfolio_drawdown_pct=0.0,
    )

    assert "R:R" not in (result.rejection_reason or "")


# ══════════════════════════════════════════════════════════════════════════════
#  RG-007 — All gates pass → APPROVED
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
def test_rg007_all_checks_pass_approved(gate, make_candidate):
    """
    A well-formed candidate with no risk violations must receive
    approved=True and rejection_reason=None.
    """
    candidate = make_candidate(
        symbol       = "BTC/USDT",
        side         = "buy",
        size_usdt    = 100.0,
        score        = 0.82,
    )

    result = gate.validate(
        candidate,
        open_positions=[],
        available_capital_usdt=10_000.0,
        portfolio_drawdown_pct=0.0,
    )

    assert result.approved is True
    assert result.rejection_reason is None


@pytest.mark.unit
def test_rg007_approved_candidate_unchanged_except_flags(gate, make_candidate):
    """
    Approval must not alter the candidate's price levels or score.
    Only approved=True is expected to change.
    """
    candidate = make_candidate(entry_price=65_000.0, score=0.85, size_usdt=100.0)
    original_entry = candidate.entry_price
    original_score = candidate.score

    result = gate.validate(
        candidate,
        open_positions=[],
        available_capital_usdt=10_000.0,
        portfolio_drawdown_pct=0.0,
    )

    assert result.entry_price == original_entry
    assert result.score       == original_score
    assert result.approved    is True


# ══════════════════════════════════════════════════════════════════════════════
#  RG-008 — Batch with mixed results
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
def test_rg008_batch_mixed_results(test_db, make_candidate):
    """
    validate_batch() with 3 candidates and max_concurrent=2.

    Processing order is descending score, so:
      Candidate A (score=0.92) → APPROVED  (slot 1 of 2)
      Candidate B (score=0.81) → APPROVED  (slot 2 of 2)
      Candidate C (score=0.70) → REJECTED  (max positions reached)
    """
    # Use fictional symbols to bypass the CorrelationController's
    # pre-computed high-correlation pairs (BTC↔ETH=0.85, ETH↔SOL=0.80).
    # Fictional pairs default to 0.50 correlation, safely below the 0.75 cap.
    gate = RiskGate(max_concurrent_positions=2)

    a = make_candidate(symbol="AAA/USDT",  score=0.92, size_usdt=100.0)
    b = make_candidate(symbol="BBB/USDT",  score=0.81, size_usdt=100.0)
    c = make_candidate(symbol="CCC/USDT",  score=0.70, size_usdt=100.0)

    approved, rejected = gate.validate_batch(
        [c, a, b],                        # deliberately mis-ordered
        open_positions=[],
        available_capital_usdt=10_000.0,
        portfolio_drawdown_pct=0.0,
    )

    assert len(approved) == 2,  f"Expected 2 approved, got {len(approved)}"
    assert len(rejected) == 1,  f"Expected 1 rejected, got {len(rejected)}"

    approved_symbols = {c.symbol for c in approved}
    assert "AAA/USDT" in approved_symbols
    assert "BBB/USDT" in approved_symbols
    assert rejected[0].symbol == "CCC/USDT"


@pytest.mark.unit
def test_rg008_batch_highest_score_processed_first(test_db, make_candidate):
    """
    When capacity is 1, the highest-scoring candidate gets the slot,
    not the one passed first in the list.
    """
    gate = RiskGate(max_concurrent_positions=1)

    low_score  = make_candidate(symbol="ETH/USDT", score=0.60, size_usdt=50.0)
    high_score = make_candidate(symbol="BTC/USDT", score=0.90, size_usdt=50.0)

    approved, rejected = gate.validate_batch(
        [low_score, high_score],           # low_score is first in list
        open_positions=[],
        available_capital_usdt=10_000.0,
        portfolio_drawdown_pct=0.0,
    )

    assert len(approved) == 1
    assert approved[0].symbol == "BTC/USDT",  (
        "Highest-scoring candidate should have been approved, not the first in the list"
    )


# ══════════════════════════════════════════════════════════════════════════════
#  RG-009 — Empty candidate list
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
def test_rg009_empty_batch_returns_empty_lists(gate):
    """
    validate_batch([]) must return ([], []) without raising.
    """
    approved, rejected = gate.validate_batch(
        [],
        open_positions=[],
        available_capital_usdt=10_000.0,
        portfolio_drawdown_pct=0.0,
    )

    assert approved == []
    assert rejected == []


# ══════════════════════════════════════════════════════════════════════════════
#  RG-010 — Boundary: exactly at drawdown limit uses >= (not >)
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
def test_rg010_drawdown_exactly_at_limit_rejected(test_db, make_candidate):
    """
    The drawdown check uses >= so a portfolio_drawdown_pct that equals
    max_portfolio_drawdown_pct exactly must still be rejected.

    This test documents the strict boundary: equal = blocked.
    """
    limit_pct = 15.0
    gate = RiskGate(max_portfolio_drawdown_pct=limit_pct)
    candidate = make_candidate()

    result = gate.validate(
        candidate,
        open_positions=[],
        available_capital_usdt=10_000.0,
        portfolio_drawdown_pct=limit_pct,   # exactly at the limit
    )

    assert result.approved is False, (
        f"Expected REJECTED at exactly {limit_pct} % drawdown "
        f"(>= check), but got APPROVED"
    )
    assert "drawdown" in (result.rejection_reason or "").lower()


@pytest.mark.unit
def test_rg010_one_basis_point_below_limit_not_blocked(test_db, make_candidate):
    """
    A drawdown of (limit - 0.01 %) must pass the drawdown check.
    Confirms the boundary is tight but fair.
    """
    limit_pct = 15.0
    gate = RiskGate(max_portfolio_drawdown_pct=limit_pct)
    candidate = make_candidate()

    result = gate.validate(
        candidate,
        open_positions=[],
        available_capital_usdt=10_000.0,
        portfolio_drawdown_pct=limit_pct - 0.01,
    )

    assert "drawdown" not in (result.rejection_reason or "").lower()


# ══════════════════════════════════════════════════════════════════════════════
#  Bonus — Spread filter
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
def test_spread_above_max_rejected(gate, make_candidate):
    """
    When the live spread exceeds max_spread_pct (0.30 %) the gate must reject.
    """
    candidate = make_candidate()

    result = gate.validate(
        candidate,
        open_positions=[],
        available_capital_usdt=10_000.0,
        portfolio_drawdown_pct=0.0,
        spread_pct=0.50,   # 0.50 % > 0.30 % limit
    )

    assert result.approved is False
    assert "spread" in (result.rejection_reason or "").lower() or "Spread" in (result.rejection_reason or "")


@pytest.mark.unit
def test_spread_none_skips_check(gate, make_candidate):
    """
    When spread_pct=None (no ticker available) the spread check must be skipped
    and must not cause a rejection.
    """
    candidate = make_candidate()

    result = gate.validate(
        candidate,
        open_positions=[],
        available_capital_usdt=10_000.0,
        portfolio_drawdown_pct=0.0,
        spread_pct=None,
    )

    assert "spread" not in (result.rejection_reason or "").lower()


# ══════════════════════════════════════════════════════════════════════════════
#  Bonus — max_capital_usdt hard cap
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
def test_max_capital_usdt_caps_position_size(test_db, make_candidate):
    """
    When max_capital_usdt is set, no single position can exceed it,
    regardless of available capital.
    """
    gate = RiskGate(max_capital_usdt=50.0)
    candidate = make_candidate(size_usdt=200.0)

    result = gate.validate(
        candidate,
        open_positions=[],
        available_capital_usdt=10_000.0,
        portfolio_drawdown_pct=0.0,
    )

    assert result.approved is True
    assert result.position_size_usdt <= 50.0 + 0.01
