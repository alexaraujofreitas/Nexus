# ============================================================
# NEXUS TRADER — Latency Monitor (Phase 6 v3)
#
# Dual-role component:
#
# ROLE 1: MODELING INPUT (deterministic, decision-critical)
#   - get_latency_estimate(symbol) → int [100, 30000] ms
#   - Per-symbol EMA of total pipeline latency
#   - Consumed by AdaptiveSlippageModel
#   - Participates in governed deterministic replay
#   - Must be deterministic from observation sequence
#   - Thread-safe via RLock
#
# ROLE 2: OBSERVABILITY OUTPUT (sidecar, non-critical)
#   - Alerts on threshold breaches
#   - Statistics/percentiles for dashboards
#   - NOT on decision path
#   - Cannot affect execution
#
# Pipeline stages tracked: SIGNAL_CREATED → PROCESSING_START → PROCESSING_END
#   → RISK_DECISION → EXECUTION_REQUEST → ORDER_SUBMITTED → FILL_RECEIVED
#
# Design invariants:
#   - No Qt/GUI imports
#   - All timestamps in milliseconds (Unix epoch)
#   - Thread-safe via RLock
#   - Deterministic latency estimates (same data → same estimate)
# ============================================================
import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Deque, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════
# PIPELINE STAGES
# ══════════════════════════════════════════════════════════════

class PipelineStage(str, Enum):
    """Ordered stages in the execution pipeline."""
    SIGNAL_CREATED    = "signal_created"
    PROCESSING_START  = "processing_start"
    PROCESSING_END    = "processing_end"
    RISK_DECISION     = "risk_decision"
    EXECUTION_REQUEST = "execution_request"
    ORDER_SUBMITTED   = "order_submitted"
    FILL_RECEIVED     = "fill_received"


# Ordered list for computing inter-stage latencies
_STAGE_ORDER = list(PipelineStage)
_STAGE_INDEX = {s: i for i, s in enumerate(_STAGE_ORDER)}


# ══════════════════════════════════════════════════════════════
# CONFIGURATION
# ══════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class LatencyConfig:
    """Latency monitoring configuration."""
    # ── Observability window ──
    window_size: int = 200

    # ── Modeling (per-symbol EMA) ──
    min_latency_observations: int = 5      # Minimum observations before estimate kicks in
    default_latency_ms: int = 2000         # Cold-start estimate (no observations yet)
    min_latency_ms: int = 100              # Lower bound for clamping
    max_latency_ms: int = 30000            # Upper bound for clamping
    latency_ema_alpha: float = 0.2         # EMA smoothing factor
    latency_window: int = 50               # Per-symbol rolling window size

    # ── Alert thresholds (ms) per inter-stage gap
    # Key: (from_stage, to_stage) name pair → max acceptable ms
    # These alerts are for operator dashboards ONLY — they do NOT
    # affect execution decisions
    stage_thresholds_ms: Dict[str, int] = field(default_factory=lambda: {
        "signal_created→processing_start": 500,
        "processing_start→processing_end": 2000,
        "processing_end→risk_decision": 500,
        "risk_decision→execution_request": 200,
        "execution_request→order_submitted": 100,
        "order_submitted→fill_received": 5000,
    })

    # ── Total pipeline threshold
    total_threshold_ms: int = 8000  # 8s signal-to-fill

    # ── Stale fill threshold (fill too late to be useful)
    stale_fill_ms: int = 30000  # 30s


# ══════════════════════════════════════════════════════════════
# LATENCY RECORD
# ══════════════════════════════════════════════════════════════

@dataclass
class LatencyRecord:
    """Complete timing record for a single trade's pipeline traversal."""
    trigger_id: str
    symbol: str
    strategy_class: str
    timestamps: Dict[str, int] = field(default_factory=dict)  # stage → ms

    @property
    def total_latency_ms(self) -> Optional[int]:
        """Total pipeline latency from signal to fill."""
        t0 = self.timestamps.get(PipelineStage.SIGNAL_CREATED.value)
        t1 = self.timestamps.get(PipelineStage.FILL_RECEIVED.value)
        if t0 is not None and t1 is not None:
            return t1 - t0
        return None

    def stage_latency(self, from_stage: PipelineStage, to_stage: PipelineStage) -> Optional[int]:
        """Compute latency between two stages."""
        t0 = self.timestamps.get(from_stage.value)
        t1 = self.timestamps.get(to_stage.value)
        if t0 is not None and t1 is not None:
            return t1 - t0
        return None

    def to_dict(self) -> dict:
        total = self.total_latency_ms
        return {
            "trigger_id": self.trigger_id,
            "symbol": self.symbol,
            "strategy_class": self.strategy_class,
            "timestamps": dict(self.timestamps),
            "total_latency_ms": total,
        }


# ══════════════════════════════════════════════════════════════
# LATENCY ALERT
# ══════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class LatencyAlert:
    """Alert emitted when latency exceeds threshold.

    OBSERVABILITY ONLY: these alerts are for dashboards and logging.
    They do NOT feed into any decision-making component.
    """
    trigger_id: str
    symbol: str
    stage_pair: str        # e.g. "signal_created→fill_received"
    latency_ms: int
    threshold_ms: int
    timestamp_ms: int
    is_stale: bool = False


# ══════════════════════════════════════════════════════════════
# LATENCY MONITOR
# ══════════════════════════════════════════════════════════════

class LatencyMonitor:
    """Dual-role latency tracking component.

    ROLE 1: MODELING INPUT (Deterministic, Decision-Critical)
    ─────────────────────────────────────────────────────────
    Provides get_latency_estimate(symbol) → int for:
      - AdaptiveSlippageModel consumption
      - Deterministic replay participation
      - Per-symbol historical latency EMA

    Contract: same observation sequence → same estimate.
    Output is bounded: [min_latency_ms, max_latency_ms].
    Cold-start: returns default_latency_ms until min_observations met.

    ROLE 2: OBSERVABILITY OUTPUT (Sidecar, Non-Critical)
    ────────────────────────────────────────────────────
    Provides:
      - Alert generation on threshold breaches
      - Statistics/percentiles for dashboards
      - Snapshots for operator awareness

    This role is NOT on the decision path. Alerts and statistics
    cannot affect execution. Threading is sidecar-safe via RLock.

    Thread Safety:
      - All public methods protected by RLock
      - get_latency_estimate() is deterministic from state
      - get_state() / restore_state() for replay persistence

    Usage (Modeling):
        est_ms = monitor.get_latency_estimate("BTCUSDT")  # → int
        # Use in AdaptiveSlippageModel

    Usage (Observability):
        monitor.record_stage(trigger_id, symbol, strategy, PipelineStage.SIGNAL_CREATED, ts)
        ...
        monitor.record_stage(trigger_id, symbol, strategy, PipelineStage.FILL_RECEIVED, ts)
        alerts = monitor.get_alerts(trigger_id)
        stats = monitor.get_statistics()
    """

    def __init__(self, config: LatencyConfig = None):
        self.config = config or LatencyConfig()
        self._lock = threading.RLock()

        # ── OBSERVABILITY STATE ──
        self._active: Dict[str, LatencyRecord] = {}  # trigger_id → record
        self._completed: Deque[LatencyRecord] = deque(maxlen=self.config.window_size)
        self._alerts: List[LatencyAlert] = []

        # Per-stage-pair rolling latencies for statistics
        self._stage_latencies: Dict[str, Deque[int]] = {}

        # ── MODELING STATE (Per-Symbol EMA) ──
        self._symbol_latencies: Dict[str, Deque[int]] = {}  # symbol → rolling window
        self._symbol_ema: Dict[str, float] = {}             # symbol → EMA state

        logger.info(
            "LatencyMonitor initialized (dual-role): "
            "window=%d, modeling_default=%dms, latency_window=%d, alpha=%.2f",
            self.config.window_size, self.config.default_latency_ms,
            self.config.latency_window, self.config.latency_ema_alpha,
        )

    # ═══════════════════════════════════════════════════════════
    # ROLE 1: MODELING INPUT (Deterministic)
    # ═══════════════════════════════════════════════════════════

    def get_latency_estimate(self, symbol: str) -> int:
        """Get estimated pipeline latency for a symbol (ms).

        DETERMINISTIC MODELING INPUT:
        This method is called by AdaptiveSlippageModel as a decision input.
        For the same symbol and historical record sequence, it always
        returns the same value.

        Algorithm:
          1. If fewer than min_latency_observations for symbol: return default_latency_ms
          2. Compute EMA of last N total_latency_ms values (from _symbol_latencies)
          3. Clamp to [min_latency_ms, max_latency_ms]
          4. Return as int

        Args:
            symbol: Trading symbol (e.g., "BTCUSDT")

        Returns:
            Estimated latency in milliseconds, bounded to [min, max] range.
        """
        with self._lock:
            # If insufficient observations, return cold-start default
            latency_deque = self._symbol_latencies.get(symbol)
            if latency_deque is None or len(latency_deque) < self.config.min_latency_observations:
                return self.config.default_latency_ms

            # Compute EMA from historical values
            ema = self._compute_ema(symbol, latency_deque)

            # Clamp to valid range
            clamped = max(self.config.min_latency_ms,
                         min(self.config.max_latency_ms, ema))

            return int(round(clamped))

    def _compute_ema(self, symbol: str, latency_values: Deque[int]) -> float:
        """Compute exponential moving average of latency values from scratch.

        DETERMINISTIC: Always recomputes from the full deque contents.
        No stored intermediate state is used — same deque → same result.
        The _symbol_ema cache is updated as a side effect for get_state().

        Args:
            symbol: Trading symbol
            latency_values: Deque of total_latency_ms values

        Returns:
            EMA as float (unbounded, will be clamped by caller)
        """
        if not latency_values:
            return float(self.config.default_latency_ms)

        alpha = self.config.latency_ema_alpha
        values = list(latency_values)

        # Initialize EMA with first value, then fold remaining
        ema = float(values[0])
        for lat_ms in values[1:]:
            ema = alpha * lat_ms + (1 - alpha) * ema

        # Cache for state persistence (not used in computation)
        self._symbol_ema[symbol] = ema

        return ema

    def get_state(self) -> dict:
        """Export latency estimation state for replay persistence.

        Returns dict containing:
          - _symbol_latencies: Dict[symbol] → list of latencies
          - _symbol_ema: Dict[symbol] → current EMA value

        This state can be restored after a system restart to maintain
        deterministic estimates based on prior observations.

        Returns:
            State dict suitable for JSON serialization
        """
        with self._lock:
            return {
                "symbol_latencies": {
                    symbol: list(deque_) for symbol, deque_ in self._symbol_latencies.items()
                },
                "symbol_ema": dict(self._symbol_ema),
            }

    def restore_state(self, state: dict) -> None:
        """Restore latency estimation state from persistence.

        Args:
            state: Dict from prior get_state() call
        """
        with self._lock:
            self._symbol_latencies.clear()
            self._symbol_ema.clear()

            # Restore symbol latency windows
            for symbol, lat_list in state.get("symbol_latencies", {}).items():
                window = deque(lat_list, maxlen=self.config.latency_window)
                self._symbol_latencies[symbol] = window

            # Restore EMA state
            self._symbol_ema = dict(state.get("symbol_ema", {}))

            logger.info(
                "LatencyMonitor: restored state for %d symbols",
                len(self._symbol_latencies),
            )

    # ═══════════════════════════════════════════════════════════
    # ROLE 2: OBSERVABILITY OUTPUT (Sidecar)
    # ═══════════════════════════════════════════════════════════

    def record_stage(
        self,
        trigger_id: str,
        symbol: str,
        strategy_class: str,
        stage: PipelineStage,
        timestamp_ms: int = None,
    ) -> Optional[LatencyAlert]:
        """Record a pipeline stage timestamp. Returns alert if threshold breached.

        This method advances both observability and modeling state.
        When a record completes (FILL_RECEIVED), the total_latency_ms
        is added to the symbol's latency deque for EMA computation.

        OBSERVABILITY ONLY (returned alert): for logging/dashboards.
        The caller MUST NOT use the alert to affect execution decisions.

        Args:
            trigger_id: Unique trigger identifier
            symbol: Trading symbol
            strategy_class: Strategy class name
            stage: Pipeline stage being recorded
            timestamp_ms: Timestamp in ms. Defaults to now.

        Returns:
            LatencyAlert if a threshold was breached, else None
        """
        if timestamp_ms is None:
            timestamp_ms = int(time.time() * 1000)

        with self._lock:
            # Get or create record
            if trigger_id not in self._active:
                self._active[trigger_id] = LatencyRecord(
                    trigger_id=trigger_id,
                    symbol=symbol,
                    strategy_class=strategy_class,
                )
            record = self._active[trigger_id]
            record.timestamps[stage.value] = timestamp_ms

            # Check inter-stage thresholds (observability only)
            alert = self._check_thresholds(record, stage, timestamp_ms)

            # If this is the final stage, finalize and update modeling state
            if stage == PipelineStage.FILL_RECEIVED:
                self._finalize(record)

            return alert

    def get_record(self, trigger_id: str) -> Optional[LatencyRecord]:
        """Get the latency record for a trigger."""
        with self._lock:
            if trigger_id in self._active:
                return self._active[trigger_id]
            for r in self._completed:
                if r.trigger_id == trigger_id:
                    return r
            return None

    def get_alerts(self, trigger_id: str = None) -> List[LatencyAlert]:
        """Get alerts, optionally filtered by trigger_id.

        OBSERVABILITY ONLY: For dashboards and logging.
        """
        with self._lock:
            if trigger_id:
                return [a for a in self._alerts if a.trigger_id == trigger_id]
            return list(self._alerts)

    def get_statistics(self) -> dict:
        """Get aggregate latency statistics across completed records.

        OBSERVABILITY ONLY: For dashboards.
        """
        with self._lock:
            stats = {}
            for pair, latencies in self._stage_latencies.items():
                if not latencies:
                    continue
                sorted_lats = sorted(latencies)
                n = len(sorted_lats)
                stats[pair] = {
                    "count": n,
                    "mean_ms": sum(sorted_lats) / n,
                    "median_ms": sorted_lats[n // 2],
                    "p95_ms": sorted_lats[int(n * 0.95)] if n >= 20 else sorted_lats[-1],
                    "p99_ms": sorted_lats[int(n * 0.99)] if n >= 100 else sorted_lats[-1],
                    "max_ms": sorted_lats[-1],
                }

            # Total pipeline stats
            totals = [
                r.total_latency_ms for r in self._completed
                if r.total_latency_ms is not None
            ]
            if totals:
                sorted_t = sorted(totals)
                n = len(sorted_t)
                stats["total_pipeline"] = {
                    "count": n,
                    "mean_ms": sum(sorted_t) / n,
                    "median_ms": sorted_t[n // 2],
                    "p95_ms": sorted_t[int(n * 0.95)] if n >= 20 else sorted_t[-1],
                    "p99_ms": sorted_t[int(n * 0.99)] if n >= 100 else sorted_t[-1],
                    "max_ms": sorted_t[-1],
                }

            return stats

    def snapshot(self) -> dict:
        """Full audit snapshot for dashboards.

        OBSERVABILITY ONLY.
        """
        with self._lock:
            return {
                "active_count": len(self._active),
                "completed_count": len(self._completed),
                "alert_count": len(self._alerts),
                "symbol_estimates_count": len(self._symbol_latencies),
                "statistics": self.get_statistics(),
            }

    def cleanup_stale(self, max_age_ms: int = 300_000) -> int:
        """Remove active records older than max_age_ms. Returns count removed."""
        now_ms = int(time.time() * 1000)
        with self._lock:
            stale_ids = [
                tid for tid, rec in self._active.items()
                if (now_ms - min(rec.timestamps.values(), default=now_ms)) > max_age_ms
            ]
            for tid in stale_ids:
                del self._active[tid]
            if stale_ids:
                logger.info("LatencyMonitor: cleaned %d stale records", len(stale_ids))
            return len(stale_ids)

    # ── Internal ─────────────────────────────────────────────

    def _check_thresholds(
        self, record: LatencyRecord, stage: PipelineStage, now_ms: int,
    ) -> Optional[LatencyAlert]:
        """Check if any threshold was breached by this stage recording.

        OBSERVABILITY ONLY: returned alert is for logging/dashboards.
        """
        cfg = self.config
        stage_idx = _STAGE_INDEX[stage]

        # Check against all preceding stages
        for prev_stage in _STAGE_ORDER[:stage_idx]:
            prev_ts = record.timestamps.get(prev_stage.value)
            if prev_ts is None:
                continue

            pair_key = f"{prev_stage.value}→{stage.value}"
            latency = now_ms - prev_ts
            threshold = cfg.stage_thresholds_ms.get(pair_key)

            if threshold is not None and latency > threshold:
                alert = LatencyAlert(
                    trigger_id=record.trigger_id,
                    symbol=record.symbol,
                    stage_pair=pair_key,
                    latency_ms=latency,
                    threshold_ms=threshold,
                    timestamp_ms=now_ms,
                    is_stale=(latency > cfg.stale_fill_ms),
                )
                self._alerts.append(alert)
                logger.warning(
                    "Latency alert (OBSERVABILITY): %s %s %dms > %dms threshold%s",
                    record.trigger_id, pair_key, latency, threshold,
                    " [STALE]" if alert.is_stale else "",
                )
                return alert

        # Check total pipeline if fill arrived
        if stage == PipelineStage.FILL_RECEIVED:
            total = record.total_latency_ms
            if total is not None and total > cfg.total_threshold_ms:
                alert = LatencyAlert(
                    trigger_id=record.trigger_id,
                    symbol=record.symbol,
                    stage_pair="total_pipeline",
                    latency_ms=total,
                    threshold_ms=cfg.total_threshold_ms,
                    timestamp_ms=now_ms,
                    is_stale=(total > cfg.stale_fill_ms),
                )
                self._alerts.append(alert)
                logger.warning(
                    "Pipeline latency alert (OBSERVABILITY): %s total=%dms > %dms%s",
                    record.trigger_id, total, cfg.total_threshold_ms,
                    " [STALE]" if alert.is_stale else "",
                )
                return alert

        return None

    def _finalize(self, record: LatencyRecord) -> None:
        """Move record to completed and update both observability and modeling state.

        Updates:
          - _stage_latencies for inter-stage statistics
          - _symbol_latencies and _symbol_ema for per-symbol EMA (modeling)
        """
        # ── Observability: compute and store inter-stage latencies ──
        for i in range(len(_STAGE_ORDER) - 1):
            s1, s2 = _STAGE_ORDER[i], _STAGE_ORDER[i + 1]
            lat = record.stage_latency(s1, s2)
            if lat is not None:
                pair_key = f"{s1.value}→{s2.value}"
                if pair_key not in self._stage_latencies:
                    self._stage_latencies[pair_key] = deque(
                        maxlen=self.config.window_size
                    )
                self._stage_latencies[pair_key].append(lat)

        # ── Modeling: update per-symbol latency window ──
        total_lat = record.total_latency_ms
        if total_lat is not None:
            symbol = record.symbol
            if symbol not in self._symbol_latencies:
                self._symbol_latencies[symbol] = deque(
                    maxlen=self.config.latency_window
                )
            self._symbol_latencies[symbol].append(total_lat)
            # Note: EMA update happens on demand in get_latency_estimate()

        # Move to completed
        self._completed.append(record)
        trigger_id = record.trigger_id
        if trigger_id in self._active:
            del self._active[trigger_id]

        logger.debug(
            "LatencyMonitor: finalized %s (%s), total=%s ms",
            trigger_id, record.symbol, record.total_latency_ms,
        )
