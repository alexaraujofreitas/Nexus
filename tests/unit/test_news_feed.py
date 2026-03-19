# ============================================================
# Tests for NewsFeed — news_feed.py rewrite
#
# Validates:
# - feedparser vs fallback detection (NF-01)
# - Per-asset CryptoPanic currencies param (NF-02)
# - User-Agent header on requests (NF-03)
# - RSS feed registry (NF-04)
# - Symbol filter relaxation (NF-05)
# - Deduplication (NF-06)
# - Timestamp parsing (NF-07)
# - Cache behaviour (NF-08)
# - SentimentModel max_age_minutes (NF-09)
# - Feedparser request_headers (NF-10)
# ============================================================
from __future__ import annotations

import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch, MagicMock

from core.nlp.news_feed import NewsFeed, _RSS_FEEDS, _HEADERS


# ── helpers ───────────────────────────────────────────────────

def _make_headline(title="BTC rallies 5%", source="TestSource",
                   age_minutes=30, url="https://example.com"):
    ts = datetime.now(timezone.utc) - timedelta(minutes=age_minutes)
    return {"title": title, "source": source, "url": url, "timestamp": ts}


# ── NF-01: feedparser detection ──────────────────────────────

class TestFeedparserDetection:
    def test_nf01a_fetch_rss_uses_feedparser_when_available(self):
        """NF-01a: _fetch_rss uses feedparser path when feedparser is installed."""
        feed = NewsFeed()
        with patch.object(feed, "_fetch_rss_feedparser", return_value=[_make_headline()]) as fp:
            with patch.object(feed, "_fetch_rss_fallback") as fb:
                result = feed._fetch_rss("Test", "https://example.com/rss")
                fp.assert_called_once()
                fb.assert_not_called()
                assert len(result) == 1

    def test_nf01b_fetch_rss_uses_fallback_without_feedparser(self):
        """NF-01b: _fetch_rss uses fallback when feedparser import fails."""
        feed = NewsFeed()
        import sys
        real_import = __builtins__.__import__ if hasattr(__builtins__, "__import__") else __import__

        def mock_import(name, *args, **kwargs):
            if name == "feedparser":
                raise ImportError("no feedparser")
            return real_import(name, *args, **kwargs)

        with patch.object(feed, "_fetch_rss_feedparser") as fp:
            with patch.object(feed, "_fetch_rss_fallback", return_value=[_make_headline()]) as fb:
                with patch("builtins.__import__", side_effect=mock_import):
                    result = feed._fetch_rss("Test", "https://example.com/rss")
                    fp.assert_not_called()
                    fb.assert_called_once()
                    assert len(result) == 1


# ── NF-02: Per-asset CryptoPanic currencies ──────────────────

class TestCryptoPanicCurrencies:
    def test_nf02a_btc_feed_sends_btc_currency(self):
        """NF-02a: NewsFeed(['BTC', 'Bitcoin']) sends currencies=BTC."""
        feed = NewsFeed(symbols=["BTC", "Bitcoin"])
        with patch("requests.get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.json.return_value = {"results": []}
            mock_resp.raise_for_status = MagicMock()
            mock_get.return_value = mock_resp
            with patch("core.security.key_vault.key_vault") as kv:
                kv.load.return_value = "fake_key"
                # Patch settings import inside the method
                with patch("config.settings.settings") as s:
                    s.get.return_value = "__vault__"
                    feed._fetch_cryptopanic()
                    url_called = mock_get.call_args[0][0]
                    assert "currencies=BTC" in url_called

    def test_nf02b_eth_feed_sends_eth_currency(self):
        """NF-02b: NewsFeed(['ETH', 'Ethereum']) sends currencies=ETH."""
        feed = NewsFeed(symbols=["ETH", "Ethereum"])
        with patch("requests.get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.json.return_value = {"results": []}
            mock_resp.raise_for_status = MagicMock()
            mock_get.return_value = mock_resp
            with patch("core.security.key_vault.key_vault") as kv:
                kv.load.return_value = "fake_key"
                with patch("config.settings.settings") as s:
                    s.get.return_value = "__vault__"
                    feed._fetch_cryptopanic()
                    url_called = mock_get.call_args[0][0]
                    assert "currencies=ETH" in url_called

    def test_nf02c_multi_ticker_currencies(self):
        """NF-02c: Multiple tickers joined with commas."""
        feed = NewsFeed(symbols=["SOL", "Solana"])
        with patch("requests.get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.json.return_value = {"results": []}
            mock_resp.raise_for_status = MagicMock()
            mock_get.return_value = mock_resp
            with patch("core.security.key_vault.key_vault") as kv:
                kv.load.return_value = "fake_key"
                with patch("config.settings.settings") as s:
                    s.get.return_value = "__vault__"
                    feed._fetch_cryptopanic()
                    url_called = mock_get.call_args[0][0]
                    assert "currencies=SOL" in url_called
                    # "Solana" should NOT be in currencies (too long / not a ticker)
                    assert "Solana" not in url_called


# ── NF-03: User-Agent header ────────────────────────────────

class TestUserAgentHeader:
    def test_nf03a_headers_dict_exists(self):
        """NF-03a: _HEADERS dict has User-Agent."""
        assert "User-Agent" in _HEADERS
        assert "NexusTrader" in _HEADERS["User-Agent"]

    def test_nf03b_cryptopanic_sends_headers(self):
        """NF-03b: CryptoPanic request includes headers."""
        feed = NewsFeed()
        with patch("requests.get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.json.return_value = {"results": []}
            mock_resp.raise_for_status = MagicMock()
            mock_get.return_value = mock_resp
            with patch("core.security.key_vault.key_vault") as kv:
                kv.load.return_value = "fake_key"
                with patch("config.settings.settings") as s:
                    s.get.return_value = "__vault__"
                    feed._fetch_cryptopanic()
                    _, kwargs = mock_get.call_args
                    assert kwargs.get("headers") == _HEADERS


# ── NF-04: RSS feed registry ────────────────────────────────

class TestRSSFeedRegistry:
    def test_nf04a_at_least_5_feeds(self):
        """NF-04a: Registry has at least 5 RSS feeds."""
        assert len(_RSS_FEEDS) >= 5

    def test_nf04b_coindesk_url_updated(self):
        """NF-04b: CoinDesk URL is the modern /arc/ URL, not FeedBurner."""
        coindesk = [f for f in _RSS_FEEDS if f[0] == "CoinDesk"]
        assert len(coindesk) == 1
        assert "feedburner" not in coindesk[0][1].lower()
        assert "arc/outboundfeeds" in coindesk[0][1]

    def test_nf04c_all_feeds_have_names_and_urls(self):
        """NF-04c: Every feed entry has name, url, is_general."""
        for name, url, is_general in _RSS_FEEDS:
            assert name
            assert url.startswith("https://")
            assert isinstance(is_general, bool)


# ── NF-05: Symbol filter relaxation ─────────────────────────

class TestSymbolFilterRelaxation:
    def test_nf05a_matching_headlines_returned(self):
        """NF-05a: Headlines matching symbols are returned normally."""
        feed = NewsFeed(symbols=["BTC", "Bitcoin"])
        headlines = [
            _make_headline("BTC hits $80k"),
            _make_headline("Ethereum update coming"),
        ]
        result = feed._filter_by_symbol(headlines)
        assert len(result) == 1
        assert result[0]["title"] == "BTC hits $80k"

    def test_nf05b_generic_fallback_when_zero_match(self):
        """NF-05b: When no headlines match symbols, all are kept with _generic flag."""
        feed = NewsFeed(symbols=["XRP", "Ripple"])
        # Mock the internal fetchers to return generic headlines
        generic = [
            _make_headline("Crypto market rebounds"),
            _make_headline("Fed holds rates steady"),
        ]
        with patch.object(feed, "_fetch_cryptopanic", side_effect=Exception("skip")):
            with patch.object(feed, "_fetch_rss", return_value=list(generic)):
                result = feed.fetch_headlines(max_age_minutes=240)
                # Should have headlines even though none mention XRP
                assert len(result) > 0
                assert all(h.get("_generic", False) for h in result)


# ── NF-06: Deduplication ────────────────────────────────────

class TestDeduplication:
    def test_nf06a_exact_duplicates_removed(self):
        """NF-06a: Exact title duplicates are removed."""
        feed = NewsFeed()
        headlines = [
            _make_headline("BTC rallies 5%", source="Source1"),
            _make_headline("BTC rallies 5%", source="Source2"),
        ]
        result = feed._deduplicate(headlines)
        assert len(result) == 1

    def test_nf06b_similar_duplicates_removed(self):
        """NF-06b: Very similar titles (>80% match) are removed."""
        feed = NewsFeed()
        headlines = [
            _make_headline("Bitcoin price surges to $80,000 today", source="S1"),
            _make_headline("Bitcoin price surges to $80,000 today!", source="S2"),
        ]
        result = feed._deduplicate(headlines)
        assert len(result) == 1

    def test_nf06c_different_titles_kept(self):
        """NF-06c: Different titles are kept."""
        feed = NewsFeed()
        headlines = [
            _make_headline("BTC rallies 5%"),
            _make_headline("Ethereum upgrade delayed"),
        ]
        result = feed._deduplicate(headlines)
        assert len(result) == 2


# ── NF-07: Timestamp parsing ────────────────────────────────

class TestTimestampParsing:
    def test_nf07a_iso8601(self):
        """NF-07a: Parses ISO 8601 timestamps."""
        dt = NewsFeed._parse_timestamp("2026-03-16T12:30:00Z")
        assert dt is not None
        assert dt.year == 2026

    def test_nf07b_rfc2822(self):
        """NF-07b: Parses RFC 2822 timestamps."""
        dt = NewsFeed._parse_timestamp("Mon, 16 Mar 2026 12:30:00 +0000")
        assert dt is not None
        assert dt.month == 3

    def test_nf07c_none_returns_none(self):
        """NF-07c: None input returns None."""
        assert NewsFeed._parse_timestamp(None) is None


# ── NF-08: Caching ──────────────────────────────────────────

class TestCaching:
    def test_nf08a_second_call_uses_cache(self):
        """NF-08a: Second call within TTL uses cache."""
        feed = NewsFeed()
        hl = [_make_headline("BTC up")]
        with patch.object(feed, "_fetch_cryptopanic", return_value=hl):
            with patch.object(feed, "_fetch_rss", return_value=[]):
                r1 = feed.fetch_headlines()
                r2 = feed.fetch_headlines()
                # fetch_cryptopanic called only once
                assert feed._fetch_cryptopanic.call_count == 1


# ── NF-09: SentimentModel max_age_minutes ───────────────────

class TestSentimentModelMaxAge:
    def test_nf09a_sentiment_model_uses_480_minutes(self):
        """NF-09a: SentimentModel reads max_age_minutes (default 480) from settings."""
        import inspect
        from core.signals.sub_models.sentiment_model import SentimentModel
        source = inspect.getsource(SentimentModel.evaluate)
        # After strategy redesign, the value is read from settings with 480 as fallback
        assert "max_age_minutes" in source
        assert "480" in source  # default value is still 480


# ── NF-10: feedparser request_headers ───────────────────────

class TestFeedparserHeaders:
    def test_nf10a_feedparser_receives_user_agent(self):
        """NF-10a: feedparser.parse() is called with request_headers."""
        feed = NewsFeed()
        mock_feed = {
            "bozo": False,
            "entries": [
                {"title": "BTC news", "link": "https://ex.com", "published": "2026-03-16T12:00:00Z"}
            ],
        }
        with patch("feedparser.parse", return_value=mock_feed) as mock_fp:
            result = feed._fetch_rss_feedparser("Test", "https://example.com/rss")
            mock_fp.assert_called_once()
            _, kwargs = mock_fp.call_args
            assert "request_headers" in kwargs
            assert "User-Agent" in kwargs["request_headers"]
