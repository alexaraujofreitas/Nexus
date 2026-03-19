# ============================================================
# NEXUS TRADER — Ensemble Regime Classifier (Phase 2a)
#
# Combines rule-based RegimeClassifier and HMMClassifier
# into a single weighted ensemble decision.
#
# Default weights: rule_based=0.65, hmm=0.35
#
# Ensemble logic:
#   - If HMM returns 'unknown': use rule-based at full confidence
#   - If both agree: boost confidence
#   - If they disagree: weighted vote with family-based fallback
#
# Regime families:
#   - bull_family: [trend_bull, recovery, accumulation]
#   - bear_family: [trend_bear, distribution, crisis, liquidation_cascade]
#   - range_family: [ranging, volatility_compression, squeeze]
#   - vol_family: [high_volatility, low_volatility]
# ============================================================
from __future__ import annotations

import logging
import pandas as pd
from typing import Optional, Tuple

from core.regime.regime_classifier import RegimeClassifier
from core.regime.hmm_classifier import HMMClassifier

logger = logging.getLogger(__name__)

# Regime families for conflict resolution
_BULL_FAMILY = {"trend_bull", "recovery", "accumulation"}
_BEAR_FAMILY = {"trend_bear", "distribution", "crisis", "liquidation_cascade"}
_RANGE_FAMILY = {"ranging", "volatility_compression", "squeeze"}
_VOL_FAMILY = {"high_volatility", "low_volatility"}

_REGIME_FAMILIES = {
    "bull_family": _BULL_FAMILY,
    "bear_family": _BEAR_FAMILY,
    "range_family": _RANGE_FAMILY,
    "vol_family": _VOL_FAMILY,
}


class EnsembleRegimeClassifier:
    """
    Weighted ensemble of rule-based + HMM classifiers.

    Combines decisions from:
    1. RegimeClassifier (rule-based): weights=0.65 by default
    2. HMMClassifier (machine learning): weight=0.35 by default

    Decision logic:
    - If HMM returns 'unknown': use rule-based result as-is
    - If both agree: boost confidence (min(avg_conf * 1.15, 1.0))
    - If they disagree within same family: use rule-based, average confidence
    - If they disagree across families: use rule-based, penalize confidence by 0.85x

    Usage
    -----
    clf = EnsembleRegimeClassifier(rule_weight=0.65, hmm_weight=0.35)
    regime, confidence, features = clf.classify(df)

    Features dict includes:
    - rule_regime, rule_confidence
    - hmm_regime, hmm_confidence
    - ensemble_method: one of ["rule_only", "agreement", "family_agreement", "conflict"]
    """

    def __init__(self, rule_weight: float = 0.65, hmm_weight: float = 0.35):
        """
        Parameters
        ----------
        rule_weight : float
            Weight for rule-based classifier (default 0.65)
        hmm_weight : float
            Weight for HMM classifier (default 0.35)
        """
        self._rule_weight = float(rule_weight)
        self._hmm_weight = float(hmm_weight)

        # Normalize weights
        total = self._rule_weight + self._hmm_weight
        if total > 0:
            self._rule_weight /= total
            self._hmm_weight /= total

        self._rule_clf = RegimeClassifier()
        self._hmm_clf = HMMClassifier()

        logger.info(
            "EnsembleRegimeClassifier: rule_weight=%.2f hmm_weight=%.2f",
            self._rule_weight,
            self._hmm_weight,
        )

    def classify(
        self, df: pd.DataFrame
    ) -> Tuple[str, float, dict]:
        """
        Classify regime using ensemble.

        Parameters
        ----------
        df : pd.DataFrame
            OHLCV + indicators DataFrame.

        Returns
        -------
        (regime_label, confidence, features_dict)

        features_dict contains:
        - rule_regime, rule_confidence
        - hmm_regime, hmm_confidence
        - ensemble_method: "rule_only" | "agreement" | "family_agreement" | "conflict"
        """
        # Get rule-based classification
        rule_regime, rule_conf, rule_features = self._rule_clf.classify(df)

        # Get HMM classification
        hmm_regime, hmm_conf = self._hmm_clf.classify(df)

        # Ensure regimes are valid strings
        if not isinstance(rule_regime, str):
            rule_regime = "uncertain"
        if not isinstance(hmm_regime, str):
            hmm_regime = "unknown"

        # Build features dict
        features = {
            "rule_regime": rule_regime,
            "rule_confidence": rule_conf,
            "hmm_regime": hmm_regime,
            "hmm_confidence": hmm_conf,
            "ensemble_method": "unknown",
        }

        # ── Ensemble logic ────────────────────────────────────

        # Case 1: HMM returns unknown → use rule-based
        if hmm_regime == "unknown":
            features["ensemble_method"] = "rule_only"
            return rule_regime, rule_conf, features

        # Case 2: Both agree → boost confidence
        if rule_regime == hmm_regime:
            avg_conf = (rule_conf + hmm_conf) / 2.0
            boosted_conf = min(avg_conf * 1.15, 1.0)
            features["ensemble_method"] = "agreement"
            return rule_regime, boosted_conf, features

        # Case 3: Disagree → check family
        rule_family = self._get_family(rule_regime)
        hmm_family = self._get_family(hmm_regime)

        # Check if HMM and rule-based give opposite directions (bull vs bear)
        rule_is_bullish = rule_family == "bull_family"
        hmm_is_bullish = hmm_family == "bull_family"
        if rule_is_bullish != hmm_is_bullish and rule_family is not None and hmm_family is not None:
            # Opposite directions: return uncertain with low confidence
            features["ensemble_method"] = "conflict_direction"
            logger.debug(
                "EnsembleRegimeClassifier: conflicting directions detected — "
                "rule=%s (%s) vs hmm=%s (%s) → returning uncertain",
                rule_regime, rule_family, hmm_regime, hmm_family
            )
            return "uncertain", 0.30, features

        if rule_family is not None and rule_family == hmm_family:
            # Same family → use rule-based, average confidence
            avg_conf = (rule_conf + hmm_conf) / 2.0
            features["ensemble_method"] = "family_agreement"
            return rule_regime, avg_conf, features
        else:
            # Different family → use rule-based, penalize
            penalized_conf = min(rule_conf * 0.85, 1.0)
            features["ensemble_method"] = "conflict"
            return rule_regime, penalized_conf, features

    def reset(self) -> None:
        """Reset both sub-classifiers."""
        self._rule_clf = RegimeClassifier()
        self._hmm_clf.reset()
        logger.info("EnsembleRegimeClassifier: reset")

    def get_hmm_state_map(self) -> dict[int, str]:
        """
        Return HMM's current state-to-regime mapping.

        Returns
        -------
        dict[int, str]
            Maps HMM state index to regime label.
        """
        return self._hmm_clf.get_state_map()

    # ── Internal helpers ──────────────────────────────────────

    @staticmethod
    def _get_family(regime: str) -> Optional[str]:
        """
        Map a regime to its family.

        Parameters
        ----------
        regime : str
            Regime label.

        Returns
        -------
        str or None
            Family name ("bull_family", "bear_family", etc.) or None if not found.
        """
        for family_name, regimes in _REGIME_FAMILIES.items():
            if regime in regimes:
                return family_name
        return None
