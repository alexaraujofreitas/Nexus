"""
Phase 9: UI / Monitoring Layer — Read Models & Views

Strict observer layer. NEVER mutates upstream state.
All data sourced from:
- EventBus event history
- Phase 6 monitoring component get_state() / get_quality_stats()
- Phase 7 PerformanceEngine.analyze() → PerformanceReport
- Phase 8 LiveExecutor / RecoveryManager get_state()
- Phase 5 PortfolioState.get_snapshot() / CircuitBreaker.get_status()

Design invariants:
- ZERO writes to execution, portfolio, risk, or exchange state
- ZERO PySide6/Qt imports (pure data layer — UI pages consume views)
- All view models are frozen dataclasses (immutable)
- Deterministic: same inputs → same views
- Replay-safe: reconstructable from trace data
"""

from .view_contracts import (
    TradeView,
    PositionView,
    HealthView,
    MetricsView,
    AlertRecord,
    AlertSeverity,
    MonitoringSnapshot,
)
from .trades_view import TradesViewBuilder
from .positions_view import PositionsViewBuilder
from .health_view import HealthViewBuilder
from .metrics_view import MetricsViewBuilder
from .alert_manager import AlertManager
from .monitoring_api import MonitoringAPI

__all__ = [
    # View contracts
    "TradeView",
    "PositionView",
    "HealthView",
    "MetricsView",
    "AlertRecord",
    "AlertSeverity",
    "MonitoringSnapshot",
    # Builders
    "TradesViewBuilder",
    "PositionsViewBuilder",
    "HealthViewBuilder",
    "MetricsViewBuilder",
    # Alert system
    "AlertManager",
    # Unified API
    "MonitoringAPI",
]
