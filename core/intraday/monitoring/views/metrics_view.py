"""
Phase 9: Metrics View Builder

Builds MetricsView projection from Phase 6 monitoring components.
STRICT OBSERVER — reads get_quality_stats()/get_state() only.

No Qt imports. No execution engine imports. Pure data transformation.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from .view_contracts import MetricsView, SlippageMetrics, LatencyMetrics

logger = logging.getLogger(__name__)


class MetricsViewBuilder:
    """
    Builds read-only metrics view from execution quality and latency data.

    Input: dicts from get_quality_stats() / get_state() / get_percentiles()
    Output: frozen MetricsView.

    Pure function — no state, no side effects.
    """

    @staticmethod
    def build(
        quality_stats: Optional[Dict[str, Dict[str, Any]]] = None,
        latency_data: Optional[Dict[str, Dict[str, Any]]] = None,
    ) -> MetricsView:
        """
        Build MetricsView from quality and latency component states.

        Args:
            quality_stats: {symbol → QualityStats dict} from ExecutionQualityTracker.
            latency_data: {symbol → latency dict} from LatencyMonitor.

        Returns:
            Frozen MetricsView.
        """
        slippage_by_symbol: Dict[str, SlippageMetrics] = {}
        latency_by_symbol: Dict[str, LatencyMetrics] = {}
        degraded_symbols: List[str] = []
        all_alerts: List[str] = []
        total_obs = 0

        # Build slippage metrics
        if quality_stats:
            for symbol, stats in quality_stats.items():
                obs_count = int(stats.get("observation_count", 0) or 0)
                total_obs += obs_count
                is_degraded = bool(stats.get("degraded", False) or stats.get("is_degraded", False))

                sm = SlippageMetrics(
                    symbol=symbol,
                    mean_slippage_bps=float(stats.get("mean", 0) or stats.get("mean_bps", 0) or 0),
                    median_slippage_bps=float(stats.get("median", 0) or stats.get("median_bps", 0) or 0),
                    p75_slippage_bps=float(stats.get("p75", 0) or stats.get("p75_bps", 0) or 0),
                    stddev_slippage_bps=float(stats.get("stddev", 0) or stats.get("stddev_bps", 0) or 0),
                    observation_count=obs_count,
                    is_degraded=is_degraded,
                )
                slippage_by_symbol[symbol] = sm

                if is_degraded:
                    degraded_symbols.append(symbol)

        # Build latency metrics
        if latency_data:
            for symbol, data in latency_data.items():
                alerts = list(data.get("alerts", []))
                all_alerts.extend(alerts)

                lm = LatencyMetrics(
                    symbol=symbol,
                    ema_total_ms=int(data.get("ema_ms", 0) or data.get("ema_total_ms", 0) or 0),
                    p50_ms=int(data.get("p50_ms", 0) or data.get("p50", 0) or 0),
                    p75_ms=int(data.get("p75_ms", 0) or data.get("p75", 0) or 0),
                    p90_ms=int(data.get("p90_ms", 0) or data.get("p90", 0) or 0),
                    p99_ms=int(data.get("p99_ms", 0) or data.get("p99", 0) or 0),
                    observation_count=int(data.get("observation_count", 0) or 0),
                    alerts=alerts,
                )
                latency_by_symbol[symbol] = lm

        return MetricsView(
            slippage_by_symbol=slippage_by_symbol,
            latency_by_symbol=latency_by_symbol,
            total_observations=total_obs,
            degraded_symbols=degraded_symbols,
            latency_alerts=all_alerts,
        )
