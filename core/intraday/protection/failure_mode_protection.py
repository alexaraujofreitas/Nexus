# ============================================================
# NEXUS TRADER — Failure Mode Protection  (Phase 5B Wave 2)
#
# Detects degrading system performance and applies graduated
# exposure reduction. Four detectors with three severity tiers.
#
# DETECTORS:
#   1. Loss clustering  — detects clustered losing trades
#   2. Drawdown acceleration — detects rapid drawdown increase
#   3. Consecutive losses — detects raw loss streaks
#   4. Regime mismatch degradation — detects model/regime misfit
#
# SEVERITY TIERS (progressive, no instant suspension):
#   WARNING   (>= 20 trades) → trace only, no action
#   DEGRADED  (>= 40 trades) → reduce exposure (0.8x multiplier)
#   SUSPENDED (>= 75 trades) → block new trades
#
# DETERMINISM:
#   - All state derived from event log (event-sourced)
#   - now_ms always injected, no wall-clock
#   - Same events → same state → same decisions
#   - Replay via replay(events)
#
# SAFETY:
#   - NEVER bypasses RiskEngine or 4%/6% caps
#   - Exposure reduction is advisory (multiplier on sizing)
#   - Trade blocking is a rejection in ProcessingEngine
#
# ZERO PySide6 imports.
# ============================================================
from __future__ import annotations

import json
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ── Constants ────────────────────────────────────────────────
_MS_PER_HOUR = 3_600_000
_MS_PER_DAY = 86_400_000
_EVENT_LOG_VERSION = 1
_RETENTION_WINDOW_MS = 7 * _MS_PER_DAY


# ── Severity Tiers ──────────────────────────────────────────

class FailureSeverity(str, Enum):
    """Progressive severity tiers. No instant suspension."""
    NORMAL = "normal"
    WARNING = "warning"
    DEGRADED = "degraded"
    SUSPENDED = "suspended"


# ── Detector IDs ────────────────────────────────────────────

class DetectorID(str, Enum):
    LOSS_CLUSTERING = "loss_clustering"
    DRAWDOWN_ACCELERATION = "drawdown_acceleration"
    CONSECUTIVE_LOSSES = "consecutive_losses"
    REGIME_MISMATCH = "regime_mismatch"


# ── Configuration ───────────────────────────────────────────

@dataclass(frozen=True)
class FailureModeConfig:
    """Configuration for all detectors and tier thresholds."""
    # ── Minimum trade counts for tier activation ──
    min_trades_warning: int = 20
    min_trades_degraded: int = 40
    min_trades_suspended: int = 75

    # ── Exposure multiplier for DEGRADED tier ──
    degraded_exposure_multiplier: float = 0.80

    # ── Loss clustering detector ──
    # Lookback window and threshold for clustered losses
    loss_cluster_lookback: int = 10       # trades
    loss_cluster_threshold: float = 0.70  # 70%+ losses in window → detect

    # ── Drawdown acceleration detector ──
    # Compare recent drawdown rate to baseline
    dd_accel_short_window_ms: int = 4 * _MS_PER_HOUR   # 4h recent
    dd_accel_long_window_ms: int = 24 * _MS_PER_HOUR    # 24h baseline
    dd_accel_ratio_threshold: float = 2.5  # Short/long > 2.5x → detect

    # ── Consecutive losses detector ──
    consec_loss_warning: int = 3    # 3 in a row → WARNING
    consec_loss_degraded: int = 5   # 5 in a row → DEGRADED
    consec_loss_suspended: int = 8  # 8 in a row → SUSPENDED

    # ── Regime mismatch degradation ──
    # Win rate in current regime vs overall
    regime_mismatch_lookback: int = 15  # trades per regime
    regime_mismatch_threshold: float = 0.20  # WR 20%+ below overall → detect


# ── Event Log ───────────────────────────────────────────────

@dataclass(frozen=True)
class FailureModeEvent:
    """Immutable record of a state-mutating event.

    Source of truth for replay.
    """
    event_type: str       # "trade_outcome" or "drawdown_sample"
    timestamp_ms: int

    # trade_outcome fields
    is_win: Optional[bool] = None
    strategy_class: str = ""
    regime: str = ""
    pnl_pct: float = 0.0      # PnL as % of capital at entry

    # drawdown_sample fields
    drawdown_pct: float = 0.0

    def to_dict(self) -> dict:
        d = {"event_type": self.event_type, "timestamp_ms": self.timestamp_ms}
        if self.event_type == "trade_outcome":
            d.update({
                "is_win": self.is_win,
                "strategy_class": self.strategy_class,
                "regime": self.regime,
                "pnl_pct": self.pnl_pct,
            })
        elif self.event_type == "drawdown_sample":
            d["drawdown_pct"] = self.drawdown_pct
        return d

    @classmethod
    def from_dict(cls, d: dict) -> FailureModeEvent:
        return cls(
            event_type=d["event_type"],
            timestamp_ms=d["timestamp_ms"],
            is_win=d.get("is_win"),
            strategy_class=d.get("strategy_class", ""),
            regime=d.get("regime", ""),
            pnl_pct=d.get("pnl_pct", 0.0),
            drawdown_pct=d.get("drawdown_pct", 0.0),
        )


# ── Detector Result ─────────────────────────────────────────

@dataclass(frozen=True)
class DetectorResult:
    """Result from a single detector."""
    detector_id: str
    triggered: bool
    severity: str         # FailureSeverity value
    confidence: float     # 0.0–1.0
    detail: str = ""

    def to_dict(self) -> dict:
        return {
            "detector_id": self.detector_id,
            "triggered": self.triggered,
            "severity": self.severity,
            "confidence": self.confidence,
            "detail": self.detail,
        }


# ── Aggregate Result (for PipelineContext) ───────────────────

@dataclass(frozen=True)
class FailureModeResult:
    """Immutable aggregate result from all detectors.

    Placed in PipelineContext for audit/traceability.
    """
    passed: bool                              # True = no blocking action
    severity: str                             # Current tier (from FailureSeverity)
    exposure_multiplier: float                # 1.0=normal, 0.8=degraded
    active_detectors: tuple                   # tuple of DetectorResult.to_dict()
    total_trade_count: int                    # Total trades in event log
    reason: str = ""

    def to_dict(self) -> dict:
        return {
            "failure_mode_passed": self.passed,
            "failure_mode_severity": self.severity,
            "exposure_multiplier": self.exposure_multiplier,
            "active_detectors": list(self.active_detectors),
            "total_trade_count": self.total_trade_count,
            "reason": self.reason,
        }


# ── State Snapshot (for persistence audit) ──────────────────

@dataclass(frozen=True)
class FailureModeSnapshot:
    """Frozen snapshot for audit trail."""
    severity: str
    total_trade_count: int
    consecutive_losses: int
    event_count: int
    detector_states: tuple     # tuple of (detector_id, triggered, severity)

    def to_dict(self) -> dict:
        return {
            "severity": self.severity,
            "total_trade_count": self.total_trade_count,
            "consecutive_losses": self.consecutive_losses,
            "event_count": self.event_count,
            "detector_states": list(self.detector_states),
        }


# ── Corrupt State Error ─────────────────────────────────────

class FailureModeStateCorruptError(Exception):
    """Fail-closed: persisted state cannot be safely restored."""
    pass


# ── Main Class ──────────────────────────────────────────────

class FailureModeProtection:
    """
    Event-sourced failure mode detector with progressive severity tiers.

    State management:
      - All state derived from event log (source of truth)
      - record_trade_outcome() and record_drawdown_sample() append events
      - evaluate() is read-only — checks state against thresholds
      - State reconstructable via replay(events)

    Concurrency: NOT thread-safe. Single-threaded pipeline.
    """

    def __init__(self, config: FailureModeConfig = None):
        self.config = config or FailureModeConfig()
        self._event_log: List[FailureModeEvent] = []

        # Derived state (rebuilt from events)
        self._trade_outcomes: List[dict] = []  # {is_win, strategy_class, regime, pnl_pct, ts}
        self._drawdown_samples: List[dict] = []  # {drawdown_pct, ts}
        self._consecutive_losses: int = 0

        logger.info(
            "FailureModeProtection initialized: tiers=[%d/%d/%d] degraded_mult=%.2f",
            self.config.min_trades_warning,
            self.config.min_trades_degraded,
            self.config.min_trades_suspended,
            self.config.degraded_exposure_multiplier,
        )

    @property
    def event_log(self) -> List[FailureModeEvent]:
        return list(self._event_log)

    @property
    def event_count(self) -> int:
        return len(self._event_log)

    @property
    def trade_count(self) -> int:
        return len(self._trade_outcomes)

    @property
    def consecutive_losses(self) -> int:
        return self._consecutive_losses

    # ── Evaluation (read-only) ──────────────────────────────

    def evaluate(self, now_ms: int) -> FailureModeResult:
        """
        Evaluate current system health against all detectors.

        READ-ONLY: does not mutate state.

        Returns FailureModeResult with:
          - passed: True if trade should proceed (WARNING or NORMAL)
          - severity: current tier
          - exposure_multiplier: 1.0 (normal/warning), 0.8 (degraded)
          - active_detectors: which detectors triggered
        """
        cfg = self.config
        n = len(self._trade_outcomes)

        # Run all detectors
        results = [
            self._detect_loss_clustering(now_ms),
            self._detect_drawdown_acceleration(now_ms),
            self._detect_consecutive_losses(now_ms),
            self._detect_regime_mismatch(now_ms),
        ]

        # Determine aggregate severity (worst triggered detector wins)
        triggered = [r for r in results if r.triggered]
        active_dicts = tuple(r.to_dict() for r in triggered)

        if not triggered:
            return FailureModeResult(
                passed=True,
                severity=FailureSeverity.NORMAL.value,
                exposure_multiplier=1.0,
                active_detectors=(),
                total_trade_count=n,
            )

        # Determine worst severity from triggered detectors
        severity_order = {
            FailureSeverity.NORMAL.value: 0,
            FailureSeverity.WARNING.value: 1,
            FailureSeverity.DEGRADED.value: 2,
            FailureSeverity.SUSPENDED.value: 3,
        }
        worst_severity = max(triggered, key=lambda r: severity_order.get(r.severity, 0))
        severity = worst_severity.severity

        # Apply tier actions
        if severity == FailureSeverity.SUSPENDED.value:
            # Block trades — but only if we have enough data
            if n < cfg.min_trades_suspended:
                severity = FailureSeverity.DEGRADED.value
            else:
                return FailureModeResult(
                    passed=False,
                    severity=severity,
                    exposure_multiplier=0.0,
                    active_detectors=active_dicts,
                    total_trade_count=n,
                    reason=f"SUSPENDED: {worst_severity.detail}",
                )

        if severity == FailureSeverity.DEGRADED.value:
            if n < cfg.min_trades_degraded:
                severity = FailureSeverity.WARNING.value
            else:
                return FailureModeResult(
                    passed=True,
                    severity=severity,
                    exposure_multiplier=cfg.degraded_exposure_multiplier,
                    active_detectors=active_dicts,
                    total_trade_count=n,
                    reason=f"DEGRADED: exposure reduced to {cfg.degraded_exposure_multiplier:.0%}",
                )

        # WARNING (or downgraded from above due to low trade count)
        if n < cfg.min_trades_warning:
            severity = FailureSeverity.NORMAL.value

        return FailureModeResult(
            passed=True,
            severity=severity,
            exposure_multiplier=1.0,
            active_detectors=active_dicts,
            total_trade_count=n,
            reason=f"WARNING: {worst_severity.detail}" if severity == FailureSeverity.WARNING.value else "",
        )

    # ── Detectors ───────────────────────────────────────────

    def _detect_loss_clustering(self, now_ms: int) -> DetectorResult:
        """Detect clustered losses in recent trade window."""
        cfg = self.config
        n = len(self._trade_outcomes)
        if n < cfg.loss_cluster_lookback:
            return DetectorResult(
                detector_id=DetectorID.LOSS_CLUSTERING.value,
                triggered=False, severity=FailureSeverity.NORMAL.value,
                confidence=0.0, detail=f"Insufficient data: {n}/{cfg.loss_cluster_lookback}",
            )

        recent = self._trade_outcomes[-cfg.loss_cluster_lookback:]
        loss_count = sum(1 for t in recent if not t["is_win"])
        loss_rate = loss_count / len(recent)

        if loss_rate < cfg.loss_cluster_threshold:
            return DetectorResult(
                detector_id=DetectorID.LOSS_CLUSTERING.value,
                triggered=False, severity=FailureSeverity.NORMAL.value,
                confidence=loss_rate / cfg.loss_cluster_threshold,
                detail=f"Loss rate {loss_rate:.1%} below threshold {cfg.loss_cluster_threshold:.1%}",
            )

        # Determine severity based on how far above threshold
        confidence = min(1.0, loss_rate / cfg.loss_cluster_threshold)
        if loss_rate >= 0.90:
            severity = FailureSeverity.SUSPENDED.value
        elif loss_rate >= cfg.loss_cluster_threshold + 0.10:
            severity = FailureSeverity.DEGRADED.value
        else:
            severity = FailureSeverity.WARNING.value

        return DetectorResult(
            detector_id=DetectorID.LOSS_CLUSTERING.value,
            triggered=True, severity=severity, confidence=confidence,
            detail=f"Loss clustering: {loss_count}/{len(recent)} = {loss_rate:.1%} >= {cfg.loss_cluster_threshold:.1%}",
        )

    def _detect_drawdown_acceleration(self, now_ms: int) -> DetectorResult:
        """Detect if recent drawdown rate exceeds baseline."""
        cfg = self.config
        if len(self._drawdown_samples) < 2:
            return DetectorResult(
                detector_id=DetectorID.DRAWDOWN_ACCELERATION.value,
                triggered=False, severity=FailureSeverity.NORMAL.value,
                confidence=0.0, detail="Insufficient drawdown samples",
            )

        short_cutoff = now_ms - cfg.dd_accel_short_window_ms
        long_cutoff = now_ms - cfg.dd_accel_long_window_ms

        short_samples = [s for s in self._drawdown_samples if s["ts"] >= short_cutoff]
        long_samples = [s for s in self._drawdown_samples if s["ts"] >= long_cutoff]

        if len(short_samples) < 1 or len(long_samples) < 2:
            return DetectorResult(
                detector_id=DetectorID.DRAWDOWN_ACCELERATION.value,
                triggered=False, severity=FailureSeverity.NORMAL.value,
                confidence=0.0, detail="Insufficient drawdown samples in window",
            )

        short_avg = sum(s["drawdown_pct"] for s in short_samples) / len(short_samples)
        long_avg = sum(s["drawdown_pct"] for s in long_samples) / len(long_samples)

        if long_avg <= 0.001:  # Avoid division by zero/near-zero
            return DetectorResult(
                detector_id=DetectorID.DRAWDOWN_ACCELERATION.value,
                triggered=False, severity=FailureSeverity.NORMAL.value,
                confidence=0.0, detail=f"Baseline drawdown near zero ({long_avg:.4f})",
            )

        ratio = short_avg / long_avg
        if ratio < cfg.dd_accel_ratio_threshold:
            return DetectorResult(
                detector_id=DetectorID.DRAWDOWN_ACCELERATION.value,
                triggered=False, severity=FailureSeverity.NORMAL.value,
                confidence=ratio / cfg.dd_accel_ratio_threshold,
                detail=f"DD accel ratio {ratio:.2f} below threshold {cfg.dd_accel_ratio_threshold:.1f}",
            )

        confidence = min(1.0, ratio / (cfg.dd_accel_ratio_threshold * 2))
        if ratio >= cfg.dd_accel_ratio_threshold * 2:
            severity = FailureSeverity.SUSPENDED.value
        elif ratio >= cfg.dd_accel_ratio_threshold * 1.5:
            severity = FailureSeverity.DEGRADED.value
        else:
            severity = FailureSeverity.WARNING.value

        return DetectorResult(
            detector_id=DetectorID.DRAWDOWN_ACCELERATION.value,
            triggered=True, severity=severity, confidence=confidence,
            detail=f"DD acceleration: ratio={ratio:.2f} (short={short_avg:.4f}, long={long_avg:.4f})",
        )

    def _detect_consecutive_losses(self, now_ms: int) -> DetectorResult:
        """Detect consecutive losing trades."""
        cfg = self.config
        streak = self._consecutive_losses

        if streak < cfg.consec_loss_warning:
            return DetectorResult(
                detector_id=DetectorID.CONSECUTIVE_LOSSES.value,
                triggered=False, severity=FailureSeverity.NORMAL.value,
                confidence=streak / max(cfg.consec_loss_warning, 1),
                detail=f"Consecutive losses: {streak}",
            )

        if streak >= cfg.consec_loss_suspended:
            severity = FailureSeverity.SUSPENDED.value
        elif streak >= cfg.consec_loss_degraded:
            severity = FailureSeverity.DEGRADED.value
        else:
            severity = FailureSeverity.WARNING.value

        confidence = min(1.0, streak / cfg.consec_loss_suspended)

        return DetectorResult(
            detector_id=DetectorID.CONSECUTIVE_LOSSES.value,
            triggered=True, severity=severity, confidence=confidence,
            detail=f"Consecutive losses: {streak} (w={cfg.consec_loss_warning}/d={cfg.consec_loss_degraded}/s={cfg.consec_loss_suspended})",
        )

    def _detect_regime_mismatch(self, now_ms: int) -> DetectorResult:
        """Detect regime-level win rate degradation vs overall."""
        cfg = self.config
        n = len(self._trade_outcomes)

        if n < cfg.regime_mismatch_lookback:
            return DetectorResult(
                detector_id=DetectorID.REGIME_MISMATCH.value,
                triggered=False, severity=FailureSeverity.NORMAL.value,
                confidence=0.0, detail=f"Insufficient data: {n}/{cfg.regime_mismatch_lookback}",
            )

        # Overall win rate
        overall_wins = sum(1 for t in self._trade_outcomes if t["is_win"])
        overall_wr = overall_wins / n

        # Per-regime win rates (only regimes with enough data)
        regime_stats: Dict[str, List[bool]] = defaultdict(list)
        for t in self._trade_outcomes:
            regime_stats[t["regime"]].append(t["is_win"])

        worst_gap = 0.0
        worst_regime = ""
        for regime, outcomes in regime_stats.items():
            if len(outcomes) < cfg.regime_mismatch_lookback:
                continue
            recent = outcomes[-cfg.regime_mismatch_lookback:]
            regime_wr = sum(1 for o in recent if o) / len(recent)
            gap = overall_wr - regime_wr
            if gap > worst_gap:
                worst_gap = gap
                worst_regime = regime

        if worst_gap < cfg.regime_mismatch_threshold:
            return DetectorResult(
                detector_id=DetectorID.REGIME_MISMATCH.value,
                triggered=False, severity=FailureSeverity.NORMAL.value,
                confidence=worst_gap / max(cfg.regime_mismatch_threshold, 0.01),
                detail=f"Max regime WR gap: {worst_gap:.1%} (regime={worst_regime or 'none'})",
            )

        confidence = min(1.0, worst_gap / (cfg.regime_mismatch_threshold * 2))
        if worst_gap >= cfg.regime_mismatch_threshold * 2:
            severity = FailureSeverity.DEGRADED.value
        else:
            severity = FailureSeverity.WARNING.value

        return DetectorResult(
            detector_id=DetectorID.REGIME_MISMATCH.value,
            triggered=True, severity=severity, confidence=confidence,
            detail=f"Regime mismatch: {worst_regime} WR gap={worst_gap:.1%} vs overall={overall_wr:.1%}",
        )

    # ── State Mutations (append to event log) ────────────────

    def record_trade_outcome(
        self, is_win: bool, strategy_class: str, regime: str,
        pnl_pct: float, now_ms: int,
    ) -> None:
        """Record a trade outcome. Appends event and updates derived state."""
        event = FailureModeEvent(
            event_type="trade_outcome", timestamp_ms=now_ms,
            is_win=is_win, strategy_class=strategy_class,
            regime=regime, pnl_pct=pnl_pct,
        )
        self._event_log.append(event)
        self._apply_trade_outcome(event)

    def record_drawdown_sample(self, drawdown_pct: float, now_ms: int) -> None:
        """Record a drawdown sample. Appends event and updates derived state."""
        event = FailureModeEvent(
            event_type="drawdown_sample", timestamp_ms=now_ms,
            drawdown_pct=drawdown_pct,
        )
        self._event_log.append(event)
        self._apply_drawdown_sample(event)

    # ── Internal state application ───────────────────────────

    def _apply_trade_outcome(self, event: FailureModeEvent) -> None:
        self._trade_outcomes.append({
            "is_win": event.is_win,
            "strategy_class": event.strategy_class,
            "regime": event.regime,
            "pnl_pct": event.pnl_pct,
            "ts": event.timestamp_ms,
        })
        if event.is_win:
            self._consecutive_losses = 0
        else:
            self._consecutive_losses += 1

    def _apply_drawdown_sample(self, event: FailureModeEvent) -> None:
        self._drawdown_samples.append({
            "drawdown_pct": event.drawdown_pct,
            "ts": event.timestamp_ms,
        })

    # ── Snapshot ─────────────────────────────────────────────

    def state_snapshot(self) -> FailureModeSnapshot:
        """Frozen snapshot for audit."""
        # Quick detector status
        now_ms = self._event_log[-1].timestamp_ms if self._event_log else 0
        detectors = []
        for det in [self._detect_loss_clustering, self._detect_drawdown_acceleration,
                     self._detect_consecutive_losses, self._detect_regime_mismatch]:
            r = det(now_ms)
            detectors.append((r.detector_id, r.triggered, r.severity))

        return FailureModeSnapshot(
            severity=self.evaluate(now_ms).severity if self._event_log else FailureSeverity.NORMAL.value,
            total_trade_count=len(self._trade_outcomes),
            consecutive_losses=self._consecutive_losses,
            event_count=len(self._event_log),
            detector_states=tuple(detectors),
        )

    # ── Replay / Persistence ─────────────────────────────────

    def replay(self, events: List[FailureModeEvent]) -> None:
        """Reconstruct state from event history."""
        self._event_log = []
        self._trade_outcomes = []
        self._drawdown_samples = []
        self._consecutive_losses = 0
        for event in events:
            self._event_log.append(event)
            if event.event_type == "trade_outcome":
                self._apply_trade_outcome(event)
            elif event.event_type == "drawdown_sample":
                self._apply_drawdown_sample(event)

    def truncate(self, now_ms: int) -> List[FailureModeEvent]:
        """Remove events older than retention window. Returns archived events."""
        cutoff_ms = now_ms - _RETENTION_WINDOW_MS
        archived = [e for e in self._event_log if e.timestamp_ms < cutoff_ms]
        kept = [e for e in self._event_log if e.timestamp_ms >= cutoff_ms]
        if archived:
            logger.info(
                "FailureModeProtection.truncate: archiving %d events, keeping %d",
                len(archived), len(kept),
            )
            self.replay(kept)
        return archived

    def to_json(self) -> str:
        """Serialise event log for persistence."""
        return json.dumps(
            {"version": _EVENT_LOG_VERSION,
             "events": [e.to_dict() for e in self._event_log]},
            sort_keys=True,
        )

    @classmethod
    def from_json(cls, data: str, config: FailureModeConfig = None) -> FailureModeProtection:
        """Restore from persisted JSON. Fail-closed on corruption."""
        try:
            parsed = json.loads(data)
        except (json.JSONDecodeError, TypeError) as e:
            raise FailureModeStateCorruptError(f"JSON corrupt: {e}") from e
        if not isinstance(parsed, dict):
            raise FailureModeStateCorruptError("Root must be object")
        if "version" not in parsed:
            raise FailureModeStateCorruptError("Missing 'version'")
        if parsed["version"] != _EVENT_LOG_VERSION:
            raise FailureModeStateCorruptError(f"Unknown version: {parsed['version']}")
        if "events" not in parsed or not isinstance(parsed["events"], list):
            raise FailureModeStateCorruptError("Missing or invalid 'events'")
        try:
            events = [FailureModeEvent.from_dict(e) for e in parsed["events"]]
        except (KeyError, TypeError, ValueError) as e:
            raise FailureModeStateCorruptError(f"Invalid event: {e}") from e
        f = cls(config=config)
        f.replay(events)
        return f
