"""
Phase 7: Expectancy Metrics

PURE FUNCTIONS: deterministic, no mutable state.
Expectancy = avg win * win_rate - avg loss * loss_rate
"""

from typing import List

from ..models import TradeSnapshot
from .profit_factor import (
    compute_win_rate,
    compute_loss_rate,
    compute_avg_win,
    compute_avg_loss,
)


def compute_expectancy_r(trades: List[TradeSnapshot]) -> float:
    """
    Compute expectancy in R (risk units).

    Definition: Average R multiple across all trades.
    Positive expectancy = system is profitable on average.

    Args:
        trades: List of TradeSnapshot.

    Returns:
        Expectancy in R units.
        Returns 0.0 if no trades.
    """
    if not trades:
        return 0.0

    total_r = sum(t.r_multiple for t in trades)
    return total_r / len(trades)


def compute_expectancy_capital(trades: List[TradeSnapshot]) -> float:
    """
    Compute expectancy in USDT (capital).

    Definition: Average realized PnL per trade.

    Args:
        trades: List of TradeSnapshot.

    Returns:
        Expectancy in USDT.
        Returns 0.0 if no trades.
    """
    if not trades:
        return 0.0

    total_pnl = sum(t.realized_pnl_usdt for t in trades)
    return total_pnl / len(trades)


def compute_expectancy_per_dollar(
    trades: List[TradeSnapshot],
) -> float:
    """
    Compute expectancy per dollar of risk deployed.

    Definition: sum(all realized PnL) / sum(all risk USDT).

    Args:
        trades: List of TradeSnapshot.

    Returns:
        Expectancy per dollar of risk (e.g., 0.05 = 5 cents per dollar risked).
        Returns 0.0 if no trades or no total risk.
    """
    if not trades:
        return 0.0

    total_pnl = sum(t.realized_pnl_usdt for t in trades)
    total_risk = sum(t.risk_usdt for t in trades)

    if total_risk == 0:
        return 0.0

    return total_pnl / total_risk


def compute_expectancy_formula(trades: List[TradeSnapshot]) -> float:
    """
    Compute expectancy using the classical formula.

    Formula: (win_rate * avg_win) - (loss_rate * avg_loss)

    This represents the expected profit per trade.

    Args:
        trades: List of TradeSnapshot.

    Returns:
        Expectancy in USDT.
        Returns 0.0 if no trades.
    """
    if not trades:
        return 0.0

    win_rate = compute_win_rate(trades)
    loss_rate = compute_loss_rate(trades)
    avg_win = compute_avg_win(trades)
    avg_loss = compute_avg_loss(trades)

    return (win_rate * avg_win) - (loss_rate * avg_loss)


def compute_expectancy_per_trade_r(trades: List[TradeSnapshot]) -> float:
    """
    Compute expectancy per trade in risk units (R).

    Formula: (win_rate * avg_win_r) - (loss_rate * avg_loss_r)

    where avg_win_r = mean(r_multiple for winners)
    and avg_loss_r = mean(r_multiple for losers)

    Args:
        trades: List of TradeSnapshot.

    Returns:
        Expectancy in R units per trade.
        Returns 0.0 if no trades.
    """
    if not trades:
        return 0.0

    winners = [t.r_multiple for t in trades if t.is_winner]
    losers = [t.r_multiple for t in trades if t.is_loser]

    win_rate = compute_win_rate(trades)
    loss_rate = compute_loss_rate(trades)

    avg_win_r = sum(winners) / len(winners) if winners else 0.0
    avg_loss_r = sum(losers) / len(losers) if losers else 0.0

    return (win_rate * avg_win_r) - (loss_rate * abs(avg_loss_r))
