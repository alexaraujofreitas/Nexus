"""
Phase 7: Metrics Functions for Trade Analytics

PURE FUNCTIONS: no mutable state, deterministic, replay-safe.
No Qt imports. No wall-clock dependence.

Exports:
  Profit Factor metrics:
  - compute_profit_factor, compute_win_rate, compute_loss_rate
  - compute_avg_win, compute_avg_loss, compute_win_loss_ratio
  - compute_total_pnl, compute_gross_profit, compute_gross_loss

  Expectancy metrics:
  - compute_expectancy_r, compute_expectancy_capital, compute_expectancy_per_dollar
  - compute_expectancy_formula, compute_expectancy_per_trade_r

  Drawdown metrics:
  - compute_max_drawdown, compute_max_drawdown_duration_ms
  - compute_calmar_ratio, compute_recovery_factor
  - compute_avg_drawdown, compute_num_drawdown_periods
  - compute_longest_drawdown_duration_trades

  Distribution metrics:
  - compute_r_multiple_distribution, compute_duration_distribution
  - compute_pnl_distribution, compute_mae_distribution, compute_mfe_distribution
  - compute_pnl_pct_distribution, compute_bars_held_distribution
  - compute_slippage_distribution, compute_signal_to_fill_distribution
  - DistributionStats

  Capital Efficiency metrics:
  - compute_capital_efficiency, compute_peak_utilization
  - compute_avg_utilization, compute_capital_turnover, compute_avg_idle_time
  - CapitalEfficiencyMetrics
"""

from .profit_factor import (
    compute_profit_factor,
    compute_win_rate,
    compute_loss_rate,
    compute_breakeven_rate,
    compute_avg_win,
    compute_avg_loss,
    compute_win_loss_ratio,
    compute_total_pnl,
    compute_gross_profit,
    compute_gross_loss,
)
from .expectancy import (
    compute_expectancy_r,
    compute_expectancy_capital,
    compute_expectancy_per_dollar,
    compute_expectancy_formula,
    compute_expectancy_per_trade_r,
)
from .drawdown import (
    compute_max_drawdown,
    compute_max_drawdown_duration_ms,
    compute_calmar_ratio,
    compute_recovery_factor,
    compute_avg_drawdown,
    compute_num_drawdown_periods,
    compute_longest_drawdown_duration_trades,
)
from .distribution import (
    compute_r_multiple_distribution,
    compute_duration_distribution,
    compute_pnl_distribution,
    compute_mae_distribution,
    compute_mfe_distribution,
    compute_pnl_pct_distribution,
    compute_bars_held_distribution,
    compute_slippage_distribution,
    compute_signal_to_fill_distribution,
    DistributionStats,
)
from .capital_efficiency import (
    compute_capital_efficiency,
    compute_peak_utilization,
    compute_avg_utilization,
    compute_capital_turnover,
    compute_avg_idle_time,
    CapitalEfficiencyMetrics,
)

__all__ = [
    # Profit factor
    "compute_profit_factor",
    "compute_win_rate",
    "compute_loss_rate",
    "compute_breakeven_rate",
    "compute_avg_win",
    "compute_avg_loss",
    "compute_win_loss_ratio",
    "compute_total_pnl",
    "compute_gross_profit",
    "compute_gross_loss",
    # Expectancy
    "compute_expectancy_r",
    "compute_expectancy_capital",
    "compute_expectancy_per_dollar",
    "compute_expectancy_formula",
    "compute_expectancy_per_trade_r",
    # Drawdown
    "compute_max_drawdown",
    "compute_max_drawdown_duration_ms",
    "compute_calmar_ratio",
    "compute_recovery_factor",
    "compute_avg_drawdown",
    "compute_num_drawdown_periods",
    "compute_longest_drawdown_duration_trades",
    # Distribution
    "compute_r_multiple_distribution",
    "compute_duration_distribution",
    "compute_pnl_distribution",
    "compute_mae_distribution",
    "compute_mfe_distribution",
    "compute_pnl_pct_distribution",
    "compute_bars_held_distribution",
    "compute_slippage_distribution",
    "compute_signal_to_fill_distribution",
    "DistributionStats",
    # Capital efficiency
    "compute_capital_efficiency",
    "compute_peak_utilization",
    "compute_avg_utilization",
    "compute_capital_turnover",
    "compute_avg_idle_time",
    "CapitalEfficiencyMetrics",
]
