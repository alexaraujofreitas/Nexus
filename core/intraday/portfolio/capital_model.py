# ============================================================
# NEXUS TRADER — Capital Model (Phase 5)
#
# Tracks account capital, equity, realized/unrealized P&L.
# Owned exclusively by PortfolioState; no external lock needed.
# ============================================================
import logging
import time
from dataclasses import dataclass

from core.intraday.execution_contracts import CapitalSnapshot, InvariantViolation

logger = logging.getLogger(__name__)


@dataclass
class CapitalModel:
    """
    Mutable capital state model. Tracks total capital, reserves, equity,
    drawdown, and daily/total P&L metrics.

    NOT thread-safe on its own — caller (PortfolioState) owns RLock.
    """
    # ── Configuration
    total_capital: float  # Starting capital (immutable after init)

    # ── Live state
    reserved_capital: float = 0.0  # Capital locked in open positions
    equity: float = 0.0            # Current account equity
    peak_equity: float = 0.0       # Highest equity ever reached

    # ── Realized P&L tracking
    realized_pnl_today: float = 0.0      # Resets at UTC midnight
    total_realized_pnl: float = 0.0      # Cumulative
    total_fees: float = 0.0              # Cumulative fees paid
    trade_count_today: int = 0           # Number of trades today (UTC)
    consecutive_losses: int = 0          # Losses since last win

    # ── Timing
    _daily_reset_ts: int = 0  # Last reset timestamp (UTC ms)

    def __post_init__(self) -> None:
        """Initialize equity and daily reset timestamp."""
        if self.equity == 0.0:
            self.equity = self.total_capital
        if self.peak_equity == 0.0:
            self.peak_equity = self.total_capital
        if self._daily_reset_ts == 0:
            self._daily_reset_ts = int(time.time() * 1000)
        logger.info(
            f"CapitalModel initialized: total={self.total_capital}, "
            f"equity={self.equity}, peak={self.peak_equity}"
        )

    @property
    def available_capital(self) -> float:
        """Available capital for new trades (equity - reserved)."""
        return max(0.0, self.equity - self.reserved_capital)

    @property
    def drawdown_pct(self) -> float:
        """Current drawdown from peak equity (%)."""
        if self.peak_equity <= 0:
            return 0.0
        dd = (self.peak_equity - self.equity) / self.peak_equity
        return max(0.0, dd)

    def reserve(self, amount: float) -> bool:
        """
        Attempt to reserve capital for a new position.
        Returns True if successful, False if insufficient available capital.
        """
        if amount <= 0:
            logger.warning(f"reserve: amount must be positive, got {amount}")
            return False
        if self.available_capital < amount - 0.01:  # Allow small rounding error
            logger.warning(
                f"reserve: insufficient available capital. "
                f"requested={amount}, available={self.available_capital}"
            )
            return False
        self.reserved_capital += amount
        logger.debug(f"reserve: {amount}, reserved_total={self.reserved_capital}")
        return True

    def release(
        self,
        amount: float,
        realized_pnl: float,
        fees: float,
        is_win: bool = False
    ) -> None:
        """
        Release reserved capital and record realized P&L + fees.

        Args:
            amount: Capital to release (typically entry_size_usdt)
            realized_pnl: P&L from the closed position
            fees: Fees incurred
            is_win: Whether this was a winning trade
        """
        if amount < 0:
            logger.error(f"release: amount must be non-negative, got {amount}")
            return
        if fees < 0:
            logger.error(f"release: fees must be non-negative, got {fees}")
            return

        # Release capital
        self.reserved_capital = max(0.0, self.reserved_capital - amount)

        # Update P&L
        self.realized_pnl_today += realized_pnl
        self.total_realized_pnl += realized_pnl
        self.total_fees += fees

        # Track consecutive losses/wins
        if realized_pnl > 0.01:
            self.consecutive_losses = 0
        elif realized_pnl < -0.01:
            self.consecutive_losses += 1

        # Increment trade count
        self.trade_count_today += 1

        logger.debug(
            f"release: amount={amount}, realized_pnl={realized_pnl}, "
            f"fees={fees}, consecutive_losses={self.consecutive_losses}"
        )

    def update_equity(self, unrealized_total: float) -> None:
        """
        Update equity from total unrealized P&L across open positions.
        Equity = total_capital + total_realized_pnl + unrealized_total - total_fees
        """
        self.equity = self.total_capital + self.total_realized_pnl + unrealized_total - self.total_fees

        # Update peak if needed
        if self.equity > self.peak_equity:
            self.peak_equity = self.equity
            logger.info(f"New peak equity: {self.peak_equity}")

        logger.debug(
            f"update_equity: unrealized_total={unrealized_total}, "
            f"equity={self.equity}, peak={self.peak_equity}"
        )

    def reset_daily(self, now_ms: int = 0) -> None:
        """Reset daily P&L counters at UTC midnight."""
        if now_ms == 0:
            now_ms = int(time.time() * 1000)

        self.realized_pnl_today = 0.0
        self.trade_count_today = 0
        self._daily_reset_ts = now_ms

        logger.info(
            f"Daily reset: realized_pnl_today={self.realized_pnl_today}, "
            f"trade_count_today={self.trade_count_today}"
        )

    def assert_invariants(self, unrealized_pnl: float = 0.0) -> None:
        """
        Verify ALL 7 capital invariants. Raises InvariantViolation on failure.

        Short-circuits on first violation for fast feedback.

        Parameters
        ----------
        unrealized_pnl : float
            Total unrealized P&L across open positions.  Required for
            INV-4 (accounting identity).  Defaults to 0.0 when called
            outside of a price-update context (e.g. at init).

        Invariants
        ----------
        INV-1  available_capital >= 0          (no negative available)
        INV-2  reserved_capital >= 0           (no negative reservation)
        INV-3  peak_equity >= equity           (peak is monotonic high-water)
        INV-4  equity ≈ total_capital + total_realized_pnl
                        + unrealized_pnl - total_fees   (accounting identity)
        INV-5  reserved_capital <= equity      (no over-reservation)
        INV-6  total_capital > 0               (positive starting capital)
        INV-7  trade_count_today >= 0          (non-negative trade count)
        """
        violations = []
        EPS = 0.01  # Rounding tolerance for float comparisons

        # INV-1: available_capital >= 0
        if self.available_capital < -EPS:
            violations.append(
                f"INV-1: available_capital < 0: {self.available_capital:.4f} "
                f"(equity={self.equity:.4f}, reserved={self.reserved_capital:.4f})"
            )

        # INV-2: reserved_capital >= 0
        if self.reserved_capital < -EPS:
            violations.append(
                f"INV-2: reserved_capital < 0: {self.reserved_capital:.4f}"
            )

        # INV-3: peak_equity >= equity (high-water mark)
        if self.peak_equity < self.equity - EPS:
            violations.append(
                f"INV-3: peak_equity < equity: "
                f"peak={self.peak_equity:.4f}, equity={self.equity:.4f}"
            )

        # INV-4: Accounting identity
        expected_equity = (
            self.total_capital
            + self.total_realized_pnl
            + unrealized_pnl
            - self.total_fees
        )
        if abs(self.equity - expected_equity) > EPS:
            violations.append(
                f"INV-4: accounting identity violated: "
                f"equity={self.equity:.4f} != expected={expected_equity:.4f} "
                f"(total={self.total_capital:.4f}, realized={self.total_realized_pnl:.4f}, "
                f"unrealized={unrealized_pnl:.4f}, fees={self.total_fees:.4f})"
            )

        # INV-5: reserved_capital <= equity (no over-reservation)
        if self.reserved_capital > self.equity + EPS:
            violations.append(
                f"INV-5: over-reservation: "
                f"reserved={self.reserved_capital:.4f} > equity={self.equity:.4f}"
            )

        # INV-6: total_capital > 0 (positive starting capital)
        if self.total_capital <= 0:
            violations.append(
                f"INV-6: total_capital must be positive: {self.total_capital:.4f}"
            )

        # INV-7: trade_count_today >= 0 (non-negative)
        if self.trade_count_today < 0:
            violations.append(
                f"INV-7: trade_count_today < 0: {self.trade_count_today}"
            )

        if violations:
            msg = (
                f"Capital invariant violations ({len(violations)}): "
                + "; ".join(violations)
            )
            logger.error(msg)
            raise InvariantViolation(msg)

    def snapshot(self) -> CapitalSnapshot:
        """Return a frozen snapshot of current capital state."""
        return CapitalSnapshot(
            total_capital=self.total_capital,
            reserved_capital=self.reserved_capital,
            available_capital=self.available_capital,
            equity=self.equity,
            peak_equity=self.peak_equity,
            drawdown_pct=self.drawdown_pct,
            realized_pnl_today=self.realized_pnl_today,
            total_realized_pnl=self.total_realized_pnl,
            total_fees=self.total_fees,
            trade_count_today=self.trade_count_today,
            consecutive_losses=self.consecutive_losses,
        )
