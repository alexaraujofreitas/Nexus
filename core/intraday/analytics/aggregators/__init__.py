"""
Phase 7: Trade Aggregators

Groups trades by strategy, regime, and other dimensions.
Computes aggregated metrics per group.

PURE FUNCTIONS: deterministic, no mutable state.

Exports:
  - StrategyAggregator, StrategyPerformance
  - RegimeAggregator, RegimePerformance
"""

from .by_strategy import StrategyAggregator, StrategyPerformance
from .by_regime import RegimeAggregator, RegimePerformance

__all__ = [
    "StrategyAggregator",
    "StrategyPerformance",
    "RegimeAggregator",
    "RegimePerformance",
]
