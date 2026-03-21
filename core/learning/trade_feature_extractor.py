"""
Trade Feature Extractor — Phase 3.
Extracts ML-ready features from trade log entries and live scan context.
Used to train the ProbabilityCalibrator logistic regression model.
"""
from __future__ import annotations
import logging
from typing import Optional
import pandas as pd

logger = logging.getLogger(__name__)

# All known models (for one-hot encoding)
_ALL_MODELS = [
    "trend", "mean_reversion", "momentum_breakout", "vwap_reversion",
    "liquidity_sweep", "funding_rate", "order_book", "sentiment", "rl_ensemble",
]

# All known regimes (for one-hot encoding)
_ALL_REGIMES = [
    "bull_trend", "bear_trend", "ranging", "volatility_expansion",
    "volatility_compression", "uncertain", "crisis", "liquidation_cascade",
    "squeeze", "recovery", "accumulation", "distribution",
]


def extract_features_from_trade(trade: dict) -> Optional[dict]:
    """
    Extract feature dict from a trade log record.
    Returns None if critical fields are missing.
    """
    try:
        regime = (trade.get("regime") or "uncertain").lower().replace(" ", "_")
        primary_model = (trade.get("primary_model") or "")
        direction = (trade.get("side") or trade.get("direction") or "buy").lower()
        utc_hour = trade.get("utc_hour_at_entry")

        features = {
            # Core numerical features
            "confluence_score":  float(trade.get("confluence_score") or 0.0),
            "rsi_at_entry":      float(trade.get("rsi_at_entry") or 50.0),
            "adx_at_entry":      float(trade.get("adx_at_entry") or 20.0),
            "atr_ratio":         float(trade.get("atr_ratio") or 1.0),
            "funding_rate":      float(trade.get("funding_rate") or 0.0),
            "utc_hour_sin":      __import__("math").sin(2 * __import__("math").pi * (utc_hour or 15) / 24),
            "utc_hour_cos":      __import__("math").cos(2 * __import__("math").pi * (utc_hour or 15) / 24),
            "is_long":           1 if direction in ("buy", "long") else 0,
            "num_models_fired":  len(trade.get("models_fired") or []),
        }

        # Regime one-hot
        for r in _ALL_REGIMES:
            features[f"regime_{r}"] = 1 if regime == r else 0

        # Primary model one-hot
        for m in _ALL_MODELS:
            features[f"model_{m}"] = 1 if primary_model == m else 0

        return features
    except Exception as exc:
        logger.debug("FeatureExtractor: error extracting from trade: %s", exc)
        return None


def extract_features_live(
    regime: str,
    confluence_score: float,
    direction: str,
    models_fired: list[str],
    rsi: Optional[float] = None,
    adx: Optional[float] = None,
    atr_ratio: Optional[float] = None,
    funding_rate: Optional[float] = None,
    utc_hour: Optional[int] = None,
) -> dict:
    """Extract features for a live trade candidate (for real-time prediction)."""
    import math
    hour = utc_hour or 15
    regime_clean = (regime or "uncertain").lower().replace(" ", "_")
    primary_model = models_fired[0] if models_fired else ""

    features = {
        "confluence_score":  float(confluence_score or 0.0),
        "rsi_at_entry":      float(rsi or 50.0),
        "adx_at_entry":      float(adx or 20.0),
        "atr_ratio":         float(atr_ratio or 1.0),
        "funding_rate":      float(funding_rate or 0.0),
        "utc_hour_sin":      math.sin(2 * math.pi * hour / 24),
        "utc_hour_cos":      math.cos(2 * math.pi * hour / 24),
        "is_long":           1 if direction.lower() in ("buy", "long") else 0,
        "num_models_fired":  len(models_fired or []),
    }
    for r in _ALL_REGIMES:
        features[f"regime_{r}"] = 1 if regime_clean == r else 0
    for m in _ALL_MODELS:
        features[f"model_{m}"] = 1 if primary_model == m else 0
    return features


def build_training_dataset(trades: list[dict]) -> tuple[list[dict], list[int]]:
    """
    Build X (feature dicts), y (labels: 1=won, 0=lost) from trade log.
    Filters out records with missing critical fields.
    """
    X, y = [], []
    for trade in trades:
        features = extract_features_from_trade(trade)
        if features is None:
            continue
        won = trade.get("won")
        if won is None:
            won = (trade.get("pnl_pct") or 0.0) > 0
        X.append(features)
        y.append(1 if won else 0)
    return X, y
