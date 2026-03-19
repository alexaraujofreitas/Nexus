# ============================================================
# NEXUS TRADER — HMM Classifier (Phase 2a)
#
# Hidden Markov Model regime classifier that runs in parallel
# with the rule-based classifier.
#
# Features:
#   - 4 hidden states internally (BULL, BEAR, RANGING, HIGH_VOL)
#   - Online training on rolling window (last 150 bars)
#   - Dynamic state mapping based on learned statistics
#   - Thread-safe with graceful hmmlearn fallback
#   - Cache-aware (skips refit if < 5 new bars)
#
# Returns:
#   classify(df) -> (regime_label, confidence)
# ============================================================
from __future__ import annotations

import logging
import threading
import numpy as np
import pandas as pd
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

# HMM hyper-parameters
_N_COMPONENTS = 4           # 4 hidden states
_N_ITER = 100               # GaussianHMM iterations
_TOL = 1e-4                 # convergence tolerance
_MIN_BARS = 50              # minimum bars required
_RETRAIN_WINDOW = 150       # rolling window for training
_RETRAIN_THRESHOLD = 5      # skip refit if < N new bars since last fit

# State → regime label mapping (populated dynamically after fit)
_STATE_MAP_DEFAULT = {
    0: "trend_bull",
    1: "trend_bear",
    2: "ranging",
    3: "high_volatility",
}


class HMMClassifier:
    """
    4-state Gaussian HMM regime classifier.

    States map internally to: BULL, BEAR, RANGING, HIGH_VOL
    Maps dynamically to regime labels: "trend_bull", "trend_bear", "ranging", "high_volatility"

    Features extracted:
    - log_returns: close-to-close log returns
    - realized_vol: 20-bar rolling std of returns
    - momentum: 5-bar cumulative returns
    - atr_ratio: ATR / close

    Trains on rolling 150-bar window on each call.
    Falls back gracefully if hmmlearn is not installed.
    Thread-safe with internal lock.

    Usage
    -----
    clf = HMMClassifier()
    regime, confidence = clf.classify(df)
    """

    def __init__(self):
        self._lock = threading.RLock()
        self._model: Optional[object] = None  # GaussianHMM instance
        self._state_map: dict[int, str] = dict(_STATE_MAP_DEFAULT)
        self._is_fitted: bool = False
        self._last_train_bar_count: int = 0
        self._hmmlearn_ok: bool = self._check_hmmlearn()

    # ── Public API ────────────────────────────────────────────

    def classify(self, df: pd.DataFrame) -> Tuple[str, float]:
        """
        Classify current regime using trained HMM.

        Parameters
        ----------
        df : pd.DataFrame
            OHLCV + indicators DataFrame.

        Returns
        -------
        (regime_label, confidence)
            regime_label: one of ["trend_bull", "trend_bear", "ranging", "high_volatility", "unknown"]
            confidence: float in [0, 1], 0.0 for unknown/error
        """
        if not self._hmmlearn_ok:
            return "unknown", 0.0

        try:
            # Check if we need to retrain
            current_bar_count = len(df)
            bars_since_last_fit = current_bar_count - self._last_train_bar_count

            if (
                not self._is_fitted
                or bars_since_last_fit >= _RETRAIN_THRESHOLD
            ):
                self._fit(df)

            # Predict current regime
            X = self._extract_features(df)
            if X is None or len(X) == 0:
                return "unknown", 0.0

            with self._lock:
                if self._model is None or not self._is_fitted:
                    return "unknown", 0.0
                model = self._model
                state_map = dict(self._state_map)

            # Use last observation for classification
            x_last = X[-1:, :]
            state_probs = model.predict_proba(x_last)  # shape (1, n_states)
            last_probs = state_probs[0]  # probability over states

            # Find best state
            best_state = np.argmax(last_probs)
            confidence = float(last_probs[best_state])

            # Map state to regime
            regime_label = state_map.get(best_state, "unknown")

            logger.debug(
                "HMMClassifier: regime=%s conf=%.3f state=%d state_probs=%s",
                regime_label,
                confidence,
                best_state,
                {i: round(float(p), 3) for i, p in enumerate(last_probs)},
            )

            return regime_label, confidence

        except Exception as exc:
            logger.debug("HMMClassifier.classify failed: %s", exc)
            return "unknown", 0.0

    def reset(self) -> None:
        """Reset fitted model and state mapping."""
        with self._lock:
            self._model = None
            self._is_fitted = False
            self._last_train_bar_count = 0
            self._state_map = dict(_STATE_MAP_DEFAULT)

    def get_state_map(self) -> dict[int, str]:
        """Return current state-to-regime mapping."""
        with self._lock:
            return dict(self._state_map)

    # ── Internal methods ──────────────────────────────────────

    def _fit(self, df: pd.DataFrame) -> bool:
        """
        Train HMM on rolling window of data.

        Parameters
        ----------
        df : pd.DataFrame
            Full OHLCV + indicators DataFrame.

        Returns
        -------
        True if fit succeeded, False otherwise.
        """
        if not self._hmmlearn_ok:
            return False

        try:
            # Use rolling window (last 150 bars)
            if len(df) < _MIN_BARS:
                logger.debug("HMMClassifier: insufficient bars (%d < %d)", len(df), _MIN_BARS)
                return False

            train_df = df.tail(_RETRAIN_WINDOW).copy()
            X = self._extract_features(train_df)

            if X is None or len(X) < _MIN_BARS:
                logger.debug("HMMClassifier: feature extraction failed or insufficient data")
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
            model.fit(X)

            # Compute state statistics for dynamic mapping
            state_seq = model.predict(X)
            self._compute_state_mapping(X, state_seq)

            with self._lock:
                self._model = model
                self._is_fitted = True
                self._last_train_bar_count = len(df)

            logger.info(
                "HMMClassifier: fitted on %d bars | state_map=%s",
                len(X),
                self._state_map,
            )
            return True

        except Exception as exc:
            logger.warning("HMMClassifier._fit failed: %s", exc)
            return False

    def _extract_features(self, df: pd.DataFrame) -> Optional[np.ndarray]:
        """
        Extract 4 features for HMM.

        Features:
        1. log_returns: close-to-close log returns (clipped)
        2. realized_vol: 20-bar rolling std of returns
        3. momentum: 5-bar cumulative returns
        4. atr_ratio: ATR / close (if ATR available, else 1.0)

        Returns
        -------
        X : np.ndarray of shape (n_bars, 4) or None if extraction failed.
        """
        try:
            if "close" not in df.columns:
                return None

            close = df["close"].astype(float)

            # Feature 1: log returns (bounded)
            log_ret = np.log(close / close.shift(1)).fillna(0.0)
            log_ret = log_ret.clip(-0.20, 0.20)

            # Feature 2: realized volatility (20-bar rolling std of returns)
            realized_vol = log_ret.rolling(window=20, min_periods=1).std().fillna(0.001)
            realized_vol = realized_vol.clip(0.0001, 1.0)

            # Feature 3: momentum (5-bar cumulative returns)
            # Use exp(sum(log_ret)) instead of prod(1+ret) — Rolling.prod() removed in pandas 2.0
            momentum = np.exp(log_ret.rolling(window=5, min_periods=1).sum()) - 1.0
            momentum = momentum.fillna(0.0).clip(-0.30, 0.30)

            # Feature 4: ATR ratio
            if "atr" in df.columns:
                atr = df["atr"].astype(float).fillna(0.0)
                atr_ratio = (atr / close).fillna(0.01).clip(0.0, 1.0)
            else:
                atr_ratio = pd.Series(np.ones(len(df)) * 0.02, index=df.index)

            X = np.column_stack([
                log_ret.values,
                realized_vol.values,
                momentum.values,
                atr_ratio.values,
            ])

            # Drop NaN/inf rows
            mask = np.isfinite(X).all(axis=1)
            X_clean = X[mask]

            if len(X_clean) == 0:
                return None

            return X_clean

        except Exception as exc:
            logger.debug("HMMClassifier._extract_features failed: %s", exc)
            return None

    def _compute_state_mapping(self, X: np.ndarray, state_seq: np.ndarray) -> None:
        """
        Dynamically map HMM states to regime labels based on learned statistics.

        Heuristics:
        - Highest mean return + moderate vol → trend_bull
        - Lowest mean return → trend_bear
        - Lowest vol → ranging
        - Highest vol → high_volatility

        Parameters
        ----------
        X : np.ndarray
            Feature matrix (n_bars, 4)
        state_seq : np.ndarray
            State assignments (n_bars,)
        """
        try:
            state_stats: dict[int, dict] = {}

            for state in range(_N_COMPONENTS):
                mask = state_seq == state
                if mask.sum() == 0:
                    state_stats[state] = {
                        "mean_ret": 0.0,
                        "mean_vol": 0.0,
                        "mean_momentum": 0.0,
                        "mean_atr_ratio": 0.0,
                    }
                else:
                    state_data = X[mask]
                    state_stats[state] = {
                        "mean_ret": float(np.mean(state_data[:, 0])),
                        "mean_vol": float(np.mean(state_data[:, 1])),
                        "mean_momentum": float(np.mean(state_data[:, 2])),
                        "mean_atr_ratio": float(np.mean(state_data[:, 3])),
                    }

            # Assign regimes based on statistics
            # Find states with highest/lowest metrics
            mean_rets = {s: state_stats[s]["mean_ret"] for s in range(_N_COMPONENTS)}
            mean_vols = {s: state_stats[s]["mean_vol"] for s in range(_N_COMPONENTS)}

            idx_max_ret = max(mean_rets, key=mean_rets.get)
            idx_min_ret = min(mean_rets, key=mean_rets.get)
            idx_max_vol = max(mean_vols, key=mean_vols.get)
            idx_min_vol = min(mean_vols, key=mean_vols.get)

            new_state_map: dict[int, str] = {}

            for state in range(_N_COMPONENTS):
                if state == idx_max_ret and mean_rets[state] > 0.0:
                    new_state_map[state] = "trend_bull"
                elif state == idx_min_ret and mean_rets[state] < 0.0:
                    new_state_map[state] = "trend_bear"
                elif state == idx_min_vol:
                    new_state_map[state] = "ranging"
                elif state == idx_max_vol:
                    new_state_map[state] = "high_volatility"
                else:
                    # Fallback assignment
                    if state_stats[state]["mean_ret"] > 0:
                        new_state_map[state] = "trend_bull"
                    else:
                        new_state_map[state] = "trend_bear"

            with self._lock:
                self._state_map = new_state_map

        except Exception as exc:
            logger.debug("HMMClassifier._compute_state_mapping failed: %s", exc)

    @staticmethod
    def _check_hmmlearn() -> bool:
        """Check if hmmlearn is available."""
        try:
            import hmmlearn  # noqa: F401
            return True
        except ImportError:
            logger.info("HMMClassifier: hmmlearn not installed, classify() will return ('unknown', 0.0)")
            return False
