"""
Phase 7: Regime-Level Aggregation

Groups trades by regime_at_entry and computes per-regime metrics.

PURE FUNCTION: deterministic, no mutable state.
"""

from dataclasses import dataclass
from typing import List, Dict
from collections import defaultdict

from ..models import TradeSnapshot
from ..metrics import (
    compute_profit_factor,
    compute_win_rate,
    compute_expectancy_r,
)


@dataclass(frozen=True)
class RegimePerformance:
    """
    Immutable performance summary for a single regime.
    """

    regime: str
    trade_count: int
    win_rate: float
    profit_factor: float
    expectancy_r: float
    total_pnl_usdt: float
    avg_r_multiple: float
    avg_duration_ms: int
    strategy_counts: Dict[str, int]  # breakdown by strategy within this regime


class RegimeAggregator:
    """
    Groups trades by regime and computes per-regime metrics.

    PURE FUNCTION: no mutable state, deterministic.
    """

    @staticmethod
    def aggregate(trades: List[TradeSnapshot]) -> Dict[str, RegimePerformance]:
        """
        Group trades by regime_at_entry, compute metrics per group.

        Args:
            trades: List of TradeSnapshot.

        Returns:
            Dict mapping regime → RegimePerformance.
        """
        if not trades:
            return {}

        # Group by regime_at_entry
        groups: Dict[str, List[TradeSnapshot]] = defaultdict(list)
        for trade in trades:
            groups[trade.regime_at_entry].append(trade)

        results = {}
        for regime, regime_trades in groups.items():
            perf = RegimeAggregator._compute_regime_perf(regime, regime_trades)
            results[regime] = perf

        return results

    @staticmethod
    def _compute_regime_perf(
        regime: str,
        trades: List[TradeSnapshot],
    ) -> RegimePerformance:
        """Compute performance for a single regime's trades."""
        if not trades:
            return RegimePerformance(
                regime=regime,
                trade_count=0,
                win_rate=0.0,
                profit_factor=0.0,
                expectancy_r=0.0,
                total_pnl_usdt=0.0,
                avg_r_multiple=0.0,
                avg_duration_ms=0,
                strategy_counts={},
            )

        # Metrics
        win_rate = compute_win_rate(trades)
        profit_factor = compute_profit_factor(trades)
        expectancy_r = compute_expectancy_r(trades)

        # PnL
        total_pnl = sum(t.realized_pnl_usdt for t in trades)

        # R multiple
        avg_r = sum(t.r_multiple for t in trades) / len(trades)

        # Duration
        avg_duration_ms = int(sum(t.duration_ms for t in trades) / len(trades))

        # Strategy breakdown
        strategy_counts: Dict[str, int] = defaultdict(int)
        for trade in trades:
            strategy_counts[trade.strategy_name] += 1

        return RegimePerformance(
            regime=regime,
            trade_count=len(trades),
            win_rate=win_rate,
            profit_factor=profit_factor,
            expectancy_r=expectancy_r,
            total_pnl_usdt=total_pnl,
            avg_r_multiple=avg_r,
            avg_duration_ms=avg_duration_ms,
            strategy_counts=dict(strategy_counts),
        )

    @staticmethod
    def cross_tabulate(
        trades: List[TradeSnapshot],
    ) -> Dict[str, Dict[str, float]]:
        """
        Cross-tabulate strategy × regime → profit_factor.

        Creates a nested dict:
            {strategy_name: {regime: profit_factor}}

        Args:
            trades: List of TradeSnapshot.

        Returns:
            Nested dict with profit factors for each strategy-regime pair.
        """
        if not trades:
            return {}

        # Group by (strategy_name, regime_at_entry)
        pairs: Dict[tuple, List[TradeSnapshot]] = defaultdict(list)
        for trade in trades:
            key = (trade.strategy_name, trade.regime_at_entry)
            pairs[key].append(trade)

        # Compute PF for each pair
        result: Dict[str, Dict[str, float]] = defaultdict(dict)
        for (strategy_name, regime), pair_trades in pairs.items():
            pf = compute_profit_factor(pair_trades)
            result[strategy_name][regime] = pf

        return dict(result)
