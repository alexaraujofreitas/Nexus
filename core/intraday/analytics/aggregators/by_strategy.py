"""
Phase 7: Strategy-Level Aggregation

Groups trades by strategy_name and computes per-strategy metrics.

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
    compute_expectancy_capital,
)


@dataclass(frozen=True)
class StrategyPerformance:
    """
    Immutable performance summary for a single strategy.
    """

    strategy_name: str
    strategy_class: str
    trade_count: int
    win_rate: float
    profit_factor: float
    expectancy_r: float
    expectancy_capital: float  # Average PnL per trade
    avg_r_multiple: float
    total_pnl_usdt: float
    avg_pnl_usdt: float
    max_win_usdt: float
    max_loss_usdt: float
    avg_duration_ms: int
    avg_bars_held: float
    close_reason_counts: Dict[str, int]  # breakdown by close reason


class StrategyAggregator:
    """
    Groups trades by strategy and computes per-strategy metrics.

    PURE FUNCTION: no mutable state, deterministic.
    """

    @staticmethod
    def aggregate(trades: List[TradeSnapshot]) -> Dict[str, StrategyPerformance]:
        """
        Group trades by strategy_name, compute metrics per group.

        Args:
            trades: List of TradeSnapshot.

        Returns:
            Dict mapping strategy_name → StrategyPerformance.
        """
        if not trades:
            return {}

        # Group by strategy_name
        groups: Dict[str, List[TradeSnapshot]] = defaultdict(list)
        for trade in trades:
            groups[trade.strategy_name].append(trade)

        results = {}
        for strategy_name, strategy_trades in groups.items():
            perf = StrategyAggregator._compute_strategy_perf(
                strategy_name, strategy_trades
            )
            results[strategy_name] = perf

        return results

    @staticmethod
    def _compute_strategy_perf(
        strategy_name: str,
        trades: List[TradeSnapshot],
    ) -> StrategyPerformance:
        """Compute performance for a single strategy's trades."""
        if not trades:
            return StrategyPerformance(
                strategy_name=strategy_name,
                strategy_class="",
                trade_count=0,
                win_rate=0.0,
                profit_factor=0.0,
                expectancy_r=0.0,
                expectancy_capital=0.0,
                avg_r_multiple=0.0,
                total_pnl_usdt=0.0,
                avg_pnl_usdt=0.0,
                max_win_usdt=0.0,
                max_loss_usdt=0.0,
                avg_duration_ms=0,
                avg_bars_held=0.0,
                close_reason_counts={},
            )

        # Strategy class (from first trade)
        strategy_class = trades[0].strategy_class

        # Metrics
        win_rate = compute_win_rate(trades)
        profit_factor = compute_profit_factor(trades)
        expectancy_r = compute_expectancy_r(trades)
        expectancy_capital = compute_expectancy_capital(trades)

        # R multiple
        avg_r = sum(t.r_multiple for t in trades) / len(trades)

        # PnL
        total_pnl = sum(t.realized_pnl_usdt for t in trades)
        avg_pnl = total_pnl / len(trades)

        # Win/Loss amounts
        winners = [t.realized_pnl_usdt for t in trades if t.is_winner]
        losers = [t.realized_pnl_usdt for t in trades if t.is_loser]
        max_win = max(winners) if winners else 0.0
        max_loss = min(losers) if losers else 0.0  # Negative number

        # Duration
        avg_duration_ms = int(sum(t.duration_ms for t in trades) / len(trades))
        avg_bars = sum(t.bars_held for t in trades) / len(trades)

        # Close reason counts
        close_reason_counts: Dict[str, int] = defaultdict(int)
        for trade in trades:
            close_reason_counts[trade.close_reason] += 1

        return StrategyPerformance(
            strategy_name=strategy_name,
            strategy_class=strategy_class,
            trade_count=len(trades),
            win_rate=win_rate,
            profit_factor=profit_factor,
            expectancy_r=expectancy_r,
            expectancy_capital=expectancy_capital,
            avg_r_multiple=avg_r,
            total_pnl_usdt=total_pnl,
            avg_pnl_usdt=avg_pnl,
            max_win_usdt=max_win,
            max_loss_usdt=max_loss,
            avg_duration_ms=avg_duration_ms,
            avg_bars_held=avg_bars,
            close_reason_counts=dict(close_reason_counts),
        )

    @staticmethod
    def rank_by_metric(
        performances: Dict[str, StrategyPerformance],
        metric: str = "profit_factor",
        descending: bool = True,
    ) -> List[StrategyPerformance]:
        """
        Rank strategies by a metric.

        Args:
            performances: Dict from aggregate().
            metric: Metric name (e.g., "profit_factor", "win_rate", "expectancy_capital").
            descending: If True, sort descending (highest first).

        Returns:
            List of StrategyPerformance sorted by metric.

        Raises:
            ValueError: If metric is not a valid attribute.
        """
        if not performances:
            return []

        # Validate metric
        sample = next(iter(performances.values()))
        if not hasattr(sample, metric):
            raise ValueError(
                f"Unknown metric '{metric}'. Valid metrics: {dir(sample)}"
            )

        # Sort
        sorted_list = sorted(
            performances.values(),
            key=lambda p: getattr(p, metric),
            reverse=descending,
        )
        return sorted_list
