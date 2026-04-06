"""
Test: NewsFeed 24h fallback when primary 8h window is empty.
Root cause: RSS feeds publish articles sparsely during US off-hours (~20:00-08:00 UTC).
Fix: When primary window (default 8h) yields 0 articles, fall back to 24h window
     and tag those articles with _stale=True.
"""
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock
import sys
sys.path.insert(0, '/sessions/wizardly-dreamy-pasteur/mnt/NexusTrader')

# All titles must contain "BTC" or "Bitcoin" to pass _filter_by_symbol
DISTINCT_TITLES = [
    "Bitcoin surges above 90k on ETF inflows",
    "BTC open interest hits record 40 billion",
    "MicroStrategy acquires additional BTC holdings",
    "Bitcoin dominance rises as altcoins consolidate",
    "BTC miners capitulate ahead of halving event",
]


class TestNewsFeedAgeFallback(unittest.TestCase):

    def _make_feed(self):
        from core.nlp.news_feed import NewsFeed
        import core.nlp.news_feed as _nf_mod
        # Reset shared raw cache so each test starts fresh
        _nf_mod._shared_raw_cache = []
        _nf_mod._shared_raw_ts = None
        feed = NewsFeed(symbols=["BTC", "Bitcoin"])
        feed._cache = {}
        feed._cache_ts = None
        return feed

    def _headline(self, age_hours: float, idx: int = 0, source: str = "CoinDesk") -> dict:
        return {
            "title": DISTINCT_TITLES[idx % len(DISTINCT_TITLES)],
            "source": source,
            "url": f"https://coindesk.com/article/{idx}",
            "timestamp": datetime.now(timezone.utc) - timedelta(hours=age_hours),
        }

    def _patch_fetch(self, feed, headlines):
        feed._fetch_all_sources = MagicMock(return_value=headlines)

    # ── Test 1: articles within 8h → primary window, no stale tag ─────────
    def test_recent_articles_no_fallback(self):
        feed = self._make_feed()
        articles = [self._headline(2.0, 0), self._headline(5.0, 1), self._headline(7.9, 2)]
        self._patch_fetch(feed, articles)
        results = feed.fetch_headlines(max_age_minutes=480)
        self.assertEqual(len(results), 3)
        self.assertFalse(any(h.get("_stale") for h in results), "No articles should be stale")

    # ── Test 2: all articles 9-23h old → 24h fallback activates ──────────
    def test_stale_fallback_when_primary_window_empty(self):
        feed = self._make_feed()
        articles = [self._headline(9.0, 0), self._headline(12.0, 1), self._headline(20.0, 2)]
        self._patch_fetch(feed, articles)
        results = feed.fetch_headlines(max_age_minutes=480)
        self.assertEqual(len(results), 3, "All three 9-20h articles should appear via 24h fallback")
        self.assertTrue(all(h.get("_stale") for h in results), "All fallback articles must be _stale=True")

    # ── Test 3: articles >24h old → both windows fail, returns empty ──────
    def test_no_articles_when_all_older_than_24h(self):
        feed = self._make_feed()
        articles = [self._headline(25.0, 0), self._headline(30.0, 1)]
        self._patch_fetch(feed, articles)
        results = feed.fetch_headlines(max_age_minutes=480)
        self.assertEqual(len(results), 0)

    # ── Test 4: mix of recent and stale → primary window wins ─────────────
    def test_mix_uses_primary_not_fallback(self):
        feed = self._make_feed()
        articles = [self._headline(3.0, 0), self._headline(10.0, 1), self._headline(20.0, 2)]
        self._patch_fetch(feed, articles)
        results = feed.fetch_headlines(max_age_minutes=480)
        self.assertEqual(len(results), 1, "Only the 3h article should be returned from primary window")
        self.assertFalse(results[0].get("_stale"), "Recent article must NOT be marked stale")

    # ── Test 5: fallback results are still cached (no double-fetch) ────────
    def test_fallback_still_cached(self):
        feed = self._make_feed()
        articles = [self._headline(10.0, 0)]
        self._patch_fetch(feed, articles)
        results1 = feed.fetch_headlines(max_age_minutes=480)
        self.assertEqual(len(results1), 1)
        # Second call within TTL must use cache
        feed._fetch_rss = MagicMock(side_effect=AssertionError("cache miss — fetch called again"))
        results2 = feed.fetch_headlines(max_age_minutes=480)
        self.assertEqual(len(results2), 1)

    # ── Test 6: fallback articles are sorted newest-first ─────────────────
    def test_fallback_articles_sorted_newest_first(self):
        feed = self._make_feed()
        articles = [self._headline(20.0, 0), self._headline(9.0, 1), self._headline(15.0, 2)]
        self._patch_fetch(feed, articles)
        results = feed.fetch_headlines(max_age_minutes=480)
        self.assertEqual(len(results), 3)
        ages = [(datetime.now(timezone.utc) - r["timestamp"]).total_seconds() / 3600 for r in results]
        self.assertEqual(ages, sorted(ages), "Should be sorted newest-first (ascending age)")

    # ── Test 7: zero RSS articles → no crash, returns empty ───────────────
    def test_empty_rss_no_crash(self):
        feed = self._make_feed()
        feed._fetch_all_sources = MagicMock(return_value=[])
        results = feed.fetch_headlines(max_age_minutes=480)
        self.assertEqual(results, [])

    # ── Test 8: primary window exact boundary just inside ─────────────────
    def test_primary_boundary_just_inside_8h(self):
        feed = self._make_feed()
        articles = [self._headline(7.998, 0)]  # 479.9 minutes → inside 8h
        self._patch_fetch(feed, articles)
        results = feed.fetch_headlines(max_age_minutes=480)
        self.assertEqual(len(results), 1)
        self.assertFalse(results[0].get("_stale"), "Just-inside article must not be stale")

    # ── Test 9: two sources in fallback window are both returned ──────────
    def test_multiple_sources_in_fallback(self):
        feed = self._make_feed()
        articles = [
            self._headline(10.0, 0, "CoinDesk"),
            self._headline(12.0, 1, "Decrypt"),
        ]
        self._patch_fetch(feed, articles)
        results = feed.fetch_headlines(max_age_minutes=480)
        sources = {h["source"] for h in results}
        self.assertIn("CoinDesk", sources)
        self.assertIn("Decrypt", sources)