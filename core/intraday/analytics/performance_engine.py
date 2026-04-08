"""
Phase 7: PerformanceEngine — Analytics Orchestrator

Reads List[TradeSnapshot] and produces PerformanceReport with complete analytics.

ARCHITECTURE:
- STRICT OBSERVER: reads trace data, produces analytics
- NO mutation of upstream state (execution, risk, positions)
- NO hidden rolling state
- NO wall-clock dependence
- DETERMINISTIC: same trades + initial_capital → identical report
- REPLAY CONSISTENT: live run analytics == replay analytics

ISOLATION PROOF:
- Only imports from core.intraday.analytics (own package)
- No imports from execution, risk, strategy, or processing layers
- No access to Engine internals
- No shared mutable objects
- Contract-based inputs only (List[TradeSnapshot])

No Qt imports. Pure Python.
"""

from dataclasses import dataclass, asdict
from typing import List, Dict, Optional, Callable

from .models import TradeSnapshot, EquityPoint, EquityCurveBuilder
from .aggregators import (
    StrategyAggregator,
    StrategyPerformance,
    RegimeAggregator,
    RegimePerformance,
)
from .metrics import (
    compute_profit_factor,
    compute_win_rate,
    compute_expectancy_r,
    compute_expectancy_capital,
    compute_expectancy_per_dollar,
    compute_max_drawdown,
    compute_max_drawdown_duration_ms,
    compute_calmar_ratio,
    compute_recovery_factor,
    compute_r_multiple_distribution,
    compute_duration_distribution,
    compute_pnl_distribution,
    compute_mae_distribution,
    compute_mfe_distribution,
    DistributionStats,
    compute_capital_efficiency,
    compute_peak_utilization,
    compute_avg_utilization,
    compute_capital_turnover,
    compute_avg_idle_time,
    CapitalEfficiencyMetrics,
)


@dataclass(frozen=True)
class PerformanceReport:
    """
    Complete performance report. Immutable.

    Contains all analytics computed from trades.
    """

    # Summary metrics
    trade_count: int
    win_rate: float
    profit_factor: float
    expectancy_r: float
    expectancy_capital: float
    expectancy_per_dollar: float
    total_pnl_usdt: float
    total_fees_usdt: float

    # Drawdown metrics
    max_drawdown_pct: float
    max_drawdown_duration_ms: int
    calmar_ratio: float
    recovery_factor: float

    # Capital efficiency
    capital_efficiency: CapitalEfficiencyMetrics

    # Distributions
    r_distribution: Optional[DistributionStats]
    duration_distribution: Optional[DistributionStats]
    pnl_distribution: Optional[DistributionStats]
    mae_distribution: Optional[DistributionStats]
    mfe_distribution: Optional[DistributionStats]

    # Per-strategy breakdown
    by_strategy: Dict[str, StrategyPerformance]

    # Per-regime breakdown
    by_regime: Dict[str, RegimePerformance]

    # Strategy × regime cross-tabulation
    strategy_regime_pf: Dict[str, Dict[str, float]]

    # Equity curve
    equity_curve: List[EquityPoint]

    # Metadata
    initial_capital: float
    analysis_period_start_ms: int
    analysis_period_end_ms: int

    def to_dict(self) -> dict:
        """
        Serialize to JSON-safe dict.

        Converts frozen dataclasses to dicts recursively.
        """
        # Convert CapitalEfficiencyMetrics
        cap_eff_dict = asdict(self.capital_efficiency)

        # Convert DistributionStats (if present)
        def dist_to_dict(d: Optional[DistributionStats]) -> Optional[dict]:
            return asdict(d) if d else None

        # Convert EquityPoint list
        equity_curve_dict = [asdict(p) for p in self.equity_curve]

        # Convert strategy performances
        by_strategy_dict = {
            k: asdict(v) for k, v in self.by_strategy.items()
        }

        # Convert regime performances
        by_regime_dict = {
            k: asdict(v) for k, v in self.by_regime.items()
        }

        return {
            "trade_count": self.trade_count,
            "win_rate": self.win_rate,
            "profit_factor": self.profit_factor,
            "expectancy_r": self.expectancy_r,
            "expectancy_capital": self.expectancy_capital,
            "expectancy_per_dollar": self.expectancy_per_dollar,
            "total_pnl_usdt": self.total_pnl_usdt,
            "total_fees_usdt": self.total_fees_usdt,
            "max_drawdown_pct": self.max_drawdown_pct,
            "max_drawdown_duration_ms": self.max_drawdown_duration_ms,
            "calmar_ratio": self.calmar_ratio,
            "recovery_factor": self.recovery_factor,
            "capital_efficiency": cap_eff_dict,
            "r_distribution": dist_to_dict(self.r_distribution),
            "duration_distribution": dist_to_dict(self.duration_distribution),
            "pnl_distribution": dist_to_dict(self.pnl_distribution),
            "mae_distribution": dist_to_dict(self.mae_distribution),
            "mfe_distribution": dist_to_dict(self.mfe_distribution),
            "by_strategy": by_strategy_dict,
            "by_regime": by_regime_dict,
            "strategy_regime_pf": self.strategy_regime_pf,
            "equity_curve": equity_curve_dict,
            "initial_capital": self.initial_capital,
            "analysis_period_start_ms": self.analysis_period_start_ms,
            "analysis_period_end_ms": self.analysis_period_end_ms,
        }


class PerformanceEngine:
    """
    Phase 7 Analytics Engine — deterministic, replay-safe, isolated.

    Read-only observer: consumes trade snapshots and produces analytics.
    No shared state, no side effects.
    """

    def analyze(
        self,
        trades: List[TradeSnapshot],
        initial_capital: float,
    ) -> PerformanceReport:
        """
        Run full analytics on trade sequence.

        Args:
            trades: List of TradeSnapshot (will be sorted internally).
            initial_capital: Starting capital for equity curve.

        Returns:
            PerformanceReport with all metrics computed.

        Raises:
            ValueError: If initial_capital <= 0.
        """
        if initial_capital <= 0:
            raise ValueError(
                f"initial_capital must be positive, got {initial_capital}"
            )

        # Sort by closed_at_ms (deterministic ordering)
        sorted_trades = sorted(trades, key=lambda t: t.closed_at_ms)

        if not sorted_trades:
            # Empty report for no trades
            return self._empty_report(initial_capital)

        # Build equity curve
        equity_curve = EquityCurveBuilder.build(sorted_trades, initial_capital)

        # Compute summary metrics
        trade_count = len(sorted_trades)
        win_rate = compute_win_rate(sorted_trades)
        profit_factor = compute_profit_factor(sorted_trades)
        expectancy_r = compute_expectancy_r(sorted_trades)
        expectancy_capital = compute_expectancy_capital(sorted_trades)
        expectancy_per_dollar = compute_expectancy_per_dollar(sorted_trades)

        # PnL
        total_pnl = sum(t.realized_pnl_usdt for t in sorted_trades)
        total_fees = sum(t.fee_total_usdt for t in sorted_trades)

        # Drawdown metrics
        max_drawdown_pct = compute_max_drawdown(equity_curve)
        max_drawdown_duration_ms = compute_max_drawdown_duration_ms(equity_curve)
        calmar_ratio = compute_calmar_ratio(equity_curve, initial_capital)
        recovery_factor = compute_recovery_factor(equity_curve)

        # Capital efficiency
        capital_efficiency = compute_capital_efficiency(sorted_trades)

        # Distributions
        r_distribution = compute_r_multiple_distribution(sorted_trades)
        duration_distribution = compute_duration_distribution(sorted_trades)
        pnl_distribution = compute_pnl_distribution(sorted_trades)
        mae_distribution = compute_mae_distribution(sorted_trades)
        mfe_distribution = compute_mfe_distribution(sorted_trades)

        # Aggregations
        by_strategy = StrategyAggregator.aggregate(sorted_trades)
        by_regime = RegimeAggregator.aggregate(sorted_trades)
        strategy_regime_pf = RegimeAggregator.cross_tabulate(sorted_trades)

        # Period metadata
        analysis_period_start_ms = sorted_trades[0].opened_at_ms
        analysis_period_end_ms = sorted_trades[-1].closed_at_ms

        return PerformanceReport(
            trade_count=trade_count,
            win_rate=win_rate,
            profit_factor=profit_factor,
            expectancy_r=expectancy_r,
            expectancy_capital=expectancy_capital,
            expectancy_per_dollar=expectancy_per_dollar,
            total_pnl_usdt=total_pnl,
            total_fees_usdt=total_fees,
            max_drawdown_pct=max_drawdown_pct,
            max_drawdown_duration_ms=max_drawdown_duration_ms,
            calmar_ratio=calmar_ratio,
            recovery_factor=recovery_factor,
            capital_efficiency=capital_efficiency,
            r_distribution=r_distribution,
            duration_distribution=duration_distribution,
            pnl_distribution=pnl_distribution,
            mae_distribution=mae_distribution,
            mfe_distribution=mfe_distribution,
            by_strategy=by_strategy,
            by_regime=by_regime,
            strategy_regime_pf=strategy_regime_pf,
            equity_curve=equity_curve,
            initial_capital=initial_capital,
            analysis_period_start_ms=analysis_period_start_ms,
            analysis_period_end_ms=analysis_period_end_ms,
        )

    def analyze_subset(
        self,
        trades: List[TradeSnapshot],
        initial_capital: float,
        filter_fn: Optional[Callable[[TradeSnapshot], bool]] = None,
    ) -> PerformanceReport:
        """
        Analyze a filtered subset of trades.

        Args:
            trades: List of TradeSnapshot.
            initial_capital: Starting capital.
            filter_fn: Optional function to filter trades.
                       If None, all trades are included.

        Returns:
            PerformanceReport for the filtered subset.

        Example:
            # Analyze only BTC trades
            report = engine.analyze_subset(
                trades,
                initial_capital=10000.0,
                filter_fn=lambda t: t.symbol == "BTCUSDT",
            )
        """
        if filter_fn is None:
            return self.analyze(trades, initial_capital)

        filtered_trades = [t for t in trades if filter_fn(t)]
        return self.analyze(filtered_trades, initial_capital)

    @staticmethod
    def _empty_report(initial_capital: float) -> PerformanceReport:
        """
        Construct an empty report for zero trades.

        All metrics are zero or empty.
        """
        return PerformanceReport(
            trade_count=0,
            win_rate=0.0,
            profit_factor=0.0,
            expectancy_r=0.0,
            expectancy_capital=0.0,
            expectancy_per_dollar=0.0,
            total_pnl_usdt=0.0,
            total_fees_usdt=0.0,
            max_drawdown_pct=0.0,
            max_drawdown_duration_ms=0,
            calmar_ratio=0.0,
            recovery_factor=0.0,
            capital_efficiency=CapitalEfficiencyMetrics(
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
            ),
            r_distribution=None,
            duration_distribution=None,
            pnl_distribution=None,
            mae_distribution=None,
            mfe_distribution=None,
            by_strategy={},
            by_regime={},
            strategy_regime_pf={},
            equity_curve=[],
            initial_capital=initial_capital,
            analysis_period_start_ms=0,
            analysis_period_end_ms=0,
        )
