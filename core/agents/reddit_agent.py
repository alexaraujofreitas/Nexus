# ============================================================
# NEXUS TRADER — Reddit Sentiment Agent
#
# Monitors Reddit sentiment for crypto signals via:
#   1. Reddit JSON API (completely free, no OAuth required)
#   2. Tracks subreddits: r/Bitcoin, r/CryptoCurrency, r/CryptoMarkets
#   3. Analyzes post flairs, upvote ratios, comment volume
#   4. Detects "moon" vs "crash" discussion trends
#
# Publishes: Topics.REDDIT_SIGNAL
# Poll interval: 900s (15 minutes)
# ============================================================
from __future__ import annotations

import logging
import threading
import urllib.request
import urllib.error
import json as _json
from typing import Any

from core.agents.base_agent import BaseAgent
from core.event_bus import Topics
from core.ai.model_registry import get_model_registry

logger = logging.getLogger(__name__)

_POLL_SECONDS = 900  # 15 minutes

# Target subreddits
_SUBREDDITS = ["Bitcoin", "CryptoCurrency", "CryptoMarkets"]

# Keywords for sentiment analysis
_MOON_KEYWORDS = {"moon", "mooning", "bullish", "breakout", "ath", "accumulation", "hodl"}
_CRASH_KEYWORDS = {
    "crash", "dumped", "bearish", "distribution", "liquidation", "rekt",
    "fud", "panic", "sell", "decline", "capitulation",
}


class RedditSentimentAgent(BaseAgent):
    """
    Monitors Reddit sentiment for cryptocurrency market signals.

    Data sources:
      • Reddit JSON API (free, no auth required)
      • Hot posts from r/Bitcoin, r/CryptoCurrency, r/CryptoMarkets

    Metrics:
      • Post scores (upvotes)
      • Comment volume
      • Upvote ratio (consensus indicator)
      • Post flair analysis (Daily Discussion, DD, News, Analysis)
      • "Moon" vs "Crash" keyword detection
    """

    def __init__(self, parent=None):
        super().__init__("reddit", parent)
        self._lock = threading.RLock()
        self._last_posts_cache: list[dict] = []
        self._scorer = None

    @property
    def event_topic(self) -> str:
        return Topics.REDDIT_SIGNAL

    @property
    def poll_interval_seconds(self) -> int:
        return _POLL_SECONDS

    def fetch(self) -> dict:
        """Fetch hot posts from target subreddits."""
        raw: dict[str, Any] = {}

        for subreddit in _SUBREDDITS:
            try:
                data = self._fetch_subreddit_hot(subreddit)
                if data:
                    raw[subreddit.lower()] = data
            except Exception as exc:
                logger.debug("RedditAgent: %s fetch failed — %s", subreddit, exc)

        return raw

    def process(self, raw: dict) -> dict:
        """Convert raw Reddit data into normalized signal."""
        if not raw:
            return {
                "signal": 0.0,
                "confidence": 0.0,
                "sentiment_label": "neutral",
                "post_count": 0,
                "avg_upvotes": 0.0,
                "avg_comments": 0.0,
                "top_posts": [],
                "subreddits_checked": [],
            }

        all_posts: list[dict] = []
        subreddits_checked = []

        # Aggregate posts from all subreddits
        for subreddit, data in raw.items():
            subreddits_checked.append(subreddit)
            posts = data.get("posts", [])
            all_posts.extend(posts)

        if not all_posts:
            return {
                "signal": 0.0,
                "confidence": 0.0,
                "sentiment_label": "neutral",
                "post_count": 0,
                "avg_upvotes": 0.0,
                "avg_comments": 0.0,
                "top_posts": [],
                "subreddits_checked": subreddits_checked,
            }

        # Score posts for sentiment
        signals_and_posts: list[tuple[float, float, dict]] = []
        total_upvotes = 0
        total_comments = 0

        for post in all_posts[:50]:  # Limit to top 50 across all subs
            title = post.get("title", "")
            selftext = post.get("selftext", "")[:200]
            text_combined = f"{title} {selftext}"

            score = post.get("score", 0)
            comments = post.get("num_comments", 0)
            upvote_ratio = post.get("upvote_ratio", 0.5)
            flair = (post.get("link_flair_text") or "").lower()

            total_upvotes += score
            total_comments += comments

            # Base sentiment score
            sig, conf = self._score_text(text_combined)

            # Amplify by post score weight
            score_weight = min(score / 1000.0, 2.0)  # Cap at 2x
            sig = sig * (1.0 + score_weight * 0.3)

            # Upvote ratio adjustment
            if upvote_ratio > 0.95 and sig > 0:
                conf = min(conf + 0.2, 1.0)

            # Daily Discussion post (moderate, normal day)
            if "daily" in flair or "discussion" in flair:
                conf = max(conf * 0.8, 0.3)

            # DD (Due Diligence) bullish post viral
            if "dd" in flair and sig > 0.5 and score > 500:
                sig = min(sig + 0.2, 1.0)
                conf = min(conf * 1.2, 1.0)

            # Crash/FUD in top posts
            if any(kw in text_combined.lower() for kw in _CRASH_KEYWORDS):
                if score > 100:
                    sig = min(sig - 0.5, -0.5)
                    conf = min(conf * 1.2, 1.0)

            sig = max(-1.0, min(1.0, sig))

            signals_and_posts.append((sig, conf, post))

        # Compute aggregate
        if not signals_and_posts:
            agg_sig = 0.0
            agg_conf = 0.0
        else:
            total_conf = sum(c for _, c, _ in signals_and_posts)
            agg_sig = (
                sum(s * c for s, c, _ in signals_and_posts) / total_conf
                if total_conf > 0 else 0.0
            )
            agg_conf = total_conf / len(signals_and_posts)

        # Sentiment label
        sentiment_label = (
            "extremely_bullish" if agg_sig > 0.60 else
            "bullish" if agg_sig > 0.25 else
            "slightly_bullish" if agg_sig > 0.10 else
            "extremely_bearish" if agg_sig < -0.60 else
            "bearish" if agg_sig < -0.25 else
            "slightly_bearish" if agg_sig < -0.10 else
            "neutral"
        )

        # Top 5 posts
        sorted_posts = sorted(signals_and_posts, key=lambda x: x[1], reverse=True)
        top_posts = [
            {
                "title": p[2].get("title", "")[:100],
                "score": p[2].get("score", 0),
                "url": p[2].get("url", ""),
                "signal": round(p[0], 3),
                "confidence": round(p[1], 3),
            }
            for p in sorted_posts[:5]
        ]

        # Cache for internal use
        with self._lock:
            self._last_posts_cache = [p[2] for p in sorted_posts[:15]]

        avg_upvotes = total_upvotes / len(all_posts) if all_posts else 0.0
        avg_comments = total_comments / len(all_posts) if all_posts else 0.0

        logger.info(
            "RedditAgent: signal=%+.3f | conf=%.2f | label=%s | posts=%d | avg_upvotes=%.0f",
            agg_sig, agg_conf, sentiment_label, len(all_posts), avg_upvotes,
        )

        return {
            "signal": round(agg_sig, 4),
            "confidence": round(agg_conf, 4),
            "sentiment_label": sentiment_label,
            "post_count": len(all_posts),
            "avg_upvotes": round(avg_upvotes, 1),
            "avg_comments": round(avg_comments, 1),
            "top_posts": top_posts,
            "subreddits_checked": subreddits_checked,
        }

    # ── Data fetchers ──────────────────────────────────────

    def _fetch_subreddit_hot(self, subreddit: str) -> dict | None:
        """
        Fetch hot posts from a subreddit using JSON API.
        Completely free, no OAuth required.
        """
        try:
            url = f"https://www.reddit.com/r/{subreddit}/hot.json?limit=25"
            req = urllib.request.Request(
                url,
                headers={"User-Agent": "NexusTrader/1.0 (research bot)"},
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = _json.loads(resp.read().decode())

            posts_data = data.get("data", {}).get("children", [])
            posts = [
                {
                    "title":          p["data"].get("title") or "",
                    "selftext":       p["data"].get("selftext") or "",
                    "score":          p["data"].get("score", 0),
                    "num_comments":   p["data"].get("num_comments", 0),
                    "upvote_ratio":   p["data"].get("upvote_ratio", 0.5),
                    "link_flair_text": p["data"].get("link_flair_text") or "",
                    "url":            p["data"].get("url") or "",
                    "created_utc":    p["data"].get("created_utc", 0),
                }
                for p in posts_data
                if p["kind"] == "t3"
            ]

            logger.debug(
                "RedditAgent: %s fetch succeeded (%s posts)", subreddit, len(posts)
            )
            return {"posts": posts}
        except Exception as exc:
            logger.debug("RedditAgent: %s fetch failed — %s", subreddit, exc)
            return None

    # ── Helpers ────────────────────────────────────────────

    def _score_text(self, text: str) -> tuple[float, float]:
        """Score text sentiment using ModelRegistry scorer."""
        if not self._scorer:
            try:
                self._scorer = get_model_registry().get_scorer("reddit")
            except Exception:
                from core.ai.model_registry import _VaderScorer
                self._scorer = _VaderScorer()

        try:
            results = self._scorer.score([text])
            if results:
                sig, conf = results[0]
                return float(sig), float(conf)
        except Exception:
            pass

        return 0.0, 0.0


# ── Module-level singleton ────────────────────────────────────
reddit_agent: RedditSentimentAgent | None = None
