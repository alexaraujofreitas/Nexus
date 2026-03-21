"""
BTC Trend Filter — Phase 5.
Suppresses trades when BTC trend strongly contradicts trade direction.
Uses a simple EMA-based BTC trend signal to avoid fighting the macro trend.
"""
from __future__ import annotations
import logging
from typing import Optional
import pandas as pd
from config.settings import settings as _s

logger = logging.getLogger(__name__)


def get_btc_trend(exchange=None) -> Optional[str]:
    """
    Returns 'bullish', 'bearish', or 'neutral' based on BTC 1h EMA alignment.
    """
    if not _s.get("filters.btc_trend.enabled", True):
        return None
    try:
        if exchange is None:
            from core.market_data.exchange_manager import exchange_manager
            exchange = exchange_manager.get_exchange()
        if exchange is None:
            return None
        raw = exchange.fetch_ohlcv("BTC/USDT", "1h", limit=110)
        if not raw or len(raw) < 50:
            return None
        df = pd.DataFrame(raw, columns=["ts", "open", "high", "low", "close", "volume"])
        close = df["close"].astype(float)
        ema21 = close.ewm(span=21, adjust=False).mean()
        ema50 = close.ewm(span=50, adjust=False).mean()
        last_close = float(close.iloc[-1])
        last_ema21 = float(ema21.iloc[-1])
        last_ema50 = float(ema50.iloc[-1])
        margin = float(_s.get("filters.btc_trend.strong_trend_margin_pct", 0.5)) / 100.0
        if last_close > last_ema21 * (1 + margin) and last_ema21 > last_ema50:
            return "bullish"
        if last_close < last_ema21 * (1 - margin) and last_ema21 < last_ema50:
            return "bearish"
        return "neutral"
    except Exception as exc:
        logger.debug("BTCTrendFilter: error: %s", exc)
        return None


def check_btc_trend_conflict(direction: str, btc_trend: Optional[str]) -> tuple[bool, str]:
    """
    Returns (passed, reason).
    Rejects if trade direction strongly conflicts with BTC macro trend.
    Only applies aggressive filter for non-BTC symbols.
    """
    if not _s.get("filters.btc_trend.enabled", True):
        return True, ""
    if btc_trend is None or btc_trend == "neutral":
        return True, ""
    is_long = direction.lower() in ("buy", "long")
    if is_long and btc_trend == "bearish":
        return False, f"BTC trend filter: BTC is bearish — suppressing long"
    if not is_long and btc_trend == "bullish":
        return False, f"BTC trend filter: BTC is bullish — suppressing short"
    return True, ""
