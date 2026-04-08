# ============================================================
# NEXUS TRADER — Trade Ledger (Phase 5)
#
# Append-only in-memory ledger of completed trades.
# ============================================================
import logging
import time
from typing import List, Optional

from core.intraday.execution_contracts import TradeRecord

logger = logging.getLogger(__name__)


class TradeLedger:
    """
    Immutable in-memory ledger of completed TradeRecord entries.
    Append-only. Supports querying by date, strategy, and loss streaks.
    """

    def __init__(self) -> None:
        """Initialize empty ledger."""
        self._trades: List[TradeRecord] = []
        logger.debug("TradeLedger initialized")

    def append(self, trade: TradeRecord) -> None:
        """
        Append a completed trade to the ledger.

        Args:
            trade: TradeRecord to add
        """
        self._trades.append(trade)
        logger.debug(
            f"append: {trade.position_id} ({trade.symbol} {trade.direction}) "
            f"realized_pnl={trade.realized_pnl_usdt}, r_multiple={trade.r_multiple}"
        )

    def get_today(self, now_ms: Optional[int] = None) -> List[TradeRecord]:
        """
        Return trades closed today (since UTC midnight).

        Args:
            now_ms: Current time in ms (defaults to now)

        Returns:
            List of TradeRecord objects from today
        """
        if now_ms is None:
            now_ms = int(time.time() * 1000)

        # Calculate UTC midnight timestamp for today
        now_s = now_ms // 1000
        now_dt = __import__('datetime').datetime.utcfromtimestamp(now_s)
        midnight_dt = now_dt.replace(hour=0, minute=0, second=0, microsecond=0)
        midnight_ms = int(midnight_dt.timestamp() * 1000)

        today_trades = [t for t in self._trades if t.closed_at_ms >= midnight_ms]
        logger.debug(f"get_today: {len(today_trades)} trades since {midnight_ms}")
        return today_trades

    def get_by_strategy(self, name: str) -> List[TradeRecord]:
        """
        Return all trades by strategy name.

        Args:
            name: Strategy name to filter by

        Returns:
            List of TradeRecord objects
        """
        strategy_trades = [t for t in self._trades if t.strategy_name == name]
        logger.debug(f"get_by_strategy: {len(strategy_trades)} trades for {name}")
        return strategy_trades

    def get_all(self) -> List[TradeRecord]:
        """Return all trades in the ledger."""
        return list(self._trades)

    def get_consecutive_losses(self) -> int:
        """
        Return count of consecutive losses from the tail.
        Stops counting at the first winning trade (realized_pnl > 0).

        Returns:
            Number of consecutive losing trades
        """
        count = 0
        for trade in reversed(self._trades):
            if trade.realized_pnl_usdt > 0.01:
                break
            if trade.realized_pnl_usdt <= -0.01:
                count += 1
        logger.debug(f"get_consecutive_losses: {count}")
        return count

    def __len__(self) -> int:
        """Return total number of trades in ledger."""
        return len(self._trades)
