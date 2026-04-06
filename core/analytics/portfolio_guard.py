"""
Portfolio Correlation Guard — Session 23.

Prevents over-concentration in correlated positions by detecting when
the same directional bias has been stacked across correlated crypto pairs.

Problem:
  BTC, ETH, SOL, XRP, and BNB are all highly correlated in risk-off events.
  If IDSS opens 3 BUY positions simultaneously (BTC + ETH + SOL all in
  bull-trend regime), the portfolio has 3× exposure to a single risk factor.
  A sudden market reversal (exchange hack, regulatory news, macro shock)
  will close all three in loss simultaneously.

Solution — PortfolioGuard:
  1. Classify each trading pair into a correlation group.
  2. Count open same-direction positions from the same group.
  3. Return a size multiplier that scales DOWN new position size as the
     same-group stack grows — without hard-blocking (preserving signal diversity).

  Multiplier schedule (same-direction positions N already open in group):
    N = 0: 1.00 (no existing correlation exposure — full size)
    N = 1: 0.80 (one existing position — mild reduction)
    N = 2: 0.55 (two existing — substantial reduction)
    N = 3: 0.30 (three existing — heavy reduction, near-minimum)
    N ≥ 4: 0.10 (hard near-zero — only token position allowed)

  Hard block: if N >= max_same_group_same_dir (default 4), returns 0.0.
  This prevents >4 stacked same-direction correlated positions.

Configuration (config.yaml):
  portfolio_guard.enabled: true
  portfolio_guard.max_same_group_same_dir: 4
  portfolio_guard.multipliers: [1.0, 0.80, 0.55, 0.30, 0.10]

Usage:
  from core.analytics.portfolio_guard import PortfolioGuard
  guard = PortfolioGuard()
  factor = guard.get_correlation_factor(
      symbol="ETH/USDT", direction="buy", open_positions=[...]
  )
  # Apply factor to the PositionSizer's computed USDT size
"""
from __future__ import annotations

import logging
from typing import Optional

from config.settings import settings as _s

logger = logging.getLogger(__name__)

# ── Correlation group definitions ─────────────────────────────────────────────
# Groups represent assets that tend to move together in risk events.
# "major_alts" covers most liquid alts whose beta to BTC ≈ 1.2–1.8.

CORRELATION_GROUPS: dict[str, list[str]] = {
    "btc":        ["BTC/USDT", "BTC/USDT:USDT", "BTCUSDT"],
    "eth":        ["ETH/USDT", "ETH/USDT:USDT", "ETHUSDT"],
    "major_alts": [
        "SOL/USDT", "SOL/USDT:USDT", "SOLUSDT",
        "XRP/USDT", "XRP/USDT:USDT", "XRPUSDT",
        "BNB/USDT", "BNB/USDT:USDT", "BNBUSDT",
        "DOGE/USDT", "DOGUSDT",
        "ADA/USDT", "ADAUSDT",
        "AVAX/USDT", "AVAXUSDT",
        "MATIC/USDT", "MATICUSDT",
        "TRX/USDT", "TRXUSDT",
        "BCH/USDT", "BCHUSDT",
        "HYPE/USDT", "HYPEUSDT",
        "LINK/USDT", "LINKUSDT",
        "XLM/USDT", "XLMUSDT",
        "HBAR/USDT", "HBARUSDT",
        "SUI/USDT", "SUIUSDT",
        "NEAR/USDT", "NEARUSDT",
        "ICP/USDT", "ICPUSDT",
        "ONDO/USDT", "ONDOUSDT",
        "ALGO/USDT", "ALGOUSDT",
        "RENDER/USDT", "RENDERUSDT",
    ],
    # Stablecoins are zero-correlation — always get factor=1.0
    "stablecoins": ["USDC/USDT", "DAI/USDT", "FRAX/USDT"],
}

# All major crypto (including BTC and ETH) correlate in systemic risk-off events.
# We treat BTC and ETH as partially correlated with major_alts for the
# systemic risk count (but not the same group for mild-regime events).
_SYSTEMIC_GROUPS = frozenset({"btc", "eth", "major_alts"})

# Build reverse lookup: normalised symbol → group name
_SYMBOL_TO_GROUP: dict[str, str] = {}
for _grp, _syms in CORRELATION_GROUPS.items():
    for _s_raw in _syms:
        _SYMBOL_TO_GROUP[_s_raw.upper()] = _grp

# Default multiplier schedule: index = number of same-group same-dir positions already open
_DEFAULT_MULTIPLIERS = [1.00, 0.80, 0.55, 0.30, 0.10]


def _normalise_symbol(symbol: str) -> str:
    return symbol.strip().upper()


def _get_group(symbol: str) -> Optional[str]:
    """Return the correlation group for a symbol, or None if unknown."""
    return _SYMBOL_TO_GROUP.get(_normalise_symbol(symbol))


class PortfolioGuard:
    """
    Computes a position-size multiplier based on how many correlated same-direction
    positions are already open. The multiplier is applied by the caller to the USDT
    size computed by PositionSizer.

    Does NOT block trades outright (except at the hard cap); it scales down size
    to preserve signal diversity while limiting correlated risk accumulation.
    """

    def get_correlation_factor(
        self,
        symbol: str,
        direction: str,
        open_positions: list[dict],
    ) -> tuple[float, str]:
        """
        Compute (size_multiplier, reason_string).

        Parameters
        ----------
        symbol : str
            The symbol being considered for a new trade.
        direction : str
            "buy" / "long" or "sell" / "short".
        open_positions : list of position dicts
            Each dict should have "symbol" and "side" keys.

        Returns
        -------
        (factor: float, reason: str)
            factor = 1.0 if guard is disabled or no correlation found.
            reason = human-readable explanation.
        """
        if not _s.get("portfolio_guard.enabled", True):
            return 1.0, "guard_disabled"

        grp = _get_group(symbol)
        if grp is None or grp == "stablecoins":
            return 1.0, f"no_group_{symbol}"

        # Normalise direction for comparison
        is_long = direction.lower() in ("buy", "long")
        dir_str = "buy" if is_long else "sell"

        # Count open positions in the same correlation group with the same direction.
        # For systemic groups (btc, eth, major_alts), also count cross-group
        # systemic exposure to detect BTC+ETH+SOL triple-stack risk-off bets.
        same_grp_count = 0
        systemic_count = 0

        for pos in open_positions:
            pos_sym  = pos.get("symbol", "")
            pos_side = pos.get("side", "").lower()
            pos_is_long = pos_side in ("buy", "long")

            if pos_is_long != is_long:
                continue  # opposite direction — no correlation concern

            pos_grp = _get_group(pos_sym)
            if pos_grp == grp:
                same_grp_count += 1
            if pos_grp in _SYSTEMIC_GROUPS and grp in _SYSTEMIC_GROUPS:
                systemic_count += 1

        # Use the more conservative of (same-group count, systemic count)
        effective_count = max(same_grp_count, systemic_count)

        # Read config
        max_allowed = int(_s.get("portfolio_guard.max_same_group_same_dir", 4))
        multipliers: list = _s.get("portfolio_guard.multipliers", _DEFAULT_MULTIPLIERS)
        if not isinstance(multipliers, list) or not multipliers:
            multipliers = _DEFAULT_MULTIPLIERS

        # Hard block
        if effective_count >= max_allowed:
            reason = (
                f"portfolio_guard: {effective_count} correlated {dir_str} positions "
                f"in group '{grp}' — hard cap reached"
            )
            logger.warning("PortfolioGuard: %s BLOCKED | %s", symbol, reason)
            return 0.0, reason

        # Look up multiplier (clamp index to last element for safety)
        idx = min(effective_count, len(multipliers) - 1)
        factor = float(multipliers[idx])

        if effective_count == 0:
            return factor, ""

        reason = (
            f"portfolio_guard: {effective_count} correlated {dir_str} positions "
            f"in group '{grp}' → size ×{factor:.2f}"
        )
        logger.info("PortfolioGuard: %s size factor=%.2f | %s", symbol, factor, reason)
        return factor, reason


# Module-level singleton
_guard_instance: Optional[PortfolioGuard] = None


def get_portfolio_guard() -> PortfolioGuard:
    """Return the module-level PortfolioGuard singleton."""
    global _guard_instance
    if _guard_instance is None:
        _guard_instance = PortfolioGuard()
    return _guard_instance
