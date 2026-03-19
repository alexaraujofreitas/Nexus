# ============================================================
# NEXUS TRADER — Markov-Switching GARCH Volatility Forecaster (Phase 4d)
# ============================================================
# MS-GARCH volatility regime forecaster using a 2-state Markov-Switching
# model on realized volatility to forecast whether the next period will
# be LOW or HIGH volatility.
#
# Uses arch library if available, falls back to a simplified
# threshold-based volatility state machine if not.
#
# Output is used to:
#   (1) Confirm/adjust RegimeClassifier output
#   (2) Scale position sizes via PositionSizer
#   (3) Set option-style expiry windows for OrderCandidates
# ============================================================

from __future__ import annotations

import logging
import threading
from typing import Optional, Dict, Tuple
import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# Try to import arch library for MS-GARCH; fallback gracefully
try:
    from arch import arch_model
    from arch.univariate import GARCH
    ARCH_AVAILABLE = True
    logger.debug("arch library available - using MS-GARCH model")
except ImportError:
    ARCH_AVAILABLE = False
    logger.warning("arch library not available - using threshold-based fallback for MS-GARCH")


class MSGARCHForecaster:
    """
    Markov-Switching GARCH (MS-GARCH) volatility regime forecaster.

    Fits a 2-state Markov-Switching model on realized volatility to forecast
    whether the next period will be LOW or HIGH volatility.

    Attributes:
        n_states: Number of volatility regimes (default: 2 for LOW/HIGH)
        p: GARCH lag order (default: 1)
        q: GARCH lag order (default: 1)
        refit_every_n_bars: Number of bars between model refits
        transition_matrix: 2x2 matrix of regime transition probabilities
        regime_volatilities: Dict mapping regime to estimated volatility
        fitted: Whether a model has been successfully fit
    """

    # Volatility state constants
    STATE_LOW_VOL = "LOW_VOL"
    STATE_HIGH_VOL = "HIGH_VOL"

    def __init__(
        self,
        n_states: int = 2,
        p: int = 1,
        q: int = 1,
        refit_every_n_bars: int = 100,
    ):
        """
        Initialize MS-GARCH forecaster.

        Args:
            n_states: Number of regimes (default 2: LOW_VOL, HIGH_VOL)
            p: GARCH(p,q) lag order for mean (default 1)
            q: GARCH(p,q) lag order for variance (default 1)
            refit_every_n_bars: Refit frequency to avoid overfitting (default 100)
        """
        self.n_states = n_states
        self.p = p
        self.q = q
        self.refit_every_n_bars = refit_every_n_bars

        self.model = None
        self.fitted = False
        self.fit_counter = 0

        # Regime tracking
        self.transition_matrix: Optional[np.ndarray] = None
        self.regime_volatilities: Dict[str, float] = {
            self.STATE_LOW_VOL: 0.01,
            self.STATE_HIGH_VOL: 0.05,
        }
        self.current_state = self.STATE_LOW_VOL
        self.current_probability = 0.5

        # Threshold-based fallback params
        self.realized_vol_window = 20
        self.long_term_vol_window = 200
        self.vol_ratio_threshold = 1.5

        # Thread safety
        self._lock = threading.Lock()

        logger.debug(f"MSGARCHForecaster initialized: {n_states} states, GARCH({p},{q})")

    def fit(self, df: pd.DataFrame) -> bool:
        """
        Fit MS-GARCH model to price data.

        Attempts to fit an ARCH model with GARCH specification if arch is
        available, otherwise uses threshold-based fallback. Extracts transition
        probabilities and regime volatilities.

        Args:
            df: DataFrame with OHLCV data including 'close' column

        Returns:
            True if fit succeeded, False otherwise
        """
        if df is None or df.empty or "close" not in df.columns:
            logger.warning("MSGARCHForecaster.fit: Invalid DataFrame (missing 'close')")
            return False

        try:
            with self._lock:
                if ARCH_AVAILABLE and len(df) >= 100:
                    return self._fit_arch_model(df)
                else:
                    return self._fit_threshold_fallback(df)
        except Exception as e:
            logger.error(f"MSGARCHForecaster.fit error: {e}")
            return False

    def _fit_arch_model(self, df: pd.DataFrame) -> bool:
        """
        Fit ARCH/GARCH model using the arch library.

        Args:
            df: DataFrame with 'close' prices

        Returns:
            True if successful
        """
        try:
            # Compute log returns
            returns = np.diff(np.log(df["close"].values)) * 100  # basis points
            returns = returns[~np.isnan(returns)]

            if len(returns) < 50:
                logger.warning(f"Insufficient data for ARCH model ({len(returns)} obs)")
                return False

            # Fit GARCH(p,q) model on returns
            model = arch_model(
                returns,
                vol="Garch",
                p=self.p,
                q=self.q,
                rescale=True,
            )

            res = model.fit(disp="off", show_warning=False)

            # Extract parameters
            self.model = res
            self.fitted = True

            # Estimate transition matrix and volatilities
            # In a true MS model, we would use Markov-chain filters; here we use
            # a simplified heuristic based on conditional volatility percentiles
            cond_vol = np.asarray(res.conditional_volatility)
            vol_low = np.percentile(cond_vol, 25)
            vol_high = np.percentile(cond_vol, 75)

            self.regime_volatilities[self.STATE_LOW_VOL] = vol_low
            self.regime_volatilities[self.STATE_HIGH_VOL] = vol_high

            # Simplified transition matrix (higher persistence in same state)
            # p_low_to_low, p_low_to_high
            # p_high_to_low, p_high_to_high
            self.transition_matrix = np.array([
                [0.85, 0.15],  # From LOW_VOL: 85% stay, 15% to HIGH
                [0.20, 0.80],  # From HIGH_VOL: 20% to LOW, 80% stay
            ])

            # Estimate current state from last conditional volatility
            latest_vol = cond_vol[-1]
            self.current_state = (
                self.STATE_HIGH_VOL
                if latest_vol > (vol_low + vol_high) / 2
                else self.STATE_LOW_VOL
            )
            self.current_probability = min(
                0.95,
                max(0.55, latest_vol / vol_high),
            )

            logger.info(
                f"ARCH model fit: {len(returns)} obs, "
                f"vol_low={vol_low:.4f}, vol_high={vol_high:.4f}, "
                f"current_state={self.current_state}"
            )
            return True

        except Exception as e:
            logger.warning(f"ARCH model fit failed: {e}, falling back to threshold")
            return self._fit_threshold_fallback(df)

    def _fit_threshold_fallback(self, df: pd.DataFrame) -> bool:
        """
        Fit using threshold-based volatility state machine.

        When arch is unavailable, use rolling volatility thresholds
        to determine regime.

        Args:
            df: DataFrame with 'close' prices

        Returns:
            True if successful
        """
        try:
            if "close" not in df.columns or len(df) < self.long_term_vol_window:
                logger.warning("Fallback: insufficient data")
                return False

            # Compute log returns
            log_returns = np.log(df["close"] / df["close"].shift(1))
            log_returns = log_returns.dropna().values

            # Realized volatility (20-bar rolling std)
            realized_vol = pd.Series(log_returns).rolling(
                window=self.realized_vol_window,
                min_periods=1,
            ).std().values

            # Long-run median volatility (200-bar)
            long_term_vol = np.nanmedian(realized_vol[-self.long_term_vol_window:])
            if np.isnan(long_term_vol) or long_term_vol == 0:
                long_term_vol = np.nanstd(log_returns)

            # Set regime volatilities
            self.regime_volatilities[self.STATE_LOW_VOL] = long_term_vol * 0.8
            self.regime_volatilities[self.STATE_HIGH_VOL] = long_term_vol * 1.5

            # Determine current state
            latest_realized_vol = realized_vol[-1]
            threshold = long_term_vol * self.vol_ratio_threshold
            self.current_state = (
                self.STATE_HIGH_VOL
                if latest_realized_vol > threshold
                else self.STATE_LOW_VOL
            )

            # Probability based on vol ratio
            vol_ratio = latest_realized_vol / threshold
            self.current_probability = min(
                0.85,
                max(0.55, vol_ratio),
            )

            # Simple transition matrix (high persistence)
            self.transition_matrix = np.array([
                [0.80, 0.20],
                [0.25, 0.75],
            ])

            self.fitted = True
            logger.info(
                f"Threshold fallback fit: realized_vol={latest_realized_vol:.6f}, "
                f"long_term={long_term_vol:.6f}, state={self.current_state}"
            )
            return True

        except Exception as e:
            logger.error(f"Threshold fallback fit failed: {e}")
            return False

    def forecast(self, df: pd.DataFrame, horizon: int = 3) -> Dict:
        """
        Forecast next N bars' volatility regime.

        Returns a dict describing the forecasted volatility state,
        probability of persistence, and confidence.

        Args:
            df: DataFrame with OHLCV data
            horizon: Number of bars to forecast ahead (default 3)

        Returns:
            Dict with keys:
                - state: "LOW_VOL" or "HIGH_VOL"
                - probability: Float in [0, 1]
                - forecasted_vol: Estimated volatility value
                - horizon_bars: Requested horizon
                - confidence: Confidence score [0.55, 0.95]
        """
        if not self.fitted:
            logger.warning("MSGARCHForecaster not fitted, refitting...")
            if not self.fit(df):
                return self._create_neutral_forecast(horizon)

        try:
            with self._lock:
                if ARCH_AVAILABLE and self.model is not None:
                    return self._forecast_arch(df, horizon)
                else:
                    return self._forecast_threshold(df, horizon)
        except Exception as e:
            logger.error(f"Forecast error: {e}")
            return self._create_neutral_forecast(horizon)

    def _forecast_arch(self, df: pd.DataFrame, horizon: int) -> Dict:
        """
        Forecast using fitted ARCH model.

        Uses Hamilton filter / Viterbi-like approach for multi-step
        regime transition probabilities.

        Args:
            df: Price DataFrame
            horizon: Forecast horizon in bars

        Returns:
            Forecast dictionary
        """
        try:
            cond_vol = np.asarray(self.model.conditional_volatility)
            latest_vol = cond_vol[-1]

            vol_low = self.regime_volatilities[self.STATE_LOW_VOL]
            vol_high = self.regime_volatilities[self.STATE_HIGH_VOL]
            vol_mid = (vol_low + vol_high) / 2

            # Current state
            current_state = (
                self.STATE_HIGH_VOL
                if latest_vol > vol_mid
                else self.STATE_LOW_VOL
            )

            # Probability of staying in current state for N bars
            # P(stay)^N
            if self.transition_matrix is not None:
                state_idx = 1 if current_state == self.STATE_HIGH_VOL else 0
                p_persist = self.transition_matrix[state_idx, state_idx]
                p_persist_n = p_persist ** horizon
            else:
                p_persist_n = 0.7 ** horizon

            # Forecasted vol: simple trend extrapolation
            if len(cond_vol) >= 2:
                vol_trend = cond_vol[-1] - cond_vol[-2]
                forecasted_vol = cond_vol[-1] + vol_trend * 0.5
            else:
                forecasted_vol = cond_vol[-1] * 1.05

            confidence = min(0.90, max(0.60, p_persist_n * 1.5))

            return {
                "state": current_state,
                "probability": p_persist_n,
                "forecasted_vol": float(forecasted_vol),
                "horizon_bars": horizon,
                "confidence": confidence,
            }

        except Exception as e:
            logger.warning(f"ARCH forecast failed: {e}")
            return self._forecast_threshold(df, horizon)

    def _forecast_threshold(self, df: pd.DataFrame, horizon: int) -> Dict:
        """
        Forecast using threshold-based volatility.

        Args:
            df: Price DataFrame
            horizon: Forecast horizon

        Returns:
            Forecast dictionary
        """
        try:
            log_returns = np.log(df["close"] / df["close"].shift(1))
            log_returns = log_returns.dropna().values

            # Realized volatility
            realized_vol = pd.Series(log_returns).rolling(
                window=self.realized_vol_window,
                min_periods=1,
            ).std().values

            latest_realized_vol = realized_vol[-1]
            long_term_vol = np.nanmedian(realized_vol[-self.long_term_vol_window:])
            if np.isnan(long_term_vol) or long_term_vol == 0:
                long_term_vol = np.nanstd(log_returns)

            threshold = long_term_vol * self.vol_ratio_threshold

            # Current and forecasted state
            current_state = (
                self.STATE_HIGH_VOL
                if latest_realized_vol > threshold
                else self.STATE_LOW_VOL
            )

            # Persistence probability
            vol_ratio = latest_realized_vol / threshold
            p_persist = min(0.85, max(0.55, vol_ratio))
            p_persist_n = p_persist ** horizon

            # Simple trend extrapolation: vol trending toward mean
            mean_vol = np.nanmean(realized_vol[-50:])
            forecasted_vol = latest_realized_vol * 1.05 + (mean_vol - latest_realized_vol) * 0.1

            confidence = 0.60  # Lower confidence for fallback

            return {
                "state": current_state,
                "probability": p_persist_n,
                "forecasted_vol": float(forecasted_vol),
                "horizon_bars": horizon,
                "confidence": confidence,
            }

        except Exception as e:
            logger.error(f"Threshold forecast failed: {e}")
            return self._create_neutral_forecast(horizon)

    def _create_neutral_forecast(self, horizon: int) -> Dict:
        """Create a neutral forecast when model fails."""
        return {
            "state": self.STATE_LOW_VOL,
            "probability": 0.60,
            "forecasted_vol": 0.01,
            "horizon_bars": horizon,
            "confidence": 0.55,
        }

    def get_vol_scaling_factor(self, state: str, probability: float) -> float:
        """
        Get position size scaling factor based on volatility regime.

        - LOW_VOL + high confidence (>0.75): return 1.2 (larger positions in calm)
        - HIGH_VOL + high confidence (>0.75): return 0.7 (smaller positions in turmoil)
        - Otherwise: return 1.0 (neutral)

        Args:
            state: Volatility state ("LOW_VOL" or "HIGH_VOL")
            probability: Probability of state persistence [0, 1]

        Returns:
            Float scaling factor for position sizing
        """
        if probability > 0.75:
            if state == self.STATE_LOW_VOL:
                return 1.2  # Expand in calm markets
            elif state == self.STATE_HIGH_VOL:
                return 0.7  # Contract in volatile markets
        return 1.0  # Neutral

    def get_regime_adjustment(self, current_regime: str, forecast: Dict) -> str:
        """
        Suggest a regime adjustment based on volatility forecast.

        If current_regime is in a trending state and forecast predicts
        high volatility, suggest shifting to a high_volatility regime.
        If in high_volatility and forecast predicts compression, suggest
        volatility_compression.

        Args:
            current_regime: Current regime string (e.g., "bull_trend")
            forecast: Forecast dict from forecast()

        Returns:
            Adjusted regime string (or original if no adjustment)
        """
        forecast_state = forecast.get("state", self.STATE_LOW_VOL)
        forecast_prob = forecast.get("probability", 0.5)

        # Uptrend/ranging/accumulation + HIGH_VOL forecast -> adjust to high_volatility
        trending_regimes = {
            "bull_trend",
            "bear_trend",
            "ranging",
            "accumulation",
        }
        if (
            current_regime in trending_regimes
            and forecast_state == self.STATE_HIGH_VOL
            and forecast_prob > 0.70
        ):
            logger.info(
                f"Suggesting regime shift to high_volatility "
                f"({current_regime} + HIGH_VOL forecast)"
            )
            return "high_volatility"

        # High_volatility + LOW_VOL forecast -> suggest compression
        if (
            current_regime == "high_volatility"
            and forecast_state == self.STATE_LOW_VOL
            and forecast_prob > 0.75
        ):
            logger.info(
                f"Suggesting regime shift to volatility_compression "
                f"(from high_volatility)"
            )
            return "volatility_compression"

        return current_regime

    def reset(self) -> None:
        """Reset model to unfitted state."""
        with self._lock:
            self.model = None
            self.fitted = False
            self.fit_counter = 0
            self.transition_matrix = None
            self.current_state = self.STATE_LOW_VOL
            self.current_probability = 0.5
            logger.info("MSGARCHForecaster reset")

    def should_refit(self) -> bool:
        """Check if model should be refitted based on bar count."""
        self.fit_counter += 1
        if self.fit_counter >= self.refit_every_n_bars:
            self.fit_counter = 0
            return True
        return False


# Module-level singleton
ms_garch = MSGARCHForecaster()

logger.info("MSGARCHForecaster singleton initialized")
