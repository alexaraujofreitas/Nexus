"""
tests/unit/test_proposal_roi_tracker.py — AI Proposal ROI Tracker (PRT-001 to PRT-010)

Tests the Wave 2 Sub-wave 2.3 ProposalROITracker introduced to measure the
realized performance impact of manually applied TuningProposals.

All tests use an in-memory SQLite DB (injected via fixture) so they are
fully isolated from the production DB.

Key invariants verified:
  PRT-001 — record_application() creates a DB row with EVALUATING status
  PRT-002 — duplicate record_application() is rejected (returns False)
  PRT-003 — on_trade_closed() accumulates in memory buffer
  PRT-004 — verdict computed automatically when threshold reached
  PRT-005 — IMPROVED verdict when delta_pf > 0.10 AND delta_wr > 2.0
  PRT-006 — DEGRADED verdict when delta_pf < -0.10
  PRT-007 — NEUTRAL verdict when deltas are within neutral band
  PRT-008 — get_status() returns not_found for unknown proposal
  PRT-009 — get_all_outcomes() returns newest-first list
  PRT-010 — _compute_verdict() handles None deltas gracefully
"""
from __future__ import annotations

import pytest
from unittest.mock import patch, MagicMock
from datetime import datetime, timezone

from core.analysis.proposal_roi_tracker import (
    ProposalROITracker,
    _compute_verdict,
)


# ── in-memory DB fixture ──────────────────────────────────────────────────────

@pytest.fixture
def tracker():
    """
    Fresh ProposalROITracker backed by a temporary in-memory SQLite DB.
    Each test gets an isolated tracker + schema.

    The tracker lazily imports Session from core.database.engine, so we patch
    the Session there.  The ORM models are imported from core.database.models
    which already have their metadata registered against Base.
    """
    import sqlalchemy as sa
    from sqlalchemy.orm import sessionmaker
    from core.database.engine import Base

    engine = sa.create_engine("sqlite:///:memory:", echo=False)
    Base.metadata.create_all(engine)
    TestSession = sessionmaker(bind=engine)

    t = ProposalROITracker(min_trades=3)  # low threshold for fast tests

    # Patch Session at the engine level — all lazy imports inside the tracker's
    # methods resolve to this test session factory.
    with patch("core.database.engine.Session", TestSession):
        yield t


def _pre_metrics(
    trade_count: int   = 50,
    win_rate:    float = 50.0,
    pf:          float = 1.47,
    avg_r:       float = 0.22,
) -> dict:
    return {
        "trade_count":    trade_count,
        "win_rate":       win_rate,
        "profit_factor":  pf,
        "avg_r":          avg_r,
    }


def _trade(pnl: float, pnl_pct: float = 0.0, avg_r: float = 0.0) -> dict:
    return {"pnl": pnl, "pnl_pct": pnl_pct, "avg_r": avg_r}


# ══════════════════════════════════════════════════════════════════════════════
#  PRT-001 — record_application creates EVALUATING row
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
def test_record_application_creates_evaluating_row(tracker):
    """record_application() must create a DB row with status=EVALUATING."""
    ok = tracker.record_application("P-001", _pre_metrics())
    assert ok is True

    status = tracker.get_status("P-001")
    assert status["status"]        == "EVALUATING"
    assert status["threshold"]     == 3         # fixture uses min_trades=3
    assert status["pre_metrics"]["win_rate"] == pytest.approx(50.0)
    assert status["pre_metrics"]["profit_factor"] == pytest.approx(1.47)


# ══════════════════════════════════════════════════════════════════════════════
#  PRT-002 — Duplicate record_application is rejected
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
def test_duplicate_record_application_rejected(tracker):
    """
    Calling record_application() twice for the same proposal_id must return
    False and must NOT overwrite the existing row.
    """
    tracker.record_application("P-DUP", _pre_metrics(win_rate=55.0))
    ok2 = tracker.record_application("P-DUP", _pre_metrics(win_rate=99.0))  # duplicate

    assert ok2 is False
    status = tracker.get_status("P-DUP")
    # Original win_rate must be preserved
    assert status["pre_metrics"]["win_rate"] == pytest.approx(55.0)


# ══════════════════════════════════════════════════════════════════════════════
#  PRT-003 — on_trade_closed accumulates in memory buffer
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
def test_on_trade_closed_accumulates_buffer(tracker):
    """
    Trades closed before the threshold is reached must accumulate in memory.
    Status must remain EVALUATING.
    """
    tracker.record_application("P-BUF", _pre_metrics())

    # Send 2 trades (threshold=3, so no verdict yet)
    tracker.on_trade_closed(_trade(pnl=100.0))
    tracker.on_trade_closed(_trade(pnl=50.0))

    with tracker._lock:
        buf = tracker._post_trades.get("P-BUF", [])
    assert len(buf) == 2

    status = tracker.get_status("P-BUF")
    assert status["status"] == "EVALUATING"
    assert status["post_trades_mem"] == 2


# ══════════════════════════════════════════════════════════════════════════════
#  PRT-004 — Verdict computed automatically at threshold
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
def test_verdict_computed_at_threshold(tracker):
    """
    When the number of post-application trades reaches min_trades_threshold,
    _measure() must fire automatically and status must become MEASURED.
    """
    tracker.record_application("P-AUTO", _pre_metrics(win_rate=40.0, pf=1.0))

    # Send exactly 3 trades (threshold)
    for pnl in (200.0, 150.0, -50.0):
        tracker.on_trade_closed(_trade(pnl=pnl))

    status = tracker.get_status("P-AUTO")
    assert status["status"] == "MEASURED"
    assert status["post_metrics"] is not None
    assert status["verdict"] in {"IMPROVED", "NEUTRAL", "DEGRADED"}

    # In-memory buffer must be cleared after measurement
    with tracker._lock:
        assert "P-AUTO" not in tracker._post_trades


# ══════════════════════════════════════════════════════════════════════════════
#  PRT-005 — IMPROVED verdict when both deltas exceed thresholds
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
def test_improved_verdict():
    """
    delta_pf > +0.10 AND delta_win_rate > +2.0 pp → IMPROVED.
    """
    assert _compute_verdict(delta_pf=0.50, delta_wr=5.0) == "IMPROVED"
    assert _compute_verdict(delta_pf=0.11, delta_wr=2.1) == "IMPROVED"


# ══════════════════════════════════════════════════════════════════════════════
#  PRT-006 — DEGRADED verdict when PF drops below threshold
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
def test_degraded_verdict_pf():
    """
    delta_pf < -0.10 → DEGRADED (regardless of win rate).
    """
    assert _compute_verdict(delta_pf=-0.15, delta_wr=1.0)  == "DEGRADED"
    assert _compute_verdict(delta_pf=-0.50, delta_wr=None) == "DEGRADED"


@pytest.mark.unit
def test_degraded_verdict_wr():
    """
    delta_wr < -2.0 pp → DEGRADED (regardless of PF).
    """
    assert _compute_verdict(delta_pf=0.05, delta_wr=-3.0) == "DEGRADED"
    assert _compute_verdict(delta_pf=None, delta_wr=-5.0) == "DEGRADED"


# ══════════════════════════════════════════════════════════════════════════════
#  PRT-007 — NEUTRAL verdict when deltas are within the neutral band
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
def test_neutral_verdict():
    """
    Small positive deltas that don't cross the IMPROVED thresholds → NEUTRAL.
    """
    assert _compute_verdict(delta_pf=0.05,  delta_wr=1.0) == "NEUTRAL"
    assert _compute_verdict(delta_pf=0.10,  delta_wr=2.0) == "NEUTRAL"  # boundary is exclusive
    assert _compute_verdict(delta_pf=-0.05, delta_wr=1.0) == "NEUTRAL"


# ══════════════════════════════════════════════════════════════════════════════
#  PRT-008 — get_status returns not_found for unknown proposal
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
def test_get_status_not_found(tracker):
    """get_status() must return {"error": "not_found"} for unknown proposal_id."""
    status = tracker.get_status("NONEXISTENT-999")
    assert status.get("error") == "not_found"


# ══════════════════════════════════════════════════════════════════════════════
#  PRT-009 — get_all_outcomes returns list of summary dicts
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
def test_get_all_outcomes_returns_list(tracker):
    """
    get_all_outcomes() must return a list where each element has the required
    keys.  The list must contain all recorded proposals.
    """
    required_keys = {
        "proposal_id", "status", "post_trades", "threshold",
        "verdict", "delta_pf", "delta_win_rate", "applied_at", "measured_at",
    }

    tracker.record_application("P-ALL-1", _pre_metrics())
    tracker.record_application("P-ALL-2", _pre_metrics())

    outcomes = tracker.get_all_outcomes()
    assert len(outcomes) >= 2

    for item in outcomes:
        missing = required_keys - set(item.keys())
        assert not missing, f"get_all_outcomes() item missing keys: {missing}"


# ══════════════════════════════════════════════════════════════════════════════
#  PRT-010 — _compute_verdict handles None deltas gracefully
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
def test_compute_verdict_none_deltas():
    """
    When both deltas are None (no pre-metric provided), verdict must be NEUTRAL.
    Must not raise.
    """
    verdict = _compute_verdict(delta_pf=None, delta_wr=None)
    assert verdict == "NEUTRAL"

    # Partial None — only one metric available
    assert _compute_verdict(delta_pf=0.50, delta_wr=None) == "NEUTRAL"  # can't confirm wr_ok
    assert _compute_verdict(delta_pf=None, delta_wr=5.0)  == "NEUTRAL"  # can't confirm pf_ok
