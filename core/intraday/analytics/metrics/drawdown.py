"""
Phase 7: Drawdown Metrics

PURE FUNCTIONS: deterministic, no mutable state.
Drawdown = (peak - trough) / peak
"""

from typing import List

from ..models import EquityPoint, TradeSnapshot, EquityCurveBuilder


def compute_max_drawdown(
    equity_curve: List[EquityPoint],
) -> float:
    """
    Compute maximum drawdown percentage from equity curve.

    Definition: (peak - trough) / peak, expressed as decimal.

    Args:
        equity_curve: List of EquityPoint from EquityCurveBuilder.build().

    Returns:
        Maximum drawdown as decimal (0.15 = 15% drawdown).
        Returns 0.0 if empty curve or no drawdown.
    """
    if not equity_curve:
        return 0.0

    max_dd = max((point.drawdown_pct for point in equity_curve), default=0.0)
    return max_dd


def compute_max_drawdown_duration_ms(
    equity_curve: List[EquityPoint],
) -> int:
    """
    Compute longest time spent in drawdown (milliseconds).

    Definition: Longest contiguous period where equity < peak_equity.

    Args:
        equity_curve: List of EquityPoint from EquityCurveBuilder.build().

    Returns:
        Duration in milliseconds.
        Returns 0 if no drawdown or empty curve.
    """
    if not equity_curve:
        return 0

    max_duration_ms = 0
    current_drawdown_start_ms = None

    for point in equity_curve:
        is_in_drawdown = point.drawdown_pct > 1e-6

        if is_in_drawdown:
            if current_drawdown_start_ms is None:
                current_drawdown_start_ms = point.timestamp_ms
        else:
            if current_drawdown_start_ms is not None:
                duration_ms = point.timestamp_ms - current_drawdown_start_ms
                max_duration_ms = max(max_duration_ms, duration_ms)
                current_drawdown_start_ms = None

    # If still in drawdown at end, count it
    if current_drawdown_start_ms is not None and equity_curve:
        duration_ms = equity_curve[-1].timestamp_ms - current_drawdown_start_ms
        max_duration_ms = max(max_duration_ms, duration_ms)

    return max_duration_ms


def compute_calmar_ratio(
    equity_curve: List[EquityPoint],
    initial_capital: float,
) -> float:
    """
    Compute Calmar ratio: CAGR / max_drawdown.

    Definition: Ratio of annualized return to maximum drawdown.
    Higher is better (more return per unit of drawdown).

    Args:
        equity_curve: List of EquityPoint.
        initial_capital: Starting capital.

    Returns:
        Calmar ratio (dimensionless).
        Returns 0.0 if no drawdown, negative if losing strategy.
    """
    if not equity_curve or initial_capital <= 0:
        return 0.0

    max_dd = compute_max_drawdown(equity_curve)
    if max_dd == 0:
        # No drawdown: either all wins or all losses
        # Return high value if profitable, else 0
        final_equity = equity_curve[-1].equity
        return float('inf') if final_equity > initial_capital else 0.0

    # Compute CAGR
    first_point = equity_curve[0]
    last_point = equity_curve[-1]

    # Compute time span in years
    duration_ms = last_point.timestamp_ms - first_point.timestamp_ms
    duration_years = duration_ms / (1000.0 * 60 * 60 * 24 * 365.25)

    # Guard against very short time periods that cause overflow
    if duration_years <= 0 or duration_years < 1e-6:
        return 0.0

    # CAGR = (ending / beginning) ^ (1 / years) - 1
    final_equity = last_point.equity
    if final_equity <= 0:
        return float('-inf') if max_dd > 0 else 0.0

    try:
        # Protect against overflow in exponentiation
        exponent = 1.0 / duration_years
        if exponent > 1000:  # Guard against very large exponents
            return 0.0
        cagr = (final_equity / initial_capital) ** exponent - 1.0
    except (OverflowError, ValueError):
        # Return 0 if computation fails
        return 0.0

    return cagr / max_dd


def compute_recovery_factor(
    equity_curve: List[EquityPoint],
) -> float:
    """
    Compute recovery factor: total profit / max_drawdown_amount.

    Definition: How many times the max loss we've recovered in profit.
    Higher is better.

    Args:
        equity_curve: List of EquityPoint.

    Returns:
        Recovery factor (dimensionless).
        Returns 0.0 if no trades or no drawdown.
    """
    if not equity_curve:
        return 0.0

    # Total profit
    first_point = equity_curve[0]
    last_point = equity_curve[-1]
    total_profit = last_point.realized_pnl_cumulative

    if total_profit <= 0:
        return 0.0

    # Max drawdown amount
    max_dd_pct = compute_max_drawdown(equity_curve)
    if max_dd_pct == 0:
        return float('inf') if total_profit > 0 else 0.0

    # Find the peak that preceded the max drawdown
    max_drawdown_amount = 0.0
    for point in equity_curve:
        dd_amount = point.peak_equity - point.equity
        max_drawdown_amount = max(max_drawdown_amount, dd_amount)

    if max_drawdown_amount == 0:
        return float('inf') if total_profit > 0 else 0.0

    return total_profit / max_drawdown_amount


def compute_avg_drawdown(equity_curve: List[EquityPoint]) -> float:
    """
    Compute average drawdown percentage across all drawdown periods.

    Definition: Mean of all drawdown percentages during active drawdown periods.

    Args:
        equity_curve: List of EquityPoint.

    Returns:
        Average drawdown as decimal (0.08 = 8%).
        Returns 0.0 if no drawdown.
    """
    if not equity_curve:
        return 0.0

    # Collect all drawdown values during drawdown periods
    drawdowns = []
    current_drawdown = 0.0

    for point in equity_curve:
        if point.drawdown_pct > 1e-6:
            drawdowns.append(point.drawdown_pct)

    if not drawdowns:
        return 0.0

    return sum(drawdowns) / len(drawdowns)


def compute_num_drawdown_periods(equity_curve: List[EquityPoint]) -> int:
    """
    Count the number of distinct drawdown periods.

    Definition: Number of times equity fell below its peak.

    Args:
        equity_curve: List of EquityPoint.

    Returns:
        Number of drawdown periods (0 if no drawdown).
    """
    if not equity_curve:
        return 0

    num_periods = 0
    in_drawdown = False

    for point in equity_curve:
        is_in_dd = point.drawdown_pct > 1e-6

        if is_in_dd and not in_drawdown:
            num_periods += 1
            in_drawdown = True
        elif not is_in_dd and in_drawdown:
            in_drawdown = False

    return num_periods


def compute_longest_drawdown_duration_trades(
    equity_curve: List[EquityPoint],
) -> int:
    """
    Count the maximum number of consecutive trades in drawdown.

    Args:
        equity_curve: List of EquityPoint (each point = 1 trade).

    Returns:
        Maximum consecutive trades in drawdown.
        Returns 0 if no drawdown.
    """
    if not equity_curve:
        return 0

    max_trades = 0
    current_trades = 0

    for point in equity_curve:
        if point.drawdown_pct > 1e-6:
            current_trades += 1
            max_trades = max(max_trades, current_trades)
        else:
            current_trades = 0

    return max_trades
