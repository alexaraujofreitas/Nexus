"""
Phase 9: Monitoring API — Unified Read-Only Access Layer

Single entry point for all monitoring data. UI pages call this
instead of reaching into engine internals.

STRICT OBSERVER CONTRACT:
- Every method returns frozen/immutable view objects
- No method accepts mutable references or callbacks
- No method writes to any upstream component
- No method participates in trading decisions
- All data flows are read-only projections

DATA SOURCES (read-only access):
- PortfolioState.get_snapshot() → positions, capital
- TradeMonitor → closed trade history
- KillSwitch.get_status() → armed/disarmed
- CircuitBreaker.get_status() → normal/warning/tripped
- FailureModeProtection state → tier
- RestartRecoveryManager.get_state() → recovery status
- ExecutionQualityTracker.get_quality_stats() → slippage
- LatencyMonitor.get_percentiles() → latency
- PerformanceEngine.analyze() → PerformanceReport
- AlertManager → recent alerts

No Qt imports. No PySide6. Pure Python.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable, Dict, List, Optional

from .view_contracts import (
    AlertRecord,
    AlertSeverity,
    DecisionView,
    HealthView,
    MetricsView,
    MonitoringSnapshot,
    PositionView,
    TradeView,
)
from .trades_view import TradesViewBuilder
from .positions_view import PositionsViewBuilder
from .health_view import HealthViewBuilder
from .metrics_view import MetricsViewBuilder
from .alert_manager import AlertManager

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════
# DATA SOURCE PROTOCOL
# ══════════════════════════════════════════════════════════════

class MonitoringDataSource:
    """
    Protocol defining read-only data accessors.

    Implementations wrap actual engine components and expose
    only their read-only state via simple dicts/lists.

    This decouples the monitoring layer from engine internals.
    The UI never touches engine objects directly.
    """

    def get_open_positions(self) -> List[Dict[str, Any]]:
        """Return list of open position dicts."""
        return []

    def get_closed_trades(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Return list of closed trade dicts."""
        return []

    def get_recent_decisions(self, limit: int = 20) -> List[Dict[str, Any]]:
        """Return list of execution decision dicts."""
        return []

    def get_kill_switch_status(self) -> Dict[str, Any]:
        """Return kill switch status dict."""
        return {"state": "armed"}

    def get_circuit_breaker_status(self) -> Dict[str, Any]:
        """Return circuit breaker status dict."""
        return {"state": "normal"}

    def get_failure_mode_state(self) -> Dict[str, Any]:
        """Return failure mode protection state dict."""
        return {"tier": "normal"}

    def get_recovery_state(self) -> Dict[str, Any]:
        """Return recovery manager state dict."""
        return {"recovery_complete": True, "trading_allowed": True}

    def get_edge_states(self) -> Dict[str, str]:
        """Return edge validity states: {strategy_class → state}."""
        return {}

    def get_quality_stats(self) -> Dict[str, Dict[str, Any]]:
        """Return execution quality stats: {symbol → stats_dict}."""
        return {}

    def get_latency_data(self) -> Dict[str, Dict[str, Any]]:
        """Return latency data: {symbol → latency_dict}."""
        return {}

    def get_capital_snapshot(self) -> Dict[str, Any]:
        """Return capital snapshot dict."""
        return {"total_capital": 0, "available_capital": 0, "equity": 0}


class DictDataSource(MonitoringDataSource):
    """
    Simple implementation backed by getter callables.

    Usage:
        source = DictDataSource(
            open_positions_fn=lambda: portfolio.get_open_position_dicts(),
            kill_switch_fn=lambda: kill_switch.get_status(),
            ...
        )
        api = MonitoringAPI(source)
    """

    def __init__(self, **kwargs):
        """
        Accept keyword arguments mapping method names to callables.

        Supported keys: open_positions_fn, closed_trades_fn,
        recent_decisions_fn, kill_switch_fn, circuit_breaker_fn,
        failure_mode_fn, recovery_fn, edge_states_fn,
        quality_stats_fn, latency_data_fn, capital_snapshot_fn.
        """
        self._fns = kwargs

    def _call(self, key: str, *args, **kwargs):
        fn = self._fns.get(key)
        if fn is None:
            return None
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            logger.warning(f"DictDataSource.{key}: {e}")
            return None

    def get_open_positions(self) -> List[Dict[str, Any]]:
        return self._call("open_positions_fn") or []

    def get_closed_trades(self, limit: int = 50) -> List[Dict[str, Any]]:
        fn = self._fns.get("closed_trades_fn")
        if fn is None:
            return []
        try:
            return fn(limit)
        except TypeError:
            try:
                return fn()
            except Exception:
                return []

    def get_recent_decisions(self, limit: int = 20) -> List[Dict[str, Any]]:
        fn = self._fns.get("recent_decisions_fn")
        if fn is None:
            return []
        try:
            return fn(limit)
        except TypeError:
            try:
                return fn()
            except Exception:
                return []

    def get_kill_switch_status(self) -> Dict[str, Any]:
        return self._call("kill_switch_fn") or {"state": "armed"}

    def get_circuit_breaker_status(self) -> Dict[str, Any]:
        return self._call("circuit_breaker_fn") or {"state": "normal"}

    def get_failure_mode_state(self) -> Dict[str, Any]:
        return self._call("failure_mode_fn") or {"tier": "normal"}

    def get_recovery_state(self) -> Dict[str, Any]:
        return self._call("recovery_fn") or {"recovery_complete": True, "trading_allowed": True}

    def get_edge_states(self) -> Dict[str, str]:
        return self._call("edge_states_fn") or {}

    def get_quality_stats(self) -> Dict[str, Dict[str, Any]]:
        return self._call("quality_stats_fn") or {}

    def get_latency_data(self) -> Dict[str, Dict[str, Any]]:
        return self._call("latency_data_fn") or {}

    def get_capital_snapshot(self) -> Dict[str, Any]:
        return self._call("capital_snapshot_fn") or {"total_capital": 0, "available_capital": 0}


# ══════════════════════════════════════════════════════════════
# MONITORING API
# ══════════════════════════════════════════════════════════════

class MonitoringAPI:
    """
    Unified read-only API for all monitoring data.

    UI pages call this. Never touches engine internals.

    Methods:
    - get_trades() → List[TradeView]
    - get_positions() → List[PositionView]
    - get_health() → HealthView
    - get_metrics() → MetricsView
    - get_alerts() → List[AlertRecord]
    - get_snapshot() → MonitoringSnapshot (everything at once)
    - get_decisions() → List[DecisionView]
    """

    def __init__(
        self,
        data_source: MonitoringDataSource,
        alert_manager: Optional[AlertManager] = None,
        now_ms_fn=None,
    ):
        """
        Args:
            data_source: Read-only data accessor.
            alert_manager: AlertManager instance (shared with event wiring).
            now_ms_fn: Optional time function for testing.
        """
        self._source = data_source
        self._alerts = alert_manager or AlertManager(now_ms_fn=now_ms_fn)
        self._now_ms_fn = now_ms_fn or (lambda: int(time.time() * 1000))

        logger.info("MonitoringAPI initialized")

    # ── Individual Views ──────────────────────────────────────

    def get_trades(self, limit: int = 50) -> List[TradeView]:
        """Get closed trade views, most recent first."""
        records = self._source.get_closed_trades(limit)
        return TradesViewBuilder.build_from_records(records)

    def get_trade_summary(self, limit: int = 50) -> Dict[str, Any]:
        """Get trade summary statistics."""
        trades = self.get_trades(limit)
        return TradesViewBuilder.compute_summary(trades)

    def get_positions(self) -> List[PositionView]:
        """Get open position views."""
        records = self._source.get_open_positions()
        return PositionsViewBuilder.build_from_records(records)

    def get_exposure(self) -> Dict[str, Any]:
        """Get exposure summary from positions."""
        positions = self.get_positions()
        return PositionsViewBuilder.compute_exposure(positions)

    def get_health(self) -> HealthView:
        """Get system health view."""
        return HealthViewBuilder.build(
            kill_switch_status=self._source.get_kill_switch_status(),
            circuit_breaker_status=self._source.get_circuit_breaker_status(),
            failure_mode_state=self._source.get_failure_mode_state(),
            recovery_state=self._source.get_recovery_state(),
            edge_states=self._source.get_edge_states(),
        )

    def get_metrics(self) -> MetricsView:
        """Get execution quality metrics view."""
        return MetricsViewBuilder.build(
            quality_stats=self._source.get_quality_stats(),
            latency_data=self._source.get_latency_data(),
        )

    def get_decisions(self, limit: int = 20) -> List[DecisionView]:
        """Get recent execution decisions (approved + rejected)."""
        records = self._source.get_recent_decisions(limit)
        views = []
        for rec in records:
            try:
                views.append(DecisionView(
                    decision_id=str(rec.get("decision_id", "")),
                    intent_id=str(rec.get("intent_id", "")),
                    symbol=str(rec.get("symbol", "")),
                    direction=str(rec.get("direction", "")),
                    strategy_name=str(rec.get("strategy_name", "")),
                    status=str(rec.get("status", "")),
                    rejection_reason=str(rec.get("rejection_reason", "")),
                    rejection_source=str(rec.get("rejection_source", "")),
                    risk_scaling=float(rec.get("risk_scaling_applied", 1.0) or 1.0),
                    final_size_usdt=float(rec.get("final_size_usdt", 0) or 0),
                    timestamp_ms=int(rec.get("created_at_ms", 0) or 0),
                ))
            except Exception as e:
                logger.warning(f"MonitoringAPI: skipping malformed decision: {e}")
        return views

    def get_alerts(self, limit: int = 50) -> List[AlertRecord]:
        """Get recent alerts, newest first."""
        return self._alerts.get_recent_alerts(limit)

    # ── Full Snapshot ─────────────────────────────────────────

    def get_snapshot(self) -> MonitoringSnapshot:
        """
        Get complete monitoring snapshot — all views at once.

        This is the primary entry point for full-page refreshes.
        Each sub-view is built independently from its data source.
        """
        now = self._now_ms_fn()

        positions = self.get_positions()
        capital = self._source.get_capital_snapshot()

        total_unrealized = sum(p.unrealized_pnl_usdt for p in positions)
        total_exposure = sum(p.current_size_usdt for p in positions)

        return MonitoringSnapshot(
            timestamp_ms=now,
            health=self.get_health(),
            open_positions=positions,
            recent_trades=self.get_trades(limit=20),
            recent_decisions=self.get_decisions(limit=20),
            metrics=self.get_metrics(),
            recent_alerts=self.get_alerts(limit=20),
            total_open_positions=len(positions),
            total_unrealized_pnl=total_unrealized,
            total_exposure_usdt=total_exposure,
            total_capital_usdt=float(capital.get("total_capital", 0) or capital.get("equity", 0) or 0),
            available_capital_usdt=float(capital.get("available_capital", 0) or 0),
        )

    # ── Alert Manager Access ──────────────────────────────────

    @property
    def alert_manager(self) -> AlertManager:
        """Direct access to alert manager for event wiring."""
        return self._alerts

    # ── Diagnostics ───────────────────────────────────────────

    def get_state(self) -> dict:
        """Get API state for diagnostics."""
        return {
            "alert_state": self._alerts.get_state(),
            "data_source_type": type(self._source).__name__,
        }
