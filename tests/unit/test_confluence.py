"""
tests/unit/test_confluence.py — ConfluenceScorer tests (CS-001 to CS-008)

Testing strategy
----------------
ConfluenceScorer is tested with explicit ModelSignal lists.  The orchestrator
is patched with a stub that returns neutral/no-veto to keep tests deterministic.
MODEL_WEIGHTS from the module are used directly to construct expected scores.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch
from datetime import datetime

import pytest

from core.meta_decision.confluence_scorer import (
    ConfluenceScorer,
    MODEL_WEIGHTS,
    SCORE_THRESHOLD,
)
from core.meta_decision.order_candidate import ModelSignal, OrderCandidate


# ── helpers ──────────────────────────────────────────────────────────────────

def make_signal(
    model_name: str = "trend",
    direction:  str = "long",
    strength:   float = 0.80,
    symbol:     str = "BTC/USDT",
    entry_price: float = 65_000.0,
    stop_loss:   float = 63_700.0,
    take_profit: float = 67_600.0,
    atr_value:   float = 650.0,
    timeframe:   str = "1h",
    regime:      str = "TRENDING_UP",
) -> ModelSignal:
    return ModelSignal(
        symbol      = symbol,
        model_name  = model_name,
        direction   = direction,
        strength    = strength,
        entry_price = entry_price,
        stop_loss   = stop_loss,
        take_profit = take_profit,
        timeframe   = timeframe,
        regime      = regime,
        rationale   = f"{model_name} test signal",
        atr_value   = atr_value,
    )


def make_cs(threshold: float = SCORE_THRESHOLD,
            base_size: float = 40.0) -> ConfluenceScorer:
    """
    Return a ConfluenceScorer with the orchestrator silenced.
    Weights are left at defaults so MODEL_WEIGHTS can be used directly.
    """
    cs = ConfluenceScorer(threshold=threshold, base_size_usdt=base_size)
    cs._orchestrator = _make_neutral_orchestrator()
    return cs


def _make_neutral_orchestrator() -> MagicMock:
    """Orchestrator stub: no veto, no threshold adjustment, neutral signal."""
    mock_orch = MagicMock()
    mock_orch.is_veto_active.return_value = False
    mock_orch.get_threshold_adjustment.return_value = 0.0
    mock_sig = MagicMock()
    mock_sig.meta_signal  = 0.0
    mock_sig.meta_confidence = 0.0
    mock_sig.direction    = "neutral"
    mock_sig.effective_agent_count = 0
    mock_orch.get_signal.return_value = mock_sig
    return mock_orch


# ══════════════════════════════════════════════════════════════════════════════
#  CS-001 — Empty signal list → None
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
def test_cs001_empty_signals_returns_none():
    """score() must return None immediately for an empty signals list."""
    cs = make_cs()
    result = cs.score([], "BTC/USDT")
    assert result is None


# ══════════════════════════════════════════════════════════════════════════════
#  CS-002 — Score below threshold → None
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
def test_cs002_score_below_threshold_returns_none():
    """
    A single model with very low strength must produce a score below the
    threshold, causing score() to return None.
    """
    cs = make_cs(threshold=0.55)
    # Very weak signal: effective score ≤ strength * 1.0 = 0.05
    weak_sig = make_signal(model_name="trend", strength=0.05)
    result = cs.score([weak_sig], "BTC/USDT")
    assert result is None


@pytest.mark.unit
def test_cs002_score_at_threshold_is_allowed():
    """
    The gate condition is 'if score < threshold → None', so a score exactly
    equal to the threshold IS allowed through and must return a candidate.
    """
    cs = make_cs(threshold=0.60)
    sig = make_signal(model_name="trend", strength=0.60)
    result = cs.score([sig], "BTC/USDT")
    assert result is not None


# ══════════════════════════════════════════════════════════════════════════════
#  CS-003 — Score above threshold → OrderCandidate produced
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
def test_cs003_score_above_threshold_returns_candidate():
    """
    With a strong signal (strength=0.90) and default threshold=0.55,
    score() must return an OrderCandidate.
    """
    cs = make_cs()
    sig = make_signal(model_name="trend", strength=0.90)
    result = cs.score([sig], "BTC/USDT")
    assert result is not None
    assert isinstance(result, OrderCandidate)


@pytest.mark.unit
def test_cs003_candidate_has_correct_symbol():
    """Returned OrderCandidate symbol must match the symbol passed to score()."""
    cs = make_cs()
    result = cs.score([make_signal(model_name="trend", strength=0.90)], "ETH/USDT")
    assert result is not None
    assert result.symbol == "ETH/USDT"


# ══════════════════════════════════════════════════════════════════════════════
#  CS-004 — Direction voting: perfectly-tied signals are rejected
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
def test_cs004_tie_in_direction_votes_chooses_long():
    """
    When long and short signals are perfectly balanced (equal adaptive weights
    and equal strengths), the weighted-dominance check yields 0.0 which is below
    the min_direction_dominance threshold (0.30).  The scorer must return None —
    a perfectly conflicted setup should not generate a trade.

    NOTE: The old behaviour was 'ties go long'; this was replaced in the
    2026-03-14 IDSS architecture upgrade with the weighted-dominance gate.
    """
    cs = make_cs()
    sigs = [
        make_signal(model_name="trend",             direction="long",  strength=0.90),
        make_signal(model_name="momentum_breakout", direction="short", strength=0.90),
    ]
    result = cs.score(sigs, "BTC/USDT")
    # Perfectly tied → dominance = 0 < 0.30 threshold → rejected
    assert result is None


# ══════════════════════════════════════════════════════════════════════════════
#  CS-005 — Direction voting: short majority wins
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
def test_cs005_short_majority_returns_sell():
    """
    When more signals are short than long, the result side must be 'sell'.
    """
    cs = make_cs()
    sigs = [
        make_signal(model_name="trend",             direction="short", strength=0.90),
        make_signal(model_name="mean_reversion",    direction="short", strength=0.85),
        make_signal(model_name="momentum_breakout", direction="long",  strength=0.80),
    ]
    result = cs.score(sigs, "BTC/USDT")
    assert result is not None
    assert result.side == "sell"


@pytest.mark.unit
def test_cs005_models_fired_only_contains_majority_direction():
    """
    models_fired in the returned candidate must only list models that
    contributed to the winning direction.
    """
    cs = make_cs()
    sigs = [
        make_signal(model_name="trend",          direction="short", strength=0.90),
        make_signal(model_name="mean_reversion", direction="short", strength=0.85),
        make_signal(model_name="liquidity_sweep",direction="long",  strength=0.80),
    ]
    result = cs.score(sigs, "BTC/USDT")
    assert result is not None
    assert "trend"          in result.models_fired
    assert "mean_reversion" in result.models_fired
    assert "liquidity_sweep" not in result.models_fired


# ══════════════════════════════════════════════════════════════════════════════
#  CS-006 — Orchestrator veto → None
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
def test_cs006_veto_suppresses_candidate():
    """
    When the orchestrator's is_veto_active() returns True, score() must
    return None regardless of signal strength.
    """
    cs = make_cs()
    cs._orchestrator.is_veto_active.return_value = True

    result = cs.score([make_signal(model_name="trend", strength=0.99)], "BTC/USDT")
    assert result is None


@pytest.mark.unit
def test_cs006_no_veto_allows_candidate():
    """With veto=False (default stub), strong signal should produce a candidate."""
    cs = make_cs()
    cs._orchestrator.is_veto_active.return_value = False

    result = cs.score([make_signal(model_name="trend", strength=0.99)], "BTC/USDT")
    assert result is not None


# ══════════════════════════════════════════════════════════════════════════════
#  CS-007 — Score computation is correctly weighted
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
def test_cs007_single_model_score_equals_strength():
    """
    With exactly one model contributing, its weight normalizes to 1.0, so
    the final score must equal the model's own strength.
    """
    cs = make_cs(threshold=0.50)
    strength = 0.78
    result = cs.score(
        [make_signal(model_name="trend", strength=strength)], "BTC/USDT"
    )
    assert result is not None
    assert result.score == pytest.approx(strength, abs=1e-3)


@pytest.mark.unit
def test_cs007_two_model_weighted_score():
    """
    With 'trend' (w=0.35) and 'momentum_breakout' (w=0.25) both firing,
    the expected score is computed manually and compared.

    Both contribute to 'long' so direction is unambiguous.
    w_trend = 0.35, w_momentum = 0.25
    total   = 0.60
    score   = (0.35/0.60)*s_trend + (0.25/0.60)*s_momentum
    """
    cs = make_cs(threshold=0.50)
    w_t  = MODEL_WEIGHTS["trend"]              # 0.35
    w_m  = MODEL_WEIGHTS["momentum_breakout"]  # 0.25
    total = w_t + w_m

    s_t, s_m = 0.85, 0.75
    expected_score = (w_t / total) * s_t + (w_m / total) * s_m

    sigs = [
        make_signal(model_name="trend",             direction="long", strength=s_t),
        make_signal(model_name="momentum_breakout", direction="long", strength=s_m),
    ]
    result = cs.score(sigs, "BTC/USDT")

    assert result is not None
    assert result.score == pytest.approx(expected_score, abs=1e-3)


# ══════════════════════════════════════════════════════════════════════════════
#  CS-008 — Returned OrderCandidate has required structure
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
def test_cs008_candidate_has_all_required_fields():
    """
    Every field of OrderCandidate must be populated (non-None / non-zero for
    the price fields) when score() returns a candidate.
    """
    cs = make_cs()
    result = cs.score(
        [make_signal(model_name="trend", strength=0.90,
                     entry_price=65_000, stop_loss=63_700, take_profit=67_600)],
        "BTC/USDT",
    )
    assert result is not None
    assert result.symbol             == "BTC/USDT"
    assert result.side               in ("buy", "sell")
    assert result.entry_type         in ("limit", "market", "conditional_limit")
    assert result.entry_price        >  0.0
    assert result.stop_loss_price    >  0.0
    assert result.take_profit_price  >  0.0
    assert result.position_size_usdt >  0.0
    assert 0.0 < result.score        <= 1.0
    assert isinstance(result.models_fired, list)
    assert len(result.models_fired)  >= 1
    assert result.expiry             is not None
    assert isinstance(result.expiry, datetime)


@pytest.mark.unit
def test_cs008_candidate_score_matches_weighted_calculation():
    """
    The score on the returned OrderCandidate must match the manually
    computed weighted average of the contributing signals' strengths.
    """
    cs = make_cs(threshold=0.50)
    w = MODEL_WEIGHTS["trend"]   # only model contributing, normalises to 1.0
    strength = 0.82

    result = cs.score(
        [make_signal(model_name="trend", strength=strength)], "BTC/USDT"
    )

    assert result is not None
    assert result.score == pytest.approx(strength, abs=1e-3)


@pytest.mark.unit
def test_cs008_unknown_model_excluded_from_weighted_score():
    """
    A model whose name is NOT in MODEL_WEIGHTS must contribute 0.0 weight and
    therefore be excluded from the score.  The other known model drives the score.
    """
    cs = make_cs(threshold=0.50)
    # "phantom_model" is not in MODEL_WEIGHTS, so it has no weight
    # "trend" is the sole weighted model → score = strength_trend
    strength = 0.80
    sigs = [
        make_signal(model_name="trend",         direction="long", strength=strength),
        make_signal(model_name="phantom_model", direction="long", strength=0.99),
    ]
    result = cs.score(sigs, "BTC/USDT")

    assert result is not None
    assert result.score == pytest.approx(strength, abs=1e-3)


# ══════════════════════════════════════════════════════════════════════════════
#  CS-009 — RL ensemble weight gating by settings
# ══════════════════════════════════════════════════════════════════════════════
#
# Root cause of the overnight 0-candidate bug (Session 5 + Session 8):
#   - config.yaml had `rl.enabled: false`
#   - ConfluenceScorer.__init__ reads settings.get("rl.enabled", False)
#   - When False → _weights["rl_ensemble"] = 0.0
#   - RL signals fired correctly in signal_generator.py but contributed 0
# These tests ensure the gating logic is explicitly covered and cannot regress.

class TestRLWeightGating:
    """CS-009 — rl_ensemble weight must be 0.0 when rl.enabled=false, non-zero when true."""

    # settings is imported locally inside ConfluenceScorer.__init__() via
    # `from config.settings import settings` — patch the object in that module.
    _SETTINGS_PATH = "config.settings.settings"

    def _make_rl_signal(self) -> ModelSignal:
        return make_signal(model_name="rl_ensemble", direction="long", strength=0.90)

    def _mock_settings(self, rl_enabled: bool):
        mock = MagicMock()
        mock.get.side_effect = lambda key, default=None: (
            rl_enabled if key == "rl.enabled" else default
        )
        return mock

    def test_cs009_rl_weight_zero_when_disabled(self):
        """When rl.enabled=False, _weights['rl_ensemble'] must be set to 0.0."""
        with patch(self._SETTINGS_PATH, self._mock_settings(False)):
            cs = ConfluenceScorer()
            cs._orchestrator = _make_neutral_orchestrator()

        assert cs._weights.get("rl_ensemble") == 0.0, (
            "rl_ensemble weight must be 0.0 when rl.enabled=False. "
            "This was the root cause of the overnight 0-candidate bug — "
            "RL signals fired but contributed nothing to confluence scores."
        )

    def test_cs009_rl_weight_zero_at_code_level(self):
        """MODEL_WEIGHTS['rl_ensemble'] = 0.0 at code level (audit reconciliation).

        RL is intentionally disabled in MODEL_WEIGHTS until agents are trained
        on 50+ live trades with ≥45% WR.  Even when rl.enabled=True in config,
        the code-level weight prevails — the config toggle only prevents the
        ConfluenceScorer from *further* zeroing an already-zero weight.
        """
        with patch(self._SETTINGS_PATH, self._mock_settings(True)):
            cs = ConfluenceScorer()
            cs._orchestrator = _make_neutral_orchestrator()

        assert cs._weights.get("rl_ensemble") == pytest.approx(0.0, abs=1e-6), (
            "rl_ensemble weight must be 0.0 — RL is disabled at code level "
            "per audit reconciliation (untrained agents)"
        )

    def test_cs009_rl_signal_contributes_zero_regardless_of_config(self):
        """An rl_ensemble ModelSignal must contribute 0 — RL weight is 0.0 at code level."""
        with patch(self._SETTINGS_PATH, self._mock_settings(False)):
            cs_disabled = ConfluenceScorer(threshold=0.01)
            cs_disabled._orchestrator = _make_neutral_orchestrator()

        with patch(self._SETTINGS_PATH, self._mock_settings(True)):
            cs_enabled = ConfluenceScorer(threshold=0.01)
            cs_enabled._orchestrator = _make_neutral_orchestrator()

        rl_sig = self._make_rl_signal()
        result_disabled = cs_disabled.score([rl_sig], "BTC/USDT")
        result_enabled  = cs_enabled.score([rl_sig],  "BTC/USDT")

        # RL weight is 0.0 at code level — both configs should produce None
        # because an rl_ensemble signal alone with 0 weight cannot pass threshold.
        assert result_disabled is None or (
            result_disabled is not None and result_disabled.score == pytest.approx(0.0, abs=1e-6)
        ), "rl_ensemble signal alone should not produce a non-zero candidate (disabled)"
        assert result_enabled is None or (
            result_enabled is not None and result_enabled.score == pytest.approx(0.0, abs=1e-6)
        ), "rl_ensemble signal alone should not produce a non-zero candidate (RL weight=0.0 at code level)"

    def test_cs009_weight_dict_unchanged_by_constructor(self):
        """
        ConfluenceScorer must not mutate MODEL_WEIGHTS beyond what is
        already set at module level.  Since rl_ensemble=0.0 at code level,
        the constructor's copy-and-zero path should be a no-op.
        """
        original_rl = MODEL_WEIGHTS.get("rl_ensemble")
        with patch(self._SETTINGS_PATH, self._mock_settings(False)):
            ConfluenceScorer()

        # Global MODEL_WEIGHTS should not change from its code-level value
        assert MODEL_WEIGHTS.get("rl_ensemble") == original_rl, (
            "MODEL_WEIGHTS global dict must not be mutated by ConfluenceScorer — "
            f"expected {original_rl}, got {MODEL_WEIGHTS.get('rl_ensemble')}"
        )
