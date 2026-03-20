# ============================================================
# NEXUS TRADER — Sentiment Engine
# Aggregates news + reddit → scores → DB → event bus
# ============================================================
from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Crypto-domain boosters applied on top of VADER
# ---------------------------------------------------------------------------
_CRYPTO_BOOST: dict[str, float] = {
    # Bullish signals
    "bull":         0.3,  "bullish":      0.35, "moon":         0.4,
    "mooning":      0.4,  "pump":         0.25, "surge":        0.3,
    "rally":        0.3,  "breakout":     0.3,  "ath":          0.4,
    "all-time high":0.4,  "accumulate":   0.2,  "buy":          0.15,
    "long":         0.15, "hodl":         0.2,  "adoption":     0.25,
    "institutional":0.2,  "etf approved": 0.5,  "partnership":  0.2,
    "upgrade":      0.2,  "mainnet":      0.2,  "launch":       0.15,
    # Bearish signals
    "bear":        -0.3,  "bearish":     -0.35, "dump":        -0.3,
    "crash":       -0.4,  "collapse":    -0.4,  "sell":        -0.15,
    "short":       -0.15, "scam":        -0.5,  "hack":        -0.5,
    "exploit":     -0.45, "rug":         -0.5,  "rug pull":    -0.55,
    "ban":         -0.4,  "banned":      -0.4,  "regulation":  -0.15,
    "sec":         -0.2,  "lawsuit":     -0.3,  "investigation":-0.25,
    "fraud":       -0.45, "bankruptcy":  -0.5,  "insolvent":   -0.5,
    "delisted":    -0.4,  "delist":      -0.35, "warning":     -0.2,
    "fud":         -0.15, "panic":       -0.3,  "fear":        -0.25,
    "correction":  -0.2,  "plunge":      -0.3,  "tumble":      -0.25,
}


def _load_vader():
    """Lazy load VADER analyser."""
    try:
        from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
        return SentimentIntensityAnalyzer()
    except ImportError:
        return None


_vader = None
_vader_lock = threading.Lock()


def _get_vader():
    global _vader
    if _vader is None:
        with _vader_lock:
            if _vader is None:
                _vader = _load_vader()
    return _vader


def score_text(text: str) -> float:
    """
    Score a piece of text → compound sentiment [-1.0, +1.0].
    Uses VADER + crypto-domain boosters.
    """
    if not text or not text.strip():
        return 0.0

    base = 0.0
    vader = _get_vader()
    if vader:
        scores = vader.polarity_scores(text)
        base = scores["compound"]

    # Apply crypto boosters
    text_lower = text.lower()
    boost = 0.0
    for kw, delta in _CRYPTO_BOOST.items():
        if kw in text_lower:
            boost += delta

    # Clamp boost contribution
    boost = max(-0.5, min(0.5, boost))
    combined = base * 0.7 + boost * 0.3
    return max(-1.0, min(1.0, combined))


def sentiment_label(score: float) -> tuple[str, str]:
    """Return (label, hex_color) for a sentiment score."""
    if score >= 0.35:
        return "Very Bullish", "#00CC77"
    elif score >= 0.10:
        return "Bullish", "#44BB66"
    elif score >= -0.10:
        return "Neutral", "#8899AA"
    elif score >= -0.35:
        return "Bearish", "#FF6633"
    else:
        return "Very Bearish", "#FF3355"


def score_color(score: float) -> str:
    return sentiment_label(score)[1]


# ---------------------------------------------------------------------------
# SentimentEngine — fetch, score, persist, publish
# ---------------------------------------------------------------------------
class SentimentEngine:
    """
    Fetches news + reddit, scores each item, aggregates per-symbol,
    persists to DB, and publishes SENTIMENT_UPDATED events.
    """

    def __init__(self):
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ #
    #  Public API                                                          #
    # ------------------------------------------------------------------ #

    def fetch_and_score(
        self,
        symbols: Optional[list[str]] = None,
        progress_cb=None,
    ) -> dict:
        """
        Full pipeline: fetch → score → persist → publish.
        Returns:
        {
            "articles": [scored_article_dict, ...],
            "posts":    [scored_post_dict, ...],
            "aggregate": {symbol: score, ..., "__overall__": score},
        }
        """
        from config.settings import settings

        articles: list[dict] = []
        posts:    list[dict] = []
        warnings: list[str]  = []

        # ── News ─────────────────────────────────────────────────────── #
        news_enabled = settings.get("sentiment.news_enabled", True)
        if news_enabled:
            if progress_cb:
                progress_cb("Fetching news articles…")
            try:
                from core.sentiment.news_fetcher import fetch_crypto_news
                from core.security.key_vault import key_vault
                api_key = key_vault.load("sentiment.news_api_key")
            except Exception:
                api_key = settings.get("sentiment.news_api_key", "")

            if symbols:
                for sym in symbols[:3]:          # cap to 3 symbols to save API calls
                    raw_sym = sym.replace("USDT", "").replace("BTC", "").replace("/", "")
                    fetched = fetch_crypto_news(api_key, symbol=raw_sym, page_size=15)
                    for a in fetched:
                        a["symbol"] = raw_sym
                    articles.extend(fetched)
            else:
                articles = fetch_crypto_news(api_key, page_size=40)

            if not articles:
                warnings.append("No news articles retrieved — check network or API key")

        # ── Reddit ───────────────────────────────────────────────────── #
        reddit_enabled = settings.get("sentiment.reddit_enabled", False)
        if reddit_enabled:
            if progress_cb:
                progress_cb("Fetching Reddit posts…")
            try:
                from core.security.key_vault import key_vault
                cid    = key_vault.load("sentiment.reddit_client_id")
                secret = key_vault.load("sentiment.reddit_client_secret")
            except Exception:
                cid    = settings.get("sentiment.reddit_client_id", "")
                secret = settings.get("sentiment.reddit_client_secret", "")

            if not cid or not secret:
                warnings.append("Reddit credentials not configured — go to Settings → Sentiment")
            else:
                from core.sentiment.reddit_fetcher import fetch_reddit_posts
                posts = fetch_reddit_posts(cid, secret, limit_per_sub=20)
                if not posts:
                    warnings.append("Reddit returned 0 posts — check credentials")

        # ── Score ────────────────────────────────────────────────────── #
        if progress_cb:
            progress_cb("Scoring articles…")
        for a in articles:
            text        = f"{a.get('title', '')} {a.get('description', '')}"
            a["score"]  = score_text(text)
            a["label"], a["color"] = sentiment_label(a["score"])

        for p in posts:
            text        = f"{p.get('title', '')} {p.get('text', '')}"
            p["score"]  = score_text(text)
            p["label"], p["color"] = sentiment_label(p["score"])

        # ── Aggregate ────────────────────────────────────────────────── #
        aggregate: dict[str, float] = {}
        all_scores = [a["score"] for a in articles] + [p["score"] for p in posts]
        aggregate["__overall__"] = (
            sum(all_scores) / len(all_scores) if all_scores else 0.0
        )

        # Per-symbol average
        if symbols:
            for sym in symbols:
                raw_sym = sym.replace("USDT", "").replace("BTC", "").replace("/", "")
                sym_scores = [
                    a["score"] for a in articles
                    if a.get("symbol", "").upper() == raw_sym.upper()
                ]
                if sym_scores:
                    aggregate[sym] = sum(sym_scores) / len(sym_scores)

        # ── Persist ──────────────────────────────────────────────────── #
        if progress_cb:
            progress_cb("Saving to database…")
        self._persist(articles, posts, aggregate)

        # ── Publish ──────────────────────────────────────────────────── #
        try:
            from core.event_bus import bus, Topics
            bus.publish(Topics.SENTIMENT_UPDATED, {
                "overall":  aggregate.get("__overall__", 0.0),
                "articles": len(articles),
                "posts":    len(posts),
                "aggregate": aggregate,
            })
        except Exception as exc:
            logger.debug("Sentiment publish failed: %s", exc)

        logger.info(
            "SentimentEngine: %d articles, %d posts, overall=%.3f",
            len(articles), len(posts), aggregate.get("__overall__", 0.0),
        )
        return {
            "articles":  articles,
            "posts":     posts,
            "aggregate": aggregate,
            "warnings":  warnings,
        }

    # ------------------------------------------------------------------ #
    #  DB persistence                                                      #
    # ------------------------------------------------------------------ #

    def _persist(
        self,
        articles: list[dict],
        posts: list[dict],
        aggregate: dict,
    ) -> None:
        try:
            from core.database.engine import get_session
            from core.database.models import SentimentData

            with get_session() as session:
                overall = aggregate.get("__overall__", 0.0)
                now     = datetime.now(timezone.utc)

                row = SentimentData(
                    source          = "aggregated",
                    timestamp       = now,
                    sentiment_score = overall,
                    narrative_score = None,
                    attention_index = float(len(articles) + len(posts)),
                    raw_data        = {
                        "article_count": len(articles),
                        "post_count":    len(posts),
                        "aggregate":     {
                            k: round(v, 4) for k, v in aggregate.items()
                        },
                    },
                )
                session.add(row)
                session.commit()
        except Exception as exc:
            logger.debug("Sentiment persist failed: %s", exc)


# ── Singleton ────────────────────────────────────────────── #
sentiment_engine = SentimentEngine()
