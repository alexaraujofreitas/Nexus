"""
Calibrator Monitor — Session 23.

Tracks the runtime predictive quality of the ProbabilityCalibrator by
recording every prediction-vs-actual pair and computing rolling accuracy
metrics.  This answers: "Is the calibrator actually better than sigmoid?"
and "Has its quality degraded since it was trained?"

Metrics computed:
  rolling_auc   — Area Under ROC Curve for the last N predictions.
                  AUC = 0.50 → random; > 0.55 → useful; > 0.65 → good.
  brier_score   — Mean squared error between predicted prob and actual win.
                  Lower is better.  Perfectly calibrated = 0.25 at 50% WR.
  accuracy      — Fraction of predictions where (prob > 0.5) == actual_win.
  prediction_count — Total predictions recorded.

Drift detection:
  Compares the most recent N predictions to the baseline AUC established
  over the first BASELINE_WINDOW predictions.
  If recent_auc drops > DRIFT_THRESHOLD (0.05) below baseline:
    → drift_detected = True
    → ProbabilityCalibrator.get_win_prob() is advised to fall back to sigmoid.

Persistence:
  Saves rolling data to data/calibrator_monitor.json on every record() call.
  The most recent WINDOW predictions are kept; older data is discarded.

Usage:
  from core.learning.calibrator_monitor import get_calibrator_monitor
  monitor = get_calibrator_monitor()

  # At signal generation time (when calibrator produces a prediction):
  monitor.record_prediction(predicted_prob=0.62, actual_win=None)  # actual filled later

  # At trade close time:
  monitor.update_outcome(predicted_prob=0.62, actual_win=True)

  # Status check:
  status = monitor.get_status()
  # {'auc': 0.61, 'brier': 0.21, 'accuracy': 0.58, 'drift': False, ...}
"""
from __future__ import annotations

import json
import logging
import math
import threading
from collections import deque
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_PERSIST_PATH     = Path(__file__).parent.parent.parent / "data" / "calibrator_monitor.json"
_WINDOW           = 100    # rolling window for AUC/Brier calculation
_BASELINE_WINDOW  = 50     # predictions before baseline AUC is established
_DRIFT_THRESHOLD  = 0.05   # AUC drop from baseline that triggers drift warning
_MIN_FOR_AUC      = 20     # minimum predictions required to compute AUC
_lock             = threading.Lock()


def _roc_auc(probs: list[float], labels: list[int]) -> Optional[float]:
    """
    Compute ROC AUC without scikit-learn (trapezoidal method).
    probs: predicted probabilities in [0,1]
    labels: 0/1 actual outcomes
    Returns None if fewer than 2 unique labels.
    """
    if len(probs) < 2:
        return None
    n_pos = sum(labels)
    n_neg = len(labels) - n_pos
    if n_pos == 0 or n_neg == 0:
        return None

    # Sort by descending predicted probability
    pairs = sorted(zip(probs, labels), key=lambda x: -x[0])

    tp = fp = 0
    tp_prev = fp_prev = 0
    auc = 0.0
    for _, label in pairs:
        if label == 1:
            tp += 1
        else:
            fp += 1
            # trapezoidal: add area between previous and current FP step
            auc += (tp + tp_prev) * 0.5 / n_pos
            tp_prev = tp
        fp_prev = fp

    # Normalize (already accounts for FPR denominator)
    return round(auc / n_neg, 4) if n_neg > 0 else None


def _brier(probs: list[float], labels: list[int]) -> float:
    """Mean squared error between predictions and labels (lower = better)."""
    if not probs:
        return 0.0
    return round(sum((p - l) ** 2 for p, l in zip(probs, labels)) / len(probs), 4)


class CalibratorMonitor:
    """
    Tracks rolling accuracy metrics for the ProbabilityCalibrator.
    Detects calibration drift and advises fallback to sigmoid.
    """

    def __init__(self):
        # Each entry: {"prob": float, "won": int (0 or 1)}
        self._window: deque[dict] = deque(maxlen=_WINDOW)
        self._baseline_auc: Optional[float] = None
        self._total_recorded: int = 0
        self._load()

    # ── Persistence ────────────────────────────────────────────────────────

    def _save(self) -> None:
        try:
            _PERSIST_PATH.parent.mkdir(parents=True, exist_ok=True)
            with _lock:
                data = {
                    "window":          list(self._window),
                    "baseline_auc":    self._baseline_auc,
                    "total_recorded":  self._total_recorded,
                }
            _PERSIST_PATH.write_text(json.dumps(data, indent=2))
        except Exception as exc:
            logger.debug("CalibratorMonitor: save failed: %s", exc)

    def _load(self) -> None:
        try:
            if _PERSIST_PATH.exists():
                data = json.loads(_PERSIST_PATH.read_text())
                with _lock:
                    for entry in data.get("window", []):
                        self._window.append(entry)
                    self._baseline_auc   = data.get("baseline_auc")
                    self._total_recorded = data.get("total_recorded", 0)
                logger.debug(
                    "CalibratorMonitor: loaded %d predictions from disk",
                    len(self._window),
                )
        except Exception as exc:
            logger.debug("CalibratorMonitor: load failed (starting fresh): %s", exc)

    # ── Recording ─────────────────────────────────────────────────────────

    def record(self, predicted_prob: float, actual_win: bool) -> None:
        """
        Record a completed prediction-outcome pair.

        Call this when a trade CLOSES, supplying the probability that was
        predicted at signal generation time and the actual outcome (won/lost).

        Parameters
        ----------
        predicted_prob : float
            The win probability output by ProbabilityCalibrator at entry time.
        actual_win : bool
            True if the trade closed in profit.
        """
        with _lock:
            self._window.append({
                "prob": round(float(predicted_prob), 4),
                "won":  int(bool(actual_win)),
            })
            self._total_recorded += 1

        # Establish baseline AUC after enough data
        if self._baseline_auc is None and self._total_recorded >= _BASELINE_WINDOW:
            baseline_auc = self._compute_auc()
            if baseline_auc is not None:
                with _lock:
                    self._baseline_auc = baseline_auc
                logger.info(
                    "CalibratorMonitor: baseline AUC established = %.4f (N=%d)",
                    self._baseline_auc, self._total_recorded,
                )

        self._save()

    # ── Metrics ───────────────────────────────────────────────────────────

    def _extract_lists(self) -> tuple[list[float], list[int]]:
        with _lock:
            entries = list(self._window)
        probs  = [e["prob"] for e in entries]
        labels = [e["won"]  for e in entries]
        return probs, labels

    def _compute_auc(self, last_n: Optional[int] = None) -> Optional[float]:
        probs, labels = self._extract_lists()
        if last_n is not None:
            probs  = probs[-last_n:]
            labels = labels[-last_n:]
        if len(probs) < _MIN_FOR_AUC:
            return None
        return _roc_auc(probs, labels)

    def compute_rolling_auc(self) -> Optional[float]:
        """AUC for the last _WINDOW predictions. None if insufficient data."""
        return self._compute_auc()

    def compute_brier_score(self) -> Optional[float]:
        """Brier score for the last _WINDOW predictions."""
        probs, labels = self._extract_lists()
        if len(probs) < 5:
            return None
        return _brier(probs, labels)

    def compute_accuracy(self) -> Optional[float]:
        """Fraction of predictions where (prob > 0.5) matches actual win."""
        probs, labels = self._extract_lists()
        if len(probs) < 5:
            return None
        correct = sum(1 for p, l in zip(probs, labels) if (p > 0.5) == bool(l))
        return round(correct / len(probs), 4)

    def detect_drift(self) -> tuple[bool, str]:
        """
        Compare recent AUC to baseline.  Returns (drift_detected, reason).

        Drift is defined as: recent_auc < baseline_auc - DRIFT_THRESHOLD.
        Only fires once baseline is established (>= BASELINE_WINDOW predictions).
        """
        if self._baseline_auc is None:
            return False, "no_baseline_yet"

        recent_auc = self._compute_auc(last_n=30)
        if recent_auc is None:
            return False, "insufficient_recent_data"

        delta = self._baseline_auc - recent_auc
        if delta > _DRIFT_THRESHOLD:
            reason = (
                f"AUC dropped {delta:.3f} below baseline "
                f"(recent={recent_auc:.3f}, baseline={self._baseline_auc:.3f})"
            )
            logger.warning(
                "CalibratorMonitor: DRIFT DETECTED | %s — recommend sigmoid fallback",
                reason,
            )
            return True, reason

        return False, f"stable (recent={recent_auc:.3f}, baseline={self._baseline_auc:.3f})"

    def get_status(self) -> dict:
        """
        Return a complete status dict for display in the validation dashboard.

        Keys:
          auc, brier, accuracy, prediction_count, baseline_auc,
          drift_detected, drift_reason, fallback_recommended
        """
        auc              = self.compute_rolling_auc()
        brier            = self.compute_brier_score()
        accuracy         = self.compute_accuracy()
        drift, reason    = self.detect_drift()

        fallback = drift or (auc is not None and auc < 0.50)

        return {
            "auc":                 auc,
            "brier":               brier,
            "accuracy":            accuracy,
            "prediction_count":    self._total_recorded,
            "window_size":         len(self._window),
            "baseline_auc":        self._baseline_auc,
            "drift_detected":      drift,
            "drift_reason":        reason,
            "fallback_recommended": fallback,
        }

    def should_fallback_to_sigmoid(self) -> bool:
        """
        True if the calibrator's recent performance is poor enough that
        falling back to the sigmoid prior is safer.
        Called by ProbabilityCalibrator.get_win_prob().
        """
        if self._total_recorded < _BASELINE_WINDOW:
            return False  # not enough data to make a judgment
        drift, _ = self.detect_drift()
        if drift:
            return True
        auc = self.compute_rolling_auc()
        if auc is not None and auc < 0.48:
            return True   # worse than random — definitely fall back
        return False


# ── Module-level singleton ─────────────────────────────────────────────────

_monitor_instance: Optional[CalibratorMonitor] = None


def get_calibrator_monitor() -> CalibratorMonitor:
    """Return the module-level CalibratorMonitor singleton."""
    global _monitor_instance
    if _monitor_instance is None:
        _monitor_instance = CalibratorMonitor()
    return _monitor_instance
