"""
tests/unit/test_regime.py — EnsembleRegimeClassifier tests (RC-001 to RC-013)

Testing strategy
----------------
The ensemble logic is tested in pure isolation by monkeypatching the two
sub-classifiers (_rule_clf and _hmm_clf) with MagicMocks so that each
branch of the ensemble decision tree is exercised deterministically.

Tests DO NOT test RegimeClassifier or HMMClassifier internals — only the
combination logic in EnsembleRegimeClassifier.classify().
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
import pytest

from core.regime.ensemble_regime_classifier import EnsembleRegimeClassifier


# ── helpers ──────────────────────────────────────────────────────────────────

def make_clf(rule_weight: float = 0.65, hmm_weight: float = 0.35) -> EnsembleRegimeClassifier:
    """Return a fresh EnsembleRegimeClassifier with sub-classifiers replaced by mocks."""
    clf = EnsembleRegimeClassifier(rule_weight=rule_weight, hmm_weight=hmm_weight)
    clf._rule_clf = MagicMock()
    clf._hmm_clf  = MagicMock()
    return clf


def _set_mocks(clf, rule_regime, rule_conf, hmm_regime, hmm_conf,
               rule_features=None):
    """Wire mock return values for a single classify() call."""
    clf._rule_clf.classify.return_value = (rule_regime, rule_conf, rule_features or {})
    clf._hmm_clf.classify.return_value  = (hmm_regime, hmm_conf)


def dummy_df(n: int = 50) -> pd.DataFrame:
    """Minimal DataFrame accepted by sub-classifiers (length ≥ 30)."""
    return pd.DataFrame({
        "close":  np.linspace(60_000, 65_000, n),
        "volume": np.ones(n) * 100.0,
    })


# ══════════════════════════════════════════════════════════════════════════════
#  RC-001 — HMM returns 'unknown' → rule_only path
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
def test_rc001_hmm_unknown_returns_rule_result():
    """
    When HMM returns 'unknown', the ensemble must use the rule-based result
    unchanged (same regime and same confidence).
    """
    clf = make_clf()
    _set_mocks(clf, rule_regime="bull_trend", rule_conf=0.72,
               hmm_regime="unknown", hmm_conf=0.0)

    regime, conf, features = clf.classify(dummy_df())

    assert regime == "bull_trend"
    assert conf == pytest.approx(0.72)
    assert features["ensemble_method"] == "rule_only"


@pytest.mark.unit
def test_rc001_hmm_unknown_features_contain_both_regimes():
    """
    Even in rule_only mode the features dict must include both rule and HMM
    results so Signal Explorer has complete provenance.
    """
    clf = make_clf()
    _set_mocks(clf, rule_regime="ranging", rule_conf=0.55,
               hmm_regime="unknown", hmm_conf=0.0)

    _, _, features = clf.classify(dummy_df())

    assert features["rule_regime"]    == "ranging"
    assert features["hmm_regime"]     == "unknown"
    assert "rule_confidence" in features
    assert "hmm_confidence"  in features


# ══════════════════════════════════════════════════════════════════════════════
#  RC-002 — Both agree → confidence boosted
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
def test_rc002_agreement_boosts_confidence():
    """
    When both classifiers agree on the same regime, the ensemble must boost
    confidence by ×1.15 (capped at 1.0).

    avg_conf = (0.70 + 0.60) / 2 = 0.65
    boosted  = min(0.65 × 1.15, 1.0) = 0.7475
    """
    clf = make_clf()
    _set_mocks(clf, rule_regime="bull_trend", rule_conf=0.70,
               hmm_regime="bull_trend", hmm_conf=0.60)

    regime, conf, features = clf.classify(dummy_df())

    assert regime == "bull_trend"
    assert conf == pytest.approx(0.7475, abs=1e-4)
    assert features["ensemble_method"] == "agreement"


@pytest.mark.unit
def test_rc002_agreement_confidence_capped_at_one():
    """
    Confidence must never exceed 1.0, even when both classifiers report
    very high individual confidences.
    """
    clf = make_clf()
    _set_mocks(clf, rule_regime="bull_trend", rule_conf=0.97,
               hmm_regime="bull_trend", hmm_conf=0.95)

    _, conf, _ = clf.classify(dummy_df())

    assert conf <= 1.0, f"Confidence exceeded 1.0: {conf}"


# ══════════════════════════════════════════════════════════════════════════════
#  RC-003 — Same family, different regime → family_agreement
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
def test_rc003_same_family_uses_rule_regime():
    """
    When classifiers disagree on exact regime but agree on family (e.g. both
    bull_family), the ensemble uses the rule-based regime and averages confidence.

    Note: the family sets use "trend_bull"/"trend_bear" (not "bull_trend"/"bear_trend")
    so tests must use these exact strings to trigger family-based logic.
    """
    clf = make_clf()
    # "trend_bull" and "recovery" are both in _BULL_FAMILY
    _set_mocks(clf, rule_regime="trend_bull", rule_conf=0.70,
               hmm_regime="recovery", hmm_conf=0.60)

    regime, conf, features = clf.classify(dummy_df())

    assert regime == "trend_bull"
    assert conf == pytest.approx(0.65, abs=1e-4)   # (0.70 + 0.60) / 2
    assert features["ensemble_method"] == "family_agreement"


@pytest.mark.unit
def test_rc003_bear_family_same_family():
    """
    Family agreement works for bear_family members too
    (trend_bear and distribution are both bear_family).
    """
    clf = make_clf()
    _set_mocks(clf, rule_regime="trend_bear", rule_conf=0.80,
               hmm_regime="distribution", hmm_conf=0.60)

    regime, conf, features = clf.classify(dummy_df())

    assert regime == "trend_bear"
    assert features["ensemble_method"] == "family_agreement"


# ══════════════════════════════════════════════════════════════════════════════
#  RC-004 — Conflicting directions (bull vs bear) → uncertain
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
def test_rc004_bull_vs_bear_returns_uncertain():
    """
    When rule is bull_family and HMM is bear_family (opposite directions),
    the ensemble must return 'uncertain' with fixed confidence 0.30.

    Uses "trend_bull"/"trend_bear" which ARE in their respective family sets.
    """
    clf = make_clf()
    _set_mocks(clf, rule_regime="trend_bull", rule_conf=0.80,
               hmm_regime="trend_bear", hmm_conf=0.75)

    regime, conf, features = clf.classify(dummy_df())

    assert regime == "uncertain"
    assert conf == pytest.approx(0.30)
    assert features["ensemble_method"] == "conflict_direction"


@pytest.mark.unit
def test_rc004_bull_vs_range_also_conflict_direction():
    """
    If rule is bull_family but HMM is range_family (not bull), the code
    treats it as a directional conflict because hmm_is_bullish != rule_is_bullish.
    """
    clf = make_clf()
    # "trend_bull" is bull_family; "ranging" is range_family
    _set_mocks(clf, rule_regime="trend_bull", rule_conf=0.70,
               hmm_regime="ranging", hmm_conf=0.60)

    regime, conf, features = clf.classify(dummy_df())

    assert regime == "uncertain"
    assert conf == pytest.approx(0.30)
    assert features["ensemble_method"] == "conflict_direction"


# ══════════════════════════════════════════════════════════════════════════════
#  RC-005 — Different non-bull families → penalized conflict
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
def test_rc005_different_non_bull_families_penalizes_confidence():
    """
    When both regimes are non-bull but belong to different families (e.g.
    range_family vs vol_family), the ensemble uses the rule-based regime but
    penalizes confidence by ×0.85.
    """
    clf = make_clf()
    # ranging = range_family, high_volatility = vol_family
    _set_mocks(clf, rule_regime="ranging", rule_conf=0.70,
               hmm_regime="high_volatility", hmm_conf=0.60)

    regime, conf, features = clf.classify(dummy_df())

    assert regime == "ranging"
    assert conf == pytest.approx(0.70 * 0.85, abs=1e-4)
    assert features["ensemble_method"] == "conflict"


# ══════════════════════════════════════════════════════════════════════════════
#  RC-006 — Features dict always present
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
def test_rc006_features_dict_has_required_keys():
    """
    The features dict returned by classify() must always contain all five
    keys regardless of which ensemble branch was taken.
    """
    clf = make_clf()
    _set_mocks(clf, rule_regime="bull_trend", rule_conf=0.70,
               hmm_regime="bull_trend", hmm_conf=0.60)

    _, _, features = clf.classify(dummy_df())

    for key in ("rule_regime", "rule_confidence", "hmm_regime",
                "hmm_confidence", "ensemble_method"):
        assert key in features, f"Missing key: {key!r}"


@pytest.mark.unit
def test_rc006_features_rule_only_has_required_keys():
    """Features dict must be complete even in the rule_only branch."""
    clf = make_clf()
    _set_mocks(clf, rule_regime="ranging", rule_conf=0.55,
               hmm_regime="unknown", hmm_conf=0.0)

    _, _, features = clf.classify(dummy_df())

    for key in ("rule_regime", "rule_confidence", "hmm_regime",
                "hmm_confidence", "ensemble_method"):
        assert key in features, f"Missing key in rule_only branch: {key!r}"


# ══════════════════════════════════════════════════════════════════════════════
#  RC-007 — Unknown regime family (None) → conflict path
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
def test_rc007_both_unknown_family_uses_conflict_path():
    """
    If both regimes are not in any known family (None), rule_is_bullish and
    hmm_is_bullish are both False → not a directional conflict → falls through
    to the 'conflict' branch (penalized confidence).
    """
    clf = make_clf()
    _set_mocks(clf, rule_regime="unknown_regime_x", rule_conf=0.50,
               hmm_regime="another_unknown_regime", hmm_conf=0.40)

    regime, conf, features = clf.classify(dummy_df())

    # Neither is bull_family so we won't get conflict_direction;
    # different/None families → conflict
    assert regime == "unknown_regime_x"
    assert features["ensemble_method"] == "conflict"


# ══════════════════════════════════════════════════════════════════════════════
#  RC-008 — Weight normalization
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
def test_rc008_default_weights_normalized():
    """
    Default rule_weight=0.65, hmm_weight=0.35 should normalize to
    rule_weight≈0.65, hmm_weight≈0.35 (already normalized since they sum to 1).
    """
    clf = EnsembleRegimeClassifier(rule_weight=0.65, hmm_weight=0.35)

    assert clf._rule_weight == pytest.approx(0.65, abs=1e-6)
    assert clf._hmm_weight  == pytest.approx(0.35, abs=1e-6)


@pytest.mark.unit
def test_rc008_custom_weights_normalized():
    """
    Non-normalized custom weights (e.g. 3:1) must be scaled so they sum to 1.
    """
    clf = EnsembleRegimeClassifier(rule_weight=3.0, hmm_weight=1.0)

    assert clf._rule_weight == pytest.approx(0.75, abs=1e-6)
    assert clf._hmm_weight  == pytest.approx(0.25, abs=1e-6)
    assert clf._rule_weight + clf._hmm_weight == pytest.approx(1.0)


# ══════════════════════════════════════════════════════════════════════════════
#  RC-009 — reset() creates fresh sub-classifiers
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
def test_rc009_reset_replaces_rule_classifier():
    """
    After reset(), _rule_clf must be a new RegimeClassifier instance, not the
    mock that was there before.
    """
    from core.regime.regime_classifier import RegimeClassifier

    clf = make_clf()
    original_mock = clf._rule_clf
    clf._hmm_clf.reset = MagicMock()   # prevent error in reset()

    clf.reset()

    assert clf._rule_clf is not original_mock
    assert isinstance(clf._rule_clf, RegimeClassifier)


@pytest.mark.unit
def test_rc009_reset_calls_hmm_reset():
    """reset() must call _hmm_clf.reset() to clear HMM internal state."""
    clf = make_clf()
    clf._hmm_clf.reset = MagicMock()

    clf.reset()

    clf._hmm_clf.reset.assert_called_once()


# ══════════════════════════════════════════════════════════════════════════════
#  RC-010 — Insufficient data guard
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
def test_rc010_short_df_returns_uncertain():
    """
    The rule-based sub-classifier returns (uncertain, 0.0, {}) for df with
    fewer than 30 rows. EnsembleRegimeClassifier must propagate this gracefully.
    """
    from core.regime.regime_classifier import RegimeClassifier

    clf = EnsembleRegimeClassifier()

    short_df = pd.DataFrame({"close": [100.0] * 20, "volume": [1.0] * 20})
    regime, conf, _ = clf.classify(short_df)

    # Rule-based returns uncertain for short DFs; HMM also can't classify
    assert regime in ("uncertain", "unknown"), f"Unexpected: {regime!r}"


# ══════════════════════════════════════════════════════════════════════════════
#  RC-011 — _get_family maps all documented regimes correctly
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
def test_rc011_get_family_bull_regimes():
    """All bull_family members must map to 'bull_family'."""
    for regime in ("trend_bull", "recovery", "accumulation"):
        result = EnsembleRegimeClassifier._get_family(regime)
        assert result == "bull_family", f"{regime!r} → {result!r} (expected 'bull_family')"


@pytest.mark.unit
def test_rc011_get_family_bear_regimes():
    """All bear_family members must map to 'bear_family'."""
    for regime in ("trend_bear", "distribution", "crisis", "liquidation_cascade"):
        result = EnsembleRegimeClassifier._get_family(regime)
        assert result == "bear_family", f"{regime!r} → {result!r} (expected 'bear_family')"


@pytest.mark.unit
def test_rc011_get_family_range_regimes():
    """range_family and vol_family members must return their correct family."""
    assert EnsembleRegimeClassifier._get_family("ranging")  == "range_family"
    assert EnsembleRegimeClassifier._get_family("volatility_compression") == "range_family"
    assert EnsembleRegimeClassifier._get_family("squeeze") == "range_family"
    assert EnsembleRegimeClassifier._get_family("high_volatility") == "vol_family"
    assert EnsembleRegimeClassifier._get_family("low_volatility")  == "vol_family"


@pytest.mark.unit
def test_rc011_get_family_unknown_returns_none():
    """Regimes not in any family must return None, not raise."""
    assert EnsembleRegimeClassifier._get_family("this_is_not_a_regime") is None
    assert EnsembleRegimeClassifier._get_family("unknown") is None
    assert EnsembleRegimeClassifier._get_family("") is None


# ══════════════════════════════════════════════════════════════════════════════
#  RC-012 — classify() returns a 3-tuple
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
def test_rc012_classify_returns_three_tuple():
    """classify() must always return exactly (str, float, dict)."""
    clf = make_clf()
    _set_mocks(clf, rule_regime="ranging", rule_conf=0.60,
               hmm_regime="ranging", hmm_conf=0.55)

    result = clf.classify(dummy_df())

    assert isinstance(result, tuple), f"Expected tuple, got {type(result)}"
    assert len(result) == 3, f"Expected 3-tuple, got length {len(result)}"
    regime, conf, features = result
    assert isinstance(regime, str),  f"regime must be str, got {type(regime)}"
    assert isinstance(conf,   float), f"conf must be float, got {type(conf)}"
    assert isinstance(features, dict), f"features must be dict, got {type(features)}"


@pytest.mark.unit
def test_rc012_confidence_always_between_zero_and_one():
    """Confidence must always be in [0.0, 1.0] regardless of branch taken."""
    clf = make_clf()

    scenarios = [
        ("bull_trend", 0.97, "bull_trend", 0.95),   # agreement → boosted, capped
        ("bull_trend", 0.80, "trend_bear", 0.75),    # conflict_direction → 0.30
        ("ranging",    0.70, "high_volatility", 0.60), # conflict → penalized
        ("bull_trend", 0.72, "unknown", 0.0),         # rule_only
    ]

    for rule_r, rule_c, hmm_r, hmm_c in scenarios:
        _set_mocks(clf, rule_regime=rule_r, rule_conf=rule_c,
                   hmm_regime=hmm_r, hmm_conf=hmm_c)
        _, conf, _ = clf.classify(dummy_df())
        assert 0.0 <= conf <= 1.0, (
            f"Confidence {conf:.4f} out of [0,1] for "
            f"rule={rule_r!r} / hmm={hmm_r!r}"
        )


# ══════════════════════════════════════════════════════════════════════════════
#  RC-013 — get_hmm_state_map delegates to HMM sub-classifier
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
def test_rc013_get_hmm_state_map_delegates_to_hmm():
    """
    get_hmm_state_map() must delegate to _hmm_clf.get_state_map() and
    return its result unchanged.
    """
    clf = make_clf()
    expected_map = {0: "ranging", 1: "bull_trend", 2: "bear_trend"}
    clf._hmm_clf.get_state_map.return_value = expected_map

    result = clf.get_hmm_state_map()

    clf._hmm_clf.get_state_map.assert_called_once()
    assert result == expected_map
