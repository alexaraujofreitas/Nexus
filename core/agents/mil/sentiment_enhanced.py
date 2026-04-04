# ============================================================
# NEXUS TRADER — MIL Phase 4B: Sentiment Enhancer
#
# Enriches social_sentiment agent data with:
#   1. Normalized score (clamped, zero-centered)
#   2. Trend (rolling linear regression over last N observations)
#   3. Spike detection (z-score relative to rolling window)
#   4. Confidence adjustment (staleness-penalized)
#   5. Staleness detection (age-aware discard)
#
# Architecture:
#   - NO I/O.  Reads ONLY from in-memory state.
#   - enhance() is called from SocialSentimentAgent.process()
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
_HISTORY_MAX = 120               # Max observations in rolling window
_HISTORY_WINDOW_S = 7200.0       # 2 hours — matches news temporal decay half-life
_STALENESS_S = 5400.0            # 90 min — 3× poll interval (1800s)
_SPIKE_Z_THRESHOLD = 2.0         # z-score threshold for spike detection
_MIN_HISTORY_FOR_TREND = 3       # Minimum observations for trend calculation
_MIN_HISTORY_FOR_SPIKE = 5       # Minimum observations for z-score


class SentimentEnhancer:
    """
    Stateful enhancer for the social_sentiment agent signal.

    Maintains a rolling window of (timestamp, signal) pairs and
    computes trend, spike, and confidence-adjusted metrics.

    Thread-safe: all state accessed under _lock (threading.Lock).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # Rolling window: deque of (monotonic_ts, signal_value)
        self._history: deque[tuple[float, float]] = deque(maxlen=_HISTORY_MAX)
        self._last_enhance_ts: float = 0.0

    # ── Public API ────────────────────────────────────────────

    def record(self, signal_value: float) -> None:
        """Record an observation.  Called from agent fetch() on every cycle."""
        now = time.monotonic()
        with self._lock:
            self._history.append((now, signal_value))
            self._prune_history(now)

    def enhance(self, agent_data: dict) -> dict:
        """
        Enrich the social_sentiment agent signal dict with MIL metadata.

        Parameters
        ----------
        agent_data : dict
            Raw signal dict from SocialSentimentAgent.process().
            Must contain at least: signal, confidence, updated_at.

        Returns
        -------
        dict
            Original dict with additional ``mil_*`` keys.
            On any error, returns the original dict unchanged (fail-open).
        """
        try:
            return self._do_enhance(agent_data)
        except Exception as exc:
            logger.debug("SentimentEnhancer: enhance failed — %s", exc)
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

        with self._lock:
            self._prune_history(now)
            history = list(self._history)
            self._last_enhance_ts = now

        # 1. Normalized score — clamp to [-1, +1], already expected but enforce
        normalized = max(-1.0, min(1.0, raw_signal))
        result["mil_enhanced"] = True
        result["mil_normalized_signal"] = round(normalized, 4)

        # 2. Trend — rolling linear regression slope
        trend = self._compute_trend(history)
        result["mil_trend"] = round(trend, 6) if trend is not None else 0.0
        result["mil_trend_available"] = trend is not None

        # 3. Spike detection — z-score
        spike, z_score = self._detect_spike(history, normalized)
        result["mil_spike_detected"] = spike
        result["mil_z_score"] = round(z_score, 3) if z_score is not None else 0.0

        # 4. Confidence adjustment — penalize staleness
        age_s = self._estimate_age(data)
        staleness_factor = max(0.10, 1.0 - (age_s / _STALENESS_S)) if age_s < _STALENESS_S else 0.0
        adjusted_confidence = raw_confidence * staleness_factor
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
    def _compute_trend(history: list[tuple[float, float]]) -> Optional[float]:
        """
        Rolling linear regression slope over the history window.

        Returns slope (signal units per second), or None if insufficient data.
        Uses the standard OLS formula: slope = Σ(xi-x̄)(yi-ȳ) / Σ(xi-x̄)²
        """
        if len(history) < _MIN_HISTORY_FOR_TREND:
            return None

        n = len(history)
        ts = [h[0] for h in history]
        vals = [h[1] for h in history]

        mean_t = sum(ts) / n
        mean_v = sum(vals) / n

        num = sum((t - mean_t) * (v - mean_v) for t, v in zip(ts, vals))
        den = sum((t - mean_t) ** 2 for t in ts)

        if abs(den) < 1e-12:
            return 0.0
        return num / den

    @staticmethod
    def _detect_spike(
        history: list[tuple[float, float]], current: float
    ) -> tuple[bool, Optional[float]]:
        """
        Z-score spike detection.

        Returns (spike_detected, z_score).
        """
        if len(history) < _MIN_HISTORY_FOR_SPIKE:
            return False, None

        vals = [h[1] for h in history]
        mean_v = sum(vals) / len(vals)
        var_v = sum((v - mean_v) ** 2 for v in vals) / len(vals)
        std_v = math.sqrt(var_v) if var_v > 0 else 0.0

        if std_v < 1e-6:
            return False, 0.0

        z = (current - mean_v) / std_v
        return abs(z) >= _SPIKE_Z_THRESHOLD, z

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
_enhancer: Optional[SentimentEnhancer] = None


def get_sentiment_enhancer() -> SentimentEnhancer:
    """Return the global SentimentEnhancer, creating it if needed."""
    global _enhancer
    if _enhancer is None:
        _enhancer = SentimentEnhancer()
    return _enhancer
