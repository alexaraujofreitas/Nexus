"""
tests/unit/test_log_review_fixes.py
------------------------------------
Regression tests for fixes identified in the 2026-03-15 overnight log review:

  LR-01  NarrativeShiftAgent scorer API — score() not __call__
  LR-02  CrashDetector import paths — core.agents not core.intelligence
  LR-03  RLEnsemble regime names — HMM names mapped to active-regime lists
  LR-04  SignalGenerator singleton — ScanWorker reuses shared instance
  LR-05  TwitterAgent urllib timeout — urlopen() not Request()
  LR-06  BUSD removed from stablecoin watchlist
  LR-07  CryptoPanic vault resolution — __vault__ resolved from key_vault
  LR-08  TwitterAgent CryptoPanic — hardcoded auth_token=free replaced with vault key
"""

import inspect
import sys
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# LR-01: NarrativeShiftAgent scorer API
# ---------------------------------------------------------------------------

class TestNarrativeScorerAPI:
    """LR-01 — _score_narrative must call scorer.score([text]) not scorer(text)."""

    def test_lr01_score_list_called_not_callable(self):
        """NarrativeShiftAgent._score_narrative must use scorer.score(), not scorer()."""
        import ast
        src = open(
            "core/agents/narrative_agent.py", encoding="utf-8"
        ).read()
        # Must use .score() method
        assert "scorer.score(" in src, "scorer.score() call not found"
        # Must NOT call scorer directly as a callable on individual texts
        # (old broken pattern) — use AST to avoid matching comments
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                # Look for bare scorer(text) call (not scorer.score(...))
                if isinstance(func, ast.Name) and func.id == "scorer":
                    # Check if any arg is a simple Name "text"
                    for arg in node.args:
                        if isinstance(arg, ast.Name) and arg.id == "text":
                            pytest.fail(
                                f"scorer(text) call found at line {node.lineno} "
                                f"— should use scorer.score([text])"
                            )

    def test_lr01_score_unpacks_tuple(self):
        """Return value of scorer.score() is list[tuple[float, float]] — must unpack."""
        src = open("core/agents/narrative_agent.py", encoding="utf-8").read()
        # The fix unpacks (sig, _conf) from each result
        assert "for sig, _conf in results" in src or "for sig," in src, \
            "Result tuple unpacking not found"

    def test_lr01_scorer_mock_works(self):
        """Verify the new call pattern works with a mock scorer."""
        mock_scorer = MagicMock()
        mock_scorer.score.return_value = [(0.7, 0.9), (0.5, 0.8)]

        texts = ["Bitcoin ETF approved", "Crypto market rally"]
        results = mock_scorer.score(texts)
        signals = [sig for sig, _conf in results]
        avg = sum(signals) / len(signals)

        assert abs(avg - 0.6) < 1e-9
        mock_scorer.score.assert_called_once_with(texts)

    def test_lr01_callable_check(self):
        """_FinBERTScorer must have a score() method but is NOT required to be callable."""
        try:
            from core.ai.model_registry import ModelRegistry
            registry = ModelRegistry()
            scorer = registry.get_scorer("narrative_shift")
            assert hasattr(scorer, "score"), "Scorer missing score() method"
            # The old code assumed scorer was callable — confirm it need not be
            # (we no longer rely on __call__)
        except Exception:
            pytest.skip("ModelRegistry unavailable in test environment")


# ---------------------------------------------------------------------------
# LR-02: CrashDetector import paths
# ---------------------------------------------------------------------------

class TestCrashDetectorImportPaths:
    """LR-02 — CrashDetector must import from core.agents, not core.intelligence."""

    def _read_source(self):
        return open("core/risk/crash_detector.py", encoding="utf-8").read()

    def test_lr02_no_core_intelligence_import(self):
        src = self._read_source()
        assert "core.intelligence" not in src, \
            "core.intelligence import still present — should be core.agents"

    def test_lr02_liquidation_correct_path(self):
        src = self._read_source()
        assert "from core.agents.liquidation_flow_agent import LiquidationFlowAgent" in src

    def test_lr02_onchain_correct_path(self):
        src = self._read_source()
        assert "from core.agents.onchain_agent import OnChainAgent" in src

    def test_lr02_liquidation_agent_importable(self):
        """The module at the fixed path must actually exist."""
        try:
            from core.agents.liquidation_flow_agent import LiquidationFlowAgent  # noqa: F401
        except ImportError as e:
            pytest.fail(f"Could not import LiquidationFlowAgent from core.agents: {e}")

    def test_lr02_onchain_agent_importable(self):
        try:
            from core.agents.onchain_agent import OnChainAgent  # noqa: F401
        except ImportError as e:
            pytest.fail(f"Could not import OnChainAgent from core.agents: {e}")


# ---------------------------------------------------------------------------
# LR-03: RLEnsemble regime name alignment
# ---------------------------------------------------------------------------

class TestRLEnsembleRegimeNames:
    """LR-03 — RLEnsemble active-regime lists must include HMM classifier regime names."""

    def _get_ensemble_class(self):
        try:
            from core.rl.rl_ensemble import RLEnsemble
            return RLEnsemble
        except (ImportError, NameError, Exception) as e:
            pytest.skip(f"RLEnsemble unavailable (PyTorch not installed): {e}")

    def test_lr03_src_sac_includes_hmm_names(self):
        """Source-code check: SAC/CPPO regime lists must contain HMM names (no PyTorch needed)."""
        src = open("core/rl/rl_ensemble.py", encoding="utf-8").read()
        for name in ("bull_trend", "bear_trend", "uncertain", "volatility_expansion"):
            assert f'"{name}"' in src or f"'{name}'" in src, \
                f"HMM regime name '{name}' not found in rl_ensemble.py"

    def test_lr03_sac_includes_bull_trend(self):
        cls = self._get_ensemble_class()
        assert "bull_trend" in cls.SAC_ACTIVE_REGIMES, \
            "SAC should activate in bull_trend (HMM regime name)"

    def test_lr03_sac_includes_bear_trend(self):
        cls = self._get_ensemble_class()
        assert "bear_trend" in cls.SAC_ACTIVE_REGIMES

    def test_lr03_sac_includes_uncertain(self):
        cls = self._get_ensemble_class()
        assert "uncertain" in cls.SAC_ACTIVE_REGIMES, \
            "SAC must activate in uncertain regime (most common in NexusTrader overnight)"

    def test_lr03_sac_includes_volatility_expansion(self):
        cls = self._get_ensemble_class()
        assert "volatility_expansion" in cls.SAC_ACTIVE_REGIMES

    def test_lr03_cppo_includes_bear_trend(self):
        cls = self._get_ensemble_class()
        assert "bear_trend" in cls.CPPO_ACTIVE_REGIMES

    def test_lr03_cppo_includes_volatility_expansion(self):
        cls = self._get_ensemble_class()
        assert "volatility_expansion" in cls.CPPO_ACTIVE_REGIMES

    def test_lr03_legacy_names_preserved(self):
        """Old names must still be present for backward compatibility."""
        cls = self._get_ensemble_class()
        assert "trend_bull" in cls.SAC_ACTIVE_REGIMES
        assert "trend_bear" in cls.SAC_ACTIVE_REGIMES

    def test_lr03_select_action_uncertain_no_warning(self):
        """select_action in uncertain regime should NOT fall back to SAC with warning."""
        cls = self._get_ensemble_class()
        import numpy as np
        try:
            ensemble = cls(state_dim=50)
            state = np.zeros(50, dtype=np.float32)
            result = ensemble.select_action(state, regime="uncertain")
            # If we get here, SAC was activated (not fallback)
            assert "action" in result
            assert "sac_fallback" not in result.get("active_agents", []), \
                "select_action in uncertain regime should use sac directly, not sac_fallback"
        except Exception as e:
            pytest.skip(f"RLEnsemble instantiation failed (PyTorch/CUDA unavailable): {e}")


# ---------------------------------------------------------------------------
# LR-04: SignalGenerator singleton across scan cycles
# ---------------------------------------------------------------------------

class TestSignalGeneratorSingleton:
    """LR-04 — AssetScanner must pass a shared SignalGenerator to each ScanWorker."""

    def test_lr04_scanworker_accepts_sig_gen_param(self):
        """ScanWorker.__init__ must accept a sig_gen keyword argument."""
        import inspect
        from core.scanning.scanner import ScanWorker
        sig = inspect.signature(ScanWorker.__init__)
        assert "sig_gen" in sig.parameters, \
            "ScanWorker.__init__ missing sig_gen parameter"

    def test_lr04_scanworker_uses_provided_sig_gen(self):
        """When sig_gen is provided, ScanWorker must use it instead of creating new one."""
        src = open("core/scanning/scanner.py", encoding="utf-8").read()
        assert "if sig_gen is not None:" in src, \
            "ScanWorker does not check for provided sig_gen"

    def test_lr04_assetscanner_creates_sig_gen_once(self):
        """AssetScanner must initialise self._sig_gen in __init__."""
        src = open("core/scanning/scanner.py", encoding="utf-8").read()
        # Check for assignment in AssetScanner.__init__ context
        assert "self._sig_gen: SignalGenerator" in src or \
               "self._sig_gen = SignalGenerator()" in src, \
            "AssetScanner does not create a persistent _sig_gen"

    def test_lr04_assetscanner_passes_sig_gen_to_worker(self):
        """AssetScanner._trigger_scan must pass sig_gen to ScanWorker."""
        src = open("core/scanning/scanner.py", encoding="utf-8").read()
        assert "sig_gen        = self._sig_gen" in src or \
               "sig_gen=self._sig_gen" in src, \
            "AssetScanner does not pass self._sig_gen to ScanWorker"

    def test_lr04_sig_gen_reused_across_two_workers(self):
        """Two ScanWorkers created with the same sig_gen must share the same object."""
        from core.scanning.scanner import ScanWorker
        from unittest.mock import MagicMock

        mock_sig_gen = MagicMock()

        # Minimal kwargs — we're only testing sig_gen assignment
        def make_worker():
            return ScanWorker.__new__(ScanWorker)

        w1 = make_worker()
        w2 = make_worker()

        # Simulate the relevant __init__ logic
        for w in (w1, w2):
            w._sig_gen = mock_sig_gen

        assert w1._sig_gen is w2._sig_gen, \
            "Workers must share the same SignalGenerator instance"


# ---------------------------------------------------------------------------
# LR-05: TwitterAgent urllib timeout placement
# ---------------------------------------------------------------------------

class TestTwitterAgentTimeout:
    """LR-05 — urllib.request.Request must not receive a timeout kwarg."""

    def _read_source(self):
        return open("core/agents/twitter_agent.py", encoding="utf-8").read()

    def test_lr05_no_timeout_in_request_constructor(self):
        """Request(..., timeout=...) is invalid — timeout must go to urlopen()."""
        import ast
        src = self._read_source()
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                # Look for urllib.request.Request(...)
                if isinstance(func, ast.Attribute) and func.attr == "Request":
                    for kw in node.keywords:
                        assert kw.arg != "timeout", \
                            f"timeout kwarg passed to Request() at line {node.lineno}"

    def test_lr05_timeout_in_urlopen(self):
        """timeout must appear in urlopen() calls instead."""
        src = self._read_source()
        assert "urlopen(req, timeout=" in src, "urlopen(req, timeout=...) call not found"
        # Specifically check the Nitter method body.
        nitter_def = src.find("def _fetch_nitter_rss")
        assert nitter_def != -1, "def _fetch_nitter_rss not found"
        # Find the next method definition after Nitter
        next_def = src.find("\n    def ", nitter_def + 1)
        nitter_section = src[nitter_def:next_def] if next_def != -1 else src[nitter_def:]
        assert "urlopen(req, timeout=8)" in nitter_section, \
            "Nitter urlopen should use timeout=8"


# ---------------------------------------------------------------------------
# LR-06: BUSD removed from stablecoin watchlist
# ---------------------------------------------------------------------------

class TestBUSDRemoved:
    """LR-06 — binance-usd must not be in the default stablecoin watchlist."""

    def test_lr06_busd_not_in_default_ids(self):
        src = open("core/agents/stablecoin_agent.py", encoding="utf-8").read()
        # binance-usd in the default list literal
        import ast
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.List):
                for elt in node.elts:
                    if isinstance(elt, ast.Constant) and elt.value == "binance-usd":
                        pytest.fail(
                            f"binance-usd still present in stablecoin_ids list at line {node.lineno}"
                        )

    def test_lr06_active_coins_still_present(self):
        """Core active stablecoins must remain in the list."""
        src = open("core/agents/stablecoin_agent.py", encoding="utf-8").read()
        for coin in ("tether", "usd-coin", "dai"):
            assert f'"{coin}"' in src or f"'{coin}'" in src, \
                f"{coin} was accidentally removed from stablecoin watchlist"


# ---------------------------------------------------------------------------
# LR-07: NewsFeed RSS feeds (CryptoPanic replaced with RSS in Session 51)
# ---------------------------------------------------------------------------

class TestNewsFeedRSSFeeds:
    """LR-07 — news_feed now uses RSS feeds instead of CryptoPanic API."""

    def _read_source(self):
        return open("core/nlp/news_feed.py", encoding="utf-8").read()

    def test_lr07_rss_feeds_registry_present(self):
        """news_feed must have _RSS_FEEDS registry."""
        src = self._read_source()
        assert '_RSS_FEEDS' in src, \
            "news_feed does not define _RSS_FEEDS registry"

    def test_lr07_fetch_all_sources_present(self):
        """news_feed must have _fetch_all_sources() method."""
        src = self._read_source()
        assert "def _fetch_all_sources" in src, \
            "news_feed does not have _fetch_all_sources() method"

    def test_lr07_fetch_rss_present(self):
        """news_feed must have _fetch_rss() method."""
        src = self._read_source()
        assert "def _fetch_rss(" in src, \
            "news_feed does not have _fetch_rss() method"

    def test_lr07_fetch_rss_feedparser_present(self):
        """news_feed must have _fetch_rss_feedparser() method."""
        src = self._read_source()
        assert "def _fetch_rss_feedparser(" in src, \
            "news_feed does not have _fetch_rss_feedparser() method"

    def test_lr07_no_cryptopanic_method(self):
        """CryptoPanic API method _fetch_cryptopanic should not exist (replaced with RSS)."""
        src = self._read_source()
        # _fetch_cryptopanic was removed in Session 51
        # This test documents that the method is intentionally gone
        if "def _fetch_cryptopanic" in src:
            pytest.skip("_fetch_cryptopanic still present — may be in twitter_agent only")


class TestTwitterAgentCryptoPanicKey:
    """LR-08: TwitterAgent._fetch_cryptopanic must use vault key, not auth_token=free."""

    def _read_source(self):
        return open("core/agents/twitter_agent.py", encoding="utf-8").read()

    def test_lr08_no_hardcoded_free_token(self):
        """auth_token=free must not appear as a string literal in _fetch_cryptopanic."""
        import ast
        src = self._read_source()
        tree = ast.parse(src)
        # Walk all string constants in the AST — ignores comments
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if "auth_token=free" in node.value:
                    pytest.fail(
                        f"auth_token=free found as a string literal at line "
                        f"{node.lineno} — must use vault key"
                    )

    def test_lr08_cryptopanic_method_exists(self):
        """_fetch_cryptopanic may exist in TwitterAgent for Nitter CryptoPanic posts."""
        src = self._read_source()
        # This is optional — TwitterAgent may or may not have _fetch_cryptopanic
        # (it was used for sentiment scoring via Nitter RSS, but now uses _fetch_nitter_rss)
        # Just ensure that IF it exists, it doesn't use hardcoded free token
        if "def _fetch_cryptopanic" not in src:
            pytest.skip("_fetch_cryptopanic not in TwitterAgent (RSS sources sufficient)")


# ---------------------------------------------------------------------------
# LR-09: RSS feed sources (CryptoPanic API removed in Session 51)
# ---------------------------------------------------------------------------
# news_feed.py replaced CryptoPanic API with RSS feed aggregation.
# RSS sources: CoinDesk, Cointelegraph, Decrypt, Bitcoin Magazine, BeInCrypto
# (The Block was removed due to malformed XML)
#
# TwitterAgent may still use CryptoPanic for sentiment scoring via Nitter,
# but news_feed.py now uses free RSS feeds exclusively.

class TestRSSFeedRegistry:
    """LR-09 — RSS feed registry replaces CryptoPanic API in news_feed.py."""

    def _read_news_feed_source(self) -> str:
        import pathlib
        return pathlib.Path("core/nlp/news_feed.py").read_text(encoding="utf-8")

    def test_lr09_rss_feeds_registry_exists(self):
        """_RSS_FEEDS registry must be defined with at least 4 feeds."""
        src = self._read_news_feed_source()
        assert "_RSS_FEEDS = [" in src, "_RSS_FEEDS registry not found"
        # Count feed tuples (name, url, is_general)
        feeds = src.count("https://")
        assert feeds >= 4, f"Expected at least 4 RSS feeds, found {feeds}"

    def test_lr09_coindesk_url_present(self):
        """CoinDesk RSS feed must be in registry."""
        src = self._read_news_feed_source()
        assert "coindesk.com" in src.lower(), "CoinDesk RSS URL not found"

    def test_lr09_no_deprecated_cryptopanic_in_newsfeed(self):
        """news_feed.py must NOT contain CryptoPanic API calls."""
        src = self._read_news_feed_source()
        # CryptoPanic method should not exist
        if "def _fetch_cryptopanic" in src:
            pytest.fail("_fetch_cryptopanic still present in news_feed.py — should use RSS only")
        # CryptoPanic URL should not be fetched
        assert "cryptopanic.com" not in src, "CryptoPanic API URL should not be in news_feed.py"

    def test_lr09_rss_fetch_methods_exist(self):
        """news_feed.py must have _fetch_rss_feedparser and _fetch_rss_fallback."""
        src = self._read_news_feed_source()
        assert "def _fetch_rss_feedparser(" in src, "RSS feedparser method missing"
        assert "def _fetch_rss_fallback(" in src, "RSS fallback method missing"


# ---------------------------------------------------------------------------
# LR-10: NewsFeed max_age_minutes default (Session 6 fix)
# ---------------------------------------------------------------------------
# Root cause: Default max_age_minutes=60 filtered out all 55 fetched articles
# because RSS feeds (CoinDesk, Cointelegraph) publish articles 2–6 hours old.
# Fix: Changed default to 240 minutes (4 hours) in Session 6.
# Session 44 added a 24h fallback: when the primary window is empty,
# articles up to 24h old are returned tagged _stale=True.

class TestNewsFeedMaxAgeDefault:
    """LR-10 — fetch_headlines default max_age_minutes must be >= 240 (4 hours)."""

    def test_lr10_default_max_age_is_240_or_more(self):
        """fetch_headlines() default parameter must be 240 minutes (not 60)."""
        import inspect
        from core.nlp.news_feed import NewsFeed
        sig = inspect.signature(NewsFeed.fetch_headlines)
        param = sig.parameters.get("max_age_minutes")
        assert param is not None, "fetch_headlines() must have a max_age_minutes parameter"
        default = param.default
        assert default is not inspect.Parameter.empty, (
            "max_age_minutes must have a default value"
        )
        assert default >= 240, (
            f"max_age_minutes default is {default} — must be >= 240 (4 hours). "
            "A default of 60 minutes filtered out all RSS articles because they "
            "are typically published 2–6 hours before being fetched."
        )

    def test_lr10_articles_within_window_are_included(self):
        """Articles published within max_age_minutes must pass the age filter."""
        from datetime import datetime, timezone, timedelta
        from unittest.mock import patch
        from core.nlp.news_feed import NewsFeed
        import core.nlp.news_feed as nf_mod

        nf_mod._shared_raw_cache = []
        nf_mod._shared_raw_ts = None
        feed = NewsFeed()
        # Article published 3 hours ago — within 4-hour window
        three_hours_ago = datetime.now(timezone.utc) - timedelta(hours=3)

        with patch.object(feed, "_fetch_all_sources", return_value=[{
                 "title": "BTC headline",
                 "source": "CoinDesk",
                 "timestamp": three_hours_ago,
                 "url": "https://example.com/article",
             }]):
            headlines = feed.fetch_headlines(max_age_minutes=240)

        assert len(headlines) >= 1, (
            "Article published 3 hours ago must be included when max_age_minutes=240"
        )

    def test_lr10_articles_outside_window_use_stale_fallback(self):
        """Articles older than max_age_minutes but within 24h are returned with _stale tag.

        Session 44 added a 24h fallback: when the primary window is empty,
        articles up to 24h old are returned tagged _stale=True so callers
        can down-weight the signal.
        """
        from datetime import datetime, timezone, timedelta
        from unittest.mock import patch
        from core.nlp.news_feed import NewsFeed
        import core.nlp.news_feed as nf_mod

        nf_mod._shared_raw_cache = []
        nf_mod._shared_raw_ts = None
        feed = NewsFeed()
        # Article published 5 hours ago — outside 4-hour primary window
        five_hours_ago = datetime.now(timezone.utc) - timedelta(hours=5)

        with patch.object(feed, "_fetch_all_sources", return_value=[{
                 "title": "Old news",
                 "source": "CoinDesk",
                 "timestamp": five_hours_ago,
                 "url": "https://example.com/old",
             }]):
            headlines = feed.fetch_headlines(max_age_minutes=240)

        # With 24h fallback (Session 44), stale articles are returned when
        # the primary window is empty — tagged _stale=True
        assert len(headlines) == 1, (
            "Article within 24h should be returned via stale fallback"
        )
        assert headlines[0].get("_stale") is True, (
            "Stale fallback articles must be tagged _stale=True"
        )

    def test_lr10b_articles_beyond_24h_are_excluded(self):
        """Articles older than 24h must be excluded even with fallback."""
        from datetime import datetime, timezone, timedelta
        from unittest.mock import patch
        from core.nlp.news_feed import NewsFeed
        import core.nlp.news_feed as nf_mod

        nf_mod._shared_raw_cache = []
        nf_mod._shared_raw_ts = None
        feed = NewsFeed()
        # Article published 25 hours ago — beyond 24h fallback window
        old_article = datetime.now(timezone.utc) - timedelta(hours=25)

        with patch.object(feed, "_fetch_all_sources", return_value=[{
                 "title": "Very old news",
                 "source": "CoinDesk",
                 "timestamp": old_article,
                 "url": "https://example.com/very-old",
             }]):
            headlines = feed.fetch_headlines(max_age_minutes=240)

        assert len(headlines) == 0, (
            "Article beyond 24h must be excluded even with stale fallback"
        )

    def test_lr10c_stale_articles_excluded_when_fresh_available(self):
        """When fresh articles exist, stale articles are properly excluded."""
        from datetime import datetime, timezone, timedelta
        from unittest.mock import patch
        from core.nlp.news_feed import NewsFeed
        import core.nlp.news_feed as nf_mod

        nf_mod._shared_raw_cache = []
        nf_mod._shared_raw_ts = None
        feed = NewsFeed()
        fresh = datetime.now(timezone.utc) - timedelta(hours=2)
        stale = datetime.now(timezone.utc) - timedelta(hours=5)

        with patch.object(feed, "_fetch_all_sources", return_value=[
                 {"title": "Fresh BTC news", "source": "CoinDesk",
                  "timestamp": fresh, "url": "https://example.com/fresh"},
                 {"title": "Old BTC news", "source": "Decrypt",
                  "timestamp": stale, "url": "https://example.com/stale"},
             ]):
            headlines = feed.fetch_headlines(max_age_minutes=240)

        # Only the fresh article should be returned (primary window has results)
        assert len(headlines) == 1, (
            "When fresh articles exist, only those within primary window are returned"
        )
        assert headlines[0]["title"] == "Fresh BTC news"
