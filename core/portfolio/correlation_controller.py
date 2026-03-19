# ============================================================
# NEXUS TRADER — Portfolio Correlation Controller  (Sprint 16)
#
# Enforces:
#  1. Correlation-based position cap (max 0.75 correlation between
#     any two open positions)
#  2. Directional exposure limits (max 70% of capital on one side)
#  3. Overnight position reduction (Friday 17:00 UTC)
#
# Used by RiskGate as an additional validation layer.
# ============================================================
from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)

# Configuration defaults
MAX_PAIRWISE_CORRELATION = 0.75  # reject new position if correlated > 75% with any open
MAX_DIRECTIONAL_EXPOSURE = 0.70  # max 70% of portfolio on one side
OVERNIGHT_REDUCE_DAY = 4         # Friday (0=Monday)
OVERNIGHT_REDUCE_HOUR = 17       # 17:00 UTC


# Correlation matrix of common crypto assets (pre-computed approximate values)
# Based on 90-day rolling correlations; updated periodically
_BASE_CORRELATION = {
    ("BTC/USDT", "ETH/USDT"): 0.85,
    ("BTC/USDT", "SOL/USDT"): 0.75,
    ("BTC/USDT", "BNB/USDT"): 0.70,
    ("BTC/USDT", "ADA/USDT"): 0.72,
    ("BTC/USDT", "XRP/USDT"): 0.65,
    ("BTC/USDT", "DOGE/USDT"): 0.60,
    ("ETH/USDT", "SOL/USDT"): 0.80,
    ("ETH/USDT", "BNB/USDT"): 0.75,
    ("ETH/USDT", "ADA/USDT"): 0.78,
    ("SOL/USDT", "BNB/USDT"): 0.72,
    ("SOL/USDT", "ADA/USDT"): 0.74,
    ("BNB/USDT", "ADA/USDT"): 0.70,
}


def get_pair_correlation(sym_a: str, sym_b: str) -> float:
    """Return approximate pairwise correlation between two symbols."""
    if sym_a == sym_b:
        return 1.0
    key1 = (sym_a, sym_b)
    key2 = (sym_b, sym_a)
    return _BASE_CORRELATION.get(key1, _BASE_CORRELATION.get(key2, 0.50))


def update_correlation(sym_a: str, sym_b: str, correlation: float) -> None:
    """Update correlation matrix with live-computed value."""
    key = (min(sym_a, sym_b), max(sym_a, sym_b))
    _BASE_CORRELATION[key] = round(max(-1.0, min(1.0, correlation)), 4)


class CorrelationController:
    """
    Validates new position candidates against portfolio correlation limits
    and directional exposure constraints.
    """

    def __init__(
        self,
        max_correlation: float = MAX_PAIRWISE_CORRELATION,
        max_directional_exposure: float = MAX_DIRECTIONAL_EXPOSURE,
    ):
        self.max_correlation = max_correlation
        self.max_directional_exposure = max_directional_exposure
        self._lock = threading.RLock()
        self._live_correlations: dict[tuple[str, str], float] = {}

    def check_correlation(
        self,
        new_symbol: str,
        open_positions: list[dict],
    ) -> tuple[bool, Optional[str]]:
        """
        Check if new_symbol is too correlated with any existing open position.

        Returns:
            (allowed, rejection_reason)
        """
        if not open_positions:
            return True, None

        for pos in open_positions:
            existing_sym = pos.get("symbol", "")
            if not existing_sym or existing_sym == new_symbol:
                continue

            corr = self._get_correlation(new_symbol, existing_sym)
            if corr > self.max_correlation:
                reason = (
                    f"Correlation cap: {new_symbol} ↔ {existing_sym} "
                    f"corr={corr:.2f} > max={self.max_correlation:.2f}"
                )
                logger.info("CorrelationController: REJECTED — %s", reason)
                return False, reason

        return True, None

    def check_directional_exposure(
        self,
        new_side: str,
        new_size_usdt: float,
        open_positions: list[dict],
        total_capital_usdt: float,
    ) -> tuple[bool, Optional[str]]:
        """
        Check if adding new position would exceed directional exposure limit.

        Returns:
            (allowed, rejection_reason)
        """
        if total_capital_usdt <= 0:
            return True, None

        # Calculate current long/short exposure
        long_exposure = sum(
            p.get("position_size_usdt", 0) or 0
            for p in open_positions
            if p.get("side", "").lower() in ("buy", "long")
        )
        short_exposure = sum(
            p.get("position_size_usdt", 0) or 0
            for p in open_positions
            if p.get("side", "").lower() in ("sell", "short")
        )

        # Add new position's size to proposed side
        new_is_long = new_side.lower() in ("buy", "long")
        if new_is_long:
            proposed_long = long_exposure + new_size_usdt
            exposure_pct = proposed_long / total_capital_usdt
        else:
            proposed_short = short_exposure + new_size_usdt
            exposure_pct = proposed_short / total_capital_usdt

        if exposure_pct > self.max_directional_exposure:
            side_label = "long" if new_is_long else "short"
            reason = (
                f"Directional exposure cap: {side_label} exposure would be "
                f"{exposure_pct:.1%} > max {self.max_directional_exposure:.1%}"
            )
            logger.info("CorrelationController: REJECTED — %s", reason)
            return False, reason

        return True, None

    def should_reduce_overnight(self) -> bool:
        """
        Return True if it's Friday 17:00 UTC (overnight position reduction window).
        Used to flag positions for partial exit before weekend.
        """
        now = datetime.now(timezone.utc)
        return (
            now.weekday() == OVERNIGHT_REDUCE_DAY and
            now.hour >= OVERNIGHT_REDUCE_HOUR
        )

    def get_overnight_reduction_pct(self) -> float:
        """
        Return the fraction of positions to close for overnight risk reduction.
        Returns 0.0 outside the reduction window.
        """
        if not self.should_reduce_overnight():
            return 0.0
        return 0.50  # reduce to 50% of current size on Fridays

    def update_live_correlation(
        self,
        sym_a: str,
        sym_b: str,
        returns_a: list[float],
        returns_b: list[float],
    ) -> float:
        """
        Compute and cache live correlation from recent returns.
        Updates the base correlation matrix.
        """
        try:
            if len(returns_a) < 10 or len(returns_b) < 10:
                return get_pair_correlation(sym_a, sym_b)

            arr_a = np.array(returns_a[-60:])  # last 60 periods
            arr_b = np.array(returns_b[-60:])

            n = min(len(arr_a), len(arr_b))
            arr_a = arr_a[-n:]
            arr_b = arr_b[-n:]

            corr = float(np.corrcoef(arr_a, arr_b)[0, 1])
            if np.isnan(corr):
                corr = 0.50

            update_correlation(sym_a, sym_b, corr)
            with self._lock:
                self._live_correlations[(sym_a, sym_b)] = corr

            return corr
        except Exception as exc:
            logger.debug("CorrelationController: live correlation error — %s", exc)
            return get_pair_correlation(sym_a, sym_b)

    def get_correlation_matrix(self, symbols: list[str]) -> dict[tuple[str, str], float]:
        """Return correlation matrix for a list of symbols."""
        matrix = {}
        for i, sym_a in enumerate(symbols):
            for sym_b in symbols[i:]:
                corr = get_pair_correlation(sym_a, sym_b)
                matrix[(sym_a, sym_b)] = corr
        return matrix

    def _get_correlation(self, sym_a: str, sym_b: str) -> float:
        """Get correlation, preferring live over static."""
        with self._lock:
            live = self._live_correlations.get((sym_a, sym_b))
            if live is None:
                live = self._live_correlations.get((sym_b, sym_a))
        if live is not None:
            return live
        return get_pair_correlation(sym_a, sym_b)


# ── Module-level singleton ────────────────────────────────────
_correlation_controller: Optional[CorrelationController] = None


def get_correlation_controller() -> CorrelationController:
    global _correlation_controller
    if _correlation_controller is None:
        _correlation_controller = CorrelationController()
    return _correlation_controller
