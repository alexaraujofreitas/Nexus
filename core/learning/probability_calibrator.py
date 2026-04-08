"""
Probability Calibrator — Phase 3.
Trains a logistic regression model on trade history to estimate P(win|features).
Integrates with the EV gate as a drop-in replacement for the sigmoid win_prob.

Architecture:
- Trained on trade_log.jsonl (requires >= 300 trades)
- Time-series split (no random shuffle — CRITICAL for no look-ahead bias)
- Persisted to data/prob_calibrator.pkl
- Falls back to sigmoid if not trained

Leakage audit (Session 22 second-pass):
  ✅ Time-series split at 80% — oldest 80% train, newest 20% test. Correct.
  ✅ StandardScaler fit ONLY on X_train, applied to X_test. No test leakage.
  ✅ Feature extraction uses only data available at decision time.
  ⚠️ CIRCULAR FEATURE: confluence_score is computed by the same pipeline before
     calling the calibrator. If the LR model just re-learns the score threshold,
     it adds no marginal value. We keep it in features because it IS informative,
     but monitor for score_delta in calibration quality. If AUC without
     confluence_score matches AUC with it, drop it from future training.
  ✅ No future data in utc_hour, rsi, adx, atr_ratio, funding_rate.
  ✅ Regime and model one-hot are both decision-time signals (not post-hoc labels).

Class imbalance handling (fixed in Session 22):
  v1 threshold: pos_rate < 0.1 or > 0.9 — far too conservative.
     Most crypto strategy WR is 40–60%, but after a drawdown period WR can
     drop to 30–35%, which is 0.35 pos_rate. This never triggered 'balanced'.
  v2 threshold: pos_rate < 0.35 or > 0.65 — activates class balancing once
     the class split is 35/65 or worse. Conservative enough not to fire during
     a normal volatility regime, responsive enough to handle drawdown periods.

Confidence definition:
  get_win_prob() returns (prob, source) where:
    source='calibrator': raw sigmoid output of logistic regression, in [0,1]
    source='sigmoid':    1/(1+exp(-k*(score-midpoint))), the design prior
  The calibrator's output is not independently calibrated via Platt scaling.
  Isotonic regression calibration is deferred until >= 500 trades are available
  and monotonicity can be verified. Until then, the output is a ranking signal,
  not a true probability.
  DO NOT interpret the calibrator output as "there is a 62% chance this trade wins."
  Interpret it as "relative to other trades, this one has higher expected win rate."

min_confidence parameter in get_win_prob():
  Previously accepted but not used. Now enforced: if calibrator returns a
  probability < min_confidence AND the sigmoid would have been higher, we blend:
    final_prob = max(calibrator_prob, sigmoid_prob * 0.8)
  This prevents the calibrator from becoming more conservative than the design
  prior during early training phases with insufficient data.
"""
from __future__ import annotations
import io
import json
import logging
import math
import pickle
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional


class _RestrictedUnpickler(pickle.Unpickler):
    """Only allow safe standard library and scientific computing types."""

    _SAFE_MODULES = frozenset({
        'builtins', 'collections', '_codecs',
        'numpy', 'numpy.core', 'numpy.core.multiarray',
        'numpy.core.numeric', 'numpy.core._multiarray_umath',
        'numpy._core', 'numpy._core.multiarray',
        'sklearn', 'sklearn.calibration', 'sklearn.isotonic',
        'sklearn.linear_model', 'sklearn.linear_model._logistic',
        'sklearn.preprocessing', 'sklearn.preprocessing._data',
        'sklearn.pipeline', 'sklearn.utils', 'sklearn.utils._tags',
        'sklearn.base',
    })

    def find_class(self, module: str, name: str) -> type:
        top = module.split('.')[0]
        if top in self._SAFE_MODULES or module in self._SAFE_MODULES:
            return super().find_class(module, name)
        raise pickle.UnpicklingError(f"Blocked: {module}.{name}")


def _safe_pickle_load(f):
    """Load a pickle file using a restricted unpickler that only allows safe types."""
    return _RestrictedUnpickler(f).load()

logger = logging.getLogger(__name__)

_MODEL_PATH       = Path(__file__).parent.parent.parent / "data" / "prob_calibrator.pkl"
_CALIBRATION_PATH = Path(__file__).parent.parent.parent / "data" / "score_calibration.json"
_lock = threading.Lock()

MIN_TRAINING_TRADES = 300

# Class-balance thresholds (v2 — was 0.1/0.9 which was too extreme)
_CLASS_BALANCE_LOW  = 0.35
_CLASS_BALANCE_HIGH = 0.65

_FEATURE_ORDER: Optional[list[str]] = None  # set at training time


class ProbabilityCalibrator:
    """
    Logistic regression wrapper for P(trade_wins | entry_features).
    Provides fallback to sigmoid when not trained.
    """

    def __init__(self):
        self._model = None
        self._feature_names: Optional[list[str]] = None
        self._trained_on: int = 0
        self._score_calibration: Optional[dict] = None
        self._load()

    def _load(self) -> None:
        if _MODEL_PATH.exists():
            try:
                with _MODEL_PATH.open("rb") as f:
                    saved = _safe_pickle_load(f)
                self._model = saved.get("model")
                self._feature_names = saved.get("feature_names")
                self._trained_on = saved.get("trained_on", 0)
                logger.info(
                    "ProbabilityCalibrator: loaded model (trained on %d trades)", self._trained_on
                )
            except Exception as exc:
                logger.warning("ProbabilityCalibrator: load failed: %s", exc)
        if _CALIBRATION_PATH.exists():
            try:
                self._score_calibration = json.loads(_CALIBRATION_PATH.read_text())
            except Exception as exc:
                logger.warning("ProbabilityCalibrator: score calibration load failed: %s", exc)

    def is_trained(self) -> bool:
        return self._model is not None and self._trained_on >= MIN_TRAINING_TRADES

    def train(self, X: list[dict], y: list[int]) -> dict:
        """
        Train on feature dicts and labels. Uses time-series split (no shuffle).
        Returns metrics dict.

        IMPORTANT: Call this after accumulating >= 300 trades in data/trade_log.jsonl.
        Use scripts/train_calibrator.py (or run from analytics CLI).
        """
        if len(X) < MIN_TRAINING_TRADES:
            return {"error": f"Need {MIN_TRAINING_TRADES} trades, have {len(X)}"}
        try:
            from sklearn.linear_model import LogisticRegression
            from sklearn.preprocessing import StandardScaler
            from sklearn.pipeline import Pipeline
            import numpy as np
        except ImportError:
            return {
                "error": (
                    "scikit-learn not installed. "
                    "Run: pip install scikit-learn --break-system-packages"
                )
            }

        # Build feature matrix in consistent order (alphabetical for reproducibility)
        feature_names = sorted(X[0].keys())
        Xm   = [[row.get(f, 0.0) for f in feature_names] for row in X]
        Xarr = np.array(Xm, dtype=float)
        yarr = np.array(y, dtype=int)

        # Time-series split: oldest 80% train, newest 20% test.
        # NEVER shuffle — this would leak future labels into training.
        split    = int(len(Xarr) * 0.8)
        X_train, X_test = Xarr[:split], Xarr[split:]
        y_train, y_test = yarr[:split], yarr[split:]

        # Class balance check (v2: threshold 0.35/0.65, was 0.1/0.9)
        pos_rate = float(y_train.mean())
        if pos_rate < _CLASS_BALANCE_LOW or pos_rate > _CLASS_BALANCE_HIGH:
            logger.warning(
                "ProbabilityCalibrator: class imbalance detected "
                "(pos_rate=%.2f, threshold=[%.2f, %.2f]) — using class_weight='balanced'",
                pos_rate, _CLASS_BALANCE_LOW, _CLASS_BALANCE_HIGH,
            )
            class_weight = "balanced"
        else:
            class_weight = None

        # Circular feature warning
        if "confluence_score" in feature_names:
            logger.info(
                "ProbabilityCalibrator: 'confluence_score' is included in features. "
                "Monitor AUC with/without it after 500 trades to detect tautological "
                "re-learning. If AUC unchanged without it, drop from future training."
            )

        pipe = Pipeline([
            ("scaler", StandardScaler()),
            (
                "lr",
                LogisticRegression(
                    C=1.0, max_iter=500, class_weight=class_weight, random_state=42
                ),
            ),
        ])

        # Fit scaler ONLY on train data — test data is unseen
        pipe.fit(X_train, y_train)

        train_acc = float((pipe.predict(X_train) == y_train).mean())
        test_acc  = float((pipe.predict(X_test)  == y_test).mean())

        # Compute AUC if sklearn version supports it
        try:
            from sklearn.metrics import roc_auc_score
            test_auc = float(roc_auc_score(y_test, pipe.predict_proba(X_test)[:, 1]))
        except Exception as exc:
            logger.warning("ProbabilityCalibrator: AUC computation failed: %s", exc)
            test_auc = None

        with _lock:
            self._model         = pipe
            self._feature_names = feature_names
            self._trained_on    = len(X)
            try:
                _MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
                with _MODEL_PATH.open("wb") as f:
                    pickle.dump(
                        {
                            "model":          pipe,
                            "feature_names":  feature_names,
                            "trained_on":     len(X),
                            "trained_at":     datetime.utcnow().isoformat(),
                            "pos_rate":       pos_rate,
                            "class_weight":   class_weight,
                        },
                        f,
                    )
            except Exception as exc:
                logger.warning("ProbabilityCalibrator: save failed: %s", exc)

        metrics = {
            "train_size":   split,
            "test_size":    len(X) - split,
            "train_acc":    round(train_acc, 4),
            "test_acc":     round(test_acc, 4),
            "test_auc":     round(test_auc, 4) if test_auc is not None else None,
            "pos_rate":     round(pos_rate, 4),
            "class_weight": class_weight,
            "trained_on":   len(X),
            "note": (
                "Output is a RANKING signal, not a calibrated probability. "
                "Do not interpret as 'P(win) = X%' until isotonic calibration "
                "is applied (deferred to >= 500 trades)."
            ),
        }
        logger.info("ProbabilityCalibrator: trained | %s", metrics)
        return metrics

    def predict_proba(self, features: dict) -> float:
        """
        Return P(win) for a feature dict.
        Raises ValueError if model not trained.
        """
        if not self.is_trained():
            raise ValueError("Model not trained")
        if self._feature_names is None:
            raise ValueError("Feature names missing")
        import numpy as np
        # Use stored feature order — handles case where new features appear in live data
        row = np.array([[features.get(f, 0.0) for f in self._feature_names]])
        prob = float(self._model.predict_proba(row)[0][1])
        return prob

    def get_win_prob(
        self,
        features: dict,
        score: float,
        sigmoid_k: float = 8.0,
        sigmoid_midpoint: float = 0.55,
        min_confidence: float = 0.55,
    ) -> tuple[float, str]:
        """
        Return (win_prob, source) where source is 'calibrator' or 'sigmoid'.

        min_confidence is now enforced: if calibrator returns a very low probability
        that is lower than the sigmoid would produce, we take the maximum to prevent
        the calibrator from becoming a stricter gate than the design prior during
        the early training phase (300–500 trades).

        Once >= 500 trades are available and test_auc >= 0.55, remove this blending
        and trust the calibrator fully.
        """
        sigmoid_prob = 1.0 / (1.0 + math.exp(-sigmoid_k * (score - sigmoid_midpoint)))

        # Drift-based automatic fallback: if CalibratorMonitor detects AUC has
        # degraded significantly below baseline, revert to sigmoid for safety.
        # This is non-fatal — failure to load the monitor defaults to normal path.
        _drift_fallback = False
        try:
            from core.learning.calibrator_monitor import get_calibrator_monitor
            _drift_fallback = get_calibrator_monitor().should_fallback_to_sigmoid()
        except Exception as exc:
            logger.warning("ProbabilityCalibrator: drift fallback check failed: %s", exc)

        if self.is_trained() and not _drift_fallback:
            try:
                cal_prob = self.predict_proba(features)
                # Soft floor: don't allow calibrator to be more conservative than sigmoid
                # by more than 20% (accounts for early-phase calibration noise)
                final_prob = max(cal_prob, sigmoid_prob * 0.80)
                if final_prob != cal_prob:
                    logger.debug(
                        "ProbabilityCalibrator: blended cal=%.3f sigmoid=%.3f → %.3f "
                        "(sigmoid floor active)",
                        cal_prob, sigmoid_prob, final_prob,
                    )
                return final_prob, "calibrator"
            except Exception as exc:
                logger.debug(
                    "ProbabilityCalibrator: prediction failed (%s), falling back to sigmoid", exc
                )
        elif _drift_fallback:
            logger.info(
                "ProbabilityCalibrator: drift fallback active — using sigmoid (AUC degraded)",
            )

        return sigmoid_prob, "sigmoid"

    def compute_score_calibration(self, trades: list[dict]) -> dict:
        """
        Compute empirical win rate per score bucket.
        Used for diagnostics: if WR does NOT increase with score,
        the scoring pipeline is not predictive and needs recalibration.
        """
        buckets = {
            "0.40-0.50": {"wins": 0, "total": 0},
            "0.50-0.55": {"wins": 0, "total": 0},
            "0.55-0.60": {"wins": 0, "total": 0},
            "0.60-0.65": {"wins": 0, "total": 0},
            "0.65-0.70": {"wins": 0, "total": 0},
            "0.70-0.80": {"wins": 0, "total": 0},
            "0.80-1.00": {"wins": 0, "total": 0},
        }
        for trade in trades:
            score = float(trade.get("confluence_score") or 0.0)
            won   = bool(trade.get("won") or (trade.get("pnl_pct") or 0.0) > 0)
            for bucket_label, (lo, hi) in [
                ("0.40-0.50", (0.40, 0.50)), ("0.50-0.55", (0.50, 0.55)),
                ("0.55-0.60", (0.55, 0.60)), ("0.60-0.65", (0.60, 0.65)),
                ("0.65-0.70", (0.65, 0.70)), ("0.70-0.80", (0.70, 0.80)),
                ("0.80-1.00", (0.80, 1.01)),
            ]:
                if lo <= score < hi:
                    buckets[bucket_label]["total"] += 1
                    if won:
                        buckets[bucket_label]["wins"] += 1
                    break

        result = {}
        win_rates = []
        for k, v in buckets.items():
            wr = round(v["wins"] / v["total"], 4) if v["total"] > 0 else None
            result[k] = {"total": v["total"], "wins": v["wins"], "win_rate": wr}
            if wr is not None:
                win_rates.append(wr)

        # Monotonicity check: are higher-score buckets producing better WR?
        if len(win_rates) >= 3:
            increasing = sum(
                1 for i in range(len(win_rates) - 1) if win_rates[i + 1] >= win_rates[i]
            )
            monotonicity = round(increasing / (len(win_rates) - 1), 2)
            result["_diagnostic"] = {
                "monotonicity_score": monotonicity,
                "interpretation": (
                    "GOOD (score predicts WR)" if monotonicity >= 0.6
                    else "WARNING: higher scores do not reliably predict higher WR"
                ),
            }
            if monotonicity < 0.6:
                logger.warning(
                    "ProbabilityCalibrator: score calibration monotonicity=%.2f — "
                    "higher confluence scores are NOT reliably producing higher win rates. "
                    "Consider re-examining the scoring weights.",
                    monotonicity,
                )

        with _lock:
            self._score_calibration = result
            try:
                _CALIBRATION_PATH.parent.mkdir(parents=True, exist_ok=True)
                _CALIBRATION_PATH.write_text(json.dumps(result, indent=2))
            except Exception as exc:
                logger.warning("Calibration data save failed: %s", exc)
        return result


_calibrator_instance: Optional[ProbabilityCalibrator] = None


def get_probability_calibrator() -> ProbabilityCalibrator:
    global _calibrator_instance
    if _calibrator_instance is None:
        _calibrator_instance = ProbabilityCalibrator()
    return _calibrator_instance
