# ============================================================
# NEXUS TRADER — Sub-Model Base Class
# ============================================================
from __future__ import annotations
from abc import ABC, abstractmethod
from typing import Optional
import pandas as pd
from core.meta_decision.order_candidate import ModelSignal


class BaseSubModel(ABC):
    """
    Abstract base for all IDSS sub-models.

    Each sub-model:
    - Declares which regimes it is active in
    - Receives a feature DataFrame (output of indicator_library.calculate_all)
    - Returns a ModelSignal or None
    """

    # Sub-models declare which regimes they are active in.
    # The scanner will only invoke a sub-model if the current regime matches.
    ACTIVE_REGIMES: list[str] = []

    # Regime affinity: continuous activation weight by regime
    # Maps regime name to weight multiplier (0.0-1.0)
    REGIME_AFFINITY: dict[str, float] = {
        "bull_trend": 0.5, "bear_trend": 0.5, "ranging": 0.5,
        "volatility_expansion": 0.5, "volatility_compression": 0.5,
        "uncertain": 0.4, "crisis": 0.0, "liquidation_cascade": 0.0,
        "squeeze": 0.3, "recovery": 0.5, "accumulation": 0.5, "distribution": 0.5,
    }

    # Regime-adjusted ATR multipliers for stop and target calculation
    REGIME_ATR_MULTIPLIERS: dict[str, float] = {
        "bull_trend": 1.875, "bear_trend": 1.875, "ranging": 3.125,
        "volatility_expansion": 3.75, "volatility_compression": 2.25,
        "uncertain": 2.5, "crisis": 3.125, "liquidation_cascade": 3.75,
        "squeeze": 2.5, "recovery": 2.25, "accumulation": 2.5, "distribution": 2.5,
    }

    # Entry price offset as a multiplier of ATR14.
    # Positive: entry is set beyond the current close in the signal direction
    #           (trend/momentum — you pay up slightly to confirm the move).
    # Negative: entry is set before the current close in the signal direction
    #           (mean-reversion — you wait for a slightly better fill).
    # Zero:     entry = close exactly (default).
    # The formula is: entry_long  = close + ENTRY_BUFFER_ATR * atr
    #                  entry_short = close - ENTRY_BUFFER_ATR * atr
    ENTRY_BUFFER_ATR: float = 0.0

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique name for this model (used in logs, rationale, scoring)."""
        ...

    @abstractmethod
    def evaluate(
        self,
        symbol: str,
        df: pd.DataFrame,
        regime: str,
        timeframe: str,
    ) -> Optional[ModelSignal]:
        """
        Evaluate the sub-model against the latest data.

        Parameters
        ----------
        symbol : str
            Trading pair, e.g. "BTC/USDT"
        df : pd.DataFrame
            OHLCV + indicator DataFrame (output of calculate_all).
            The LAST row is the most recent CLOSED candle.
        regime : str
            Current regime as classified by RegimeClassifier.
        timeframe : str
            Primary timeframe of df.

        Returns
        -------
        ModelSignal if a trade opportunity is found, else None.
        """
        ...

    def is_active_in_regime(self, regime: str) -> bool:
        """Return True if this model should run in the given regime."""
        return not self.ACTIVE_REGIMES or regime in self.ACTIVE_REGIMES

    def get_activation_weight(self, regime_probs: dict) -> float:
        """
        Compute continuous activation weight from regime probability distribution.

        Parameters
        ----------
        regime_probs : dict
            Mapping of regime name to probability (0.0-1.0)

        Returns
        -------
        float
            Activation weight (0.0-1.0), rounded to 4 decimals
        """
        if not regime_probs:
            # Fallback: binary is_active check
            return 1.0 if not self.ACTIVE_REGIMES else 0.0

        weight = sum(
            self.REGIME_AFFINITY.get(regime, 0.3) * prob
            for regime, prob in regime_probs.items()
        )
        return round(max(0.0, min(1.0, weight)), 4)

    def _entry_price(self, close: float, atr: float, direction: str) -> float:
        """
        Compute a model-appropriate entry price using ENTRY_BUFFER_ATR.

        For trend/momentum models (ENTRY_BUFFER_ATR > 0):
          - long:  entry = close + buffer*ATR  (pay up to confirm the break)
          - short: entry = close - buffer*ATR  (sell down to confirm)

        For mean-reversion models (ENTRY_BUFFER_ATR < 0):
          - long:  entry = close - |buffer|*ATR  (wait for slightly cheaper fill)
          - short: entry = close + |buffer|*ATR  (wait for slightly higher fill)

        Parameters
        ----------
        close : float
            Last closed candle close price
        atr : float
            ATR14 value
        direction : str
            "long" or "short"

        Returns
        -------
        float
            Adjusted entry price
        """
        offset = self.ENTRY_BUFFER_ATR * atr
        if direction == "long":
            return close + offset
        else:
            return close - offset

    @staticmethod
    def get_atr_multiplier(regime: str) -> float:
        """
        Get the ATR multiplier for stop/target calculation in a given regime.

        Parameters
        ----------
        regime : str
            Market regime

        Returns
        -------
        float
            ATR multiplier (default 2.0 if regime not found)
        """
        return BaseSubModel.REGIME_ATR_MULTIPLIERS.get(regime, 2.0)

    @staticmethod
    def _atr(df: pd.DataFrame, period: int = 14) -> float:
        """Safe ATR extraction from DataFrame."""
        col = f"atr_{period}"
        if col in df.columns:
            v = df[col].iloc[-1]
            if pd.notna(v) and v > 0:
                return float(v)
        # Fallback: manual ATR approximation
        if len(df) >= 2:
            recent = df.tail(period)
            hl = (recent["high"] - recent["low"]).mean()
            return float(hl) if hl > 0 else float(df["close"].iloc[-1]) * 0.01
        return float(df["close"].iloc[-1]) * 0.01

    @staticmethod
    def _col(df: pd.DataFrame, name: str, default: Optional[float] = None) -> Optional[float]:
        """Safe column extraction (last value)."""
        if name in df.columns:
            v = df[name].iloc[-1]
            return float(v) if pd.notna(v) else default
        return default
