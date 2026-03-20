from __future__ import annotations
from enum import Enum
from typing import TypedDict


class MarketRegime(Enum):
    """Market regime classification for adaptive position sizing."""
    TRENDING_UP = 1.0
    TRENDING_DOWN = 0.8
    RANGING = 0.7
    HIGH_VOLATILITY = 0.5
    CRISIS = 0.25
    RECOVERY = 0.9
    UNKNOWN = 0.6


class KellyResult(TypedDict):
    """Kelly criterion calculation result."""
    kelly_f: float
    fractional_kelly: float
    capped_kelly: float


class KellyATRResult(TypedDict):
    """ATR volatility-adjusted Kelly sizing result."""
    quantity: float
    position_value_usdt: float
    kelly_adjusted: float
    vol_scalar: float
    realized_vol_pct: float


class ATRStopResult(TypedDict):
    """ATR-stop based sizing result."""
    quantity: float
    position_value_usdt: float
    risk_per_trade_usdt: float
    stop_distance: float


class RegimeAdjustedResult(TypedDict):
    """Regime-adjusted sizing result."""
    quantity: float
    position_value_usdt: float
    kelly_adjusted: float
    vol_scalar: float
    regime_multiplier: float
    final_position_pct: float


class PositionSizer:
    """
    Comprehensive position sizing with volatility adjustment, Kelly criterion,
    ATR-based risk management, and regime-aware scaling.

    Methods support multiple sizing approaches:
    - Kelly criterion with volatility adjustment (primary)
    - ATR-stop based fixed risk sizing
    - Fixed percentage sizing
    - Regime-adaptive sizing
    """

    def __init__(self, risk_per_trade_pct: float = 1.0):
        """
        Initialize position sizer.

        Parameters
        ----------
        risk_per_trade_pct : float
            Default max % of account equity to risk per trade (default 1.0%).
        """
        self.risk_per_trade_pct = risk_per_trade_pct

    def compute_kelly(
        self,
        win_rate: float,
        avg_win: float,
        avg_loss: float,
        fractional: float = 0.25,
    ) -> KellyResult:
        """
        Compute Kelly criterion fraction.

        Kelly formula: f* = (p * b - q) / b
        where p = win_rate, q = 1-p, b = avg_win / avg_loss

        Parameters
        ----------
        win_rate : float
            Probability of winning trade (0.0 to 1.0).
        avg_win : float
            Average win amount in USDT.
        avg_loss : float
            Average loss amount in USDT (positive value).
        fractional : float
            Fractional Kelly multiplier to apply (default 0.25 = quarter-Kelly).

        Returns
        -------
        KellyResult
            Dictionary with kelly_f, fractional_kelly, and capped_kelly.
        """
        if avg_loss <= 0 or win_rate <= 0 or win_rate >= 1:
            return {
                'kelly_f': 0.0,
                'fractional_kelly': 0.0,
                'capped_kelly': 0.0,
            }

        q = 1.0 - win_rate
        b = avg_win / avg_loss

        # Full Kelly: f* = (p * b - q) / b
        kelly_f = (win_rate * b - q) / b if b > 0 else 0.0
        kelly_f = max(0.0, kelly_f)  # Ensure non-negative

        # Apply fractional multiplier
        fractional_kelly = kelly_f * fractional

        # Cap at 20% max
        capped_kelly = min(fractional_kelly, 0.20)

        return {
            'kelly_f': kelly_f,
            'fractional_kelly': fractional_kelly,
            'capped_kelly': capped_kelly,
        }

    def size_from_kelly_atr(
        self,
        equity: float,
        kelly_fraction: float,
        entry_price: float,
        atr_14: float,
        target_volatility_pct: float = 0.01,
        max_position_pct: float = 0.02,
    ) -> KellyATRResult:
        """
        Compute position size using ATR-adjusted Kelly criterion.

        Scales Kelly fraction based on realized volatility vs target volatility.
        If realized volatility is lower than target, increase position (up to max).
        If realized volatility is higher, decrease position (down to min).

        Parameters
        ----------
        equity : float
            Account equity in USDT.
        kelly_fraction : float
            Kelly fraction (e.g., 0.05 for 5%, typically from compute_kelly()).
        entry_price : float
            Entry price in USDT.
        atr_14 : float
            14-period ATR value in USDT.
        target_volatility_pct : float
            Target daily volatility as fraction (default 0.01 = 1%).
        max_position_pct : float
            Max position size as fraction of equity (default 0.02 = 2%).

        Returns
        -------
        KellyATRResult
            Dictionary with quantity, position_value_usdt, kelly_adjusted,
            vol_scalar, and realized_vol_pct.
        """
        if equity <= 0 or entry_price <= 0 or atr_14 <= 0:
            return {
                'quantity': 0.0,
                'position_value_usdt': 0.0,
                'kelly_adjusted': 0.0,
                'vol_scalar': 1.0,
                'realized_vol_pct': 0.0,
            }

        # Compute realized volatility as ATR fraction of entry price
        realized_vol = atr_14 / entry_price
        realized_vol_pct = realized_vol * 100.0

        # Compute volatility scalar: ratio of target vol to current vol
        # If current vol < target, scalar > 1 (increase position)
        # If current vol > target, scalar < 1 (decrease position)
        vol_scalar = target_volatility_pct / max(realized_vol, 0.0001)

        # Clamp vol_scalar to [0.25, 2.0] to prevent extreme sizing
        vol_scalar = max(0.25, min(2.0, vol_scalar))

        # Compute Kelly-adjusted fraction
        kelly_adjusted = min(kelly_fraction * vol_scalar, max_position_pct)

        # Compute position value and quantity
        position_value_usdt = equity * kelly_adjusted
        quantity = position_value_usdt / entry_price

        return {
            'quantity': quantity,
            'position_value_usdt': position_value_usdt,
            'kelly_adjusted': kelly_adjusted,
            'vol_scalar': vol_scalar,
            'realized_vol_pct': realized_vol_pct,
        }

    def size_from_atr_stop(
        self,
        equity: float,
        entry_price: float,
        stop_price: float,
        risk_pct: float = 0.01,
        max_position_pct: float = 0.02,
    ) -> ATRStopResult:
        """
        Compute position size using ATR-based stop loss.

        Sizes position such that max loss = equity * risk_pct.
        Final position is capped at max_position_pct * equity.

        Parameters
        ----------
        equity : float
            Account equity in USDT.
        entry_price : float
            Entry price in USDT.
        stop_price : float
            Stop loss price in USDT.
        risk_pct : float
            Max risk per trade as fraction (default 0.01 = 1%).
        max_position_pct : float
            Max position size as fraction of equity (default 0.02 = 2%).

        Returns
        -------
        ATRStopResult
            Dictionary with quantity, position_value_usdt, risk_per_trade_usdt,
            and stop_distance.
        """
        if equity <= 0 or entry_price <= 0:
            return {
                'quantity': 0.0,
                'position_value_usdt': 0.0,
                'risk_per_trade_usdt': 0.0,
                'stop_distance': 0.0,
            }

        stop_distance = abs(entry_price - stop_price)
        if stop_distance <= 0:
            return {
                'quantity': 0.0,
                'position_value_usdt': 0.0,
                'risk_per_trade_usdt': 0.0,
                'stop_distance': 0.0,
            }

        risk_per_trade_usdt = equity * risk_pct

        # quantity = risk_amount / stop_distance (in base units)
        quantity = risk_per_trade_usdt / stop_distance

        # position_value = quantity * entry_price
        position_value_usdt = quantity * entry_price

        # Cap to max_position_pct
        max_position_value = equity * max_position_pct
        if position_value_usdt > max_position_value:
            position_value_usdt = max_position_value
            quantity = position_value_usdt / entry_price

        return {
            'quantity': quantity,
            'position_value_usdt': position_value_usdt,
            'risk_per_trade_usdt': risk_per_trade_usdt,
            'stop_distance': stop_distance,
        }

    def size_from_fixed_pct(
        self,
        equity: float,
        price: float,
        pct: float = 0.01,
    ) -> float:
        """
        Compute position size using fixed percentage of equity.

        Parameters
        ----------
        equity : float
            Account equity in USDT.
        price : float
            Entry price in USDT.
        pct : float
            Position size as fraction of equity (default 0.01 = 1%).

        Returns
        -------
        float
            Position quantity.
        """
        if equity <= 0 or price <= 0:
            return 0.0

        position_value_usdt = equity * pct
        quantity = position_value_usdt / price
        return quantity

    def size_for_regime(
        self,
        equity: float,
        entry_price: float,
        atr_14: float,
        regime: MarketRegime,
        kelly_fraction: float,
        target_vol_pct: float = 0.01,
        max_position_pct: float = 0.02,
    ) -> RegimeAdjustedResult:
        """
        Compute position size adjusted for market regime.

        Uses size_from_kelly_atr as base, then applies regime multiplier.
        Final position is capped at max_position_pct.

        Regime multipliers:
        - TRENDING_UP: 1.0 (full confidence)
        - TRENDING_DOWN: 0.8 (slightly reduce, bias to short)
        - RANGING: 0.7 (reduce, mean reversion)
        - HIGH_VOLATILITY: 0.5 (halve position)
        - CRISIS: 0.25 (quarter size)
        - RECOVERY: 0.9 (near-full, building confidence)
        - UNKNOWN: 0.6 (default caution)

        Parameters
        ----------
        equity : float
            Account equity in USDT.
        entry_price : float
            Entry price in USDT.
        atr_14 : float
            14-period ATR value in USDT.
        regime : MarketRegime
            Current market regime classification.
        kelly_fraction : float
            Kelly fraction to use.
        target_vol_pct : float
            Target daily volatility (default 0.01).
        max_position_pct : float
            Max position as fraction (default 0.02).

        Returns
        -------
        RegimeAdjustedResult
            Dictionary with quantity, position_value_usdt, kelly_adjusted,
            vol_scalar, regime_multiplier, and final_position_pct.
        """
        # Get base Kelly-ATR sizing
        kelly_result = self.size_from_kelly_atr(
            equity=equity,
            kelly_fraction=kelly_fraction,
            entry_price=entry_price,
            atr_14=atr_14,
            target_volatility_pct=target_vol_pct,
            max_position_pct=max_position_pct,
        )

        # Get regime multiplier — accept both MarketRegime enum and plain str
        if isinstance(regime, MarketRegime):
            regime_multiplier = regime.value
        elif isinstance(regime, str):
            try:
                regime_multiplier = MarketRegime[regime.upper()].value
            except KeyError:
                regime_multiplier = MarketRegime.UNKNOWN.value
        else:
            regime_multiplier = MarketRegime.UNKNOWN.value

        # Apply regime multiplier to position
        final_position_value = kelly_result['position_value_usdt'] * regime_multiplier
        final_position_pct = (final_position_value / equity) if equity > 0 else 0.0

        # Cap to max_position_pct
        final_position_pct = min(final_position_pct, max_position_pct)
        final_position_value = equity * final_position_pct

        final_quantity = final_position_value / entry_price if entry_price > 0 else 0.0

        return {
            'quantity': final_quantity,
            'position_value_usdt': final_position_value,
            'kelly_adjusted': kelly_result['kelly_adjusted'],
            'vol_scalar': kelly_result['vol_scalar'],
            'regime_multiplier': regime_multiplier,
            'final_position_pct': final_position_pct,
        }


# Module-level singleton
position_sizer = PositionSizer()
