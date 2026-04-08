# ============================================================
# NEXUS TRADER — Exposure Tracker (Phase 5)
#
# Stateless calculator for portfolio exposure metrics.
# ============================================================
import logging
from typing import List

from core.intraday.execution_contracts import ExposureSnapshot, PositionRecord
from core.intraday.signal_contracts import Direction

logger = logging.getLogger(__name__)


class ExposureTracker:
    """
    Stateless exposure calculator. Takes open positions and capital,
    returns frozen ExposureSnapshot with per-symbol, long/short, and heat metrics.
    """

    @staticmethod
    def calculate(positions: List[PositionRecord], total_capital: float) -> ExposureSnapshot:
        """
        Calculate exposure metrics from open positions.

        Args:
            positions: List of open PositionRecord objects
            total_capital: Total account capital

        Returns:
            ExposureSnapshot with frozen metrics
        """
        if total_capital <= 0:
            logger.warning(f"calculate: total_capital <= 0, got {total_capital}")
            return ExposureSnapshot(
                per_symbol={},
                long_exposure=0.0,
                short_exposure=0.0,
                net_exposure=0.0,
                portfolio_heat=0.0,
            )

        # Per-symbol exposure: sum of entry_size_usdt / total_capital
        per_symbol = {}
        long_exposure_total = 0.0
        short_exposure_total = 0.0
        portfolio_heat = 0.0

        for pos in positions:
            # Per-symbol fraction
            symbol_fraction = pos.entry_size_usdt / total_capital
            if pos.symbol in per_symbol:
                per_symbol[pos.symbol] += symbol_fraction
            else:
                per_symbol[pos.symbol] = symbol_fraction

            # Long vs short
            if pos.direction == Direction.LONG:
                long_exposure_total += symbol_fraction
            else:
                short_exposure_total += symbol_fraction

            # Portfolio heat (total risk / capital)
            portfolio_heat += pos.total_risk_usdt / total_capital

        net_exposure = long_exposure_total - short_exposure_total

        logger.debug(
            f"calculate: {len(positions)} positions, "
            f"long={long_exposure_total:.4f}, short={short_exposure_total:.4f}, "
            f"net={net_exposure:.4f}, heat={portfolio_heat:.4f}"
        )

        return ExposureSnapshot(
            per_symbol=per_symbol,
            long_exposure=long_exposure_total,
            short_exposure=short_exposure_total,
            net_exposure=net_exposure,
            portfolio_heat=portfolio_heat,
        )
