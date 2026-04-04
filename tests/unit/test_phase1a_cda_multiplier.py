"""
tests/unit/test_phase1a_cda_multiplier.py — Phase 1A: CDA multiplier wiring tests
                                              (P1A-001 to P1A-013)

Phase 1A wired CrashDetectionAgent.position_size_multiplier into RiskGate so
that position sizes are automatically scaled down during elevated crash risk.

What these tests verify
────────────────────────
CDA module level:
  P1A-001  TIER_MULTIPLIERS is a module-level constant with correct 5-tier values
  P1A-002  get_position_size_multiplier() returns correct value for every tier
  P1A-003  get_position_size_multiplier() returns 1.0 when data is stale (fail-safe)
  P1A-004  get_crash_state() includes 'position_size_multiplier' key
  P1A-005  get_crash_state()['position_size_multiplier'] reflects the current tier

RiskGate integration:
  P1A-006  CDA multiplier 1.0 (NORMAL) — max_size and position unchanged
  P1A-007  CDA multiplier 0.65 (DEFENSIVE) — position size reduced proportionally
  P1A-008  CDA multiplier 0.35 (HIGH_ALERT) — position size reduced proportionally
  P1A-009  CDA multiplier 0.10 (EMERGENCY) — position size reduced proportionally
  P1A-010  CDA multiplier 0.00 (SYSTEMIC) — position size reduced to 0.0
  P1A-011  CDA singleton = None — multiplier defaults to 1.0 (fail-safe)
  P1A-012  CDA raises exception on get_position_size_multiplier() — defaults to 1.0
  P1A-013  CDA multiplier does not affect size when position already within limit

Note on DB side-effects
────────────────────────
validate() calls _persist_signal_log() on the APPROVED path.  The method is
wrapped in a broad try/except so a missing table never raises.  We pass
test_db to every RiskGate integration test so the full code path is exercised
without touching the real nexus_trader.db.
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch

import pytest

import core.agents.crash_detection_agent as _cda_mod
from core.agents.crash_detection_agent import (
    TIER_MULTIPLIERS,
    TIER_NORMAL,
    TIER_DEFENSIVE,
    TIER_HIGH_ALERT,
    TIER_EMERGENCY,
    TIER_SYSTEMIC,
)
from core.risk.risk_gate import RiskGate


# ══════════════════════════════════════════════════════════════════════════════
#  Helpers
# ══════════════════════════════════════════════════════════════════════════════

def _mock_cda(multiplier: float = 1.0, tier: str = TIER_NORMAL) -> MagicMock:
    """
    Build a MagicMock that impersonates a CrashDetectionAgent singleton.

    get_position_size_multiplier() returns *multiplier*.
    get_crash_state() returns a dict that includes the expected keys.
    """
    m = MagicMock()
    m.get_position_size_multiplier.return_value = multiplier
    m.get_crash_state.return_value = {
        "score":                    5.0 if multiplier < 1.0 else 0.0,
        "tier":                     tier,
        "stale":                    False,
        "position_size_multiplier": multiplier,
    }
    return m


def _gate_with_cda(monkeypatch, multiplier: float, tier: str = TIER_NORMAL) -> RiskGate:
    """
    Return a default RiskGate with the CDA singleton mocked to return *multiplier*.
    """
    monkeypatch.setattr(_cda_mod, "crash_detection_agent", _mock_cda(multiplier, tier))
    return RiskGate()


# ══════════════════════════════════════════════════════════════════════════════
#  P1A-001 — TIER_MULTIPLIERS is a module-level constant with correct values
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
def test_p1a001_tier_multipliers_module_level():
    """
    TIER_MULTIPLIERS must be importable as a module-level name (not buried
    inside a function or class) and must contain exactly 5 tiers with the
    correct multiplier for each.
    """
    assert isinstance(TIER_MULTIPLIERS, dict), "TIER_MULTIPLIERS must be a dict"

    expected = {
        TIER_NORMAL:     1.00,
        TIER_DEFENSIVE:  0.65,
        TIER_HIGH_ALERT: 0.35,
        TIER_EMERGENCY:  0.10,
        TIER_SYSTEMIC:   0.00,
    }
    assert len(TIER_MULTIPLIERS) == 5, (
        f"Expected 5 tiers, got {len(TIER_MULTIPLIERS)}: {list(TIER_MULTIPLIERS)}"
    )
    for tier, expected_mult in expected.items():
        actual = TIER_MULTIPLIERS[tier]
        assert actual == pytest.approx(expected_mult, abs=1e-6), (
            f"TIER_MULTIPLIERS[{tier!r}]: expected {expected_mult}, got {actual}"
        )


# ══════════════════════════════════════════════════════════════════════════════
#  P1A-002 — get_position_size_multiplier() per tier (actual CDA instance)
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
@pytest.mark.parametrize("tier,expected_mult", [
    (TIER_NORMAL,     1.00),
    (TIER_DEFENSIVE,  0.65),
    (TIER_HIGH_ALERT, 0.35),
    (TIER_EMERGENCY,  0.10),
    (TIER_SYSTEMIC,   0.00),
])
def test_p1a002_get_multiplier_per_tier(qt_app, tier, expected_mult):
    """
    get_position_size_multiplier() must return the correct scalar for every
    tier.  We inject the tier by setting _current_tier directly (no network
    calls required) and ensure _last_updated is fresh so is_stale is False.
    """
    from core.agents.crash_detection_agent import CrashDetectionAgent

    agent = CrashDetectionAgent()

    # Make data fresh (not stale) so the fail-safe branch is not taken
    agent._last_updated = datetime.now(timezone.utc)
    agent._current_tier = tier

    result = agent.get_position_size_multiplier()

    assert result == pytest.approx(expected_mult, abs=1e-6), (
        f"Tier {tier!r}: expected multiplier {expected_mult}, got {result}"
    )


# ══════════════════════════════════════════════════════════════════════════════
#  P1A-003 — get_position_size_multiplier() returns 1.0 when stale (fail-safe)
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
def test_p1a003_multiplier_returns_1_when_stale(qt_app):
    """
    If the agent has stale data (last updated too long ago), the multiplier
    must return 1.0 regardless of the current tier — fail-safe, no penalty.

    We simulate staleness by setting _last_updated to the distant past.
    """
    from core.agents.crash_detection_agent import CrashDetectionAgent

    agent = CrashDetectionAgent()
    # Force the agent into SYSTEMIC tier with stale data
    agent._last_updated = datetime.now(timezone.utc) - timedelta(hours=24)
    agent._current_tier = TIER_SYSTEMIC

    result = agent.get_position_size_multiplier()

    assert result == pytest.approx(1.0), (
        f"Expected 1.0 (stale fail-safe) for SYSTEMIC tier, got {result}"
    )


@pytest.mark.unit
def test_p1a003_multiplier_returns_1_when_never_updated(qt_app):
    """
    An agent that has never run (_last_updated=None) is also stale.
    Must return 1.0 to avoid blocking all trades at startup.
    """
    from core.agents.crash_detection_agent import CrashDetectionAgent

    agent = CrashDetectionAgent()
    assert agent._last_updated is None  # precondition: fresh agent, never run
    agent._current_tier = TIER_EMERGENCY

    result = agent.get_position_size_multiplier()

    assert result == pytest.approx(1.0), (
        f"Expected 1.0 (never-updated fail-safe) for EMERGENCY tier, got {result}"
    )


# ══════════════════════════════════════════════════════════════════════════════
#  P1A-004 — get_crash_state() includes 'position_size_multiplier' key
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
def test_p1a004_crash_state_has_multiplier_key(qt_app):
    """
    get_crash_state() must return a dict that includes 'position_size_multiplier'.
    This is required so callers can inspect the multiplier alongside other
    state without calling get_position_size_multiplier() separately.
    """
    from core.agents.crash_detection_agent import CrashDetectionAgent

    agent = CrashDetectionAgent()
    state = agent.get_crash_state()

    assert "position_size_multiplier" in state, (
        f"'position_size_multiplier' key missing from get_crash_state() dict. "
        f"Keys present: {list(state.keys())}"
    )


# ══════════════════════════════════════════════════════════════════════════════
#  P1A-005 — get_crash_state()['position_size_multiplier'] reflects current tier
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
@pytest.mark.parametrize("tier,expected_mult", [
    (TIER_NORMAL,     1.00),
    (TIER_DEFENSIVE,  0.65),
    (TIER_HIGH_ALERT, 0.35),
    (TIER_EMERGENCY,  0.10),
    (TIER_SYSTEMIC,   0.00),
])
def test_p1a005_crash_state_multiplier_matches_tier(qt_app, tier, expected_mult):
    """
    The 'position_size_multiplier' value in get_crash_state() must equal
    TIER_MULTIPLIERS[tier] for every tier.
    """
    from core.agents.crash_detection_agent import CrashDetectionAgent

    agent = CrashDetectionAgent()
    agent._current_tier = tier
    state = agent.get_crash_state()

    actual = state["position_size_multiplier"]
    assert actual == pytest.approx(expected_mult, abs=1e-6), (
        f"Tier {tier!r}: get_crash_state()['position_size_multiplier'] = {actual}, "
        f"expected {expected_mult}"
    )


# ══════════════════════════════════════════════════════════════════════════════
#  P1A-006 — RiskGate: CDA multiplier 1.0 → size unchanged
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
def test_p1a006_cda_multiplier_1_no_change(test_db, monkeypatch, make_candidate):
    """
    When the CDA singleton returns multiplier 1.0 (NORMAL tier), the
    max_position_capital_pct is effectively unchanged and a properly-sized
    position should not be reduced.

    Setup: 10,000 USDT capital, max_position_capital_pct=0.25 → max_size=2,500.
    Position: 100 USDT (well within limit).
    CDA multiplier: 1.0 → effective limit still 2,500. No reduction expected.
    """
    gate = _gate_with_cda(monkeypatch, multiplier=1.0, tier=TIER_NORMAL)
    candidate = make_candidate(size_usdt=100.0)

    result = gate.validate(
        candidate,
        open_positions=[],
        available_capital_usdt=10_000.0,
        portfolio_drawdown_pct=0.0,
    )

    assert result.approved is True
    assert result.position_size_usdt == pytest.approx(100.0, abs=0.01), (
        f"CDA mult=1.0: position size should be unchanged at 100.0, "
        f"got {result.position_size_usdt}"
    )


# ══════════════════════════════════════════════════════════════════════════════
#  P1A-007 — RiskGate: CDA multiplier 0.65 (DEFENSIVE) reduces cap
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
def test_p1a007_cda_defensive_reduces_cap(test_db, monkeypatch, make_candidate):
    """
    DEFENSIVE tier multiplier (0.65) reduces the effective capital cap from
    25% to 16.25%.

    Setup: 1,000 USDT capital, max_position_capital_pct=0.25
      Normal cap:    250 USDT (25% × 1000)
      Defensive cap: 162.5 USDT (25% × 0.65 × 1000)

    Request 300 USDT → should be capped at 162.5 USDT.
    """
    gate = _gate_with_cda(monkeypatch, multiplier=0.65, tier=TIER_DEFENSIVE)
    candidate = make_candidate(size_usdt=300.0)

    result = gate.validate(
        candidate,
        open_positions=[],
        available_capital_usdt=1_000.0,
        portfolio_drawdown_pct=0.0,
    )

    expected_cap = 1_000.0 * 0.25 * 0.65  # = 162.50
    assert result.position_size_usdt <= expected_cap + 0.01, (
        f"DEFENSIVE mult=0.65: expected size ≤ {expected_cap:.2f}, "
        f"got {result.position_size_usdt}"
    )
    assert result.position_size_usdt > 0.0, "Size must be positive for DEFENSIVE tier"


# ══════════════════════════════════════════════════════════════════════════════
#  P1A-008 — RiskGate: CDA multiplier 0.35 (HIGH_ALERT) reduces cap
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
def test_p1a008_cda_high_alert_reduces_cap(test_db, monkeypatch, make_candidate):
    """
    HIGH_ALERT tier multiplier (0.35) reduces the effective capital cap from
    25% to 8.75%.

    Setup: 1,000 USDT capital
      High-alert cap: 87.5 USDT (25% × 0.35 × 1000)

    Request 200 USDT → should be capped at 87.5 USDT.
    """
    gate = _gate_with_cda(monkeypatch, multiplier=0.35, tier=TIER_HIGH_ALERT)
    candidate = make_candidate(size_usdt=200.0)

    result = gate.validate(
        candidate,
        open_positions=[],
        available_capital_usdt=1_000.0,
        portfolio_drawdown_pct=0.0,
    )

    expected_cap = 1_000.0 * 0.25 * 0.35  # = 87.50
    assert result.position_size_usdt <= expected_cap + 0.01, (
        f"HIGH_ALERT mult=0.35: expected size ≤ {expected_cap:.2f}, "
        f"got {result.position_size_usdt}"
    )
    assert result.position_size_usdt > 0.0, "Size must be positive for HIGH_ALERT tier"


# ══════════════════════════════════════════════════════════════════════════════
#  P1A-009 — RiskGate: CDA multiplier 0.10 (EMERGENCY) reduces cap
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
def test_p1a009_cda_emergency_reduces_cap(test_db, monkeypatch, make_candidate):
    """
    EMERGENCY tier multiplier (0.10) reduces the effective capital cap from
    25% to 2.5%.

    Setup: 1,000 USDT capital
      Emergency cap: 25.0 USDT (25% × 0.10 × 1000)

    Request 100 USDT → should be capped at 25 USDT.
    """
    gate = _gate_with_cda(monkeypatch, multiplier=0.10, tier=TIER_EMERGENCY)
    candidate = make_candidate(size_usdt=100.0)

    result = gate.validate(
        candidate,
        open_positions=[],
        available_capital_usdt=1_000.0,
        portfolio_drawdown_pct=0.0,
    )

    expected_cap = 1_000.0 * 0.25 * 0.10  # = 25.00
    assert result.position_size_usdt <= expected_cap + 0.01, (
        f"EMERGENCY mult=0.10: expected size ≤ {expected_cap:.2f}, "
        f"got {result.position_size_usdt}"
    )


# ══════════════════════════════════════════════════════════════════════════════
#  P1A-010 — RiskGate: CDA multiplier 0.00 (SYSTEMIC) reduces position to 0
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
def test_p1a010_cda_systemic_reduces_to_zero(test_db, monkeypatch, make_candidate):
    """
    SYSTEMIC tier multiplier (0.00) sets max_size = 0, so position_size_usdt
    is reduced to 0.0.

    Semantics: RiskGate does not hard-reject (approved may still be True),
    but the position size is zero — the executor must handle this gracefully.
    This is intentional: allows downstream components to log/audit the
    blocked signal rather than silently swallowing it.
    """
    gate = _gate_with_cda(monkeypatch, multiplier=0.00, tier=TIER_SYSTEMIC)
    candidate = make_candidate(size_usdt=100.0)

    result = gate.validate(
        candidate,
        open_positions=[],
        available_capital_usdt=10_000.0,
        portfolio_drawdown_pct=0.0,
    )

    assert result.position_size_usdt == pytest.approx(0.0, abs=0.01), (
        f"SYSTEMIC mult=0.00: expected position_size_usdt ≈ 0.0, "
        f"got {result.position_size_usdt}"
    )


# ══════════════════════════════════════════════════════════════════════════════
#  P1A-011 — RiskGate: CDA singleton = None → multiplier defaults to 1.0
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
def test_p1a011_cda_none_singleton_fails_safe(test_db, monkeypatch, make_candidate):
    """
    When the CDA singleton is None (not yet initialised at startup),
    RiskGate must default the multiplier to 1.0 and NOT block or reduce
    position sizes due to this absence.

    This is critical for startup safety: CDA initialises asynchronously
    and must not stall the first scanner cycle.
    """
    monkeypatch.setattr(_cda_mod, "crash_detection_agent", None)
    gate = RiskGate()
    candidate = make_candidate(size_usdt=100.0)

    result = gate.validate(
        candidate,
        open_positions=[],
        available_capital_usdt=10_000.0,
        portfolio_drawdown_pct=0.0,
    )

    # A 100 USDT position with 10k capital should be approved with no size change
    assert result.approved is True
    assert result.position_size_usdt == pytest.approx(100.0, abs=0.01), (
        f"CDA singleton=None: position should be unchanged, "
        f"got {result.position_size_usdt}"
    )


# ══════════════════════════════════════════════════════════════════════════════
#  P1A-012 — RiskGate: CDA raises exception → defaults to 1.0 (fail-safe)
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
def test_p1a012_cda_exception_fails_safe(test_db, monkeypatch, make_candidate):
    """
    If get_position_size_multiplier() raises any exception (network error,
    import error, attribute error, etc.), RiskGate must catch it and default
    to multiplier=1.0 — no disruption to normal trading.
    """
    bad_cda = MagicMock()
    bad_cda.get_position_size_multiplier.side_effect = RuntimeError(
        "Simulated CDA failure"
    )
    monkeypatch.setattr(_cda_mod, "crash_detection_agent", bad_cda)

    gate = RiskGate()
    candidate = make_candidate(size_usdt=100.0)

    # Must not raise
    result = gate.validate(
        candidate,
        open_positions=[],
        available_capital_usdt=10_000.0,
        portfolio_drawdown_pct=0.0,
    )

    # Should behave as if multiplier=1.0 (no size reduction from CDA)
    assert result.approved is True
    assert result.position_size_usdt == pytest.approx(100.0, abs=0.01), (
        f"CDA exception: expected position_size_usdt unchanged at 100.0, "
        f"got {result.position_size_usdt}"
    )


# ══════════════════════════════════════════════════════════════════════════════
#  P1A-013 — CDA multiplier does not touch size when position already within cap
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
def test_p1a013_small_position_not_reduced_by_cda(test_db, monkeypatch, make_candidate):
    """
    If a position is already well within the reduced cap (after applying the
    CDA multiplier), it must not be altered.

    Setup: 10,000 USDT capital, CDA multiplier=0.65 (DEFENSIVE)
      Effective cap: 10,000 × 0.25 × 0.65 = 1,625 USDT
      Request 50 USDT → well under cap → must be untouched.

    This confirms CDA multiplier only fires a reduction when the position
    actually exceeds the *effective* cap, not unconditionally.
    """
    gate = _gate_with_cda(monkeypatch, multiplier=0.65, tier=TIER_DEFENSIVE)
    candidate = make_candidate(size_usdt=50.0)

    result = gate.validate(
        candidate,
        open_positions=[],
        available_capital_usdt=10_000.0,
        portfolio_drawdown_pct=0.0,
    )

    assert result.position_size_usdt == pytest.approx(50.0, abs=0.01), (
        f"Small position (50 USDT) should not be reduced by DEFENSIVE multiplier, "
        f"got {result.position_size_usdt}"
    )
    assert result.approved is True
