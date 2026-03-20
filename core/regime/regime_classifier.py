# ============================================================
# NEXUS TRADER — Regime Classifier  (Sprint 15 — 8-state extension)
#
# Rule-based market regime detection using:
#   ADX  (trend strength)
#   EMA slope  (trend direction)
#   Bollinger Band width  (volatility state)
#   Volume trend  (accumulation / distribution)
#   RSI  (overbought / oversold extremes)
#
# Regimes (8 states):
#   bull_trend           — strong upward trend (ADX high, EMA rising)
#   bear_trend           — strong downward trend (ADX high, EMA falling)
#   ranging              — low ADX, price oscillating in range
#   volatility_expansion — BB width expanding rapidly (breakout/breakdown)
#   volatility_compression — BB width compressing (pre-breakout squeeze)
#   accumulation         — price consolidating near support, volume rising
#   distribution         — price near highs, volume declining, RSI diverging
#   uncertain            — insufficient data or borderline conditions
# ============================================================
from __future__ import annotations

import logging
import numpy as np
import pandas as pd
from typing import Optional

logger = logging.getLogger(__name__)

# Regime constants
REGIME_BULL_TREND    = "bull_trend"
REGIME_BEAR_TREND    = "bear_trend"
REGIME_RANGING       = "ranging"
REGIME_VOL_EXPANSION = "volatility_expansion"
REGIME_VOL_COMPRESS  = "volatility_compression"
REGIME_ACCUMULATION  = "accumulation"
REGIME_DISTRIBUTION  = "distribution"
REGIME_UNCERTAIN     = "uncertain"
REGIME_CRISIS        = "crisis"
REGIME_RECOVERY      = "recovery"
REGIME_LIQUIDATION_CASCADE = "liquidation_cascade"
REGIME_SQUEEZE       = "squeeze"

ALL_REGIMES = [
    REGIME_BULL_TREND, REGIME_BEAR_TREND, REGIME_RANGING,
    REGIME_VOL_EXPANSION, REGIME_VOL_COMPRESS,
    REGIME_ACCUMULATION, REGIME_DISTRIBUTION,
    REGIME_UNCERTAIN,
    REGIME_CRISIS, REGIME_RECOVERY, REGIME_LIQUIDATION_CASCADE, REGIME_SQUEEZE,
]

# Used by OrchestratorEngine
REGIME_NAME_MAP = {
    "bull_trend": "TRENDING_UP",
    "bear_trend": "TRENDING_DOWN",
    "ranging": "RANGING",
    "volatility_expansion": "HIGH_VOLATILITY",
    "volatility_compression": "HIGH_VOLATILITY",
    "accumulation": "ACCUMULATION",
    "distribution": "DISTRIBUTION",
    "crisis": "CRISIS",
    "recovery": "RECOVERY",
    "liquidation_cascade": "CRISIS",
    "squeeze": "HIGH_VOLATILITY",
    "uncertain": "UNKNOWN",
}

# ── Orchestrator weight tables for all 8 regimes ─────────────
# Maps regime → per-agent weight dict (all weights sum to 1.0)
REGIME_WEIGHTS: dict[str, dict[str, float]] = {
    REGIME_BULL_TREND: {
        "funding_rate":       0.10,
        "order_book":         0.12,
        "options_flow":       0.08,
        "macro":              0.10,
        "social_sentiment":   0.05,
        "news":               0.05,
        "geopolitical":       0.05,
        "sector_rotation":    0.05,
        "onchain":            0.10,
        "volatility_surface": 0.10,
        "liquidation_flow":   0.10,
        "crash_detection":    0.10,
    },
    REGIME_BEAR_TREND: {
        "funding_rate":       0.15,
        "order_book":         0.10,
        "options_flow":       0.12,
        "macro":              0.12,
        "social_sentiment":   0.06,
        "news":               0.05,
        "geopolitical":       0.07,
        "sector_rotation":    0.03,
        "onchain":            0.10,
        "volatility_surface": 0.08,
        "liquidation_flow":   0.07,
        "crash_detection":    0.05,
    },
    REGIME_RANGING: {
        "funding_rate":       0.12,
        "order_book":         0.15,
        "options_flow":       0.08,
        "macro":              0.08,
        "social_sentiment":   0.08,
        "news":               0.06,
        "geopolitical":       0.04,
        "sector_rotation":    0.05,
        "onchain":            0.10,
        "volatility_surface": 0.09,
        "liquidation_flow":   0.10,
        "crash_detection":    0.05,
    },
    REGIME_VOL_EXPANSION: {
        "funding_rate":       0.10,
        "order_book":         0.08,
        "options_flow":       0.15,
        "macro":              0.08,
        "social_sentiment":   0.05,
        "news":               0.04,
        "geopolitical":       0.05,
        "sector_rotation":    0.02,
        "onchain":            0.10,
        "volatility_surface": 0.18,
        "liquidation_flow":   0.10,
        "crash_detection":    0.05,
    },
    REGIME_VOL_COMPRESS: {
        "funding_rate":       0.08,
        "order_book":         0.12,
        "options_flow":       0.18,
        "macro":              0.07,
        "social_sentiment":   0.05,
        "news":               0.03,
        "geopolitical":       0.03,
        "sector_rotation":    0.04,
        "onchain":            0.10,
        "volatility_surface": 0.18,
        "liquidation_flow":   0.07,
        "crash_detection":    0.05,
    },
    REGIME_ACCUMULATION: {
        "funding_rate":       0.08,
        "order_book":         0.14,
        "options_flow":       0.07,
        "macro":              0.09,
        "social_sentiment":   0.07,
        "news":               0.05,
        "geopolitical":       0.04,
        "sector_rotation":    0.06,
        "onchain":            0.18,
        "volatility_surface": 0.07,
        "liquidation_flow":   0.10,
        "crash_detection":    0.05,
    },
    REGIME_DISTRIBUTION: {
        "funding_rate":       0.15,
        "order_book":         0.10,
        "options_flow":       0.12,
        "macro":              0.10,
        "social_sentiment":   0.08,
        "news":               0.05,
        "geopolitical":       0.05,
        "sector_rotation":    0.03,
        "onchain":            0.12,
        "volatility_surface": 0.08,
        "liquidation_flow":   0.07,
        "crash_detection":    0.05,
    },
    REGIME_UNCERTAIN: {
        "funding_rate":       0.09,
        "order_book":         0.09,
        "options_flow":       0.09,
        "macro":              0.09,
        "social_sentiment":   0.08,
        "news":               0.08,
        "geopolitical":       0.08,
        "sector_rotation":    0.07,
        "onchain":            0.09,
        "volatility_surface": 0.08,
        "liquidation_flow":   0.08,
        "crash_detection":    0.08,
    },
    REGIME_CRISIS: {
        "crash_detection":    0.25,
        "liquidation_flow":   0.20,
        "funding_rate":       0.15,
        "macro":              0.15,
        "options_flow":       0.10,
        "order_book":         0.05,
        "onchain":            0.05,
        "volatility_surface": 0.05,
        "social_sentiment":   0.00,
        "news":               0.00,
        "geopolitical":       0.00,
        "sector_rotation":    0.00,
    },
    REGIME_RECOVERY: {
        "onchain":            0.20,
        "funding_rate":       0.15,
        "order_book":         0.15,
        "macro":              0.12,
        "volatility_surface": 0.10,
        "liquidation_flow":   0.10,
        "options_flow":       0.08,
        "social_sentiment":   0.05,
        "news":               0.03,
        "crash_detection":    0.02,
        "geopolitical":       0.00,
        "sector_rotation":    0.00,
    },
    REGIME_LIQUIDATION_CASCADE: {
        "liquidation_flow":   0.35,
        "crash_detection":    0.25,
        "funding_rate":       0.15,
        "order_book":         0.10,
        "volatility_surface": 0.10,
        "options_flow":       0.05,
        "macro":              0.00,
        "social_sentiment":   0.00,
        "news":               0.00,
        "geopolitical":       0.00,
        "sector_rotation":    0.00,
        "onchain":            0.00,
    },
    REGIME_SQUEEZE: {
        "funding_rate":       0.30,
        "order_book":         0.25,
        "liquidation_flow":   0.20,
        "volatility_surface": 0.10,
        "crash_detection":    0.08,
        "options_flow":       0.07,
        "macro":              0.00,
        "social_sentiment":   0.00,
        "news":               0.00,
        "geopolitical":       0.00,
        "sector_rotation":    0.00,
        "onchain":            0.00,
    },
}


class RegimeClassifier:
    """
    Classifies the current market regime into one of 8 states using
    rule-based logic on precomputed indicators.

    Parameters
    ----------
    adx_trend_threshold : float
        ADX level above which a trend is considered strong (default 25).
    adx_ranging_threshold : float
        ADX level below which market is considered ranging (default 20).
    ema_slope_window : int
        Number of bars to measure EMA slope over (default 5).
    bb_expansion_factor : float
        BB width multiple above rolling mean that signals expansion (default 1.5).
    bb_compression_factor : float
        BB width multiple below rolling mean that signals compression (default 0.6).
    bb_rolling_window : int
        Rolling window for BB width baseline (default 20).
    """

    def __init__(
        self,
        adx_trend_threshold: float = 25.0,
        adx_ranging_threshold: float = 20.0,
        ema_slope_window: int = 5,
        bb_expansion_factor: float = 1.5,
        bb_compression_factor: float = 0.6,
        bb_rolling_window: int = 20,
    ):
        self.adx_trend_threshold    = adx_trend_threshold
        self.adx_ranging_threshold  = adx_ranging_threshold
        self.ema_slope_window       = ema_slope_window
        self.bb_expansion_factor    = bb_expansion_factor
        self.bb_compression_factor  = bb_compression_factor
        self.bb_rolling_window      = bb_rolling_window
        # Hysteresis to prevent single-candle regime flips
        self._regime_buffer: list = []
        self._hysteresis_bars: int = 3
        self._committed_regime: str = "uncertain"

    def classify(self, df: pd.DataFrame) -> tuple[str, float, dict]:
        """
        Classify the regime from the last row of an indicator DataFrame.

        Parameters
        ----------
        df : pd.DataFrame
            Must contain at minimum the columns produced by
            indicator_library.calculate_all(). Needs at least 30 rows.

        Returns
        -------
        (regime_label, confidence, features_dict)
            regime_label : one of ALL_REGIMES
            confidence   : 0.0–1.0
            features_dict: raw feature values used for classification
        """
        if df is None or len(df) < 30:
            return REGIME_UNCERTAIN, 0.0, {}

        try:
            return self._classify(df)
        except Exception as exc:
            logger.warning("RegimeClassifier error: %s", exc)
            return REGIME_UNCERTAIN, 0.0, {}

    def _classify(self, df: pd.DataFrame) -> tuple[str, float, dict]:
        # Helper function to apply hysteresis
        def _apply_hysteresis(new_regime: str, confidence: float) -> tuple[str, float]:
            """Apply hysteresis buffer to prevent single-candle regime flips."""
            self._regime_buffer.append(new_regime)
            if len(self._regime_buffer) > self._hysteresis_bars:
                self._regime_buffer.pop(0)

            if len(self._regime_buffer) == self._hysteresis_bars and all(r == self._regime_buffer[0] for r in self._regime_buffer):
                self._committed_regime = new_regime
                return new_regime, confidence
            else:
                return self._committed_regime, confidence * 0.8  # Penalize confidence when using committed regime

        # ── Extract last-row indicator values ─────────────────
        adx_col      = "adx"     if "adx"     in df.columns else None
        ema20_col    = "ema_20"  if "ema_20"  in df.columns else None
        bb_upper_col = "bb_upper" if "bb_upper" in df.columns else None
        bb_lower_col = "bb_lower" if "bb_lower" in df.columns else None
        bb_mid_col   = "bb_mid"   if "bb_mid"   in df.columns else None
        rsi_col      = "rsi"     if "rsi"     in df.columns else None
        vol_col      = "volume"  if "volume"  in df.columns else None
        close_col    = "close"   if "close"   in df.columns else None

        features: dict = {}

        # ── ADX ───────────────────────────────────────────────
        adx = float(df[adx_col].iloc[-1]) if adx_col else None
        features["adx"] = adx

        # ── EMA slope (% change over N bars) ──────────────────
        ema_slope = None
        if ema20_col:
            n = min(self.ema_slope_window, len(df) - 1)
            ema_now  = float(df[ema20_col].iloc[-1])
            ema_prev = float(df[ema20_col].iloc[-1 - n])
            if ema_prev and ema_prev != 0:
                ema_slope = (ema_now - ema_prev) / ema_prev * 100.0
        features["ema_slope_pct"] = ema_slope

        # ── BB width (relative) ───────────────────────────────
        bb_width = None
        bb_width_ratio = None
        if bb_upper_col and bb_lower_col and bb_mid_col:
            upper = df[bb_upper_col].dropna()
            lower = df[bb_lower_col].dropna()
            mid   = df[bb_mid_col].dropna()
            if len(upper) > self.bb_rolling_window and float(mid.iloc[-1]) != 0:
                widths = ((upper - lower) / mid).dropna()
                bb_width = float(widths.iloc[-1])
                rolling_mean = float(widths.rolling(self.bb_rolling_window).mean().iloc[-1])
                bb_width_ratio = bb_width / rolling_mean if rolling_mean else None
        features["bb_width"] = bb_width
        features["bb_width_ratio"] = bb_width_ratio

        # ── RSI ───────────────────────────────────────────────
        rsi = float(df[rsi_col].iloc[-1]) if rsi_col else None
        features["rsi"] = rsi

        # ── Volume trend (20-bar vs 60-bar average) ───────────
        vol_trend = None
        if vol_col and len(df) >= 60:
            vol_recent = float(df[vol_col].iloc[-20:].mean())
            vol_base   = float(df[vol_col].iloc[-60:].mean())
            vol_trend  = (vol_recent / vol_base - 1.0) * 100.0 if vol_base > 0 else 0.0
        features["vol_trend_pct"] = vol_trend

        # ── Price position (% from 20-bar high) ──────────────
        price_from_high = None
        if close_col and len(df) >= 20:
            recent_high = float(df[close_col].iloc[-20:].max())
            current_close = float(df[close_col].iloc[-1])
            if recent_high > 0:
                price_from_high = (current_close / recent_high - 1.0) * 100.0
        features["price_from_20h_pct"] = price_from_high

        # ── EMA direction ─────────────────────────────────────
        ema_slope_current = None
        if ema20_col and len(df) >= 2:
            ema_now = float(df[ema20_col].iloc[-1])
            ema_prev = float(df[ema20_col].iloc[-2])
            if ema_prev is not None and not np.isnan(ema_prev) and ema_prev != 0:
                ema_slope_current = (ema_now - ema_prev) / ema_prev
        features["ema_slope_current"] = ema_slope_current

        # ── Classification logic ───────────────────────────────

        # Priority 0: Crisis / Liquidation Cascade / Squeeze detection
        # These override everything when extreme conditions are present

        # Liquidation Cascade: rapid price decline, BB expansion extreme, RSI < 22
        if (bb_width_ratio is not None and bb_width_ratio > 2.5 and
                rsi is not None and rsi < 22 and
                vol_trend is not None and vol_trend > 50):
            confidence = min(1.0, (bb_width_ratio - 2.5) * 0.5 + 0.7)
            features["crisis_type"] = "liquidation_cascade"
            regime, conf = _apply_hysteresis(REGIME_LIQUIDATION_CASCADE, confidence)
            return regime, round(conf, 3), features

        # Squeeze: RSI extreme (>78 or <22) AND BB width contracting AND volume declining
        if (rsi is not None and (rsi > 78 or rsi < 22) and
                bb_width_ratio is not None and bb_width_ratio < 0.5 and
                vol_trend is not None and vol_trend < -15):
            confidence = min(1.0, abs(rsi - 50) / 50.0 * 0.8 + 0.4)
            direction_note = "long_squeeze" if rsi > 50 else "short_squeeze"
            features["squeeze_direction"] = direction_note
            regime, conf = _apply_hysteresis(REGIME_SQUEEZE, confidence)
            return regime, round(conf, 3), features

        # Crisis: BB expansion >2.0, RSI < 28, volume trend rising sharply
        if (bb_width_ratio is not None and bb_width_ratio > 2.0 and
                rsi is not None and rsi < 28 and
                vol_trend is not None and vol_trend > 30):
            confidence = min(1.0, (28.0 - rsi) / 28.0 * 0.5 + 0.5)
            features["crisis_type"] = "bb_expansion_rsi_low"
            regime, conf = _apply_hysteresis(REGIME_CRISIS, confidence)
            return regime, round(conf, 3), features

        # Recovery: close > EMA20, RSI recovering (38-55), normal BB, positive EMA slope, ADX < 25
        if (close_col is not None and ema20_col is not None and
                rsi is not None and 38 <= rsi < 55 and
                vol_trend is not None and vol_trend > 5 and
                ema_slope_current is not None and ema_slope_current > 0 and
                adx is not None and adx < 25 and
                bb_width_ratio is not None and 0.7 <= bb_width_ratio <= 1.3):
            close_now = float(df[close_col].iloc[-1])
            ema_now = float(df[ema20_col].iloc[-1])
            if close_now is not None and ema_now is not None and close_now > ema_now:
                confidence = 0.55 + min(0.25, (rsi - 38) / 50.0)
                features["recovery_type"] = "post_decline"
                regime, conf = _apply_hysteresis(REGIME_RECOVERY, confidence)
                return regime, round(conf, 3), features

        # Priority 1: Volatility state overrides trend/ranging
        if bb_width_ratio is not None:
            if bb_width_ratio >= self.bb_expansion_factor:
                confidence = min(1.0, (bb_width_ratio - self.bb_expansion_factor) * 2.0 + 0.5)
                regime, conf = _apply_hysteresis(REGIME_VOL_EXPANSION, confidence)
                return regime, round(conf, 3), features
            if bb_width_ratio <= self.bb_compression_factor:
                confidence = min(1.0, (self.bb_compression_factor - bb_width_ratio) * 3.0 + 0.5)
                regime, conf = _apply_hysteresis(REGIME_VOL_COMPRESS, confidence)
                return regime, round(conf, 3), features

        # Priority 2: Trend detection via ADX + EMA slope
        if adx is not None and not np.isnan(adx):
            if adx >= self.adx_trend_threshold:
                if ema_slope is not None:
                    if ema_slope > 0:
                        confidence = min(1.0, (adx - self.adx_trend_threshold) / 20.0 + 0.5)
                        regime, conf = _apply_hysteresis(REGIME_BULL_TREND, confidence)
                        return regime, round(conf, 3), features
                    else:
                        confidence = min(1.0, (adx - self.adx_trend_threshold) / 20.0 + 0.5)
                        regime, conf = _apply_hysteresis(REGIME_BEAR_TREND, confidence)
                        return regime, round(conf, 3), features

            if adx < self.adx_ranging_threshold:
                # Priority 3: Accumulation vs Distribution vs Ranging
                # Accumulation: ranging + rising volume + RSI recovering
                if (vol_trend is not None and vol_trend > 10.0 and
                        rsi is not None and 30 <= rsi <= 55):
                    confidence = min(1.0, 0.50 + vol_trend / 100.0)
                    regime, conf = _apply_hysteresis(REGIME_ACCUMULATION, confidence)
                    return regime, round(conf, 3), features

                # Distribution: ranging + declining volume + RSI diverging near top
                if (vol_trend is not None and vol_trend < -10.0 and
                        rsi is not None and rsi >= 60 and
                        price_from_high is not None and price_from_high > -5.0):
                    confidence = min(1.0, 0.50 + abs(vol_trend) / 100.0)
                    regime, conf = _apply_hysteresis(REGIME_DISTRIBUTION, confidence)
                    return regime, round(conf, 3), features

                confidence = min(1.0, (self.adx_ranging_threshold - adx) / self.adx_ranging_threshold + 0.4)
                regime, conf = _apply_hysteresis(REGIME_RANGING, confidence)
                return regime, round(conf, 3), features

        # Fallback: uncertain
        confidence = 0.3
        regime, conf = _apply_hysteresis(REGIME_UNCERTAIN, confidence)
        return regime, round(conf, 3), features

    def get_regime_weights(self, regime: str) -> dict[str, float]:
        """Return agent weight table for the given regime."""
        return REGIME_WEIGHTS.get(regime, REGIME_WEIGHTS[REGIME_UNCERTAIN])
