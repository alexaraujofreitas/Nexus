# ============================================================
# NEXUS TRADER — FinBERT Sentiment Sub-Model (Phase 2c)
#
# Wraps FinBERT/VADER sentiment analysis into tradeable signals.
# Fetches headlines via NewsFeed, runs FinBERT pipeline, fires signals.
#
# Active in all regimes EXCEPT "liquidation_cascade" and "crisis"
# (sentiment unreliable in panic conditions).
#
# Signal fires only if:
#   |net_score| > 0.35 AND headline_count >= 3 AND confidence > 0.55
#
# Strength = min(abs(net_score) * 1.2, 0.95) (sentiment is confirming, not primary)
# SL: 1.5× ATR  TP: 2.5× ATR
# ============================================================
from __future__ import annotations

import logging
import threading
from typing import Optional

import pandas as pd

from config.settings import settings as _s
from core.signals.sub_models.base import BaseSubModel
from core.meta_decision.order_candidate import ModelSignal
from core.event_bus import bus, Topics

logger = logging.getLogger(__name__)

# ── Asset-specific keyword mapping ───────────────────────────
ASSET_KEYWORDS: dict[str, list[str]] = {
    "BTC":  ["BTC", "Bitcoin"],
    "ETH":  ["ETH", "Ethereum"],
    "BNB":  ["BNB", "Binance Coin", "Binance"],
    "SOL":  ["SOL", "Solana"],
    "XRP":  ["XRP", "Ripple"],
    "ADA":  ["ADA", "Cardano"],
    "DOGE": ["DOGE", "Dogecoin"],
    "DOT":  ["DOT", "Polkadot"],
    "AVAX": ["AVAX", "Avalanche"],
    "MATIC":["MATIC", "Polygon"],
    "LINK": ["LINK", "Chainlink"],
    "LTC":  ["LTC", "Litecoin"],
    "UNI":  ["UNI", "Uniswap"],
    "ATOM": ["ATOM", "Cosmos"],
}

# ── Module-level singletons (thread-safe lazy init) ─────────
# _news_feeds: per-asset dict so each asset queries its own keywords
_news_feeds: dict[str, object] = {}
_finbert: Optional[object] = None
_singleton_lock = threading.Lock()


def _get_news_feed(base_asset: str):
    """
    Lazy-load per-asset NewsFeed instance.
    Each asset gets its own NewsFeed with asset-specific search keywords,
    ensuring sentiment signals are relevant to the asset being evaluated.
    Falls back to the asset name alone if no keyword mapping exists.
    """
    if base_asset not in _news_feeds:
        with _singleton_lock:
            if base_asset not in _news_feeds:
                try:
                    from core.nlp.news_feed import NewsFeed
                    keywords = ASSET_KEYWORDS.get(base_asset, [base_asset])
                    _news_feeds[base_asset] = NewsFeed(symbols=keywords)
                    logger.debug(
                        "SentimentModel: initialized NewsFeed for %s with keywords %s",
                        base_asset, keywords,
                    )
                except Exception as e:
                    logger.warning(f"SentimentModel: failed to load NewsFeed for {base_asset}: {e}")
                    return None
    return _news_feeds.get(base_asset)


def _get_finbert_pipeline():
    """
    Lazy-load FinBERTPipeline singleton.
    Uses CUDA GPU if available (RTX 4070 provides ~5-10ms vs ~30-80ms on CPU),
    falling back to CPU if CUDA is not available or initialization fails.
    """
    global _finbert
    if _finbert is None:
        with _singleton_lock:
            if _finbert is None:
                try:
                    from core.nlp.finbert_pipeline import FinBERTPipeline
                    # Prefer GPU for low-latency inference; fall back to CPU
                    try:
                        import torch
                        device = "cuda" if torch.cuda.is_available() else "cpu"
                    except ImportError:
                        device = "cpu"
                    _finbert = FinBERTPipeline(device=device)
                    logger.info("SentimentModel: initialized FinBERTPipeline on device=%s", device)
                except Exception as e:
                    logger.warning(f"SentimentModel: failed to load FinBERTPipeline: {e}")
                    return None
    return _finbert


class SentimentModel(BaseSubModel):
    """
    Sub-model that fires signals based on FinBERT sentiment analysis.

    Fetches headlines via NewsFeed, analyzes with FinBERTPipeline,
    publishes FINBERT_SIGNAL event, and generates ModelSignal for IDSS.

    Active in all regimes except crisis/liquidation_cascade.
    """

    # Active in all regimes EXCEPT crisis and liquidation_cascade
    ACTIVE_REGIMES: list[str] = [
        "bull_trend",
        "bear_trend",
        "ranging",
        "volatility_expansion",
        "volatility_compression",
        "accumulation",
        "distribution",
        "uncertain",
        "recovery",
    ]

    @property
    def name(self) -> str:
        return "sentiment"

    def evaluate(
        self,
        symbol: str,
        df: pd.DataFrame,
        regime: str,
        timeframe: str,
    ) -> Optional[ModelSignal]:
        """
        Evaluate FinBERT sentiment signal.

        Parameters
        ----------
        symbol : str
            Trading pair (e.g., "BTC/USDT")
        df : pd.DataFrame
            OHLCV + indicator DataFrame
        regime : str
            Current regime
        timeframe : str
            Primary timeframe

        Returns
        -------
        ModelSignal or None
        """
        # Read tunable parameters from settings with fallback defaults
        _min_signal = float(_s.get('models.sentiment.min_signal', 0.35))
        _min_confidence = float(_s.get('models.sentiment.min_confidence', 0.55))
        _min_headlines = int(_s.get('models.sentiment.min_headlines', 3))
        _max_age_minutes = int(_s.get('models.sentiment.max_age_minutes', 480))
        _sl_atr_mult = float(_s.get('models.sentiment.sl_atr_mult', 1.5))
        _tp_atr_mult = float(_s.get('models.sentiment.tp_atr_mult', 2.5))

        # Derive the base asset ticker (e.g., "BTC" from "BTC/USDT")
        base_asset = symbol.split("/")[0] if "/" in symbol else symbol

        # ── Fetch and analyze headlines ────────────────────────
        news_feed = _get_news_feed(base_asset)
        finbert = _get_finbert_pipeline()

        if news_feed is None or finbert is None:
            return None
        try:
            headlines = news_feed.fetch_headlines(max_age_minutes=_max_age_minutes)
        except Exception as e:
            logger.debug(f"SentimentModel: failed to fetch headlines: {e}")
            return None

        if not headlines:
            return None

        # Extract titles
        titles = [h["title"] for h in headlines]

        # ── Run FinBERT aggregation ────────────────────────────
        try:
            aggregate = finbert.aggregate_sentiment(titles, max_texts=20)
        except Exception as e:
            logger.warning(f"SentimentModel: FinBERT aggregation failed: {e}")
            return None

        net_score = aggregate.get("net_score", 0.0)
        confidence = aggregate.get("confidence", 0.0)
        headline_count = aggregate.get("headline_count", 0)

        # ── Check signal thresholds ────────────────────────────
        if abs(net_score) <= _min_signal:
            return None
        if confidence <= _min_confidence:
            return None
        if headline_count < _min_headlines:
            return None

        # ── Determine direction ────────────────────────────────
        direction = "long" if net_score > 0 else "short"

        # ── Calculate strength ─────────────────────────────────
        # Sentiment is confirming, not primary → scale down
        strength = min(abs(net_score) * 1.2, 0.95)

        # ── Price levels (ATR-based) ───────────────────────────
        price = float(df["close"].iloc[-1])
        atr = self._atr(df)

        if direction == "long":
            entry = price
            stop_loss = price - _sl_atr_mult * atr
            take_profit = price + _tp_atr_mult * atr
        else:
            entry = price
            stop_loss = price + _sl_atr_mult * atr
            take_profit = price - _tp_atr_mult * atr

        # ── Rationale with headline snippets ───────────────────
        snippets = []
        for h in headlines[:3]:
            title = h["title"][:60]  # First 60 chars
            snippets.append(title)

        snippets_str = " | ".join(snippets)

        rationale = (
            f"FinBERT: {direction} | net_score={net_score:+.3f} | "
            f"confidence={confidence:.2f} | headlines={headline_count} | "
            f"regime={regime} | headlines: {snippets_str}"
        )

        # ── Create ModelSignal ─────────────────────────────────
        signal = ModelSignal(
            symbol=symbol,
            model_name=self.name,
            direction=direction,
            strength=round(strength, 4),
            entry_price=round(entry, 8),
            stop_loss=round(stop_loss, 8),
            take_profit=round(take_profit, 8),
            atr_value=round(atr, 8),
            timeframe=timeframe,
            regime=regime,
            rationale=rationale,
        )

        # ── Publish FINBERT_SIGNAL event ───────────────────────
        try:
            finbert_signal_data = {
                "symbol": symbol,
                "direction": direction,
                "net_score": round(net_score, 4),
                "headline_count": headline_count,
                "confidence": round(confidence, 4),
                "bullish_pct": round(aggregate.get("bullish_pct", 0.0), 2),
                "bearish_pct": round(aggregate.get("bearish_pct", 0.0), 2),
                "neutral_pct": round(aggregate.get("neutral_pct", 0.0), 2),
                "strength": round(strength, 4),
                "regime": regime,
                "timeframe": timeframe,
            }
            bus.publish(Topics.FINBERT_SIGNAL, finbert_signal_data, source="sentiment_model")
            logger.info(
                f"SentimentModel: published FINBERT_SIGNAL for {symbol} | "
                f"direction={direction} | net_score={net_score:+.3f}"
            )
        except Exception as e:
            logger.warning(f"SentimentModel: failed to publish FINBERT_SIGNAL: {e}")

        return signal
