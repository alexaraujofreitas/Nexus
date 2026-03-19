"""
Phase 3e: RL Signal Model bridge to IDSS pipeline.

Converts RLEnsemble action outputs into ModelSignal objects for consumption
by ConfluenceScorer. Acts as an adapter between the standalone RL ensemble
and the main trading signal pipeline.

This is NOT a BaseSubModel — it's invoked directly from SignalGenerator and
injects signals via the ModelSignal dataclass, which flows into the standard
IDSS confluence scoring and order generation pipeline.

Key features:
- Lazy initialization of RLEnsemble singleton (first call to evaluate)
- State vector extraction matching BTCTradingEnv (50-dim observation)
- Threshold-based long/short/flat signal generation
- ATR-based position sizing with conservative risk limits
"""

from __future__ import annotations

import logging
import numpy as np
import pandas as pd
from typing import Optional
from pathlib import Path

# Signal dataclass
from core.meta_decision.order_candidate import ModelSignal

# RL ensemble
try:
    from core.rl.rl_ensemble import RLEnsemble
    RL_AVAILABLE = True
except ImportError:
    RL_AVAILABLE = False

logger = logging.getLogger(__name__)


class RLSignalModel:
    """
    Bridge between RLEnsemble and IDSS ModelSignal pipeline.

    Converts RL ensemble action outputs into ModelSignal objects suitable
    for the ConfluenceScorer. This is NOT a BaseSubModel subclass — instead,
    it's called directly from SignalGenerator.evaluate() and generates signals
    that flow into the standard confluence voting system.

    Attributes:
        _ensemble: Optional singleton RLEnsemble instance (lazy-initialized)
        _state_dim: State vector dimension (50, must match BTCTradingEnv)
    """

    # Signal thresholds
    LONG_THRESHOLD = 0.3
    SHORT_THRESHOLD = -0.3
    STRENGTH_SCALE = 0.95  # RL signals capped at 0.95 max strength
    STRENGTH_CAP = 0.90

    # Risk parameters
    STOP_ATR_MULT = 1.5  # Conservative stop placement
    TARGET_ATR_MULT = 2.5  # Target placement

    def __init__(self, state_dim: int = 50) -> None:
        """
        Initialize RL signal model with lazy ensemble initialization.

        Does NOT immediately create the RLEnsemble — that's created on first
        call to evaluate() to avoid startup latency if RL is unavailable.

        Args:
            state_dim: Dimension of state vector (default: 50, matches BTCTradingEnv)
        """
        self._state_dim = state_dim
        self._ensemble: Optional[RLEnsemble] = None

        if not RL_AVAILABLE:
            logger.warning("RL ensemble not available — PyTorch installation required")

    def _ensure_ensemble(self) -> bool:
        """
        Lazy-initialize RLEnsemble singleton on first call.

        Returns:
            True if ensemble is available, False otherwise
        """
        if self._ensemble is None and RL_AVAILABLE:
            try:
                self._ensemble = RLEnsemble(state_dim=self._state_dim)
                logger.info("RLEnsemble initialized (lazy)")
                return True
            except Exception as exc:
                logger.error("Failed to initialize RLEnsemble: %s", exc)
                return False

        return self._ensemble is not None

    def evaluate(
        self,
        symbol: str,
        df: pd.DataFrame,
        regime: str,
        timeframe: str,
    ) -> Optional[ModelSignal]:
        """
        Generate ModelSignal from RL ensemble action output.

        Extracts state vector from the latest bar, queries the ensemble for
        an action, and converts the action to a trading signal (long/short/none).

        Signal strength is scaled by the absolute action magnitude and capped
        conservatively for risk management.

        Args:
            symbol: Trading pair (e.g. "BTC/USDT")
            df: DataFrame with OHLCV and technical indicator columns
            regime: Current market regime string
            timeframe: Primary timeframe string

        Returns:
            ModelSignal if action triggers a signal (> threshold), else None
        """
        if not self._ensure_ensemble():
            return None

        if len(df) < 2:
            return None

        # Build state vector from current bar
        try:
            state = self.build_state_vector(df)
        except Exception as exc:
            logger.debug("Failed to build state vector for %s: %s", symbol, exc)
            return None

        # Get ensemble action
        try:
            ensemble_result = self._ensemble.select_action(state, regime=regime)
        except Exception as exc:
            logger.debug("Ensemble action selection failed for %s: %s", symbol, exc)
            return None

        action = ensemble_result.get("action", 0.0)
        active_agents = ensemble_result.get("active_agents", [])
        weights = ensemble_result.get("weights", {})

        # Determine direction
        if action > self.LONG_THRESHOLD:
            direction = "long"
        elif action < self.SHORT_THRESHOLD:
            direction = "short"
        else:
            # Flat signal — no trade
            return None

        # Current price and ATR
        close = float(df["close"].iloc[-1])
        atr = self._get_atr(df, period=14)

        if atr is None or atr <= 0:
            return None

        # Signal strength: scaled by action magnitude, capped
        strength = min(abs(action) * self.STRENGTH_SCALE, self.STRENGTH_CAP)

        # Stop and target: ATR-based with conservative multipliers
        if direction == "long":
            stop_loss = close - atr * self.STOP_ATR_MULT
            take_profit = close + atr * self.TARGET_ATR_MULT
        else:  # short
            stop_loss = close + atr * self.STOP_ATR_MULT
            take_profit = close - atr * self.TARGET_ATR_MULT

        # Rationale
        rationale = (
            f"RL Ensemble: action={action:.3f} | "
            f"agents={','.join(active_agents)} | "
            f"regime={regime} | "
            f"weights={{{','.join(f'{k}:{v:.2f}' for k,v in weights.items())}}}"
        )

        return ModelSignal(
            symbol=symbol,
            model_name="rl_ensemble",
            direction=direction,
            strength=round(strength, 4),
            entry_price=round(close, 8),
            stop_loss=round(stop_loss, 8),
            take_profit=round(take_profit, 8),
            timeframe=timeframe,
            regime=regime,
            rationale=rationale,
            atr_value=round(atr, 8),
        )

    def build_state_vector(self, df: pd.DataFrame) -> np.ndarray:
        """
        Extract 50-dimensional state vector from DataFrame.

        Builds the state vector matching BTCTradingEnv's observation space:
        - Normalized OHLCV (5 features)
        - Technical indicators: RSI, MACD, MACD Signal, Bollinger Bands, ATR, ADX, etc.
        - Position state: current position size, bars in position, entry price
        - Regime encoding: one-hot or numeric regime representation (12 regimes)

        Features are extracted from the last row of df. Missing indicators are
        filled with 0.0.

        Args:
            df: DataFrame with close, open, high, low, volume and indicator columns

        Returns:
            50-dimensional numpy array (float32)
        """
        if len(df) < 1:
            return np.zeros(50, dtype=np.float32)

        row = df.iloc[-1]

        # Start with 50-element array
        state = np.zeros(50, dtype=np.float32)
        idx = 0

        # ────── Price features (normalized) ──────────────────────────────
        close = float(row.get("close", 0.0))
        open_ = float(row.get("open", close))
        high = float(row.get("high", close))
        low = float(row.get("low", close))
        volume = float(row.get("volume", 0.0))

        # Normalize by close (prevent zero-division)
        if close > 0:
            state[idx] = (close - open_) / close  # close-open ratio
            state[idx + 1] = (high - close) / close  # high-close ratio
            state[idx + 2] = (close - low) / close  # close-low ratio
            state[idx + 3] = np.log(close) if close > 0 else 0.0  # log close
            state[idx + 4] = volume / 1e8 if volume > 0 else 0.0  # normalized volume
        idx += 5

        # ────── RSI and momentum ────────────────────────────────────────
        rsi = float(row.get("rsi_14", 50.0)) / 100.0  # normalize to [0, 1]
        state[idx] = rsi
        idx += 1

        macd = float(row.get("macd", 0.0))
        macd_signal = float(row.get("macd_signal", 0.0))
        macd_hist = macd - macd_signal
        state[idx] = np.clip(macd / 100.0, -1.0, 1.0) if abs(macd) > 0 else 0.0
        state[idx + 1] = np.clip(macd_hist / 100.0, -1.0, 1.0) if abs(macd_hist) > 0 else 0.0
        idx += 2

        # ────── Bollinger Bands ─────────────────────────────────────────
        bb_upper = float(row.get("bb_upper", close))
        bb_lower = float(row.get("bb_lower", close))
        bb_mid = (bb_upper + bb_lower) / 2.0 if (bb_upper + bb_lower) > 0 else close

        bb_width = bb_upper - bb_lower
        if bb_width > 0 and close > 0:
            state[idx] = (close - bb_mid) / bb_width  # position within bands
            state[idx + 1] = bb_width / close  # bandwidth ratio
        idx += 2

        # ────── ATR and volatility ──────────────────────────────────────
        atr = float(row.get("atr_14", 0.0))
        if atr > 0 and close > 0:
            state[idx] = atr / close  # normalized ATR
        idx += 1

        adx = float(row.get("adx", 20.0)) / 100.0  # normalize to [0, 1]
        state[idx] = adx
        idx += 1

        # ────── EMA crossovers ──────────────────────────────────────────
        ema9 = float(row.get("ema_9", close))
        ema21 = float(row.get("ema_21", close))
        ema100 = float(row.get("ema_100", close))

        if close > 0:
            state[idx] = (ema9 - ema21) / close if close > 0 else 0.0
            state[idx + 1] = (ema21 - ema100) / close if close > 0 else 0.0
        idx += 2

        # ────── Stochastic oscillator ───────────────────────────────────
        stoch_k = float(row.get("stoch_k", 50.0)) / 100.0
        stoch_d = float(row.get("stoch_d", 50.0)) / 100.0
        state[idx] = stoch_k
        state[idx + 1] = stoch_d
        idx += 2

        # ────── Additional indicators ───────────────────────────────────
        obv = float(row.get("obv", 0.0))
        state[idx] = np.clip(obv / 1e9, -1.0, 1.0)  # normalized OBV
        idx += 1

        williams_r = float(row.get("williams_r", -50.0)) / 100.0  # [-1, 0]
        state[idx] = williams_r
        idx += 1

        cci = float(row.get("cci", 0.0))
        state[idx] = np.clip(cci / 200.0, -1.0, 1.0)  # normalized CCI
        idx += 1

        # ────── Position state (normalized) ─────────────────────────────
        position_size = float(row.get("position", 0.0))
        state[idx] = position_size  # already in [-1, 1]
        idx += 1

        bars_in_position = float(row.get("bars_in_position", 0.0))
        state[idx] = np.clip(bars_in_position / 100.0, -1.0, 1.0)
        idx += 1

        entry_price_norm = float(row.get("entry_price", close))
        if close > 0:
            state[idx] = (entry_price_norm - close) / close if entry_price_norm > 0 else 0.0
        idx += 1

        # ────── Regime encoding (one-hot style, 12 regimes) ───────────────
        # Remaining indices: (50 - idx) should be ~12
        regime_names = [
            "trend_bull", "trend_bear", "distribution", "high_volatility",
            "crisis", "volatility_compression", "ranging", "accumulation",
            "squeeze", "recovery", "consolidation", "quiet_accumulation"
        ]
        current_regime = row.get("regime", "trend_bull")

        for i, regime_name in enumerate(regime_names):
            if idx < 50:
                state[idx] = 1.0 if current_regime == regime_name else 0.0
                idx += 1

        # Pad any remaining slots with zeros (should be rare)
        while idx < 50:
            state[idx] = 0.0
            idx += 1

        return state.astype(np.float32)

    @staticmethod
    def _get_atr(df: pd.DataFrame, period: int = 14) -> Optional[float]:
        """
        Extract ATR from DataFrame or compute if missing.

        Args:
            df: DataFrame with OHLC columns
            period: ATR period (default: 14)

        Returns:
            ATR value or None if cannot be computed
        """
        if "atr_14" in df.columns:
            atr = float(df["atr_14"].iloc[-1])
            if atr > 0:
                return atr

        # Fallback: rough computation from high-low
        if len(df) > 1:
            high = float(df["high"].iloc[-1])
            low = float(df["low"].iloc[-1])
            close_prev = float(df["close"].iloc[-2])

            tr = max(
                high - low,
                abs(high - close_prev),
                abs(low - close_prev)
            )

            return max(tr, 0.0001)

        return None

    @staticmethod
    def is_available() -> bool:
        """
        Check if RL ensemble and PyTorch are available.

        Returns:
            True if RL can be used, False otherwise
        """
        return RL_AVAILABLE
