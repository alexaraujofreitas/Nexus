# ============================================================
# NEXUS TRADER — HMM Regime Classifier  (Sprint 6 — ML Upgrade)
#
# Hidden Markov Model-based regime detection that AUGMENTS
# (not replaces) the existing rule-based RegimeClassifier.
#
# Architecture:
#   1. HMMRegimeClassifier.fit(df_history) — train on 500+ bars
#   2. HMMRegimeClassifier.classify(df)    — returns:
#      - regime label (same set as rule-based classifier)
#      - confidence = P(current state | observations)
#      - regime_probs = probability distribution over all 6 regimes
#
# Features used (from IndicatorLibrary output):
#   - Returns (log return of close)
#   - Volatility (BB width or rolling std)
#   - Trend strength (ADX normalised)
#   - Volume momentum (volume / rolling-mean volume)
#
# HMM configuration:
#   - 6 hidden states (mapping to 6 regime labels)
#   - GaussianHMM with full covariance
#   - Trained with n_iter=200, convergence tol=1e-4
#   - Minimum 200 bars for training (soft requirement)
#
# Integration:
#   The scanner calls classify() and receives both the label
#   AND the full probability distribution.  The OrchestratorEngine
#   can use the distribution to detect regime uncertainty and
#   raise the ConfluenceScorer threshold accordingly.
#
# Graceful degradation:
#   If hmmlearn is not installed, falls back to rule-based
#   RegimeClassifier silently.
# ============================================================
from __future__ import annotations

import logging
import threading
import warnings
import numpy as np
import pandas as pd
from typing import Optional

from core.regime.regime_classifier import (
    RegimeClassifier,
    REGIME_BULL_TREND, REGIME_BEAR_TREND, REGIME_RANGING,
    REGIME_VOL_EXPANSION, REGIME_VOL_COMPRESS, REGIME_UNCERTAIN,
    ALL_REGIMES,
)

logger = logging.getLogger(__name__)

# HMM hyper-parameters
_N_COMPONENTS  = 6       # one state per regime
_N_ITER        = 200
_TOL           = 1e-4
_MIN_TRAIN_BARS = 200    # minimum history for meaningful training
_FEATURE_WINDOW = 5      # rolling window for feature smoothing

# State → regime label mapping is LEARNED empirically during
# label_states().  This dict is populated post-fit.
_DEFAULT_STATE_MAP: dict[int, str] = {
    0: REGIME_BULL_TREND,
    1: REGIME_BEAR_TREND,
    2: REGIME_RANGING,
    3: REGIME_VOL_EXPANSION,
    4: REGIME_VOL_COMPRESS,
    5: REGIME_UNCERTAIN,
}


class HMMRegimeClassifier:
    """
    HMM-based regime classifier with automatic labeling of learned states.

    Usage
    -----
    clf = HMMRegimeClassifier()
    clf.fit(df_history)           # train on historical data
    regime, conf, probs = clf.classify(df_current)

    If not fitted or hmmlearn not available, falls back to the
    rule-based RegimeClassifier automatically.
    """

    def __init__(self):
        self._lock          = threading.RLock()
        self._model         = None        # GaussianHMM instance
        self._state_map:    dict[int, str] = dict(_DEFAULT_STATE_MAP)
        self._is_fitted     = False
        self._fallback      = RegimeClassifier()
        self._hmmlearn_ok   = self._check_hmmlearn()

    # ── Public API ────────────────────────────────────────────

    def fit(self, df: pd.DataFrame) -> bool:
        """
        Train the HMM on a historical indicator DataFrame.

        Parameters
        ----------
        df : pd.DataFrame with IndicatorLibrary columns, minimum 200 rows.

        Returns
        -------
        True if training succeeded, False if fallback to rule-based.
        """
        if not self._hmmlearn_ok:
            logger.debug("HMMRegimeClassifier: hmmlearn not available, skipping fit")
            return False

        if df is None or len(df) < _MIN_TRAIN_BARS:
            logger.warning(
                "HMMRegimeClassifier: insufficient data (%d rows, need %d)",
                0 if df is None else len(df), _MIN_TRAIN_BARS,
            )
            return False

        try:
            X = self._extract_features(df)
            if X is None or len(X) < _MIN_TRAIN_BARS // 2:
                return False

            from hmmlearn.hmm import GaussianHMM
            model = GaussianHMM(
                n_components=_N_COMPONENTS,
                covariance_type="diag",
                n_iter=_N_ITER,
                tol=_TOL,
                random_state=42,
                verbose=False,
            )
            # Suppress hmmlearn UserWarnings (transmat_ zero sum, convergence) during fit.
            # These are expected on low-variance or sparse price series and do not affect
            # correctness — the fit() still returns the best estimate found.  Genuine
            # failures (NaN / ValueError) are caught by the outer except block.
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", UserWarning)
                model.fit(X)

            # Label states by comparing HMM-decoded regimes to rule-based labels
            state_seq = model.predict(X)
            self._label_states(df, state_seq)

            with self._lock:
                self._model    = model
                self._is_fitted = True

            logger.info(
                "HMMRegimeClassifier: fitted on %d bars | state_map=%s",
                len(X), self._state_map,
            )
            return True

        except Exception as exc:
            logger.warning("HMMRegimeClassifier: fit failed — %s", exc)
            return False

    def classify(self, df: pd.DataFrame) -> tuple[str, float, dict[str, float]]:
        """
        Classify the current regime.

        Returns
        -------
        (regime_label, confidence, regime_probs)
            regime_probs: {regime_name: probability, ...} — full distribution
        """
        with self._lock:
            if not self._is_fitted or self._model is None:
                # Fallback to rule-based
                label, conf, _ = self._fallback.classify(df)
                probs          = self._one_hot_probs(label, conf)
                return label, conf, probs

        try:
            X = self._extract_features(df)
            if X is None or len(X) == 0:
                raise ValueError("No features extracted")

            with self._lock:
                model     = self._model
                state_map = dict(self._state_map)

            # Use only the last observation for real-time classification
            x_last = X[-1:, :]
            state_probs = model.predict_proba(X)  # shape (T, n_states)
            last_probs  = state_probs[-1]          # probability over states

            # Map state probabilities to regime probabilities
            regime_probs: dict[str, float] = {r: 0.0 for r in ALL_REGIMES}
            for state_idx, prob in enumerate(last_probs):
                regime_label = state_map.get(state_idx, REGIME_UNCERTAIN)
                regime_probs[regime_label] = regime_probs.get(regime_label, 0.0) + float(prob)

            # Best regime = highest probability
            best_regime = max(regime_probs, key=lambda k: regime_probs[k])
            confidence  = round(regime_probs[best_regime], 4)

            # Uncertainty penalty: if second-best is close, reduce confidence
            sorted_probs = sorted(regime_probs.values(), reverse=True)
            if len(sorted_probs) > 1 and sorted_probs[0] - sorted_probs[1] < 0.15:
                confidence = round(confidence * 0.75, 4)

            logger.debug(
                "HMMRegimeClassifier: %s | conf=%.2f | probs=%s",
                best_regime, confidence,
                {k: round(v, 3) for k, v in regime_probs.items()},
            )
            return best_regime, confidence, regime_probs

        except Exception as exc:
            logger.debug("HMMRegimeClassifier: classify failed, using rule-based — %s", exc)
            label, conf, _ = self._fallback.classify(df)
            return label, conf, self._one_hot_probs(label, conf)

    def classify_combined(self, df: pd.DataFrame) -> tuple[str, float, dict[str, float]]:
        """
        Ensemble: combine HMM probabilities with rule-based classification.

        Weights are adaptive based on HMM calibration quality:
          - Well-calibrated  (≤ 50% states map to 'uncertain'): HMM=0.60, RB=0.40
          - Poorly calibrated (> 50% states map to 'uncertain'): HMM=0.20, RB=0.80

        Falls back to pure rule-based (HMM=0.0, RB=1.0) when HMM is not fitted.

        Calibration quality degrades when the HMM was trained on data where many
        bars are labelled 'uncertain' by the rule-based classifier (e.g. early
        startup data, sparse candles).  This causes multiple HMM states to inherit
        the 'uncertain' label, and the resulting high 'uncertain' probability mass
        suppresses valid bull/bear/ranging signals from the rule-based classifier.
        """
        hmm_label, hmm_conf, hmm_probs = self.classify(df)
        rb_label,  rb_conf, _          = self._fallback.classify(df)

        if not self._is_fitted:
            return rb_label, rb_conf, self._one_hot_probs(rb_label, rb_conf)

        # ── Adaptive HMM weight based on calibration quality ────────────────
        # Count what fraction of HMM state-map entries are 'uncertain'.
        # If > 50%, the model is poorly calibrated: weight rule-based higher.
        with self._lock:
            state_map = dict(self._state_map)

        n_states        = max(1, len(state_map))
        n_uncertain     = sum(1 for v in state_map.values() if v == REGIME_UNCERTAIN)
        uncertain_frac  = n_uncertain / n_states

        if uncertain_frac > 0.50:
            hmm_w, rb_w = 0.20, 0.80
            logger.debug(
                "HMMRegimeClassifier: degraded calibration "
                "(%d/%d states='uncertain') — using HMM_W=0.20 RB_W=0.80",
                n_uncertain, n_states,
            )
        else:
            hmm_w, rb_w = 0.60, 0.40

        # ── Blend probabilities ──────────────────────────────────────────────
        rb_probs = self._one_hot_probs(rb_label, rb_conf)
        blended: dict[str, float] = {}
        for regime in ALL_REGIMES:
            blended[regime] = (
                hmm_probs.get(regime, 0.0) * hmm_w +
                rb_probs.get(regime,  0.0) * rb_w
            )

        best_regime = max(blended, key=lambda k: blended[k])
        confidence  = round(blended[best_regime], 4)
        return best_regime, confidence, blended

    @property
    def is_fitted(self) -> bool:
        with self._lock:
            return self._is_fitted

    # ── Feature extraction ────────────────────────────────────

    def _extract_features(self, df: pd.DataFrame) -> Optional[np.ndarray]:
        """
        Build observation matrix from indicator columns.
        Returns shape (T, 4): [log_return, volatility, adx_norm, vol_momentum]
        """
        try:
            required_close = "close" in df.columns
            if not required_close:
                return None

            close = df["close"].astype(float)

            # Feature 1: log return (bounded)
            log_ret = np.log(close / close.shift(1)).fillna(0.0)
            log_ret = log_ret.clip(-0.15, 0.15)

            # Feature 2: realised volatility (rolling std of returns)
            vol = log_ret.rolling(window=_FEATURE_WINDOW, min_periods=1).std().fillna(0.01)

            # Feature 3: ADX normalised [0, 1]
            if "adx" in df.columns:
                adx = df["adx"].astype(float).fillna(25.0)
                adx_norm = (adx / 100.0).clip(0.0, 1.0)
            else:
                adx_norm = pd.Series(np.full(len(df), 0.25), index=df.index)

            # Feature 4: volume momentum
            if "volume" in df.columns:
                vol_col = df["volume"].astype(float)
                vol_ma  = vol_col.rolling(window=20, min_periods=1).mean().replace(0, 1)
                vol_mom = (vol_col / vol_ma).clip(0.0, 5.0).fillna(1.0)
            else:
                vol_mom = pd.Series(np.ones(len(df)), index=df.index)

            X = np.column_stack([
                log_ret.values,
                vol.values,
                adx_norm.values,
                vol_mom.values,
            ])
            # Drop rows with NaN/inf
            mask = np.isfinite(X).all(axis=1)
            return X[mask]

        except Exception as exc:
            logger.debug("HMMRegimeClassifier: feature extraction failed — %s", exc)
            return None

    # ── State labeling ────────────────────────────────────────

    def _label_states(self, df: pd.DataFrame, state_seq: np.ndarray) -> None:
        """
        Map HMM state indices → regime labels by comparing to rule-based labels.
        Uses majority vote: for each HMM state, which rule-based label
        is most common among bars assigned that state?
        """
        try:
            rule_labels: list[str] = []
            # Only skip the very first 5 bars (insufficient for any indicator).
            # Previously hardcoded to 30, which biased many HMM states toward
            # REGIME_UNCERTAIN — states capturing early-bar behaviour inherited
            # the 'uncertain' label regardless of actual market conditions.
            _WARMUP_BARS = 5
            for i in range(len(df)):
                if i < _WARMUP_BARS:
                    rule_labels.append(REGIME_UNCERTAIN)
                    continue
                sub = df.iloc[max(0, i - 50): i + 1]
                label, _, _ = self._fallback.classify(sub)
                rule_labels.append(label)

            state_map: dict[int, str] = {}
            for state in range(_N_COMPONENTS):
                idx_for_state = np.where(state_seq == state)[0]
                if len(idx_for_state) == 0:
                    state_map[state] = REGIME_UNCERTAIN
                    continue
                labels_for_state = [rule_labels[i] for i in idx_for_state if i < len(rule_labels)]
                if labels_for_state:
                    from collections import Counter
                    state_map[state] = Counter(labels_for_state).most_common(1)[0][0]
                else:
                    state_map[state] = REGIME_UNCERTAIN

            with self._lock:
                self._state_map = state_map

        except Exception as exc:
            logger.debug("HMMRegimeClassifier: label_states failed — %s", exc)

    # ── Utilities ─────────────────────────────────────────────

    @staticmethod
    def _check_hmmlearn() -> bool:
        try:
            import hmmlearn  # noqa: F401
            return True
        except ImportError:
            logger.info("HMMRegimeClassifier: hmmlearn not installed — using rule-based only")
            return False

    @staticmethod
    def _one_hot_probs(label: str, confidence: float) -> dict[str, float]:
        """Create a pseudo-probability dict concentrated on one regime."""
        probs = {r: 0.0 for r in ALL_REGIMES}
        if label in probs:
            probs[label]          = confidence
            # Distribute remaining probability uniformly over others
            remainder = (1.0 - confidence) / max(1, len(ALL_REGIMES) - 1)
            for k in probs:
                if k != label:
                    probs[k] = remainder
        return probs


# ── Module-level singleton ────────────────────────────────────
hmm_classifier: HMMRegimeClassifier = HMMRegimeClassifier()
