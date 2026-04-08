"""
Phase 7: Capital Efficiency Metrics

PURE FUNCTIONS: compute capital usage and efficiency.
"""

from dataclasses import dataclass
from typing import List

from ..models import TradeSnapshot


@dataclass(frozen=True)
class CapitalEfficiencyMetrics:
    """
    Immutable capital efficiency statistics.

    Measures how efficiently capital is deployed.
    """

    total_pnl_usdt: float  # Sum of all realized PnL
    total_fees_usdt: float  # Sum of all fees paid
    net_pnl_usdt: float  # total_pnl - total_fees (profit after costs)
    total_risk_deployed_usdt: float  # Sum of all risk_usdt across trades
    total_capital_locked_usdt: float  # Sum of all entry_size_usdt across trades
    trade_count: int
    avg_capital_per_trade: float  # total_capital_locked / trade_count
    avg_risk_per_trade: float  # total_risk_deployed / trade_count
    return_on_risk_pct: float  # net_pnl / total_risk_deployed (as %)
    return_on_capital_pct: float  # net_pnl / total_capital_locked (as %)


def compute_capital_efficiency(
    trades: List[TradeSnapshot],
) -> CapitalEfficiencyMetrics:
    """
    Compute capital efficiency metrics.

    Args:
        trades: List of TradeSnapshot.

    Returns:
        CapitalEfficiencyMetrics with all efficiency stats.
    """
    if not trades:
        return CapitalEfficiencyMetrics(
            total_pnl_usdt=0.0,
            total_fees_usdt=0.0,
            net_pnl_usdt=0.0,
            total_risk_deployed_usdt=0.0,
            total_capital_locked_usdt=0.0,
            trade_count=0,
            avg_capital_per_trade=0.0,
            avg_risk_per_trade=0.0,
            return_on_risk_pct=0.0,
            return_on_capital_pct=0.0,
        )

    total_pnl = sum(t.realized_pnl_usdt for t in trades)
    total_fees = sum(t.fee_total_usdt for t in trades)
    total_risk = sum(t.risk_usdt for t in trades)
    total_capital = sum(t.entry_size_usdt for t in trades)
    trade_count = len(trades)

    net_pnl = total_pnl - total_fees

    # Averages
    avg_capital_per_trade = total_capital / trade_count if trade_count > 0 else 0.0
    avg_risk_per_trade = total_risk / trade_count if trade_count > 0 else 0.0

    # Returns on capital and risk (as percentages)
    return_on_risk_pct = (net_pnl / total_risk * 100.0) if total_risk > 0 else 0.0
    return_on_capital_pct = (net_pnl / total_capital * 100.0) if total_capital > 0 else 0.0

    return CapitalEfficiencyMetrics(
        total_pnl_usdt=total_pnl,
        total_fees_usdt=total_fees,
        net_pnl_usdt=net_pnl,
        total_risk_deployed_usdt=total_risk,
        total_capital_locked_usdt=total_capital,
        trade_count=trade_count,
        avg_capital_per_trade=avg_capital_per_trade,
        avg_risk_per_trade=avg_risk_per_trade,
        return_on_risk_pct=return_on_risk_pct,
        return_on_capital_pct=return_on_capital_pct,
    )


def compute_peak_utilization(
    trades: List[TradeSnapshot],
    initial_capital: float,
) -> float:
    """
    Compute peak capital utilization from overlapping positions.

    Uses event-based approach: for each trade, create open/close events,
    then accumulate capital to find peak concurrent deployment.

    Assumes trades are NOT necessarily sorted.

    Args:
        trades: List of TradeSnapshot.
        initial_capital: Initial account capital.

    Returns:
        Peak utilization as a decimal (0.75 = 75% of capital in use).
        Returns 0.0 if no trades or zero capital.
    """
    if not trades or initial_capital <= 0:
        return 0.0

    # Create events: (timestamp, capital_delta)
    # Positive delta = opening a trade, negative = closing
    events = []
    for trade in trades:
        events.append((trade.opened_at_ms, trade.entry_size_usdt))  # Open
        events.append((trade.closed_at_ms, -trade.entry_size_usdt))  # Close

    # Sort by timestamp
    events.sort()

    # Accumulate to find peak
    peak_capital = 0.0
    current_capital = 0.0

    for _, delta in events:
        current_capital += delta
        peak_capital = max(peak_capital, current_capital)

    return peak_capital / initial_capital


def compute_avg_utilization(
    trades: List[TradeSnapshot],
    initial_capital: float,
) -> float:
    """
    Compute average capital utilization across all trades.

    Definition: mean(entry_size_usdt) / initial_capital.

    Args:
        trades: List of TradeSnapshot.
        initial_capital: Initial account capital.

    Returns:
        Average utilization as a decimal.
        Returns 0.0 if no trades or zero capital.
    """
    if not trades or initial_capital <= 0:
        return 0.0

    avg_size = sum(t.entry_size_usdt for t in trades) / len(trades)
    return avg_size / initial_capital


def compute_capital_turnover(
    trades: List[TradeSnapshot],
    initial_capital: float,
) -> float:
    """
    Compute capital turnover: total capital deployed / initial capital.

    Definition: sum(entry_size_usdt) / initial_capital.

    Args:
        trades: List of TradeSnapshot.
        initial_capital: Initial account capital.

    Returns:
        Capital turnover ratio.
        Returns 0.0 if zero capital.
    """
    if initial_capital <= 0:
        return 0.0

    total_deployed = sum(t.entry_size_usdt for t in trades)
    return total_deployed / initial_capital


def compute_avg_idle_time(
    trades: List[TradeSnapshot],
) -> int:
    """
    Compute average idle time between consecutive trades (milliseconds).

    Definition: mean gap from close of one trade to open of next.

    Args:
        trades: List of TradeSnapshot (should be sorted by opened_at_ms).

    Returns:
        Average idle time in milliseconds.
        Returns 0 if fewer than 2 trades.
    """
    if len(trades) < 2:
        return 0

    # Sort by closed_at_ms to find gaps
    sorted_trades = sorted(trades, key=lambda t: t.closed_at_ms)

    gaps = []
    for i in range(len(sorted_trades) - 1):
        close_time = sorted_trades[i].closed_at_ms
        next_open_time = sorted_trades[i + 1].opened_at_ms

        # Only count if this trade closed before the next opened
        if close_time < next_open_time:
            gap = next_open_time - close_time
            gaps.append(gap)

    if not gaps:
        return 0

    return int(sum(gaps) / len(gaps))
