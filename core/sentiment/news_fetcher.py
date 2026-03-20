# ============================================================
# NEXUS TRADER — News Fetcher
# Source priority (first non-empty result wins):
#   1. CryptoPanic API  — crypto-specific, free tier with registered key
#   2. CryptoCompare    — free, no key required
#   3. Messari          — free public endpoint, no key required
#   4. NewsAPI.org      — general, requires valid paid/free-tier key
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

_CRYPTOPANIC_BASE = "https://cryptopanic.com/api/v1/posts/"
_CRYPTOPANIC_CURRENCIES = "BTC,ETH,BNB,XRP,SOL,ADA,DOGE,AVAX"


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
    token = api_key if (api_key and api_key not in ("", "__vault__")) else "free"
    currencies = symbol.upper() if symbol else _CRYPTOPANIC_CURRENCIES
    url = (
        f"{_CRYPTOPANIC_BASE}"
        f"?auth_token={token}"
        f"&currencies={currencies}"
        f"&kind=news"
        f"&filter=hot"
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


def fetch_crypto_news(
    api_key: str,
    query: str = _DEFAULT_QUERY,
    symbol: Optional[str] = None,
    page_size: int = 40,
    language: str = "en",
) -> list[dict]:
    """
    Fetch crypto news using a four-source fallback chain:
      1. CryptoPanic   — crypto-specific; uses vault key or registered free key
      2. CryptoCompare — free, no key required
      3. Messari       — free public endpoint, no key required
      4. NewsAPI.org   — general, requires a valid key

    `api_key` is the NewsAPI key (kept for backwards compatibility);
    a separate CryptoPanic key is loaded from the vault when available.
    """
    # ── 1. CryptoPanic ─────────────────────────────────────
    try:
        from core.security.key_vault import key_vault
        cp_key = key_vault.load("agents.cryptopanic_api_key") or ""
    except Exception:
        cp_key = ""

    # Only attempt CryptoPanic when a real registered key is present.
    # The "free" public token was silently revoked by CryptoPanic and
    # consistently returns 0 results or HTTP 401/403.
    if cp_key and cp_key not in ("", "free", "__vault__"):
        articles = _fetch_cryptopanic(api_key=cp_key, symbol=symbol, page_size=page_size)
        if articles:
            logger.info("News source: CryptoPanic (%d articles)", len(articles))
            return articles
        logger.info("CryptoPanic returned 0; trying free fallbacks")
    else:
        logger.debug("No CryptoPanic API key — skipping to free fallbacks")

    # ── 2. CryptoCompare (free, no key) ────────────────────
    articles = _fetch_cryptocompare(symbol=symbol, page_size=page_size)
    if articles:
        logger.info("News source: CryptoCompare (%d articles)", len(articles))
        return articles

    logger.info("CryptoCompare returned 0; trying Messari")

    # ── 3. Messari (free, no key) ───────────────────────────
    articles = _fetch_messari(symbol=symbol, page_size=page_size)
    if articles:
        logger.info("News source: Messari (%d articles)", len(articles))
        return articles

    logger.info("Messari returned 0; trying NewsAPI fallback")

    # ── 4. NewsAPI (paid/free-tier, requires valid key) ─────
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
