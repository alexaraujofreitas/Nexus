# core/monitoring/phase1_metrics.py
"""
Phase1MetricsTracker — centralized metrics collection for Phase 1 components.

Tracks:
- TransitionDetector: signal counts by type/symbol, active transitions
- CoverageGuarantee: escalation level history, fallback trade counts
- RegimeCapitalAllocator: sizing adjustment counts, regime distribution
- Global: regime distribution across scan cycles

Thread-safe: uses RLock for concurrent access from scanner + dashboard.
"""
from __future__ import annotations

import logging
import threading
import time
from collections import defaultdict
from typing import Optional

logger = logging.getLogger(__name__)


class Phase1MetricsTracker:
    """Singleton metrics tracker for Phase 1 regime-orchestrated components."""

    def __init__(self):
        self._lock = threading.RLock()

        # ── TransitionDetector metrics ──────────────────────────
        self._transition_signals: list[dict] = []  # all signals (capped at 200)
        self._transition_counts: dict[str, int] = defaultdict(int)  # by type
        self._transition_counts_by_symbol: dict[str, int] = defaultdict(int)
        self._active_transitions: dict[str, dict] = {}  # symbol -> latest signal

        # ── CoverageGuarantee metrics ──────────────────────────
        self._cg_level_history: list[dict] = []  # (timestamp, level, action, regime)
        self._cg_current_level: int = 0
        self._cg_idle_cycles: int = 0
        self._cg_fallback_trade_count: int = 0
        self._cg_max_level_reached: int = 0
        self._cg_gap_episodes: int = 0  # number of times level went from 0 to >0

        # ── RegimeCapitalAllocator metrics ─────────────────────
        self._rca_adjustments: list[dict] = []  # (timestamp, regime, multiplier, size_before, size_after)
        self._rca_adjustment_count: int = 0
        self._rca_regime_distribution: dict[str, int] = defaultdict(int)  # regime -> count of adjustments

        # ── Global regime distribution ─────────────────────────
        self._regime_distribution: dict[str, int] = defaultdict(int)  # all regimes seen across scans
        self._total_scan_cycles: int = 0

        # ── Timestamps ─────────────────────────────────────────
        self._created_at: float = time.time()
        self._last_transition_at: Optional[float] = None
        self._last_cg_escalation_at: Optional[float] = None
        self._last_rca_adjustment_at: Optional[float] = None

    # ════════════════════════════════════════════════════════════
    # TransitionDetector recording
    # ════════════════════════════════════════════════════════════

    def record_transition(
        self,
        symbol: str,
        transition_type: str,
        direction: str,
        confidence: float,
        source_regime: str,
        target_regime: str,
    ) -> None:
        """Record a transition signal detection."""
        with self._lock:
            now = time.time()
            entry = {
                "timestamp": now,
                "symbol": symbol,
                "type": transition_type,
                "direction": direction,
                "confidence": round(confidence, 3),
                "source_regime": source_regime,
                "target_regime": target_regime,
            }
            self._transition_signals.append(entry)
            if len(self._transition_signals) > 200:
                self._transition_signals = self._transition_signals[-200:]

            self._transition_counts[transition_type] += 1
            self._transition_counts_by_symbol[symbol] += 1
            self._active_transitions[symbol] = entry
            self._last_transition_at = now

            logger.info(
                "Phase1Metrics: TRANSITION %s on %s dir=%s conf=%.2f (%s→%s) "
                "[total: %d signals, %d by type %s]",
                transition_type, symbol, direction, confidence,
                source_regime, target_regime,
                len(self._transition_signals),
                self._transition_counts[transition_type],
                transition_type,
            )

    def clear_active_transition(self, symbol: str) -> None:
        """Clear active transition for a symbol (expired)."""
        with self._lock:
            self._active_transitions.pop(symbol, None)

    # ════════════════════════════════════════════════════════════
    # CoverageGuarantee recording
    # ════════════════════════════════════════════════════════════

    def record_coverage_event(
        self,
        level: int,
        action: str,
        idle_cycles: int,
        dominant_regime: str,
        fallback_trades_remaining: int = 0,
    ) -> None:
        """Record a CoverageGuarantee escalation event."""
        with self._lock:
            now = time.time()

            # Detect new gap episode
            if self._cg_current_level == 0 and level > 0:
                self._cg_gap_episodes += 1

            prev_level = self._cg_current_level
            self._cg_current_level = level
            self._cg_idle_cycles = idle_cycles
            self._cg_max_level_reached = max(self._cg_max_level_reached, level)

            entry = {
                "timestamp": now,
                "level": level,
                "action": action,
                "idle_cycles": idle_cycles,
                "dominant_regime": dominant_regime,
                "fallback_remaining": fallback_trades_remaining,
            }
            self._cg_level_history.append(entry)
            if len(self._cg_level_history) > 200:
                self._cg_level_history = self._cg_level_history[-200:]

            if level > prev_level:
                self._last_cg_escalation_at = now
                logger.warning(
                    "Phase1Metrics: COVERAGE ESCALATION level %d→%d action=%s "
                    "idle=%d regime=%s [episode #%d, max_level=%d]",
                    prev_level, level, action, idle_cycles, dominant_regime,
                    self._cg_gap_episodes, self._cg_max_level_reached,
                )
            elif level == 0 and prev_level > 0:
                logger.info(
                    "Phase1Metrics: COVERAGE RETRACTED level %d→0 (primary signal restored) "
                    "idle_was=%d regime=%s",
                    prev_level, idle_cycles, dominant_regime,
                )

    def record_fallback_trade(self) -> None:
        """Record that a fallback trade was executed."""
        with self._lock:
            self._cg_fallback_trade_count += 1
            logger.info(
                "Phase1Metrics: FALLBACK TRADE executed [total: %d]",
                self._cg_fallback_trade_count,
            )

    # ════════════════════════════════════════════════════════════
    # RegimeCapitalAllocator recording
    # ════════════════════════════════════════════════════════════

    def record_rca_adjustment(
        self,
        regime: str,
        multiplier: float,
        size_before: float,
        size_after: float,
        is_transition: bool = False,
        is_fallback: bool = False,
    ) -> None:
        """Record a regime-based sizing adjustment."""
        with self._lock:
            now = time.time()
            entry = {
                "timestamp": now,
                "regime": regime,
                "multiplier": round(multiplier, 3),
                "size_before": round(size_before, 2),
                "size_after": round(size_after, 2),
                "is_transition": is_transition,
                "is_fallback": is_fallback,
            }
            self._rca_adjustments.append(entry)
            if len(self._rca_adjustments) > 200:
                self._rca_adjustments = self._rca_adjustments[-200:]

            self._rca_adjustment_count += 1
            self._rca_regime_distribution[regime] += 1
            self._last_rca_adjustment_at = now

            logger.info(
                "Phase1Metrics: RCA SIZING regime=%s mult=%.2f size=%.2f→%.2f "
                "transition=%s fallback=%s [total: %d adjustments]",
                regime, multiplier, size_before, size_after,
                is_transition, is_fallback, self._rca_adjustment_count,
            )

    # ════════════════════════════════════════════════════════════
    # Global regime distribution
    # ════════════════════════════════════════════════════════════

    def record_scan_cycle(self, regime_distribution: dict) -> None:
        """Record regime distribution from a scan cycle."""
        with self._lock:
            self._total_scan_cycles += 1
            for regime, count in regime_distribution.items():
                self._regime_distribution[regime] += count

    # ════════════════════════════════════════════════════════════
    # Snapshot for dashboard / diagnostics
    # ════════════════════════════════════════════════════════════

    def get_snapshot(self) -> dict:
        """Thread-safe snapshot of all metrics for dashboard display."""
        with self._lock:
            now = time.time()
            uptime_min = (now - self._created_at) / 60

            return {
                # TransitionDetector
                "transition_total": sum(self._transition_counts.values()),
                "transition_by_type": dict(self._transition_counts),
                "transition_by_symbol": dict(self._transition_counts_by_symbol),
                "active_transitions": dict(self._active_transitions),
                "last_transition_ago_min": round((now - self._last_transition_at) / 60, 1)
                    if self._last_transition_at else None,

                # CoverageGuarantee
                "cg_current_level": self._cg_current_level,
                "cg_idle_cycles": self._cg_idle_cycles,
                "cg_max_level_reached": self._cg_max_level_reached,
                "cg_gap_episodes": self._cg_gap_episodes,
                "cg_fallback_trade_count": self._cg_fallback_trade_count,
                "cg_last_escalation_ago_min": round((now - self._last_cg_escalation_at) / 60, 1)
                    if self._last_cg_escalation_at else None,

                # RegimeCapitalAllocator
                "rca_adjustment_count": self._rca_adjustment_count,
                "rca_regime_distribution": dict(self._rca_regime_distribution),
                "rca_last_adjustment_ago_min": round((now - self._last_rca_adjustment_at) / 60, 1)
                    if self._last_rca_adjustment_at else None,

                # Global
                "regime_distribution": dict(self._regime_distribution),
                "total_scan_cycles": self._total_scan_cycles,
                "uptime_min": round(uptime_min, 1),

                # Recent events (last 5 each)
                "recent_transitions": self._transition_signals[-5:],
                "recent_cg_events": self._cg_level_history[-5:],
                "recent_rca_adjustments": self._rca_adjustments[-5:],
            }

    def get_summary_line(self) -> str:
        """One-line summary for status row display."""
        with self._lock:
            parts = []

            # Transitions
            t_total = sum(self._transition_counts.values())
            if t_total > 0:
                active = len(self._active_transitions)
                parts.append(f"TD: {t_total} signals ({active} active)")
            else:
                parts.append("TD: idle")

            # Coverage
            if self._cg_current_level > 0:
                level_names = {1: "INFO", 2: "EXPAND", 3: "ENRICH", 4: "NOTIFY"}
                parts.append(f"CG: L{self._cg_current_level} {level_names.get(self._cg_current_level, '?')}")
            else:
                parts.append("CG: normal")

            # RCA
            if self._rca_adjustment_count > 0:
                parts.append(f"RCA: {self._rca_adjustment_count} adj")
            else:
                parts.append("RCA: idle")

            return " | ".join(parts)


# ── Module-level singleton ─────────────────────────────────────
_tracker: Optional[Phase1MetricsTracker] = None


def get_phase1_metrics() -> Phase1MetricsTracker:
    """Get or create the singleton Phase1MetricsTracker."""
    global _tracker
    if _tracker is None:
        _tracker = Phase1MetricsTracker()
    return _tracker
