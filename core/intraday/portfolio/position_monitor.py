# ============================================================
# NEXUS TRADER — Position Monitor (Phase 5)
#
# Monitors open positions for exit conditions:
#   - Stop loss hit
#   - Take profit hit
#   - Time stop (max bars held)
#   - Auto-partial (1R reached)
# ============================================================
import logging
import time
from typing import List, Optional

from core.intraday.execution_contracts import CloseReason, PositionStatus
from core.intraday.portfolio.portfolio_state import PortfolioState
from core.intraday.signal_contracts import Direction

logger = logging.getLogger(__name__)


class PositionMonitor:
    """
    Monitors open positions for exit conditions.
    Not thread-safe on its own — caller must synchronize access to PortfolioState.
    """

    def __init__(
        self,
        portfolio_state: PortfolioState,
        time_stop_bars: int = 20,
    ) -> None:
        """
        Initialize position monitor.

        Args:
            portfolio_state: Reference to PortfolioState
            time_stop_bars: Max bars a position can be held
        """
        self._portfolio = portfolio_state
        self._time_stop_bars = time_stop_bars
        logger.info(f"PositionMonitor initialized: time_stop_bars={time_stop_bars}")

    def check_positions(
        self,
        symbol: str,
        current_price: float,
        now_ms: Optional[int] = None,
    ) -> List[dict]:
        """
        Check all open positions on a symbol for exit conditions.
        Returns list of events describing what happened (for EventBus publishing).

        Args:
            symbol: Symbol being checked (e.g. "BTC/USDT")
            current_price: Current market price
            now_ms: Current time in ms (defaults to now)

        Returns:
            List of event dicts describing closes/partials
        """
        if now_ms is None:
            now_ms = int(time.time() * 1000)

        events = []

        # Get all open positions on this symbol
        positions = self._portfolio.get_open_by_symbol(symbol)
        logger.debug(f"check_positions: {symbol} {len(positions)} open positions")

        for pos in positions:
            if pos.status != PositionStatus.OPEN and pos.status != PositionStatus.PARTIALLY_CLOSED:
                continue

            # Update current price
            self._portfolio.update_price(pos.position_id, current_price)

            # Increment bars held
            pos.bars_held += 1

            # Check SL
            if self._check_stop_loss(pos, current_price):
                closed_pos = self._portfolio.close_position(
                    pos.position_id,
                    current_price,
                    CloseReason.SL_HIT.value,
                    now_ms,
                )
                if closed_pos:
                    events.append({
                        "type": "position_closed",
                        "position_id": pos.position_id,
                        "reason": CloseReason.SL_HIT.value,
                        "price": current_price,
                    })
                continue

            # Check TP
            if self._check_take_profit(pos, current_price):
                closed_pos = self._portfolio.close_position(
                    pos.position_id,
                    current_price,
                    CloseReason.TP_HIT.value,
                    now_ms,
                )
                if closed_pos:
                    events.append({
                        "type": "position_closed",
                        "position_id": pos.position_id,
                        "reason": CloseReason.TP_HIT.value,
                        "price": current_price,
                    })
                continue

            # Check time stop
            if self._check_time_stop(pos):
                closed_pos = self._portfolio.close_position(
                    pos.position_id,
                    current_price,
                    CloseReason.TIME_STOP.value,
                    now_ms,
                )
                if closed_pos:
                    events.append({
                        "type": "position_closed",
                        "position_id": pos.position_id,
                        "reason": CloseReason.TIME_STOP.value,
                        "price": current_price,
                    })
                continue

            # Check auto-partial (1R reached)
            if self._check_auto_partial(pos, current_price):
                partial_result = self._portfolio.partial_close(
                    pos.position_id,
                    0.33,  # Close 1/3
                    current_price,
                    now_ms,
                )
                if partial_result:
                    fill, event = partial_result
                    # Move SL to entry (breakeven)
                    pos.stop_loss = pos.entry_price
                    pos.breakeven_applied = True
                    events.append({
                        "type": "partial_close",
                        "position_id": pos.position_id,
                        "pct_closed": 0.33,
                        "price": current_price,
                    })

        if events:
            logger.info(f"check_positions: {symbol} {len(events)} exits triggered")

        return events

    def _check_stop_loss(self, pos, current_price: float) -> bool:
        """Check if stop loss has been hit."""
        hit = False
        if pos.direction == Direction.LONG:
            hit = current_price <= pos.stop_loss
        else:  # SHORT
            hit = current_price >= pos.stop_loss

        if hit:
            logger.info(
                f"_check_stop_loss: {pos.symbol} {pos.direction.value} "
                f"SL={pos.stop_loss} price={current_price}"
            )
        return hit

    def _check_take_profit(self, pos, current_price: float) -> bool:
        """Check if take profit has been hit."""
        hit = False
        if pos.direction == Direction.LONG:
            hit = current_price >= pos.take_profit
        else:  # SHORT
            hit = current_price <= pos.take_profit

        if hit:
            logger.info(
                f"_check_take_profit: {pos.symbol} {pos.direction.value} "
                f"TP={pos.take_profit} price={current_price}"
            )
        return hit

    def _check_time_stop(self, pos) -> bool:
        """Check if time stop (max bars held) has been exceeded."""
        hit = pos.bars_held >= self._time_stop_bars
        if hit:
            logger.info(
                f"_check_time_stop: {pos.symbol} bars_held={pos.bars_held} "
                f">= limit={self._time_stop_bars}"
            )
        return hit

    def _check_auto_partial(self, pos, current_price: float) -> bool:
        """
        Check if auto-partial should trigger:
        - Unrealized P&L >= 1R (total_risk_usdt)
        - Not already applied
        """
        if pos.auto_partial_applied:
            return False

        # Update price to get current unrealized P&L
        pos.update_price(current_price)

        total_risk = pos.total_risk_usdt
        hit = pos.unrealized_pnl_usdt >= total_risk

        if hit:
            logger.info(
                f"_check_auto_partial: {pos.symbol} {pos.direction.value} "
                f"unrealized={pos.unrealized_pnl_usdt} >= risk={total_risk}"
            )

        return hit
