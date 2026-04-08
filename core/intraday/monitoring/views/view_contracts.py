"""
Phase 9: View Contracts — Frozen Dataclass View Models

All view models are immutable (frozen=True).
UI pages consume these — never raw engine objects.
Replay-safe: identical inputs → identical views.

No Qt imports. No execution imports. Pure data.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional


# ══════════════════════════════════════════════════════════════
# 1. ALERT SEVERITY
# ══════════════════════════════════════════════════════════════

class AlertSeverity(str, Enum):
    """Severity levels for monitoring alerts."""
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"
    EMERGENCY = "emergency"


# ══════════════════════════════════════════════════════════════
# 2. ALERT RECORD
# ══════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class AlertRecord:
    """
    Immutable alert record. Event-driven, reason-coded, timestamped.

    Every alert carries:
    - unique ID for dedup
    - reason code for programmatic handling
    - human-readable message
    - severity for UI color-coding
    - source component for attribution
    """
    alert_id: str
    timestamp_ms: int
    severity: AlertSeverity
    reason_code: str          # e.g., "kill_switch_activated", "reconciliation_failure"
    message: str              # Human-readable description
    source: str               # Component that raised it (e.g., "circuit_breaker")
    symbol: str = ""          # Symbol context (if applicable)
    metadata: str = ""        # JSON string for extra context

    def to_dict(self) -> dict:
        return {
            "alert_id": self.alert_id,
            "timestamp_ms": self.timestamp_ms,
            "severity": self.severity.value,
            "reason_code": self.reason_code,
            "message": self.message,
            "source": self.source,
            "symbol": self.symbol,
            "metadata": self.metadata,
        }


# ══════════════════════════════════════════════════════════════
# 3. TRADE VIEW
# ══════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class TradeView:
    """
    Read-only projection of a closed trade.
    Sourced from TradeRecord / PositionRecord.
    """
    position_id: str
    symbol: str
    direction: str             # "long" or "short"
    strategy_name: str
    entry_price: float
    exit_price: float
    quantity: float
    entry_size_usdt: float
    realized_pnl_usdt: float
    fee_total_usdt: float
    r_multiple: float
    close_reason: str
    regime_at_entry: str
    opened_at_ms: int
    closed_at_ms: int
    duration_ms: int
    bars_held: int
    slippage_pct: float = 0.0

    @property
    def is_winner(self) -> bool:
        return self.realized_pnl_usdt > 0

    @property
    def net_pnl_usdt(self) -> float:
        return self.realized_pnl_usdt - self.fee_total_usdt

    def to_dict(self) -> dict:
        return {
            "position_id": self.position_id,
            "symbol": self.symbol,
            "direction": self.direction,
            "strategy_name": self.strategy_name,
            "entry_price": self.entry_price,
            "exit_price": self.exit_price,
            "quantity": self.quantity,
            "entry_size_usdt": self.entry_size_usdt,
            "realized_pnl_usdt": self.realized_pnl_usdt,
            "fee_total_usdt": self.fee_total_usdt,
            "r_multiple": self.r_multiple,
            "close_reason": self.close_reason,
            "regime_at_entry": self.regime_at_entry,
            "opened_at_ms": self.opened_at_ms,
            "closed_at_ms": self.closed_at_ms,
            "duration_ms": self.duration_ms,
            "bars_held": self.bars_held,
            "slippage_pct": self.slippage_pct,
        }


# ══════════════════════════════════════════════════════════════
# 4. POSITION VIEW
# ══════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class PositionView:
    """
    Read-only projection of an open position.
    Sourced from PortfolioState.get_snapshot().
    """
    position_id: str
    symbol: str
    direction: str
    strategy_name: str
    entry_price: float
    current_price: float
    quantity: float
    entry_size_usdt: float
    current_size_usdt: float
    unrealized_pnl_usdt: float
    unrealized_pnl_pct: float
    stop_loss: float
    take_profit: float
    regime_at_entry: str
    opened_at_ms: int
    bars_held: int
    auto_partial_applied: bool = False
    breakeven_applied: bool = False

    @property
    def risk_usdt(self) -> float:
        """Dollar risk from entry to stop."""
        risk_per_unit = abs(self.entry_price - self.stop_loss)
        return risk_per_unit * self.quantity

    @property
    def current_r(self) -> float:
        """Current R-multiple (unrealized)."""
        risk_per_unit = abs(self.entry_price - self.stop_loss)
        if risk_per_unit <= 0:
            return 0.0
        if self.direction == "long":
            move = self.current_price - self.entry_price
        else:
            move = self.entry_price - self.current_price
        return move / risk_per_unit

    def to_dict(self) -> dict:
        return {
            "position_id": self.position_id,
            "symbol": self.symbol,
            "direction": self.direction,
            "strategy_name": self.strategy_name,
            "entry_price": self.entry_price,
            "current_price": self.current_price,
            "quantity": self.quantity,
            "entry_size_usdt": self.entry_size_usdt,
            "current_size_usdt": self.current_size_usdt,
            "unrealized_pnl_usdt": self.unrealized_pnl_usdt,
            "unrealized_pnl_pct": self.unrealized_pnl_pct,
            "stop_loss": self.stop_loss,
            "take_profit": self.take_profit,
            "regime_at_entry": self.regime_at_entry,
            "opened_at_ms": self.opened_at_ms,
            "bars_held": self.bars_held,
            "auto_partial_applied": self.auto_partial_applied,
            "breakeven_applied": self.breakeven_applied,
            "risk_usdt": self.risk_usdt,
            "current_r": self.current_r,
        }


# ══════════════════════════════════════════════════════════════
# 5. HEALTH VIEW
# ══════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class HealthView:
    """
    Read-only projection of system health state.
    Aggregates kill switch, circuit breaker, failure mode, recovery.
    """
    # Kill switch
    kill_switch_state: str       # "armed" or "disarmed"
    kill_switch_reason: str = ""
    kill_switch_at_ms: int = 0

    # Circuit breaker
    circuit_breaker_state: str = "normal"  # "normal", "warning", "tripped"
    circuit_breaker_tripped_at_ms: int = 0

    # Failure mode
    failure_mode_tier: str = "normal"      # "normal", "warning", "degraded", "suspended"
    failure_mode_detectors: int = 0        # Number of active detectors

    # Recovery (Phase 8)
    recovery_complete: bool = True
    recovery_trading_allowed: bool = True
    last_reconciliation_clean: bool = True
    last_reconciliation_mismatches: int = 0

    # Edge validity
    edge_states: Dict[str, str] = field(default_factory=dict)
    # e.g., {"MX": "normal", "MPC": "warning"}

    # Overall health score
    @property
    def overall_status(self) -> str:
        """Compute overall system status from components."""
        if self.kill_switch_state == "disarmed":
            return "halted"
        if self.circuit_breaker_state == "tripped":
            return "circuit_breaker_tripped"
        if not self.recovery_trading_allowed:
            return "recovery_pending"
        if self.failure_mode_tier == "suspended":
            return "suspended"
        if self.failure_mode_tier == "degraded":
            return "degraded"
        if self.circuit_breaker_state == "warning":
            return "warning"
        if self.failure_mode_tier == "warning":
            return "warning"
        return "healthy"

    def to_dict(self) -> dict:
        return {
            "overall_status": self.overall_status,
            "kill_switch_state": self.kill_switch_state,
            "kill_switch_reason": self.kill_switch_reason,
            "kill_switch_at_ms": self.kill_switch_at_ms,
            "circuit_breaker_state": self.circuit_breaker_state,
            "circuit_breaker_tripped_at_ms": self.circuit_breaker_tripped_at_ms,
            "failure_mode_tier": self.failure_mode_tier,
            "failure_mode_detectors": self.failure_mode_detectors,
            "recovery_complete": self.recovery_complete,
            "recovery_trading_allowed": self.recovery_trading_allowed,
            "last_reconciliation_clean": self.last_reconciliation_clean,
            "last_reconciliation_mismatches": self.last_reconciliation_mismatches,
            "edge_states": dict(self.edge_states),
        }


# ══════════════════════════════════════════════════════════════
# 6. METRICS VIEW
# ══════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class SlippageMetrics:
    """Slippage metrics for a single symbol."""
    symbol: str
    mean_slippage_bps: float
    median_slippage_bps: float
    p75_slippage_bps: float
    stddev_slippage_bps: float
    observation_count: int
    is_degraded: bool

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "mean_slippage_bps": self.mean_slippage_bps,
            "median_slippage_bps": self.median_slippage_bps,
            "p75_slippage_bps": self.p75_slippage_bps,
            "stddev_slippage_bps": self.stddev_slippage_bps,
            "observation_count": self.observation_count,
            "is_degraded": self.is_degraded,
        }


@dataclass(frozen=True)
class LatencyMetrics:
    """Pipeline latency metrics for a single symbol."""
    symbol: str
    ema_total_ms: int
    p50_ms: int = 0
    p75_ms: int = 0
    p90_ms: int = 0
    p99_ms: int = 0
    observation_count: int = 0
    alerts: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "ema_total_ms": self.ema_total_ms,
            "p50_ms": self.p50_ms,
            "p75_ms": self.p75_ms,
            "p90_ms": self.p90_ms,
            "p99_ms": self.p99_ms,
            "observation_count": self.observation_count,
            "alerts": list(self.alerts),
        }


@dataclass(frozen=True)
class MetricsView:
    """
    Read-only projection of execution quality metrics.
    Aggregates slippage and latency across all symbols.
    """
    slippage_by_symbol: Dict[str, SlippageMetrics] = field(default_factory=dict)
    latency_by_symbol: Dict[str, LatencyMetrics] = field(default_factory=dict)
    total_observations: int = 0
    degraded_symbols: List[str] = field(default_factory=list)
    latency_alerts: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "slippage_by_symbol": {
                k: v.to_dict() for k, v in self.slippage_by_symbol.items()
            },
            "latency_by_symbol": {
                k: v.to_dict() for k, v in self.latency_by_symbol.items()
            },
            "total_observations": self.total_observations,
            "degraded_symbols": list(self.degraded_symbols),
            "latency_alerts": list(self.latency_alerts),
        }


# ══════════════════════════════════════════════════════════════
# 7. MONITORING SNAPSHOT
# ══════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class DecisionView:
    """Read-only projection of an execution decision (accepted or rejected)."""
    decision_id: str
    intent_id: str
    symbol: str
    direction: str
    strategy_name: str
    status: str              # "approved" or "rejected"
    rejection_reason: str = ""
    rejection_source: str = ""
    risk_scaling: float = 1.0
    final_size_usdt: float = 0.0
    timestamp_ms: int = 0

    def to_dict(self) -> dict:
        return {
            "decision_id": self.decision_id,
            "intent_id": self.intent_id,
            "symbol": self.symbol,
            "direction": self.direction,
            "strategy_name": self.strategy_name,
            "status": self.status,
            "rejection_reason": self.rejection_reason,
            "rejection_source": self.rejection_source,
            "risk_scaling": self.risk_scaling,
            "final_size_usdt": self.final_size_usdt,
            "timestamp_ms": self.timestamp_ms,
        }


@dataclass(frozen=True)
class MonitoringSnapshot:
    """
    Complete read-only snapshot of system state at a point in time.
    Top-level view consumed by UI pages.
    """
    timestamp_ms: int
    health: HealthView
    open_positions: List[PositionView] = field(default_factory=list)
    recent_trades: List[TradeView] = field(default_factory=list)
    recent_decisions: List[DecisionView] = field(default_factory=list)
    metrics: Optional[MetricsView] = None
    recent_alerts: List[AlertRecord] = field(default_factory=list)

    # Summary stats
    total_open_positions: int = 0
    total_unrealized_pnl: float = 0.0
    total_exposure_usdt: float = 0.0
    total_capital_usdt: float = 0.0
    available_capital_usdt: float = 0.0

    def to_dict(self) -> dict:
        return {
            "timestamp_ms": self.timestamp_ms,
            "health": self.health.to_dict(),
            "open_positions": [p.to_dict() for p in self.open_positions],
            "recent_trades": [t.to_dict() for t in self.recent_trades],
            "recent_decisions": [d.to_dict() for d in self.recent_decisions],
            "metrics": self.metrics.to_dict() if self.metrics else None,
            "recent_alerts": [a.to_dict() for a in self.recent_alerts],
            "total_open_positions": self.total_open_positions,
            "total_unrealized_pnl": self.total_unrealized_pnl,
            "total_exposure_usdt": self.total_exposure_usdt,
            "total_capital_usdt": self.total_capital_usdt,
            "available_capital_usdt": self.available_capital_usdt,
        }
