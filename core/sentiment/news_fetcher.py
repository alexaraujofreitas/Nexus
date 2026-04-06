# ============================================================
# NEXUS TRADER — News Fetcher
# Source priority (free sources first, CryptoPanic optional):
#   1. CryptoCompare    — free, no key required
#   2. Messari          — free public endpoint, no key required
#   3. NewsAPI.org      — general, requires valid paid/free-tier key
#   4. RSS fallback     — 5 major crypto RSS sources, always available
#   5. CryptoPanic API  — crypto-specific, optional bonus (registered key only)
# ============================================================
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

# Crypto-centric default query for NewsAPI fallback
_DEFAULT_QUERY = (
    "bitcoin OR ethereum OR crypto OR cryptocurrency OR "
    "BTC OR ETH OR altcoin OR blockchain OR DeFi OR NFT"
)

# CryptoPanic v2 — tier determined by auth_token, NOT the URL path.
# /api/v1/ returns 404 for all tiers since the v2 migration.
_CRYPTOPANIC_BASE = "https://cryptopanic.com/api/developer/v2/posts/"
_CRYPTOPANIC_CURRENCIES = "BTC,ETH,BNB,XRP,SOL,ADA,DOGE,AVAX"

# RSS sources — same registry as core/nlp/news_feed.py.
# Used as a zero-key fallback when all API sources fail.
_RSS_SOURCES = [
    ("CoinDesk",          "https://www.coindesk.com/arc/outboundfeeds/rss/"),
    ("Cointelegraph",     "https://cointelegraph.com/rss"),
    ("The Block",         "https://www.theblock.co/rss.xml"),
    ("Decrypt",           "https://decrypt.co/feed"),
    ("Bitcoin Magazine",  "https://bitcoinmagazine.com/feed"),
]
_RSS_HEADERS = {"User-Agent": "NexusTrader/1.0 (crypto news aggregator)"}


def _fetch_cryptopanic(
    api_key: str = "free",
    symbol: Optional[str] = None,
    page_size: int = 40,
) -> list[dict]:
    """
    Fetch crypto news from CryptoPanic API.
    Uses 'free' public token if no key is provided.
    Returns normalised article dicts.
    """
    # v2: tier determined by token — "free" public token no longer works.
    # Caller is responsible for passing a real registered key.
    token = api_key if (api_key and api_key not in ("", "__vault__", "free")) else ""
    if not token:
        logger.debug("CryptoPanic: no valid API key — skipping")
        return []
    currencies = symbol.upper() if symbol else _CRYPTOPANIC_CURRENCIES
    url = (
        f"{_CRYPTOPANIC_BASE}"
        f"?auth_token={token}"
        f"&currencies={currencies}"
        f"&kind=news"      # content-type filter (v2)
        f"&public=true"
    )

    try:
        req = urllib.request.Request(url, headers={"User-Agent": "NexusTrader/1.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
    except Exception as exc:
        logger.warning("CryptoPanic fetch failed: %s", exc)
        return []

    articles = []
    for item in data.get("results", [])[:page_size]:
        try:
            created = item.get("created_at", "")
            if created:
                pub_dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
            else:
                pub_dt = datetime.now(timezone.utc)

            # CryptoPanic article URL
            source_info = item.get("source") or {}
            article_url = item.get("url", "") or source_info.get("url", "")

            # Extract symbol from currencies list
            currencies_list = item.get("currencies", []) or []
            sym = currencies_list[0].get("code", "") if currencies_list else (symbol or "")

            articles.append({
                "title":        item.get("title", ""),
                "description":  "",          # CryptoPanic only provides title
                "url":          article_url,
                "source":       source_info.get("title", "CryptoPanic"),
                "published_at": pub_dt,
                "symbol":       sym,
                "raw":          item,
            })
        except Exception:
            continue

    logger.info("CryptoPanic: fetched %d articles", len(articles))
    return articles


def _fetch_newsapi(
    api_key: str,
    query: str = _DEFAULT_QUERY,
    symbol: Optional[str] = None,
    page_size: int = 40,
    language: str = "en",
) -> list[dict]:
    """
    Fetch crypto news via NewsAPI.org.
    Returns normalised article dicts, or [] on any error.
    """
    # A real NewsAPI key is a 32-character hex string.
    # Reject blank, placeholder, or obviously invalid keys silently so they
    # never reach the API and produce noisy "apiKeyInvalid" warnings.
    if not api_key or len(api_key.strip()) < 20 or api_key.strip() in ("__vault__", "your_key_here"):
        logger.debug("NewsAPI: no valid API key configured — skipping")
        return []

    try:
        from newsapi import NewsApiClient
    except ImportError:
        logger.warning("newsapi-python not installed; pip install newsapi-python")
        return []

    if symbol:
        query = f"{symbol} AND ({_DEFAULT_QUERY})"

    try:
        client = NewsApiClient(api_key=api_key)
        resp = client.get_everything(
            q=query,
            language=language,
            sort_by="publishedAt",
            page_size=min(page_size, 100),
        )
    except Exception as exc:
        logger.warning("NewsAPI fetch failed: %s", exc)
        return []

    # Detect API-level errors returned as a response dict
    if isinstance(resp, dict) and resp.get("status") == "error":
        logger.warning(
            "NewsAPI error [%s]: %s",
            resp.get("code", "unknown"),
            resp.get("message", ""),
        )
        return []

    articles = []
    for raw in resp.get("articles", []):
        try:
            pub = raw.get("publishedAt", "")
            pub_dt = (
                datetime.fromisoformat(pub.replace("Z", "+00:00"))
                if pub
                else datetime.now(timezone.utc)
            )
            articles.append({
                "title":        raw.get("title", ""),
                "description":  raw.get("description") or "",
                "url":          raw.get("url", ""),
                "source":       raw.get("source", {}).get("name", "Unknown"),
                "published_at": pub_dt,
                "symbol":       symbol or "",
                "raw":          raw,
            })
        except Exception:
            continue

    logger.info("NewsAPI: fetched %d articles", len(articles))
    return articles


def _fetch_cryptocompare(
    symbol: Optional[str] = None,
    page_size: int = 20,
) -> list[dict]:
    """
    Fetch from CryptoCompare News API — free, no key required.
    Returns normalised article dicts compatible with the rest of the pipeline.
    """
    url = (
        "https://min-api.cryptocompare.com/data/v2/news/"
        f"?lang=EN&sortOrder=latest&limit={min(page_size, 50)}"
    )
    if symbol:
        url += f"&categories={symbol.upper()}"

    try:
        req = urllib.request.Request(
            url,
            headers={"Accept": "application/json", "User-Agent": "NexusTrader/1.0"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
    except Exception as exc:
        logger.warning("CryptoCompare news fetch failed: %s", exc)
        return []

    articles = []
    for item in data.get("Data", []):
        try:
            ts = int(item.get("published_on", 0) or 0)
            pub_dt = (
                datetime.fromtimestamp(ts, tz=timezone.utc)
                if ts
                else datetime.now(timezone.utc)
            )
            articles.append({
                "title":        item.get("title", ""),
                "description":  (item.get("body") or "")[:300],
                "url":          item.get("url", ""),
                "source":       item.get("source_info", {}).get("name", "CryptoCompare"),
                "published_at": pub_dt,
                "symbol":       symbol or "",
                "raw":          item,
            })
        except Exception:
            continue

    logger.info("CryptoCompare: fetched %d articles", len(articles))
    return articles


def _fetch_messari(
    symbol: Optional[str] = None,
    page_size: int = 20,
) -> list[dict]:
    """
    Fetch from Messari public news endpoint — free, no key required.
    Returns normalised article dicts compatible with the rest of the pipeline.
    """
    url = f"https://data.messari.io/api/v1/news?limit={min(page_size, 20)}"
    if symbol:
        url = f"https://data.messari.io/api/v1/assets/{symbol.lower()}/news?limit={min(page_size, 20)}"

    try:
        req = urllib.request.Request(
            url,
            headers={"Accept": "application/json", "User-Agent": "NexusTrader/1.0"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
    except Exception as exc:
        logger.warning("Messari news fetch failed: %s", exc)
        return []

    articles = []
    for item in data.get("data", []):
        try:
            pub_str = item.get("published_at", "")
            pub_dt = (
                datetime.fromisoformat(pub_str.replace("Z", "+00:00"))
                if pub_str
                else datetime.now(timezone.utc)
            )
            articles.append({
                "title":        item.get("title", ""),
                "description":  (item.get("content") or "")[:300],
                "url":          item.get("url", ""),
                "source":       "Messari",
                "published_at": pub_dt,
                "symbol":       symbol or "",
                "raw":          item,
            })
        except Exception:
            continue

    logger.info("Messari: fetched %d articles", len(articles))
    return articles


def _fetch_rss_fallback(
    symbol: Optional[str] = None,
    max_age_minutes: int = 480,
    page_size: int = 40,
) -> list[dict]:
    """
    Fetch from 5 RSS sources using feedparser.
    This is the zero-key fallback — always available.
    Articles older than max_age_minutes are discarded.
    """
    try:
        import feedparser
        _fp_available = True
    except ImportError:
        _fp_available = False

    if not _fp_available:
        logger.debug("feedparser not installed — RSS fallback skipped")
        return []

    from datetime import timedelta
    import concurrent.futures

    cutoff = datetime.now(timezone.utc) - timedelta(minutes=max_age_minutes)
    sym_keywords: list[str] = []
    if symbol:
        base = symbol.split("/")[0].upper()
        sym_keywords = [base, base.lower(), "bitcoin" if base == "BTC" else
                        "ethereum" if base == "ETH" else base.lower()]

    def _fetch_one(name: str, url: str) -> list[dict]:
        try:
            req = urllib.request.Request(url, headers=_RSS_HEADERS)
            with urllib.request.urlopen(req, timeout=10) as resp:
                raw = resp.read()
            feed = feedparser.parse(raw)
        except Exception as exc:
            logger.debug("RSS %s fetch failed: %s", name, exc)
            return []

        results = []
        for entry in feed.entries:
            try:
                title = entry.get("title", "")
                if not title:
                    continue

                # Parse publish time
                tp = entry.get("published_parsed") or entry.get("updated_parsed")
                if tp:
                    from calendar import timegm
                    pub_dt = datetime.fromtimestamp(timegm(tp), tz=timezone.utc)
                else:
                    pub_dt = datetime.now(timezone.utc)

                if pub_dt < cutoff:
                    continue

                # Symbol filter — keep if matches or no filter set
                if sym_keywords:
                    text_lower = (title + " " + entry.get("summary", "")).lower()
                    if not any(kw in text_lower for kw in sym_keywords):
                        continue

                results.append({
                    "title":        title,
                    "description":  (entry.get("summary") or "")[:300],
                    "url":          entry.get("link", ""),
                    "source":       name,
                    "published_at": pub_dt,
                    "symbol":       symbol or "",
                    "raw":          {},
                })
            except Exception:
                continue
        return results

    articles: list[dict] = []
    _pool = concurrent.futures.ThreadPoolExecutor(max_workers=len(_RSS_SOURCES))
    try:
        futures = {_pool.submit(_fetch_one, name, url): name
                   for name, url in _RSS_SOURCES}
        for fut in concurrent.futures.as_completed(futures, timeout=15):
            try:
                articles.extend(fut.result())
            except Exception:
                pass
    except Exception as exc:
        logger.debug("RSS fallback pool error: %s", exc)
    finally:
        _pool.shutdown(wait=False, cancel_futures=True)

    # Deduplicate by title, sort newest first, cap at page_size
    seen: set[str] = set()
    unique: list[dict] = []
    for a in sorted(articles, key=lambda x: x["published_at"], reverse=True):
        key = a["title"].lower().strip()
        if key not in seen:
            seen.add(key)
            unique.append(a)
        if len(unique) >= page_size:
            break

    logger.info("RSS fallback: %d articles from %d sources", len(unique), len(_RSS_SOURCES))
    return unique


def fetch_crypto_news(
    api_key: str,
    query: str = _DEFAULT_QUERY,
    symbol: Optional[str] = None,
    page_size: int = 40,
    language: str = "en",
) -> list[dict]:
    """
    Fetch crypto news using a five-source fallback chain (free sources primary):
      1. CryptoCompare — free, no key required
      2. Messari       — free public endpoint, no key required
      3. NewsAPI.org   — general, requires a valid key
      4. RSS fallback  — 5 major crypto RSS sources, always available
      5. CryptoPanic   — crypto-specific; optional bonus if vault key available

    `api_key` is the NewsAPI key (kept for backwards compatibility);
    a separate CryptoPanic key is loaded from the vault when available.

    CryptoPanic is optional — the system functions perfectly without it.
    Free sources are tried first to ensure reliable news delivery.
    """
    logger.info("CryptoPanic is optional; using free sources as primary")

    # ── 1. CryptoCompare (free, no key) ────────────────────
    articles = _fetch_cryptocompare(symbol=symbol, page_size=page_size)
    if articles:
        logger.info("News source: CryptoCompare (%d articles)", len(articles))
        return articles

    logger.info("CryptoCompare returned 0; trying Messari")

    # ── 2. Messari (free, no key) ───────────────────────────
    articles = _fetch_messari(symbol=symbol, page_size=page_size)
    if articles:
        logger.info("News source: Messari (%d articles)", len(articles))
        return articles

    logger.info("Messari returned 0; trying NewsAPI fallback")

    # ── 3. NewsAPI (paid/free-tier, requires valid key) ─────
    articles = _fetch_newsapi(
        api_key=api_key,
        query=query,
        symbol=symbol,
        page_size=page_size,
        language=language,
    )
    if articles:
        logger.info("News source: NewsAPI (%d articles)", len(articles))
        return articles

    # ── 4. RSS fallback (always available, no key required) ──
    # Guarantees articles are shown even when all API keys are absent/invalid.
    logger.info("NewsAPI returned 0; falling back to RSS sources")
    articles = _fetch_rss_fallback(symbol=symbol, page_size=page_size)
    if articles:
        logger.info("News source: RSS fallback (%d articles)", len(articles))
        return articles

    # ── 5. CryptoPanic (optional bonus, if vault key available) ──────
    # Only attempted after all free sources are exhausted.
    # The "free" public token was silently revoked by CryptoPanic and
    # consistently returns 0 results or HTTP 401/403.
    try:
        from core.security.key_vault import key_vault
        cp_key = key_vault.load("agents.cryptopanic_api_key") or ""
    except Exception:
        cp_key = ""

    if cp_key and cp_key not in ("", "free", "__vault__"):
        logger.info("Attempting CryptoPanic (optional bonus source)")
        articles = _fetch_cryptopanic(api_key=cp_key, symbol=symbol, page_size=page_size)
        if articles:
            logger.info("News source: CryptoPanic (%d articles)", len(articles))
            return articles
        logger.info("CryptoPanic returned 0")
    else:
        logger.debug("No CryptoPanic API key available (optional feature)")

    logger.warning("No news articles found from any source")
    return []
