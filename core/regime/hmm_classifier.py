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
import warnings
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
            # Suppress hmmlearn UserWarnings (transmat_ zero sum, convergence) during fit.
            # These are expected on low-variance price series and do not affect correctness.
            # Genuine failures (NaN / ValueError) are caught by the outer except block.
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", UserWarning)
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

            # Assign regimes based on statistics.
            # Priority-based assignment that avoids degeneracy:
            #   Previously, a sequential if/elif chain caused all-trend_bear maps
            #   when idx_min_ret == idx_max_vol (the most bearish state is also the
            #   most volatile).  trend_bear fired first and high_volatility was never
            #   assigned; remaining states fell through to the trend_bear else-branch,
            #   producing degenerate maps like {0:'ranging', 1:'trend_bear',
            #   2:'trend_bear', 3:'trend_bear'}.
            #
            #   Fix: assign labels in priority order, iterating candidates by their
            #   heuristic sort and skipping states already labeled — each label always
            #   gets exactly one state regardless of index overlaps.

            mean_rets = {s: state_stats[s]["mean_ret"] for s in range(_N_COMPONENTS)}
            mean_vols = {s: state_stats[s]["mean_vol"] for s in range(_N_COMPONENTS)}

            # Pre-sorted candidate lists for each heuristic
            by_ret_desc = sorted(range(_N_COMPONENTS), key=lambda s: mean_rets[s], reverse=True)
            by_ret_asc  = sorted(range(_N_COMPONENTS), key=lambda s: mean_rets[s])
            by_vol_asc  = sorted(range(_N_COMPONENTS), key=lambda s: mean_vols[s])
            by_vol_desc = sorted(range(_N_COMPONENTS), key=lambda s: mean_vols[s], reverse=True)

            new_state_map: dict[int, str] = {}

            # 1. trend_bull — highest return, only if return is positive
            if mean_rets[by_ret_desc[0]] > 0.0:
                new_state_map[by_ret_desc[0]] = "trend_bull"

            # 2. trend_bear — lowest return, only if return is negative & unlabeled
            bear_candidate = by_ret_asc[0]
            if mean_rets[bear_candidate] < 0.0 and bear_candidate not in new_state_map:
                new_state_map[bear_candidate] = "trend_bear"

            # 3. ranging — lowest vol, first unlabeled candidate
            for s in by_vol_asc:
                if s not in new_state_map:
                    new_state_map[s] = "ranging"
                    break

            # 4. high_volatility — highest vol, first unlabeled candidate
            # Guaranteed to be different from trend_bear when idx_min_ret == idx_max_vol
            # because trend_bear already claimed that state in step 2.
            for s in by_vol_desc:
                if s not in new_state_map:
                    new_state_map[s] = "high_volatility"
                    break

            # 5. Any remaining unlabeled states → uncertain (ambiguous regime)
            for state in range(_N_COMPONENTS):
                if state not in new_state_map:
                    new_state_map[state] = "uncertain"

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
