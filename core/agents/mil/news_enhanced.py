# ============================================================
# NEXUS TRADER — MIL Phase 4B: News Enhancer
#
# Enriches news agent data with:
#   1. Event classification (positive / negative / neutral)
#   2. Impact scoring (magnitude × credibility)
#   3. Exponential decay (2-hour half-life)
#   4. Confidence adjustment (staleness-penalized)
#   5. Staleness detection (age-aware discard)
#
# Architecture:
#   - NO I/O.  Reads ONLY from in-memory state.
#   - enhance() is called from NewsAgent.process()
#     BEFORE the agent publishes to the event bus.
#   - Thread-safe: all mutable state behind _lock.
#   - Fail-open: any error returns the original unmodified dict.
#   - Bounded complexity: O(N) where N = history window size.
#
# Backtest isolation: enhancement metadata flows through
# OrchestratorEngine only.  ConfluenceScorer.score(technical_only=True)
# blocks it via the orchestrator gate.
# ============================================================
from __future__ import annotations

import logging
import math
import threading
import time
from collections import deque
from typing import Optional

logger = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────
_HISTORY_MAX = 60                # Max observations in rolling window
_HISTORY_WINDOW_S = 3600.0       # 1 hour rolling window
_STALENESS_S = 2700.0            # 45 min — 3× poll interval (900s)
_DECAY_HALF_LIFE_S = 7200.0      # 2-hour half-life for exponential decay
_IMPACT_THRESHOLD = 0.20         # Minimum |signal| to classify as non-neutral
_HIGH_IMPACT_THRESHOLD = 0.60    # Threshold for "high_impact" classification
_MIN_ARTICLES_CONFIDENCE = 3     # Articles needed for reasonable confidence


class NewsEnhancer:
    """
    Stateful enhancer for the news agent signal.

    Maintains a rolling window of (timestamp, signal, article_count) tuples
    and computes event classification, impact scoring, and decay-adjusted metrics.

    Thread-safe: all state accessed under _lock (threading.Lock).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # Rolling window: deque of (monotonic_ts, signal, article_count)
        self._history: deque[tuple[float, float, int]] = deque(maxlen=_HISTORY_MAX)
        self._last_enhance_ts: float = 0.0

    # ── Public API ────────────────────────────────────────────

    def record(self, signal_value: float, article_count: int = 0) -> None:
        """Record an observation.  Called from agent fetch() on every cycle."""
        now = time.monotonic()
        with self._lock:
            self._history.append((now, signal_value, article_count))
            self._prune_history(now)

    def enhance(self, agent_data: dict) -> dict:
        """
        Enrich the news agent signal dict with MIL metadata.

        Parameters
        ----------
        agent_data : dict
            Raw signal dict from NewsAgent.process().
            Must contain at least: signal, confidence, article_count, updated_at.

        Returns
        -------
        dict
            Original dict with additional ``mil_*`` keys.
            On any error, returns the original dict unchanged (fail-open).
        """
        try:
            return self._do_enhance(agent_data)
        except Exception as exc:
            logger.debug("NewsEnhancer: enhance failed — %s", exc)
            return agent_data

    def get_diagnostics(self) -> dict:
        """Return diagnostic snapshot (no I/O, no side effects)."""
        with self._lock:
            return {
                "history_size": len(self._history),
                "last_enhance_ts": self._last_enhance_ts,
            }

    # ── Internal ──────────────────────────────────────────────

    def _do_enhance(self, data: dict) -> dict:
        now = time.monotonic()
        result = dict(data)  # shallow copy — don't mutate caller's dict

        raw_signal = float(data.get("signal", 0.0))
        raw_confidence = float(data.get("confidence", 0.0))
        article_count = int(data.get("article_count", 0))

        with self._lock:
            self._prune_history(now)
            history = list(self._history)
            self._last_enhance_ts = now

        result["mil_enhanced"] = True

        # 1. Event classification
        classification = self._classify_event(raw_signal)
        result["mil_event_class"] = classification

        # 2. Impact scoring — magnitude × article confidence factor
        article_factor = min(1.0, article_count / _MIN_ARTICLES_CONFIDENCE)
        impact_score = abs(raw_signal) * article_factor
        result["mil_impact_score"] = round(impact_score, 4)

        # 3. Exponential decay — decay-weighted rolling average
        decay_signal = self._compute_decay_weighted(history, now)
        result["mil_decay_signal"] = round(decay_signal, 4)

        # 4. Confidence adjustment — penalize staleness + low article count
        age_s = self._estimate_age(data)
        staleness_factor = max(0.10, 1.0 - (age_s / _STALENESS_S)) if age_s < _STALENESS_S else 0.0
        adjusted_confidence = raw_confidence * staleness_factor * article_factor
        result["mil_adjusted_confidence"] = round(adjusted_confidence, 4)
        result["mil_staleness_factor"] = round(staleness_factor, 4)

        # 5. Staleness detection
        result["mil_stale"] = age_s >= _STALENESS_S
        result["mil_data_age_s"] = round(age_s, 1)
        result["mil_timestamp"] = now

        return result

    def _prune_history(self, now: float) -> None:
        """Remove entries older than the rolling window."""
        cutoff = now - _HISTORY_WINDOW_S
        while self._history and self._history[0][0] < cutoff:
            self._history.popleft()

    @staticmethod
    def _classify_event(signal: float) -> str:
        """
        Classify the news event based on signal magnitude.

        Returns one of: "high_impact_positive", "positive", "neutral",
                         "negative", "high_impact_negative"
        """
        if abs(signal) < _IMPACT_THRESHOLD:
            return "neutral"
        if signal >= _HIGH_IMPACT_THRESHOLD:
            return "high_impact_positive"
        if signal > 0:
            return "positive"
        if signal <= -_HIGH_IMPACT_THRESHOLD:
            return "high_impact_negative"
        return "negative"

    @staticmethod
    def _compute_decay_weighted(
        history: list[tuple[float, float, int]], now: float
    ) -> float:
        """
        Exponential-decay-weighted average of historical signals.

        decay_weight = exp(-age / half_life × ln(2))
        """
        if not history:
            return 0.0

        ln2 = math.log(2.0)
        total_weight = 0.0
        total_signal = 0.0

        for ts, sig, _ in history:
            age = max(0.0, now - ts)
            w = math.exp(-age * ln2 / _DECAY_HALF_LIFE_S)
            total_signal += sig * w
            total_weight += w

        return total_signal / total_weight if total_weight > 1e-12 else 0.0

    @staticmethod
    def _estimate_age(data: dict) -> float:
        """Estimate data age in seconds from updated_at ISO timestamp."""
        updated_at = data.get("updated_at", "")
        if not updated_at:
            return _STALENESS_S  # Assume stale if no timestamp

        try:
            from datetime import datetime, timezone
            dt = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
            age = (datetime.now(timezone.utc) - dt).total_seconds()
            return max(0.0, age)
        except Exception:
            return _STALENESS_S


# ── Module-level singleton ────────────────────────────────────
_enhancer: Optional[NewsEnhancer] = None


def get_news_enhancer() -> NewsEnhancer:
    """Return the global NewsEnhancer, creating it if needed."""
    global _enhancer
    if _enhancer is None:
        _enhancer = NewsEnhancer()
    return _enhancer
