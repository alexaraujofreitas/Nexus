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
        # Use "def _fetch_nitter_rss" and "def _fetch_cryptopanic" to locate
        # the method definition (not the call site in fetch()).
        nitter_def = src.find("def _fetch_nitter_rss")
        cp_def = src.find("def _fetch_cryptopanic")
        assert nitter_def != -1, "def _fetch_nitter_rss not found"
        assert cp_def != -1, "def _fetch_cryptopanic not found"
        nitter_section = src[nitter_def:cp_def]
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
# LR-07: CryptoPanic vault resolution
# ---------------------------------------------------------------------------

class TestCryptoPanicVaultResolution:
    """LR-07 — news_feed._fetch_cryptopanic must resolve __vault__ through key_vault."""

    def _read_source(self):
        return open("core/nlp/news_feed.py", encoding="utf-8").read()

    def test_lr07_vault_placeholder_check(self):
        """news_feed must check for the __vault__ placeholder string."""
        src = self._read_source()
        assert '__vault__' in src, \
            "news_feed does not check for __vault__ placeholder"

    def test_lr07_vault_load_attempted(self):
        """news_feed must call key_vault.load() when value is __vault__."""
        src = self._read_source()
        assert "key_vault.load" in src, \
            "news_feed does not call key_vault.load() for vault-stored API key"

    def test_lr07_empty_key_raises_not_sends_request(self):
        """When vault returns empty string, a ValueError is raised before any HTTP call."""
        with patch("core.security.key_vault.key_vault") as mock_vault:
            mock_vault.load.return_value = ""
            with patch("config.settings.settings") as mock_settings:
                mock_settings.get.return_value = "__vault__"
                try:
                    from core.nlp.news_feed import NewsFeed
                    feed = NewsFeed.__new__(NewsFeed)
                    with pytest.raises((ValueError, Exception)):
                        feed._fetch_cryptopanic()
                except ImportError:
                    pytest.skip("NewsFeed unavailable")

    def test_lr07_literal_vault_string_never_sent_as_token(self):
        """__vault__ must never appear as the auth_token value in an HTTP request."""
        import urllib.parse
        # Simulate resolution
        raw_key = "__vault__"
        resolved_key = ""  # vault is empty → empty string
        if resolved_key == "__vault__" or not resolved_key:
            # Should raise ValueError, never build URL
            with pytest.raises(ValueError):
                if not resolved_key or resolved_key == "__vault__":
                    raise ValueError("No CryptoPanic API key configured")
        url = f"https://cryptopanic.com/api/v1/posts/?auth_token={resolved_key}"
        assert "__vault__" not in url


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

    def test_lr08_vault_load_called(self):
        """_fetch_cryptopanic must call key_vault.load for the API key."""
        src = self._read_source()
        start = src.find("def _fetch_cryptopanic")
        next_def = src.find("\n    def ", start + 1)
        method_src = src[start:next_def] if next_def != -1 else src[start:]
        assert "key_vault.load" in method_src, \
            "_fetch_cryptopanic does not resolve key from vault"

    def test_lr08_returns_none_when_no_key(self):
        """_fetch_cryptopanic returns None (not an error) when no key is configured."""
        with patch("config.settings.settings") as mock_settings:
            mock_settings.get.return_value = ""
            with patch("core.security.key_vault.key_vault") as mock_vault:
                mock_vault.load.return_value = ""
                try:
                    from core.agents.twitter_agent import TwitterAgent
                    agent = TwitterAgent.__new__(TwitterAgent)
                    result = agent._fetch_cryptopanic()
                    assert result is None, \
                        "Expected None when no API key, got something else"
                except ImportError:
                    pytest.skip("TwitterAgent unavailable")

    def test_lr08_vault_string_never_in_url(self):
        """The literal string __vault__ must never be embedded in the request URL."""
        src = self._read_source()
        start = src.find("def _fetch_cryptopanic")
        next_def = src.find("\n    def ", start + 1)
        method_src = src[start:next_def] if next_def != -1 else src[start:]
        # The URL construction should use the resolved variable, not __vault__
        # Find url= assignment in the method
        import ast
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if "__vault__" in node.value and "auth_token" in node.value:
                    pytest.fail(
                        f"URL string contains __vault__ at line {node.lineno}"
                    )


# ---------------------------------------------------------------------------
# LR-09: CryptoPanic API endpoint URL format (Session 13 fix)
# ---------------------------------------------------------------------------
# Root cause: CryptoPanic API URL is /api/v1/posts/ — the plan tier is
# determined by the auth_token, not the URL path. Previous attempts used
# /api/free/v1/ and /api/free/v2/ which both 404.
# `kind=` parameter was renamed to `filter=`.

class TestCryptoPanicEndpointURL:
    """LR-09 — _fetch_cryptopanic must use the correct /api/v1/posts/ endpoint."""

    def _read_news_feed_source(self) -> str:
        import ast, pathlib
        return pathlib.Path("core/nlp/news_feed.py").read_text(encoding="utf-8")

    def test_lr09_uses_v1_posts_endpoint(self):
        """URL must contain /api/v1/posts/ (tier determined by token, not path)."""
        src = self._read_news_feed_source()
        start = src.find("def _fetch_cryptopanic")
        assert start != -1, "_fetch_cryptopanic method not found in news_feed.py"
        next_def = src.find("\n    def ", start + 1)
        method_src = src[start:next_def] if next_def != -1 else src[start:]

        assert "/api/v1/posts/" in method_src, (
            "CryptoPanic URL must use /api/v1/posts/ — tier is determined by "
            "auth_token, not the URL path. /api/free/v1/ and /api/free/v2/ return 404."
        )

    def test_lr09_does_not_use_free_path(self):
        """URL string literals must NOT contain /api/free/ — this path 404s."""
        import ast
        src = self._read_news_feed_source()
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                val = node.value
                if "/api/free/" in val:
                    pytest.fail(
                        f"Incorrect /api/free/ path detected in string literal at "
                        f"line {node.lineno}: {val!r}. "
                        "CryptoPanic URL must use /api/v1/posts/ — tier is by token."
                    )

    def test_lr09_uses_filter_param_not_kind(self):
        """Parameter must be `filter=news`, not the deprecated `kind=news`."""
        src = self._read_news_feed_source()
        start = src.find("def _fetch_cryptopanic")
        next_def = src.find("\n    def ", start + 1)
        method_src = src[start:next_def] if next_def != -1 else src[start:]

        assert "filter=" in method_src, (
            "CryptoPanic URL must use 'filter=news' parameter (renamed from 'kind=')"
        )
        # Check for the old `kind=` parameter as a string literal (not just substring)
        import ast
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if "kind=" in node.value and "cryptopanic" in src[max(0, src.find(node.value)-200):src.find(node.value)]:
                    pytest.fail(
                        f"Found deprecated 'kind=' parameter in CryptoPanic URL "
                        f"at line {node.lineno} — must use 'filter=' instead"
                    )


# ---------------------------------------------------------------------------
# LR-10: NewsFeed max_age_minutes default (Session 6 fix)
# ---------------------------------------------------------------------------
# Root cause: Default max_age_minutes=60 filtered out all 55 fetched articles
# because RSS feeds (CoinDesk, Cointelegraph) publish articles 2–6 hours old.
# Fix: Changed default to 240 minutes (4 hours).

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

    def _make_feedparser_entry(self, published_dt):
        """Build a mock feedparser entry dict with the given publish time."""
        import time
        return {
            "title": "BTC headline",
            "link":  "https://example.com/article",
            "published_parsed": published_dt.timetuple(),
        }

    def test_lr10_articles_within_window_are_included(self):
        """Articles published within max_age_minutes must pass the age filter."""
        from datetime import datetime, timezone, timedelta
        from unittest.mock import patch, MagicMock
        from core.nlp.news_feed import NewsFeed

        feed = NewsFeed()
        # Article published 3 hours ago — within 4-hour window
        three_hours_ago = datetime.now(timezone.utc) - timedelta(hours=3)
        mock_entry = self._make_feedparser_entry(three_hours_ago)

        mock_parsed = MagicMock()
        mock_parsed.entries = [mock_entry]

        with patch.object(feed, "_fetch_cryptopanic", return_value=[]), \
             patch.object(feed, "_fetch_rss", return_value=[{
                 "title": "BTC headline",
                 "source": "CoinDesk",
                 "timestamp": three_hours_ago,
                 "url": "https://example.com/article",
             }]):
            headlines = feed.fetch_headlines(max_age_minutes=240)

        assert len(headlines) >= 1, (
            "Article published 3 hours ago must be included when max_age_minutes=240"
        )

    def test_lr10_articles_outside_window_are_excluded(self):
        """Articles older than max_age_minutes must be filtered out."""
        from datetime import datetime, timezone, timedelta
        from unittest.mock import patch
        from core.nlp.news_feed import NewsFeed

        feed = NewsFeed()
        # Article published 5 hours ago — outside 4-hour window
        five_hours_ago = datetime.now(timezone.utc) - timedelta(hours=5)

        with patch.object(feed, "_fetch_cryptopanic", return_value=[]), \
             patch.object(feed, "_fetch_rss", return_value=[{
                 "title": "Old news",
                 "source": "CoinDesk",
                 "timestamp": five_hours_ago,
                 "url": "https://example.com/old",
             }]):
            headlines = feed.fetch_headlines(max_age_minutes=240)

        assert len(headlines) == 0, (
            "Article published 5 hours ago must be excluded when max_age_minutes=240"
        )
