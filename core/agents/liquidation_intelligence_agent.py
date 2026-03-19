# ============================================================
# NEXUS TRADER — Liquidation Intelligence Agent (Phase 2b)
#
# Fetches and analyzes liquidation data to detect cascade risk.
#
# Data sources (in priority order):
# 1. Coinglass API (if API key configured)
# 2. Exchange liquidation endpoints via CCXT
# 3. Synthetic estimation from price + volume action
#
# Publishes CASCADE_ALERT and LIQUIDITY_ALERT events.
# Updates internal state accessible to sub-models.
# ============================================================
from __future__ import annotations

import logging
import threading
import time
import requests
from typing import Optional
from datetime import datetime, timedelta
import pandas as pd
from dataclasses import dataclass
from core.event_bus import bus, Topics

logger = logging.getLogger(__name__)


@dataclass
class LiquidationState:
    """State snapshot from liquidation analysis."""
    liq_density_long: float              # 0-1: concentration of long liquidations nearby above price
    liq_density_short: float             # 0-1: concentration of short liquidations nearby below price
    cascade_risk: float                  # 0-1: probability of cascade in next 3 candles
    dominant_side: str                   # "long" | "short" | "neutral"
    estimated_liq_level_long: float      # price level where mass long liq would trigger
    estimated_liq_level_short: float     # price level where mass short liq would trigger
    data_source: str                     # "coinglass" | "exchange" | "synthetic"
    timestamp: datetime = None

    def __post_init__(self):
        if self.timestamp is None:
            self.timestamp = datetime.utcnow()

    def to_dict(self) -> dict:
        """Convert to dict for event publishing."""
        return {
            "liq_density_long": round(self.liq_density_long, 4),
            "liq_density_short": round(self.liq_density_short, 4),
            "cascade_risk": round(self.cascade_risk, 4),
            "dominant_side": self.dominant_side,
            "estimated_liq_level_long": round(self.estimated_liq_level_long, 8),
            "estimated_liq_level_short": round(self.estimated_liq_level_short, 8),
            "data_source": self.data_source,
            "timestamp": self.timestamp.isoformat(),
        }


class LiquidationIntelligenceAgent:
    """
    Fetches and analyzes liquidation data to detect cascade risk.

    Thread-safe with 5-minute TTL caching per symbol.
    Publishes CASCADE_ALERT and LIQUIDITY_ALERT events.
    """

    COINGLASS_API_URL = "https://open-api.coinglass.com/public/v2/liquidation_map"
    CACHE_TTL_SECONDS = 300  # 5-minute cache per symbol
    REQUEST_TIMEOUT = 5.0

    def __init__(self, exchange=None):
        """
        Parameters
        ----------
        exchange : optional
            CCXT exchange instance for fallback liquidation data.
        """
        self.exchange = exchange
        self._cache: dict[str, tuple[LiquidationState, float]] = {}  # symbol -> (state, timestamp)
        self._lock = threading.Lock()
        self._warned_coinglass = False

    def update(
        self,
        symbol: str,
        df: pd.DataFrame,
        ticker: dict,
    ) -> dict:
        """
        Fetch and analyze liquidation data.

        Parameters
        ----------
        symbol : str
            Trading pair, e.g. "BTC/USDT"
        df : pd.DataFrame
            OHLCV DataFrame with indicators (must have at least 20 rows)
        ticker : dict
            Latest ticker data from exchange

        Returns
        -------
        dict containing liquidation_state (LiquidationState as dict)
        """
        with self._lock:
            # Check cache
            if symbol in self._cache:
                state, cache_time = self._cache[symbol]
                if time.time() - cache_time < self.CACHE_TTL_SECONDS:
                    return {"liquidation_state": state.to_dict()}

        # Attempt to fetch from Coinglass
        state = self._fetch_coinglass(symbol, df, ticker)

        if state is None:
            # Fallback to synthetic estimation
            state = self._estimate_synthetic(symbol, df, ticker)

        # Publish alerts if thresholds met
        self._publish_alerts(symbol, state)

        # Cache result
        with self._lock:
            self._cache[symbol] = (state, time.time())

        return {"liquidation_state": state.to_dict()}

    def _fetch_coinglass(
        self,
        symbol: str,
        df: pd.DataFrame,
        ticker: dict,
    ) -> Optional[LiquidationState]:
        """Fetch liquidation map from Coinglass API."""
        try:
            from config.settings import settings
        except ImportError:
            logger.debug("Settings module not available; skipping Coinglass")
            return None

        api_key = settings.get("agents.coinglass_api_key", "")
        if not api_key or api_key == "__vault__":
            if not self._warned_coinglass:
                logger.debug("Coinglass API key not configured")
                self._warned_coinglass = True
            return None

        try:
            # Extract base symbol (BTC from BTC/USDT)
            base = symbol.split("/")[0].upper()

            params = {
                "symbol": base,
                "token": api_key,
            }

            response = requests.get(
                self.COINGLASS_API_URL,
                params=params,
                timeout=self.REQUEST_TIMEOUT,
            )
            response.raise_for_status()
            data = response.json()

            if data.get("code") != 0:
                logger.warning("Coinglass API error: %s", data.get("msg"))
                return None

            result = data.get("data", {})
            if not result:
                logger.debug("Coinglass returned empty data for %s", symbol)
                return None

            # Parse liquidation density from API response
            # Assuming API returns structure like:
            # { "longLiquidationDensity": 0.6, "shortLiquidationDensity": 0.4, ... }
            liq_density_long = float(result.get("longLiquidationDensity", 0.0))
            liq_density_short = float(result.get("shortLiquidationDensity", 0.0))

            # Clamp to 0-1 range
            liq_density_long = max(0.0, min(1.0, liq_density_long))
            liq_density_short = max(0.0, min(1.0, liq_density_short))

            current_price = float(df["close"].iloc[-1])

            # Cascade risk from liquidation density
            cascade_risk = max(liq_density_long, liq_density_short) * 0.8

            # Estimate liquidation levels
            estimated_liq_level_long = float(result.get("estimatedLongLiqLevel", current_price * 0.985))
            estimated_liq_level_short = float(result.get("estimatedShortLiqLevel", current_price * 1.015))

            # Dominant side
            if liq_density_long > liq_density_short + 0.1:
                dominant_side = "long"
            elif liq_density_short > liq_density_long + 0.1:
                dominant_side = "short"
            else:
                dominant_side = "neutral"

            state = LiquidationState(
                liq_density_long=liq_density_long,
                liq_density_short=liq_density_short,
                cascade_risk=cascade_risk,
                dominant_side=dominant_side,
                estimated_liq_level_long=estimated_liq_level_long,
                estimated_liq_level_short=estimated_liq_level_short,
                data_source="coinglass",
            )

            logger.info(
                "Coinglass liquidation data for %s: long_density=%.3f, short_density=%.3f, cascade_risk=%.3f",
                symbol, liq_density_long, liq_density_short, cascade_risk,
            )

            return state

        except requests.RequestException as e:
            logger.warning("Coinglass API request failed for %s: %s", symbol, e)
            return None
        except Exception as e:
            logger.error("Unexpected error fetching Coinglass data: %s", e)
            return None

    def _fetch_exchange(
        self,
        symbol: str,
        df: pd.DataFrame,
        ticker: dict,
    ) -> Optional[LiquidationState]:
        """Fetch liquidation from exchange API via CCXT (placeholder for future)."""
        if not self.exchange:
            return None

        try:
            # TODO: Implement exchange-specific liquidation endpoint calls
            # For now, return None to fall back to synthetic
            return None
        except Exception as e:
            logger.debug("Exchange liquidation fetch failed: %s", e)
            return None

    def _estimate_synthetic(
        self,
        symbol: str,
        df: pd.DataFrame,
        ticker: dict,
    ) -> LiquidationState:
        """
        Synthetic estimation from price + volume action.

        Fallback when APIs unavailable.
        """
        if len(df) < 20:
            # Insufficient data; return neutral state
            current_price = float(df["close"].iloc[-1])
            return LiquidationState(
                liq_density_long=0.0,
                liq_density_short=0.0,
                cascade_risk=0.0,
                dominant_side="neutral",
                estimated_liq_level_long=current_price * 0.985,
                estimated_liq_level_short=current_price * 1.015,
                data_source="synthetic",
            )

        # Extract recent price action
        close = float(df["close"].iloc[-1])
        low_20 = float(df["low"].iloc[-20:].min())
        high_20 = float(df["high"].iloc[-20:].max())
        recent_low = low_20
        recent_high = high_20

        # Volume analysis
        vol_now = float(df["volume"].iloc[-1])
        vol_avg_20 = float(df["volume"].iloc[-20:].mean())
        vol_mult = vol_now / vol_avg_20 if vol_avg_20 > 0 else 0.0

        # Estimate liquidation levels
        estimated_liq_level_long = recent_low * 0.985  # Just below recent 20-bar low
        estimated_liq_level_short = recent_high * 1.015  # Just above recent 20-bar high

        # ADX and trend analysis
        adx = self._col(df, "adx")
        ema20 = self._col(df, "ema_20")

        # Base cascade risk
        cascade_risk = 0.0

        # Check for volume spike on downward break
        if len(df) >= 2 and close < low_20 and vol_mult > 2.0:
            cascade_risk = 0.65

        # Add risk if strong downtrend and ADX high
        if adx and adx > 30 and ema20 and close < ema20:
            cascade_risk = min(1.0, cascade_risk + 0.15)

        # ATR-based breakout distance
        atr = self._atr(df, 14)
        if atr > 0:
            distance_to_long_liq = (close - estimated_liq_level_long) / atr
            if distance_to_long_liq < 1.0 and distance_to_long_liq > 0:
                cascade_risk = min(1.0, cascade_risk + 0.1)

        # Liquidation density (synthetic)
        # Long liquidation density: concentration above current price
        if close > 0 and recent_high > close:
            liq_density_long = min(1.0, (recent_high - close) / (recent_high - recent_low + 0.001))
        else:
            liq_density_long = 0.0

        # Short liquidation density: concentration below current price
        if close > 0 and recent_low < close:
            liq_density_short = min(1.0, (close - recent_low) / (recent_high - recent_low + 0.001))
        else:
            liq_density_short = 0.0

        # Dominant side
        if liq_density_long > liq_density_short + 0.1:
            dominant_side = "long"
        elif liq_density_short > liq_density_long + 0.1:
            dominant_side = "short"
        else:
            dominant_side = "neutral"

        # Check for cross-exchange divergence
        try:
            if self.exchange and hasattr(self.exchange, 'fetch_ticker'):
                # Fetch Binance price as benchmark
                binance_ticker = self.exchange.fetch_ticker("BTC/USDT")
                binance_price = float(binance_ticker.get("last", close))
                divergence_pct = abs(close - binance_price) / binance_price * 100.0
                if divergence_pct > 0.5:
                    cascade_risk = min(1.0, cascade_risk + 0.1)
        except Exception:
            pass  # Silently skip divergence check if exchange unavailable

        state = LiquidationState(
            liq_density_long=liq_density_long,
            liq_density_short=liq_density_short,
            cascade_risk=cascade_risk,
            dominant_side=dominant_side,
            estimated_liq_level_long=estimated_liq_level_long,
            estimated_liq_level_short=estimated_liq_level_short,
            data_source="synthetic",
        )

        logger.debug(
            "Synthetic liquidation estimate for %s: long_density=%.3f, short_density=%.3f, cascade_risk=%.3f",
            symbol, liq_density_long, liq_density_short, cascade_risk,
        )

        return state

    def _publish_alerts(self, symbol: str, state: LiquidationState) -> None:
        """Publish CASCADE_ALERT and LIQUIDITY_ALERT events if thresholds met."""
        # Publish CASCADE_ALERT if cascade_risk > 0.70
        if state.cascade_risk > 0.70:
            bus.publish(
                Topics.CASCADE_ALERT,
                {
                    "symbol": symbol,
                    "cascade_risk": state.cascade_risk,
                    "dominant_side": state.dominant_side,
                    "data_source": state.data_source,
                    "estimated_liq_level_long": state.estimated_liq_level_long,
                    "estimated_liq_level_short": state.estimated_liq_level_short,
                },
                source="liquidation_agent",
            )

        # Publish LIQUIDITY_ALERT if density > 0.75
        if state.liq_density_long > 0.75 or state.liq_density_short > 0.75:
            bus.publish(
                Topics.LIQUIDITY_ALERT,
                {
                    "symbol": symbol,
                    "liq_density_long": state.liq_density_long,
                    "liq_density_short": state.liq_density_short,
                    "dominant_side": state.dominant_side,
                    "data_source": state.data_source,
                },
                source="liquidation_agent",
            )

    @staticmethod
    def _atr(df: pd.DataFrame, period: int = 14) -> float:
        """Safe ATR extraction from DataFrame."""
        col = f"atr_{period}"
        if col in df.columns:
            v = df[col].iloc[-1]
            if pd.notna(v) and v > 0:
                return float(v)
        # Fallback: manual approximation
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
