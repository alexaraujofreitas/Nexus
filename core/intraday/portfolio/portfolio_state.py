# ============================================================
# NEXUS TRADER — Portfolio State (Phase 5)
#
# Central mutable portfolio state manager with thread-safe locking.
# Owns positions, capital, and listener dispatch.
# ============================================================
import logging
import time
from threading import RLock
from typing import Callable, Dict, List, Optional, Tuple

from core.intraday.execution_contracts import (
    CapitalSnapshot,
    CloseReason,
    FillRecord,
    PositionRecord,
    PositionStatus,
    PortfolioSnapshot,
    Side,
    TradeRecord,
)
from core.intraday.portfolio.capital_model import CapitalModel
from core.intraday.portfolio.exposure_tracker import ExposureTracker
from core.intraday.signal_contracts import Direction

logger = logging.getLogger(__name__)


class PortfolioState:
    """
    Central state manager for intraday portfolio. Thread-safe via RLock.
    Owns open positions, capital model, and event listeners.

    Listeners are called OUTSIDE the lock to prevent deadlocks.
    """

    def __init__(self, total_capital: float) -> None:
        """
        Initialize portfolio state.

        Args:
            total_capital: Starting account capital
        """
        self._lock = RLock()
        self._open_positions: Dict[str, PositionRecord] = {}
        self._capital = CapitalModel(total_capital=total_capital)

        # Event listeners: {event_type: [callbacks]}
        self._listeners: Dict[str, List[Callable[[dict], None]]] = {
            "position_opened": [],
            "position_updated": [],
            "position_closed": [],
            "partial_close": [],
        }

        logger.info(f"PortfolioState initialized: capital={total_capital}")

    def add_listener(self, event_type: str, callback: Callable[[dict], None]) -> None:
        """
        Register a listener for portfolio events.

        Args:
            event_type: "position_opened", "position_updated", "position_closed", "partial_close"
            callback: Function(event_dict) -> None
        """
        if event_type not in self._listeners:
            logger.warning(f"add_listener: unknown event_type {event_type}")
            return
        self._listeners[event_type].append(callback)
        logger.debug(f"add_listener: registered {callback.__name__} for {event_type}")

    def open_position(
        self,
        fill: FillRecord,
        metadata: dict,
    ) -> PositionRecord:
        """
        Open a new position from a fill event + metadata.

        Args:
            fill: FillRecord from order execution
            metadata: Dict with decision_id, trigger_id, setup_id, strategy_name,
                     strategy_class, direction, stop_loss, take_profit,
                     original_stop_loss, regime, candle_trace_ids

        Returns:
            Created PositionRecord

        Raises:
            ValueError if reserve fails or metadata incomplete
        """
        # Validate metadata
        required_keys = {
            "decision_id", "trigger_id", "setup_id", "strategy_name",
            "strategy_class", "direction", "stop_loss", "take_profit",
            "original_stop_loss", "regime", "candle_trace_ids"
        }
        missing = required_keys - set(metadata.keys())
        if missing:
            raise ValueError(f"open_position: missing metadata keys: {missing}")

        with self._lock:
            # Reserve capital
            if not self._capital.reserve(fill.notional_usdt):
                raise ValueError(
                    f"open_position: failed to reserve {fill.notional_usdt} USDT"
                )

            # Create position record
            position_id = f"{fill.order_id}_{int(time.time() * 1000)}"
            direction = metadata["direction"]
            if isinstance(direction, str):
                direction = Direction.LONG if direction == "long" else Direction.SHORT

            pos = PositionRecord(
                position_id=position_id,
                order_id=fill.order_id,
                decision_id=metadata["decision_id"],
                trigger_id=metadata["trigger_id"],
                setup_id=metadata["setup_id"],
                symbol=fill.symbol,
                direction=direction,
                strategy_name=metadata["strategy_name"],
                strategy_class=metadata["strategy_class"],
                entry_price=fill.price,
                entry_size_usdt=fill.notional_usdt,
                current_size_usdt=fill.notional_usdt,
                quantity=fill.quantity,
                stop_loss=metadata["stop_loss"],
                original_stop_loss=metadata["original_stop_loss"],
                take_profit=metadata["take_profit"],
                current_price=fill.price,
                fee_total_usdt=fill.fee_usdt,
                regime_at_entry=metadata["regime"],
                candle_trace_ids=tuple(metadata.get("candle_trace_ids", [])),
            )

            self._open_positions[position_id] = pos
            logger.info(
                f"open_position: {position_id} ({fill.symbol} {direction.value}) "
                f"entry={fill.price}, qty={fill.quantity}, size={fill.notional_usdt}"
            )

            # Prepare event (copy data for thread safety)
            event = {
                "position_id": position_id,
                "symbol": fill.symbol,
                "direction": direction.value,
                "entry_price": fill.price,
                "quantity": fill.quantity,
                "entry_size_usdt": fill.notional_usdt,
                "strategy_name": metadata["strategy_name"],
            }

        # Call listeners OUTSIDE lock
        self._dispatch_event("position_opened", event)

        return pos

    def update_price(self, position_id: str, price: float) -> Optional[PositionRecord]:
        """
        Update position's current price (used in mark-to-market).

        Args:
            position_id: Position ID
            price: New price

        Returns:
            Updated PositionRecord or None if not found
        """
        with self._lock:
            pos = self._open_positions.get(position_id)
            if not pos:
                logger.warning(f"update_price: position not found: {position_id}")
                return None

            pos.update_price(price)

            # Prepare event
            event = {
                "position_id": position_id,
                "current_price": price,
                "unrealized_pnl_usdt": pos.unrealized_pnl_usdt,
                "unrealized_pnl_pct": pos.unrealized_pnl_pct,
            }

        # Call listeners OUTSIDE lock
        self._dispatch_event("position_updated", event)

        return pos

    def partial_close(
        self,
        position_id: str,
        pct: float,
        price: float,
        now_ms: Optional[int] = None,
    ) -> Optional[Tuple[FillRecord, dict]]:
        """
        Partially close a position by reducing quantity/size.

        Args:
            position_id: Position ID to partially close
            pct: Percentage to close (0.0-1.0, typically 0.33 for 1/3)
            price: Exit price
            now_ms: Current time in ms (defaults to now)

        Returns:
            Tuple of (FillRecord for closed portion, event_dict) or None if not found
        """
        if now_ms is None:
            now_ms = int(time.time() * 1000)

        with self._lock:
            pos = self._open_positions.get(position_id)
            if not pos:
                logger.warning(f"partial_close: position not found: {position_id}")
                return None

            if pct <= 0 or pct > 1.0:
                logger.error(f"partial_close: invalid pct {pct}")
                return None

            # Calculate closed portion
            closed_qty = pos.quantity * pct
            closed_size = pos.current_size_usdt * pct

            # Compute realized P&L for closed portion
            pnl_per_unit = pos.compute_r_multiple(price)
            realized_pnl = (price - pos.entry_price) * closed_qty
            if pos.direction == Direction.SHORT:
                realized_pnl = (pos.entry_price - price) * closed_qty

            # Fee (proportional to closed size)
            fee_closed = pos.fee_total_usdt * pct

            # Create fill record for closed portion
            fill_id = f"{position_id}_partial_{int(time.time() * 1000)}"
            fill = FillRecord(
                fill_id=fill_id,
                order_id=pos.order_id,
                symbol=pos.symbol,
                side=Side.from_direction(pos.direction),
                price=price,
                quantity=closed_qty,
                fee_usdt=fee_closed,
                fee_rate=0.0,
                slippage_pct=0.0,
                is_maker=True,
                filled_at_ms=now_ms,
            )

            # Update position
            pos.quantity -= closed_qty
            pos.current_size_usdt -= closed_size
            pos.realized_pnl_usdt += realized_pnl
            pos.fee_total_usdt -= fee_closed
            pos.status = PositionStatus.PARTIALLY_CLOSED
            pos.auto_partial_applied = True

            # Release capital
            self._capital.release(
                amount=closed_size,
                realized_pnl=realized_pnl,
                fees=fee_closed,
                is_win=(realized_pnl > 0.01),
            )

            logger.info(
                f"partial_close: {position_id} closed {pct*100:.1f}%, "
                f"qty {closed_qty}, realized_pnl={realized_pnl}"
            )

            # Prepare event (copy data for thread safety)
            event = {
                "position_id": position_id,
                "symbol": pos.symbol,
                "closed_qty": closed_qty,
                "closed_size": closed_size,
                "price": price,
                "realized_pnl": realized_pnl,
                "remaining_qty": pos.quantity,
                "remaining_size": pos.current_size_usdt,
            }

        # Call listeners OUTSIDE lock
        self._dispatch_event("partial_close", event)

        return (fill, event)

    def close_position(
        self,
        position_id: str,
        price: float,
        reason: str,
        now_ms: Optional[int] = None,
    ) -> Optional[PositionRecord]:
        """
        Close a position entirely.

        Args:
            position_id: Position ID to close
            price: Exit price
            reason: Close reason (e.g. CloseReason.TP_HIT.value)
            now_ms: Current time in ms (defaults to now)

        Returns:
            Closed PositionRecord or None if not found
        """
        if now_ms is None:
            now_ms = int(time.time() * 1000)

        with self._lock:
            pos = self._open_positions.get(position_id)
            if not pos:
                logger.warning(f"close_position: position not found: {position_id}")
                return None

            # Compute final realized P&L
            if pos.direction == Direction.LONG:
                realized_pnl = (price - pos.entry_price) * pos.quantity
            else:
                realized_pnl = (pos.entry_price - price) * pos.quantity

            # Compute R-multiple
            r_multiple = pos.compute_r_multiple(price)

            # Update position
            pos.close_price = price
            pos.realized_pnl_usdt = realized_pnl
            pos.status = PositionStatus.CLOSED
            pos.close_reason = reason
            pos.closed_at_ms = now_ms
            pos.r_multiple = r_multiple

            # Release capital
            self._capital.release(
                amount=pos.current_size_usdt,
                realized_pnl=realized_pnl,
                fees=pos.fee_total_usdt,
                is_win=(realized_pnl > 0.01),
            )

            logger.info(
                f"close_position: {position_id} ({pos.symbol} {pos.direction.value}) "
                f"at {price}, realized_pnl={realized_pnl}, r_multiple={r_multiple}"
            )

            # Prepare event (copy data for thread safety)
            event = {
                "position_id": position_id,
                "symbol": pos.symbol,
                "direction": pos.direction.value,
                "entry_price": pos.entry_price,
                "exit_price": price,
                "quantity": pos.quantity,
                "realized_pnl": realized_pnl,
                "r_multiple": r_multiple,
                "close_reason": reason,
                "bars_held": pos.bars_held,
            }

        # Call listeners OUTSIDE lock
        self._dispatch_event("position_closed", event)

        return pos

    def get_position(self, position_id: str) -> Optional[PositionRecord]:
        """Get a position by ID (thread-safe copy)."""
        with self._lock:
            return self._open_positions.get(position_id)

    def get_open_by_symbol(self, symbol: str) -> List[PositionRecord]:
        """Get all open positions for a symbol."""
        with self._lock:
            return [p for p in self._open_positions.values() if p.symbol == symbol]

    def get_snapshot(self) -> PortfolioSnapshot:
        """
        Get a frozen snapshot of current portfolio state.

        Returns:
            PortfolioSnapshot with capital, exposure, and positions
        """
        with self._lock:
            # Calculate total unrealized P&L
            unrealized_total = sum(p.unrealized_pnl_usdt for p in self._open_positions.values())

            # Update equity
            self._capital.update_equity(unrealized_total)

            # Get capital snapshot
            capital_snap = self._capital.snapshot()

            # Get exposure snapshot
            positions_list = list(self._open_positions.values())
            exposure_snap = ExposureTracker.calculate(
                positions_list,
                self._capital.total_capital
            )

            # Create portfolio snapshot
            return PortfolioSnapshot(
                capital=capital_snap,
                exposure=exposure_snap,
                open_positions=tuple(positions_list),
                open_position_count=len(positions_list),
            )

    def assert_invariants(self) -> None:
        """Assert all capital invariants (passes unrealized P&L for INV-4)."""
        with self._lock:
            unrealized = sum(
                pos.unrealized_pnl_usdt for pos in self._positions.values()
            )
            self._capital.assert_invariants(unrealized_pnl=unrealized)

    def _dispatch_event(self, event_type: str, event_data: dict) -> None:
        """
        Dispatch an event to all registered listeners (OUTSIDE lock).

        Args:
            event_type: Event type key
            event_data: Event data dict
        """
        callbacks = self._listeners.get(event_type, [])
        for callback in callbacks:
            try:
                callback(event_data)
            except Exception as e:
                logger.error(
                    f"_dispatch_event: callback raised exception for {event_type}: {e}",
                    exc_info=True
                )
