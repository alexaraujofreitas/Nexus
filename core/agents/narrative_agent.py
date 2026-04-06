# ============================================================
# NEXUS TRADER — Narrative Shift Detection Agent
#
# Monitors crypto narrative changes across news sources using
# CryptoCompare, Messari, and CoinGecko data (all free APIs).
#
# Tracks 10 major crypto narratives and detects shifts that
# could indicate major regime changes in market sentiment.
#
# Signal logic:
#   Narrative shift detected + bullish new narrative     → +0.6
#   Narrative shift detected + bearish new narrative     → -0.7
#   Same narrative, strengthening in articles            → mild signal (+/- 0.2-0.3)
#   hack_exploit or regulatory dominant                  → strong bearish -0.75
#
# Returns: signal, confidence, dominant_narrative, previous_narrative,
#   narrative_shift_score, narrative_sentiment, top_articles, article_count
#
# Publishes: Topics.NARRATIVE_SHIFT
# ============================================================
from __future__ import annotations

import json
import logging
import urllib.request
from typing import Any
from urllib.error import URLError

from core.agents.base_agent import BaseAgent
from core.ai.model_registry import get_model_registry
from core.event_bus import Topics
from core.scanning.watchlist_gate import get_api_call_counter

logger = logging.getLogger(__name__)

_POLL_SECONDS = 1800  # 30 minutes

_NARRATIVES = {
    "btc_etf": ["etf", "spot etf", "blackrock", "fidelity", "institutional"],
    "defi_summer": ["defi", "yield", "liquidity mining", "tvl", "protocol"],
    "layer2": ["layer 2", "l2", "rollup", "base", "arbitrum", "optimism", "zk"],
    "rwa": ["real world asset", "rwa", "tokenization", "treasury", "bonds"],
    "ai_crypto": ["ai token", "artificial intelligence", "compute", "render", "fetch"],
    "meme_season": ["meme coin", "doge", "shib", "pepe", "wif", "bonk"],
    "regulatory": ["sec", "regulation", "ban", "cftc", "enforcement", "legal"],
    "macro_risk": ["recession", "fed rate", "inflation", "dollar", "treasury yield"],
    "hack_exploit": ["hack", "exploit", "bridge attack", "rug pull", "exit scam"],
    "btc_halving": ["halving", "halvening", "block reward", "mining reward", "supply"],
}

_BULLISH_NARRATIVES = {"btc_etf", "defi_summer", "layer2", "rwa", "ai_crypto"}
_BEARISH_NARRATIVES = {"regulatory", "macro_risk", "hack_exploit"}


class NarrativeShiftAgent(BaseAgent):
    """
    Monitors crypto narrative shifts across news sources.

    Detects when major narratives (ETF hype, DeFi, regulation, etc.)
    gain or lose dominance in the media landscape, which can signal
    sentiment regime changes.
    """

    def __init__(self, parent=None):
        super().__init__("narrative_shift", parent)
        self._previous_dominant: str | None = None
        self._cache: dict = {}
        self._scorer = None

    # ── BaseAgent interface ────────────────────────────────────

    @property
    def event_topic(self) -> str:
        return Topics.NARRATIVE_SHIFT

    @property
    def poll_interval_seconds(self) -> int:
        return _POLL_SECONDS

    def on_settings_changed(self) -> None:
        """Reload model registry on settings change."""
        try:
            self._scorer = None  # Force reload
            self._get_scorer()
        except Exception as exc:
            logger.debug("NarrativeShiftAgent: settings update error: %s", exc)

    def fetch(self) -> dict[str, Any]:
        """Fetch news articles from CryptoCompare, Messari, and CoinGecko."""
        articles = []

        # CryptoCompare news
        try:
            cc_articles = self._fetch_cryptocompare_news()
            if cc_articles:
                articles.extend(cc_articles)
                get_api_call_counter().record("cryptocompare")
        except Exception as exc:
            logger.debug("NarrativeShiftAgent: CryptoCompare fetch failed — %s", exc)

        # Messari news
        try:
            msg_articles = self._fetch_messari_news()
            if msg_articles:
                articles.extend(msg_articles)
                get_api_call_counter().record("messari")
        except Exception as exc:
            logger.debug("NarrativeShiftAgent: Messari fetch failed — %s", exc)

        # CoinGecko trending
        try:
            trend_articles = self._fetch_coingecko_trending()
            if trend_articles:
                articles.extend(trend_articles)
                get_api_call_counter().record("coingecko")
        except Exception as exc:
            logger.debug("NarrativeShiftAgent: CoinGecko trending fetch failed — %s", exc)

        return {"articles": articles, "count": len(articles)}

    def process(self, raw: dict[str, Any]) -> dict:
        """
        Analyze narrative themes in articles and detect shifts.
        Returns signal, confidence, narrative metadata.
        """
        articles = raw.get("articles", [])
        article_count = len(articles)

        if not articles:
            return {
                "signal": 0.0,
                "confidence": 0.0,
                "has_data": False,
                "dominant_narrative": None,
                "previous_narrative": self._previous_dominant,
                "narrative_shift_score": 0.0,
                "narrative_sentiment": 0.0,
                "top_articles": [],
                "article_count": 0,
            }

        # Count narrative mentions
        narrative_counts = self._count_narratives(articles)

        # Find dominant narrative
        dominant = max(narrative_counts, key=narrative_counts.get) if narrative_counts else None
        previous = self._previous_dominant

        # Compute narrative shift score
        shift_score = self._compute_shift_score(dominant, previous, narrative_counts)

        # Compute sentiment of dominant narrative
        dominant_sentiment = self._compute_narrative_sentiment(dominant, articles)

        # Compute signal
        signal, confidence = self._compute_signal(
            dominant, previous, shift_score, dominant_sentiment
        )

        # Extract top 5 articles
        top_articles = [
            article.get("title", "") for article in articles[:5]
        ]

        # Update cache
        self._previous_dominant = dominant
        self._cache = {
            "dominant_narrative": dominant,
            "previous_narrative": previous,
            "narrative_counts": narrative_counts,
            "shift_score": shift_score,
            "sentiment": dominant_sentiment,
            "signal": signal,
            "confidence": confidence,
        }

        logger.info(
            "NarrativeShiftAgent: dominant=%s prev=%s shift=%.2f sentiment=%.2f signal=%.3f",
            dominant, previous, shift_score, dominant_sentiment, signal,
        )

        return {
            "signal": round(signal, 4),
            "confidence": round(confidence, 4),
            "has_data": True,
            "dominant_narrative": dominant,
            "previous_narrative": previous,
            "narrative_shift_score": round(shift_score, 4),
            "narrative_sentiment": round(dominant_sentiment, 4),
            "top_articles": top_articles,
            "article_count": article_count,
        }

    # ── Narrative detection ────────────────────────────────────

    @staticmethod
    def _count_narratives(articles: list[dict]) -> dict[str, int]:
        """Count how many articles mention each narrative theme."""
        counts = {theme: 0 for theme in _NARRATIVES}

        for article in articles:
            title = (article.get("title", "") or "").lower()
            body = (article.get("body", "") or "").lower()
            text = f"{title} {body}"

            for theme, keywords in _NARRATIVES.items():
                for keyword in keywords:
                    if keyword.lower() in text:
                        counts[theme] += 1
                        break  # Count theme once per article

        return counts

    def _compute_shift_score(
        self, dominant: str | None, previous: str | None, counts: dict[str, int]
    ) -> float:
        """
        Compute narrative shift score (0 = no shift, 1 = complete shift).
        Considers both narrative change and mention count acceleration.
        """
        if not dominant or not previous:
            return 0.0

        if dominant == previous:
            # Same narrative: check if strengthening
            # If mention count increased, small shift
            return 0.0

        # Different narrative: measure magnitude of shift
        # Simple binary: different theme = significant shift
        return 0.7

    def _compute_narrative_sentiment(self, narrative: str | None, articles: list[dict]) -> float:
        """
        Score the sentiment of the dominant narrative.
        Uses ML scorer if available, otherwise uses heuristic.
        """
        if not narrative or not articles:
            return 0.0

        try:
            scorer = self._get_scorer()
            if scorer:
                # Score top articles mentioning this narrative
                theme_articles = []
                for article in articles:
                    text = f"{article.get('title', '')} {article.get('body', '')}"
                    if any(kw in text.lower() for kw in _NARRATIVES.get(narrative, [])):
                        theme_articles.append(text)

                if theme_articles:
                    # scorer.score() accepts list[str], returns list[tuple[float, float]]
                    # Do NOT call scorer(text) — _FinBERTScorer is not callable directly.
                    results = scorer.score(theme_articles[:10])
                    signals = [sig for sig, _conf in results]
                    return sum(signals) / len(signals) if signals else 0.0
        except Exception as exc:
            logger.debug("NarrativeShiftAgent: scorer failed — %s", exc)

        # Fallback: heuristic based on narrative type
        if narrative in _BULLISH_NARRATIVES:
            return 0.5
        elif narrative in _BEARISH_NARRATIVES:
            return -0.5
        else:
            return 0.0

    def _compute_signal(
        self, dominant: str | None, previous: str | None, shift_score: float, sentiment: float
    ) -> tuple[float, float]:
        """
        Compute trading signal based on narrative shift and sentiment.
        Returns (signal, confidence).
        """
        signal = 0.0
        confidence = 0.5

        if not dominant:
            return 0.0, 0.2

        # Strong bearish narratives override everything
        if dominant in {"hack_exploit", "regulatory"}:
            return -0.75, 0.80

        # Narrative shift detected
        if shift_score > 0.4 and dominant != previous:
            if dominant in _BULLISH_NARRATIVES:
                signal = 0.6
                confidence = 0.75
            elif dominant in _BEARISH_NARRATIVES:
                signal = -0.7
                confidence = 0.75
            else:
                signal = sentiment * 0.4
                confidence = 0.60
        else:
            # Same narrative: mild signal if strengthening
            if sentiment > 0.3:
                signal = 0.2
                confidence = 0.40
            elif sentiment < -0.3:
                signal = -0.2
                confidence = 0.40

        return round(signal, 4), round(confidence, 4)

    # ── API methods ────────────────────────────────────────────

    @staticmethod
    def _fetch_cryptocompare_news() -> list[dict] | None:
        """Fetch latest 50 articles from CryptoCompare news feed."""
        url = (
            "https://min-api.cryptocompare.com/data/v2/news/"
            "?lang=EN&feeds=cointelegraph,coindesk,decrypt&limit=50"
        )

        try:
            with urllib.request.urlopen(url, timeout=10) as response:
                text = response.read().decode("utf-8")
                data = json.loads(text)

            articles = []
            for item in data.get("Data", []):
                articles.append({
                    "title": item.get("title", ""),
                    "body": item.get("body", ""),
                    "source": item.get("source", ""),
                    "url": item.get("url", ""),
                })

            return articles
        except (URLError, json.JSONDecodeError, KeyError, ValueError) as exc:
            logger.debug("NarrativeShiftAgent: CryptoCompare news fetch failed — %s", exc)
            return None

    @staticmethod
    def _fetch_messari_news() -> list[dict] | None:
        """Fetch latest 20 articles from Messari free API."""
        url = "https://data.messari.io/api/v1/news?sort=published_at&limit=20"

        try:
            with urllib.request.urlopen(url, timeout=10) as response:
                text = response.read().decode("utf-8")
                data = json.loads(text)

            articles = []
            for item in data.get("data", []):
                articles.append({
                    "title": item.get("title", ""),
                    "body": item.get("content", ""),
                    "source": "Messari",
                    "url": item.get("id", ""),
                })

            return articles
        except (URLError, json.JSONDecodeError, KeyError, ValueError) as exc:
            logger.debug("NarrativeShiftAgent: Messari news fetch failed — %s", exc)
            return None

    @staticmethod
    def _fetch_coingecko_trending() -> list[dict] | None:
        """Fetch trending searches from CoinGecko to infer narrative momentum."""
        url = "https://api.coingecko.com/api/v3/search/trending"

        try:
            with urllib.request.urlopen(url, timeout=10) as response:
                text = response.read().decode("utf-8")
                data = json.loads(text)

            articles = []
            for item in data.get("coins", []):
                coin = item.get("item", {})
                articles.append({
                    "title": f"{coin.get('name', '')} trending",
                    "body": coin.get('symbol', ''),
                    "source": "CoinGecko Trending",
                    "url": coin.get('id', ''),
                })

            return articles
        except (URLError, json.JSONDecodeError, KeyError, ValueError) as exc:
            logger.debug("NarrativeShiftAgent: CoinGecko trending fetch failed — %s", exc)
            return None

    def _get_scorer(self):
        """Lazily load the narrative sentiment scorer."""
        if self._scorer is None:
            try:
                registry = get_model_registry()
                self._scorer = registry.get_scorer("narrative_shift")
            except Exception as exc:
                logger.debug("NarrativeShiftAgent: failed to load scorer — %s", exc)
                self._scorer = None

        return self._scorer

    def get_narrative_state(self) -> dict:
        """Return cached narrative state."""
        return dict(self._cache) if self._cache else {}


# ── Module-level singleton (initialised by AgentCoordinator) ──
narrative_agent: NarrativeShiftAgent | None = None
