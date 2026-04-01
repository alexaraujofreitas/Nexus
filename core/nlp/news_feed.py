# ============================================================
# NEXUS TRADER — FinBERT News NLP Pipeline: News Feed Aggregator
#
# Aggregates crypto news headlines from multiple free sources.
# Sources: CryptoPanic API, CoinDesk RSS, Cointelegraph RSS,
#          The Block RSS, Decrypt RSS, Bitcoin Magazine RSS
# Returns cleaned headline list with timestamps.
# ============================================================
from __future__ import annotations

import logging
import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import urljoin
from difflib import SequenceMatcher

logger = logging.getLogger(__name__)

# ── Headline deduplication threshold ─────────────────────────
_DUPLICATE_THRESHOLD = 0.80  # >80% similarity = duplicate

# ── Common request headers (avoids 403 from Cloudflare/WAFs) ─
_HEADERS = {
    "User-Agent": "NexusTrader/1.0 (News Aggregator; +https://github.com)",
    "Accept": "application/rss+xml, application/xml, text/xml, */*",
}

# ── RSS feed registry ───────────────────────────────────────
# Each entry: (name, url, is_general)
# is_general=True means the feed covers all crypto, not asset-specific
_RSS_FEEDS = [
    ("CoinDesk",       "https://www.coindesk.com/arc/outboundfeeds/rss/",  True),
    ("Cointelegraph",  "https://cointelegraph.com/rss",                     True),
    # The Block removed: theblock.co/rss.xml returns malformed XML on every request
    # (not well-formed at offset 2:751). Removing saves ~1s of wasted fetch time per
    # scan cycle. Re-add if The Block fixes their feed.
    ("Decrypt",        "https://decrypt.co/feed",                           True),
    ("Bitcoin Magazine","https://bitcoinmagazine.com/feed",                  True),
]


class NewsFeed:
    """
    Aggregates crypto news headlines from multiple free sources.

    Sources tried in order (fail silently if unavailable):
    1. CryptoPanic API (requires auth_token from settings)
    2. Multiple RSS feeds (CoinDesk, Cointelegraph, The Block, Decrypt, Bitcoin Magazine)

    Returns deduplicated, symbol-filtered headlines with timestamps.
    Caches results for 5 minutes to avoid hammering APIs.
    """

    def __init__(self, symbols: list[str] | None = None):
        """
        Initialize news feed aggregator.

        Parameters
        ----------
        symbols : list[str], optional
            Symbols to filter for (e.g., ["BTC", "Bitcoin"]).
            Defaults to ["BTC", "Bitcoin"].
        """
        self.symbols = symbols or ["BTC", "Bitcoin"]
        self._cache: dict = {}
        self._cache_ts: datetime | None = None
        self._cache_ttl_seconds = 300  # 5 minutes

    def fetch_headlines(self, max_age_minutes: int = 480) -> list[dict]:
        """
        Fetch and aggregate headlines from all available sources.

        Parameters
        ----------
        max_age_minutes : int
            Only return headlines newer than this (in minutes).

        Returns
        -------
        list[dict]
            List of dicts: {"title": str, "source": str, "timestamp": datetime, "url": str}
            - Deduplicated by title similarity
            - Filtered by tracked symbols (case-insensitive)
            - Sorted by timestamp descending (newest first)
            - Max 50 headlines returned
        """
        # Check cache
        if self._cache and self._cache_ts:
            age = (datetime.now(timezone.utc) - self._cache_ts).total_seconds()
            if age < self._cache_ttl_seconds:
                logger.debug(f"NewsFeed: using cached results (age={age:.0f}s)")
                return self._cache.get("headlines", [])

        headlines = []

        # Try CryptoPanic API first (asset-aware)
        try:
            cp_headlines = self._fetch_cryptopanic()
            headlines.extend(cp_headlines)
            logger.debug(f"NewsFeed: fetched {len(cp_headlines)} from CryptoPanic")
        except Exception as e:
            logger.debug(f"NewsFeed: CryptoPanic unavailable: {e}")

        # Try all RSS feeds
        for feed_name, feed_url, _is_general in _RSS_FEEDS:
            try:
                rss_headlines = self._fetch_rss(feed_name, feed_url)
                headlines.extend(rss_headlines)
                logger.debug(f"NewsFeed: fetched {len(rss_headlines)} from {feed_name}")
            except Exception as e:
                logger.debug(f"NewsFeed: {feed_name} unavailable: {e}")

        # Deduplicate by title similarity
        headlines = self._deduplicate(headlines)

        # Filter by symbol (relaxed: if zero pass, return all with a flag)
        filtered = self._filter_by_symbol(headlines)
        if filtered:
            headlines = filtered
        else:
            # If no headlines match the specific asset keywords, keep all
            # headlines anyway — general crypto news still has value for
            # overall market sentiment. Tag them so callers can weight
            # them lower if desired.
            for h in headlines:
                h["_generic"] = True

        # Sort by timestamp descending
        headlines.sort(key=lambda h: h["timestamp"], reverse=True)

        # Enforce max age — primary window
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=max_age_minutes)
        recent = [h for h in headlines if h["timestamp"] >= cutoff]

        if recent:
            headlines = recent
        else:
            # Primary window (default 8h) is empty — common during the US
            # business-hours gap (~20:00–08:00 UTC) when crypto outlets publish
            # fewer articles.  Fall back to a 24h window and tag articles as
            # stale so callers can weight them lower if desired.
            extended_cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
            stale = [h for h in headlines if h["timestamp"] >= extended_cutoff]
            for h in stale:
                h["_stale"] = True
            if stale:
                headlines = stale
                logger.debug(
                    f"NewsFeed: primary {max_age_minutes}min window empty — "
                    f"using 24h fallback ({len(stale)} stale headlines)"
                )
            else:
                headlines = []

        # Limit to 50
        headlines = headlines[:50]

        # Cache results
        self._cache = {"headlines": headlines}
        self._cache_ts = datetime.now(timezone.utc)

        logger.info(
            f"NewsFeed: aggregated {len(headlines)} headlines from "
            f"{len(set(h['source'] for h in headlines))} sources"
        )

        return headlines

    def _fetch_cryptopanic(self) -> list[dict]:
        """Fetch headlines from CryptoPanic API."""
        try:
            import requests
        except ImportError:
            raise ImportError("requests library required for CryptoPanic API")

        # Get API key from settings
        try:
            from config.settings import settings
            api_key = settings.get("agents.cryptopanic_api_key", "")
            # If the settings value is the vault placeholder, resolve it from the vault.
            if not api_key or api_key == "__vault__":
                try:
                    from core.security.key_vault import key_vault
                    api_key = key_vault.load("agents.cryptopanic_api_key") or ""
                except Exception:
                    api_key = ""
        except Exception:
            api_key = ""

        if not api_key or api_key == "__vault__":
            raise ValueError("No CryptoPanic API key configured")

        # Build the currencies parameter from the symbols list.
        # CryptoPanic expects ticker symbols like BTC,ETH,SOL.
        # Extract just the ticker from the keywords list (first item is usually the ticker).
        currencies = []
        for sym in self.symbols:
            # Only add short tickers (not full names like "Bitcoin")
            if len(sym) <= 5 and sym.isalpha() and sym.isupper():
                currencies.append(sym)
        currencies_param = ",".join(currencies) if currencies else "BTC"

        # CryptoPanic API v2 — base endpoint changed from /api/v1/ to /api/developer/v2/
        # The `kind` parameter filters by content type (news/media/all).
        # The `filter` parameter now filters by sentiment (rising/hot/bullish/bearish/…).
        # `public=true` requests non-personalised posts (no user-specific sources).
        url = (
            f"https://cryptopanic.com/api/developer/v2/posts/"
            f"?auth_token={api_key}"
            f"&kind=news"
            f"&public=true"
            f"&currencies={currencies_param}"
        )
        response = requests.get(url, timeout=10, headers=_HEADERS)
        response.raise_for_status()

        data = response.json()
        headlines = []

        for item in data.get("results", []):
            headline = {
                "title": item.get("title", ""),
                "source": "CryptoPanic",
                "url": item.get("url", ""),
                "timestamp": self._parse_timestamp(item.get("published_at")),
            }
            if headline["title"] and headline["timestamp"]:
                headlines.append(headline)

        return headlines

    def _fetch_rss(self, source_name: str, url: str) -> list[dict]:
        """
        Fetch headlines from an RSS feed.

        Tries feedparser first (best compatibility), falls back to
        xml.etree + requests if feedparser is not installed.
        """
        _have_feedparser = False
        try:
            import feedparser
            _have_feedparser = True
        except ImportError:
            pass

        if _have_feedparser:
            return self._fetch_rss_feedparser(source_name, url)
        else:
            return self._fetch_rss_fallback(source_name, url)

    def _fetch_rss_feedparser(self, source_name: str, url: str) -> list[dict]:
        """Fetch RSS feed using feedparser (preferred)."""
        import feedparser
        import concurrent.futures

        # feedparser.parse() uses urllib internally and has NO timeout
        # parameter.  A hanging RSS server can block the entire ScanWorker
        # thread for minutes.  Wrap in a ThreadPoolExecutor with a 10s
        # timeout so a single slow feed cannot stall the scan.
        def _do_parse():
            return feedparser.parse(
                url,
                request_headers={
                    "User-Agent": _HEADERS["User-Agent"],
                    "Accept": _HEADERS["Accept"],
                },
            )

        # IMPORTANT: Do NOT use `with ThreadPoolExecutor` — shutdown(wait=True)
        # in __exit__ blocks indefinitely if the parse thread hangs.
        _tp = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        try:
            feed = _tp.submit(_do_parse).result(timeout=10)
        except concurrent.futures.TimeoutError:
            raise RuntimeError(f"{source_name} RSS fetch timed out after 10s")
        finally:
            _tp.shutdown(wait=False, cancel_futures=True)

        if feed.get("bozo") and not feed.get("entries"):
            exc = feed.get("bozo_exception", "unknown error")
            raise RuntimeError(f"{source_name} RSS parse failed: {exc}")

        headlines = []
        for entry in feed.get("entries", []):
            headline = {
                "title": entry.get("title", ""),
                "source": source_name,
                "url": entry.get("link", ""),
                "timestamp": self._parse_timestamp(entry.get("published")),
            }
            if headline["title"] and headline["timestamp"]:
                headlines.append(headline)
        return headlines

    def _fetch_rss_fallback(self, source_name: str, url: str) -> list[dict]:
        """Fetch RSS feed using requests + xml.etree (fallback)."""
        import requests
        import xml.etree.ElementTree as ET

        response = requests.get(url, timeout=10, headers=_HEADERS)
        response.raise_for_status()
        root = ET.fromstring(response.content)

        headlines = []
        for item in root.findall(".//item"):
            title_elem = item.find("title")
            link_elem = item.find("link")
            pubdate_elem = item.find("pubDate")

            title = title_elem.text if title_elem is not None else ""
            url_val = link_elem.text if link_elem is not None else ""
            pubdate = pubdate_elem.text if pubdate_elem is not None else None

            headline = {
                "title": title,
                "source": source_name,
                "url": url_val,
                "timestamp": self._parse_timestamp(pubdate),
            }
            if headline["title"] and headline["timestamp"]:
                headlines.append(headline)

        return headlines

    @staticmethod
    def _parse_timestamp(ts_str: str | None) -> datetime | None:
        """Parse RFC 3339 or RFC 2822 timestamp strings."""
        if not ts_str:
            return None

        # Try ISO 8601 / RFC 3339
        for fmt in [
            "%Y-%m-%dT%H:%M:%S%z",
            "%Y-%m-%dT%H:%M:%SZ",
            "%Y-%m-%dT%H:%M:%S",
        ]:
            try:
                dt = datetime.strptime(ts_str.replace("Z", "+0000"), fmt)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt
            except ValueError:
                pass

        # Try RFC 2822 (email format)
        try:
            from email.utils import parsedate_to_datetime
            dt = parsedate_to_datetime(ts_str)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except (ValueError, TypeError):
            pass

        # Fallback: return now if we can't parse
        logger.debug(f"NewsFeed: could not parse timestamp '{ts_str}', using now()")
        return datetime.now(timezone.utc)

    def _deduplicate(self, headlines: list[dict]) -> list[dict]:
        """Remove duplicate headlines by title similarity."""
        if not headlines:
            return []

        seen_titles = []
        deduplicated = []

        for headline in headlines:
            title = headline["title"].lower().strip()
            is_duplicate = False

            for seen_title in seen_titles:
                similarity = SequenceMatcher(None, title, seen_title).ratio()
                if similarity > _DUPLICATE_THRESHOLD:
                    is_duplicate = True
                    break

            if not is_duplicate:
                seen_titles.append(title)
                deduplicated.append(headline)

        logger.debug(
            f"NewsFeed: deduplicated {len(headlines)} → {len(deduplicated)} headlines"
        )
        return deduplicated

    def _filter_by_symbol(self, headlines: list[dict]) -> list[dict]:
        """Filter headlines to only those mentioning tracked symbols."""
        filtered = []

        for headline in headlines:
            title_lower = headline["title"].lower()
            for symbol in self.symbols:
                if symbol.lower() in title_lower:
                    filtered.append(headline)
                    break

        logger.debug(
            f"NewsFeed: filtered {len(headlines)} → {len(filtered)} headlines by symbol"
        )
        return filtered
