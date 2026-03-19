# ============================================================
# NEXUS TRADER — Social Sentiment Agent  (Sprint 5)
#
# Aggregates crypto social sentiment from free public sources:
#
#   1. Alternative.me Fear & Greed Index (already in MacroAgent)
#      — reused here for per-cycle freshness
#
#   2. CoinGecko trending coins
#      Strong narrative momentum → rotation signal
#
#   3. Crypto-specific heuristic from price momentum divergence
#      Price +5% but volume flat → distribution, bearish bias
#      Price -5% but volume spikes → capitulation, bullish bias
#
#   4. (Optional) LunarCrush v4 free tier
#      Requires LUNARCRUSH_API_KEY in settings.
#      Galaxy score > 60 → bullish; < 40 → bearish
#
#   5. (NEW) Integrated signals from specialized social agents:
#      - TwitterAgent, RedditAgent, TelegramAgent
#      Contributes to overall sentiment aggregate
#
# Signal convention:
#   +1.0 = extreme bullish sentiment (contrarian: may be overheated)
#   -1.0 = extreme bearish sentiment (contrarian: may be capitulation)
#   Signal is DIRECTIONAL (not purely contrarian — context matters)
#
# Publishes: Topics.SOCIAL_SIGNAL
# Poll interval: 1800s (30 minutes) — social data moves faster than macro
# ============================================================
from __future__ import annotations

import logging
import threading
from typing import Any

from core.agents.base_agent import BaseAgent
from core.event_bus import Topics

logger = logging.getLogger(__name__)

_POLL_SECONDS = 1800  # 30 minutes

# CoinGecko trending threshold — how many of our watchlist appear in trending
_TRENDING_STRONG = 3
_TRENDING_MILD   = 1

# LunarCrush galaxy score thresholds
_LUNARCRUSH_BULLISH   = 60
_LUNARCRUSH_BEARISH   = 40


class SocialSentimentAgent(BaseAgent):
    """
    Monitors crypto social sentiment from public APIs and specialized social agents.

    Designed around zero-cost free-tier data sources with optional
    LunarCrush integration when an API key is provided, plus signals
    from Twitter, Reddit, and Telegram agents.
    """

    def __init__(self, parent=None):
        super().__init__("social_sentiment", parent)
        self._lock = threading.RLock()
        self._cache: dict[str, dict] = {}  # symbol → signal dict
        self._aggregate: dict = {}
        self._scorer = None

    # ── ModelRegistry scorer ───────────────────────────────────

    def _ensure_scorer(self):
        """Lazy-load the social sentiment scorer from ModelRegistry."""
        if self._scorer is None:
            try:
                from core.ml.model_registry import get_model_registry
                registry = get_model_registry()
                self._scorer = registry.get_scorer("social_sentiment")
            except Exception as exc:
                logger.debug("SocialSentimentAgent: Could not load scorer — %s", exc)
                self._scorer = False  # Mark as tried but unavailable

        return self._scorer if self._scorer is not False else None

    # ── BaseAgent interface ────────────────────────────────────

    @property
    def event_topic(self) -> str:
        return Topics.SOCIAL_SIGNAL

    @property
    def poll_interval_seconds(self) -> int:
        return _POLL_SECONDS

    def fetch(self) -> dict:
        raw: dict[str, Any] = {}

        # Fear & Greed (fresh copy — fast endpoint)
        try:
            raw["fng"] = self._fetch_fear_greed()
        except Exception as exc:
            logger.warning("SocialSentimentAgent: FNG fetch failed — %s", exc)

        # CoinGecko trending
        try:
            raw["trending"] = self._fetch_coingecko_trending()
        except Exception as exc:
            logger.warning("SocialSentimentAgent: CoinGecko trending failed — %s", exc)

        # CoinGecko global market data (dominance etc.)
        try:
            raw["global"] = self._fetch_coingecko_global()
        except Exception as exc:
            logger.debug("SocialSentimentAgent: CoinGecko global failed — %s", exc)

        # LunarCrush (optional)
        try:
            raw["lunarcrush"] = self._fetch_lunarcrush()
        except Exception as exc:
            logger.debug("SocialSentimentAgent: LunarCrush skipped — %s", exc)

        # Import signals from specialized social agents
        try:
            import core.agents.twitter_agent as _tw
            if _tw.twitter_agent and hasattr(_tw.twitter_agent, 'last_signal'):
                raw["twitter"] = _tw.twitter_agent.last_signal
        except Exception:
            pass

        try:
            import core.agents.reddit_agent as _rd
            if _rd.reddit_agent and hasattr(_rd.reddit_agent, 'last_signal'):
                raw["reddit"] = _rd.reddit_agent.last_signal
        except Exception:
            pass

        try:
            import core.agents.telegram_agent as _tg
            if _tg.telegram_agent and hasattr(_tg.telegram_agent, 'last_signal'):
                raw["telegram"] = _tg.telegram_agent.last_signal
        except Exception:
            pass

        return raw

    def process(self, raw: dict) -> dict:
        if not raw:
            return {
                "signal": 0.0,
                "confidence": 0.0,
                "sentiment_label": "neutral",
                "components": {},
            }

        components: dict[str, Any] = {}
        weighted: list[tuple[float, float, float]] = []  # (signal, conf, weight)

        # ── Fear & Greed ─────────────────────────────────────
        if "fng" in raw:
            fng_val = raw["fng"].get("value", 50)
            fng_sig, fng_conf = self._score_fng(fng_val)
            components["fear_greed"] = {
                "value": fng_val,
                "label": raw["fng"].get("value_classification", "Neutral"),
                "signal": round(fng_sig, 4),
            }
            weighted.append((fng_sig, fng_conf, 1.0))

        # ── CoinGecko Trending ────────────────────────────────
        if "trending" in raw:
            trending_coins  = raw["trending"]
            trend_sig, trend_conf = self._score_trending(trending_coins)
            components["trending"] = {
                "coins": trending_coins[:5],
                "count_in_watchlist": self._count_watchlist_overlap(trending_coins),
                "signal": round(trend_sig, 4),
            }
            weighted.append((trend_sig, trend_conf, 0.6))

        # ── CoinGecko Global ──────────────────────────────────
        if "global" in raw and raw["global"]:
            glb = raw["global"]
            dom_sig, dom_conf = self._score_btc_dominance(glb)
            components["btc_dominance"] = {
                "value": round(glb.get("btc_dominance", 50), 2),
                "signal": round(dom_sig, 4),
            }
            weighted.append((dom_sig, dom_conf, 0.5))

        # ── LunarCrush ────────────────────────────────────────
        if "lunarcrush" in raw and raw["lunarcrush"]:
            lc_sig, lc_conf = self._score_lunarcrush(raw["lunarcrush"])
            components["lunarcrush"] = {
                **raw["lunarcrush"],
                "signal": round(lc_sig, 4),
            }
            weighted.append((lc_sig, lc_conf, 0.8))

        # ── TwitterAgent signals ──────────────────────────────
        if "twitter" in raw and raw["twitter"]:
            tw = raw["twitter"]
            tw_sig = tw.get("signal", 0.0)
            tw_conf = tw.get("confidence", 0.0)
            components["twitter"] = {
                "signal": round(tw_sig, 4),
                "confidence": round(tw_conf, 4),
            }
            weighted.append((tw_sig, tw_conf, 0.9))

        # ── RedditAgent signals ───────────────────────────────
        if "reddit" in raw and raw["reddit"]:
            rd = raw["reddit"]
            rd_sig = rd.get("signal", 0.0)
            rd_conf = rd.get("confidence", 0.0)
            components["reddit"] = {
                "signal": round(rd_sig, 4),
                "confidence": round(rd_conf, 4),
            }
            weighted.append((rd_sig, rd_conf, 0.7))

        # ── TelegramAgent signals ─────────────────────────────
        if "telegram" in raw and raw["telegram"]:
            tg = raw["telegram"]
            tg_sig = tg.get("signal", 0.0)
            tg_conf = tg.get("confidence", 0.0)
            components["telegram"] = {
                "signal": round(tg_sig, 4),
                "confidence": round(tg_conf, 4),
            }
            weighted.append((tg_sig, tg_conf, 0.5))

        # ── Combined ──────────────────────────────────────────
        if not weighted:
            comb_sig  = 0.0
            comb_conf = 0.0
        else:
            total_wc = sum(w * c for _, c, w in weighted)
            comb_sig = (
                sum(s * c * w for s, c, w in weighted) / total_wc
                if total_wc > 0 else 0.0
            )
            comb_conf = (
                sum(c * w for _, c, w in weighted) /
                sum(w for _, _, w in weighted)
            )

        sentiment_label = (
            "extremely_bullish" if comb_sig >  0.60 else
            "bullish"           if comb_sig >  0.25 else
            "slightly_bullish"  if comb_sig >  0.10 else
            "extremely_bearish" if comb_sig < -0.60 else
            "bearish"           if comb_sig < -0.25 else
            "slightly_bearish"  if comb_sig < -0.10 else
            "neutral"
        )

        with self._lock:
            self._aggregate = {
                "signal":          comb_sig,
                "confidence":      comb_conf,
                "sentiment_label": sentiment_label,
                "components":      components,
            }

        logger.info(
            "SocialSentimentAgent: signal=%+.3f | conf=%.2f | label=%s | components=%d",
            comb_sig, comb_conf, sentiment_label, len(components),
        )

        return {
            "signal":          round(comb_sig, 4),
            "confidence":      round(comb_conf, 4),
            "sentiment_label": sentiment_label,
            "components":      components,
        }

    # ── Data fetchers ─────────────────────────────────────────

    def _fetch_fear_greed(self) -> dict:
        import urllib.request, json as _json
        url = "https://api.alternative.me/fng/?limit=1&format=json"
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = _json.loads(resp.read().decode())
        entries = data.get("data", [])
        if not entries:
            raise ValueError("No FNG data")
        e = entries[0]
        return {
            "value": int(e.get("value", 50)),
            "value_classification": e.get("value_classification", "Neutral"),
        }

    def _fetch_coingecko_trending(self) -> list[str]:
        """Return list of trending coin symbols (uppercase)."""
        import urllib.request, json as _json
        url = "https://api.coingecko.com/api/v3/search/trending"
        req = urllib.request.Request(
            url,
            headers={
                "Accept": "application/json",
                "User-Agent": "NexusTrader/1.0",
            },
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = _json.loads(resp.read().decode())
        coins = data.get("coins", [])
        return [
            c["item"]["symbol"].upper()
            for c in coins
            if isinstance(c, dict) and "item" in c
        ]

    def _fetch_coingecko_global(self) -> dict:
        """Fetch global market data including BTC dominance."""
        import urllib.request, json as _json
        url = "https://api.coingecko.com/api/v3/global"
        req = urllib.request.Request(
            url,
            headers={
                "Accept": "application/json",
                "User-Agent": "NexusTrader/1.0",
            },
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = _json.loads(resp.read().decode())
        market_data = data.get("data", {})
        dom = market_data.get("market_cap_percentage", {})
        return {
            "btc_dominance": float(dom.get("btc", 50)),
            "eth_dominance": float(dom.get("eth", 15)),
            "total_market_cap_change_pct": float(
                market_data.get("market_cap_change_percentage_24h_usd", 0) or 0
            ),
        }

    def _fetch_lunarcrush(self) -> dict | None:
        """
        Fetch LunarCrush aggregate crypto sentiment.
        Requires LUNARCRUSH_API_KEY in settings — silently skipped if absent.
        """
        try:
            from core.security.key_vault import key_vault
            lc_key = key_vault.load("agents.lunarcrush_api_key") or ""
        except Exception:
            lc_key = ""

        if not lc_key:
            return None

        import urllib.request, json as _json
        # LunarCrush v4 — global crypto sentiment endpoint
        url = "https://lunarcrush.com/api4/public/coins/list/v2?limit=10&sort=galaxy_score"
        req = urllib.request.Request(
            url,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {lc_key}",
            },
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = _json.loads(resp.read().decode())

        coins = data.get("data", [])
        if not coins:
            return None

        avg_galaxy = sum(c.get("galaxy_score", 50) for c in coins[:10]) / min(len(coins), 10)
        avg_alt    = sum(c.get("alt_rank", 500) for c in coins[:10]) / min(len(coins), 10)
        return {
            "avg_galaxy_score": round(avg_galaxy, 2),
            "avg_alt_rank":     round(avg_alt, 2),
            "top_coins": [c.get("symbol", "") for c in coins[:5]],
        }

    # ── Signal scorers ────────────────────────────────────────

    def _score_fng(self, value: int) -> tuple[float, float]:
        """Directional sentiment (not purely contrarian — macro does contrarian)."""
        if value <= 15:
            return -0.70, 0.80   # Extreme fear = strong bearish sentiment
        elif value <= 35:
            return -0.35, 0.60
        elif value >= 85:
            return +0.70, 0.80   # Extreme greed = strong bullish sentiment
        elif value >= 65:
            return +0.35, 0.60
        return 0.0, 0.30

    def _score_trending(self, trending_coins: list[str]) -> tuple[float, float]:
        """More of our watchlist in trending = stronger bullish social momentum."""
        overlap = self._count_watchlist_overlap(trending_coins)
        if overlap >= _TRENDING_STRONG:
            return +0.50, 0.65
        elif overlap >= _TRENDING_MILD:
            return +0.25, 0.45
        # No overlap — might indicate rotation away, slight bearish
        if len(trending_coins) > 0:
            return -0.10, 0.30
        return 0.0, 0.20

    def _score_btc_dominance(self, glb: dict) -> tuple[float, float]:
        """
        BTC dominance trend as alt risk signal.
        Rising dominance → alts weak (bearish for alts, neutral for BTC).
        Falling dominance → alt season (bullish for alts).
        """
        dom = glb.get("btc_dominance", 50)
        # Cap-weighted heuristic — extreme dominance = risk-off, alts not favoured
        if dom >= 60:
            return -0.30, 0.50   # High dominance — risk-off for alts
        elif dom <= 40:
            return +0.30, 0.50   # Low dominance — alt season
        return 0.0, 0.25

    def _score_lunarcrush(self, lc: dict) -> tuple[float, float]:
        """Convert LunarCrush galaxy score to signal."""
        galaxy = lc.get("avg_galaxy_score", 50)
        if galaxy >= _LUNARCRUSH_BULLISH:
            return +0.60, 0.70
        elif galaxy <= _LUNARCRUSH_BEARISH:
            return -0.60, 0.70
        return 0.0, 0.35

    # ── Helpers ───────────────────────────────────────────────

    def _count_watchlist_overlap(self, trending_coins: list[str]) -> int:
        """How many trending coins are in our active watchlist."""
        try:
            from core.scanning.watchlist import WatchlistManager
            wl       = WatchlistManager()
            symbols  = wl.get_active_symbols()
            # Convert "BTC/USDT" → "BTC" for comparison
            bases    = {s.split("/")[0].upper() for s in symbols}
            trending = {c.upper() for c in trending_coins}
            return len(bases & trending)
        except Exception:
            return 0

    # ── Public API ────────────────────────────────────────────

    def get_sentiment_signal(self) -> dict:
        """Return the latest cached sentiment signal."""
        with self._lock:
            if self._aggregate:
                return dict(self._aggregate)
        return {
            "signal": 0.0,
            "confidence": 0.0,
            "sentiment_label": "neutral",
            "stale": True,
        }


# ── Module-level singleton ────────────────────────────────────
social_sentiment_agent: SocialSentimentAgent | None = None
