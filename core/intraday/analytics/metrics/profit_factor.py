"""
Phase 7: Profit Factor Metrics

PURE FUNCTIONS: deterministic, no mutable state.
No wall-clock dependence. No Qt imports.
"""

from typing import List

from ..models import TradeSnapshot


def compute_profit_factor(trades: List[TradeSnapshot]) -> float:
    """
    Compute profit factor: sum(winning trades) / abs(sum(losing trades)).

    Definition: Ratio of total profits to total losses.
    If no losses exist, returns inf if any profit, else 0.0.

    Args:
        trades: List of TradeSnapshot.

    Returns:
        Profit factor as a decimal.
        Returns 0.0 if no trades or all trades are breakeven.
    """
    if not trades:
        return 0.0

    gross_profit = sum(t.realized_pnl_usdt for t in trades if t.is_winner)
    gross_loss = abs(sum(t.realized_pnl_usdt for t in trades if t.is_loser))

    if gross_loss == 0:
        # No losses: return inf if profit, else 0
        return float('inf') if gross_profit > 0 else 0.0

    return gross_profit / gross_loss


def compute_win_rate(trades: List[TradeSnapshot]) -> float:
    """
    Compute win rate as a percentage.

    Definition: Number of winning trades / total trades.
    Winning trade: realized_pnl_usdt > 0.

    Args:
        trades: List of TradeSnapshot.

    Returns:
        Win rate as a decimal (0.65 means 65%).
        Returns 0.0 if no trades.
    """
    if not trades:
        return 0.0

    winners = sum(1 for t in trades if t.is_winner)
    return winners / len(trades)


def compute_loss_rate(trades: List[TradeSnapshot]) -> float:
    """
    Compute loss rate as a percentage.

    Definition: Number of losing trades / total trades.
    Losing trade: realized_pnl_usdt < 0.

    Args:
        trades: List of TradeSnapshot.

    Returns:
        Loss rate as a decimal (0.35 means 35%).
        Returns 0.0 if no trades.
    """
    if not trades:
        return 0.0

    losers = sum(1 for t in trades if t.is_loser)
    return losers / len(trades)


def compute_breakeven_rate(trades: List[TradeSnapshot]) -> float:
    """
    Compute breakeven rate as a percentage.

    Definition: Number of breakeven trades / total trades.
    Breakeven trade: realized_pnl_usdt == 0.

    Args:
        trades: List of TradeSnapshot.

    Returns:
        Breakeven rate as a decimal (0.05 means 5%).
        Returns 0.0 if no trades.
    """
    if not trades:
        return 0.0

    breakevens = sum(1 for t in trades if t.realized_pnl_usdt == 0.0)
    return breakevens / len(trades)


def compute_avg_win(trades: List[TradeSnapshot]) -> float:
    """
    Compute average winning trade (in USDT).

    Args:
        trades: List of TradeSnapshot.

    Returns:
        Average PnL of winning trades.
        Returns 0.0 if no winning trades.
    """
    winners = [t.realized_pnl_usdt for t in trades if t.is_winner]
    if not winners:
        return 0.0
    return sum(winners) / len(winners)


def compute_avg_loss(trades: List[TradeSnapshot]) -> float:
    """
    Compute average losing trade (in USDT).

    Note: Returns absolute value of loss (positive number).

    Args:
        trades: List of TradeSnapshot.

    Returns:
        Average absolute loss of losing trades.
        Returns 0.0 if no losing trades.
    """
    losers = [abs(t.realized_pnl_usdt) for t in trades if t.is_loser]
    if not losers:
        return 0.0
    return sum(losers) / len(losers)


def compute_win_loss_ratio(trades: List[TradeSnapshot]) -> float:
    """
    Compute win/loss ratio = avg_win / avg_loss.

    Measures the size of average win relative to average loss.
    Higher is better (larger wins relative to losses).

    Args:
        trades: List of TradeSnapshot.

    Returns:
        Ratio as decimal (1.5 means avg win is 1.5x avg loss).
        Returns 0.0 if no losses or no wins.
    """
    avg_win = compute_avg_win(trades)
    avg_loss = compute_avg_loss(trades)

    if avg_loss == 0.0:
        return 0.0

    return avg_win / avg_loss


def compute_total_pnl(trades: List[TradeSnapshot]) -> float:
    """
    Compute total realized PnL across all trades.

    Args:
        trades: List of TradeSnapshot.

    Returns:
        Sum of all realized_pnl_usdt (can be negative).
    """
    return sum(t.realized_pnl_usdt for t in trades)


def compute_gross_profit(trades: List[TradeSnapshot]) -> float:
    """
    Compute gross profit = sum of all winning trade PnL.

    Args:
        trades: List of TradeSnapshot.

    Returns:
        Sum of realized_pnl_usdt where realized_pnl_usdt > 0.
    """
    return sum(t.realized_pnl_usdt for t in trades if t.is_winner)


def compute_gross_loss(trades: List[TradeSnapshot]) -> float:
    """
    Compute gross loss = sum of all losing trade PnL.

    Note: Returns absolute value (positive number).

    Args:
        trades: List of TradeSnapshot.

    Returns:
        Absolute sum of realized_pnl_usdt where realized_pnl_usdt < 0.
    """
    return abs(sum(t.realized_pnl_usdt for t in trades if t.is_loser))
