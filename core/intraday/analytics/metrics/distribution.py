"""
Phase 7: Distribution Metrics

PURE FUNCTIONS: compute distribution statistics for various metrics.
Returns DistributionStats with percentiles, mean, stddev, min, max.
"""

from dataclasses import dataclass
from typing import List, Optional
import statistics

from ..models import TradeSnapshot


@dataclass(frozen=True)
class DistributionStats:
    """
    Immutable distribution statistics.

    Includes percentiles, mean, stddev, min, max.
    """

    count: int  # Number of samples
    mean: float
    stddev: float
    min_value: float
    max_value: float
    median: float  # 50th percentile
    p25: float  # 25th percentile
    p75: float  # 75th percentile
    p5: float  # 5th percentile
    p95: float  # 95th percentile


def _compute_distribution_stats(values: List[float]) -> Optional[DistributionStats]:
    """
    Compute distribution statistics from a list of values.

    Args:
        values: List of numeric values.

    Returns:
        DistributionStats if values is non-empty, else None.
    """
    if not values:
        return None

    sorted_vals = sorted(values)
    count = len(sorted_vals)

    # Mean
    mean = sum(sorted_vals) / count

    # Stddev
    if count > 1:
        variance = sum((v - mean) ** 2 for v in sorted_vals) / count
        stddev = variance ** 0.5
    else:
        stddev = 0.0

    # Min/Max
    min_value = sorted_vals[0]
    max_value = sorted_vals[-1]

    # Percentiles using linear interpolation
    def percentile(p: float) -> float:
        """Compute pth percentile."""
        if count == 1:
            return sorted_vals[0]
        idx = (p / 100.0) * (count - 1)
        lower = int(idx)
        upper = lower + 1
        if upper >= count:
            return sorted_vals[count - 1]
        frac = idx - lower
        return sorted_vals[lower] * (1 - frac) + sorted_vals[upper] * frac

    return DistributionStats(
        count=count,
        mean=mean,
        stddev=stddev,
        min_value=min_value,
        max_value=max_value,
        median=percentile(50.0),
        p25=percentile(25.0),
        p75=percentile(75.0),
        p5=percentile(5.0),
        p95=percentile(95.0),
    )


def compute_r_multiple_distribution(
    trades: List[TradeSnapshot],
) -> Optional[DistributionStats]:
    """
    Compute distribution of R multiples across trades.

    Args:
        trades: List of TradeSnapshot.

    Returns:
        DistributionStats or None if no trades.
    """
    if not trades:
        return None

    r_multiples = [t.r_multiple for t in trades]
    return _compute_distribution_stats(r_multiples)


def compute_duration_distribution(
    trades: List[TradeSnapshot],
) -> Optional[DistributionStats]:
    """
    Compute distribution of trade durations (milliseconds).

    Args:
        trades: List of TradeSnapshot.

    Returns:
        DistributionStats or None if no trades.
    """
    if not trades:
        return None

    durations = [float(t.duration_ms) for t in trades]
    return _compute_distribution_stats(durations)


def compute_pnl_distribution(
    trades: List[TradeSnapshot],
) -> Optional[DistributionStats]:
    """
    Compute distribution of realized PnL (in USDT).

    Args:
        trades: List of TradeSnapshot.

    Returns:
        DistributionStats or None if no trades.
    """
    if not trades:
        return None

    pnls = [t.realized_pnl_usdt for t in trades]
    return _compute_distribution_stats(pnls)


def compute_mae_distribution(
    trades: List[TradeSnapshot],
) -> Optional[DistributionStats]:
    """
    Compute distribution of Maximum Adverse Excursion (MAE) percentages.

    Only includes trades where mae_pct is not None.

    Args:
        trades: List of TradeSnapshot.

    Returns:
        DistributionStats or None if no trades have mae_pct.
    """
    mae_values = [t.mae_pct for t in trades if t.mae_pct is not None]
    if not mae_values:
        return None

    return _compute_distribution_stats(mae_values)


def compute_mfe_distribution(
    trades: List[TradeSnapshot],
) -> Optional[DistributionStats]:
    """
    Compute distribution of Maximum Favorable Excursion (MFE) percentages.

    Only includes trades where mfe_pct is not None.

    Args:
        trades: List of TradeSnapshot.

    Returns:
        DistributionStats or None if no trades have mfe_pct.
    """
    mfe_values = [t.mfe_pct for t in trades if t.mfe_pct is not None]
    if not mfe_values:
        return None

    return _compute_distribution_stats(mfe_values)


def compute_pnl_pct_distribution(
    trades: List[TradeSnapshot],
) -> Optional[DistributionStats]:
    """
    Compute distribution of PnL percentages (realized_pnl / entry_size).

    Args:
        trades: List of TradeSnapshot.

    Returns:
        DistributionStats or None if no trades.
    """
    if not trades:
        return None

    pnl_pcts = [
        (t.realized_pnl_usdt / t.entry_size_usdt * 100.0)
        if t.entry_size_usdt > 0
        else 0.0
        for t in trades
    ]
    return _compute_distribution_stats(pnl_pcts)


def compute_bars_held_distribution(
    trades: List[TradeSnapshot],
) -> Optional[DistributionStats]:
    """
    Compute distribution of bars held per trade.

    Args:
        trades: List of TradeSnapshot.

    Returns:
        DistributionStats or None if no trades.
    """
    if not trades:
        return None

    bars_held = [float(t.bars_held) for t in trades]
    return _compute_distribution_stats(bars_held)


def compute_slippage_distribution(
    trades: List[TradeSnapshot],
) -> Optional[DistributionStats]:
    """
    Compute distribution of slippage percentages.

    Args:
        trades: List of TradeSnapshot.

    Returns:
        DistributionStats or None if no trades.
    """
    if not trades:
        return None

    slippages = [t.slippage_pct for t in trades]
    return _compute_distribution_stats(slippages)


def compute_signal_to_fill_distribution(
    trades: List[TradeSnapshot],
) -> Optional[DistributionStats]:
    """
    Compute distribution of signal-to-fill latencies (milliseconds).

    Args:
        trades: List of TradeSnapshot.

    Returns:
        DistributionStats or None if no trades.
    """
    if not trades:
        return None

    latencies = [float(t.signal_to_fill_ms) for t in trades]
    return _compute_distribution_stats(latencies)
