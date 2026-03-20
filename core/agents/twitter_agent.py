# ============================================================
# NEXUS TRADER — Twitter Sentiment Agent
#
# Monitors Twitter/X sentiment for crypto signals via:
#   1. Nitter RSS feeds (public Twitter mirror, no auth)
#   2. CryptoPanic free API (public posts)
#   3. Fallback to Alternative.me FNG + CoinGecko trending
#   4. Detects major influencer posts with sentiment boost
#
# Publishes: Topics.TWITTER_SIGNAL
#            Topics.INFLUENCER_ALERT (when influencers detected)
# Poll interval: 600s (10 minutes) — faster cadence for Twitter
# ============================================================
from __future__ import annotations

import logging
import threading
import urllib.request
import urllib.error
import json as _json
import re
from typing import Any
from datetime import datetime, timezone

from core.agents.base_agent import BaseAgent
from core.event_bus import bus, Topics
from core.ai.model_registry import get_model_registry

logger = logging.getLogger(__name__)

_POLL_SECONDS = 600  # 10 minutes

# Known crypto influencer handles (lowercase)
_INFLUENCERS = {
    "elonmusk": "Elon Musk",
    "saylor": "Michael Saylor (MicroStrategy)",
    "cz_binance": "CZ (Binance)",
    "vitalikbuterin": "Vitalik Buterin",
    "aantonop": "Andreas M. Antonopoulos",
    "planb": "Plan B",
    "woonomic": "Willy Woo",
    "glassnode": "Glassnode",
    "cryptoquant": "CryptoQuant",
    "whale_alert": "Whale Alert",
}

# Keywords for viral negative events
_NEGATIVE_KEYWORDS = {
    "hack", "hacked", "exploit", "stolen", "breach", "fraud",
    "crashed", "crash", "collapse", "bankruptcy", "liquidation",
    "sec", "ban", "banned", "regulatory", "regulation",
}

# Keywords for volume/spike detection
_VOLUME_KEYWORDS = {
    "surge", "spike", "surge", "rally", "soaring", "exploded",
    "crashed", "tanked", "plummeted", "dumped",
}


class TwitterSentimentAgent(BaseAgent):
    """
    Monitors Twitter sentiment for cryptocurrency market signals.

    Data sources:
      1. Nitter RSS feeds (multiple instances tried for resilience)
      2. CryptoPanic free API
      3. Fallback to cached social data + FNG

    Detects influencer activity and publishes separate INFLUENCER_ALERT events.
    """

    def __init__(self, parent=None):
        super().__init__("twitter", parent)
        self._lock = threading.RLock()
        self._last_posts_cache: list[dict] = []
        self._scorer = None

    @property
    def event_topic(self) -> str:
        return Topics.TWITTER_SIGNAL

    @property
    def poll_interval_seconds(self) -> int:
        return _POLL_SECONDS

    def fetch(self) -> dict:
        """Fetch tweets from multiple fallback sources."""
        raw: dict[str, Any] = {}

        # Try Nitter RSS feeds (multiple instances)
        nitter_data = self._fetch_nitter_rss()
        if nitter_data:
            raw["nitter"] = nitter_data
        else:
            # Fallback to CryptoPanic
            try:
                cp_data = self._fetch_cryptopanic()
                if cp_data:
                    raw["cryptopanic"] = cp_data
            except Exception as exc:
                logger.debug("TwitterAgent: CryptoPanic fetch failed — %s", exc)

            # Final fallback: use cached data
            if not raw:
                try:
                    raw["cached"] = self._fetch_cached_sentiment()
                except Exception as exc:
                    logger.debug("TwitterAgent: cached fetch failed — %s", exc)

        return raw

    def process(self, raw: dict) -> dict:
        """Convert raw Twitter data into normalized signal."""
        if not raw:
            return {
                "signal": 0.0,
                "confidence": 0.0,
                "sentiment_label": "neutral",
                "mention_volume": 0,
                "top_posts": [],
                "influencers_detected": [],
                "trending_hashtags": [],
            }

        posts: list[dict] = []
        influencers: set[str] = set()
        hashtags: set[str] = set()

        # Extract posts from all sources
        if "nitter" in raw:
            posts.extend(raw["nitter"].get("posts", []))
            hashtags.update(raw["nitter"].get("hashtags", []))

        if "cryptopanic" in raw:
            posts.extend(raw["cryptopanic"].get("posts", []))

        if "cached" in raw:
            posts.extend(raw["cached"].get("posts", []))

        if not posts:
            return {
                "signal": 0.0,
                "confidence": 0.0,
                "sentiment_label": "neutral",
                "mention_volume": 0,
                "top_posts": [],
                "influencers_detected": [],
                "trending_hashtags": [],
            }

        # Score posts for sentiment
        signals_and_confs: list[tuple[float, float, dict]] = []
        for post in posts:
            text = post.get("text", "")
            author = post.get("author", "").lower()

            # Check for influencer
            if author in _INFLUENCERS:
                influencers.add(author)

            # Score sentiment
            sig, conf = self._score_text(text)

            # Boost confidence if influencer
            if author in _INFLUENCERS:
                conf = min(conf + 0.2, 1.0)

            # Amplify for viral negative event
            if any(kw in text.lower() for kw in _NEGATIVE_KEYWORDS):
                sig = min(sig - 0.8, -0.8)
                conf = min(conf * 1.3, 1.0)

            signals_and_confs.append((sig, conf, post))

        # Compute aggregate
        if not signals_and_confs:
            agg_sig = 0.0
            agg_conf = 0.0
        else:
            total_conf = sum(c for _, c, _ in signals_and_confs)
            agg_sig = (
                sum(s * c for s, c, _ in signals_and_confs) / total_conf
                if total_conf > 0 else 0.0
            )
            agg_conf = total_conf / len(signals_and_confs)

        # Check for volume spike
        mention_volume = len(posts)
        if mention_volume > 50:
            agg_sig *= 1.3
            agg_conf = min(agg_conf * 1.2, 1.0)

        # Clamp signal
        agg_sig = max(-1.0, min(1.0, agg_sig))

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

        # Top 3 posts
        sorted_posts = sorted(
            signals_and_confs, key=lambda x: x[1], reverse=True
        )
        top_posts = [
            {
                "text": p[2].get("text", "")[:200],
                "author": p[2].get("author", ""),
                "url": p[2].get("url", ""),
                "signal": round(p[0], 3),
                "confidence": round(p[1], 3),
            }
            for p in sorted_posts[:3]
        ]

        # Cache for internal use
        with self._lock:
            self._last_posts_cache = [p[2] for p in sorted_posts[:10]]

        # Publish influencer alerts
        for influencer in influencers:
            try:
                # Find posts by this influencer
                inf_posts = [p for p in posts if p.get("author", "").lower() == influencer]
                for post in inf_posts[:1]:  # Top post from influencer
                    inf_sig, inf_conf = self._score_text(post.get("text", ""))
                    bus.publish(
                        Topics.INFLUENCER_ALERT,
                        {
                            "handle": influencer,
                            "name": _INFLUENCERS.get(influencer, influencer),
                            "text": post.get("text", "")[:200],
                            "url": post.get("url", ""),
                            "signal": round(inf_sig, 3),
                            "confidence": round(inf_conf, 3),
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                        },
                        source="twitter",
                    )
            except Exception as exc:
                logger.debug("TwitterAgent: influencer alert failed — %s", exc)

        logger.info(
            "TwitterAgent: signal=%+.3f | conf=%.2f | label=%s | influencers=%s",
            agg_sig, agg_conf, sentiment_label, list(influencers),
        )

        return {
            "signal": round(agg_sig, 4),
            "confidence": round(agg_conf, 4),
            "sentiment_label": sentiment_label,
            "mention_volume": mention_volume,
            "top_posts": top_posts,
            "influencers_detected": sorted(list(influencers)),
            "trending_hashtags": sorted(list(hashtags))[:10],
        }

    # ── Data fetchers ──────────────────────────────────────

    def _fetch_nitter_rss(self) -> dict | None:
        """Try multiple Nitter instances for RSS feed of BTC/crypto tweets."""
        nitter_instances = [
            "https://nitter.net",
            "https://nitter.privacyredirect.com",
            "https://nitter.fdn.fr",
            "https://twitter.tokhmi.xyz",
        ]

        for instance in nitter_instances:
            try:
                url = f"{instance}/search/rss?q=%23bitcoin+OR+%23BTC+OR+%23crypto&f=tweets"
                # urllib.request.Request does NOT accept a timeout parameter —
                # it must be passed to urlopen() instead.
                req = urllib.request.Request(
                    url,
                    headers={"User-Agent": "NexusTrader/1.0"},
                )
                with urllib.request.urlopen(req, timeout=8) as resp:
                    content = resp.read().decode("utf-8", errors="ignore")

                # Simple RSS parsing
                posts = self._parse_rss_content(content)
                if posts:
                    hashtags = self._extract_hashtags(content)
                    logger.debug(
                        "TwitterAgent: Nitter fetch succeeded (%s posts)", len(posts)
                    )
                    return {"posts": posts, "hashtags": hashtags}
            except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError):
                logger.debug("TwitterAgent: Nitter instance %s failed", instance)
                continue
            except Exception as exc:
                logger.debug("TwitterAgent: Nitter parse error — %s", exc)
                continue

        return None

    def _fetch_cryptopanic(self) -> dict | None:
        """Fetch public posts from CryptoPanic API using the configured API key."""
        try:
            # Resolve API key from settings, falling back to the key vault if the
            # settings value is still the __vault__ migration placeholder.
            # CryptoPanic deprecated the free 'auth_token=free' token — a real key
            # is now required for all requests.
            from config.settings import settings as _settings
            api_key = _settings.get("agents.cryptopanic_api_key", "")
            if not api_key or api_key == "__vault__":
                try:
                    from core.security.key_vault import key_vault
                    api_key = key_vault.load("agents.cryptopanic_api_key") or ""
                except Exception:
                    api_key = ""
            if not api_key or api_key == "__vault__":
                logger.debug("TwitterAgent: CryptoPanic skipped — no API key configured")
                return None

            # CryptoPanic API v2 — base endpoint changed from /api/v1/ to /api/developer/v2/
            # filter=hot still valid (sentiment filter); public=true for non-personalised posts
            url = f"https://cryptopanic.com/api/developer/v2/posts/?auth_token={api_key}&filter=hot&currencies=BTC,ETH&public=true"
            req = urllib.request.Request(
                url,
                headers={"User-Agent": "NexusTrader/1.0"},
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = _json.loads(resp.read().decode())

            posts_data = data.get("results", [])
            posts = [
                {
                    "text": p.get("title", ""),
                    "author": p.get("source", {}).get("title", "cryptopanic"),
                    "url": p.get("url", ""),
                }
                for p in posts_data[:30]
            ]
            logger.debug("TwitterAgent: CryptoPanic fetch succeeded (%s posts)", len(posts))
            return {"posts": posts}
        except Exception as exc:
            logger.debug("TwitterAgent: CryptoPanic fetch failed — %s", exc)
            return None

    def _fetch_cached_sentiment(self) -> dict:
        """Fallback: use Alternative.me FNG + CoinGecko trending as proxy."""
        try:
            from core.agents.social_sentiment_agent import social_sentiment_agent
            if social_sentiment_agent:
                sig_dict = social_sentiment_agent.get_sentiment_signal()
                # Convert FNG + trending into synthetic "posts"
                posts = [
                    {
                        "text": f"Market sentiment: {sig_dict.get('sentiment_label', 'neutral')}",
                        "author": "cached_sentiment",
                        "url": "",
                    }
                ]
                return {"posts": posts}
        except Exception:
            pass

        return {"posts": []}

    # ── Helpers ────────────────────────────────────────────

    def _score_text(self, text: str) -> tuple[float, float]:
        """Score text sentiment using ModelRegistry scorer."""
        if not self._scorer:
            try:
                self._scorer = get_model_registry().get_scorer("twitter")
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

    def _parse_rss_content(self, rss_xml: str) -> list[dict]:
        """Extract posts from RSS XML (basic parsing)."""
        posts = []
        try:
            # Extract <item> blocks
            items = re.findall(r"<item>(.*?)</item>", rss_xml, re.DOTALL)
            for item in items[:30]:
                title_match = re.search(r"<title>(.*?)</title>", item)
                author_match = re.search(r"<author>(.*?)</author>", item)
                link_match = re.search(r"<link>(.*?)</link>", item)

                if title_match:
                    posts.append(
                        {
                            "text": title_match.group(1),
                            "author": (
                                author_match.group(1)
                                if author_match
                                else "twitter_user"
                            ),
                            "url": link_match.group(1) if link_match else "",
                        }
                    )
        except Exception:
            pass

        return posts

    def _extract_hashtags(self, content: str) -> list[str]:
        """Extract hashtags from content."""
        hashtags = re.findall(r"#\w+", content)
        return list(set(hashtags))[:20]


# ── Module-level singleton ────────────────────────────────────
twitter_agent: TwitterSentimentAgent | None = None
