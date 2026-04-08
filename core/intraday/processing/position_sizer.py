# ============================================================
# NEXUS TRADER — Position Sizer  (Phase 5)
#
# Stateless position sizing logic: risk-based and hard-capped.
# No state mutations, no side effects, pure function.
#
# Formula:
#   risk_usdt = risk_pct * available_capital
#   quantity = risk_usdt / abs(entry_price - stop_loss)
#   size_usdt = quantity * entry_price
#   size_usdt = min(size_usdt, max_capital_pct * total_capital)
#   size_usdt = max(size_usdt, 0)  if computed < min_size, reject
#
# ZERO PySide6 imports.
# ============================================================
import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PositionSizerConfig:
    """Configuration for position sizing."""
    risk_pct: float = 0.005             # Default 0.5% per trade
    max_capital_pct: float = 0.04       # Default 4% per-trade cap
    min_size_usdt: float = 10.0         # Default minimum size


class PositionSizer:
    """
    Stateless position sizer. Computes size_usdt, quantity, and risk_usdt
    from entry price, stop loss, and capital state.
    """

    def __init__(self, config: PositionSizerConfig = None):
        """
        Initialize with optional config. Uses defaults if None.

        Parameters
        ----------
        config : PositionSizerConfig, optional
            Configuration object. If None, uses PositionSizerConfig() defaults.
        """
        self.config = config or PositionSizerConfig()
        logger.info(
            "PositionSizer initialized: risk_pct=%.4f%%, max_capital_pct=%.2f%%, "
            "min_size_usdt=%.2f",
            self.config.risk_pct * 100,
            self.config.max_capital_pct * 100,
            self.config.min_size_usdt,
        )

    def calculate(
        self,
        entry_price: float,
        stop_loss: float,
        available_capital: float,
        total_capital: float,
        config: PositionSizerConfig = None,
    ) -> dict:
        """
        Calculate position size given entry price, stop loss, and capital.

        Parameters
        ----------
        entry_price : float
            Entry price for the trade
        stop_loss : float
            Stop loss price (invalidation level)
        available_capital : float
            Available capital for new positions
        total_capital : float
            Total account capital (for max cap)
        config : PositionSizerConfig, optional
            Override config for this calculation. Uses instance config if None.

        Returns
        -------
        dict with keys:
            - size_usdt: Final position size in USDT (capped and floored)
            - quantity: Quantity in base asset (size_usdt / entry_price)
            - risk_usdt: Dollar risk on this trade (from Kelly formula)

        Notes
        -----
        - If computed size < min_size_usdt, returns size_usdt=0, quantity=0.
        - Validates all inputs are positive.
        - Logs calculation details and any capping/flooring applied.
        """
        cfg = config or self.config

        # ── Input validation ──────────────────────────────────
        if entry_price <= 0:
            logger.error("PositionSizer: entry_price must be positive, got %.2f", entry_price)
            return {"size_usdt": 0.0, "quantity": 0.0, "risk_usdt": 0.0}

        if stop_loss <= 0:
            logger.error("PositionSizer: stop_loss must be positive, got %.2f", stop_loss)
            return {"size_usdt": 0.0, "quantity": 0.0, "risk_usdt": 0.0}

        if available_capital <= 0:
            logger.error(
                "PositionSizer: available_capital must be positive, got %.2f",
                available_capital,
            )
            return {"size_usdt": 0.0, "quantity": 0.0, "risk_usdt": 0.0}

        if total_capital <= 0:
            logger.error("PositionSizer: total_capital must be positive, got %.2f", total_capital)
            return {"size_usdt": 0.0, "quantity": 0.0, "risk_usdt": 0.0}

        # ── Calculate risk in dollars ─────────────────────────
        risk_usdt = cfg.risk_pct * available_capital
        logger.debug(
            "PositionSizer: risk_pct=%.4f%% × available_capital=%.2f → risk_usdt=%.2f",
            cfg.risk_pct * 100,
            available_capital,
            risk_usdt,
        )

        # ── Calculate quantity from risk ──────────────────────
        price_diff = abs(entry_price - stop_loss)
        if price_diff <= 0:
            logger.warning(
                "PositionSizer: entry_price and stop_loss are identical (%.2f), rejecting",
                entry_price,
            )
            return {"size_usdt": 0.0, "quantity": 0.0, "risk_usdt": 0.0}

        quantity = risk_usdt / price_diff
        logger.debug(
            "PositionSizer: risk_usdt=%.2f / price_diff=%.4f → quantity=%.8f",
            risk_usdt,
            price_diff,
            quantity,
        )

        # ── Calculate notional size ───────────────────────────
        size_usdt = quantity * entry_price
        logger.debug(
            "PositionSizer: quantity=%.8f × entry_price=%.2f → size_usdt=%.2f",
            quantity,
            entry_price,
            size_usdt,
        )

        # ── Apply hard cap ───────────────────────────────────
        max_size_usdt = cfg.max_capital_pct * total_capital
        if size_usdt > max_size_usdt:
            logger.info(
                "PositionSizer: size_usdt=%.2f exceeds max_capital_pct cap=%.2f, capping",
                size_usdt,
                max_size_usdt,
            )
            size_usdt = max_size_usdt
            quantity = size_usdt / entry_price

        # ── Apply floor: if below min, reject entire position ─
        if size_usdt < cfg.min_size_usdt:
            logger.info(
                "PositionSizer: size_usdt=%.2f below min_size_usdt=%.2f, rejecting",
                size_usdt,
                cfg.min_size_usdt,
            )
            return {"size_usdt": 0.0, "quantity": 0.0, "risk_usdt": 0.0}

        logger.debug(
            "PositionSizer: APPROVED size_usdt=%.2f, quantity=%.8f, risk_usdt=%.2f",
            size_usdt,
            quantity,
            risk_usdt,
        )

        return {
            "size_usdt": size_usdt,
            "quantity": quantity,
            "risk_usdt": risk_usdt,
        }
