"""
tests/integration/test_pipeline.py — End-to-end IDSS pipeline (PIPE-001 to PIPE-004)

Integration tests verify that the three core components hand off correctly:

  ModelSignals → ConfluenceScorer → OrderCandidate
               → RiskGate.validate → approved/rejected
               → PaperExecutor.submit → position opened/skipped

No external services are required.  The orchestrator is mocked, and test_db
(from conftest) provides an isolated in-memory SQLite.

These tests are marked @pytest.mark.integration.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from core.meta_decision.confluence_scorer import ConfluenceScorer, SCORE_THRESHOLD
from core.meta_decision.order_candidate import ModelSignal, OrderCandidate
from core.risk.risk_gate import RiskGate


# ── helpers ──────────────────────────────────────────────────────────────────

def make_signal(
    model_name: str,
    direction:  str  = "long",
    strength:   float = 0.85,
    symbol:     str  = "BTC/USDT",
    entry:      float = 65_000.0,
    sl:         float = 63_700.0,
    tp:         float = 68_000.0,
) -> ModelSignal:
    return ModelSignal(
        symbol      = symbol,
        model_name  = model_name,
        direction   = direction,
        strength    = strength,
        entry_price = entry,
        stop_loss   = sl,
        take_profit = tp,
        timeframe   = "1h",
        regime      = "TRENDING_UP",
        rationale   = f"{model_name} integration test signal",
        atr_value   = 650.0,
    )


def make_neutral_orchestrator() -> MagicMock:
    mock_orch = MagicMock()
    mock_orch.is_veto_active.return_value = False
    mock_orch.get_threshold_adjustment.return_value = 0.0
    mock_sig = MagicMock()
    mock_sig.meta_signal       = 0.0
    mock_sig.meta_confidence   = 0.0
    mock_sig.direction         = "neutral"
    mock_sig.effective_agent_count = 0
    mock_orch.get_signal.return_value = mock_sig
    return mock_orch


@pytest.fixture
def cs():
    """ConfluenceScorer with neutral orchestrator stub."""
    scorer = ConfluenceScorer(threshold=SCORE_THRESHOLD)
    scorer._orchestrator = make_neutral_orchestrator()
    return scorer


@pytest.fixture
def gate():
    """RiskGate with default limits."""
    return RiskGate(
        max_concurrent_positions   = 3,
        max_portfolio_drawdown_pct = 15.0,
        max_position_capital_pct   = 0.50,
        max_spread_pct             = 0.30,
        min_risk_reward            = 1.3,
    )


# ══════════════════════════════════════════════════════════════════════════════
#  PIPE-001 — Happy path: signals → confluence → risk gate → position opens
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.integration
def test_pipe001_full_pipeline_happy_path(cs, gate, paper_executor):
    """
    Two strong signals → ConfluenceScorer produces a candidate above threshold
    → RiskGate approves it → PaperExecutor opens a position.

    This test exercises the handoff between all three components.
    """
    # ── Step 1: strong signals from two models ─────────────────
    # Use AAA/USDT (unknown to correlation controller → no corr blocking)
    signals = [
        make_signal("trend",             direction="long", strength=0.88, symbol="AAA/USDT",
                    entry=1_000.0, sl=980.0, tp=1_040.0),
        make_signal("momentum_breakout", direction="long", strength=0.82, symbol="AAA/USDT",
                    entry=1_000.0, sl=980.0, tp=1_040.0),
    ]

    # ── Step 2: ConfluenceScorer ────────────────────────────────
    candidate = cs.score(signals, "AAA/USDT")

    assert candidate is not None, (
        "ConfluenceScorer failed to produce a candidate from strong signals"
    )
    assert candidate.symbol == "AAA/USDT"
    assert candidate.score  >= SCORE_THRESHOLD

    # ── Step 3: RiskGate ───────────────────────────────────────
    # Use 100_000 to match PaperExecutor's starting capital (which ConfluenceScorer
    # reads for risk-based sizing — position sizes are computed from 100k).
    candidate.approved = False   # ensure we're testing validation, not preset flag
    result = gate.validate(
        candidate,
        open_positions          = [],
        available_capital_usdt  = 100_000.0,
        portfolio_drawdown_pct  = 0.0,
    )

    assert result.approved, (
        f"RiskGate unexpectedly rejected: {result.rejection_reason}"
    )

    # ── Step 4: PaperExecutor ──────────────────────────────────
    opened = paper_executor.submit(result)

    assert opened is True, "PaperExecutor failed to open the approved position"
    assert "AAA/USDT" in paper_executor._positions


# ══════════════════════════════════════════════════════════════════════════════
#  PIPE-002 — Risk gate blocks a duplicate-position entry
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.integration
def test_pipe002_risk_gate_blocks_duplicate_position(cs, gate, paper_executor):
    """
    Session 11 introduced condition-based dedup:
      - RiskGate allows multiple positions per symbol (up to max_positions_per_symbol=10)
      - PaperExecutor blocks SAME-CONDITION entries (same side + models_fired + regime)

    This test verifies BOTH layers:
    1. RiskGate APPROVES a second candidate with a different signal condition (different
       models_fired) — multi-position is allowed by design.
    2. PaperExecutor.submit() REJECTS a second candidate with the EXACT SAME condition
       fingerprint (side + models_fired + regime) as an already-open position.
    """
    symbol = "BBB/USDT"

    # Open an initial position directly — models_fired=["trend","momentum_breakout"]
    from tests.unit.test_execution import make_candidate as make_exec_cand
    first_candidate = make_exec_cand(symbol=symbol, entry=500.0, sl=490.0, tp=520.0)
    # first_candidate: side=buy, models_fired=["trend","momentum_breakout"], regime=TRENDING_UP
    paper_executor.submit(first_candidate)
    assert symbol in paper_executor._positions

    # ── Part 1: RiskGate allows a DIFFERENT condition on same symbol ──────────
    signals = [
        make_signal("trend",          direction="long", strength=0.90, symbol=symbol,
                    entry=500.0, sl=490.0, tp=520.0),
        make_signal("mean_reversion", direction="long", strength=0.85, symbol=symbol,
                    entry=500.0, sl=490.0, tp=520.0),
    ]
    candidate_diff_condition = cs.score(signals, symbol)
    assert candidate_diff_condition is not None

    open_positions = paper_executor.get_open_positions()
    result_diff = gate.validate(
        candidate_diff_condition,
        open_positions         = open_positions,
        available_capital_usdt = 100_000.0,   # match PaperExecutor starting capital
        portfolio_drawdown_pct = 0.0,
    )
    # Different models_fired → different condition → RiskGate should approve
    assert result_diff.approved, (
        f"RiskGate should allow a different condition on the same symbol, "
        f"got rejection: {result_diff.rejection_reason!r}"
    )

    # ── Part 2: PaperExecutor rejects an identical condition ─────────────────
    # Build a candidate with the EXACT same fingerprint as the open position
    # (side=buy, models_fired=["trend","momentum_breakout"], regime=TRENDING_UP)
    duplicate_candidate = make_exec_cand(symbol=symbol, entry=505.0, sl=492.0, tp=525.0)
    duplicate_candidate.approved = True
    submit_result = paper_executor.submit(duplicate_candidate)
    assert submit_result is False, (
        "PaperExecutor must reject a candidate whose (side, models_fired, regime) "
        "fingerprint exactly matches an existing open position"
    )


# ══════════════════════════════════════════════════════════════════════════════
#  PIPE-003 — Weak signals → confluence threshold not met → no trade
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.integration
def test_pipe003_weak_signals_produce_no_candidate(cs, gate, paper_executor):
    """
    When all signals have very low strength, ConfluenceScorer returns None
    and no order reaches the risk gate or executor.
    """
    signals = [
        make_signal("trend",          direction="long", strength=0.10, symbol="CCC/USDT",
                    entry=200.0, sl=195.0, tp=210.0),
        make_signal("mean_reversion", direction="long", strength=0.08, symbol="CCC/USDT",
                    entry=200.0, sl=195.0, tp=210.0),
    ]

    candidate = cs.score(signals, "CCC/USDT")

    assert candidate is None, (
        f"Expected None from weak signals, got score={getattr(candidate, 'score', 'N/A')}"
    )

    # No position should have been opened
    assert "CCC/USDT" not in paper_executor._positions


# ══════════════════════════════════════════════════════════════════════════════
#  PIPE-004 — Stop-loss fires after pipeline opens position → capital updated
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.integration
def test_pipe004_stop_loss_closes_position_and_updates_capital(cs, gate, paper_executor):
    """
    Full lifecycle test:
    1. Pipeline opens a position for DDD/USDT via confluence + risk gate.
    2. A tick price below the stop-loss is delivered.
    3. PaperExecutor auto-closes the position with reason 'stop_loss'.
    4. _capital is updated (P&L applied).
    5. No open positions remain for DDD/USDT.
    """
    symbol   = "DDD/USDT"
    entry    = 100.0
    sl_price = 95.0
    tp_price = 110.0

    # ── Step 1: open via pipeline ──────────────────────────────
    signals = [
        make_signal("trend",          symbol=symbol, entry=entry,
                    sl=sl_price, tp=tp_price, strength=0.90),
        make_signal("momentum_breakout", symbol=symbol, entry=entry,
                    sl=sl_price, tp=tp_price, strength=0.85),
    ]

    candidate = cs.score(signals, symbol)
    assert candidate is not None, "Pipeline produced no candidate"

    candidate = gate.validate(
        candidate,
        open_positions         = [],
        available_capital_usdt = 100_000.0,   # match PaperExecutor starting capital
        portfolio_drawdown_pct = 0.0,
    )
    assert candidate.approved, (
        f"RiskGate rejected unexpectedly: {candidate.rejection_reason}"
    )

    opened = paper_executor.submit(candidate)
    assert opened, "PaperExecutor failed to open position"
    assert symbol in paper_executor._positions

    capital_before_close = paper_executor._capital
    initial_closed_count = len(paper_executor._closed_trades)

    # ── Step 2: deliver a tick well below the stop-loss ────────
    paper_executor.on_tick(symbol, 80.0)   # 80 < 95 SL → triggers stop_loss

    # ── Step 3: verify closure ─────────────────────────────────
    assert symbol not in paper_executor._positions, (
        f"Position still open after SL hit: {paper_executor._positions}"
    )

    assert len(paper_executor._closed_trades) == initial_closed_count + 1
    closed_trade = paper_executor._closed_trades[-1]
    assert closed_trade["exit_reason"] == "stop_loss"
    assert closed_trade["symbol"] == symbol

    # ── Step 4: capital changed (P&L applied) ─────────────────
    # Stop-loss at a loss → capital should decrease
    assert paper_executor._capital != capital_before_close, (
        "Capital unchanged after stop-loss close — P&L not applied"
    )
    # A losing stop-loss trade should reduce capital
    assert paper_executor._capital < capital_before_close, (
        f"Expected capital to decrease on stop-loss; "
        f"before={capital_before_close:.2f}, after={paper_executor._capital:.2f}"
    )
