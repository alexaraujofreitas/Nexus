# ============================================================
# NEXUS TRADER — Fill Simulator (Phase 5)
#
# Simulates order fills with configurable fee and slippage models.
# Protocol-based design: FeeModel and SlippageModel can be swapped
# for live or test implementations.
#
# Production use: deterministic, fully logged, contract-validated.
# ============================================================
import logging
import random
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass

from core.intraday.execution_contracts import (
    FillRecord,
    OrderRecord,
    Side,
    _make_id,
)

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════
# PROTOCOL INTERFACES
# ══════════════════════════════════════════════════════════════


class FeeModel(ABC):
    """Abstract base for fee calculation models."""

    @abstractmethod
    def calculate_fee(self, price: float, quantity: float, is_maker: bool) -> float:
        """
        Calculate fee in USDT.

        Args:
            price: Fill price
            quantity: Quantity filled
            is_maker: True if maker, False if taker

        Returns:
            Fee amount in USDT (non-negative)
        """
        pass


class SlippageModel(ABC):
    """Abstract base for slippage calculation models."""

    @abstractmethod
    def calculate_slippage(self, price: float, side: Side, seed: int = None) -> float:
        """
        Calculate slippage (positive or negative).

        Args:
            price: Current/requested price
            side: BUY or SELL
            seed: Optional random seed for determinism

        Returns:
            Slippage amount (added to price for BUY, subtracted for SELL)
        """
        pass


# ══════════════════════════════════════════════════════════════
# DEFAULT IMPLEMENTATIONS
# ══════════════════════════════════════════════════════════════


class DefaultFeeModel(FeeModel):
    """Default tiered fee model: maker 0.0002, taker 0.0004."""

    def __init__(self, taker_rate: float = 0.0004, maker_rate: float = 0.0002):
        """
        Initialize fee model.

        Args:
            taker_rate: Taker fee rate (0.0004 = 0.04%)
            maker_rate: Maker fee rate (0.0002 = 0.02%)
        """
        self.taker_rate = taker_rate
        self.maker_rate = maker_rate
        logger.debug(
            f"DefaultFeeModel initialized: taker={taker_rate*100:.2f}%, "
            f"maker={maker_rate*100:.2f}%"
        )

    def calculate_fee(self, price: float, quantity: float, is_maker: bool) -> float:
        """Calculate fee in USDT."""
        notional = price * quantity
        rate = self.maker_rate if is_maker else self.taker_rate
        fee = notional * rate
        logger.debug(
            f"Fee calculated: notional={notional:.2f} USDT, "
            f"rate={rate*100:.4f}% ({'maker' if is_maker else 'taker'}), "
            f"fee={fee:.6f} USDT"
        )
        return fee


class DefaultSlippageModel(SlippageModel):
    """Default slippage model: uniform 0–0.02% in direction of execution."""

    def __init__(self, max_slippage_pct: float = 0.0002):
        """
        Initialize slippage model.

        Args:
            max_slippage_pct: Maximum slippage as fraction (0.0002 = 0.02%)
        """
        self.max_slippage_pct = max_slippage_pct
        logger.debug(f"DefaultSlippageModel initialized: max={max_slippage_pct*100:.4f}%")

    def calculate_slippage(self, price: float, side: Side, seed: int = None) -> float:
        """
        Calculate slippage deterministically.

        For BUY: positive slippage (price increases)
        For SELL: negative slippage (price decreases)
        """
        rng = random.Random(seed)
        slippage_pct = rng.uniform(0.0, self.max_slippage_pct)

        if side == Side.BUY:
            slippage = price * slippage_pct
        else:  # SELL
            slippage = -(price * slippage_pct)

        logger.debug(
            f"Slippage calculated: price={price:.2f}, side={side.value}, "
            f"slippage_pct={slippage_pct*100:.4f}%, slippage={slippage:.6f}"
        )
        return slippage


# ══════════════════════════════════════════════════════════════
# FILL SIMULATOR
# ══════════════════════════════════════════════════════════════


class FillSimulator:
    """
    Simulates order fills with configurable fee and slippage.

    Production use: stateless, deterministic, fully logged.
    """

    def __init__(
        self,
        fee_model: FeeModel = None,
        slippage_model: SlippageModel = None,
    ):
        """
        Initialize fill simulator.

        Args:
            fee_model: FeeModel instance (defaults to DefaultFeeModel)
            slippage_model: SlippageModel instance (defaults to DefaultSlippageModel)
        """
        self.fee_model = fee_model or DefaultFeeModel()
        self.slippage_model = slippage_model or DefaultSlippageModel()
        logger.info(
            f"FillSimulator initialized with {self.fee_model.__class__.__name__} "
            f"and {self.slippage_model.__class__.__name__}"
        )

    def simulate_fill(
        self, order: OrderRecord, now_ms: int = None, seed: int = None
    ) -> FillRecord:
        """
        Simulate a fill for an order.

        Args:
            order: OrderRecord with requested price and quantity
            now_ms: Timestamp override (ms); defaults to current time
            seed: Random seed for determinism

        Returns:
            FillRecord representing the simulated fill

        Raises:
            ValueError: If order is invalid
        """
        if now_ms is None:
            now_ms = int(time.time() * 1000)

        logger.debug(f"Simulating fill for order {order.order_id}")

        # Calculate slippage
        slippage = self.slippage_model.calculate_slippage(
            order.requested_price, order.side, seed=seed
        )
        fill_price = order.requested_price + slippage

        # Calculate fee (always taker for paper trading)
        fee_usdt = self.fee_model.calculate_fee(
            fill_price, order.requested_quantity, is_maker=False
        )

        # Slippage as percentage
        slippage_pct = (slippage / order.requested_price) if order.requested_price > 0 else 0.0

        # Generate fill ID
        fill_id = _make_id(order.order_id, now_ms, 0)

        logger.info(
            f"Fill simulated: order_id={order.order_id}, "
            f"requested={order.requested_price:.2f}@{order.requested_quantity:.4f}, "
            f"fill_price={fill_price:.2f}, slippage_pct={slippage_pct*100:.4f}%, "
            f"fee={fee_usdt:.6f} USDT"
        )

        fill_record = FillRecord(
            fill_id=fill_id,
            order_id=order.order_id,
            symbol=order.symbol,
            side=order.side,
            price=fill_price,
            quantity=order.requested_quantity,
            fee_usdt=fee_usdt,
            fee_rate=self.fee_model.taker_rate,
            slippage_pct=slippage_pct,
            is_maker=False,
            filled_at_ms=now_ms,
        )

        logger.debug(f"FillRecord created: {fill_record.fill_id}")
        return fill_record
